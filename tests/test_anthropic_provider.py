"""Messages adapters keep Anthropic auth and thinking out of subtitle text."""

import json
import unittest

import httpx

from yakiniku.provider import ProviderError, Translator


def message(text="中文译文。", *, reason="end_turn", content=None):
    return {"type": "message", "role": "assistant", "stop_reason": reason,
            "content": [{"type": "text", "text": text}] if content is None else content}


def messages_stream(*, text="中文译文。", reason="end_turn"):
    return [
        {"type": "message_start", "message": message(reason=None, content=[])},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "private reasoning"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "opaque-signature"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": reason}},
        {"type": "message_stop"},
    ]


def sse(events):
    return "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                   for event in events)


class AnthropicProviderTests(unittest.IsolatedAsyncioTestCase):
    async def translated(self, body, *, content_type="application/json", status=200):
        def respond(request):
            if content_type == "application/json":
                return httpx.Response(status, json=body)
            return httpx.Response(status, text=body, headers={"content-type": content_type})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await Translator("private-test-key", "fixture-text-model", api_format="anthropic",
                                    base_url="https://fixture.example/anthropic", client=client).translate("日本語")

    async def test_minimax_bearer_request_uses_messages_endpoint_and_own_payload(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=message())

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            translator = Translator("private-test-key", "fixture-selected-model", "配信=直播",
                                    api_format="anthropic", anthropic_auth="bearer",
                                    base_url="https://api.minimax.io/anthropic", client=client)
            self.assertEqual(await translator.translate("日本語", [{"ja": "前文", "zh": "上文"}]), "中文译文。")

        request = requests[0]
        self.assertEqual(str(request.url), "https://api.minimax.io/anthropic/v1/messages")
        self.assertEqual(request.headers["authorization"], "Bearer private-test-key")
        self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("x-api-key", request.headers)
        payload = json.loads(request.content)
        self.assertEqual(set(payload), {"model", "system", "messages", "max_tokens", "stream"})
        self.assertEqual(payload["model"], "fixture-selected-model")
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertFalse(payload["stream"])
        self.assertIsInstance(payload["system"], str)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(json.loads(payload["messages"][0]["content"]), {
            "current_text": "日本語", "glossary": "配信=直播",
            "recent_context": [{"ja": "前文", "zh": "上文"}],
        })

    async def test_generic_anthropic_default_uses_key_auth_and_preserves_v1(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=message(reason="stop_sequence"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            translator = Translator("private-test-key", "fixture-model", api_format="anthropic",
                                    base_url="https://fixture.example/proxy/v1", client=client)
            self.assertEqual(await translator.translate("日本語"), "中文译文。")
        self.assertEqual(str(requests[0].url), "https://fixture.example/proxy/v1/messages")
        self.assertEqual(requests[0].headers["x-api-key"], "private-test-key")
        self.assertEqual(requests[0].headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", requests[0].headers)

    async def test_correction_then_translation_send_corrected_japanese_to_second_call(self):
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            output = "皆さん、こんばんは。" if len(requests) == 1 else "大家晚上好。"
            return httpx.Response(200, json=message(content=[
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": output},
            ]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            translator = Translator("private-test-key", "fixture-model", api_format="anthropic",
                                    base_url="https://fixture.example/anthropic", client=client)
            corrected = await translator.correct("皆さんこんばんわ")
            translated = await translator.translate(corrected)
        self.assertEqual(corrected, "皆さん、こんばんは。")
        self.assertEqual(translated, "大家晚上好。")
        self.assertEqual(len(requests), 2)
        self.assertIn("日语字幕校对员", requests[0]["system"])
        self.assertIn("中文字幕译者", requests[1]["system"])
        self.assertEqual(json.loads(requests[1]["messages"][0]["content"])["current_text"], corrected)

    async def test_text_blocks_join_and_thinking_is_ignored(self):
        body = message(content=[
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "text", "text": "中文"},
            {"type": "redacted_thinking", "data": "opaque-data"},
            {"type": "text", "text": "译文。"},
        ])
        self.assertEqual(await self.translated(body), "中文译文。")

    async def test_gpt_model_does_not_add_openai_reasoning_fields_to_messages(self):
        payloads = []

        def respond(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=message())

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await Translator("private-test-key", "gpt-6-sol", api_format="anthropic",
                             base_url="https://fixture.example/anthropic", client=client).translate("日本語")
        self.assertNotIn("reasoning", payloads[0])
        self.assertNotIn("reasoning_effort", payloads[0])
        self.assertNotIn("thinking", payloads[0])

    async def test_nonfinal_results_are_rejected(self):
        for reason in ("max_tokens", "tool_use", "pause_turn", "refusal", None):
            with self.subTest(reason=reason), self.assertRaises(ProviderError):
                await self.translated(message(reason=reason))

    async def test_malformed_or_nontext_messages_are_rejected(self):
        cases = (
            [], {"content": [{"type": "text", "text": "文字"}]},
            {**message(), "role": "user"}, {**message(), "content": "文字"},
            message(content=[None]), message(content=[{"type": "text", "text": 123}]),
            message(content=[{"type": "tool_use", "id": "tool-id", "input": {}}]),
            message(content=[{"type": "thinking", "thinking": "private reasoning"}]),
            message(content=[]), message("   "),
        )
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ProviderError):
                await self.translated(body)

    async def test_complete_sse_returns_only_visible_text(self):
        events = messages_stream()
        events.insert(1, {"type": "ping"})
        self.assertEqual(await self.translated(sse(events), content_type="text/event-stream"), "中文译文。")

    async def test_complete_sse_without_content_type_is_recognized(self):
        self.assertEqual(await self.translated(sse(messages_stream()), content_type="text/plain"), "中文译文。")

    async def test_unfinished_sse_never_accepts_partial_text_or_openai_done(self):
        events = messages_stream()
        for body in (sse(events[:-1]), sse(events[:-1]) + "data: [DONE]\n\n",
                     sse(events[:-2] + [events[-1]]),
                     sse(events[:8]) + sse([{"type": "message_stop"}]), "data: [DONE]\n\n"):
            with self.subTest(body=body), self.assertRaises(ProviderError):
                await self.translated(body, content_type="text/event-stream")

    async def test_sse_error_tool_block_and_malformed_lifecycle_are_rejected(self):
        start = messages_stream()[:1]
        cases = (
            start + [{"type": "error", "error": {"message": "private-test-key"}}],
            start + [{"type": "content_block_start", "index": 0,
                      "content_block": {"type": "tool_use", "id": "tool-id", "input": {}}}],
            start + [{"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": "半句"}}],
            start + [{"type": "content_block_stop", "index": 0}],
            start + [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                     {"type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": "未结束"}},
                     {"type": "message_stop"}],
            messages_stream() + [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}}],
        )
        for events in cases:
            with self.subTest(events=events), self.assertRaises(ProviderError) as failure:
                await self.translated(sse(events), content_type="text/event-stream")
            self.assertNotIn("private-test-key", str(failure.exception))

    async def test_http_errors_are_sanitized_without_protocol_or_model_fallback(self):
        for status in (400, 401):
            with self.subTest(status=status):
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(status, json={"error": {"type": "authentication_error",
                                                                "message": "private-test-key private upstream body"}})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    translator = Translator("private-test-key", "fixture-selected-model", api_format="anthropic",
                                            base_url="https://fixture.example/anthropic", client=client)
                    with self.assertRaises(ProviderError) as failure:
                        await translator.translate("日本語")
                self.assertEqual(len(requests), 1)
                self.assertEqual(json.loads(requests[0].content)["model"], "fixture-selected-model")
                self.assertNotIn("private-test-key", str(failure.exception))
                self.assertNotIn("private upstream body", str(failure.exception))

    async def test_auth_selector_rejects_unknown_value(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            with self.assertRaises(ValueError):
                Translator("private-test-key", api_format="anthropic", anthropic_auth="both",
                           base_url="https://fixture.example", client=client)


if __name__ == "__main__":
    unittest.main()
