"""Independent file-ASR settings and model discovery use isolated local fakes."""

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


class ImportSettingsTests(unittest.TestCase):
    shared_key = "fixture-shared-secret"
    import_key = "fixture-file-asr-secret"
    translation_key = "fixture-translation-secret"
    draft_key = "fixture-draft-asr-secret"
    origin = "http://127.0.0.1:8765"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.store = SettingsStore(self.root)
        self.requests = []
        self.client_options = []
        self.handler = lambda request: httpx.Response(200, json={"data": []})

        def transport(request):
            self.requests.append(request)
            return self.handler(request)

        def client_factory(**options):
            self.client_options.append(options)
            return httpx.AsyncClient(transport=httpx.MockTransport(transport), **options)

        self.client = TestClient(create_app(self.root, probe_client_factory=client_factory), base_url=self.origin)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def assert_private(self, value):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for key in (self.shared_key, self.import_key, self.translation_key, self.draft_key):
            self.assertNotIn(key, rendered)

    def shared(self, **values):
        self.store.save({
            "api_base_url": "https://shared.example", "api_key": self.shared_key,
            "transcription_mode": "local", "transcription_model": "tiny",
            "translation_base_url": "https://translate.example/gateway",
            "translation_api_key": self.translation_key, "translation_model": "fixture-text-model",
            "glossary": "配信 = 直播", **values,
        })

    def save(self, **values):
        response = self.client.post("/api/import-settings", json=values)
        self.assert_private(response.text)
        return response

    def discover(self, **draft):
        response = self.client.post("/api/settings/models", json={"role": "import_transcription", **draft})
        self.assert_private(response.text)
        return response

    def assert_request(self, url, key):
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), url)
        self.assertEqual(request.headers["Authorization"], "Bearer " + key)
        self.assertEqual(request.content, b"")

    def test_defaults_do_not_guess_a_speech_model_or_change_live_local_tiny(self):
        response = self.client.get("/api/import-settings")
        self.assertEqual(response.status_code, 200, response.text)
        public = response.json()
        self.assertEqual(public["import_transcription_model"], "")
        self.assertEqual(public["import_transcription_base_url"], "")
        self.assertEqual(public["import_transcription_response_format"], "verbose_json")
        self.assertFalse(public["import_configuration_ready"])
        self.assertFalse(public["import_transcription_key_configured"])
        self.assertEqual(self.store.load()["transcription_mode"], "local")
        self.assertEqual(self.store.load()["transcription_model"], "tiny")
        self.assertNotIn("import_transcription_api_key", public)
        self.assertFalse(self.store.path.exists(), "read-only configuration must not create a settings file")

    def test_saving_independent_cloud_configuration_preserves_all_live_and_translation_settings(self):
        self.shared()
        before = self.store.load()
        response = self.save(import_transcription_base_url="https://speech.example/team/",
                             import_transcription_api_key=self.import_key,
                             import_transcription_model="provider/file-speech-model",
                             import_transcription_response_format="srt",
                             transcription_mode="realtime", transcription_model="should-not-change")
        self.assertEqual(response.status_code, 200, response.text)
        public = response.json()
        self.assertEqual(public["import_transcription_base_url"], "https://speech.example/team")
        self.assertEqual(public["import_effective_base_url"], "https://speech.example/team")
        self.assertEqual(public["import_transcription_model"], "provider/file-speech-model")
        self.assertTrue(public["import_transcription_has_own_key"])
        self.assertTrue(public["import_configuration_ready"])
        self.assertEqual(self.store.load()["import_transcription_api_key"], self.import_key)
        for name in before:
            if not name.startswith("import_"):
                self.assertEqual(self.store.load()[name], before[name], name)
        for route in ("/api/import-settings", "/api/settings"):
            result = self.client.get(route)
            self.assertEqual(result.status_code, 200, result.text)
            self.assert_private(result.text)
            for name in ("api_key", "transcription_api_key", "translation_api_key", "import_transcription_api_key"):
                self.assertNotIn(name, result.json())

    def test_blank_key_preserves_saved_key_and_explicit_clear_uses_shared_fallback(self):
        self.shared(import_transcription_base_url="https://speech.example/custom",
                    import_transcription_api_key=self.import_key,
                    import_transcription_model="fixture-speech")
        response = self.save(import_transcription_api_key="", import_transcription_response_format="json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.load()["import_transcription_api_key"], self.import_key)
        self.assertTrue(response.json()["import_transcription_has_own_key"])
        response = self.save(import_transcription_api_key="", clear_import_transcription_api_key=True,
                             import_transcription_base_url="")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.load()["import_transcription_api_key"], "")
        self.assertEqual(response.json()["import_transcription_base_url"], "")
        self.assertEqual(response.json()["import_effective_base_url"], "https://shared.example")
        self.assertFalse(response.json()["import_transcription_has_own_key"])
        self.assertTrue(response.json()["import_transcription_key_configured"])
        self.assertEqual(effective_provider(self.store.load(), "import_transcription"),
                         {"base_url": "https://shared.example", "api_key": self.shared_key})

    def test_cloud_readiness_does_not_require_a_local_speech_model_but_does_require_translation(self):
        self.shared(transcription_model="not-installed-local-model")
        response = self.save(import_transcription_model="fixture-speech")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["import_configuration_ready"])
        self.store.save({"translation_model": ""})
        self.assertFalse(self.client.get("/api/import-settings").json()["import_configuration_ready"])
        self.store.save({"translation_model": "fixture-text-model", "api_key": "", "translation_api_key": ""})
        public = self.client.get("/api/import-settings").json()
        self.assertFalse(public["translation_key_configured"])
        self.assertFalse(public["import_configuration_ready"])

    def test_new_origin_requires_explicit_key_and_same_origin_path_change_preserves_key(self):
        self.shared(import_transcription_base_url="https://speech.example/old",
                    import_transcription_api_key=self.import_key,
                    import_transcription_model="fixture-speech")
        previous = self.store.path.read_bytes()
        response = self.save(import_transcription_base_url="https://new-speech.example", import_transcription_api_key="")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("重新填写", response.json()["detail"])
        self.assertEqual(self.store.path.read_bytes(), previous)
        response = self.save(import_transcription_base_url="https://speech.example/new")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.load()["import_transcription_api_key"], self.import_key)
        response = self.save(import_transcription_base_url="https://new-speech.example", import_transcription_api_key=self.import_key)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["import_effective_base_url"], "https://new-speech.example")

    def test_discovery_uses_unsaved_role_draft_and_returns_all_models_without_adding_v1(self):
        self.shared(import_transcription_base_url="https://saved-speech.example/v2",
                    import_transcription_api_key=self.import_key,
                    import_transcription_model="saved-model")
        previous = self.store.path.read_bytes()
        self.handler = lambda request: httpx.Response(200, json={"data": [
            {"id": "text-only-model"}, {"id": "embedding-model"}, {"id": "speech-model"},
            {"id": "vendor/日本語モデル"}, {"id": "speech-model"}, {"id": ""},
            {"id": "bad\nmodel"}, {"id": 17}, {}, "bad",
        ]})
        response = self.discover(import_transcription_base_url="https://draft-speech.example/team/",
                                 import_transcription_api_key=self.draft_key,
                                 translation_base_url="invalid ignored translation draft",
                                 translation_api_key="ignored key with spaces")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"models": ["embedding-model", "speech-model", "text-only-model", "vendor/日本語モデル"]})
        self.assert_request("https://draft-speech.example/team/models", self.draft_key)
        self.assertEqual(self.client_options, [{"timeout": 12, "follow_redirects": False}])
        self.assertEqual(self.store.path.read_bytes(), previous)

    def test_discovery_can_use_saved_own_key_or_clear_to_shared_without_saving(self):
        self.shared(import_transcription_base_url="https://saved-speech.example",
                    import_transcription_api_key=self.import_key)
        previous = self.store.path.read_bytes()
        response = self.discover(import_transcription_api_key="")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_request("https://saved-speech.example/models", self.import_key)
        self.requests.clear()
        response = self.discover(import_transcription_base_url="", clear_import_transcription_api_key=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_request("https://shared.example/models", self.shared_key)
        self.assertEqual(self.store.path.read_bytes(), previous)

    def test_discovery_draft_cannot_implicitly_send_saved_key_to_a_new_origin(self):
        self.shared(import_transcription_base_url="https://saved-speech.example",
                    import_transcription_api_key=self.import_key)
        previous = self.store.path.read_bytes()
        response = self.discover(import_transcription_base_url="https://new-speech.example", api_key=self.shared_key)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.requests, [])
        response = self.discover(import_transcription_base_url="https://new-speech.example", import_transcription_api_key=self.import_key)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_request("https://new-speech.example/models", self.import_key)
        self.assertEqual(self.store.path.read_bytes(), previous)

    def test_invalid_response_format_model_url_and_key_are_rejected_without_echo(self):
        self.shared(import_transcription_model="fixture-speech")
        previous = self.store.path.read_bytes()
        examples = [
            {"import_transcription_response_format": self.draft_key},
            {"import_transcription_model": "bad\nmodel"},
            {"import_transcription_base_url": "https://user:" + self.draft_key + "@speech.example"},
            {"import_transcription_base_url": "https://speech.example/audio/transcriptions"},
            {"import_transcription_api_key": self.draft_key + "\x00suffix"},
            {"import_transcription_api_key": "bad private key"},
        ]
        for example in examples:
            with self.subTest(field=next(iter(example))):
                response = self.save(**example)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.store.path.read_bytes(), previous)
                self.assertNotIn("bad\nmodel", response.text)
                self.assertNotIn("bad private key", response.text)
        self.assertEqual(self.requests, [])

    def test_validation_errors_never_include_original_secrets(self):
        for route, body in [
            ("/api/import-settings", {"import_transcription_api_key": {"private": self.draft_key}}),
            ("/api/import-settings", {"import_transcription_model": "x" * 257, "import_transcription_api_key": self.draft_key}),
            ("/api/settings/models", {"role": "import_transcription", "import_transcription_api_key": {"private": self.draft_key}}),
        ]:
            with self.subTest(route=route):
                response = self.client.post(route, json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_private(response.text)
        self.assertEqual(self.requests, [])

    def test_shared_origin_change_cannot_redirect_retained_independent_import_key(self):
        self.shared(import_transcription_base_url="", import_transcription_api_key=self.import_key,
                    import_transcription_model="fixture-speech")
        previous = self.store.path.read_bytes()
        for explicit_shared_key in (self.shared_key, self.draft_key):
            with self.subTest(changed_shared_key=explicit_shared_key != self.shared_key):
                response = self.client.post("/api/settings", json={
                    "api_base_url": "https://new-shared.example", "api_key": explicit_shared_key,
                })
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("完整音频语音识别", response.json()["detail"])
                self.assert_private(response.text)
                self.assertEqual(self.store.path.read_bytes(), previous)
        self.assertEqual(self.requests, [])

    def test_shared_same_origin_path_change_preserves_independent_import_key(self):
        self.shared(import_transcription_base_url="", import_transcription_api_key=self.import_key,
                    import_transcription_model="fixture-speech")
        response = self.client.post("/api/settings", json={"api_base_url": "https://shared.example/new/path/"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_private(response.text)
        self.assertEqual(effective_provider(self.store.load(), "import_transcription"), {
            "base_url": "https://shared.example/new/path", "api_key": self.import_key,
        })
        self.assertEqual(self.store.load()["transcription_mode"], "local")
        self.assertEqual(self.store.load()["transcription_model"], "tiny")
        self.assertEqual(self.requests, [])

    def test_shared_origin_can_change_when_import_has_a_fixed_independent_base(self):
        self.shared(import_transcription_base_url="https://speech.example/fixed",
                    import_transcription_api_key=self.import_key,
                    import_transcription_model="fixture-speech")
        response = self.client.post("/api/settings", json={
            "api_base_url": "https://new-shared.example", "api_key": self.draft_key,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_private(response.text)
        self.assertEqual(self.store.load()["api_base_url"], "https://new-shared.example")
        self.assertEqual(effective_provider(self.store.load(), "import_transcription"), {
            "base_url": "https://speech.example/fixed", "api_key": self.import_key,
        })
        self.assertEqual(self.requests, [])

    def test_import_inheriting_shared_key_accepts_an_explicit_shared_key_for_new_origin(self):
        self.shared(import_transcription_base_url="", import_transcription_api_key="",
                    import_transcription_model="fixture-speech")
        previous = self.store.path.read_bytes()
        response = self.client.post("/api/settings", json={"api_base_url": "https://new-shared.example"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.path.read_bytes(), previous)
        response = self.client.post("/api/settings", json={
            "api_base_url": "https://new-shared.example", "api_key": self.shared_key,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_private(response.text)
        self.assertEqual(effective_provider(self.store.load(), "import_transcription"), {
            "base_url": "https://new-shared.example", "api_key": self.shared_key,
        })
        self.assertFalse(response.json()["import_transcription_has_own_key"])
        self.assertEqual(self.requests, [])

    def test_request_shared_key_cannot_authorize_redirect_of_an_environment_shared_key(self):
        self.shared(import_transcription_base_url="", import_transcription_api_key="",
                    import_transcription_model="fixture-speech")
        previous = self.store.path.read_bytes()
        environment_key = "fixture-environment-secret"
        with patch.dict(os.environ, {"OPENAI_API_KEY": environment_key}):
            for submitted_key in (environment_key, self.draft_key):
                with self.subTest(submitted_matches_environment=submitted_key == environment_key):
                    response = self.client.post("/api/settings", json={
                        "api_base_url": "https://new-shared.example", "api_key": submitted_key,
                    })
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("完整音频语音识别", response.json()["detail"])
                    self.assert_private(response.text)
                    self.assertNotIn(environment_key, response.text)
                    self.assertEqual(self.store.path.read_bytes(), previous)
                    self.assertEqual(effective_provider(self.store.load(), "import_transcription"), {
                        "base_url": "https://shared.example", "api_key": environment_key,
                    })
        self.assertEqual(self.requests, [])

    def test_discovery_rejects_invalid_key_before_any_network_or_settings_change(self):
        self.shared()
        previous = self.store.path.read_bytes()
        response = self.discover(import_transcription_api_key=self.draft_key + "\x00suffix")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.path.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
