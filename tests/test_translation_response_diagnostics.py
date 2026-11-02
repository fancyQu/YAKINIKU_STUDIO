"""Wrong gateway paths can return a successful website page instead of JSON."""
import unittest

import httpx

from yakiniku.provider import ProviderError, Translator


class TranslationResponseDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_success_is_diagnosed_without_guessing_a_new_endpoint(self):
        secret = "private-upstream-body-marker"
        html = "<!doctype html><html><head><title>API Console</title></head><body>" + secret + "</body></html>"
        for api_format in ("chat_completions", "responses", "anthropic"):
            for content_type in ("text/html; charset=utf-8", "application/json", "text/plain"):
                with self.subTest(api_format=api_format, content_type=content_type):
                    requests = []

                    def respond(request):
                        requests.append(request)
                        return httpx.Response(200, text=html, headers={"content-type": content_type})

                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                        translator = Translator("private-api-key", model="fixture-model", base_url="https://gateway.example",
                                                api_format=api_format, client=client)
                        with self.assertRaises(ProviderError) as failure:
                            await translator.translate("こんにちは。")
                    message = str(failure.exception)
                    self.assertIn("网页", message)
                    self.assertIn("地址", message)
                    self.assertNotIn(secret, message)
                    self.assertNotIn("private-api-key", message)
                    self.assertEqual(len(requests), 1)
                    expected_path = {"chat_completions": "/chat/completions", "responses": "/responses",
                                     "anthropic": "/v1/messages"}[api_format]
                    self.assertEqual(requests[0].url.path, expected_path)


if __name__ == "__main__":
    unittest.main()
