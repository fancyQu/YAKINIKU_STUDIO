import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yakiniku.config import effective_provider, endpoint_url, normalize_base_url, websocket_url
from yakiniku.storage import SettingsStore


class BaseURLTests(unittest.TestCase):
    def test_gateway_prefix_and_version_paths_are_preserved(self):
        self.assertEqual(normalize_base_url("https://proxy.example:8443/team/openai/v1///"),
                         "https://proxy.example:8443/team/openai/v1")
        self.assertEqual(endpoint_url("https://proxy.example/custom/api/", "/chat/completions"),
                         "https://proxy.example/custom/api/chat/completions")
        self.assertEqual(endpoint_url("https://proxy.example", "audio/transcriptions"),
                         "https://proxy.example/audio/transcriptions")
        self.assertEqual(normalize_base_url("HTTPS://PROXY.EXAMPLE/V1/"), "https://proxy.example/V1")

    def test_websocket_uses_configured_server_and_prefix(self):
        self.assertEqual(websocket_url("https://gateway.example/prefix/v1/"),
                         "wss://gateway.example/prefix/v1/realtime?intent=transcription")
        self.assertEqual(websocket_url("http://localhost:8080/v1"),
                         "ws://localhost:8080/v1/realtime?intent=transcription")
        self.assertEqual(websocket_url("http://[::1]:8000/api"),
                         "ws://[::1]:8000/api/realtime?intent=transcription")

    def test_plain_http_is_allowed_only_for_loopback_services(self):
        for address in ("http://localhost:8000/v1", "http://127.0.0.1:11434/v1", "http://127.3.2.1/v1", "http://[::1]/v1"):
            self.assertEqual(normalize_base_url(address), address)
        for address in ("http://remote.example/v1", "http://192.168.1.2/v1", "http://[2001:db8::1]/v1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                normalize_base_url(address)

    def test_invalid_urls_never_reflect_credentials_in_error(self):
        invalid = (
            "", None, "file:///tmp/service", "wss://proxy.example/v1",
            "https://user:secret-token@proxy.example/v1", "https://secret-token@proxy.example/v1",
            "https://proxy.example/v1?api_key=secret-token", "https://proxy.example/v1#secret-token",
            "https://proxy.example/v1?", "https://proxy.example/v1#",
            "https://proxy.example:70000/v1", "https://proxy.example:0/v1", "https://proxy.example:/v1",
            "https://bad_host.example/v1", "https:///v1", "https://[broken]/v1", "https://[::1]extra/v1",
            "https://999.999.999.999/v1",
            "https://proxy.example/my path", "https://proxy.example/\nsecret-token", " https://proxy.example/v1",
            "https://proxy.example\\@other.example/v1", "https://proxy.example/-/\x7f",
        )
        for address in invalid:
            with self.subTest(address=address):
                with self.assertRaises(ValueError) as raised:
                    normalize_base_url(address)
                self.assertNotIn("secret-token", str(raised.exception))
                self.assertNotIn("proxy.example", str(raised.exception))

    def test_complete_endpoint_addresses_are_rejected(self):
        for path in ("chat/completions", "responses", "audio/transcriptions", "realtime"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                normalize_base_url("https://proxy.example/prefix/v1/" + path + "/")


class ProviderSettingsTests(unittest.TestCase):
    def test_shared_defaults_and_independent_overrides(self):
        settings = {"api_key": "shared-secret", "api_base_url": "https://shared.example/gateway/v1/"}
        self.assertEqual(effective_provider(settings, "transcription"), {
            "api_key": "shared-secret", "base_url": "https://shared.example/gateway/v1",
        })
        settings.update({
            "transcription_api_key": "speech-secret", "transcription_base_url": "https://speech.example/api",
            "translation_base_url": "https://translate.example/v1",
        })
        self.assertEqual(effective_provider(settings, "transcription"), {
            "api_key": "speech-secret", "base_url": "https://speech.example/api",
        })
        self.assertEqual(effective_provider(settings, "translation"), {
            "api_key": "shared-secret", "base_url": "https://translate.example/v1",
        })
        settings["translation_api_key"] = "translation-secret"
        settings["translation_base_url"] = ""
        self.assertEqual(effective_provider(settings, "translation"), {
            "api_key": "translation-secret", "base_url": "https://shared.example/gateway/v1",
        })
        with self.assertRaises(ValueError):
            effective_provider(settings, "other")

    def test_old_settings_gain_defaults_without_losing_their_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps({"api_key": "existing-secret", "glossary": "旧术语"}))
            store = SettingsStore(root)
            settings = store.load()
            self.assertEqual(settings["api_base_url"], "")
            self.assertEqual(settings["transcription_mode"], "local")
            self.assertEqual(settings["translation_api"], "responses")
            self.assertEqual(settings["segment_seconds"], 5)
            self.assertEqual(effective_provider(settings, "translation")["api_key"], "existing-secret")
            self.assertEqual(effective_provider(settings, "translation")["base_url"], "")
            self.assertEqual(settings["glossary"], "旧术语")
            self.assertTrue(store.public()["key_configured"])
            self.assertFalse(store.public()["configuration_ready"])

    def test_missing_base_never_selects_an_implicit_remote_service(self):
        for settings in ({}, {"api_key": "existing-secret"}, {"api_base_url": "", "transcription_base_url": ""}):
            with self.subTest(settings=settings):
                for role in ("transcription", "translation"):
                    self.assertEqual(effective_provider(settings, role)["base_url"], "")

    def test_saved_user_base_url_remains_explicitly_selected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = SettingsStore(Path(directory))
            selected = "https://chosen.example/my-gateway/v1"
            store.save({"api_base_url": selected, "api_key": "user-secret", "transcription_mode": "realtime", "transcription_model": "user-speech-model"})
            reloaded = SettingsStore(Path(directory))
            self.assertEqual(reloaded.load()["api_base_url"], selected)
            self.assertEqual(effective_provider(reloaded.load(), "transcription")["base_url"], selected)
            self.assertEqual(effective_provider(reloaded.load(), "translation")["base_url"], selected)
            self.assertTrue(reloaded.public()["configuration_ready"])

    def test_public_configuration_redacts_all_three_keys(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = SettingsStore(Path(directory))
            store.save({
                "api_key": "shared-secret", "transcription_api_key": "speech-secret",
                "translation_api_key": "translation-secret", "translation_api": "chat_completions",
                "transcription_base_url": "https://speech.example/api",
            })
            public = store.public()
            for name in ("api_key", "transcription_api_key", "translation_api_key"):
                self.assertNotIn(name, public)
            rendered = json.dumps(public)
            for secret in ("shared-secret", "speech-secret", "translation-secret"):
                self.assertNotIn(secret, rendered)
            self.assertTrue(public["key_configured"])
            self.assertTrue(public["shared_key_configured"])
            self.assertTrue(public["transcription_has_own_key"])
            self.assertTrue(public["translation_has_own_key"])
            self.assertEqual(public["translation_api"], "chat_completions")
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_readiness_accepts_two_independent_keys_without_shared_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = SettingsStore(Path(directory))
            store.save({"transcription_api_key": "speech-secret"})
            public = store.public()
            self.assertFalse(public["key_configured"])
            self.assertTrue(public["transcription_key_configured"])
            self.assertFalse(public["translation_key_configured"])
            store.save({"translation_api_key": "translation-secret"})
            self.assertTrue(store.public()["key_configured"])
            self.assertFalse(store.public()["shared_key_configured"])

    def test_environment_shared_key_is_fallback_and_never_persisted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENAI_API_KEY": "environment-secret"}):
            store = SettingsStore(Path(directory))
            store.save({"transcription_api_key": "speech-secret", "api_base_url": "https://proxy.example/v1"})
            self.assertEqual(effective_provider(store.load(), "transcription")["api_key"], "speech-secret")
            self.assertEqual(effective_provider(store.load(), "translation")["api_key"], "environment-secret")
            public = store.public()
            self.assertTrue(public["key_from_environment"])
            self.assertTrue(public["key_configured"])
            self.assertNotIn("environment-secret", store.path.read_text())


if __name__ == "__main__":
    unittest.main()
