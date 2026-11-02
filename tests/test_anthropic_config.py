"""Anthropic connections use explicit protocol/auth and isolated fake services."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from yakiniku import config
from yakiniku.server import create_app
from yakiniku.storage import SettingsStore
from yakiniku.text_pipeline import text_options, validate_text_settings


class AnthropicConfigurationTests(unittest.TestCase):
    speech_key = "fixture-native-minimax-speech-secret"
    text_key = "fixture-anthropic-text-secret"
    draft_key = "fixture-anthropic-draft-secret"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.store = SettingsStore(self.root)
        self.requests = []
        self.handler = lambda request: httpx.Response(200, json={"data": []})

        def transport(request):
            self.requests.append(request)
            return self.handler(request)

        def factory(**options):
            return httpx.AsyncClient(transport=httpx.MockTransport(transport), **options)

        self.client = TestClient(create_app(self.root, probe_client_factory=factory),
                                 base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def assert_secret_free(self, value):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for secret in (self.speech_key, self.text_key, self.draft_key):
            self.assertNotIn(secret, rendered)
        if isinstance(value, dict):
            for name, child in value.items():
                self.assertNotIn(name, ("api_key", "correction_api_key", "translation_api_key",
                                        "import_transcription_api_key", "transcription_api_key"))
                self.assert_secret_free(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_secret_free(child)

    def body(self, auth="bearer"):
        return {"providers": [
            {"id": "speech", "name": "MiniMax 原生识别", "vendor": "minimax",
             "base_url": "https://speech.example/v1", "api_key": self.speech_key,
             "text_api": "chat_completions", "speech_api": "minimax", "enabled": True},
            {"id": "text", "name": "Anthropic 文字服务", "vendor": "minimax_anthropic",
             "base_url": "https://text.example/anthropic/", "api_key": self.text_key,
             "text_api": "anthropic", "anthropic_auth": auth, "speech_api": "none", "enabled": True},
        ], "routes": {
            "file_asr": {"provider_id": "speech", "model": "asr-1.0", "response_format": "verbose_json"},
            "correction": {"provider_id": "text", "model": "user-chosen-proofreading-model", "response_format": "verbose_json"},
            "translation": {"provider_id": "text", "model": "user-chosen-translation-model", "response_format": "verbose_json"},
        }}

    def save(self, body=None):
        response = self.client.post("/api/service-config", json=body or self.body())
        self.assert_secret_free(response.text)
        return response

    def save_initial(self, auth="bearer"):
        response = self.save(self.body(auth))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def discover(self, body):
        response = self.client.post("/api/service-config/models", json=body)
        self.assert_secret_free(response.text)
        return response

    def assert_anthropic_request(self, request, url, key, auth):
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), url)
        self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
        if auth == "bearer":
            self.assertEqual(request.headers["Authorization"], "Bearer " + key)
            self.assertNotIn("x-api-key", request.headers)
        else:
            self.assertEqual(request.headers["x-api-key"], key)
            self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.content, b"")

    def test_anthropic_endpoint_adds_one_version_and_preserves_proxy_prefixes(self):
        cases = (
            ("https://api.minimax.io/anthropic", "messages", "https://api.minimax.io/anthropic/v1/messages"),
            ("https://api.anthropic.com/", "models", "https://api.anthropic.com/v1/models"),
            ("https://proxy.example/team/v1/", "/messages", "https://proxy.example/team/v1/messages"),
            ("https://proxy.example/team", "models", "https://proxy.example/team/v1/models"),
        )
        for base, path, expected in cases:
            with self.subTest(base=base, path=path):
                self.assertEqual(config.anthropic_endpoint_url(base, path), expected)
        self.assertEqual(config.anthropic_endpoint_url("https://api.minimax.io/anthropic"),
                         "https://api.minimax.io/anthropic/v1/messages")

    def test_openai_endpoint_does_not_silently_add_version_path(self):
        self.assertEqual(config.endpoint_url("https://proxy.example/team", "models"),
                         "https://proxy.example/team/models")
        self.assertEqual(config.endpoint_url("https://proxy.example/team/v1/", "chat/completions"),
                         "https://proxy.example/team/v1/chat/completions")

    def test_protocol_and_both_auth_modes_roundtrip_without_changing_speech_or_live(self):
        self.store.save({"transcription_mode": "local", "transcription_model": "tiny"})
        for auth in ("api_key", "bearer"):
            with self.subTest(auth=auth):
                public = self.save_initial(auth)
                self.assert_secret_free(public)
                self.assertEqual(public["live"], {"mode": "local", "model": "tiny"})
                profile = next(p for p in public["providers"] if p["id"] == "text")
                self.assertEqual(profile["text_api"], "anthropic")
                self.assertEqual(profile["anthropic_auth"], auth)
                self.assertTrue(profile["key_configured"])
                self.assertEqual(public["ready"], {"file_asr": True, "correction": True, "translation": True})
                reloaded = SettingsStore(self.root).load()
                self.assertEqual(reloaded["import_transcription_api"], "minimax")
                self.assertEqual(reloaded["import_transcription_model"], "asr-1.0")
                self.assertEqual(config.effective_provider(reloaded, "import_transcription"),
                                 {"base_url": "https://speech.example/v1", "api_key": self.speech_key})
                for role in ("correction", "translation"):
                    self.assertEqual(reloaded[f"{role}_api"], "anthropic")
                    self.assertEqual(reloaded[f"{role}_anthropic_auth"], auth)
                    options = text_options(reloaded, role)
                    self.assertEqual(options["api_format"], "anthropic")
                    self.assertEqual(options["anthropic_auth"], auth)
                    self.assertEqual(options["api_key"], self.text_key)
                    self.assertEqual(options["model"], self.body()["routes"][role]["model"])
                validate_text_settings(reloaded)
        self.assertEqual(self.requests, [])

    def test_auth_default_is_native_api_key_and_omitted_for_openai_adapter_options(self):
        body = self.body()
        body["providers"][1].pop("anthropic_auth")
        self.assertEqual(self.save(body).status_code, 200)
        settings = self.store.load()
        self.assertEqual(text_options(settings, "translation")["anthropic_auth"], "api_key")
        for api in ("responses", "chat_completions"):
            with self.subTest(api=api):
                body["providers"][1]["text_api"] = api
                self.assertEqual(self.save(body).status_code, 200)
                settings = self.store.load()
                for role in ("correction", "translation"):
                    options = text_options(settings, role)
                    self.assertEqual(options["api_format"], api)
                    self.assertNotIn("anthropic_auth", options)
                validate_text_settings(settings)

    def test_correction_and_translation_keep_their_own_anthropic_auth_and_keys(self):
        body = self.body("bearer")
        body["providers"].append({"id": "proofreading", "name": "另一校对服务", "vendor": "anthropic",
                                  "base_url": "https://proofreading.example", "api_key": self.draft_key,
                                  "text_api": "anthropic", "anthropic_auth": "api_key",
                                  "speech_api": "none", "enabled": True})
        body["routes"]["correction"]["provider_id"] = "proofreading"
        response = self.save(body)
        self.assertEqual(response.status_code, 200, response.text)
        settings = self.store.load()
        correction = text_options(settings, "correction")
        translation = text_options(settings, "translation")
        self.assertEqual(correction["base_url"], "https://proofreading.example")
        self.assertEqual(correction["api_key"], self.draft_key)
        self.assertEqual(correction["anthropic_auth"], "api_key")
        self.assertEqual(translation["base_url"], "https://text.example/anthropic")
        self.assertEqual(translation["api_key"], self.text_key)
        self.assertEqual(translation["anthropic_auth"], "bearer")
        validate_text_settings(settings)

    def test_public_configuration_endpoints_never_expose_nested_anthropic_keys(self):
        self.save_initial()
        for endpoint in ("/api/settings", "/api/service-config", "/api/import-settings"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_secret_free(response.json())

    def test_legacy_migration_keeps_correction_and_translation_protocols_separate(self):
        self.store.save({"api_base_url": "https://legacy.example", "api_key": self.text_key,
                         "translation_api": "responses", "translation_model": "legacy-gpt-model",
                         "correction_api": "anthropic", "correction_model": "legacy-proofreading-model",
                         "correction_anthropic_auth": "bearer"})
        before = self.store.path.read_bytes()
        response = self.client.get("/api/service-config")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assert_secret_free(body)
        profiles = {p["id"]: p for p in body["providers"]}
        correction = profiles[body["routes"]["correction"]["provider_id"]]
        translation = profiles[body["routes"]["translation"]["provider_id"]]
        self.assertEqual(correction["text_api"], "anthropic")
        self.assertEqual(correction["anthropic_auth"], "bearer")
        self.assertEqual(translation["text_api"], "responses")
        self.assertNotEqual(correction["id"], translation["id"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.requests, [])

    def test_legacy_migration_does_not_merge_distinct_anthropic_auth_modes(self):
        self.store.save({"api_base_url": "https://legacy.example", "api_key": self.text_key,
                         "translation_api": "anthropic", "translation_model": "legacy-text-model",
                         "translation_anthropic_auth": "bearer", "correction_api": "anthropic",
                         "correction_anthropic_auth": "api_key"})
        before = self.store.path.read_bytes()
        response = self.client.get("/api/service-config")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assert_secret_free(body)
        profiles = {p["id"]: p for p in body["providers"]}
        correction = profiles[body["routes"]["correction"]["provider_id"]]
        translation = profiles[body["routes"]["translation"]["provider_id"]]
        self.assertEqual(correction["anthropic_auth"], "api_key")
        self.assertEqual(translation["anthropic_auth"], "bearer")
        self.assertNotEqual(correction["id"], translation["id"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_saved_profile_discovery_uses_its_protocol_and_auth_when_draft_omits_them(self):
        for auth in ("api_key", "bearer"):
            with self.subTest(auth=auth):
                self.save_initial(auth)
                before = self.store.path.read_bytes()
                response = self.discover({"provider_id": "text", "base_url": "https://text.example/anthropic/"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_anthropic_request(self.requests[-1],
                                             "https://text.example/anthropic/v1/models", self.text_key, auth)
                self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(len(self.requests), 2)

    def test_unsaved_draft_discovery_overrides_saved_protocol_auth_and_does_not_persist(self):
        body = self.body()
        body["providers"][1]["text_api"] = "chat_completions"
        self.assertEqual(self.save(body).status_code, 200)
        before = self.store.path.read_bytes()
        self.handler = lambda request: httpx.Response(200, json={"data": [
            {"id": "chosen-audio-model"}, {"id": "chosen-text-model"},
            {"id": "chosen-text-model"}, {"id": "other-capability-model"},
        ]})
        for auth in ("api_key", "bearer"):
            with self.subTest(auth=auth):
                response = self.discover({"provider_id": "text", "base_url": "https://draft.example/team/v1/",
                                          "api_key": self.draft_key, "text_api": "anthropic",
                                          "anthropic_auth": auth})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"models": ["chosen-audio-model", "chosen-text-model",
                                                              "other-capability-model"]})
                self.assert_anthropic_request(self.requests[-1], "https://draft.example/team/v1/models",
                                             self.draft_key, auth)
                self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(len(self.requests), 2)

    def test_first_unsaved_anthropic_discovery_does_not_create_settings(self):
        response = self.discover({"provider_id": "new-draft", "base_url": "https://api.anthropic.com",
                                  "api_key": self.draft_key, "text_api": "anthropic"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.requests), 1)
        self.assert_anthropic_request(self.requests[0], "https://api.anthropic.com/v1/models",
                                     self.draft_key, "api_key")
        self.assertFalse(self.store.path.exists())

    def test_explicit_openai_draft_protocol_never_sends_saved_anthropic_headers(self):
        self.save_initial("api_key")
        before = self.store.path.read_bytes()
        response = self.discover({"provider_id": "text", "base_url": "https://text.example/openai-proxy",
                                  "text_api": "chat_completions"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://text.example/openai-proxy/models")
        self.assertEqual(request.headers["Authorization"], "Bearer " + self.text_key)
        self.assertNotIn("x-api-key", request.headers)
        self.assertNotIn("anthropic-version", request.headers)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_cleared_draft_key_never_reuses_saved_anthropic_credentials(self):
        self.save_initial()
        before = self.store.path.read_bytes()
        response = self.discover({"provider_id": "text", "base_url": "https://text.example/anthropic",
                                  "clear_api_key": True, "text_api": "anthropic"})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_cross_origin_anthropic_discovery_requires_explicit_key(self):
        self.save_initial()
        before = self.store.path.read_bytes()
        response = self.discover({"provider_id": "text", "base_url": "https://other.example/anthropic",
                                  "text_api": "anthropic", "anthropic_auth": "api_key"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_models_unavailable_never_falls_back_or_mutates_route(self):
        self.save_initial()
        before = self.store.path.read_bytes()
        for status in (404, 405):
            with self.subTest(status=status):
                self.handler = lambda request: httpx.Response(status, json={"error": self.text_key})
                response = self.discover({"provider_id": "text", "base_url": "https://text.example/anthropic"})
                self.assertEqual(response.status_code, 502, response.text)
                self.assertTrue(response.json()["detail"])
                self.assert_anthropic_request(self.requests[-1], "https://text.example/anthropic/v1/models",
                                             self.text_key, "bearer")
                self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.client.get("/api/service-config").json()["routes"], self.body()["routes"])

    def test_invalid_protocol_and_auth_never_echo_credentials_or_change_settings(self):
        self.save_initial()
        before = self.store.path.read_bytes()
        for field in ("text_api", "anthropic_auth"):
            with self.subTest(field=field):
                body = self.body()
                body["providers"][1][field] = self.draft_key
                response = self.save(body)
                self.assertEqual(response.status_code, 422, response.text)
                response = self.discover({"provider_id": "text", "base_url": "https://text.example/anthropic",
                                          "api_key": self.draft_key, field: self.draft_key})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.requests, [])

    def test_presets_expose_both_anthropic_auth_choices_without_editing_saved_connections(self):
        self.save_initial()
        before = self.store.path.read_bytes()
        response = self.client.get("/api/service-config")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        presets = {p["id"]: p for p in body["presets"]}
        self.assertEqual(presets["minimax"]["speech_api"], "minimax")
        self.assertEqual(presets["minimax"]["text_api"], "chat_completions")
        for name, auth, base in (
            ("minimax_anthropic", "bearer", "https://api.minimax.io/anthropic"),
            ("anthropic", "api_key", "https://api.anthropic.com"),
        ):
            with self.subTest(preset=name):
                self.assertEqual(presets[name]["text_api"], "anthropic")
                self.assertEqual(presets[name]["anthropic_auth"], auth)
                self.assertEqual(presets[name]["speech_api"], "none")
                self.assertEqual(presets[name]["suggested_base_url"], base)
        self.assertEqual(body["routes"], self.body()["routes"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
