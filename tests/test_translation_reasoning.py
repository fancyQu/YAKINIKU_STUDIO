"""Low latency reasoning settings stay narrow and tolerate explicit field rejection."""
import json
import unittest

import httpx

from yakiniku.provider import ProviderError, Translator


def translated(api_format):
    if api_format == "chat_completions":
        return {"choices": [{"finish_reason": "stop", "message": {"content": "译文"}}]}
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": "译文"}]}]}


class TranslationReasoningTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_for_exact_supported_model_in_both_protocols(self):
        for api_format in ("responses", "chat_completions"):
            for model in ("gpt-6-sol", "gpt-6-sol-custom", "other-model"):
                with self.subTest(api_format=api_format, model=model):
                    payloads = []

                    def respond(request):
                        payloads.append(json.loads(request.content))
                        return httpx.Response(200, json=translated(api_format))

                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                        result = await Translator("test-key", model, api_format=api_format,
                                                  client=client).translate("日本語")
                    self.assertEqual(result, "译文")
                    field = "reasoning_effort" if api_format == "chat_completions" else "reasoning"
                    if model == "gpt-6-sol":
                        self.assertEqual(payloads[0][field], "none" if field == "reasoning_effort" else {"effort": "none"})
                    else:
                        self.assertNotIn(field, payloads[0])

    async def test_explicit_rejection_retries_once_and_caches_for_instance(self):
        for api_format in ("responses", "chat_completions"):
            for status in (400, 422):
                with self.subTest(api_format=api_format, status=status):
                    payloads = []
                    field = "reasoning_effort" if api_format == "chat_completions" else "reasoning"

                    def respond(request):
                        payloads.append(json.loads(request.content))
                        if len(payloads) == 1:
                            return httpx.Response(status, json={"error": {
                                "code": "unknown_parameter", "param": field,
                                "message": "Unknown parameter: '" + field + "'."}})
                        return httpx.Response(200, json=translated(api_format))

                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                        translator = Translator("test-key", "gpt-6-sol", api_format=api_format, client=client)
                        self.assertEqual(await translator.translate("一番"), "译文")
                        self.assertEqual(await translator.translate("二番"), "译文")
                    self.assertEqual(len(payloads), 3)
                    self.assertIn(field, payloads[0])
                    self.assertNotIn(field, payloads[1])
                    self.assertNotIn(field, payloads[2])

    async def test_422_extra_field_validation_has_one_fallback(self):
        payloads = []

        def respond(request):
            payloads.append(json.loads(request.content))
            if len(payloads) == 1:
                return httpx.Response(422, json={"detail": [{"loc": ["body", "reasoning"],
                    "type": "extra_forbidden", "msg": "Extra inputs are not permitted"}]})
            return httpx.Response(200, json=translated("responses"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            self.assertEqual(await Translator("test-key", "gpt-6-sol", client=client).translate("原文"), "译文")
        self.assertEqual(len(payloads), 2)

    async def test_other_errors_are_not_hidden_or_retried(self):
        for status, error in (
            (400, {"code": "invalid_model", "message": "Model unavailable"}),
            (400, {"code": "unsupported_parameter", "param": "max_tokens", "message": "Reasoning model: unsupported parameter max_tokens"}),
            (400, {"code": "unsupported_value", "param": "reasoning.effort", "message": "Invalid effort value none"}),
            (400, {"code": "unsupported_value", "param": "reasoning.effort", "message": "Unsupported parameter value: reasoning.effort none"}),
            (401, {"code": "unknown_parameter", "param": "reasoning", "message": "Unknown parameter reasoning"}),
            (422, {"code": "invalid_request_error", "message": "reasoning request invalid"}),
        ):
            with self.subTest(status=status, error=error):
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(status, json={"error": error})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    with self.assertRaises(ProviderError):
                        await Translator("test-key", "gpt-6-sol", client=client).translate("原文")
                self.assertEqual(len(requests), 1)

    async def test_failed_fallback_does_not_loop(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(400, json={"error": {"code": "unknown_parameter", "param": "reasoning"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(ProviderError):
                await Translator("test-key", "gpt-6-sol", client=client).translate("原文")
        self.assertEqual(len(requests), 2)


if __name__ == "__main__":
    unittest.main()
