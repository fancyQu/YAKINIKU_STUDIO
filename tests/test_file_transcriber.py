import asyncio
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np

from yakiniku.file_transcriber import (
    FileTranscriber, parse_json_cues, parse_srt_cues, wav_payload,
)
from yakiniku.import_audio import AudioImportManager, ImportAudioError
from yakiniku.provider import ProviderError
from yakiniku.storage import SessionStore
from test_import_audio import FakeFileTranscriber, FakeTranslator, settings, wav_bytes


class FileTranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def transcribe_response(self, response, *, response_format="verbose_json", api_format="openai",
                                  model="chosen-speech-model", prompt=""):
        requests = []

        async def handler(request):
            await request.aread()
            requests.append(request)
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FileTranscriber("private-file-key", model,
                                       base_url="https://speech.example/gateway/v1",
                                       response_format=response_format, api_format=api_format,
                                       prompt=prompt, client=client)
            result = await provider.transcribe(np.full(16000, .1, dtype=np.float32), start_ms=120000, end_ms=121000)
            await provider.close()
            self.assertFalse(client.is_closed)
        return result, requests

    async def test_real_multipart_wav_and_segment_timestamps_use_selected_model_and_format(self):
        cues, requests = await self.transcribe_response(httpx.Response(200, json={
            "text": "こんにちは。", "segments": [{"text": "こんにちは。", "start": .1, "end": .8}],
        }))
        self.assertEqual([(cue["start_ms"], cue["end_ms"]) for cue in cues], [(120100, 120800)])
        self.assertEqual(cues[0]["timestamp_source"], "api_segments")
        self.assertEqual(cues[0]["alignment"], "provider")
        request = requests[0]
        self.assertEqual(str(request.url), "https://speech.example/gateway/v1/audio/transcriptions")
        self.assertEqual(request.headers["Authorization"], "Bearer private-file-key")
        body = request.content
        for expected in (b'name="model"\r\n\r\nchosen-speech-model', b'name="language"\r\n\r\nja',
                         b'name="response_format"\r\n\r\nverbose_json',
                         b'name="timestamp_granularities[]"\r\n\r\nsegment'):
            self.assertIn(expected, body)
        start = body.index(b"RIFF")
        size = int.from_bytes(body[start + 4:start + 8], "little") + 8
        with wave.open(io.BytesIO(body[start:start + size])) as reader:
            self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getnframes()), (16000, 1, 16000))

    async def test_text_only_json_is_explicitly_approximate_without_request_format_fallback(self):
        cues, requests = await self.transcribe_response(httpx.Response(200, json={"text": "こんにちは。ありがとう。"}),
                                                       response_format="json")
        self.assertEqual(len(requests), 1)
        self.assertNotIn(b"timestamp_granularities", requests[0].content)
        self.assertTrue(all(cue["alignment"] == "approximate" and cue["timestamp_source"] == "approximate" for cue in cues))
        self.assertEqual(cues[0]["start_ms"], 120000)
        self.assertEqual(cues[-1]["end_ms"], 121000)

    async def test_srt_response_uses_exact_global_clock(self):
        cues, requests = await self.transcribe_response(httpx.Response(200, text="1\n00:00:00,120 --> 00:00:00,850\nこんにちは。\n"),
                                                       response_format="srt")
        self.assertEqual((cues[0]["start_ms"], cues[0]["end_ms"], cues[0]["timestamp_source"]),
                         (120120, 120850, "api_srt"))
        self.assertEqual(len(requests), 1)

    async def test_http_errors_do_not_retry_other_models_or_formats_or_expose_body(self):
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(400, text="private-upstream-key-and-details")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FileTranscriber("private-file-key", "selected-model", base_url="https://speech.example/v1", client=client)
            with self.assertRaises(ProviderError) as failure:
                await provider.transcribe(np.ones(16000, dtype=np.float32), start_ms=0, end_ms=1000)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private", str(failure.exception))

    async def test_wrong_selected_response_format_is_a_clear_failure(self):
        with self.assertRaisesRegex(ProviderError, "SRT"):
            await self.transcribe_response(httpx.Response(200, json={"text": "こんにちは"}), response_format="srt")

    async def test_explicit_gpt_transcribe_family_uses_documented_language_array(self):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"text": "こんにちは。"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FileTranscriber("test-key", "gpt-transcribe-2026-01-01", base_url="https://speech.example/v1",
                                       response_format="json", client=client)
            await provider.transcribe(np.ones(16000, dtype=np.float32), start_ms=0, end_ms=1000)
        self.assertIn(b'name="languages[]"\r\n\r\nja', requests[0].content)
        self.assertNotIn(b'name="language"', requests[0].content)
        self.assertIn(b"gpt-transcribe-2026-01-01", requests[0].content)

    async def test_minimax_native_multipart_uses_documented_headers_and_sentence_timestamps(self):
        cues, requests = await self.transcribe_response(httpx.Response(200, json={
            "text": "こんにちは。", "duration": 1, "n_speakers": 1,
            "segments": [{"id": 0, "speaker": "S1", "text": "こんにちは。", "start": .1, "end": .8}],
        }), api_format="minimax", model="asr-1.0", prompt="この名前は参考です。")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://speech.example/gateway/v1/speech_to_text")
        self.assertEqual(request.headers["Authorization"], "Bearer private-file-key")
        self.assertEqual(request.headers["language"], "ja")
        self.assertTrue(request.headers["content-type"].startswith("multipart/form-data; boundary="))
        body = request.content
        for expected in (b'name="model"\r\n\r\nasr-1.0',
                         b'name="response_format"\r\n\r\nverbose_json',
                         b'name="timestamp_level"\r\n\r\nsentence'):
            self.assertIn(expected, body)
        for unsupported in (b'name="language"', b'name="languages[]"', b'name="prompt"',
                            b'name="timestamp_granularities[]"'):
            self.assertNotIn(unsupported, body)
        start = body.index(b"RIFF")
        size = int.from_bytes(body[start + 4:start + 8], "little") + 8
        with wave.open(io.BytesIO(body[start:start + size])) as reader:
            self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getnframes()), (16000, 1, 16000))
        self.assertEqual([(cue["start_ms"], cue["end_ms"]) for cue in cues], [(120100, 120800)])
        self.assertEqual(cues[0]["alignment"], "provider")

    async def test_minimax_json_keeps_selected_model_and_labels_text_only_timing_as_estimated(self):
        cues, requests = await self.transcribe_response(httpx.Response(200, json={
            "text": "こんにちは。ありがとう。", "duration": 1,
        }), response_format="json", api_format="minimax", model="user-selected-asr-model")
        body = requests[0].content
        self.assertIn(b'name="model"\r\n\r\nuser-selected-asr-model', body)
        self.assertNotIn(b"timestamp_level", body)
        self.assertNotIn(b"timestamp_granularities", body)
        self.assertNotIn(b"asr-1.0", body)
        self.assertEqual(len(cues), 2)
        self.assertTrue(all(cue["timestamp_source"] == "approximate" for cue in cues))

    async def test_minimax_srt_response_preserves_global_timing(self):
        cues, requests = await self.transcribe_response(httpx.Response(200, text=
            "1\n00:00:00,100 --> 00:00:00,850\nこんにちは。\n"),
            response_format="srt", api_format="minimax", model="asr-1.0")
        self.assertEqual((cues[0]["start_ms"], cues[0]["end_ms"], cues[0]["timestamp_source"]),
                         (120100, 120850, "api_srt"))
        self.assertIn(b'name="response_format"\r\n\r\nsrt', requests[0].content)
        self.assertIn(b'name="timestamp_level"\r\n\r\nsentence', requests[0].content)

    async def test_minimax_errors_do_not_switch_protocol_or_leak_upstream_credentials(self):
        for status in (400, 401, 402, 429, 500):
            with self.subTest(status=status):
                calls = []

                async def handler(request):
                    calls.append(request)
                    return httpx.Response(status, json={"type": "error", "error": {
                        "type": "insufficient_balance_error", "message": "private-key-and-audio-details"},
                        "request_id": "private-trace"})

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    provider = FileTranscriber("private-file-key", "asr-1.0", api_format="minimax",
                                               base_url="https://speech.example", client=client)
                    with self.assertRaises(ProviderError) as failure:
                        await provider.transcribe(np.ones(16000, dtype=np.float32), start_ms=0, end_ms=1000)
                self.assertEqual(len(calls), 1)
                self.assertEqual(str(calls[0].url), "https://speech.example/speech_to_text",
                                 "An unversioned user URL must not gain /v1 automatically")
                self.assertNotIn("private", str(failure.exception))
                if status == 402:
                    self.assertIn("余额", str(failure.exception))

    async def test_minimax_rejects_audio_longer_than_documented_limit_without_uploading(self):
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"text": ""})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FileTranscriber("key", "asr-1.0", api_format="minimax",
                                       base_url="https://speech.example/v1", client=client)
            with self.assertRaisesRegex(ProviderError, "500 秒"):
                await provider.transcribe(np.zeros(501 * 16000, dtype=np.float32), start_ms=0, end_ms=501000)
        self.assertEqual(calls, [])

    def test_minimax_requires_a_model_and_known_protocol_instead_of_guessing(self):
        with self.assertRaisesRegex(ProviderError, "模型"):
            FileTranscriber("key", "", api_format="minimax", base_url="https://speech.example/v1")
        with self.assertRaisesRegex(ProviderError, "接口"):
            FileTranscriber("key", "asr-1.0", api_format="unknown", base_url="https://speech.example/v1")

    def test_word_timestamps_create_sentences_and_invalid_timestamps_are_not_estimated(self):
        cues = parse_json_cues({"words": [{"word": "こんにちは。", "start": .1, "end": .4},
                                          {"word": "ありがとう。", "start": .6, "end": .9}]}, 2000, 3000)
        self.assertEqual([(cue["start_ms"], cue["end_ms"]) for cue in cues], [(2100, 2400), (2600, 2900)])
        self.assertTrue(all(cue["timestamp_source"] == "api_words" for cue in cues))
        with self.assertRaises(ProviderError):
            parse_json_cues({"text": "日本語", "segments": [{"text": "日本語", "start": float("nan"), "end": 1}]}, 0, 1000)
        with self.assertRaises(ProviderError):
            parse_srt_cues("private unexpected output", 0, 1000)

    def test_wav_windows_stay_below_25mb_and_missing_model_is_never_invented(self):
        payload = wav_payload(np.zeros(120 * 16000, dtype=np.float32))
        self.assertEqual(len(payload), 120 * 16000 * 2 + 44)
        self.assertLess(len(payload), 25_000_000)
        with self.assertRaisesRegex(ProviderError, "模型"):
            FileTranscriber("key", "", base_url="https://speech.example/v1")


class CloudImportJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = settings(self.root)
        self.settings.save({"import_transcription_base_url": "https://speech.example/v1",
                            "import_transcription_api_key": "private-independent-speech-key"})
        self.store = SessionStore(self.root / "recordings")
        self.transcribers = []

        def factory(**options):
            provider = FakeFileTranscriber(**options)
            provider.calls = []
            original = provider.transcribe

            async def tracked(audio, **clock):
                provider.calls.append((audio.shape, clock))
                return await original(audio, **clock)

            provider.transcribe = tracked
            self.transcribers.append(provider)
            return provider

        def no_local_model(path):
            raise AssertionError("Cloud imports must not load a local model")

        self.manager = AudioImportManager(self.store, self.settings, FakeTranslator,
                                          model_loader=no_local_model, file_transcriber_factory=factory)

    def project(self, seconds=2):
        session = self.store.create("Cloud imported test")
        session.update(source_type="import", source_file="source.wav", original_filename="配信.wav",
                       import_transcription_mode="cloud", transcription_mode="cloud_file",
                       import_upload_complete=True, import_progress=0, import_transcribed_ms=0)
        self.store.file_path(session["id"], "source.wav").write_bytes(wav_bytes(seconds))
        self.store.save(session)
        return session

    async def run_project(self, session):
        self.manager.start(session)
        await self.manager.tasks[session["id"]]
        return self.store.load(session["id"])

    async def test_cloud_import_uses_independent_provider_and_never_local_tiny(self):
        session = await self.run_project(self.project())
        self.assertEqual(session["import_stage"], "completed", session)
        self.assertEqual(session["transcription_mode"], "cloud_file")
        self.assertEqual(session["transcription_model"], "fixture-file-speech-model")
        self.assertEqual(self.transcribers[0].options["api_key"], "private-independent-speech-key")
        self.assertEqual(self.transcribers[0].options["base_url"], "https://speech.example/v1")
        self.assertEqual(self.transcribers[0].options["response_format"], "verbose_json")
        self.assertNotIn("private-independent", json.dumps(session))
        self.assertTrue(all(cue["timestamp_source"] == "api_segments" for cue in session["segments"]))

    async def test_full_source_uses_120_second_windows_and_global_tail_clock(self):
        session = await self.run_project(self.project(121))
        self.assertEqual(session["import_stage"], "completed", session)
        self.assertEqual([shape for shape, _ in self.transcribers[0].calls], [(1920000,), (16000,)])
        self.assertEqual([clock for _, clock in self.transcribers[0].calls],
                         [{"start_ms": 0, "end_ms": 120000}, {"start_ms": 120000, "end_ms": 121000}])
        self.assertEqual(session["import_transcribed_ms"], 121000)
        self.assertEqual(session["segments"][-1]["start_ms"], 120500)

    async def test_cloud_failure_retries_only_after_completed_checkpoint(self):
        original = self.manager.file_transcriber_factory
        failed = False

        def factory(**options):
            provider = original(**options)
            transcribe = provider.transcribe

            async def fail_second(audio, **clock):
                nonlocal failed
                if clock["start_ms"] == 1000 and not failed:
                    failed = True
                    raise ProviderError("测试语音服务暂时不可用")
                return await transcribe(audio, **clock)

            provider.transcribe = fail_second
            return provider

        self.manager.file_transcriber_factory = factory
        with patch("yakiniku.import_audio.CLOUD_IMPORT_CHUNK_SECONDS", 1):
            failed_project = await self.run_project(self.project())
            self.assertEqual(failed_project["import_stage"], "failed")
            self.assertEqual(failed_project["import_transcribed_ms"], 1000)
            self.settings.save({"import_transcription_model": "new-user-selected-speech-model",
                                "import_transcription_response_format": "srt"})
            resumed = await self.run_project(failed_project)
        self.assertEqual(resumed["import_stage"], "completed")
        self.assertEqual([clock["start_ms"] for _, clock in self.transcribers[1].calls], [1000])
        self.assertEqual(len({cue["id"] for cue in resumed["segments"]}), 4)
        self.assertEqual(self.transcribers[1].options["model"], "new-user-selected-speech-model")
        self.assertEqual(resumed["segments"][0]["transcription_model"], "fixture-file-speech-model")
        self.assertEqual(resumed["segments"][-1]["transcription_model"], "new-user-selected-speech-model")
        self.assertEqual(resumed["segments"][-1]["transcription_response_format"], "srt")

    async def test_translation_only_retry_preserves_actual_asr_model_without_new_audio_upload(self):
        original_factory = self.manager.translator_factory

        def fail_factory(**options):
            translator = original_factory(**options)
            translator.fail = True
            return translator

        self.manager.translator_factory = fail_factory
        failed_project = await self.run_project(self.project())
        self.assertEqual(failed_project["import_stage"], "failed")
        self.assertTrue(failed_project["import_transcription_complete"])
        self.manager.translator_factory = original_factory
        self.settings.save({"import_transcription_model": "", "import_transcription_response_format": "srt"})
        resumed = await self.run_project(failed_project)
        self.assertEqual(resumed["import_stage"], "completed")
        self.assertEqual(len(self.transcribers), 1)
        self.assertEqual(resumed["transcription_model"], "fixture-file-speech-model")
        self.assertEqual(resumed["import_transcription_response_format"], "verbose_json")

    async def test_approximate_timestamp_warning_is_persisted(self):
        class TextOnly(FakeFileTranscriber):
            async def transcribe(self, audio, *, start_ms, end_ms):
                return parse_json_cues({"text": "これはテストです。"}, start_ms, end_ms)

        self.manager.file_transcriber_factory = TextOnly
        result = await self.run_project(self.project())
        self.assertTrue(result["import_timestamps_approximate"])
        self.assertEqual(result["segments"][0]["alignment"], "approximate")
        self.assertTrue(any("估算" in warning for warning in result["warnings"]))

    def test_blank_import_speech_model_cannot_fall_back_to_tiny_or_translation_model(self):
        self.settings.save({"import_transcription_model": ""})
        with self.assertRaisesRegex(ImportAudioError, "语音转写.*模型"):
            self.manager.validate_settings()


if __name__ == "__main__":
    unittest.main()
