"""Single-request correction keeps local recognition and subtitle timing intact."""

import json
import unittest

import httpx

from yakiniku.provider import ProviderError, Translator
from yakiniku.server import translate_segment


class CorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_protocols_correct_and_translate_in_one_request(self):
        for api_format in ("responses", "chat_completions"):
            with self.subTest(api_format=api_format):
                requests = []

                def respond(request):
                    requests.append(request)
                    output = '```json\n{"ja":"皆さん、こんばんは。","zh":"大家晚上好。"}\n```'
                    body = ({"choices": [{"finish_reason": "stop", "message": {"content": output}}]}
                            if api_format == "chat_completions" else
                            {"status": "completed", "output": [{"type": "message", "content": [
                                {"type": "output_text", "text": output}]}]})
                    return httpx.Response(200, json=body)

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    translator = Translator("test-key", "chosen-text-model", "配信=直播",
                                            api_format=api_format, client=client)
                    result = await translator.translate_and_correct("皆さんこんばんわ", context=[
                        {"ja": str(index), "zh": str(index)} for index in range(8)])
                    self.assertEqual(result, {"ja": "皆さん、こんばんは。", "zh": "大家晚上好。"})
                    self.assertEqual(len(requests), 1)
                    payload = json.loads(requests[0].content)
                    self.assertEqual(payload["model"], "chosen-text-model")
                    text_input = (payload["messages"][1]["content"] if api_format == "chat_completions"
                                  else payload["input"])
                    self.assertEqual(len(json.loads(text_input)["recent_context"]), 4)
                    self.assertEqual(json.loads(text_input)["current_text"], "皆さんこんばんわ")
                    self.assertNotIn("response_format", payload)

    async def test_plain_translation_fallback_preserves_japanese(self):
        body = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "大家晚上好。"}]}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
            result = await Translator("test-key", client=client).translate_and_correct("皆さんこんばんわ")
            self.assertEqual(result, {"ja": "皆さんこんばんわ", "zh": "大家晚上好。"})

    async def test_invalid_json_result_is_not_written_into_subtitles(self):
        for output in ('{"ja":"補完"}', '{"ja":"補完","zh":[]}', '{"ja":', '[]'):
            with self.subTest(output=output):
                body = {"status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": output}]}]}
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
                    with self.assertRaises(ProviderError):
                        await Translator("test-key", client=client).translate_and_correct("認識原文")

    async def test_segment_keeps_raw_text_and_timing(self):
        class Corrector:
            async def translate_and_correct(self, text, context):
                self.input = text
                return {"ja": "こんばんは。", "zh": "晚上好。"}

        corrector = Corrector()
        segment = {"id": "original-id", "start_ms": 1234, "end_ms": 4567, "ja": "こんばんわ", "zh": ""}
        await translate_segment(corrector, segment, [])
        self.assertEqual(corrector.input, "こんばんわ")
        self.assertEqual(segment["ja_raw"], "こんばんわ")
        self.assertEqual(segment["ja"], "こんばんは。")
        self.assertEqual(segment["zh"], "晚上好。")
        self.assertEqual((segment["id"], segment["start_ms"], segment["end_ms"]), ("original-id", 1234, 4567))
        await translate_segment(corrector, segment, [])
        self.assertEqual(corrector.input, "こんばんわ", "retries must use raw recognition, not a previous correction")

    async def test_failure_preserves_original_text(self):
        class FailingCorrector:
            async def translate_and_correct(self, text, context):
                raise ProviderError("测试失败")

        segment = {"ja": "認識原文", "zh": ""}
        with self.assertRaises(ProviderError):
            await translate_segment(FailingCorrector(), segment, [])
        self.assertEqual(segment, {"ja": "認識原文", "ja_raw": "認識原文", "zh": ""})


if __name__ == "__main__":
    unittest.main()
