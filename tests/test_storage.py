import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yakiniku.storage import SessionStore, SettingsStore, timestamp
from yakiniku.audio import SegmentedWaveWriter


class SettingsTests(unittest.TestCase):
    def test_fresh_settings_leave_all_service_addresses_and_keys_empty(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = SettingsStore(Path(directory))
            settings = store.load()
            for name in (
                "api_base_url", "transcription_base_url", "translation_base_url",
                "api_key", "transcription_api_key", "translation_api_key",
            ):
                self.assertEqual(settings[name], "")
            self.assertEqual(settings["transcription_mode"], "local")
            self.assertEqual(settings["transcription_model"], "tiny")
            self.assertFalse(store.public()["key_configured"])
            self.assertFalse(store.public()["configuration_ready"])

    def test_models_are_explicit_and_saved_speech_selection_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = SettingsStore(Path(directory))
            store.save({"api_base_url": "https://chosen.example/v1", "api_key": "test-key", "transcription_mode": "realtime", "transcription_model": ""})
            self.assertEqual(store.public()["transcription_model"], "")
            self.assertFalse(store.public()["configuration_ready"])
            store.save({"transcription_model": "user-chosen-speech-model"})
            self.assertTrue(SettingsStore(Path(directory)).public()["configuration_ready"])
            self.assertEqual(SettingsStore(Path(directory)).load()["transcription_model"], "user-chosen-speech-model")
            store.save({"translation_model": ""})
            self.assertFalse(store.public()["configuration_ready"])

    def test_key_is_private_and_environment_is_not_written_to_disk(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = SettingsStore(Path(directory))
            self.assertFalse(store.public()["key_configured"])
            store.save({"api_key": "private-test-key", "unrecognized": "ignored"})
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-test-key", json.dumps(store.public()))
            self.assertTrue(store.public()["key_configured"])
            with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-test-key"}):
                self.assertEqual(store.load()["api_key"], "environment-test-key")
                self.assertTrue(store.public()["key_from_environment"])
                store.save({"glossary": "配信 = 直播"})
                self.assertNotIn("environment-test-key", store.path.read_text())
            self.assertEqual(store.load()["api_key"], "private-test-key")
            self.assertNotIn("unrecognized", store.load())

    def test_symlink_settings_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "other.json"
            target.write_text('{"api_key":"secret"}')
            (root / "settings.json").symlink_to(target)
            with self.assertRaises(ValueError):
                SettingsStore(root).load()


class SessionTests(unittest.TestCase):
    def test_imported_source_files_are_listed_and_confined(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create("Imported audio")
            source = store.file_path(session["id"], "source.m4a")
            source.write_bytes(b"original-audio")
            self.assertIn("source.m4a", [item["name"] for item in store.files(session)])
            for invalid in ("source../settings.json", "source.json", "source.MP3", "source.mp3/other", "source.mp3.exe"):
                with self.assertRaises(ValueError):
                    store.file_path(session["id"], invalid)
            source.unlink()
            source.symlink_to(Path(directory) / "secret")
            with self.assertRaises(ValueError):
                store.file_path(session["id"], "source.m4a")
            self.assertNotIn("source.m4a", [item["name"] for item in store.files(session)])

    def test_import_recovery_retains_timeline_and_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create("Imported audio")
            session.update(source_type="import", status="processing", import_stage="translating",
                           import_progress=80, duration_ms=60000)
            session["segments"] = [{"id": "a", "start_ms": 1200, "end_ms": 2400,
                                     "ja": "テスト", "zh": "", "translation_status": "pending"}]
            store.save(session)
            self.assertEqual(store.recover(), 1)
            saved = store.load(session["id"])
            self.assertEqual(saved["status"], "interrupted")
            self.assertEqual(saved["import_stage"], "interrupted")
            self.assertEqual(saved["duration_ms"], 60000)
            self.assertEqual(saved["import_progress"], 80)
            self.assertEqual(saved["segments"][0]["translation_status"], "failed")
            self.assertTrue(store.file_path(session["id"], "ja.srt").is_file())
            self.assertEqual(store.recover(), 0)

    def test_exports_are_ordered_and_empty_translation_is_not_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create('配信 / "直播"')
            session["segments"] = [
                {"id": "second", "start_ms": 3661002, "end_ms": 3663004,
                 "ja": "二番です。", "zh": "这是第二句。"},
                {"id": "first", "start_ms": 1000, "end_ms": 2500,
                 "ja": "一番です。", "zh": "这是第一句。"},
                {"id": "third", "start_ms": 3664000, "end_ms": 3665000,
                 "ja": "翻訳待ち。", "zh": "", "translation_status": "failed"},
            ]
            exported = store.export(session)
            chinese = store.file_path(session["id"], "zh-CN.srt").read_text()
            self.assertIn("00:00:01,000 --> 00:00:02,500", chinese)
            self.assertIn("01:01:01,002 --> 01:01:03,004", chinese)
            self.assertLess(chinese.index("这是第一句"), chinese.index("这是第二句"))
            self.assertNotIn("翻訳待ち", chinese)
            bilingual = store.file_path(session["id"], "bilingual.srt").read_text()
            self.assertIn("这是第一句。\n一番です。", bilingual)
            self.assertIn("翻訳待ち。", bilingual)
            vtt = store.file_path(session["id"], "bilingual.vtt").read_text()
            self.assertTrue(vtt.startswith("WEBVTT\n\n"))
            self.assertIn("01:01:01.002 --> 01:01:03.004", vtt)
            self.assertEqual([item["id"] for item in session["segments"]], ["first", "second", "third"])
            self.assertTrue(all(item["url"].startswith(f"/api/sessions/{session['id']}/files/") for item in exported["files"]))
            self.assertNotIn("files", store.load(session["id"]))

    def test_clamps_invalid_times_and_sanitizes_empty_subtitle_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create("Test")
            session["segments"] = [
                {"id": "x", "start_ms": -12, "end_ms": -1, "ja": "a\r\n\r\nb\x00", "zh": "z"},
                {"id": "y", "start_ms": "nonsense", "end_ms": float("inf"), "ja": "y", "zh": ""},
            ]
            store.export(session)
            self.assertTrue(all(segment["start_ms"] == 0 and segment["end_ms"] == 1 for segment in session["segments"]))
            self.assertEqual(session["segments"][0]["ja"], "a\nb")
            self.assertEqual(timestamp(27 * 3600 * 1000 + 12), "27:00:00,012")

    def test_ids_names_and_symlinks_cannot_escape_recording_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            for invalid in ("../outside", "/tmp", "a" * 31, "A" * 32):
                with self.assertRaises(ValueError):
                    store.directory(invalid)
            session = store.create("Test")
            with self.assertRaises(ValueError):
                store.file_path(session["id"], "../settings.json")
            outside = Path(directory) / "secret.txt"
            outside.write_text("secret")
            store.file_path(session["id"], "audio.wav").symlink_to(outside)
            with self.assertRaises(ValueError):
                store.file_path(session["id"], "audio.wav")
            self.assertFalse(any(item["name"] == "audio.wav" for item in store.files(session)))

    def test_interrupted_sessions_recover_saved_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create("Test")
            store.append_segment(session, {"id": "line", "start_ms": 0, "end_ms": 1000, "ja": "テスト", "zh": "测试"})
            store.append_segment(session, {"id": "line", "zh": "测试完成"})
            store.save(session)
            self.assertEqual(len(store.load(session["id"])["segments"]), 1)
            self.assertEqual(store.recover(), 1)
            self.assertEqual(store.load(session["id"])["status"], "interrupted")
            self.assertIn("测试完成", store.file_path(session["id"], "zh-CN.srt").read_text())
            self.assertEqual(store.recover(), 0)

    def test_recovery_rebuilds_multipart_duration_from_wav_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create("Long livestream")
            writer = SegmentedWaveWriter(store.directory(session["id"]), 48000, 2, part_seconds=.1)
            writer.write(struct.pack("<hh", 111, -222) * 12000)
            writer.close()
            # Simulate metadata checkpointing before the last audio chunks.
            session["duration_ms"] = 20
            session["audio_parts"] = [{"name": "audio.wav", "start_ms": 0, "end_ms": 20}]
            store.save(session)
            self.assertEqual(store.recover(), 1)
            recovered = store.load(session["id"])
            self.assertEqual(recovered["duration_ms"], 250)
            self.assertEqual(recovered["audio_parts"], [
                {"name": "audio.wav", "start_ms": 0, "end_ms": 100},
                {"name": "audio-002.wav", "start_ms": 100, "end_ms": 200},
                {"name": "audio-003.wav", "start_ms": 200, "end_ms": 250},
            ])
            self.assertEqual(
                [item["name"] for item in store.files(session) if item["name"].endswith(".wav")],
                ["audio.wav", "audio-002.wav", "audio-003.wav"],
            )
            for invalid in ("audio-001.wav", "audio-02.wav", "audio-000.wav", "audio-../../x.wav"):
                with self.assertRaises(ValueError):
                    store.file_path(session["id"], invalid)

    def test_summary_counts_both_failed_and_unfinished_translations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create("Translation metadata")
            session["segments"] = [
                {"ja": "失敗", "zh": "", "translation_status": "failed"},
                {"ja": "待ち", "zh": "", "translation_status": "pending"},
                {"ja": "完了", "zh": "完成", "translation_status": "done"},
                {"ja": "  ", "zh": "", "translation_status": "failed"},
                {"ja": "旧データ", "zh": "  "},
            ]
            store.save(session)
            summary = store.list()[0]
            self.assertEqual(summary["segment_count"], 5)
            self.assertEqual(summary["failed_translation_count"], 3)
            self.assertNotIn("segments", summary)

    def test_preparing_and_finalizing_sessions_recover_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            identifiers = []
            for status in ("preparing", "finalizing"):
                session = store.create(status)
                identifiers.append(session["id"])
                session["status"] = status
                session["segments"] = [{
                    "id": "line", "ja": "待っています", "zh": "",
                    "start_ms": 0, "end_ms": 1000, "translation_status": "pending",
                }]
                store.save(session)
            self.assertEqual(store.recover(), 2)
            for identifier in identifiers:
                recovered = store.load(identifier)
                self.assertEqual(recovered["status"], "interrupted")
                self.assertEqual(recovered["segments"][0]["translation_status"], "failed")
            self.assertEqual(store.recover(), 0)


if __name__ == "__main__":
    unittest.main()
