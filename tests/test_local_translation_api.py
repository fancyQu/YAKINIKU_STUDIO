"""Local live translation routing, explicit downloads and persistence boundaries."""
import asyncio
import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_live_api import RuntimeDouble
from tests.test_model_download_api import DownloadDouble
from yakiniku.live_config import LiveSettingsInput, live_snapshot, resolve_live_settings, save_live_settings
from yakiniku.local_translation_models import MODEL_CATALOG
from yakiniku.server import create_app
from yakiniku.storage import SettingsStore

MODEL = "qwen3-4b-instruct-2507-4bit"
OTHER = "qwen2.5-3b-instruct-4bit"


class TranslationRuntimeDouble:
    def __init__(self, root):
        self.root = root
        self.state = "not_prepared"
        self.prepares = []
        self.translations = []
        self.revision = "fixed-local-revision"

    def status(self, model=MODEL):
        return {"state": self.state, "model": model, "prepared_files": True,
                "path": str(self.root / model), "version": "test-version", "error": "",
                "source": {"revision": self.revision, "weights_sha256": "test-weights"}}

    async def prepare(self, model=MODEL):
        self.prepares.append(model)
        await asyncio.sleep(0)
        self.state = "ready"
        return self.status(model)

    async def translate(self, text, context=None, model=MODEL, glossary=""):
        self.translations.append({"text": text, "model": model, "context": context, "glossary": glossary})
        await asyncio.sleep(0)
        return "本机翻译结果。"


class LocalTranslationAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = SettingsStore(self.root)
        self.asr = RuntimeDouble()
        self.asr.state = "ready"
        self.local = TranslationRuntimeDouble(self.root / "translation")
        self.downloads = DownloadDouble(self.root / "translation")
        self.asr_downloads = DownloadDouble(self.root / "models")
        self.cloud_calls = []
        outer = self

        class CloudTranslator:
            def __init__(self, **options):
                outer.cloud_calls.append(options)
            async def translate(self, *args, **kwargs):
                raise AssertionError("local live mode must not send text to cloud")
            async def close(self):
                pass

        self.app = create_app(self.root, live_model_runtime=self.asr, translator_factory=CloudTranslator,
                              local_translation_runtime=self.local, local_translation_downloader=self.downloads,
                              live_model_downloader=self.asr_downloads)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.body = {"backend": "mlx", "model": "mlx-whisper-small", "translation_backend": "local",
                     "local_translation_model": MODEL, "translation_route": {"provider_id": "", "model": ""},
                     "glossary": "試験 → 测试", "segment_seconds": 3, "silence_ms": 600}
        self.store.save({"live_settings": self.body})

    def test_local_configuration_requires_no_cloud_credentials_and_preserves_file_settings(self):
        self.store.save({"service_profiles_enabled": True, "task_routes": {
            "file_asr": {"provider_id": "file", "model": "asr-1.0"},
            "correction": {"provider_id": "quality", "model": "proof"},
            "translation": {"provider_id": "quality", "model": "translate"}},
            "service_profiles": [{"id": "file", "name": "MiniMax", "scope": "asr", "speech_api": "minimax",
                                  "base_url": "https://speech.example/v1", "api_key": "private-file"},
                                 {"id": "quality", "name": "Text", "scope": "text", "text_api": "responses",
                                  "base_url": "https://text.example/v1", "api_key": "private-text"}]})
        before = self.store._stored()
        saved = self.client.put("/api/live-settings", json=self.body)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertFalse(saved.json()["translation_ready"])
        for key, value in before.items():
            if key != "live_settings":
                self.assertEqual(self.store._stored()[key], value)
        resolved = resolve_live_settings(self.store)
        self.assertEqual(resolved["translation_api"], "local_mlx")
        self.assertEqual(resolved["translation_api_key"], "")
        self.assertEqual(resolved["translation_base_url"], "")
        self.assertNotIn("private-", saved.text)

    def test_catalog_is_local_read_only_and_explains_memory_separately_from_disk(self):
        before = self.store.path.read_bytes()
        result = self.client.get("/api/live-translation/models")
        self.assertEqual(result.status_code, 200, result.text)
        models = result.json()["models"]
        self.assertEqual({item["id"] for item in models}, {item["id"] for item in MODEL_CATALOG})
        for item in models:
            self.assertGreater(item["disk_bytes"], 0)
            self.assertGreater(item["memory_gb_min"], 0)
            self.assertGreaterEqual(item["memory_gb_max"], item["memory_gb_min"])
            self.assertIn("memory_estimate_note", item)
            self.assertIn("download", item)
        self.assertEqual(self.downloads.starts, [])
        self.assertEqual(self.local.prepares, [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_prepare_does_not_save_selection_download_or_require_a_key(self):
        before = self.store.path.read_bytes()
        result = self.client.post("/api/live-translation/prepare", json={"model": OTHER})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["state"], "ready")
        self.assertEqual(self.local.prepares, [OTHER])
        self.assertEqual(self.downloads.starts, [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_local_unready_cannot_start_and_ready_can_start_without_api(self):
        failed = self.client.post("/api/sessions", json={})
        self.assertEqual(failed.status_code, 400, failed.text)
        self.assertIn("本地翻译模型", failed.text)
        self.local.state = "ready"
        result = self.client.post("/api/sessions", json={})
        self.assertEqual(result.status_code, 200, result.text)
        session = result.json()
        self.assertEqual(session["translation_backend"], "local")
        self.assertEqual(session["translation_model"], MODEL)
        self.assertEqual(session["live_snapshot"]["translation_backend"], "local")
        self.assertEqual(session["live_snapshot"]["translation_model_source"]["source"]["revision"], self.local.revision)
        self.assertEqual(self.cloud_calls, [])

    def test_live_capture_calls_local_translator_and_preserves_japanese_and_timeline(self):
        self.local.state = "ready"
        session = self.client.post("/api/sessions", json={}).json()
        events = []
        with self.client.websocket_connect(
                f"ws://127.0.0.1:8765/ws/{session['id']}?token={session['token']}",
                headers={"origin": "http://127.0.0.1:8765"}) as socket:
            for _ in range(40):
                if socket.receive_json().get("type") == "ready":
                    break
            socket.send_bytes(b"\x00" + struct.pack("<hh", 1600, 1600) * 57600)
            socket.send_bytes(b"\x01" + struct.pack("<h", 1600) * 28800)
            socket.send_json({"type": "stop"})
            for _ in range(60):
                event = socket.receive_json()
                events.append(event)
                if event.get("type") == "saved":
                    break
            else:
                self.fail("Local live recording did not finish")
        saved = self.client.get(f"/api/sessions/{session['id']}").json()
        self.assertEqual(saved["segments"][0]["zh"], "本机翻译结果。")
        self.assertEqual(saved["segments"][0]["ja_raw"], "試験です。")
        self.assertGreater(saved["segments"][0]["end_ms"], saved["segments"][0]["start_ms"])
        self.assertTrue(self.local.translations)
        self.assertEqual(self.local.translations[0]["model"], MODEL)
        self.assertEqual(self.cloud_calls, [])

    def test_invalid_models_and_non_boolean_download_consent_are_rejected(self):
        for model in ("../outside", "/tmp/outside", "https://example.com/model", "invented"):
            for endpoint in ("status?model=" + model, "models?unused=" + model):
                if endpoint.startswith("status"):
                    self.assertEqual(self.client.get("/api/live-translation/" + endpoint).status_code, 400)
            self.assertEqual(self.client.post("/api/live-translation/prepare", json={"model": model}).status_code, 400)
            self.assertEqual(self.client.post("/api/live-translation/download", json={"model": model, "consent": True}).status_code, 400)
        for consent in (False, 1, "true", None):
            response = self.client.post("/api/live-translation/download", json={"model": MODEL, "consent": consent})
            self.assertIn(response.status_code, (400, 422))
        self.assertEqual(self.downloads.starts, [])

    def test_download_is_explicit_and_does_not_prepare_or_change_saved_model(self):
        before = self.store.path.read_bytes()
        response = self.client.post("/api/live-translation/download", json={"model": OTHER, "consent": True})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.downloads.starts, [("mlx", OTHER)])
        self.assertEqual(self.local.prepares, [])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(self.client.get("/api/health").json()["model_downloading"])
        status = self.client.get("/api/live-translation/status?model=" + MODEL).json()
        self.assertEqual(status["active_download"]["model"], OTHER)
        self.assertEqual(self.client.put("/api/live-settings", json=self.body).status_code, 409)
        self.assertEqual(self.client.post("/api/sessions", json={}).status_code, 409)
        self.assertEqual(self.client.post("/api/live-model/download", json={"model": "mlx-whisper-small", "consent": True}).status_code, 409)
        cancelled = self.client.post("/api/live-translation/download/cancel", json={"model": OTHER})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertFalse(self.client.get("/api/health").json()["model_downloading"])

    def test_asr_download_prevents_text_download_or_preparation(self):
        self.asr_downloads.states["mlx", "mlx-whisper-small"] = "downloading"
        for path, body in (("download", {"model": MODEL, "consent": True}), ("prepare", {"model": MODEL})):
            response = self.client.post("/api/live-translation/" + path, json=body)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.downloads.starts, [])
        self.assertEqual(self.local.prepares, [])

    def test_local_snapshot_survives_switch_to_cloud_without_using_cloud(self):
        resolved = resolve_live_settings(self.store)
        snap = live_snapshot(resolved, translation_state=self.local.status())
        raw = self.store._stored()
        raw["live_settings"] = {**self.body, "translation_backend": "cloud", "local_translation_model": OTHER}
        self.store.save(raw)
        retry = resolve_live_settings(self.store, snapshot=snap)
        self.assertEqual(retry["translation_backend"], "local")
        self.assertEqual(retry["translation_model"], MODEL)
        self.assertEqual(retry["translation_api_key"], "")

    def test_unknown_local_model_cannot_be_saved(self):
        before = self.store.path.read_bytes()
        response = self.client.put("/api/live-settings", json={**self.body, "local_translation_model": "../outside"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
