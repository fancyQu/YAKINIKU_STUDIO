"""Application-level live invariants using deterministic local-only adapters."""
import asyncio
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from yakiniku.server import create_app
from yakiniku.audio import WaveWriter
from yakiniku.live_config import live_snapshot, resolve_live_settings
from yakiniku.storage import SessionStore, SettingsStore


class RuntimeDouble:
    def __init__(self):
        self.state = "not_prepared"
        self.prepares = []
        self.recognitions = []
        self.prepare_gate = None
        self.prepare_started = threading.Event()
        self.model_source = {"repo_id": "fixture/small", "revision": "revision-one", "weights_sha256": "fixture-hash"}

    def status(self, backend="mlx", model="mlx-whisper-small"):
        return {"state": self.state, "backend": backend, "model": model,
                "path": "/local/models/" + model, "version": "fixture-revision", "error": "", "source": dict(self.model_source)}

    def describe_models(self):
        return [{"backend": "mlx", "model": "mlx-whisper-small", "label": "Small"}]

    async def prepare(self, backend="mlx", model="mlx-whisper-small"):
        self.prepares.append((backend, model))
        self.state = "loading"
        self.prepare_started.set()
        if self.prepare_gate is not None:
            await asyncio.to_thread(self.prepare_gate.wait)
        self.state = "ready"
        return self.status(backend, model)

    async def transcribe_pcm(self, pcm, **options):
        self.recognitions.append({"bytes": len(pcm), **options})
        await asyncio.sleep(0)
        return {"status": "done", "ja": "試験です。"}


class LiveAPITests(unittest.TestCase):
    origin = "http://127.0.0.1:8765"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = SettingsStore(self.root)
        self.sessions = SessionStore(self.root / "recordings")
        self.runtime = RuntimeDouble()
        self.translations = []
        outer = self

        class TranslatorDouble:
            def __init__(self, **options): self.options = options
            async def correct(self, *args, **kwargs): raise AssertionError("live must not request proofreading")
            async def translate(self, japanese, context=None):
                outer.translations.append({"ja": japanese, "context": context, "model": self.options["model"]})
                await asyncio.sleep(0)
                return "这是测试。"
            async def close(self): pass

        self.translator_factory = TranslatorDouble
        self.app = create_app(self.root, translator_factory=TranslatorDouble, live_model_runtime=self.runtime)
        self.client = TestClient(self.app, base_url=self.origin)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.settings.save({
            "service_profiles_enabled": True,
            "service_profiles": [{"id": "text", "name": "Text", "scope": "text", "base_url": "https://text.example/v1", "api_key": "private-test-key", "text_api": "responses", "enabled": True}],
            "task_routes": {"file_asr": {"provider_id": "", "model": ""}, "correction": {"provider_id": "", "model": ""}, "translation": {"provider_id": "text", "model": "file-model"}},
            "live_settings": {"backend": "mlx", "model": "mlx-whisper-small", "translation_route": {"provider_id": "text", "model": "live-model"}, "glossary": "", "segment_seconds": 5, "silence_ms": 600},
        })

    def until(self, socket, kind):
        events = []
        for _ in range(60):
            value = socket.receive_json()
            events.append(value)
            if value.get("type") == kind: return value, events
        self.fail("Missing event " + kind)

    def prepare(self):
        response = self.client.post("/api/live-model/prepare", json={"backend": "mlx", "model": "mlx-whisper-small"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "ready")

    def create(self):
        response = self.client.post("/api/sessions", json={"title": "Live fixture"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_model_readiness_gate_prevents_creating_an_unready_session(self):
        response = self.client.post("/api/sessions", json={})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("就绪", response.json()["detail"])
        self.assertEqual(self.sessions.list(), [])
        self.assertEqual(self.runtime.recognitions, [])
        self.prepare()
        created = self.create()
        self.assertEqual(created["transcription_backend"], "mlx")
        self.assertNotIn("private-test-key", json.dumps(created))

    def test_only_live_translation_is_required_and_file_configuration_is_unchanged(self):
        before = self.settings._stored()
        self.prepare()
        created = self.create()
        self.assertEqual(created["translation_model"], "live-model")
        self.assertFalse(self.settings.public()["import_configuration_ready"])
        self.assertEqual(self.settings._stored(), before)
        public = self.client.get("/api/live-settings").json()
        self.assertTrue(public["translation_ready"])
        self.assertTrue(public["locked"])
        self.assertNotIn("private-test-key", json.dumps(public))

    def test_tail_flush_ack_and_audio_saved_precedes_subtitle_completion(self):
        self.prepare()
        created = self.create()
        before = self.settings.path.read_bytes()
        with self.client.websocket_connect(f"ws://127.0.0.1:8765/ws/{created['id']}?token={created['token']}", headers={"origin": self.origin}) as socket:
            self.until(socket, "ready")
            socket.send_bytes(b"\x00" + struct.pack("<hh", 1200, 1200) * 57600)
            socket.send_bytes(b"\x01" + struct.pack("<h", 1200) * 28800)
            socket.send_json({"type": "flush"})
            ack, first_events = self.until(socket, "audio_ack")
            self.assertEqual(ack["received_frames"], 57600)
            socket.send_json({"type": "stop"})
            saved, events = self.until(socket, "saved")
        kinds = [e["type"] for e in events]
        self.assertLess(kinds.index("audio_saved"), kinds.index("saved"))
        session = saved["session"]
        self.assertTrue(session["audio_saved"])
        self.assertEqual(session["duration_ms"], 1200)
        self.assertEqual(session["status"], "completed")
        self.assertEqual(len(session["segments"]), 1)
        self.assertEqual(session["segments"][0]["start_ms"], 0)
        self.assertEqual(session["segments"][0]["end_ms"], 1200)
        self.assertEqual(session["segments"][0]["ja_raw"], "試験です。")
        self.assertEqual(session["segments"][0]["translation_status"], "done")
        self.assertEqual(len(self.translations), 1)
        self.assertEqual(self.translations[0]["model"], "live-model")
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertTrue(all(event["session_id"] == created["id"] for event in first_events + events))
        self.assertNotIn("private-test-key", json.dumps(session))

    def test_old_recording_retry_preserves_corrected_japanese_and_paid_translations(self):
        old = self.sessions.create("legacy")
        old.update(status="completed", duration_ms=2000, transcription_model="tiny", segments=[
            {"id": "one", "start_ms": 0, "end_ms": 1000, "ja_raw": "誤字", "ja": "校正済みの原文", "zh": "", "translation_status": "failed", "correction_status": "done"},
            {"id": "two", "start_ms": 1000, "end_ms": 2000, "ja_raw": "原稿", "ja": "既存の日本語", "zh": "已有翻译", "translation_status": "done", "correction_status": "done"},
        ])
        self.sessions.export(old)
        response = self.client.post(f"/api/sessions/{old['id']}/retry-translations")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["segments"][0]["ja"], "校正済みの原文")
        self.assertEqual(result["segments"][0]["ja_raw"], "誤字")
        self.assertEqual(result["segments"][1]["zh"], "已有翻译")
        self.assertEqual([item["ja"] for item in self.translations], ["校正済みの原文"])
        self.assertEqual(self.runtime.prepares, [])
        self.assertEqual(self.runtime.recognitions, [])
        self.assertTrue(result["legacy_translation_only"])
        self.assertEqual(result["live_snapshot"]["translation_route"]["model"], "live-model")
        response = self.client.post(f"/api/sessions/{old['id']}/retry-translations")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.translations), 1)

    def test_raw_audio_tail_without_recognition_copy_is_backfilled_after_audio_saved(self):
        self.prepare(); created = self.create()
        with self.client.websocket_connect(f"ws://127.0.0.1:8765/ws/{created['id']}?token={created['token']}", headers={"origin": self.origin}) as socket:
            self.until(socket, "ready")
            socket.send_bytes(b"\x00" + struct.pack("<hh", 1200, 1200) * 48000)
            socket.send_json({"type": "stop"})
            saved, events = self.until(socket, "saved")
        kinds = [item["type"] for item in events]
        self.assertLess(kinds.index("audio_saved"), kinds.index("saved"))
        result = saved["session"]
        self.assertEqual(result["duration_ms"], 1000)
        self.assertTrue(result["audio_saved"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["end_ms"], 1000)
        self.assertEqual(result["segments"][0]["translation_status"], "done")
        self.assertEqual(len(self.runtime.recognitions), 1)

    def seed_interrupted_raw(self, *, with_segment=False):
        settings = resolve_live_settings(self.settings)
        old = self.sessions.create("recover raw")
        old.update(source_type="live", status="recording", duration_ms=1000,
                   transcription_backend="mlx", transcription_model="mlx-whisper-small",
                   live_snapshot=live_snapshot(settings, self.runtime.status()),
                   live_committed_until_ms=1000 if with_segment else 0)
        if with_segment:
            old["segments"] = [{"id": "recover-one", "segment_id": "recover-one", "session_id": old["id"], "sequence": 1,
                                "start_ms": 0, "end_ms": 1000, "ja_raw": "", "ja": "", "zh": "",
                                "asr_status": "deferred", "translation_status": "deferred"}]
        writer = WaveWriter(self.sessions.directory(old["id"]) / "audio.wav", 48000, 2)
        writer.write(struct.pack("<hh", 1200, 1200) * 48000); writer.sync(); writer.close()
        self.sessions.save(old)
        return old

    def test_restart_recovers_received_raw_tail_and_retries_without_duplicate_segments(self):
        old = self.seed_interrupted_raw()
        restarted = create_app(self.root, translator_factory=self.translator_factory, live_model_runtime=self.runtime)
        with TestClient(restarted, base_url=self.origin) as client:
            recovered = self.sessions.load(old["id"])
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered["live_recovery_from_ms"], 0)
            self.assertTrue(recovered["audio_saved"])
            response = client.post(f"/api/sessions/{old['id']}/retry-translations")
            self.assertEqual(response.status_code, 200, response.text)
            first = response.json()
            self.assertEqual(first["status"], "completed")
            self.assertEqual(len(first["segments"]), 1)
            self.assertEqual(first["segments"][0]["translation_status"], "done")
            response = client.post(f"/api/sessions/{old['id']}/retry-translations")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([s["id"] for s in response.json()["segments"]], [s["id"] for s in first["segments"]])
            self.assertEqual(len(self.runtime.recognitions), 1)
            self.assertEqual(len(self.translations), 1)

    def test_changed_local_model_revision_cannot_silently_backfill_original_session(self):
        old = self.seed_interrupted_raw(with_segment=True)
        audio = self.sessions.file_path(old["id"], "audio.wav").read_bytes()
        self.runtime.model_source["revision"] = "replacement-revision"
        response = self.client.post(f"/api/sessions/{old['id']}/retry-translations")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(any(word in response.json()["detail"] for word in ("版本", "权重", "模型")))
        self.assertEqual(self.runtime.recognitions, [])
        self.assertEqual(self.translations, [])
        self.assertEqual(self.sessions.file_path(old["id"], "audio.wav").read_bytes(), audio)

    def test_slow_model_preparation_locks_capture_import_configuration_and_second_prepare(self):
        self.runtime.prepare_gate = threading.Event()
        responses = []
        errors = []
        def run_prepare():
            try:
                responses.append(self.client.post("/api/live-model/prepare", json={"backend": "mlx", "model": "mlx-whisper-small"}))
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=run_prepare)
        worker.start()
        try:
            self.assertTrue(self.runtime.prepare_started.wait(timeout=2), "fake preparation did not start")
            self.assertTrue(self.client.get("/api/health").json()["model_preparing"])
            self.assertEqual(self.client.post("/api/sessions", json={}).status_code, 409)
            self.assertEqual(self.client.post("/api/imports", content=b"test").status_code, 409)
            body = self.settings._stored()["live_settings"]
            self.assertEqual(self.client.put("/api/live-settings", json=body).status_code, 409)
            self.assertEqual(self.client.post("/api/live-model/prepare", json={"backend": "tiny", "model": "tiny"}).status_code, 409)
            self.assertTrue(self.client.get("/api/live-settings").json()["locked"])
            self.assertEqual(len(self.runtime.prepares), 1)
        finally:
            self.runtime.prepare_gate.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses[0].status_code, 200, responses[0].text)
        self.assertFalse(self.client.get("/api/health").json()["model_preparing"])

    def test_reserved_capture_locks_live_configuration_and_model_preparation(self):
        self.prepare(); self.create()
        before = self.settings.path.read_bytes()
        body = self.settings._stored()["live_settings"]
        body["model"] = "mlx-whisper-large-v3-turbo"
        self.assertEqual(self.client.put("/api/live-settings", json=body).status_code, 409)
        self.assertEqual(self.client.post("/api/live-model/prepare", json={"backend": "tiny", "model": "tiny"}).status_code, 409)
        self.assertEqual(self.settings.path.read_bytes(), before)


if __name__ == "__main__": unittest.main()
