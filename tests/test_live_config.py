"""Live references are independent from file pipelines and never leak secrets."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yakiniku.live_config import (LiveSettingsInput, live_snapshot, public_live_settings,
                                  resolve_live_settings, save_live_settings)
from yakiniku.provider import ProviderError
from yakiniku.service_config import ConnectionInput, ServiceConfigInput, save_configuration
from yakiniku.storage import SettingsStore
from yakiniku.text_pipeline import text_options


class LiveConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SettingsStore(Path(self.tmp.name))
        self.settings = {
            "service_profiles_enabled": True,
            "service_profiles": [
                {"id": "speech", "name": "MiniMax", "scope": "asr", "base_url": "https://speech.example/v1", "api_key": "speech-private", "speech_api": "minimax", "enabled": True},
                {"id": "text", "name": "Text", "scope": "text", "base_url": "https://text.example/v1", "api_key": "text-private", "text_api": "responses", "enabled": True},
                {"id": "live", "name": "Live", "scope": "text", "base_url": "https://live.example/v1", "api_key": "live-private", "text_api": "chat_completions", "enabled": True},
            ],
            "task_routes": {"file_asr": {"provider_id": "speech", "model": "asr-1.0", "response_format": "json"}, "correction": {"provider_id": "text", "model": "file-correct"}, "translation": {"provider_id": "text", "model": "file-translate"}},
            "glossary": "old file glossary", "transcription_model": "tiny",
        }
        self.store.save(self.settings)

    def live_body(self, **updates):
        value = {"backend": "mlx", "model": "mlx-whisper-small", "translation_route": {"provider_id": "live", "model": "live-translate"}, "glossary": "live glossary", "segment_seconds": 3, "silence_ms": 600}
        value.update(updates)
        return LiveSettingsInput(**value)

    def test_defaults_preview_translation_without_writing_or_resetting_legacy(self):
        before = self.store.path.read_bytes()
        result = public_live_settings(self.store)
        self.assertEqual(result["model"], "mlx-whisper-small")
        self.assertEqual(result["translation_route"], {"provider_id": "text", "model": "file-translate"})
        self.assertTrue(result["translation_ready"])
        self.assertFalse(result["configured"])
        self.assertNotIn("speech", [p["id"] for p in result["providers"]])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store._stored()["transcription_model"], "tiny")

    def test_save_changes_only_live_settings_and_roundtrips_model(self):
        before = self.store._stored()
        result = save_live_settings(self.store, self.live_body())
        after = self.store._stored()
        for key, value in before.items():
            if key != "live_settings":
                self.assertEqual(after[key], value, key)
        self.assertEqual(public_live_settings(self.store)["model"], "mlx-whisper-small")
        self.assertTrue(result["configured"])
        self.assertTrue(result["configuration_ready"])

    def test_live_options_bypass_file_translation_route_and_need_no_correction(self):
        save_live_settings(self.store, self.live_body())
        current = self.store._stored()
        current["task_routes"]["correction"] = {"provider_id": "", "model": ""}
        current["task_routes"]["file_asr"] = {"provider_id": "", "model": ""}
        self.store.save(current)
        settings = resolve_live_settings(self.store)
        opts = text_options(settings, "translation")
        self.assertEqual(opts["base_url"], "https://live.example/v1")
        self.assertEqual(opts["api_key"], "live-private")
        self.assertEqual(opts["model"], "live-translate")
        self.assertEqual(opts["api_format"], "chat_completions")
        self.assertEqual(opts["glossary"], "live glossary")
        self.assertEqual(self.store.load()["translation_model"], "file-translate")

    def test_public_and_snapshot_are_allowlisted_and_secret_free(self):
        save_live_settings(self.store, self.live_body())
        settings = resolve_live_settings(self.store)
        settings["unexpected_secret"] = "never-copy"
        snapshot = live_snapshot(settings, {"path": "/models/small", "version": "fixed-revision", "api_key": "never-copy"})
        for value in (public_live_settings(self.store), snapshot):
            encoded = json.dumps(value)
            for secret in ("speech-private", "text-private", "live-private", "never-copy"):
                self.assertNotIn(secret, encoded)
        self.assertEqual(snapshot["model_source"]["version"], "fixed-revision")
        self.assertEqual(snapshot["translation_route"]["provider_id"], "live")

    def test_snapshot_keeps_old_route_model_and_parameters_after_configuration_changes(self):
        save_live_settings(self.store, self.live_body())
        snap = live_snapshot(resolve_live_settings(self.store))
        save_live_settings(self.store, self.live_body(backend="tiny", model="tiny", translation_route={"provider_id": "text", "model": "new-model"}, glossary="new glossary", segment_seconds=5))
        retry = resolve_live_settings(self.store, snapshot=snap)
        self.assertEqual(retry["translation_model"], "live-translate")
        self.assertEqual(retry["translation_base_url"], "https://live.example/v1")
        self.assertEqual(retry["live_asr_model"], "mlx-whisper-small")
        self.assertEqual(retry["segment_seconds"], 3)
        self.assertEqual(retry["glossary"], "live glossary")

    def test_deleted_or_repurposed_snapshot_provider_never_falls_back(self):
        save_live_settings(self.store, self.live_body())
        snap = live_snapshot(resolve_live_settings(self.store))
        for mutation in ("delete", "address", "protocol", "disabled"):
            with self.subTest(mutation=mutation):
                raw = self.store._stored()
                profiles = copy.deepcopy(self.settings["service_profiles"])
                if mutation == "delete": profiles = [p for p in profiles if p["id"] != "live"]
                elif mutation == "address": profiles[-1]["base_url"] = "https://other.example/v1"
                elif mutation == "protocol": profiles[-1]["text_api"] = "anthropic"
                else: profiles[-1]["enabled"] = False
                self.store.save({"service_profiles": profiles})
                with self.assertRaises(ProviderError): resolve_live_settings(self.store, snapshot=snap)
                self.store.save({"service_profiles": self.settings["service_profiles"]})

    def test_missing_live_translation_can_be_saved_but_cannot_start(self):
        result = save_live_settings(self.store, self.live_body(translation_route={"provider_id": "", "model": ""}))
        self.assertFalse(result["translation_ready"])
        with self.assertRaises(ProviderError): resolve_live_settings(self.store)

    def test_wrong_backend_model_and_asr_only_connection_rejected_without_write(self):
        before = self.store.path.read_bytes()
        for body in (self.live_body(backend="tiny"), self.live_body(translation_route={"provider_id": "speech", "model": "bad"}), self.live_body(translation_route={"provider_id": "live", "model": "bad\nmodel"})):
            with self.assertRaises(ValueError): save_live_settings(self.store, body)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_existing_environment_key_resolves_without_being_persisted(self):
        self.store.save({"service_profiles_enabled": False, "api_base_url": "https://legacy.example/v1", "translation_api_key": "", "api_key": "", "live_settings": {}})
        with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-private"}):
            public = public_live_settings(self.store)
            route = public["translation_route"]
            save_live_settings(self.store, self.live_body(translation_route=route))
            self.assertEqual(resolve_live_settings(self.store)["translation_api_key"], "environment-private")
        self.assertNotIn("environment-private", self.store.path.read_text())

    def test_file_config_cannot_remove_live_referenced_provider(self):
        save_live_settings(self.store, self.live_body())
        before = self.store.path.read_bytes()
        body = ServiceConfigInput(providers=[ConnectionInput(**p) for p in self.settings["service_profiles"] if p["id"] != "live"], routes=self.settings["task_routes"])
        with self.assertRaisesRegex(ValueError, "直播设置"):
            save_configuration(self.store, body)
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__": unittest.main()
