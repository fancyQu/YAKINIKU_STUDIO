"""Compatible gateways may return SSE even without an explicit stream request."""

import json
import unittest

import httpx

from yakiniku.provider import ProviderError, Translator


def stream(events, done=False):
    value = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
    return value + ("data: [DONE]\n\n" if done else "")


class TranslationSSETests(unittest.IsolatedAsyncioTestCase):
    async def translate(self, body, api_format="responses", content_type="text/event-stream", correction=False):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body, headers={"content-type": content_type}))) as client:
            translator = Translator("private-test-key", api_format=api_format, client=client)
            return await (translator.translate_and_correct("こんばんわ") if correction else translator.translate("日本語"))

    async def test_responses_completed_final_body_takes_precedence(self):
        body = stream([
            {"type": "response.output_text.delta", "delta": "前缀"},
            {"type": "response.completed", "response": {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "完整译文。"}]}]}},
        ])
        self.assertEqual(await self.translate(body), "完整译文。")

    async def test_responses_done_uses_confirmed_deltas_for_correction(self):
        output = '{"ja":"こんばんは。","zh":"晚上好。"}'
        body = stream([
            {"type": "response.output_text.delta", "delta": output[:14]},
            {"type": "response.output_text.delta", "delta": output[14:]},
            {"type": "response.done", "response": {"status": "completed", "output": []}},
        ])
        self.assertEqual(await self.translate(body, correction=True), {"ja": "こんばんは。", "zh": "晚上好。"})

    async def test_responses_text_done_does_not_duplicate_delta_text(self):
        body = stream([
            {"type": "response.output_text.delta", "delta": "部分"},
            {"type": "response.output_text.done", "text": "完整译文。"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ])
        self.assertEqual(await self.translate(body), "完整译文。")

    async def test_responses_done_sentinel_and_event_header(self):
        body = ('event: response.output_text.delta\ndata: {"delta":"中文译文。"}\n\n'
                'data: [DONE]\n\n')
        self.assertEqual(await self.translate(body, content_type="text/plain"), "中文译文。")

    async def test_failed_incomplete_or_truncated_stream_never_returns_partial_translation(self):
        delta = {"type": "response.output_text.delta", "delta": "不能保存的半句话"}
        for events, done in (
            ([delta], False),
            ([delta, {"type": "response.output_text.done", "text": "半句"}], False),
            ([delta, {"type": "response.failed", "response": {"error": {"message": "private-test-key"}}}], True),
            ([delta, {"type": "response.incomplete"}], True),
            ([delta, {"status": "failed"}], True),
            ([delta, {"type": "response.completed", "response": {"status": "incomplete"}}], True),
        ):
            with self.subTest(events=events):
                with self.assertRaises(ProviderError) as failure:
                    await self.translate(stream(events, done))
                self.assertNotIn("private-test-key", str(failure.exception))

    async def test_chat_sse_deltas_with_stop(self):
        body = stream([
            {"choices": [{"index": 0, "delta": {"content": "中文"}}]},
            {"choices": [{"index": 0, "delta": {"content": "译文。"}, "finish_reason": "stop"}]},
        ], done=True)
        self.assertEqual(await self.translate(body, api_format="chat_completions"), "中文译文。")

    async def test_chat_sse_length_and_filter_never_return_partial_text(self):
        for reason in ("length", "content_filter", "tool_calls"):
            with self.subTest(reason=reason):
                body = stream([{"choices": [{"delta": {"content": "半句话"}, "finish_reason": reason}]}], done=True)
                with self.assertRaises(ProviderError):
                    await self.translate(body, api_format="chat_completions")

    async def test_empty_or_malformed_stream_fails(self):
        for body in ("data: [DONE]\n\n", "data: not-json\n\n", ": heartbeat\n\n"):
            with self.subTest(body=body):
                with self.assertRaises(ProviderError):
                    await self.translate(body)


if __name__ == "__main__":
    unittest.main()
