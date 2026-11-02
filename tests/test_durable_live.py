"""Audio durability and recoverable subtitles without a GPU or network service."""
import asyncio
from collections import Counter
import copy
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from yakiniku.audio import SegmentedWaveWriter
from yakiniku.live_pipeline import (
    ASR_QUEUE_LIMIT, TRANSLATION_QUEUE_LIMIT, DurableLiveSession, new_segment, pending_count,
    read_audio_range, recover_tail, retry_live_subtitles,
)
from yakiniku.storage import SessionStore


def pcm(value, milliseconds=100, rate=8000, channels=1):
    return struct.pack("<h", value) * round(rate * milliseconds / 1000) * channels


class Socket:
    def __init__(self):
        self.events = []
        self.audio_saved = asyncio.Event()
        self.changed = asyncio.Event()

    async def send_json(self, event):
        self.events.append(copy.deepcopy(event))
        self.changed.set()
        if event.get("type") == "audio_saved":
            self.audio_saved.set()

    async def wait_event(self, predicate):
        async def wait():
            while True:
                for event in self.events:
                    if predicate(event):
                        return event
                self.changed.clear()
                await self.changed.wait()
        return await asyncio.wait_for(wait(), 3)


class Runtime:
    def __init__(self, *, block=False, results=None):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.results = results or {}

    def status(self, **options):
        return {"state": "ready"}

    async def transcribe_pcm(self, data, *, sample_rate, channels, **options):
        value = struct.unpack_from("<h", data)[0]
        self.calls.append({"value": value, "pcm": data, "rate": sample_rate, "channels": channels, **options})
        self.started.set()
        await self.release.wait()
        result = self.results.get(value)
        if isinstance(result, BaseException):
            raise result
        return result if result is not None else {"status": "done", "ja": f"原文{value}"}


def translation_factory(calls, *, results=None, gate=None):
    results = results or {}

    class Translator:
        def __init__(self, **options):
            self.options = options

        async def correct(self, text, context=None):
            raise AssertionError("Live must not perform an independent proofreading request")

        async def translate(self, text, context=None):
            calls.append({"text": text, "context": copy.deepcopy(context or [])})
            if gate is not None:
                await gate.wait()
            result = results.get(text)
            if isinstance(result, BaseException):
                raise result
            return result if result is not None else f"中文{text}"

        async def close(self):
            pass

    return Translator


class DurableLiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temporary.name))
        self.session = self.store.create("fake live", sample_rate=8000, channels=1)
        self.session["live_snapshot"] = {"asr": {"backend": "mlx", "model": "mlx-whisper-small"}}
        self.settings = {"live_asr_backend": "mlx", "live_asr_model": "mlx-whisper-small",
                         "segment_seconds": 0.1, "silence_ms": 60, "glossary": "名前=姓名",
                         "translation_model": "chosen-text-model", "translation_api": "responses",
                         "translation_base_url": "https://example.invalid/v1", "translation_api_key": "test-only-key"}
        self.socket = Socket()
        self.calls = []
        self.lives = []

    async def asyncTearDown(self):
        for live in self.lives:
            if live.writer is not None and not live.writer.closed:
                live.writer.close()
            tasks = ([live.asr_task] if live.asr_task else []) + live.translation_tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.temporary.cleanup()

    async def start(self, runtime=None, factory=None):
        runtime = runtime or Runtime()
        live = DurableLiveSession(self.session, self.settings, self.store, self.socket, runtime,
                                  factory or translation_factory(self.calls))
        self.lives.append(live)
        await live.start()
        return live, runtime

    async def send_turn(self, live, value, milliseconds=100):
        await live.receive_audio(b"\x00" + pcm(value, milliseconds))
        await live.receive_audio(b"\x01" + pcm(value, milliseconds, rate=24000))

    async def test_overload_is_durable_stop_confirms_audio_before_slow_asr_then_backfills_once(self):
        live, runtime = await self.start(Runtime(block=True))
        await self.send_turn(live, 1000)
        await asyncio.wait_for(runtime.started.wait(), 1)
        for value in range(1001, 1007):
            await self.send_turn(live, value)
        self.assertEqual(live.asr_queue.qsize(), ASR_QUEUE_LIMIT)
        deferred = [s for s in self.session["segments"] if s["asr_status"] == "deferred"]
        self.assertEqual(len(deferred), 3)
        self.assertTrue(all(s["audio_durable"] for s in self.session["segments"]))

        stopped = asyncio.create_task(live.finish())
        await asyncio.wait_for(self.socket.audio_saved.wait(), 1)
        self.assertFalse(stopped.done())
        saved_event = next(event for event in self.socket.events if event["type"] == "audio_saved")
        self.assertTrue(saved_event["session"]["audio_saved"])
        self.assertTrue(live.writer.closed)
        with wave.open(str(self.store.file_path(self.session["id"], "audio.wav")), "rb") as reader:
            self.assertEqual(reader.getnframes(), 5600)
            self.assertEqual(reader.readframes(5600), b"".join(pcm(value) for value in range(1000, 1007)))

        runtime.release.set()
        await asyncio.wait_for(stopped, 5)
        self.assertEqual([call["value"] for call in runtime.calls], list(range(1000, 1007)))
        self.assertEqual(Counter(call["text"] for call in self.calls), Counter(f"原文{value}" for value in range(1000, 1007)))
        self.assertEqual(self.session["status"], "completed")
        self.assertEqual(pending_count(self.session), 0)
        self.assertEqual(len(self.session["segments"]), 7)
        self.assertEqual([s["sequence"] for s in self.session["segments"]], list(range(1, 8)))
        self.assertTrue(all(event["session_id"] == self.session["id"] for event in self.socket.events))

        identifiers = [s["id"] for s in self.session["segments"]]
        await retry_live_subtitles(self.store, self.session, self.settings, runtime, translation_factory(self.calls))
        self.assertEqual([s["id"] for s in self.session["segments"]], identifiers)
        self.assertEqual(len(runtime.calls), 7)
        self.assertEqual(len(self.calls), 7)

    async def test_confirmed_no_speech_is_terminal_and_never_translated(self):
        live, runtime = await self.start(Runtime(results={1000: {"status": "no_speech", "ja": ""}}))
        await self.send_turn(live, 1000)
        await live.finish()
        segment = self.session["segments"][0]
        self.assertEqual(segment["asr_status"], "no_speech")
        self.assertEqual(segment["translation_status"], "skipped")
        self.assertEqual(self.calls, [])
        self.assertEqual(pending_count(self.session), 0)
        self.assertTrue(self.session["audio_saved"])
        self.assertEqual(self.session["status"], "completed")

    async def test_translation_backlog_is_bounded_and_only_missing_text_is_backfilled(self):
        gate = asyncio.Event()
        live, runtime = await self.start(factory=translation_factory(self.calls, gate=gate))
        for value in range(1000, 1012):
            await self.send_turn(live, value)
            await live.asr_queue.join()
            await asyncio.sleep(0)
        self.assertEqual(live.translation_queue.qsize(), TRANSLATION_QUEUE_LIMIT)
        self.assertEqual(sum(s["translation_status"] == "deferred" for s in self.session["segments"]), 3)
        self.assertTrue(all(s["asr_status"] == "done" for s in self.session["segments"]))
        stopped = asyncio.create_task(live.finish())
        await asyncio.wait_for(self.socket.audio_saved.wait(), 1)
        self.assertFalse(stopped.done())
        gate.set()
        await asyncio.wait_for(stopped, 5)
        self.assertEqual(len(runtime.calls), 12)
        self.assertEqual(Counter(call["text"] for call in self.calls), Counter(f"原文{value}" for value in range(1000, 1012)))
        self.assertEqual(self.session["status"], "completed")

    async def test_translation_out_of_order_updates_original_ids_and_history_order(self):
        first_release, second_release = asyncio.Event(), asyncio.Event()
        calls = []

        class OutOfOrderTranslator:
            def __init__(self, **options):
                pass

            async def translate(self, text, context=None):
                calls.append(text)
                if text == "原文1000":
                    await first_release.wait()
                elif text == "原文1001":
                    await second_release.wait()
                return "中文" + text

            async def close(self):
                pass

        live, runtime = await self.start(factory=OutOfOrderTranslator)
        for value in range(1000, 1003):
            await self.send_turn(live, value)
            await live.asr_queue.join()
            await asyncio.sleep(0)
        await self.socket.wait_event(lambda event: event.get("type") == "translation"
                                     and event["segment"].get("sequence") == 3)
        first_release.set()
        await self.socket.wait_event(lambda event: event.get("type") == "translation"
                                     and event["segment"].get("sequence") == 1)
        second_release.set()
        await live.finish()
        translated = [event["segment"] for event in self.socket.events
                      if event["type"] == "translation" and event["segment"].get("translation_status") == "done"]
        self.assertEqual([segment["sequence"] for segment in translated], [3, 1, 2])
        self.assertEqual([segment["sequence"] for segment in self.session["segments"]], [1, 2, 3])
        self.assertEqual([segment["zh"] for segment in self.session["segments"]],
                         ["中文原文1000", "中文原文1001", "中文原文1002"])

    async def test_empty_asr_preserves_audio_and_explicit_retry_updates_same_segment_only(self):
        live, runtime = await self.start(Runtime(results={1000: {"status": "done", "ja": ""}}))
        await self.send_turn(live, 1000)
        await self.send_turn(live, 1001)
        await live.finish()
        failed, good = self.session["segments"]
        self.assertEqual(failed["asr_status"], "failed")
        self.assertEqual(failed["ja_raw"], "")
        self.assertIn("空结果", failed["error"])
        self.assertEqual(good["translation_status"], "done")
        self.assertEqual(self.session["status"], "partial")
        self.assertEqual(len(runtime.calls), 2)  # Failed attempts are not silently repeated at stop.
        identifiers = [s["id"] for s in self.session["segments"]]
        runtime.results.clear()
        await retry_live_subtitles(self.store, self.session, self.settings, runtime, translation_factory(self.calls))
        self.assertEqual([s["id"] for s in self.session["segments"]], identifiers)
        self.assertEqual(len(runtime.calls), 3)
        self.assertEqual(Counter(call["text"] for call in self.calls), Counter(["原文1000", "原文1001"]))
        self.assertEqual(self.session["status"], "completed")
        srt = self.store.file_path(self.session["id"], "zh-CN.srt").read_text()
        self.assertNotIn("空结果", srt)
        self.assertNotIn("错误", srt)

    async def test_stop_recovers_raw_tail_when_browser_recognition_copy_is_short(self):
        live, runtime = await self.start()
        await live.receive_audio(b"\x00" + pcm(1000, 200))
        await live.receive_audio(b"\x01" + pcm(1000, 100, rate=24000))
        await live.finish()
        intervals = [(segment["start_ms"], segment["end_ms"]) for segment in self.session["segments"]]
        self.assertEqual(intervals, [(0, 100), (100, 200)])
        self.assertEqual(self.session["duration_ms"], 200)
        self.assertEqual(self.session["status"], "completed")
        self.assertEqual(sum(len(call["pcm"]) for call in runtime.calls), len(pcm(1000, 200)))

    async def test_recognition_copy_ahead_of_raw_audio_is_not_treated_as_saved(self):
        live, runtime = await self.start()
        await live.receive_audio(b"\x01" + pcm(1000, 100, rate=24000))
        self.assertEqual(self.session["segments"], [])
        self.assertEqual(runtime.calls, [])
        await live.receive_audio(b"\x00" + pcm(1000, 50))
        await live.finish()
        self.assertEqual(self.session["duration_ms"], 50)
        self.assertEqual([(s["start_ms"], s["end_ms"]) for s in self.session["segments"]], [(0, 50)])
        self.assertEqual(len(runtime.calls[0]["pcm"]), len(pcm(1000, 50)))

    async def test_empty_translation_preserves_japanese_and_retry_does_not_repeat_asr(self):
        results = {"原文1000": "   "}
        factory = translation_factory(self.calls, results=results)
        live, runtime = await self.start(factory=factory)
        await self.send_turn(live, 1000)
        await live.finish()
        old = self.session["segments"][0]
        identifier = old["id"]
        self.assertEqual(old["ja_raw"], "原文1000")
        self.assertEqual(old["translation_status"], "failed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.session["status"], "partial")
        results["原文1000"] = "翻译成功"
        await retry_live_subtitles(self.store, self.session, self.settings, runtime, factory)
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.session["segments"][0]["id"], identifier)
        self.assertEqual(self.session["segments"][0]["zh"], "翻译成功")
        self.assertEqual(self.session["segments"][0]["ja"], "原文1000")

    async def test_legacy_retry_preserves_corrected_japanese_and_successful_text_without_asr(self):
        failed = new_segment(self.session, 0, 100)
        failed.update(ja_raw="旧识别", ja="已校对日语", asr_status="done", correction_status="done", translation_status="failed")
        good = new_segment(self.session, 100, 200)
        good.update(ja_raw="原文", ja="已校对完成", zh="旧译文", asr_status="done", translation_status="done")
        self.session["segments"] = [failed, good]
        self.store.export(self.session)
        runtime = Runtime()
        await retry_live_subtitles(self.store, self.session, self.settings, runtime,
                                   translation_factory(self.calls), legacy=True)
        self.assertEqual(runtime.calls, [])
        self.assertEqual([call["text"] for call in self.calls], ["已校对日语"])
        self.assertEqual(self.session["segments"][0]["ja"], "已校对日语")
        self.assertEqual(self.session["segments"][0]["ja_raw"], "旧识别")
        self.assertEqual(self.session["segments"][1]["zh"], "旧译文")


class DurableAudioRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temporary.name))
        self.session = self.store.create("recovery", sample_rate=8000, channels=1)
        self.settings = {"segment_seconds": .1, "silence_ms": 60}

    def tearDown(self):
        self.temporary.cleanup()

    def write_parts(self, values):
        writer = SegmentedWaveWriter(self.store.directory(self.session["id"]), 8000, 1, part_seconds=.1)
        for value in values:
            writer.write(pcm(value))
        writer.close()
        self.session.update(duration_ms=writer.duration_ms, audio_parts=writer.parts)
        self.store.save(self.session)

    def test_global_audio_range_crosses_volumes_without_losing_samples(self):
        self.write_parts([1000, 2000, 3000])
        data, rate, channels = read_audio_range(self.store, self.session, 50, 250)
        self.assertEqual((rate, channels), (8000, 1))
        self.assertEqual(data, pcm(1000, 50) + pcm(2000) + pcm(3000, 50))
        with self.assertRaisesRegex(Exception, "找不到"):
            read_audio_range(self.store, self.session, 300, 400)

    def test_recover_tail_only_segments_uncommitted_durable_audio_and_is_idempotent(self):
        self.write_parts([1000] * 10)
        done = new_segment(self.session, 0, 300)
        done.update(ja_raw="已有原文", ja="已有原文", zh="已有译文", asr_status="done", translation_status="done")
        self.session["segments"] = [done]
        self.session["live_committed_until_ms"] = 300
        self.session["live_recovery_from_ms"] = 300
        recover_tail(self.store, self.session, self.settings)
        tail = self.session["segments"][1:]
        self.assertEqual([(s["start_ms"], s["end_ms"]) for s in tail], [(start, start + 100) for start in range(300, 1000, 100)])
        self.assertTrue(all(s["asr_status"] == "deferred" and s["audio_durable"] for s in tail))
        identifiers = [s["id"] for s in self.session["segments"]]
        recover_tail(self.store, self.session, self.settings)
        self.assertEqual([s["id"] for s in self.session["segments"]], identifiers)
        self.assertEqual(self.session["segments"][0]["zh"], "已有译文")

    def test_process_restart_restores_running_stages_as_deferred_and_preserves_audio(self):
        self.write_parts([1000, 2000, 3000])
        first = new_segment(self.session, 0, 100)
        first.update(asr_status="running")
        self.store.append_segment(self.session, first)
        second = new_segment(self.session, 100, 200)
        second.update(asr_status="done", ja_raw="已有日语", ja="已有日语", translation_status="running")
        self.store.append_segment(self.session, second)
        self.session.update(status="recording", live_snapshot={"asr": {"backend": "mlx"}},
                            live_committed_until_ms=200)
        self.store.save(self.session)
        self.assertEqual(self.store.recover(), 1)
        recovered = self.store.load(self.session["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertTrue(recovered["audio_saved"])
        self.assertEqual(recovered["duration_ms"], 300)
        self.assertEqual(recovered["segments"][0]["asr_status"], "deferred")
        self.assertEqual(recovered["segments"][1]["translation_status"], "deferred")
        self.assertEqual(recovered["segments"][1]["ja_raw"], "已有日语")
        self.assertEqual(recovered["live_recovery_from_ms"], 200)
        recover_tail(self.store, recovered, self.settings)
        self.assertEqual(len(recovered["segments"]), 3)
        self.assertEqual((recovered["segments"][2]["start_ms"], recovered["segments"][2]["end_ms"]), (200, 300))
        self.assertEqual(self.store.recover(), 0)


if __name__ == "__main__":
    unittest.main()
