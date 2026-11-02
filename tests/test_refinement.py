"""Saved-live refinement uses isolated files and the explicit three-stage chain."""
import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from yakiniku.provider import ProviderError
from yakiniku.server import create_app
from yakiniku.storage import SessionStore, SettingsStore
from tests.test_import_audio import wav_bytes


class RefinementApiTests(unittest.TestCase):
    origin = "http://127.0.0.1:8765"
    speech_secret = "fixture-refinement-minimax-secret"
    text_secret = "fixture-refinement-gpt-secret"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = SettingsStore(self.root)
        self.store = SessionStore(self.root / "recordings")
        self.calls = []
        self.instances = []
        self.fail_translation = False
        self.block_asr = False
        self.asr_entered = threading.Event()
        self.asr_release = threading.Event()
        self.configure()
        owner = self

        class Speech:
            def __init__(self, **options):
                self.options = options
                owner.instances.append(("asr", options))

            async def transcribe(self, audio, *, start_ms, end_ms):
                owner.calls.append(("asr", start_ms, end_ms, len(audio)))
                owner.asr_entered.set()
                while owner.block_asr and not owner.asr_release.is_set():
                    await asyncio.sleep(.005)
                return [{"ja": "fixture raw Japanese", "start_ms": start_ms,
                         "end_ms": end_ms, "timestamp_source": "api_segments",
                         "alignment": "provider"}]

            async def close(self):
                pass

        class Text:
            def __init__(self, **options):
                self.model = options["model"]
                self.api_format = options["api_format"]
                owner.instances.append(("text", options))

            async def correct(self, text, context=None):
                owner.calls.append(("correct", text, self.model))
                return "fixture corrected Japanese"

            async def translate(self, text, context=None):
                owner.calls.append(("translate", text, self.model))
                if owner.fail_translation:
                    raise ProviderError("fixture translation unavailable")
                return "fixture Chinese translation"

            async def close(self):
                pass

        self.text_factory = Text
        self.speech_factory = Speech
        self.app = create_app(self.root, translator_factory=Text,
                              import_file_transcriber_factory=Speech)
        self.client = TestClient(self.app, base_url=self.origin)
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))

    def restart_service(self):
        self.client.__exit__(None, None, None)
        self.app = create_app(self.root, translator_factory=self.text_factory,
                              import_file_transcriber_factory=self.speech_factory)
        self.client = TestClient(self.app, base_url=self.origin)
        self.client.__enter__()

    def configure(self):
        self.settings.save({
            "service_profiles_enabled": True,
            "service_profiles": [
                {"id": "speech", "name": "MiniMax fixture", "vendor": "minimax", "scope": "asr",
                 "base_url": "https://speech.example/v1", "api_key": self.speech_secret,
                 "speech_api": "minimax", "text_api": "chat_completions", "enabled": True},
                {"id": "text", "name": "GPT fixture", "vendor": "custom", "scope": "text",
                 "base_url": "https://text.example/v1", "api_key": self.text_secret,
                 "speech_api": "none", "text_api": "chat_completions", "enabled": True},
            ],
            "task_routes": {
                "file_asr": {"provider_id": "speech", "model": "asr-1.0", "response_format": "verbose_json"},
                "correction": {"provider_id": "text", "model": "gpt-fixture-correct"},
                "translation": {"provider_id": "text", "model": "proxy/gpt-fixture-translate"},
            },
            "glossary": "fixture glossary",
        })

    def live_recording(self, rates=(48000,), status="completed"):
        project = self.store.create("fixture live recording")
        project.update(source_type="live", status=status, audio_saved=True,
                       duration_ms=len(rates) * 1000, sample_rate=rates[0], channels=2,
                       segments=[{"id": "original", "start_ms": 0, "end_ms": 500,
                                  "ja": "original Japanese", "zh": "original Chinese",
                                  "asr_status": "done", "translation_status": "done"}])
        project["audio_parts"] = []
        for index, rate in enumerate(rates):
            name = "audio.wav" if index == 0 else f"audio-{index + 1:03d}.wav"
            self.store.file_path(project["id"], name).write_bytes(wav_bytes(rate=rate, channels=2))
            project["audio_parts"].append({"name": name, "start_ms": index * 1000, "end_ms": (index + 1) * 1000})
        self.store.export(project)
        return project

    def post_refine(self, project, **options):
        return self.client.post(f"/api/sessions/{project['id']}/refine", json={"consent": True}, **options)

    def wait_result(self, identifier):
        for _ in range(500):
            response = self.client.get(f"/api/imports/{identifier}")
            self.assertEqual(response.status_code, 200, response.text)
            project = response.json()
            if project.get("import_stage") in ("completed", "failed", "interrupted"):
                return project
            time.sleep(.01)
        self.fail("Refinement did not finish")

    def original_hashes(self, project):
        return {item["name"]: hashlib.sha256(self.store.file_path(project["id"], item["name"]).read_bytes()).hexdigest()
                for item in self.store.files(project)}

    def assert_no_secrets(self, value):
        serialized = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        for secret in (self.speech_secret, self.text_secret):
            self.assertNotIn(secret, serialized)

    def test_refines_all_wav_parts_and_keeps_original_project_byte_for_byte(self):
        original = self.live_recording((44100, 24000))
        original_hashes = self.original_hashes(original)
        readiness = self.client.get(f"/api/sessions/{original['id']}/refinement")
        self.assertEqual(readiness.status_code, 200, readiness.text)
        self.assertTrue(readiness.json()["eligible"])
        self.assertTrue(readiness.json()["ready"])
        self.assertEqual([item["role"] for item in readiness.json()["chain"]],
                         ["file_asr", "correction", "translation"])
        response = self.post_refine(original)
        self.assertEqual(response.status_code, 202, response.text)
        child = self.wait_result(response.json()["id"])
        self.assertNotEqual(child["id"], original["id"])
        self.assertEqual(child["source_type"], "import")
        self.assertEqual(child["refinement_source_id"], original["id"])
        self.assertEqual(len(child["refinement_source_parts"]), 2)
        self.assertTrue(child["refinement_snapshot"])
        self.assertEqual(child["import_stage"], "completed", child)
        self.assertEqual(child["duration_ms"], 2000)
        self.assertEqual([call[0] for call in self.calls], ["asr", "correct", "translate"])
        self.assertEqual(self.calls[0][1:], (0, 2000, 32000))
        self.assertEqual(self.calls[2][1], "fixture corrected Japanese")
        self.assertEqual(child["segments"][0]["ja_raw"], "fixture raw Japanese")
        self.assertEqual(child["segments"][0]["ja"], "fixture corrected Japanese")
        self.assertEqual(child["segments"][0]["zh"], "fixture Chinese translation")
        for part in child["audio_parts"]:
            with wave.open(str(self.store.file_path(child["id"], part["name"]))) as reader:
                self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()), (48000, 2, 2))
        files = {item["name"]: item for item in child["files"]}
        for name in ("ja.srt", "zh-CN.srt", "bilingual.srt"):
            self.assertGreater(files[name]["size"], 0)
        self.assertEqual(self.original_hashes(original), original_hashes)
        speech_options = next(options for kind, options in self.instances if kind == "asr")
        self.assertEqual(speech_options["api_format"], "minimax")
        self.assertEqual(speech_options["model"], "asr-1.0")
        self.assert_no_secrets(readiness.text)
        self.assert_no_secrets(child)

    def test_requires_consent_before_creating_or_sending_any_audio(self):
        original = self.live_recording()
        for body in ({}, {"consent": False}):
            response = self.client.post(f"/api/sessions/{original['id']}/refine", json=body)
            self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.store.list()), 1)

    def test_requires_minimax_asr_and_configured_gpt_for_both_text_stages(self):
        original = self.live_recording()
        original_settings = self.settings._stored()
        for update in ("speech_protocol", "correction_model", "translation_model", "speech_key"):
            with self.subTest(update=update):
                changed = json.loads(json.dumps(original_settings))
                if update == "speech_protocol":
                    changed["service_profiles"][0]["speech_api"] = "openai"
                elif update == "speech_key":
                    changed["service_profiles"][0]["api_key"] = ""
                else:
                    changed["task_routes"][update.removesuffix("_model")]["model"] = "other-text-model"
                self.settings.save(changed)
                readiness = self.client.get(f"/api/sessions/{original['id']}/refinement").json()
                self.assertFalse(readiness["ready"])
                self.assertTrue(readiness["message"])
                response = self.post_refine(original)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.calls, [])
                self.assertEqual(len(self.store.list()), 1)

    def test_duplicate_request_reuses_active_and_completed_child(self):
        original = self.live_recording()
        self.block_asr = True
        response = self.post_refine(original)
        self.assertEqual(response.status_code, 202, response.text)
        child_id = response.json()["id"]
        try:
            self.assertTrue(self.asr_entered.wait(2))
            repeated = self.post_refine(original)
            self.assertEqual(repeated.status_code, 202, repeated.text)
            self.assertEqual(repeated.json()["id"], child_id)
            state = self.client.get(f"/api/sessions/{original['id']}/refinement").json()
            self.assertTrue(state["active"])
            self.assertEqual(state["project_id"], child_id)
        finally:
            self.asr_release.set()
        self.assertEqual(self.wait_result(child_id)["import_stage"], "completed")
        repeated = self.post_refine(original)
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json()["id"], child_id)
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual(sum(call[0] == "asr" for call in self.calls), 1)

    def test_failed_translation_resumes_without_repeating_asr_or_correction(self):
        original = self.live_recording()
        self.fail_translation = True
        response = self.post_refine(original)
        self.assertEqual(response.status_code, 202, response.text)
        failed = self.wait_result(response.json()["id"])
        self.assertEqual(failed["import_stage"], "failed")
        self.assertEqual(failed["segments"][0]["correction_status"], "done")
        self.fail_translation = False
        retry = self.client.post(f"/api/imports/{failed['id']}/retry")
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(self.wait_result(failed["id"])["import_stage"], "completed")
        self.assertEqual([call[0] for call in self.calls], ["asr", "correct", "translate", "translate"])

    def test_legacy_retry_cannot_bypass_refinement_snapshot(self):
        original = self.live_recording()
        self.fail_translation = True
        child = self.wait_result(self.post_refine(original).json()["id"])
        previous = list(self.calls)
        response = self.client.post(f"/api/sessions/{child['id']}/retry-translations")
        self.assertEqual(response.status_code, 409)
        self.assertIn("继续精准校验", response.json()["detail"])
        self.assertEqual(self.calls, previous)

    def test_retry_pins_models_and_glossary_but_resolves_rotated_secrets(self):
        original = self.live_recording()
        self.fail_translation = True
        response = self.post_refine(original)
        self.assertEqual(response.status_code, 202, response.text)
        failed = self.wait_result(response.json()["id"])
        settings = self.settings._stored()
        settings["task_routes"]["translation"]["model"] = "gpt-new-model"
        settings["task_routes"]["correction"]["model"] = "gpt-new-corrector"
        settings["glossary"] = "new glossary"
        settings["service_profiles"][1]["api_key"] = "fixture-rotated-text-key"
        self.settings.save(settings)
        self.fail_translation = False
        retry = self.client.post(f"/api/imports/{failed['id']}/retry")
        self.assertEqual(retry.status_code, 202, retry.text)
        complete = self.wait_result(failed["id"])
        self.assertEqual(complete["import_stage"], "completed", complete)
        self.assertEqual(self.calls[-1][-1], "proxy/gpt-fixture-translate")
        last_options = [options for kind, options in self.instances if kind == "text"][-1]
        self.assertEqual(last_options["glossary"], "fixture glossary")
        self.assertEqual(last_options["api_key"], "fixture-rotated-text-key")
        self.assertNotIn("fixture-rotated-text-key", json.dumps(complete))

    def test_retry_rejects_changed_provider_destination_without_sending_more_audio(self):
        original = self.live_recording()
        self.fail_translation = True
        response = self.post_refine(original)
        self.assertEqual(response.status_code, 202, response.text)
        failed = self.wait_result(response.json()["id"])
        changed = self.settings._stored()
        changed["service_profiles"][1]["base_url"] = "https://text.example/another-gateway/v1"
        self.settings.save(changed)
        prior_calls = list(self.calls)
        retry = self.client.post(f"/api/imports/{failed['id']}/retry")
        self.assertEqual(retry.status_code, 400, retry.text)
        self.assertEqual(self.calls, prior_calls)
        self.assertEqual(self.store.load(failed["id"])["import_stage"], "failed")

    def test_refinement_occupies_existing_processing_locks(self):
        original = self.live_recording()
        another = self.live_recording()
        self.block_asr = True
        response = self.post_refine(original)
        self.assertEqual(response.status_code, 202, response.text)
        try:
            self.assertTrue(self.asr_entered.wait(2))
            self.assertTrue(self.client.get("/api/health").json()["import_active"])
            self.assertEqual(self.post_refine(another).status_code, 409)
            self.assertEqual(self.client.post("/api/sessions", json={}).status_code, 409)
            self.assertEqual(self.client.post("/api/settings", json={"glossary": "changed"}).status_code, 409)
            self.assertEqual(self.client.post("/api/imports", content=wav_bytes(), headers={
                "Content-Type": "application/octet-stream", "X-Filename": "fixture.wav"}).status_code, 409)
        finally:
            self.asr_release.set()
        self.assertEqual(self.wait_result(response.json()["id"])["import_stage"], "completed")

    def test_restart_keeps_child_id_and_interrupted_job_is_explicitly_resumable(self):
        original = self.live_recording()
        original_hashes = self.original_hashes(original)
        self.block_asr = True
        response = self.post_refine(original)
        self.assertEqual(response.status_code, 202, response.text)
        child_id = response.json()["id"]
        self.assertTrue(self.asr_entered.wait(2))
        self.restart_service()
        saved = self.store.load(child_id)
        self.assertEqual(saved["import_stage"], "interrupted")
        self.assertEqual(saved["status"], "interrupted")
        state = self.client.get(f"/api/sessions/{original['id']}/refinement").json()
        self.assertFalse(state["active"])
        self.assertEqual(state["project_id"], child_id)
        self.assertEqual(state["project_status"], "interrupted")
        repeated = self.post_refine(original)
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json()["id"], child_id)
        self.assertEqual(len(self.calls), 1, "discovering an interrupted child must not resume uploads")
        self.block_asr = False
        self.asr_release.set()
        resumed = self.client.post(f"/api/imports/{child_id}/retry")
        self.assertEqual(resumed.status_code, 202, resumed.text)
        self.assertEqual(self.wait_result(child_id)["import_stage"], "completed")
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual(self.original_hashes(original), original_hashes)

    def test_incomplete_recording_import_and_invalid_ids_cannot_refine(self):
        for identifier in ("invalid-id", "0" * 32):
            self.assertEqual(self.client.get(f"/api/sessions/{identifier}/refinement").status_code, 404)
            self.assertEqual(self.client.post(f"/api/sessions/{identifier}/refine", json={"consent": True}).status_code, 404)
        live = self.live_recording(status="recording")
        imported = self.live_recording()
        imported["source_type"] = "import"
        self.store.save(imported)
        for project in (live, imported):
            readiness = self.client.get(f"/api/sessions/{project['id']}/refinement")
            self.assertEqual(readiness.status_code, 200, readiness.text)
            self.assertFalse(readiness.json()["eligible"])
            self.assertIn(self.post_refine(project).status_code, (400, 409))
        self.assertEqual(self.calls, [])

    def test_missing_or_symlink_audio_is_rejected_without_upload(self):
        for link in (False, True):
            with self.subTest(link=link):
                original = self.live_recording()
                audio = self.store.file_path(original["id"], "audio.wav")
                audio.unlink()
                if link:
                    other = self.root / "external.wav"
                    other.write_bytes(wav_bytes())
                    audio.symlink_to(other)
                response = self.post_refine(original)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.calls, [])

    def test_truncated_audio_tail_is_rejected_even_when_header_still_parses(self):
        original = self.live_recording()
        audio = self.store.file_path(original["id"], "audio.wav")
        audio.write_bytes(audio.read_bytes()[:-4])
        response = self.post_refine(original)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
