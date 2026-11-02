import asyncio
import io
import json
import struct
import unittest
import wave
from email import policy
from email.parser import BytesParser

import httpx

from yakiniku.audio import AudioSegmenter
from yakiniku.provider import ChunkedTranscriber, ProviderError, RealtimeTranscriber, Translator


CUSTOM_BASE = "https://gateway.example/proxy/openai/v1"
CUSTOM_KEY = "sk-third-party-private"


def multipart_fields(request):
    header = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
    message = BytesParser(policy=policy.default).parsebytes(header + request.content)
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in message.iter_parts()
    }


class CompatibleTranslatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_chat_url_auth_and_prompt_payload(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop", "message": {"content": "大家晚上好。"},
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            translator = Translator(CUSTOM_KEY, "vendor-model", "配信=直播", base_url=CUSTOM_BASE,
                                    api_format="chat_completions", client=client)
            self.assertEqual(await translator.translate("皆さんこんばんは。", [{"ja": "前文", "zh": "上文"}]), "大家晚上好。")
            self.assertEqual(len(requests), 1)
            request = requests[0]
            self.assertEqual(str(request.url), CUSTOM_BASE + "/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer " + CUSTOM_KEY)
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "vendor-model")
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["max_tokens"], 1800)
            self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user"])
            content = json.loads(payload["messages"][1]["content"])
            self.assertEqual(content["current_text"], "皆さんこんばんは。")
            self.assertEqual(content["recent_context"][0]["zh"], "上文")
            self.assertEqual(content["glossary"], "配信=直播")

    async def test_chat_text_parts_and_responses_custom_prefix(self):
        for api_format, response in (
            ("chat_completions", {"choices": [{"finish_reason": "stop", "message": {"content": [{"type": "text", "text": "中文"}, {"type": "text", "text": "译文。"}]}}]}),
            ("responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "中文译文。"}]}]}),
        ):
            with self.subTest(api_format=api_format):
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(200, json=response)

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    result = await Translator(CUSTOM_KEY, base_url=CUSTOM_BASE, api_format=api_format, client=client).translate("日本語")
                    self.assertEqual(result, "中文译文。")
                    path = "responses" if api_format == "responses" else "chat/completions"
                    self.assertEqual(str(requests[0].url), CUSTOM_BASE + "/" + path)

    async def test_chat_truncation_refusal_and_missing_content_are_failures(self):
        for choice in (
            {"finish_reason": "length", "message": {"content": "半句话"}},
            {"finish_reason": "content_filter", "message": {"content": ""}},
            {"finish_reason": "stop", "message": {"content": None, "refusal": "refused"}},
            {"finish_reason": "stop", "message": {"content": ""}},
        ):
            with self.subTest(choice=choice):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [choice]}))) as client:
                    with self.assertRaises(ProviderError):
                        await Translator(CUSTOM_KEY, base_url=CUSTOM_BASE, api_format="chat_completions", client=client).translate("日本語")

    async def test_format_error_and_redirect_never_fallback_or_reveal_secrets(self):
        for status, extra in ((404, {}), (302, {"headers": {"location": "https://api.openai.com/v1/responses"}})):
            with self.subTest(status=status):
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(status, json={"error": {"message": CUSTOM_KEY}}, **extra)

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
                    with self.assertRaises(ProviderError) as result:
                        await Translator(CUSTOM_KEY, base_url=CUSTOM_BASE, api_format="chat_completions", client=client).translate("日本語")
                    self.assertNotIn(CUSTOM_KEY, str(result.exception))
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].url.host, "gateway.example")


class CompatibleRealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_realtime_socket_preserves_proxy_prefix(self):
        class Socket:
            def __init__(self):
                self.queue = asyncio.Queue()

            def __aiter__(self):
                return self

            async def __anext__(self):
                value = await self.queue.get()
                if value is None:
                    raise StopAsyncIteration
                return json.dumps(value)

            async def send(self, raw):
                if json.loads(raw)["type"] == "session.update":
                    self.queue.put_nowait({"type": "session.updated"})

            async def close(self):
                self.queue.put_nowait(None)

        captured = []
        socket = Socket()

        async def connect_fn(url, **kwargs):
            captured.append((url, kwargs))
            return socket

        provider = RealtimeTranscriber(CUSTOM_KEY, base_url=CUSTOM_BASE, connect_fn=connect_fn)
        try:
            await provider.connect()
            self.assertEqual(captured[0][0], "wss://gateway.example/proxy/openai/v1/realtime?intent=transcription")
            self.assertEqual(captured[0][1]["additional_headers"]["Authorization"], "Bearer " + CUSTOM_KEY)
        finally:
            await provider.close()


class CompatibleChunkedTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_turn_posts_mono_wav_and_keeps_original_time(self):
        requests, events = [], []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"text": "日本語です。"})

        async def emit(event):
            events.append(event)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChunkedTranscriber(CUSTOM_KEY, "whisper-1", "日本語の参考", emit, base_url=CUSTOM_BASE, client=client)
            await provider.connect()
            self.assertEqual(requests, [])
            pcm = struct.pack("<h", 1234) * 4800
            await provider.append(pcm)
            await provider.commit(9000, 9200)
            self.assertEqual(requests, [], "commit must enqueue rather than await HTTP")
            self.assertTrue(await provider.finish(timeout=1))
            self.assertEqual(len(requests), 1)
            request = requests[0]
            self.assertEqual(str(request.url), CUSTOM_BASE + "/audio/transcriptions")
            self.assertEqual(request.headers["authorization"], "Bearer " + CUSTOM_KEY)
            fields = multipart_fields(request)
            self.assertEqual(fields["model"], b"whisper-1")
            self.assertEqual(fields["language"], b"ja")
            self.assertNotIn("languages[]", fields)
            self.assertEqual(fields["response_format"], b"json")
            self.assertEqual(fields["prompt"].decode(), "日本語の参考")
            with wave.open(io.BytesIO(fields["file"]), "rb") as reader:
                self.assertEqual((reader.getnchannels(), reader.getframerate(), reader.getnframes()), (1, 24000, 4800))
                self.assertEqual(reader.readframes(4800), pcm)
            final = [event for event in events if event["type"] == "transcript"][0]
            self.assertEqual((final["start_ms"], final["end_ms"], final["ja"]), (9000, 9200, "日本語です。"))
            await provider.close()
            self.assertFalse(client.is_closed)

    async def test_final_short_tail_uses_padded_wav_but_original_cue_end(self):
        requests, events = [], []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"text": "短い。"})

        async def emit(event):
            events.append(event)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChunkedTranscriber(CUSTOM_KEY, "gpt-transcribe-2026-01-01", on_event=emit, base_url=CUSTOM_BASE, client=client)
            await provider.connect()
            segmenter = AudioSegmenter(preroll_ms=0)
            actions = segmenter.process(struct.pack("<h", 1200) * 480) + segmenter.flush()
            for action in actions:
                if action["type"] == "append":
                    await provider.append(action["pcm"])
                else:
                    await provider.commit(action["start_ms"], action["end_ms"])
            self.assertTrue(await provider.finish(timeout=1))
            fields = multipart_fields(requests[0])
            self.assertEqual(fields["languages[]"], b"ja")
            self.assertNotIn("language", fields)
            with wave.open(io.BytesIO(fields["file"]), "rb") as reader:
                self.assertEqual(reader.getnframes(), 2400)
            final = [event for event in events if event["type"] == "transcript"][0]
            self.assertEqual((final["start_ms"], final["end_ms"]), (0, 20))
            await provider.close()

    async def test_bad_chunk_warns_interval_and_next_chunk_still_succeeds(self):
        requests, events = [], []

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(404, json={"error": {"message": CUSTOM_KEY}})
            return httpx.Response(200, json={"text": "次の句。"})

        async def emit(event):
            events.append(event)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChunkedTranscriber(CUSTOM_KEY, on_event=emit, base_url=CUSTOM_BASE, client=client)
            await provider.connect()
            for start in (0, 10000):
                await provider.append(b"\x00\x00" * 2400)
                await provider.commit(start, start + 100)
            self.assertFalse(await provider.finish(timeout=1))
            self.assertFalse(provider.failed)
            self.assertEqual(len(requests), 2, "format errors must not retry")
            warning = [event for event in events if event["type"] == "warning"][0]
            self.assertEqual((warning["start_ms"], warning["end_ms"]), (0, 100))
            self.assertNotIn(CUSTOM_KEY, json.dumps(events))
            final = [event for event in events if event["type"] == "transcript"][0]
            self.assertEqual((final["start_ms"], final["ja"]), (10000, "次の句。"))
            self.assertTrue(all(request.url.host == "gateway.example" for request in requests))
            await provider.close()

    async def test_queue_is_bounded_and_overflow_warns_for_discarded_intervals(self):
        started, release = asyncio.Event(), asyncio.Event()
        requests, events = [], []

        async def respond(request):
            requests.append(request)
            started.set()
            await release.wait()
            return httpx.Response(200, json={"text": "最初。"})

        async def emit(event):
            events.append(event)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChunkedTranscriber(CUSTOM_KEY, on_event=emit, base_url=CUSTOM_BASE, client=client, max_queued=2)
            await provider.connect()
            await provider.append(b"\x00\x00" * 2400)
            await provider.commit(0, 100)
            await asyncio.wait_for(started.wait(), timeout=1)
            for start in (100, 200):
                await provider.append(b"\x00\x00" * 2400)
                await provider.commit(start, start + 100)
            self.assertEqual(provider.pending_count, 3)  # One running + two queued.
            await provider.append(b"\x00\x00" * 2400)
            with self.assertRaises(ProviderError):
                await provider.commit(300, 400)
            self.assertTrue(provider.failed)
            self.assertEqual(provider.pending_count, 1)
            warnings = [event for event in events if event["type"] == "warning" and "start_ms" in event]
            self.assertEqual({(event["start_ms"], event["end_ms"]) for event in warnings}, {(100, 200), (200, 300), (300, 400)})
            release.set()
            self.assertFalse(await provider.finish(timeout=1))
            self.assertEqual(len(requests), 1)
            await provider.close()

    async def test_finish_timeout_is_finite_and_close_cancels_pending_http(self):
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []

        async def respond(request):
            started.set()
            try:
                await release.wait()
            finally:
                cancelled.set()
            return httpx.Response(200, json={"text": "unused"})

        async def emit(event):
            events.append(event)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChunkedTranscriber(CUSTOM_KEY, on_event=emit, base_url=CUSTOM_BASE, client=client)
            await provider.connect()
            await provider.append(b"\x00\x00" * 2400)
            await provider.commit(1000, 1100)
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertFalse(await provider.finish(timeout=0.001))
            await provider.close()
            self.assertTrue(cancelled.is_set())
            self.assertEqual(provider.pending_count, 0)
            self.assertTrue(any(event["type"] == "warning" for event in events))

    async def test_transient_error_retries_once_and_empty_text_is_not_fabricated(self):
        requests, events = [], []

        def respond(request):
            requests.append(request)
            return httpx.Response(503, json={}) if len(requests) == 1 else httpx.Response(200, json={"text": " "})

        async def emit(event):
            events.append(event)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChunkedTranscriber(CUSTOM_KEY, on_event=emit, base_url=CUSTOM_BASE, client=client)
            await provider.connect()
            await provider.append(b"\x00\x00" * 2400)
            await provider.commit(0, 100)
            self.assertTrue(await provider.finish(timeout=1))
            self.assertEqual(len(requests), 2)
            self.assertFalse(any(event["type"] == "transcript" for event in events))
            await provider.close()

    async def test_oversized_uncommitted_turn_is_rejected_without_network(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"text": "unused"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChunkedTranscriber(CUSTOM_KEY, base_url=CUSTOM_BASE, client=client)
            await provider.connect()
            with self.assertRaises(ProviderError):
                await provider.append(b"\x00\x00" * (24000 * 30 + 1))
            self.assertTrue(provider.failed)
            self.assertEqual(len(provider._buffer), 0)
            self.assertEqual(requests, [])
            await provider.close()


if __name__ == "__main__":
    unittest.main()
