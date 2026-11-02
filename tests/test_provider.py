import asyncio
import json
import unittest

import httpx

from yakiniku.provider import ProviderError, RealtimeTranscriber, Translator


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.send_failure = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    async def send(self, raw):
        if self.send_failure:
            raise self.send_failure
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.incoming.put_nowait({"type": "session.updated"})

    async def close(self):
        self.incoming.put_nowait(None)


class RealtimeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.socket = FakeSocket()
        self.events = []
        self.partial_received = asyncio.Event()

        async def connect_fn(*args, **kwargs):
            self.connection_arguments = (args, kwargs)
            return self.socket

        async def on_event(event):
            # finish() must wait for the final callback, not only remove its ID.
            if event["type"] == "transcript":
                await asyncio.sleep(0.001)
            self.events.append(event)
            if event["type"] == "partial":
                self.partial_received.set()

        self.provider = RealtimeTranscriber(
            "sk-private-test-value", on_event=on_event, connect_fn=connect_fn,
        )
        await self.provider.connect()

    async def asyncTearDown(self):
        await self.provider.close()

    def push(self, kind, **fields):
        self.socket.incoming.put_nowait({"type": kind, **fields})

    async def test_live_partial_appears_before_commit_without_duplicate_delta(self):
        self.push("conversation.item.input_audio_transcription.delta", item_id="live", delta="皆さん")
        await asyncio.wait_for(self.partial_received.wait(), timeout=1)
        partial = [event for event in self.events if event["type"] == "partial"][0]
        self.assertEqual(partial["ja"], "皆さん")
        self.assertIsNone(partial["start_ms"])
        self.assertEqual(self.provider.pending_count, 0)

        await self.provider.commit(1000, 4000)
        self.push("input_audio_buffer.committed", item_id="live")
        self.push("conversation.item.input_audio_transcription.delta", item_id="live", delta="こんばんは。")
        self.push("conversation.item.input_audio_transcription.completed", item_id="live", transcript="皆さんこんばんは。")
        self.assertTrue(await self.provider.finish(timeout=1))
        partials = [event for event in self.events if event["type"] == "partial"]
        self.assertEqual([event["ja"] for event in partials], ["皆さん", "皆さんこんばんは。"])
        self.assertEqual(partials[-1]["start_ms"], 1000)
        final = [event for event in self.events if event["type"] == "transcript"][0]
        self.assertEqual((final["start_ms"], final["end_ms"]), (1000, 4000))

    async def test_out_of_order_finals_keep_acknowledged_audio_time_ranges(self):
        await self.provider.append(b"\x00\x00" * 2400)
        await self.provider.commit(0, 4000)
        await self.provider.commit(4000, 8000)
        self.push("input_audio_buffer.committed", item_id="first")
        self.push("input_audio_buffer.committed", item_id="second")
        self.push("conversation.item.input_audio_transcription.completed", item_id="second", transcript="二番です。")
        self.push("conversation.item.input_audio_transcription.completed", item_id="first", transcript="一番です。")
        self.assertTrue(await self.provider.finish(timeout=1))
        finals = [event for event in self.events if event["type"] == "transcript"]
        self.assertEqual(
            [(event["id"], event["start_ms"], event["end_ms"]) for event in finals],
            [("second", 4000, 8000), ("first", 0, 4000)],
        )
        update = self.socket.sent[0]
        config = update["session"]["audio"]["input"]
        self.assertEqual(config["transcription"]["model"], "gpt-live-transcribe")
        self.assertEqual(config["transcription"]["languages"], ["ja"])
        self.assertIsNone(config["turn_detection"])
        self.assertEqual(config["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertNotIn("model=", self.connection_arguments[0][0])

    async def test_final_before_ack_is_deferred_until_timing_is_known(self):
        await self.provider.commit(2000, 5000)
        self.push("conversation.item.input_audio_transcription.completed", item_id="early", transcript="完了。")
        self.push("input_audio_buffer.committed", item_id="early")
        self.assertTrue(await self.provider.finish(timeout=1))
        final = [event for event in self.events if event["type"] == "transcript"][0]
        self.assertEqual(final["start_ms"], 2000)

    async def test_missing_final_has_finite_timeout_and_user_visible_warning(self):
        await self.provider.commit(0, 1000)
        self.assertFalse(await self.provider.finish(timeout=0.001))
        self.assertEqual(self.provider.pending_count, 1)
        self.assertTrue(any(event["type"] == "warning" for event in self.events))

    async def test_completed_turn_state_is_released_and_late_duplicates_are_ignored(self):
        await self.provider.commit(0, 1000)
        self.push("input_audio_buffer.committed", item_id="done")
        self.push("conversation.item.input_audio_transcription.completed", item_id="done", transcript="完了。")
        self.assertTrue(await self.provider.finish(timeout=1))
        self.assertEqual(self.provider._timings, {})
        self.assertEqual(self.provider._texts, {})
        self.push("conversation.item.input_audio_transcription.completed", item_id="done", transcript="重複。")
        self.push("conversation.item.input_audio_transcription.delta", item_id="done", delta="遅れ")
        await self.provider.commit(1000, 2000)
        self.push("input_audio_buffer.committed", item_id="next")
        self.push("conversation.item.input_audio_transcription.completed", item_id="next", transcript="次。")
        self.assertTrue(await self.provider.finish(timeout=1))
        finals = [event for event in self.events if event["type"] == "transcript"]
        self.assertEqual([event["ja"] for event in finals], ["完了。", "次。"])
        self.assertEqual(self.provider._deferred, {})
        await self.provider.close()
        self.assertEqual(self.provider._completed_ids, set())

    async def test_network_failure_never_exposes_api_secret(self):
        self.socket.send_failure = RuntimeError("failed using sk-private-test-value")
        with self.assertRaises(ProviderError) as result:
            await self.provider.append(b"\x00\x00")
        self.assertTrue(self.provider.failed)
        self.assertNotIn("sk-private-test-value", str(result.exception))
        self.assertNotIn("sk-private-test-value", json.dumps(self.events))
        self.assertFalse(await self.provider.finish())


class TranslatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_translation_sends_bounded_context_and_parses_response_text(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={
                "status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": " 大家晚上好。 "},
                ]}],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            translator = Translator("sk-private-test-value", glossary="配信 = 直播", client=client)
            context = [{"ja": str(index), "zh": str(index)} for index in range(12)]
            self.assertEqual(await translator.translate("皆さんこんばんは。", context), "大家晚上好。")
            body = json.loads(requests[0].content)
            input_data = json.loads(body["input"])
            self.assertFalse(body["store"])
            self.assertEqual(input_data["current_text"], "皆さんこんばんは。")
            self.assertEqual(len(input_data["recent_context"]), 4)
            self.assertEqual(input_data["glossary"], "配信 = 直播")
            await translator.close()
            self.assertFalse(client.is_closed)

    async def test_truncated_and_missing_translation_are_failures(self):
        for body in (
            {"status": "incomplete", "output": [{"type": "message", "content": [{"type": "output_text", "text": "半句话"}]}]},
            {"status": "completed", "output": []},
        ):
            with self.subTest(body=body):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
                    with self.assertRaises(ProviderError):
                        await Translator("secret", client=client).translate("テスト")

    async def test_auth_error_does_not_expose_response_body_or_api_key(self):
        def respond(request):
            return httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "sk-private-test-value"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(ProviderError) as result:
                await Translator("sk-private-test-value", client=client).translate("テスト")
            self.assertNotIn("sk-private-test-value", str(result.exception))
            self.assertIn("密钥", str(result.exception))


if __name__ == "__main__":
    unittest.main()
