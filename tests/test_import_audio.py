import asyncio
import io
import json
import math
import os
import struct
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient

from yakiniku.import_audio import (
    AudioImportManager, ImportAudioError, decode_audio, import_filename,
    probe_audio, recognize_chunk, transcription_chunks,
)
from yakiniku.server import create_app
from yakiniku.storage import SessionStore, SettingsStore


def wav_bytes(seconds=1, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        tone = b"".join(struct.pack("<h", round(6000 * math.sin(index * .2))) for index in range(rate))
        if channels == 2:
            tone = b"".join(tone[index:index + 2] * 2 for index in range(0, len(tone), 2))
        writer.writeframes(tone * seconds)
    return output.getvalue()


class FakeModel:
    def __init__(self, fail_call=None):
        self.calls = []
        self.fail_call = fail_call

    def transcribe(self, audio, **options):
        self.calls.append((audio.shape, options))
        if len(self.calls) == self.fail_call:
            raise RuntimeError("private model details")
        words = [SimpleNamespace(word="こんにちは。", start=.1, end=.4),
                 SimpleNamespace(word="ありがとう。", start=.5, end=.8)]
        return iter([SimpleNamespace(text="こんにちは。ありがとう。", start=.1, end=.8, words=words)]), None


class FakeTranslator:
    def __init__(self, **options):
        self.options = options
        self.active = 0
        self.maximum = 0
        self.calls = []
        self.closed = False
        self.fail = False

    async def translate_and_correct(self, text, context=None):
        self.calls.append(text)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(.005)
            if self.fail and text.startswith("ありがとう"):
                raise RuntimeError("private translation details")
            return {"ja": text, "zh": "中文：" + text}
        finally:
            self.active -= 1

    async def close(self):
        self.closed = True


class FakeFileTranscriber:
    def __init__(self, **options):
        self.options = options

    async def transcribe(self, audio, *, start_ms, end_ms):
        return [{"ja": "こんにちは。", "start_ms": start_ms + 100, "end_ms": min(end_ms, start_ms + 400),
                 "timestamp_source": "api_segments", "alignment": "provider"},
                {"ja": "ありがとう。", "start_ms": start_ms + 500, "end_ms": min(end_ms, start_ms + 800),
                 "timestamp_source": "api_segments", "alignment": "provider"}]

    async def close(self):
        pass


def settings(root):
    store = SettingsStore(root)
    store.save({"transcription_mode": "local", "transcription_model": "tiny",
                "api_base_url": "https://example.test/v1", "api_key": "private-import-key",
                "import_transcription_model": "fixture-file-speech-model",
                "translation_model": "test-text-model", "glossary": "星街すいせい"})
    return store


class AudioDecodeTests(unittest.TestCase):
    def test_actual_wav_decodes_to_stereo_preview_and_bounded_recognition_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            original = wav_bytes(32)
            source.write_bytes(original)
            self.assertEqual(probe_audio(source)["source_duration_ms"], 32000)
            result = decode_audio(source, root, {}, threading.Event())
            self.assertEqual(result["duration_ms"], 32000)
            with wave.open(str(root / "audio.wav")) as reader:
                self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getnframes()),
                                 (48000, 2, 48000 * 32))
            chunks = list(transcription_chunks([root / "audio.wav"]))
            self.assertEqual([(chunk["start_ms"], chunk["end_ms"], chunk["audio"].shape)
                              for chunk in chunks], [(0, 30000, (480000,)), (30000, 32000, (32000,))])
            resumed = list(transcription_chunks([root / "audio.wav"], resume_ms=30000))
            self.assertEqual(resumed[0]["start_ms"], 30000)
            self.assertEqual(source.read_bytes(), original)

    def test_invalid_or_disguised_playlist_is_rejected_without_following_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp3"
            source.write_text("#EXTM3U\nhttps://example.test/private.mp3\n")
            with self.assertRaises(ImportAudioError) as failure:
                probe_audio(source)
            self.assertNotIn("https://", str(failure.exception))

    def test_filename_is_normalized_and_unsupported_extension_is_rejected(self):
        self.assertEqual(import_filename(quote("../配信音声.M4A")), ("配信音声.M4A", "source.m4a"))
        with self.assertRaises(ImportAudioError):
            import_filename("source.m3u8")

    def test_sentence_timestamps_follow_window_offset_and_silence_has_no_cues(self):
        import numpy as np
        model = FakeModel()
        cues = recognize_chunk(model, {"start_ms": 30000, "end_ms": 31000,
                                       "audio": np.full(16000, .1, dtype=np.float32)}, "人名")
        self.assertEqual([(cue["start_ms"], cue["end_ms"]) for cue in cues], [(30100, 30400), (30500, 30800)])
        self.assertEqual(model.calls[0][1]["initial_prompt"], "人名")
        self.assertEqual(model.calls[0][1]["language"], "ja")
        self.assertEqual(recognize_chunk(model, {"start_ms": 0, "end_ms": 1000,
                                                 "audio": np.zeros(16000, dtype=np.float32)}), [])
        self.assertEqual(len(model.calls), 1)


class AudioImportJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = settings(self.root)
        self.store = SessionStore(self.root / "recordings")
        self.model = FakeModel()
        self.translators = []

        def translator_factory(**options):
            translator = FakeTranslator(**options)
            self.translators.append(translator)
            return translator

        self.manager = AudioImportManager(self.store, self.settings, translator_factory,
                                          model_loader=lambda path: self.model)

    def new_import(self, seconds=1):
        session = self.store.create("Imported test")
        session.update(source_type="import", source_file="source.wav", original_filename="配信.wav",
                       import_upload_complete=True, import_progress=0, import_transcribed_ms=0)
        self.store.file_path(session["id"], "source.wav").write_bytes(wav_bytes(seconds))
        self.store.save(session)
        return session

    async def run_job(self, session):
        self.manager.start(session)
        await self.manager.tasks[session["id"]]
        return self.store.load(session["id"])

    async def test_complete_import_keeps_original_and_saves_corrected_bilingual_subtitles(self):
        session = self.new_import(32)
        result = await self.run_job(session)
        self.assertEqual((result["status"], result["import_stage"], result["import_progress"]),
                         ("completed", "completed", 100))
        self.assertEqual(len(result["segments"]), 4)
        self.assertTrue(all(cue["translation_status"] == "done" and cue["ja_raw"] == cue["ja"]
                            for cue in result["segments"]))
        self.assertEqual(self.translators[0].maximum, 3)
        self.assertTrue(self.translators[0].closed)
        names = {item["name"] for item in self.store.files(result)}
        self.assertTrue({"source.wav", "audio.wav", "ja.srt", "zh-CN.srt", "bilingual.srt"} <= names)
        text = self.store.file_path(session["id"], "bilingual.srt").read_text()
        self.assertIn("00:00:30,100 --> 00:00:30,400", text)
        self.assertNotIn("private-import-key", json.dumps(result))

    async def test_recognition_failure_resumes_after_saved_chunk_without_duplicates(self):
        session = self.new_import(32)
        self.model.fail_call = 2
        failed = await self.run_job(session)
        self.assertEqual(failed["import_stage"], "failed")
        self.assertEqual(failed["import_transcribed_ms"], 30000)
        self.assertEqual(len(failed["segments"]), 2)
        self.assertNotIn("private model", str(failed))
        self.model.fail_call = None
        resumed = await self.run_job(failed)
        self.assertEqual(resumed["import_stage"], "completed")
        self.assertEqual(len(self.model.calls), 3)
        self.assertEqual(len({cue["id"] for cue in resumed["segments"]}), 4)

    async def test_translation_retry_only_retries_missing_cues_and_uses_current_settings(self):
        session = self.new_import()
        original_factory = self.manager.translator_factory

        def failing_factory(**options):
            translator = original_factory(**options)
            translator.fail = True
            return translator

        self.manager.translator_factory = failing_factory
        failed = await self.run_job(session)
        self.assertEqual(failed["import_stage"], "failed")
        self.assertEqual([cue["translation_status"] for cue in failed["segments"]], ["done", "failed"])
        self.manager.translator_factory = original_factory
        self.settings.save({"translation_model": "new-current-model"})
        resumed = await self.run_job(failed)
        self.assertEqual(resumed["import_stage"], "completed")
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.translators[-1].calls, ["ありがとう。"])
        self.assertEqual(self.translators[-1].options["model"], "new-current-model")

    async def test_shutdown_keeps_original_and_marks_interrupted(self):
        entered = asyncio.Event()

        class SlowTranslator(FakeTranslator):
            async def translate_and_correct(self, text, context=None):
                entered.set()
                await asyncio.Event().wait()

        self.manager.translator_factory = SlowTranslator
        session = self.new_import()
        self.manager.start(session)
        await asyncio.wait_for(entered.wait(), 2)
        await self.manager.shutdown()
        result = self.store.load(session["id"])
        self.assertEqual(result["import_stage"], "interrupted")
        self.assertTrue(self.store.file_path(session["id"], "source.wav").exists())
        self.assertEqual(len(result["segments"]), 2)


class AudioImportApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        settings(self.root)
        self.app = create_app(self.root, translator_factory=FakeTranslator,
                              import_model_loader=lambda path: FakeModel(),
                              import_file_transcriber_factory=FakeFileTranscriber,
                              live_model_runtime=type("ReadyRuntime", (), {
                                  "status": lambda self, **kw: {"state": "ready"}})())
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def upload(self, data=None, filename="配信.wav"):
        return self.client.post("/api/imports", content=data if data is not None else wav_bytes(),
                                headers={"Content-Type": "application/octet-stream",
                                         "X-Filename": quote(filename), "X-Title": quote("导入标题")})

    def wait_result(self, identifier):
        for _ in range(200):
            result = self.client.get("/api/imports/" + identifier).json()
            if result.get("import_stage") in ("completed", "failed", "interrupted"):
                return result
            time.sleep(.01)
        self.fail("Import did not finish")

    def test_upload_returns_project_then_progress_detail_and_separate_downloads(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.text)
        identifier = response.json()["id"]
        result = self.wait_result(identifier)
        self.assertEqual(result["import_stage"], "completed", result)
        self.assertEqual(result["title"], "导入标题")
        self.assertEqual(result["original_filename"], "配信.wav")
        self.assertTrue(result["import_upload_complete"])
        self.assertEqual(self.client.get("/api/sessions/" + identifier).json()["segments"], result["segments"])
        files = {item["name"]: item["url"] for item in result["files"]}
        self.assertEqual(self.client.get(files["source.wav"]).content, wav_bytes())
        preview_range = self.client.get(files["audio.wav"], headers={"Range": "bytes=0-43"})
        self.assertEqual(preview_range.status_code, 206)
        self.assertEqual(len(preview_range.content), 44)
        self.assertTrue(preview_range.content.startswith(b"RIFF"))
        self.assertIn("中文", self.client.get(files["zh-CN.srt"]).text)
        self.assertNotIn("private-import-key", json.dumps(result))
        self.assertTrue(self.client.get("/api/health").json()["import_supported"])

    def test_invalid_audio_empty_upload_and_oversize_are_clear_errors(self):
        self.assertEqual(self.upload(b"not an audio file").status_code, 400)
        self.assertEqual(self.upload(b"").status_code, 400)
        self.assertEqual(self.upload(b"audio", filename="unknown.txt").status_code, 400)
        with patch("yakiniku.server.MAX_IMPORT_BYTES", 8):
            self.assertEqual(self.upload(b"123456789").status_code, 413)

    def test_upload_is_blocked_while_live_reservation_exists(self):
        live = self.client.post("/api/sessions", json={})
        self.assertEqual(live.status_code, 200, live.text)
        self.assertEqual(self.upload().status_code, 409)

    def test_running_import_blocks_live_settings_and_other_writers(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingTranslator(FakeTranslator):
            async def translate_and_correct(self, text, context=None):
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(.005)
                return {"ja": text, "zh": "中文"}

        self.app.state.imports.translator_factory = BlockingTranslator
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.text)
        identifier = response.json()["id"]
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.client.get("/api/health").json()["import_active"])
            self.assertEqual(self.client.post("/api/settings", json={"glossary": "changed"}).status_code, 409)
            self.assertEqual(self.client.post("/api/sessions", json={}).status_code, 409)
            self.assertEqual(self.client.post(f"/api/imports/{identifier}/retry").status_code, 409)
            self.assertEqual(self.client.post(f"/api/sessions/{identifier}/retry-translations").status_code, 409)
            self.assertEqual(self.upload().status_code, 409)
        finally:
            release.set()
        self.assertEqual(self.wait_result(identifier)["import_stage"], "completed")

    def test_expired_live_reservations_do_not_block_import_or_settings(self):
        base = time.monotonic()
        with patch("yakiniku.server.time.monotonic", return_value=base):
            live = self.client.post("/api/sessions", json={})
        self.assertEqual(live.status_code, 200, live.text)
        with patch("yakiniku.server.time.monotonic", return_value=base + 61):
            saved = self.client.post("/api/settings", json={"glossary": "人名"})
        self.assertEqual(saved.status_code, 200, saved.text)
        with patch("yakiniku.server.time.monotonic", return_value=base):
            live = self.client.post("/api/sessions", json={})
        self.assertEqual(live.status_code, 200, live.text)
        with patch("yakiniku.server.time.monotonic", return_value=base + 61):
            uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 202, uploaded.text)

    def test_missing_and_non_import_projects_are_not_import_details(self):
        self.assertEqual(self.client.get("/api/imports/" + "0" * 32).status_code, 404)
        self.assertEqual(self.client.post("/api/imports/" + "0" * 32 + "/retry").status_code, 404)
        self.assertEqual(self.client.get("/api/sessions/not-valid").status_code, 404)


if __name__ == "__main__":
    unittest.main()
