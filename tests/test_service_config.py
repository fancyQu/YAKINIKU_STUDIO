"""Named API connections and three-stage routes use isolated fake services."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from yakiniku.config import effective_provider
from yakiniku.server import create_app
from yakiniku.storage import SettingsStore


class ServiceConfigurationTests(unittest.TestCase):
    shared_key = "fixture-legacy-shared-secret"
    speech_key = "fixture-minimax-speech-secret"
    text_key = "fixture-gpt-text-secret"
    environment_key = "fixture-environment-only-secret"
    draft_key = "fixture-unsaved-provider-secret"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = SettingsStore(self.root)
        self.requests = []
        self.probe_options = []
        self.handler = lambda request: httpx.Response(200, json={"data": []})

        def transport(request):
            self.requests.append(request)
            return self.handler(request)

        def factory(**options):
            self.probe_options.append(options)
            return httpx.AsyncClient(transport=httpx.MockTransport(transport), **options)

        self.client = TestClient(create_app(self.root, probe_client_factory=factory),
                                 base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def assert_secret_free(self, value):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for secret in (self.shared_key, self.speech_key, self.text_key,
                       self.environment_key, self.draft_key):
            self.assertNotIn(secret, rendered)
        if isinstance(value, dict):
            for name, child in value.items():
                self.assertNotIn(name, ("api_key", "correction_api_key", "translation_api_key",
                                        "import_transcription_api_key", "transcription_api_key"))
                self.assert_secret_free(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_secret_free(child)

    def legacy(self, **updates):
        self.store.save({"api_base_url": "https://legacy.example/v1", "api_key": self.shared_key,
                         "transcription_mode": "local", "transcription_model": "tiny",
                         "translation_model": "saved-gpt-model", "translation_api": "responses",
                         "glossary": "配信 = 直播", **updates})

    def body(self):
        return {"providers": [
            {"id": "speech-minimax", "name": "MiniMax 日语识别", "vendor": "minimax",
             "base_url": "https://speech.example/team/v1/", "api_key": self.speech_key,
             "text_api": "chat_completions", "speech_api": "minimax", "enabled": True},
            {"id": "gpt-text", "name": "GPT 校对与翻译", "vendor": "custom",
             "base_url": "https://text.example/proxy/", "api_key": self.text_key,
             "text_api": "responses", "speech_api": "none", "enabled": True},
        ], "routes": {
            "file_asr": {"provider_id": "speech-minimax", "model": "asr-1.0", "response_format": "verbose_json"},
            "correction": {"provider_id": "gpt-text", "model": "chosen-proofreading-model"},
            "translation": {"provider_id": "gpt-text", "model": "chosen-translation-model"},
        }}

    def save(self, body):
        response = self.client.post("/api/service-config", json=body)
        self.assert_secret_free(response.text)
        return response

    def discover(self, body):
        response = self.client.post("/api/service-config/models", json=body)
        self.assert_secret_free(response.text)
        return response

    def save_initial(self):
        result = self.save(self.body())
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def assert_request(self, url, key):
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), url)
        self.assertEqual(request.headers["Authorization"], "Bearer " + key)
        self.assertEqual(request.content, b"")

    def test_first_read_does_not_create_settings_or_guess_file_speech_model(self):
        response = self.client.get("/api/service-config")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assert_secret_free(result)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(result["live"], {"mode": "local", "model": "tiny"})
        self.assertEqual(result["routes"]["file_asr"]["model"], "")
        self.assertFalse(result["ready"]["file_asr"])

    def test_legacy_shared_translation_migrates_in_memory_to_same_correction_connection(self):
        self.legacy()
        previous = self.store.path.read_bytes()
        response = self.client.get("/api/service-config")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assert_secret_free(result)
        self.assertEqual(result["routes"]["correction"]["provider_id"],
                         result["routes"]["translation"]["provider_id"])
        self.assertEqual(result["routes"]["correction"]["model"], "saved-gpt-model")
        self.assertEqual(result["routes"]["translation"]["model"], "saved-gpt-model")
        self.assertTrue(result["ready"]["correction"])
        self.assertEqual(result["live"], {"mode": "local", "model": "tiny"})
        self.assertEqual(self.store.path.read_bytes(), previous)
        self.assertFalse(self.store.load()["service_profiles_enabled"])
        self.assertEqual(self.requests, [])

    def test_save_routes_minimax_asr_and_two_gpt_stages_without_changing_live(self):
        self.legacy()
        response = self.save(self.body())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["ready"], {"file_asr": True, "correction": True, "translation": True})
        settings = self.store.load()
        self.assertEqual((settings["transcription_mode"], settings["transcription_model"]), ("local", "tiny"))
        self.assertEqual(settings["glossary"], "配信 = 直播")
        self.assertEqual(effective_provider(settings, "import_transcription"),
                         {"base_url": "https://speech.example/team/v1", "api_key": self.speech_key})
        for role in ("correction", "translation"):
            self.assertEqual(effective_provider(settings, role),
                             {"base_url": "https://text.example/proxy", "api_key": self.text_key})
        self.assertEqual(settings["import_transcription_api"], "minimax")
        self.assertEqual(settings["import_transcription_model"], "asr-1.0")
        self.assertEqual(settings["correction_model"], "chosen-proofreading-model")
        self.assertEqual(settings["translation_model"], "chosen-translation-model")
        self.assertEqual((settings["correction_api"], settings["translation_api"]), ("responses", "responses"))

    def test_both_public_configuration_apis_remove_all_nested_credentials(self):
        self.legacy(correction_api_key="fixture-extra-correction-secret")
        self.save_initial()
        for endpoint in ("/api/settings", "/api/service-config", "/api/import-settings"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_secret_free(response.json())
            self.assertNotIn("fixture-extra-correction-secret", response.text)

    def test_blank_key_keeps_own_key_and_clear_never_inherits_another_profile_or_shared_key(self):
        self.legacy()
        self.save_initial()
        draft = self.body()
        for profile in draft["providers"]:
            profile["api_key"] = ""
        self.assertEqual(self.save(draft).status_code, 200)
        self.assertEqual(effective_provider(self.store.load(), "import_transcription")["api_key"], self.speech_key)
        draft["providers"][0]["clear_api_key"] = True
        result = self.save(draft)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(effective_provider(self.store.load(), "import_transcription")["api_key"], "")
        self.assertEqual(effective_provider(self.store.load(), "translation")["api_key"], self.text_key)
        self.assertFalse(result.json()["ready"]["file_asr"])
        self.assertFalse(result.json()["providers"][0]["key_configured"])

    def test_cross_origin_save_requires_reentered_own_key_and_preserves_saved_config_on_failure(self):
        self.save_initial()
        previous = self.store.path.read_bytes()
        draft = self.body()
        draft["providers"][0]["base_url"] = "https://new-speech.example/v1"
        draft["providers"][0]["api_key"] = ""
        response = self.save(draft)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.path.read_bytes(), previous)
        draft["providers"][0]["api_key"] = self.speech_key
        response = self.save(draft)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(effective_provider(self.store.load(), "import_transcription")["base_url"],
                         "https://new-speech.example/v1")

    def test_cross_origin_models_cannot_send_saved_key_without_explicit_key(self):
        self.save_initial()
        previous = self.store.path.read_bytes()
        draft = {"provider_id": "speech-minimax", "base_url": "https://new-speech.example/v1", "api_key": ""}
        response = self.discover(draft)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.path.read_bytes(), previous)
        draft["api_key"] = self.draft_key
        response = self.discover(draft)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_request("https://new-speech.example/v1/models", self.draft_key)
        self.assertEqual(self.store.path.read_bytes(), previous)

    def test_model_discovery_returns_all_model_types_using_draft_without_persistence_or_version_guess(self):
        self.save_initial()
        previous = self.store.path.read_bytes()
        self.handler = lambda request: httpx.Response(200, json={"data": [
            {"id": "text-only"}, {"id": "embedding-only"}, {"id": "asr-1.0"},
            {"id": "text-only"}, {"id": ""}, {"id": "invalid\nmodel"}, {"id": 17}, {},
        ]})
        response = self.discover({"provider_id": "new-unsaved", "base_url": "https://draft.example/gateway/",
                                  "api_key": self.draft_key})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"models": ["asr-1.0", "embedding-only", "text-only"]})
        self.assert_request("https://draft.example/gateway/models", self.draft_key)
        self.assertEqual(self.probe_options, [{"timeout": 12, "follow_redirects": False}])
        self.assertEqual(self.store.path.read_bytes(), previous)

    def test_empty_models_are_not_replaced_by_a_different_task_model(self):
        self.save_initial()
        for task in ("file_asr", "correction", "translation"):
            with self.subTest(task=task):
                draft = self.body()
                draft["routes"][task]["model"] = ""
                response = self.save(draft)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["ready"][task])
                self.assertFalse(self.client.get("/api/settings").json()["import_configuration_ready"])

    def test_text_only_connection_cannot_be_selected_as_audio_recognition(self):
        draft = self.body()
        draft["routes"]["file_asr"]["provider_id"] = "gpt-text"
        response = self.save(draft)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.store.path.exists())

    def test_disabled_profile_and_removed_routes_cannot_inherit_legacy_credentials(self):
        self.legacy()
        self.save_initial()
        draft = self.body()
        draft["providers"][1]["enabled"] = False
        response = self.save(draft)
        self.assertEqual(response.status_code, 200, response.text)
        for role in ("correction", "translation"):
            self.assertEqual(effective_provider(self.store.load(), role), {"api_key": "", "base_url": ""})
            self.assertFalse(response.json()["ready"][role])
        response = self.save({"providers": [], "routes": {}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(any(response.json()["ready"].values()))
        for role in ("correction", "translation", "import_transcription"):
            self.assertEqual(effective_provider(self.store.load(), role), {"api_key": "", "base_url": ""})

    def test_clearing_migrated_environment_key_disables_only_that_connection(self):
        self.legacy(import_transcription_base_url="https://speech.example/v1",
                    import_transcription_api_key=self.speech_key, import_transcription_model="asr-1.0")
        os.environ["OPENAI_API_KEY"] = self.environment_key
        initial = self.client.get("/api/service-config").json()
        text_id = initial["routes"]["translation"]["provider_id"]
        speech_id = initial["routes"]["file_asr"]["provider_id"]
        self.assertNotEqual(text_id, speech_id)
        draft = {"providers": initial["providers"], "routes": initial["routes"]}
        for profile in draft["providers"]:
            if profile["id"] == text_id:
                profile["clear_api_key"] = True
        response = self.save(draft)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(self.environment_key, self.store.path.read_text())
        self.assertEqual(effective_provider(self.store.load(), "translation")["api_key"], "")
        self.assertEqual(effective_provider(self.store.load(), "correction")["api_key"], "")
        self.assertEqual(effective_provider(self.store.load(), "import_transcription")["api_key"], self.speech_key)
        self.assertTrue(response.json()["ready"]["file_asr"])

    def test_environment_migration_never_persists_environment_secret_or_falls_back_to_old_shared_key(self):
        self.legacy()
        os.environ["OPENAI_API_KEY"] = self.environment_key
        initial = self.client.get("/api/service-config").json()
        self.assert_secret_free(initial)
        providers = initial["providers"]
        self.assertTrue(all(profile["key_from_environment"] for profile in providers))
        draft = {"providers": [{key: value for key, value in profile.items()
                                if key not in ("key_configured", "key_from_environment")}
                               for profile in providers], "routes": initial["routes"]}
        response = self.save(draft)
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.store.path.read_text()
        self.assertNotIn(self.environment_key, stored)
        self.assertEqual(effective_provider(self.store.load(), "translation")["api_key"], self.environment_key)
        os.environ.pop("OPENAI_API_KEY")
        for role in ("correction", "translation", "import_transcription"):
            self.assertEqual(effective_provider(self.store.load(), role)["api_key"], "", role)
        public = self.client.get("/api/service-config").json()
        self.assertFalse(public["ready"]["correction"])
        self.assertFalse(public["ready"]["translation"])

    def test_invalid_routes_duplicate_ids_and_controls_never_echo_secret_inputs(self):
        self.save_initial()
        previous = self.store.path.read_bytes()
        invalid = []
        draft = self.body(); draft["routes"]["correction"]["provider_id"] = "unknown-provider"; invalid.append(draft)
        draft = self.body(); draft["providers"][1]["id"] = "speech-minimax"; invalid.append(draft)
        draft = self.body(); draft["providers"][0]["name"] = "bad\nname"; invalid.append(draft)
        draft = self.body(); draft["providers"][0]["api_key"] = self.draft_key + "\x00suffix"; invalid.append(draft)
        draft = self.body(); draft["routes"]["translation"]["model"] = "bad\nmodel"; invalid.append(draft)
        draft = self.body(); draft["providers"][0]["base_url"] = "https://user:" + self.draft_key + "@speech.example"; invalid.append(draft)
        draft = self.body(); draft["providers"][0]["speech_api"] = self.draft_key; invalid.append(draft)
        draft = self.body(); draft["providers"][0]["api_key"] = {"secret": self.draft_key}; invalid.append(draft)
        for draft in invalid:
            with self.subTest(index=invalid.index(draft)):
                response = self.save(draft)
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(self.store.path.read_bytes(), previous)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
