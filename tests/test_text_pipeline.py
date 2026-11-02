"""Proofreading and translation are independent, ordered, resumable requests."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from yakiniku.provider import ProviderError, Translator
from yakiniku.storage import SessionStore, SettingsStore
from yakiniku.text_pipeline import TextPipeline, create_text_processor, process_segment, text_options, validate_text_settings
from yakiniku.import_audio import AudioImportManager
from test_import_audio import FakeModel, wav_bytes


class TextPipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_unified_routes_require_an_explicit_correction_model_while_legacy_can_inherit(self):
        settings = {"api_base_url": "https://fixture.example/v1", "api_key": "test-key",
                    "translation_model": "translation-fixture", "correction_model": ""}
        self.assertEqual(text_options(settings, "correction")["model"], "translation-fixture")
        settings.update(service_profiles_enabled=True,
                        service_profiles=[{"id": "text", "base_url": "https://fixture.example/v1", "api_key": "test-key"}],
                        task_routes={"correction": {"provider_id": "text"}, "translation": {"provider_id": "text"}})
        self.assertEqual(text_options(settings, "correction")["model"], "")
        with self.assertRaisesRegex(ProviderError, "日语校对"):
            validate_text_settings(settings)

    async def test_real_protocols_use_different_services_and_translation_sees_only_corrected_japanese(self):
        for correction_format, translation_format in (("responses", "chat_completions"),
                                                       ("chat_completions", "responses")):
            with self.subTest(correction_format=correction_format):
                requests, checkpoints = [], []

                async def respond(request):
                    payload = json.loads(request.content)
                    requests.append((request, payload))
                    correction = request.url.host == "proof.example"
                    content = "皆さん、こんばんは。" if correction else "大家晚上好。"
                    if not correction:
                        self.assertEqual(checkpoints, ["皆さん、こんばんは。"], "save correction before translation")
                    result = ({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
                              if request.url.path.endswith("chat/completions") else
                              {"status": "completed", "output": [{"type": "message", "content": [
                                  {"type": "output_text", "text": content}]}]})
                    return httpx.Response(200, json=result)

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    corrector = Translator("proof-test-key", "proof-model-fixture", base_url="https://proof.example/v1",
                                           api_format=correction_format, client=client)
                    translator = Translator("translation-test-key", "translation-model-fixture",
                                            base_url="https://translate.example/gateway", api_format=translation_format,
                                            client=client)
                    segment = {"id": "cue", "ja": "皆さんこんばんわ", "zh": "", "start_ms": 1200, "end_ms": 2400}

                    def checkpoint(value):
                        self.assertEqual(value["translation_status"], "pending")
                        self.assertEqual(value["text_stage"], "translating")
                        self.assertEqual(value["zh"], "")
                        checkpoints.append(value["ja"])

                    await process_segment(TextPipeline(corrector, translator), segment, [],
                                          checkpoint=checkpoint)
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0][0].headers["Authorization"], "Bearer proof-test-key")
                self.assertEqual(requests[1][0].headers["Authorization"], "Bearer translation-test-key")
                self.assertEqual(requests[0][1]["model"], "proof-model-fixture")
                self.assertEqual(requests[1][1]["model"], "translation-model-fixture")
                text_input = (requests[1][1]["input"] if translation_format == "responses"
                              else requests[1][1]["messages"][1]["content"])
                self.assertEqual(json.loads(text_input)["current_text"], "皆さん、こんばんは。")
                self.assertEqual((segment["ja_raw"], segment["ja"], segment["zh"]),
                                 ("皆さんこんばんわ", "皆さん、こんばんは。", "大家晚上好。"))
                self.assertEqual((segment["id"], segment["start_ms"], segment["end_ms"]), ("cue", 1200, 2400))
                self.assertEqual(segment["correction_status"], "done")
                self.assertNotIn("test-key", json.dumps(segment))

    async def test_failed_translation_saves_correction_and_retry_does_not_request_correction_again(self):
        calls = []

        class Corrector:
            model = "proof-fixture"
            api_format = "responses"

            async def correct(self, text, context=None):
                calls.append(("correct", text))
                return "こんばんは。"

        class FailingTranslator:
            async def translate(self, text, context=None):
                calls.append(("translate-fail", text))
                raise ProviderError("测试翻译失败")

        class RetryingCorrector:
            async def correct(self, text, context=None):
                raise AssertionError("A completed correction must not be requested twice")

        class Translator:
            model = "new-translation-fixture"
            api_format = "chat_completions"

            async def translate(self, text, context=None):
                calls.append(("translate-retry", text))
                return "晚上好。"

        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(Path(temporary))
            session = store.create("checkpoint test")
            segment = {"id": "cue", "ja": "こんばんわ", "zh": "", "start_ms": 100, "end_ms": 900}
            store.append_segment(session, segment)

            def checkpoint(value):
                store.append_segment(session, value)
                store.export(session)

            with self.assertRaises(ProviderError):
                await process_segment(TextPipeline(Corrector(), FailingTranslator()), segment, [], checkpoint=checkpoint)
            saved = store.load(session["id"])["segments"][0]
            self.assertEqual((saved["ja_raw"], saved["ja"], saved["zh"], saved["correction_status"]),
                             ("こんばんわ", "こんばんは。", "", "done"))
            await process_segment(TextPipeline(RetryingCorrector(), Translator()), saved, [])
            self.assertEqual(saved["zh"], "晚上好。")
            self.assertEqual(calls, [("correct", "こんばんわ"), ("translate-fail", "こんばんは。"),
                                     ("translate-retry", "こんばんは。")])
            self.assertEqual(saved["correction_model"], "proof-fixture")

    async def test_correction_failure_never_translates_raw_text_or_overwrites_japanese(self):
        class Corrector:
            async def correct(self, text, context=None):
                raise ProviderError("测试校对失败")

        class Translator:
            async def translate(self, text, context=None):
                raise AssertionError("Failed correction must stop this sentence")

        segment = {"ja": "認識原文", "zh": ""}
        with self.assertRaises(ProviderError):
            await process_segment(TextPipeline(Corrector(), Translator()), segment, [])
        self.assertEqual(segment, {"ja": "認識原文", "ja_raw": "認識原文", "zh": "", "correction_status": "failed",
                                   "text_stage": "correction_failed"})

    async def test_cancel_during_translation_keeps_durable_correction_checkpoint(self):
        entered = asyncio.Event()
        checkpoints = []

        class Corrector:
            async def correct(self, text, context=None):
                return "校対済みの日本語。"

        class Translator:
            async def translate(self, text, context=None):
                entered.set()
                await asyncio.Event().wait()

        segment = {"ja": "認識原文", "zh": ""}
        task = asyncio.create_task(process_segment(TextPipeline(Corrector(), Translator()), segment, [],
                                   checkpoint=lambda value: checkpoints.append(dict(value))))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(checkpoints[0]["correction_status"], "done")
        self.assertEqual(checkpoints[0]["ja"], "校対済みの日本語。")
        self.assertEqual(segment["correction_status"], "done")

    async def test_factory_resolves_independent_text_services_and_keeps_legacy_injection_single(self):
        instances = []

        class Client:
            def __init__(self, **options):
                self.options = options
                self.closed = False
                instances.append(self)

            async def correct(self, text, context=None):
                return text

            async def close(self):
                self.closed = True

        settings = {"api_base_url": "https://shared.example/v1", "api_key": "shared-test",
                    "translation_base_url": "https://translation.example/proxy", "translation_api_key": "translation-test",
                    "translation_model": "translation-fixture", "translation_api": "responses",
                    "correction_base_url": "https://proof.example/v1", "correction_api_key": "proof-test",
                    "correction_model": "proof-fixture", "correction_api": "chat_completions"}
        pipeline = await create_text_processor(settings, Client)
        self.assertEqual(pipeline.corrector.options["api_key"], "proof-test")
        self.assertEqual(pipeline.corrector.options["api_format"], "chat_completions")
        self.assertEqual(pipeline.translator.options["model"], "translation-fixture")
        await pipeline.close()
        self.assertTrue(all(instance.closed for instance in instances))

        class Legacy:
            def __init__(self, **options):
                self.options = options

        calls = []

        def legacy_factory(**options):
            calls.append(options)
            return Legacy(**options)

        processor = await create_text_processor(settings, legacy_factory)
        self.assertIsInstance(processor, Legacy)
        self.assertEqual(len(calls), 1)

    async def test_import_retry_preserves_all_saved_corrections_and_only_translates_missing_sentence(self):
        calls = []
        fail_translation = True

        class Client:
            def __init__(self, **options):
                self.model = options["model"]
                self.api_format = options["api_format"]

            async def correct(self, text, context=None):
                calls.append(("correct", text, self.model))
                return text + " 校正済み"

            async def translate(self, text, context=None):
                calls.append(("translate", text, self.model))
                if "ありがとう" in text and fail_translation:
                    raise ProviderError("测试翻译暂时失败")
                return "译文：" + text

            async def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = SettingsStore(root)
            settings.save({"api_base_url": "https://fixture.example/v1", "api_key": "test-key",
                           "translation_model": "translation-fixture", "correction_model": "proof-fixture"})
            store = SessionStore(root / "recordings")
            session = store.create("import checkpoint test")
            session.update(source_type="import", source_file="source.wav", import_upload_complete=True,
                           original_filename="test.wav", transcription_mode="local", transcription_model="tiny")
            store.file_path(session["id"], "source.wav").write_bytes(wav_bytes())
            store.save(session)
            manager = AudioImportManager(store, settings, Client, model_loader=lambda path: FakeModel())
            manager.start(session)
            await manager.tasks[session["id"]]
            failed = store.load(session["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertTrue(all(cue["correction_status"] == "done" for cue in failed["segments"]))
            self.assertEqual(sum(bool(cue["zh"]) for cue in failed["segments"]), 1)
            self.assertEqual(len([call for call in calls if call[0] == "correct"]), 2)
            fail_translation = False
            settings.save({"correction_model": "new-proof-fixture"})
            manager.start(failed)
            await manager.tasks[session["id"]]
            completed = store.load(session["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(len([call for call in calls if call[0] == "correct"]), 2)
            self.assertEqual(len([call for call in calls if call[0] == "translate"]), 3)
            self.assertTrue(all(call[1].endswith(" 校正済み") for call in calls if call[0] == "translate"))
            self.assertTrue(all(cue["correction_model"] == "proof-fixture" for cue in completed["segments"]))
            self.assertEqual(completed["correction_model"], "proof-fixture")


if __name__ == "__main__":
    unittest.main()
