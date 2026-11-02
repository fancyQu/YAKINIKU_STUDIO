"""Explicit MLX downloads and application locks use local, inert doubles only."""

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.test_live_api import RuntimeDouble
from yakiniku.server import create_app
from yakiniku.storage import SessionStore, SettingsStore


class DownloadDouble:
    def __init__(self, root):
        self.root = root
        self.states = {}
        self.starts = []
        self.cancels = []
        self.shutdowns = 0

    def status(self, backend="mlx", model="mlx-whisper-small"):
        return {"backend": backend, "model": model,
                "state": self.states.get((backend, model), "missing"),
                "downloaded_bytes": 0, "total_bytes": 100,
                "message": "local fixture", "error": "",
                "repo_id": "fixture/" + model, "revision": "fixture-fixed-revision"}

    def is_active(self):
        return any(state in {"downloading", "verifying"} for state in self.states.values())

    def active_status(self):
        for (backend, model), state in self.states.items():
            if state in {"downloading", "verifying"}:
                return self.status(backend, model)
        return None

    async def start(self, backend="mlx", model="mlx-whisper-small"):
        self.starts.append((backend, model))
        self.states[backend, model] = "downloading"
        return self.status(backend, model)

    async def cancel(self, backend="mlx", model="mlx-whisper-small"):
        self.cancels.append((backend, model))
        self.states[backend, model] = "cancelled"
        return self.status(backend, model)

    async def shutdown(self):
        self.shutdowns += 1
        for key, state in self.states.items():
            if state in {"downloading", "verifying"}:
                self.states[key] = "cancelled"


class ModelDownloadAPITests(unittest.TestCase):
    origin = "http://127.0.0.1:8765"
    model = "mlx-whisper-small"
    alternative = "mlx-whisper-large-v3-turbo"
    secret = "private-model-download-fixture-key"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = SettingsStore(self.root)
        self.sessions = SessionStore(self.root / "recordings")
        self.runtime = RuntimeDouble()
        self.downloads = DownloadDouble(self.root / "models")
        self.translation_entered = threading.Event()
        self.translation_release = threading.Event()
        self.block_translation = False
        self.addCleanup(self.translation_release.set)
        owner = self

        class TextDouble:
            def __init__(self, **options):
                self.model = options["model"]
                self.api_format = options["api_format"]

            async def correct(self, text, context=None):
                return text

            async def translate(self, text, context=None):
                owner.translation_entered.set()
                while owner.block_translation and not owner.translation_release.is_set():
                    await asyncio.sleep(.005)
                return "fixture translation"

            async def close(self):
                pass

        self.app = create_app(self.root, translator_factory=TextDouble,
                              live_model_runtime=self.runtime,
                              live_model_downloader=self.downloads)
        self.client = TestClient(self.app, base_url=self.origin)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.settings.save({
            "service_profiles_enabled": True,
            "service_profiles": [{"id": "text", "name": "Fixture text", "scope": "text",
                                  "base_url": "https://text.example/v1", "api_key": self.secret,
                                  "text_api": "responses", "enabled": True}],
            "task_routes": {"file_asr": {"provider_id": "", "model": ""},
                            "correction": {"provider_id": "text", "model": "gpt-fixture-correction"},
                            "translation": {"provider_id": "text", "model": "gpt-fixture-translation"}},
            "live_settings": {"backend": "mlx", "model": self.model,
                              "translation_route": {"provider_id": "text", "model": "fixture-live"},
                              "glossary": "fixture glossary", "segment_seconds": 5, "silence_ms": 600},
        })

    def download(self, model=None, **values):
        return self.client.post("/api/live-model/download",
                                json={"backend": "mlx", "model": model or self.model,
                                      "consent": True, **values})

    def assert_private(self, response):
        self.assertNotIn(self.secret, response.text)

    def test_reading_and_selecting_model_never_starts_a_download(self):
        before = self.settings.path.read_bytes()
        for model in (self.model, self.alternative, self.model):
            response = self.client.get(f"/api/live-model/status?backend=mlx&model={model}")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["download"]["model"], model)
            self.assertEqual(result["download"]["state"], "missing")
            self.assertIn("state", result)
            self.assertIn("models", result)
            self.assert_private(response)
        self.assertEqual(self.downloads.starts, [])
        self.assertEqual(self.runtime.prepares, [])
        self.assertEqual(self.settings.path.read_bytes(), before)

    def test_download_requires_explicit_boolean_consent(self):
        examples = [{"backend": "mlx", "model": self.model},
                    {"backend": "mlx", "model": self.model, "consent": False},
                    {"backend": "mlx", "model": self.model, "consent": "true"},
                    {"backend": "mlx", "model": self.model, "consent": 1}]
        for body in examples:
            with self.subTest(body=body):
                response = self.client.post("/api/live-model/download", json=body)
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assert_private(response)
        self.assertEqual(self.downloads.starts, [])

    def test_only_known_mlx_models_can_be_downloaded(self):
        for values in ({"backend": "tiny", "model": "tiny"},
                       {"backend": "mlx", "model": "../outside"},
                       {"backend": "mlx", "model": "/tmp/outside"},
                       {"backend": "mlx", "model": "https://external.example/model"},
                       {"backend": "mlx", "model": "invented-model"}):
            with self.subTest(values=values):
                response = self.client.post("/api/live-model/download", json={"consent": True, **values})
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.downloads.starts, [])

    def test_explicit_download_does_not_save_selection_prepare_or_record(self):
        before = self.settings.path.read_bytes()
        response = self.download(self.alternative)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "downloading")
        self.assertEqual(response.json()["model"], self.alternative)
        self.assertEqual(self.downloads.starts, [("mlx", self.alternative)])
        self.assertEqual(self.runtime.prepares, [])
        self.assertEqual(self.sessions.list(), [])
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assert_private(response)

    def test_same_download_is_idempotent_and_another_model_is_locked(self):
        self.assertEqual(self.download().status_code, 202)
        repeated = self.download()
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json()["state"], "downloading")
        self.assertEqual(self.downloads.starts, [("mlx", self.model)])
        other = self.download(self.alternative)
        self.assertEqual(other.status_code, 409, other.text)
        self.downloads.states["mlx", self.model] = "verifying"
        self.assertEqual(self.download().status_code, 202)
        self.assertEqual(self.downloads.starts, [("mlx", self.model)])

    def test_status_exposes_unsaved_other_model_download_for_recovery_and_cancel(self):
        before = self.settings.path.read_bytes()
        self.assertEqual(self.download(self.alternative).status_code, 202)
        for path in ("/api/live-model/status",
                     f"/api/live-model/status?backend=mlx&model={self.model}"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            state = response.json()
            self.assertEqual(state["download"]["model"], self.model)
            self.assertEqual(state["download"]["state"], "missing")
            self.assertEqual(state["active_download"]["model"], self.alternative)
            self.assertEqual(state["active_download"]["state"], "downloading")
            self.assert_private(response)
        self.assertEqual(self.downloads.starts, [("mlx", self.alternative)])
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertEqual(self.runtime.prepares, [])
        response = self.client.post("/api/live-model/download/cancel",
                                    json={"backend": "mlx", "model": self.alternative})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "cancelled")
        self.assertEqual(self.downloads.cancels, [("mlx", self.alternative)])
        self.assertIsNone(self.client.get("/api/live-model/status").json()["active_download"])
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertEqual(self.runtime.prepares, [])

    def test_active_download_locks_capture_import_configuration_and_preparation(self):
        before = self.settings.path.read_bytes()
        self.assertEqual(self.download().status_code, 202)
        health = self.client.get("/api/health").json()
        self.assertTrue(health["model_downloading"])
        self.assertFalse(health["model_preparing"])
        self.assertFalse(health["active"])
        self.assertTrue(self.client.get("/api/live-settings").json()["locked"])
        self.assertTrue(self.client.get("/api/service-config").json()["locked"])
        self.assertTrue(self.client.get("/api/subtitle-settings").json()["locked"])
        actions = [
            self.client.post("/api/sessions", json={}),
            self.client.post("/api/imports", content=b"not uploaded"),
            self.client.post("/api/live-model/prepare", json={"backend": "mlx", "model": self.model}),
            self.client.put("/api/live-settings", json=self.settings._stored()["live_settings"]),
            self.client.put("/api/subtitle-settings", json={"enabled": True, "max_chars": 24, "max_seconds": 6}),
            self.client.put("/api/service-config/provider", json={"id": "text", "name": "not saved"}),
        ]
        for response in actions:
            self.assertEqual(response.status_code, 409, response.text)
            self.assert_private(response)
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertEqual(self.sessions.list(), [])
        self.assertEqual(self.runtime.prepares, [])

    def test_import_upload_prevents_model_download(self):
        self.app.state.imports.uploading = True
        try:
            response = self.download()
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.downloads.starts, [])
        finally:
            self.app.state.imports.uploading = False

    def test_pending_live_reservation_prevents_model_download(self):
        self.runtime.state = "ready"
        reserved = self.client.post("/api/sessions", json={})
        self.assertEqual(reserved.status_code, 200, reserved.text)
        response = self.download()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.downloads.starts, [])

    def test_active_live_recording_prevents_model_download(self):
        self.runtime.state = "ready"
        response = self.client.post("/api/sessions", json={})
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()
        with self.client.websocket_connect(f"ws://127.0.0.1:8765/ws/{session['id']}?token={session['token']}",
                                           headers={"origin": self.origin}) as socket:
            for _ in range(30):
                if socket.receive_json().get("type") == "ready":
                    break
            else:
                self.fail("fixture recording did not become ready")
            response = self.download()
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.downloads.starts, [])
            socket.send_json({"type": "stop"})
            for _ in range(30):
                if socket.receive_json().get("type") == "saved":
                    break
            else:
                self.fail("fixture recording did not finish")

    def test_model_warmup_prevents_download(self):
        self.runtime.prepare_gate = threading.Event()
        responses, errors = [], []

        def prepare():
            try:
                responses.append(self.client.post("/api/live-model/prepare", json={"backend": "mlx", "model": self.model}))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=prepare)
        thread.start()
        try:
            self.assertTrue(self.runtime.prepare_started.wait(3))
            response = self.download()
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.downloads.starts, [])
        finally:
            self.runtime.prepare_gate.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses[0].status_code, 200, responses[0].text)

    def test_subtitle_backfill_prevents_download(self):
        session = self.sessions.create("local retry fixture")
        session.update(source_type="import", status="completed", duration_ms=1000,
                       segments=[{"id": "one", "start_ms": 0, "end_ms": 1000,
                                  "ja": "fixture", "zh": "", "translation_status": "failed"}])
        self.sessions.export(session)
        self.block_translation = True
        responses, errors = [], []

        def retry():
            try:
                responses.append(self.client.post(f"/api/sessions/{session['id']}/retry-translations"))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=retry)
        thread.start()
        try:
            self.assertTrue(self.translation_entered.wait(3))
            response = self.download()
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.downloads.starts, [])
        finally:
            self.translation_release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses[0].status_code, 200, responses[0].text)

    def test_cancel_current_download_is_allowed_and_unlocks_configuration(self):
        self.assertEqual(self.download().status_code, 202)
        response = self.client.post("/api/live-model/download/cancel", json={"backend": "mlx", "model": self.model})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "cancelled")
        self.assertEqual(self.downloads.cancels, [("mlx", self.model)])
        self.assertFalse(self.client.get("/api/health").json()["model_downloading"])
        self.assertFalse(self.client.get("/api/live-settings").json()["locked"])
        self.assertEqual(self.downloads.starts, [("mlx", self.model)])
        self.assertEqual(self.runtime.prepares, [])

    def test_application_shutdown_stops_download_manager(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        manager = DownloadDouble(root / "models")
        app = create_app(root, live_model_runtime=RuntimeDouble(), live_model_downloader=manager)
        with TestClient(app, base_url=self.origin) as client:
            result = client.post("/api/live-model/download", json={"backend": "mlx", "model": self.model, "consent": True})
            self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(manager.shutdowns, 1)
        self.assertFalse(manager.is_active())


if __name__ == "__main__":
    unittest.main()
