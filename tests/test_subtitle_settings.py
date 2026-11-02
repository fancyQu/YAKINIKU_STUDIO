"""Independent subtitle settings and import snapshots use only local fakes."""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from yakiniku.provider import ProviderError
from yakiniku.server import create_app
from yakiniku.storage import SessionStore, SettingsStore
from tests.test_import_audio import wav_bytes
from tests.test_live_api import RuntimeDouble


class SubtitleSettingsTests(unittest.TestCase):
    origin = "http://127.0.0.1:8765"
    speech_key = "private-fixture-subtitle-speech-key"
    text_key = "private-fixture-subtitle-text-key"
    long_japanese = "あいうえおかきくけこ" * 6

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
        self.calls = []
        self.fail_translation = False
        self.block_translation = False
        self.translation_entered = threading.Event()
        self.translation_release = threading.Event()
        self.addCleanup(self.translation_release.set)
        owner = self

        class Speech:
            def __init__(self, **options):
                pass

            async def transcribe(self, audio, *, start_ms, end_ms):
                owner.calls.append(("asr", start_ms, end_ms))
                return [{"ja": owner.long_japanese, "start_ms": start_ms, "end_ms": end_ms,
                         "alignment": "provider", "timestamp_source": "api_segments"}]

            async def close(self):
                pass

        class Text:
            def __init__(self, **options):
                self.model = options["model"]
                self.api_format = options["api_format"]

            async def correct(self, text, context=None):
                owner.calls.append(("correct", text))
                return text

            async def translate(self, text, context=None):
                owner.calls.append(("translate", text))
                owner.translation_entered.set()
                while owner.block_translation and not owner.translation_release.is_set():
                    await asyncio.sleep(.005)
                if owner.fail_translation:
                    raise ProviderError("fixture translation temporarily unavailable")
                return "中文：" + text

            async def close(self):
                pass

        self.app = create_app(self.root, translator_factory=Text,
                              import_file_transcriber_factory=Speech, live_model_runtime=self.runtime)
        self.client = TestClient(self.app, base_url=self.origin)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def configure(self):
        self.settings.save({
            "service_profiles_enabled": True,
            "service_profiles": [
                {"id": "speech", "name": "Speech fixture", "scope": "asr", "vendor": "minimax",
                 "base_url": "https://speech.example/v1", "api_key": self.speech_key,
                 "speech_api": "minimax", "text_api": "chat_completions", "enabled": True},
                {"id": "text", "name": "Text fixture", "scope": "text", "vendor": "custom",
                 "base_url": "https://text.example/v1", "api_key": self.text_key,
                 "speech_api": "none", "text_api": "responses", "enabled": True},
            ],
            "task_routes": {
                "file_asr": {"provider_id": "speech", "model": "fixture-asr", "response_format": "verbose_json"},
                "correction": {"provider_id": "text", "model": "gpt-fixture-proofreading"},
                "translation": {"provider_id": "text", "model": "gpt-fixture-translation"},
            },
            "live_settings": {"backend": "mlx", "model": "mlx-whisper-small",
                              "translation_route": {"provider_id": "text", "model": "fixture-live"},
                              "segment_seconds": 5, "silence_ms": 600, "glossary": "fixture live glossary"},
            "glossary": "fixture file glossary", "segment_seconds": 7,
        })

    def save(self, **body):
        return self.client.put("/api/subtitle-settings", json=body)

    def assert_private(self, response):
        for secret in (self.speech_key, self.text_key):
            self.assertNotIn(secret, response.text)

    def assert_locked(self):
        before = self.settings.path.read_bytes()
        current = self.client.get("/api/subtitle-settings")
        self.assertEqual(current.status_code, 200, current.text)
        self.assertTrue(current.json()["locked"])
        response = self.save(enabled=True, max_chars=16, max_seconds=4)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assert_private(response)

    def prepare_live(self):
        result = self.client.post("/api/live-model/prepare", json={"backend": "mlx", "model": "mlx-whisper-small"})
        self.assertEqual(result.status_code, 200, result.text)

    def wait_result(self, identifier):
        for _ in range(500):
            response = self.client.get(f"/api/imports/{identifier}")
            self.assertEqual(response.status_code, 200, response.text)
            session = response.json()
            if session.get("import_stage") in ("completed", "failed", "interrupted"):
                return session
            time.sleep(.01)
        self.fail("local fake import did not finish")

    def upload(self, seconds=12):
        result = self.client.post("/api/imports", content=wav_bytes(seconds),
                                  headers={"Content-Type": "application/octet-stream", "X-Filename": "fixture.wav"})
        self.assertEqual(result.status_code, 202, result.text)
        self.assert_private(result)
        return self.wait_result(result.json()["id"])

    def test_defaults_are_read_only_and_do_not_expose_service_configuration(self):
        result = self.client.get("/api/subtitle-settings")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"enabled": True, "max_chars": 24, "max_seconds": 6.0, "locked": False})
        self.assertFalse(self.settings.path.exists())

    def test_save_roundtrip_preserves_credentials_routes_and_live_settings(self):
        self.configure()
        previous = self.settings._stored()
        result = self.save(enabled=False, max_chars=18, max_seconds=4.5)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"enabled": False, "max_chars": 18, "max_seconds": 4.5, "locked": False})
        self.assertEqual(self.client.get("/api/subtitle-settings").json(), result.json())
        self.assert_private(result)
        changed = self.settings._stored()
        self.assertEqual(changed["subtitle_split_enabled"], False)
        self.assertEqual(changed["subtitle_max_chars"], 18)
        self.assertEqual(changed["subtitle_max_seconds"], 4.5)
        for key, value in previous.items():
            if key not in {"subtitle_split_enabled", "subtitle_max_chars", "subtitle_max_seconds"}:
                self.assertEqual(changed[key], value, key)

    def test_documented_range_boundaries_are_accepted(self):
        for chars, seconds in ((8, 2), (80, 15)):
            with self.subTest(chars=chars, seconds=seconds):
                result = self.save(enabled=True, max_chars=chars, max_seconds=seconds)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["max_chars"], chars)
                self.assertEqual(result.json()["max_seconds"], seconds)

    def test_invalid_limits_and_types_do_not_modify_saved_configuration(self):
        self.configure()
        previous = self.settings.path.read_bytes()
        examples = [
            {"max_chars": 7}, {"max_chars": 81}, {"max_chars": 12.5},
            {"max_chars": True}, {"max_chars": "24"}, {"max_chars": []},
            {"max_seconds": 1.99}, {"max_seconds": 15.01}, {"max_seconds": False},
            {"max_seconds": {}}, {"enabled": "false"}, {"enabled": 0},
        ]
        for invalid in examples:
            with self.subTest(invalid=invalid):
                result = self.save(**{"enabled": True, "max_chars": 24, "max_seconds": 6, **invalid})
                self.assertIn(result.status_code, (400, 422), result.text)
                self.assertEqual(self.settings.path.read_bytes(), previous)
                self.assert_private(result)

    def test_import_upload_locks_settings(self):
        self.configure()
        self.app.state.imports.uploading = True
        try:
            self.assert_locked()
        finally:
            self.app.state.imports.uploading = False
        self.assertFalse(self.client.get("/api/subtitle-settings").json()["locked"])

    def test_pending_live_reservation_locks_settings(self):
        self.configure()
        self.prepare_live()
        result = self.client.post("/api/sessions", json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assert_locked()

    def test_active_live_session_locks_settings_until_saved(self):
        self.configure()
        self.prepare_live()
        response = self.client.post("/api/sessions", json={})
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()
        with self.client.websocket_connect(f"ws://127.0.0.1:8765/ws/{session['id']}?token={session['token']}",
                                           headers={"origin": self.origin}) as socket:
            for _ in range(30):
                if socket.receive_json().get("type") == "ready":
                    break
            else:
                self.fail("live session did not become ready")
            self.assert_locked()
            socket.send_json({"type": "stop"})
            for _ in range(30):
                if socket.receive_json().get("type") == "saved":
                    break
            else:
                self.fail("live session did not finish saving")
        self.assertFalse(self.client.get("/api/subtitle-settings").json()["locked"])

    def test_model_preparation_locks_settings(self):
        self.configure()
        self.runtime.prepare_gate = threading.Event()
        results = []
        thread = threading.Thread(target=lambda: results.append(self.client.post("/api/live-model/prepare", json={})))
        thread.start()
        try:
            self.assertTrue(self.runtime.prepare_started.wait(3))
            self.assert_locked()
        finally:
            self.runtime.prepare_gate.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status_code, 200, results[0].text)
        self.assertFalse(self.client.get("/api/subtitle-settings").json()["locked"])

    def test_subtitle_retry_locks_settings(self):
        self.configure()
        session = self.sessions.create("retry fixture")
        session.update(source_type="import", status="completed", duration_ms=2000,
                       segments=[{"id": "one", "start_ms": 0, "end_ms": 2000,
                                  "ja": "テスト字幕", "zh": "", "translation_status": "failed"}])
        self.sessions.export(session)
        self.block_translation = True
        results = []
        thread = threading.Thread(target=lambda: results.append(
            self.client.post(f"/api/sessions/{session['id']}/retry-translations")))
        thread.start()
        try:
            self.assertTrue(self.translation_entered.wait(3))
            self.assert_locked()
        finally:
            self.translation_release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status_code, 200, results[0].text)
        self.assertFalse(self.client.get("/api/subtitle-settings").json()["locked"])

    def test_new_import_snapshots_settings_and_splits_before_proofreading_and_translation(self):
        self.configure()
        response = self.save(enabled=True, max_chars=12, max_seconds=4)
        self.assertEqual(response.status_code, 200, response.text)
        result = self.upload()
        self.assertEqual(result["status"], "completed", result.get("import_error"))
        self.assertEqual(result["subtitle_split_settings"], {"enabled": True, "max_chars": 12, "max_seconds": 4.0})
        segments = result["segments"]
        self.assertGreater(len(segments), 1)
        self.assertEqual("".join(item["ja_raw"] for item in segments), self.long_japanese)
        for segment in segments:
            self.assertLessEqual(len(segment["ja_raw"]), 12)
            self.assertLessEqual(segment["end_ms"] - segment["start_ms"], 4000)
            self.assertEqual(segment["correction_status"], "done")
            self.assertEqual(segment["translation_status"], "done")
            self.assertEqual(segment["zh"], "中文：" + segment["ja"])
            correct = self.calls.index(("correct", segment["ja_raw"]))
            translated = self.calls.index(("translate", segment["ja"]))
            self.assertLess(correct, translated)
        self.assertEqual(len([call for call in self.calls if call[0] == "correct"]), len(segments))
        self.assertEqual(len([call for call in self.calls if call[0] == "translate"]), len(segments))
        names = {item["name"] for item in result["files"]}
        self.assertTrue({"source.wav", "audio.wav", "ja.srt", "zh-CN.srt", "bilingual.srt"} <= names)
        self.assertNotIn(self.speech_key, json.dumps(result))
        self.assertNotIn(self.text_key, json.dumps(result))

    def test_translation_retry_keeps_original_split_snapshot_after_global_setting_changes(self):
        self.configure()
        self.assertEqual(self.save(enabled=True, max_chars=12, max_seconds=4).status_code, 200)
        self.fail_translation = True
        failed = self.upload()
        self.assertEqual(failed["status"], "failed")
        previous = [(item["id"], item["start_ms"], item["end_ms"], item["ja_raw"]) for item in failed["segments"]]
        self.assertEqual(self.save(enabled=False, max_chars=80, max_seconds=15).status_code, 200)
        self.fail_translation = False
        result = self.client.post(f"/api/imports/{failed['id']}/retry")
        self.assertEqual(result.status_code, 202, result.text)
        completed = self.wait_result(failed["id"])
        self.assertEqual(completed["status"], "completed", completed.get("import_error"))
        self.assertEqual(completed["subtitle_split_settings"], failed["subtitle_split_settings"])
        self.assertEqual([(item["id"], item["start_ms"], item["end_ms"], item["ja_raw"])
                          for item in completed["segments"]], previous)
        self.assertEqual(len([call for call in self.calls if call[0] == "asr"]), 1)

    def test_legacy_unfinished_import_without_snapshot_is_not_resegmented_on_resume(self):
        self.configure()
        self.assertEqual(self.save(enabled=True, max_chars=8, max_seconds=2).status_code, 200)
        session = self.sessions.create("legacy unfinished import")
        session.update(source_type="import", source_file="source.wav", original_filename="legacy.wav",
                       status="interrupted", import_stage="interrupted", import_upload_complete=True,
                       import_transcription_mode="cloud", transcription_mode="cloud_file",
                       transcription_model="fixture-asr", import_transcribed_ms=0)
        self.sessions.file_path(session["id"], "source.wav").write_bytes(wav_bytes(12))
        self.sessions.save(session)
        result = self.client.post(f"/api/imports/{session['id']}/retry")
        self.assertEqual(result.status_code, 202, result.text)
        completed = self.wait_result(session["id"])
        self.assertEqual(completed["status"], "completed", completed.get("import_error"))
        self.assertNotIn("subtitle_split_settings", completed)
        self.assertEqual(len(completed["segments"]), 1)
        self.assertEqual(completed["segments"][0]["ja_raw"], self.long_japanese)

    def test_precise_refinement_snapshots_split_settings_and_reuses_them_on_retry(self):
        self.configure()
        self.assertEqual(self.save(enabled=True, max_chars=12, max_seconds=4).status_code, 200)
        original = self.sessions.create("saved live fixture")
        original.update(source_type="live", status="completed", duration_ms=12000, audio_saved=True,
                        sample_rate=48000, channels=2, audio_parts=[{"name": "audio.wav", "start_ms": 0, "end_ms": 12000}],
                        segments=[{"id": "original-one", "start_ms": 0, "end_ms": 12000,
                                   "ja": "元のライブ字幕", "zh": "原直播字幕", "translation_status": "done"}])
        audio = self.sessions.file_path(original["id"], "audio.wav")
        original_audio = wav_bytes(12, rate=48000, channels=2)
        audio.write_bytes(original_audio)
        self.sessions.export(original)
        original_session = self.sessions.load(original["id"])
        self.fail_translation = True
        response = self.client.post(f"/api/sessions/{original['id']}/refine", json={"consent": True})
        self.assertEqual(response.status_code, 202, response.text)
        failed = self.wait_result(response.json()["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["refinement_source_id"], original["id"])
        self.assertEqual(failed["subtitle_split_settings"], {"enabled": True, "max_chars": 12, "max_seconds": 4.0})
        previous = [(item["id"], item["start_ms"], item["end_ms"], item["ja_raw"])
                    for item in failed["segments"]]
        self.assertGreater(len(previous), 1)
        self.assertEqual("".join(item[3] for item in previous), self.long_japanese)
        self.assertTrue(all(len(item[3]) <= 12 and item[2] - item[1] <= 4000 for item in previous))
        self.assertEqual(self.save(enabled=False, max_chars=80, max_seconds=15).status_code, 200)
        self.fail_translation = False
        response = self.client.post(f"/api/imports/{failed['id']}/retry")
        self.assertEqual(response.status_code, 202, response.text)
        completed = self.wait_result(failed["id"])
        self.assertEqual(completed["status"], "completed", completed.get("import_error"))
        self.assertEqual(completed["subtitle_split_settings"], failed["subtitle_split_settings"])
        self.assertEqual(completed["refinement_snapshot"], failed["refinement_snapshot"])
        self.assertEqual([(item["id"], item["start_ms"], item["end_ms"], item["ja_raw"])
                          for item in completed["segments"]], previous)
        self.assertEqual(len([call for call in self.calls if call[0] == "asr"]), 1)
        self.assertEqual(len([call for call in self.calls if call[0] == "correct"]), len(previous))
        self.assertEqual(self.sessions.load(original["id"]), original_session)
        self.assertEqual(audio.read_bytes(), original_audio)


if __name__ == "__main__":
    unittest.main()
