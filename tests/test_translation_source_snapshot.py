"""A mirror source may change provenance, never the session's model contents."""
import copy
import unittest

from yakiniku.live_pipeline import validate_translation_snapshot
from yakiniku.provider import ProviderError


class TranslationSourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.hashes = {"config.json": "a" * 64,
                       "tokenizer_config.json": "b" * 64,
                       "tokenizer.json": "c" * 64,
                       "model.safetensors": "d" * 64,
                       "vocab.json": "e" * 64}
        self.expected = {"path": "/models/translation/qwen", "version": "mlx-lm-test",
                         "source": {"revision": "old-source-revision", "repo_id": "old/qwen",
                                    "weights_sha256": self.hashes["model.safetensors"],
                                    "files_sha256": self.hashes.copy()}}
        self.session = {"live_snapshot": {"translation_model_source": self.expected}}
        self.actual = copy.deepcopy(self.expected)

    def test_identical_verified_files_can_move_to_another_repository_revision(self):
        self.actual["source"].update(revision="modelscope-revision", repo_id="modelscope/qwen")
        validate_translation_snapshot(self.session, self.actual)

    def test_tokenizer_or_other_file_changes_are_rejected_even_with_same_weights(self):
        for filename in ("tokenizer.json", "tokenizer_config.json", "vocab.json", "config.json"):
            with self.subTest(filename=filename):
                actual = copy.deepcopy(self.actual)
                actual["source"]["files_sha256"][filename] = "f" * 64
                with self.assertRaises(ProviderError):
                    validate_translation_snapshot(self.session, actual)

    def test_changed_weights_are_rejected_in_both_digest_fields(self):
        for field in ("weights_sha256", "files_sha256"):
            with self.subTest(field=field):
                actual = copy.deepcopy(self.actual)
                if field == "weights_sha256":
                    actual["source"][field] = "f" * 64
                else:
                    actual["source"][field]["model.safetensors"] = "f" * 64
                with self.assertRaises(ProviderError):
                    validate_translation_snapshot(self.session, actual)

    def test_legacy_snapshot_without_manifest_still_requires_original_revision(self):
        self.expected["source"].pop("files_sha256")
        validate_translation_snapshot(self.session, self.actual)
        self.actual["source"]["revision"] = "modelscope-revision"
        with self.assertRaises(ProviderError):
            validate_translation_snapshot(self.session, self.actual)

    def test_empty_partial_or_invalid_manifests_do_not_relax_revision(self):
        for hashes in ({}, {"model.safetensors": "d" * 64},
                       {**self.hashes, "tokenizer.json": "not-a-sha256"}):
            with self.subTest(hashes=hashes):
                self.expected["source"]["files_sha256"] = hashes.copy()
                self.actual["source"]["files_sha256"] = hashes.copy()
                self.actual["source"]["revision"] = "modelscope-revision"
                with self.assertRaises(ProviderError):
                    validate_translation_snapshot(self.session, self.actual)

    def test_missing_current_manifest_cannot_prove_identical_content(self):
        self.actual["source"].pop("files_sha256")
        with self.assertRaises(ProviderError):
            validate_translation_snapshot(self.session, self.actual)

    def test_a_removed_or_added_file_is_a_content_change(self):
        for add in (False, True):
            with self.subTest(add=add):
                actual = copy.deepcopy(self.actual)
                if add:
                    actual["source"]["files_sha256"]["added_tokens.json"] = "f" * 64
                else:
                    actual["source"]["files_sha256"].pop("vocab.json")
                with self.assertRaises(ProviderError):
                    validate_translation_snapshot(self.session, actual)

    def test_identical_content_cannot_change_runtime_path_or_version(self):
        for field in ("path", "version"):
            with self.subTest(field=field):
                actual = copy.deepcopy(self.actual)
                actual[field] += "-changed"
                with self.assertRaises(ProviderError):
                    validate_translation_snapshot(self.session, actual)


if __name__ == "__main__":
    unittest.main()
