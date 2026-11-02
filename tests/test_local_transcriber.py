import asyncio
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from yakiniku.local_transcriber import LocalTranscriber
from yakiniku.server import LiveSession, create_app
from yakiniku.storage import DEFAULT_SETTINGS, SettingsStore


class LocalTranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_model_receives_resampled_audio_and_keeps_cue_times(self):
        events = []
        calls = []

        class FakeModel:
            def transcribe(self, audio, **options):
                calls.append((audio, options))
                return iter([SimpleNamespace(text="皆さん、"), SimpleNamespace(text="こんばんは。")]), None

        async def emit(event):
            events.append(event)

        provider = LocalTranscriber(model="tiny", on_event=emit, model_loader=lambda path: FakeModel())
        await provider.connect()
        try:
            await provider.append(struct.pack("<h", 1234) * 24000)
            await provider.commit(8000, 9000)
            self.assertTrue(await provider.finish(timeout=2))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0].shape, (16000,))
            self.assertEqual(calls[0][1]["language"], "ja")
            self.assertFalse(calls[0][1]["condition_on_previous_text"])
            transcript = [event for event in events if event["type"] == "transcript"][0]
            self.assertEqual((transcript["start_ms"], transcript["end_ms"]), (8000, 9000))
            self.assertEqual(transcript["ja"], "皆さん、こんばんは。")
        finally:
            await provider.close()

    async def test_model_load_failure_is_safe_and_does_not_download(self):
        def fail(path):
            raise RuntimeError("private upstream information")
        provider = LocalTranscriber(model_loader=fail)
        with self.assertRaisesRegex(Exception, "模型加载失败") as result:
            await provider.connect()
        self.assertNotIn("private", str(result.exception))
        await provider.close()


class LocalModeServerTests(unittest.TestCase):
    def test_local_mode_needs_only_translation_key_and_never_routes_audio_to_cloud(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {}, clear=True):
            root = Path(temporary)
            store = SettingsStore(root)
            store.save({"transcription_mode": "local", "transcription_model": "tiny",
                        "translation_base_url": "https://text.example/v1", "translation_api_key": "private-translation-key",
                        "translation_model": "selected-text-model"})
            app = create_app(root, local_transcriber_factory=object)
            with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                result = client.post("/api/sessions", json={"sample_rate": 48000, "channels": 2})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["transcription_mode"], "local")
                self.assertNotIn("private-translation-key", result.text)


class RecognitionPromptTests(unittest.IsolatedAsyncioTestCase):
    async def captured_prompt(self, mode, glossary=""):
        captured = {}

        class Transcriber:
            def __init__(self, **options):
                captured.update(options)

            async def connect(self):
                pass

        settings = {**DEFAULT_SETTINGS, "transcription_mode": mode,
                    "api_base_url": "https://example.test/v1", "glossary": glossary}
        live = LiveSession({}, settings, None, None, Transcriber, None)
        await live.connect_transcriber()
        return captured["prompt"]

    async def test_local_recognition_has_no_instruction_prompt(self):
        self.assertEqual(await self.captured_prompt("local"), "")

    async def test_local_recognition_preserves_only_glossary(self):
        glossary = "星街すいせい、宝鐘マリン"
        self.assertEqual(await self.captured_prompt("local", glossary), glossary)

    async def test_cloud_recognition_retains_instruction_and_glossary(self):
        prompt = await self.captured_prompt("realtime", "星街すいせい")
        self.assertIn("聞こえた日本語だけを忠実に文字起こししてください", prompt)
        self.assertIn("星街すいせい", prompt)
