"""Independent ASR/text settings updates never replace the other module."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from yakiniku.server import create_app
from yakiniku.storage import SettingsStore


class SectionConfigurationTests(unittest.TestCase):
    speech_key = "fixture-section-speech-secret"
    text_key = "fixture-section-text-secret"
    shared_key = "fixture-section-shared-secret"
    draft_key = "fixture-section-draft-secret"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.store = SettingsStore(self.root)
        self.requests = []

        def transport(request):
            self.requests.append(request)
            return httpx.Response(200, json={"data": []})

        def factory(**options):
            return httpx.AsyncClient(transport=httpx.MockTransport(transport), **options)

        self.app = create_app(self.root, probe_client_factory=factory,
                              local_transcriber_factory=lambda **options: None)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self.assert_no_provider_requests)

    def assert_no_provider_requests(self):
        self.assertEqual(self.requests, [])

    def assert_secret_free(self, value):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for secret in (self.speech_key, self.text_key, self.shared_key, self.draft_key):
            self.assertNotIn(secret, rendered)
        if isinstance(value, dict):
            for name, child in value.items():
                self.assertNotIn(name, ("api_key", "correction_api_key", "translation_api_key",
                                        "import_transcription_api_key", "transcription_api_key"))
                self.assert_secret_free(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_secret_free(child)

    def provider(self, identifier, scope, **updates):
        speech = scope == "asr"
        return {"id": identifier, "name": identifier, "scope": scope,
                "vendor": "minimax" if speech else "custom",
                "base_url": "https://speech.example/v1" if speech else "https://text.example/v1",
                "api_key": self.speech_key if speech else self.text_key,
                "text_api": "chat_completions" if speech else "responses",
                "speech_api": "minimax" if speech else "none", "enabled": True, **updates}

    def body(self):
        return {"providers": [
            self.provider("asr-main", "asr"), self.provider("asr-unused", "asr"),
            self.provider("text-main", "text"), self.provider("text-unused", "text"),
        ], "routes": {
            "file_asr": {"provider_id": "asr-main", "model": "asr-1.0", "response_format": "verbose_json"},
            "correction": {"provider_id": "text-main", "model": "chosen-proofreader", "response_format": "verbose_json"},
            "translation": {"provider_id": "text-main", "model": "chosen-translator", "response_format": "verbose_json"},
        }}

    def initial(self, body=None):
        self.store.save({"transcription_mode": "local", "transcription_model": "tiny", "glossary": "保持词表"})
        response = self.client.post("/api/service-config", json=body or self.body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_secret_free(response.json())
        return response.json()

    def put_provider(self, body):
        response = self.client.put("/api/service-config/provider", json=body)
        self.assert_secret_free(response.text)
        return response

    def put_section(self, section, body):
        response = self.client.put(f"/api/service-config/section/{section}", json=body)
        self.assert_secret_free(response.text)
        return response

    def stored(self):
        return self.store._stored()

    def profiles(self):
        return {p["id"]: p for p in self.stored()["service_profiles"]}

    def section_body(self, section, body=None):
        full = copy.deepcopy(body or self.body())
        tasks = ("file_asr",) if section == "asr" else ("correction", "translation")
        return {"providers": [p for p in full["providers"] if p["scope"] in (section, "shared")],
                "routes": {task: full["routes"][task] for task in tasks}}

    def assert_live_unchanged(self):
        settings = self.store.load()
        self.assertEqual((settings["transcription_mode"], settings["transcription_model"]), ("local", "tiny"))
        self.assertEqual(settings["glossary"], "保持词表")

    def test_old_profiles_infer_scope_from_routes_then_unused_speech_capability_without_persistence(self):
        profiles = [self.provider("speech", "asr", speech_api="none"),
                    self.provider("text", "text", speech_api="minimax"),
                    self.provider("unused-speech", "asr", speech_api="openai"),
                    self.provider("unused-text", "text")]
        for profile in profiles:
            profile.pop("scope")
        routes = {"file_asr": {"provider_id": "speech", "model": "speech-model"},
                  "correction": {"provider_id": "text", "model": "text-model"},
                  "translation": {"provider_id": "text", "model": "text-model"}}
        self.store.save({"service_profiles_enabled": True, "service_profiles": profiles, "task_routes": routes})
        before = self.store.path.read_bytes()
        response = self.client.get("/api/service-config")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_secret_free(response.json())
        inferred = {p["id"]: p["scope"] for p in response.json()["providers"]}
        self.assertEqual(inferred, {"speech": "asr", "text": "text", "unused-speech": "asr", "unused-text": "text"})
        self.assertEqual(self.store.path.read_bytes(), before)
        routes["translation"]["provider_id"] = "speech"
        self.store.save({"task_routes": routes})
        before = self.store.path.read_bytes()
        result = self.client.get("/api/service-config").json()
        inferred = {p["id"]: p["scope"] for p in result["providers"]}
        self.assertEqual(inferred["speech"], "text")
        self.assertEqual(inferred["speech-asr"], "asr")
        self.assertEqual(result["routes"]["file_asr"]["provider_id"], "speech-asr")
        self.assertEqual(result["routes"]["translation"]["provider_id"], "speech")
        self.assertEqual(inferred["text"], "text")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_explicit_scope_is_kept_and_no_settings_are_created_by_first_read(self):
        response = self.client.get("/api/service-config")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.store.path.exists())
        body = self.body()
        body["providers"][0]["scope"] = "shared"
        self.initial(body)
        result = self.client.get("/api/service-config").json()
        scopes = {p["id"]: p["scope"] for p in result["providers"]}
        self.assertEqual(scopes["asr-main"], "text")
        self.assertEqual(scopes["asr-main-asr"], "asr")
        self.assertEqual(result["routes"]["file_asr"]["provider_id"], "asr-main-asr")

    def test_single_provider_add_does_not_change_routes_other_connections_or_live(self):
        self.initial()
        before = self.stored()
        response = self.put_provider(self.provider("new-text", "text", api_key=self.draft_key))
        self.assertEqual(response.status_code, 200, response.text)
        after = self.stored()
        self.assertEqual(after["task_routes"], before["task_routes"])
        by_id = self.profiles()
        self.assertEqual(set(by_id), {p["id"] for p in before["service_profiles"]} | {"new-text"})
        for profile in before["service_profiles"]:
            self.assertEqual(by_id[profile["id"]], profile)
        self.assertEqual(by_id["new-text"]["api_key"], self.draft_key)
        self.assertEqual(by_id["new-text"]["scope"], "text")
        self.assert_live_unchanged()

    def test_single_provider_edit_keeps_blank_key_and_omitted_scope(self):
        self.initial()
        before = self.stored()
        draft = self.provider("asr-main", "asr", name="更新的语音连接", api_key="")
        draft.pop("scope")
        response = self.put_provider(draft)
        self.assertEqual(response.status_code, 200, response.text)
        after = self.stored()
        self.assertEqual(after["task_routes"], before["task_routes"])
        profiles = self.profiles()
        self.assertEqual(profiles["asr-main"]["name"], "更新的语音连接")
        self.assertEqual(profiles["asr-main"]["api_key"], self.speech_key)
        self.assertEqual(profiles["asr-main"]["scope"], "asr")
        for original in before["service_profiles"]:
            if original["id"] != "asr-main":
                self.assertEqual(profiles[original["id"]], original)
        self.assert_live_unchanged()

    def test_single_provider_preserves_unrelated_legacy_records_and_routes_exactly(self):
        self.initial()
        raw = self.stored()
        raw["service_profiles"][2].pop("scope")
        raw["service_profiles"][2]["legacy_metadata"] = {"keep": "verbatim"}
        raw["task_routes"]["correction"].pop("response_format")
        self.store.save(raw)
        before = self.stored()
        response = self.put_provider(self.provider("asr-main", "asr", name="语音新名称", api_key=""))
        self.assertEqual(response.status_code, 200, response.text)
        after = self.stored()
        self.assertEqual(after["task_routes"], before["task_routes"])
        self.assertEqual(self.profiles()["text-main"], before["service_profiles"][2])

    def test_single_provider_cross_origin_change_requires_explicit_key(self):
        self.initial()
        before = self.store.path.read_bytes()
        draft = self.provider("asr-main", "asr", base_url="https://new-speech.example/v1", api_key="")
        response = self.put_provider(draft)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.path.read_bytes(), before)
        draft["api_key"] = self.draft_key
        response = self.put_provider(draft)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.profiles()["asr-main"]["api_key"], self.draft_key)
        self.assertEqual(self.profiles()["text-main"]["api_key"], self.text_key)

    def test_single_provider_clear_key_changes_only_its_connection(self):
        self.initial()
        before = self.stored()
        response = self.put_provider(self.provider("asr-main", "asr", api_key="", clear_api_key=True))
        self.assertEqual(response.status_code, 200, response.text)
        profiles = self.profiles()
        self.assertEqual(profiles["asr-main"]["api_key"], "")
        self.assertEqual(profiles["text-main"]["api_key"], self.text_key)
        self.assertEqual(self.stored()["task_routes"], before["task_routes"])

    def test_asr_section_saves_and_deletes_only_its_own_connections_and_route(self):
        self.initial()
        before = self.stored()
        body = self.section_body("asr")
        body["providers"] = [self.provider("asr-main", "asr", name="语音配置更新", api_key=""),
                             self.provider("asr-new", "asr", api_key=self.draft_key)]
        body["routes"]["file_asr"].update(provider_id="asr-new", model="user-chosen-new-speech-model")
        response = self.put_section("asr", body)
        self.assertEqual(response.status_code, 200, response.text)
        profiles = self.profiles()
        self.assertNotIn("asr-unused", profiles)
        self.assertEqual(profiles["asr-main"]["api_key"], self.speech_key)
        self.assertEqual(profiles["asr-new"]["api_key"], self.draft_key)
        for original in before["service_profiles"]:
            if original["scope"] == "text":
                self.assertEqual(profiles[original["id"]], original)
        routes = self.stored()["task_routes"]
        self.assertEqual(routes["file_asr"], body["routes"]["file_asr"])
        for task in ("correction", "translation"):
            self.assertEqual(routes[task], before["task_routes"][task])
        self.assert_live_unchanged()

    def test_text_section_saves_and_deletes_only_its_own_connections_and_routes(self):
        self.initial()
        before = self.stored()
        body = self.section_body("text")
        body["providers"] = [self.provider("text-main", "text", api_key=""),
                             self.provider("text-new", "text", api_key=self.draft_key, text_api="anthropic",
                                           anthropic_auth="bearer")]
        body["routes"]["correction"].update(provider_id="text-new", model="chosen-anthropic-proofreader")
        body["routes"]["translation"]["model"] = "chosen-new-gpt-translator"
        response = self.put_section("text", body)
        self.assertEqual(response.status_code, 200, response.text)
        profiles = self.profiles()
        self.assertNotIn("text-unused", profiles)
        self.assertEqual(profiles["text-main"]["api_key"], self.text_key)
        self.assertEqual(profiles["text-new"]["anthropic_auth"], "bearer")
        for original in before["service_profiles"]:
            if original["scope"] == "asr":
                self.assertEqual(profiles[original["id"]], original)
        routes = self.stored()["task_routes"]
        self.assertEqual(routes["file_asr"], before["task_routes"]["file_asr"])
        for task in ("correction", "translation"):
            self.assertEqual(routes[task], body["routes"][task])
        self.assert_live_unchanged()

    def test_section_route_subset_keeps_unprovided_routes(self):
        self.initial()
        before = self.stored()
        body = self.section_body("text")
        body["routes"] = {"translation": {"provider_id": "text-main", "model": "only-translator-changed"}}
        response = self.put_section("text", body)
        self.assertEqual(response.status_code, 200, response.text)
        routes = self.stored()["task_routes"]
        self.assertEqual(routes["correction"], before["task_routes"]["correction"])
        self.assertEqual(routes["file_asr"], before["task_routes"]["file_asr"])
        self.assertEqual(routes["translation"]["model"], "only-translator-changed")

    def test_new_section_connection_omitting_scope_uses_current_module(self):
        for section, speech_api in (("text", "openai"), ("asr", "none")):
            with self.subTest(section=section):
                self.initial()
                body = self.section_body(section)
                draft = self.provider("new-unspecified", section, speech_api=speech_api)
                draft.pop("scope")
                body["providers"].append(draft)
                before_routes = self.stored()["task_routes"]
                response = self.put_section(section, body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.profiles()["new-unspecified"]["scope"], section)
                self.assertEqual(self.stored()["task_routes"], before_routes)

    def test_section_can_clear_own_connections_and_routes_without_clearing_other_module(self):
        for section, tasks, other_tasks in (("asr", ("file_asr",), ("correction", "translation")),
                                            ("text", ("correction", "translation"), ("file_asr",))):
            with self.subTest(section=section):
                self.initial()
                before = self.stored()
                response = self.put_section(section, {"providers": [], "routes": {
                    task: {"provider_id": "", "model": ""} for task in tasks}})
                self.assertEqual(response.status_code, 200, response.text)
                profiles = self.profiles()
                self.assertTrue(all(p["scope"] != section for p in profiles.values()))
                for original in before["service_profiles"]:
                    if original["scope"] != section:
                        self.assertEqual(profiles[original["id"]], original)
                for task in tasks:
                    self.assertEqual(self.stored()["task_routes"][task]["provider_id"], "")
                for task in other_tasks:
                    self.assertEqual(self.stored()["task_routes"][task], before["task_routes"][task])

    def test_section_rejects_other_modules_routes_without_writing(self):
        self.initial()
        before = self.store.path.read_bytes()
        for section, foreign_task in (("asr", "translation"), ("text", "file_asr")):
            with self.subTest(section=section):
                body = self.section_body(section)
                body["routes"][foreign_task] = {"provider_id": "", "model": "overwritten"}
                response = self.put_section(section, body)
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_section_cannot_modify_or_relabel_another_scopes_connection(self):
        self.initial()
        before = self.store.path.read_bytes()
        for section, target in (("asr", "text-main"), ("text", "asr-main")):
            for requested_scope in (section, "shared"):
                with self.subTest(section=section, requested_scope=requested_scope):
                    body = self.section_body(section)
                    body["providers"].append(self.provider(target, requested_scope, name="不应修改另一模块"))
                    response = self.put_section(section, body)
                    self.assertIn(response.status_code, (400, 422), response.text)
                    self.assertEqual(self.store.path.read_bytes(), before)

    def test_scoped_connections_cannot_be_routed_to_the_other_module(self):
        self.initial()
        before = self.store.path.read_bytes()
        for section, task, foreign_provider in (("asr", "file_asr", "text-main"),
                                                 ("text", "correction", "asr-main"),
                                                 ("text", "translation", "asr-main")):
            with self.subTest(section=section, task=task):
                body = self.section_body(section)
                body["routes"][task]["provider_id"] = foreign_provider
                response = self.put_section(section, body)
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(self.store.path.read_bytes(), before)

    def shared_body(self):
        body = self.body()
        body["providers"].append(self.provider("shared", "shared", speech_api="openai",
                                              base_url="https://shared.example/v1", api_key=self.shared_key))
        body["routes"]["file_asr"]["provider_id"] = "shared"
        body["routes"]["translation"]["provider_id"] = "shared"
        return body

    def test_split_legacy_connection_can_be_deleted_in_one_module_without_affecting_other(self):
        initial = self.shared_body()
        for section in ("asr", "text"):
            with self.subTest(section=section):
                self.store.save({"service_profiles_enabled": False, "service_profiles": [], "task_routes": {}})
                public = self.initial(initial)
                before = self.stored()
                target = "shared-asr" if section == "asr" else "shared"
                other = "shared" if section == "asr" else "shared-asr"
                body = self.section_body(section, public)
                body["providers"] = [p for p in body["providers"] if p["id"] != target]
                for route in body["routes"].values():
                    route["provider_id"] = "asr-main" if section == "asr" else "text-main"
                response = self.put_section(section, body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(target, self.profiles())
                self.assertEqual(self.profiles()[other], next(p for p in before["service_profiles"] if p["id"] == other))
                for task in (("correction", "translation") if section == "asr" else ("file_asr",)):
                    self.assertEqual(self.stored()["task_routes"][task], before["task_routes"][task])

    def test_stale_shared_payloads_cannot_overwrite_split_connections(self):
        initial = self.shared_body()
        self.initial(initial)
        before = self.store.path.read_bytes()
        for section in ("asr", "text"):
            with self.subTest(section=section):
                body = self.section_body(section, initial)
                next(p for p in body["providers"] if p["id"] == "shared")["scope"] = "asr" if section == "asr" else "shared"
                response = self.put_section(section, body)
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(self.store.path.read_bytes(), before)
                draft = next(copy.deepcopy(p) for p in initial["providers"] if p["id"] == "shared")
                draft["scope"] = "asr" if section == "asr" else "shared"
                response = self.put_provider(draft)
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_full_configuration_rejects_new_cross_scope_route_bindings(self):
        self.initial()
        before = self.store.path.read_bytes()
        for task, provider_id in (("file_asr", "text-main"), ("translation", "asr-main")):
            with self.subTest(task=task):
                body = self.body()
                body["routes"][task]["provider_id"] = provider_id
                response = self.client.post("/api/service-config", json=body)
                self.assert_secret_free(response.text)
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_unknown_sections_and_invalid_inputs_never_echo_credentials_or_write(self):
        self.initial()
        before = self.store.path.read_bytes()
        response = self.put_section("unknown", self.section_body("asr"))
        self.assertIn(response.status_code, (400, 404, 422), response.text)
        invalid = [self.provider("new", "invalid-scope", api_key=self.draft_key),
                   self.provider("new", "text", api_key={"secret": self.draft_key}),
                   self.provider("new", "text", name="bad\nname", api_key=self.draft_key),
                   self.provider("new", "text", base_url="https://user:" + self.draft_key + "@text.example")]
        for body in invalid:
            with self.subTest(body=list(body)):
                response = self.put_provider(body)
                self.assertIn(response.status_code, (400, 422), response.text)
                response = self.put_section("text", {"providers": [body], "routes": {}})
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_pending_live_reservation_blocks_provider_and_both_section_updates(self):
        self.initial()
        response = self.client.post("/api/sessions", json={})
        self.assertEqual(response.status_code, 200, response.text)
        before = self.store.path.read_bytes()
        response = self.put_provider(self.provider("asr-main", "asr", name="busy"))
        self.assertEqual(response.status_code, 409, response.text)
        for section in ("asr", "text"):
            response = self.put_section(section, self.section_body(section))
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_active_import_blocks_provider_and_both_section_updates(self):
        self.initial()
        before = self.store.path.read_bytes()
        self.app.state.imports.uploading = True
        try:
            response = self.put_provider(self.provider("asr-main", "asr", name="busy"))
            self.assertEqual(response.status_code, 409, response.text)
            for section in ("asr", "text"):
                response = self.put_section(section, self.section_body(section))
                self.assertEqual(response.status_code, 409, response.text)
        finally:
            self.app.state.imports.uploading = False
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
