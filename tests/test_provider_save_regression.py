"""Module edits must not mutate a legacy connection used by the other module."""
import os
import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yakiniku.config import effective_provider
from yakiniku.service_config import (ConfigurationConflict, ConnectionInput, SectionConfigInput,
                                     private_configuration, public_configuration, save_connection, save_section)
from yakiniku.storage import SettingsStore


class LegacySharedSaveRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.store = SettingsStore(Path(temporary.name))
        self.store.save({"api_base_url": "https://shared.example/v1", "api_key": "fixture-shared-secret",
                         "translation_model": "gpt-fixture", "correction_model": "gpt-fixture",
                         "translation_api": "chat_completions", "import_transcription_model": "fixture-asr"})

    def test_asr_edit_cannot_redirect_legacy_gpt_connection(self):
        original = self.store.load()
        public = public_configuration(self.store)
        provider = next(p for p in public["providers"] if p["id"] == public["routes"]["file_asr"]["provider_id"])
        draft = {**provider, "base_url": "https://speech.example/v1", "api_key": "fixture-speech-secret",
                 "speech_api": "minimax"}
        body = SectionConfigInput.model_validate({"providers": [draft], "routes": {
            "file_asr": {**public["routes"]["file_asr"], "model": "asr-1.0"}}})
        try:
            save_section(self.store, "asr", body)
        except ValueError:
            # A clear safe refusal is preferable to silently rewriting GPT.
            self.assertEqual(effective_provider(self.store.load(), "translation"),
                             effective_provider(original, "translation"))
            return
        after = self.store.load()
        self.assertEqual(effective_provider(after, "import_transcription")["base_url"], "https://speech.example/v1")
        for role in ("translation", "correction"):
            self.assertEqual(effective_provider(after, role), effective_provider(original, role),
                             "ASR save redirected a shared GPT connection")

    def test_text_edit_cannot_redirect_legacy_asr_connection(self):
        original = self.store.load()
        public = public_configuration(self.store)
        provider = next(p for p in public["providers"] if p["id"] == public["routes"]["translation"]["provider_id"])
        draft = {**provider, "base_url": "https://new-text.example/v1", "api_key": "fixture-text-secret"}
        body = SectionConfigInput.model_validate({"providers": [draft], "routes": {
            task: public["routes"][task] for task in ("correction", "translation")}})
        try:
            save_section(self.store, "text", body)
        except ValueError:
            self.assertEqual(effective_provider(self.store.load(), "import_transcription"),
                             effective_provider(original, "import_transcription"))
            return
        after = self.store.load()
        self.assertEqual(effective_provider(after, "translation")["base_url"], "https://new-text.example/v1")
        self.assertEqual(effective_provider(after, "import_transcription"),
                         effective_provider(original, "import_transcription"),
                         "Text save redirected a shared ASR connection")

    def section_body(self, public, section):
        tasks = ("file_asr",) if section == "asr" else ("correction", "translation")
        return {"providers": [copy.deepcopy(p) for p in public["providers"] if p["scope"] == section],
                "routes": {task: copy.deepcopy(public["routes"][task]) for task in tasks},
                "expected_revision": public["section_revisions"][section]}

    def test_read_split_is_stable_and_retains_text_identity_without_writing_settings(self):
        before = self.store.path.read_bytes()
        first, second = public_configuration(self.store), public_configuration(self.store)
        self.assertEqual(first, second)
        self.assertEqual(self.store.path.read_bytes(), before)
        text_id = first["routes"]["translation"]["provider_id"]
        speech_id = first["routes"]["file_asr"]["provider_id"]
        self.assertEqual(text_id, "existing-1")
        self.assertEqual(speech_id, "existing-1-asr")
        self.assertEqual(first["routes"]["correction"]["provider_id"], text_id)
        self.assertEqual({p["id"]: p["scope"] for p in first["providers"]},
                         {text_id: "text", speech_id: "asr"})
        profiles, _ = private_configuration(self.store)
        self.assertTrue(all(p["api_key"] == "fixture-shared-secret" for p in profiles))
        self.assertNotIn("fixture-shared-secret", json.dumps(first))
        body = self.section_body(first, "asr")
        save_section(self.store, "asr", SectionConfigInput.model_validate(body))
        self.assertTrue(self.store._stored()["service_profiles_enabled"])
        persisted = self.store._stored()["service_profiles"]
        self.assertEqual({p["id"]: p["scope"] for p in persisted}, {text_id: "text", speech_id: "asr"})
        self.assertTrue(all(p["api_key"] == "fixture-shared-secret" for p in persisted))
        self.assertTrue(all("section" not in p and "expected_revision" not in p for p in persisted))

    def test_saved_shared_split_handles_long_ids_and_collisions_without_get_writes(self):
        identifier = "x" * 80
        collision = "x" * 76 + "-asr"
        self.store.save({"service_profiles_enabled": True, "service_profiles": [
            {"id": identifier, "name": "Legacy", "scope": "shared", "api_key": "fixture-shared-secret",
             "base_url": "https://shared.example/v1", "speech_api": "openai", "text_api": "chat_completions"},
            {"id": collision, "name": "Existing speech", "scope": "asr", "api_key": "fixture-other-secret",
             "base_url": "https://other.example/v1", "speech_api": "minimax"},
        ], "task_routes": {
            "file_asr": {"provider_id": identifier, "model": "fixture-asr"},
            "correction": {"provider_id": identifier, "model": "gpt-fixture"},
            "translation": {"provider_id": identifier, "model": "gpt-fixture"},
        }})
        before = self.store.path.read_bytes()
        public = public_configuration(self.store)
        generated = public["routes"]["file_asr"]["provider_id"]
        self.assertEqual(generated, "x" * 74 + "-asr-2")
        self.assertEqual(public["routes"]["translation"]["provider_id"], identifier)
        self.assertEqual(len({p["id"] for p in public["providers"]}), 3)
        self.assertEqual(public_configuration(self.store), public)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_environment_key_reference_is_split_without_persisting_secret(self):
        self.store.save({"api_key": ""})
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-environment-secret"}):
            public = public_configuration(self.store)
            self.assertTrue(all(p["key_from_environment"] for p in public["providers"]))
            save_section(self.store, "asr", SectionConfigInput.model_validate(self.section_body(public, "asr")))
            self.assertNotIn("fixture-environment-secret", self.store.path.read_text())
            self.assertNotIn("fixture-environment-secret", json.dumps(public))
            self.assertTrue(all(p["use_environment_key"] for p in self.store._stored()["service_profiles"]))

    def test_stale_shared_and_cross_module_provider_requests_are_rejected_without_writes(self):
        public = public_configuration(self.store)
        text = next(p for p in public["providers"] if p["scope"] == "text")
        before = self.store.path.read_bytes()
        for section, scope in (("asr", "asr"), ("text", "shared")):
            with self.subTest(section=section):
                stale = {**text, "section": section, "scope": scope, "base_url": "https://wrong.example/v1",
                         "api_key": "fixture-replacement-secret"}
                with self.assertRaises(ValueError):
                    save_connection(self.store, ConnectionInput.model_validate(stale))
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_section_revisions_reject_same_module_stale_drafts_but_allow_other_module_save(self):
        first = public_configuration(self.store)
        asr_body, text_body = self.section_body(first, "asr"), self.section_body(first, "text")
        asr_body["providers"][0]["name"] = "New ASR name"
        save_section(self.store, "asr", SectionConfigInput.model_validate(asr_body))
        current = public_configuration(self.store)
        self.assertNotEqual(current["section_revisions"]["asr"], first["section_revisions"]["asr"])
        self.assertEqual(current["section_revisions"]["text"], first["section_revisions"]["text"])
        before = self.store.path.read_bytes()
        with self.assertRaises(ConfigurationConflict):
            save_section(self.store, "asr", SectionConfigInput.model_validate(asr_body))
        self.assertEqual(self.store.path.read_bytes(), before)
        text_body["providers"][0]["name"] = "New GPT name"
        save_section(self.store, "text", SectionConfigInput.model_validate(text_body))
        names = {p["scope"]: p["name"] for p in public_configuration(self.store)["providers"]}
        self.assertEqual(names, {"asr": "New ASR name", "text": "New GPT name"})

    def test_provider_revision_protects_against_a_stale_same_module_page(self):
        first = public_configuration(self.store)
        speech = next(p for p in first["providers"] if p["scope"] == "asr")
        draft = {**speech, "section": "asr", "expected_revision": first["section_revisions"]["asr"],
                 "name": "Saved in another page"}
        save_connection(self.store, ConnectionInput.model_validate(draft))
        before = self.store.path.read_bytes()
        with self.assertRaises(ConfigurationConflict):
            save_connection(self.store, ConnectionInput.model_validate({**draft, "name": "Stale overwrite"}))
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
