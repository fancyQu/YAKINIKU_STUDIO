import asyncio
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from yakiniku.provider import ProviderError
from yakiniku.server import LiveSession
from yakiniku.storage import DEFAULT_SETTINGS, SessionStore


class QuietTranscriber:
    def __init__(self, **options):
        pass

    async def connect(self):
        pass

    async def finish(self, timeout=20):
        return True

    async def close(self):
        pass


class TranslationConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SessionStore(Path(self.temporary.name))
        self.session = self.store.create("Concurrent subtitles")
        self.events = []
        self.translated = asyncio.Event()
        owner = self

        class Socket:
            async def send_json(self, event):
                owner.events.append(event)
                if event["type"] == "translation":
                    owner.translated.set()

        self.socket = Socket()

    async def start(self, translator):
        settings = {**DEFAULT_SETTINGS, "transcription_mode": "local", "transcription_model": "tiny",
                    "api_base_url": "https://example.test/v1", "api_key": "private-fixture-key"}
        live = LiveSession(self.session, settings, self.store, self.socket,
                           QuietTranscriber, lambda **options: translator)
        await live.start()
        return live

    async def queue(self, live, count):
        for index in range(count):
            await live.on_provider_event({"type": "transcript", "id": str(index), "ja": f"日本語{index}",
                                          "start_ms": index * 1000, "end_ms": (index + 1) * 1000})

    async def test_three_requests_run_together_and_stop_drains_out_of_order_results(self):
        gates = [asyncio.Event() for _ in range(6)]
        three_started = asyncio.Event()

        class Translator:
            active = 0
            maximum = 0
            closed = False

            async def translate(self, text, context=None):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 3:
                    three_started.set()
                try:
                    await gates[int(text[-1])].wait()
                    return "中文" + text[-1]
                finally:
                    self.active -= 1

            async def close(self):
                self.closed = True

        translator = Translator()
        live = await self.start(translator)
        await self.queue(live, 6)
        await asyncio.wait_for(three_started.wait(), 1)
        self.assertEqual(translator.maximum, 3)
        finishing = asyncio.create_task(live.finish())
        gates[2].set()
        await asyncio.wait_for(self.translated.wait(), 1)
        self.assertEqual([event["segment"]["id"] for event in self.events if event["type"] == "translation"], ["2"])
        self.assertFalse(finishing.done())
        self.assertFalse(translator.closed)
        for gate in gates:
            gate.set()
        await asyncio.wait_for(finishing, 2)
        await asyncio.wait_for(live.translation_queue.join(), 1)
        self.assertTrue(all(task.done() for task in live.translation_tasks))
        self.assertTrue(translator.closed)
        self.assertEqual(translator.active, 0)
        self.assertEqual(translator.maximum, 3)
        self.assertEqual([(s["id"], s["start_ms"], s["end_ms"], s["translation_status"])
                          for s in self.session["segments"]],
                         [(str(i), i * 1000, (i + 1) * 1000, "done") for i in range(6)])
        subtitles = self.store.file_path(self.session["id"], "zh-CN.srt").read_text()
        self.assertEqual([subtitles.index(f"中文{i}") for i in range(6)],
                         sorted(subtitles.index(f"中文{i}") for i in range(6)))
        with wave.open(str(self.store.file_path(self.session["id"], "audio.wav"))) as recording:
            self.assertEqual(recording.getnchannels(), 2)

    async def test_request_exceptions_fail_only_their_turns_and_keep_workers_alive(self):
        class Translator:
            async def translate(self, text, context=None):
                if text.endswith("0"):
                    raise RuntimeError("private upstream details")
                if text.endswith("1"):
                    raise ProviderError("测试服务失败")
                return "中文" + text[-1]

            async def close(self):
                pass

        live = await self.start(Translator())
        await self.queue(live, 8)
        await asyncio.wait_for(live.finish(), 2)
        await asyncio.wait_for(live.translation_queue.join(), 1)
        self.assertEqual([s["translation_status"] for s in self.session["segments"]],
                         ["failed", "failed"] + ["done"] * 6)
        self.assertTrue(all(s["ja"] == f"日本語{i}" for i, s in enumerate(self.session["segments"])))
        self.assertNotIn("private upstream", str(self.session))
        self.assertTrue(all(task.exception() is None for task in live.translation_tasks))

    async def test_hung_request_times_out_without_blocking_other_turns_or_stop(self):
        class Translator:
            cancelled = False

            async def translate(self, text, context=None):
                if text.endswith("0"):
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        self.cancelled = True
                        raise
                return "中文" + text[-1]

            async def close(self):
                pass

        translator = Translator()
        live = await self.start(translator)
        with patch("yakiniku.server.TRANSLATION_REQUEST_TIMEOUT", .02):
            await self.queue(live, 5)
            await asyncio.wait_for(live.finish(), 2)
        await asyncio.wait_for(live.translation_queue.join(), 1)
        self.assertTrue(translator.cancelled)
        self.assertEqual([s["translation_status"] for s in self.session["segments"]], ["failed"] + ["done"] * 4)
        self.assertTrue(any("超时" in message for message in self.session["warnings"]))


if __name__ == "__main__":
    unittest.main()
