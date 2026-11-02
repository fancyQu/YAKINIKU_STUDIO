"""Role-specific model discovery uses drafts without saving or generating text."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from yakiniku.server import create_app
from yakiniku.storage import SettingsStore


class ModelListingTests(unittest.TestCase):
    shared_key = "test-shared-private-key"
    translation_key = "test-translation-private-key"

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

        def factory(**kwargs):
            self.client_options.append(kwargs)
            return httpx.AsyncClient(transport=httpx.MockTransport(transport), **kwargs)

        self.client = TestClient(create_app(self.root, probe_client_factory=factory),
                                 base_url="http://127.0.0.1:8765")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def discover(self, **draft):
        return self.client.post("/api/settings/models", json=draft)

    def shared(self, **updates):
        self.store.save({"api_base_url": "https://shared.example/proxy/v1",
                         "api_key": self.shared_key, **updates})

    def assert_auth(self, url, key):
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), url)
        self.assertEqual(request.content, b"")
        self.assertEqual(request.headers["Authorization"], "Bearer " + key)

    def test_first_unsaved_draft_returns_every_valid_model_without_filtering_type(self):
        self.handler = lambda request: httpx.Response(200, json={"data": [
            {"id": "z-text"}, {"id": "whisper-1"}, {"id": "embedding-model"},
            {"id": "vendor/翻译 2"}, {"id": "z-text"}, {"id": ""},
            {"id": "bad\nmodel"}, {"id": "x" * 257}, {"id": 17}, {}, "bad",
        ]})
        response = self.discover(api_base_url="https://draft.example/team/v1/",
                                 api_key=self.shared_key)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"models": [
            "embedding-model", "vendor/翻译 2", "whisper-1", "z-text",
        ]})
        self.assert_auth("https://draft.example/team/v1/models", self.shared_key)
        self.assertFalse(self.store.path.exists(), "discovery must not persist the draft")
        self.assertEqual(self.client_options, [{"timeout": 12, "follow_redirects": False}])

    def test_transcription_discovery_uses_only_the_selected_role_provider(self):
        self.shared(translation_base_url="https://translation.example/v1",
                    translation_api_key=self.translation_key)
        previous = self.store.path.read_bytes()
        self.handler = lambda request: httpx.Response(200, json={"data": [
            {"id": "provider-speech-model"}, {"id": "provider-text-model"},
        ]})
        response = self.discover(
            role="transcription", transcription_base_url="https://speech.example/team/v1/",
            transcription_api_key="speech-test-key", translation_base_url="invalid unused draft",
            translation_api_key="ignored translation key with spaces",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], ["provider-speech-model", "provider-text-model"])
        self.assert_auth("https://speech.example/team/v1/models", "speech-test-key")
        self.assertEqual(self.store.path.read_bytes(), previous)

    def test_saved_transcription_key_is_not_sent_to_a_new_origin_implicitly(self):
        self.shared(transcription_base_url="https://speech.example/v1",
                    transcription_api_key="speech-test-key")
        response = self.discover(role="transcription", transcription_base_url="https://new.example/v1",
                                 api_key=self.shared_key)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("语音识别", response.json()["detail"])
        self.assertEqual(self.requests, [])
        response = self.discover(role="transcription", transcription_base_url="https://new.example/v1",
                                 transcription_api_key="speech-test-key")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_auth("https://new.example/v1/models", "speech-test-key")

    def test_transcription_clear_key_and_url_use_shared_without_persisting(self):
        self.shared(transcription_base_url="https://speech.example/v1",
                    transcription_api_key="speech-test-key")
        previous = self.store.path.read_bytes()
        response = self.discover(role="transcription", transcription_base_url="",
                                 transcription_api_key="", clear_transcription_api_key=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_auth("https://shared.example/proxy/v1/models", self.shared_key)
        self.assertEqual(self.store.path.read_bytes(), previous)

    def test_invalid_role_is_rejected_before_network(self):
        response = self.discover(role="other", api_key=self.shared_key)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn(self.shared_key, response.text)
        self.assertEqual(self.requests, [])

    def test_saved_translation_provider_takes_precedence_and_blanks_preserve_keys(self):
        self.shared(translation_base_url="https://text.example/v1",
                    translation_api_key=self.translation_key)
        previous = self.store.path.read_bytes()
        response = self.discover(api_key="", translation_api_key="")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_auth("https://text.example/v1/models", self.translation_key)
        self.assertEqual(previous, self.store.path.read_bytes())

    def test_explicit_empty_role_url_falls_back_and_clear_role_key_uses_shared(self):
        self.shared(translation_base_url="https://text.example/v1",
                    translation_api_key=self.translation_key)
        response = self.discover(translation_base_url="", clear_translation_api_key=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_auth("https://shared.example/proxy/v1/models", self.shared_key)
        self.assertEqual(self.store.load()["translation_api_key"], self.translation_key)

    def test_independent_translation_does_not_require_shared_or_transcription_configuration(self):
        for shared_url in ("", "unfinished shared address"):
            with self.subTest(shared_url=shared_url):
                self.requests.clear()
                response = self.discover(api_base_url=shared_url,
                                         translation_base_url="https://text.example/v1",
                                         translation_api_key=self.translation_key)
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_auth("https://text.example/v1/models", self.translation_key)

    def test_missing_address_or_key_fails_before_network(self):
        for draft, message in (({}, "地址"), ({"api_base_url": "https://text.example/v1"}, "密钥")):
            with self.subTest(draft=draft):
                response = self.discover(**draft)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(message, response.json()["detail"])
        self.assertEqual(self.requests, [])

    def test_changed_origin_cannot_reuse_a_saved_shared_key_implicitly(self):
        self.shared()
        response = self.discover(api_base_url="https://new.example/v1", api_key="")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("重新填写", response.json()["detail"])
        self.assertEqual(self.requests, [])
        response = self.discover(api_base_url="https://new.example/v1", api_key=self.shared_key)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_auth("https://new.example/v1/models", self.shared_key)

    def test_explicit_shared_key_cannot_implicitly_authorize_retained_role_key_on_new_origin(self):
        self.shared(translation_base_url="https://text.example/v1",
                    translation_api_key=self.translation_key)
        response = self.discover(api_key=self.shared_key,
                                 translation_base_url="https://new.example/v1")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.requests, [])
        response = self.discover(translation_base_url="https://new.example/v1",
                                 translation_api_key=self.translation_key)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_auth("https://new.example/v1/models", self.translation_key)

    def test_same_origin_prefix_change_retains_saved_key(self):
        self.shared()
        response = self.discover(api_base_url="https://shared.example/other/v1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_auth("https://shared.example/other/v1/models", self.shared_key)

    def test_environment_key_precedence_and_changed_origin_protection(self):
        self.shared()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-test-key"}):
            response = self.discover(api_key="ignored-draft-key")
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_auth("https://shared.example/proxy/v1/models", "environment-test-key")
            self.requests.clear()
            response = self.discover(api_base_url="https://new.example/v1", api_key=self.shared_key)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.requests, [])
            response = self.discover(api_base_url="https://new.example/v1",
                                     translation_api_key=self.translation_key)
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_auth("https://new.example/v1/models", self.translation_key)

    def test_upstream_errors_and_redirects_are_fixed_messages_without_credentials(self):
        self.shared()
        for status in (301, 401, 403, 404, 405, 429, 500):
            with self.subTest(status=status):
                self.requests.clear()
                self.handler = lambda request: httpx.Response(
                    status, headers={"location": "https://unapproved.example/v1/models"},
                    json={"error": {"message": self.shared_key}})
                response = self.discover()
                self.assertEqual(response.status_code, 502, response.text)
                self.assertNotIn(self.shared_key, response.text)
                self.assertNotIn("unapproved.example", response.text)
                self.assertEqual(len(self.requests), 1, "must never follow a redirect")

    def test_malformed_empty_and_explicitly_paginated_responses(self):
        self.shared()
        for body in ({}, {"data": {}}, [], {"data": [], "has_more": True},
                     {"data": [{"id": "first"}], "next_page": "2"},
                     {"data": [], "next": "https://other.example/models"}):
            with self.subTest(body=body):
                self.handler = lambda request: httpx.Response(200, json=body)
                response = self.discover()
                self.assertEqual(response.status_code, 502, response.text)
        self.handler = lambda request: httpx.Response(200, json={"data": []})
        response = self.discover()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"models": []})

    def test_network_and_json_errors_do_not_reflect_provider_text(self):
        self.shared()
        for failure in (httpx.ReadTimeout, httpx.ConnectError):
            with self.subTest(failure=failure):
                def fail(request):
                    raise failure(self.shared_key, request=request)
                self.handler = fail
                response = self.discover()
                self.assertEqual(response.status_code, 502, response.text)
                self.assertNotIn(self.shared_key, response.text)
        self.handler = lambda request: httpx.Response(200, text=self.shared_key)
        response = self.discover()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn(self.shared_key, response.text)

    def test_input_validation_does_not_reflect_secrets_or_make_requests(self):
        for draft in ({"api_key": self.shared_key * 100},
                      {"api_key": {"secret": self.shared_key}},
                      {"api_base_url": "https://user:password@text.example/v1", "api_key": self.shared_key},
                      {"api_base_url": "http://remote.example/v1", "api_key": self.shared_key},
                      {"api_base_url": "https://text.example/v1", "api_key": "bad\nkey"}):
            with self.subTest(draft=draft):
                response = self.discover(**draft)
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertNotIn(self.shared_key, response.text)
                self.assertNotIn("password", response.text)
                self.assertNotIn('"input"', response.text)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
