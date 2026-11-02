"""Exercise browser transport through real HTTP adapters without paid requests."""

import io
import json
import os
import struct
import tempfile
import unittest
import wave
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from yakiniku.provider import ChunkedTranscriber, Translator
from yakiniku.server import create_app


class CompatiblePipelineTests(unittest.TestCase):
    origin = "http://127.0.0.1:8765"
    speech_base = "https://speech.vendor.example/gateway/audio/v1"
    translation_base = "https://translation.vendor.example/proxy/text/v1"
    speech_key = "sk-independent-speech-secret"
    translation_key = "sk-independent-translation-secret"
    shared_key = "sk-unused-shared-secret"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.http_clients = []
        self.requests = []
        self.asr_failure_status = None
        self.uploaded_wave = None

        def http_client():
            client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond), follow_redirects=True)
            self.http_clients.append(client)
            return client

        def speech_factory(**kwargs):
            return ChunkedTranscriber(client=http_client(), **kwargs)

        def translator_factory(**kwargs):
            return Translator(client=http_client(), **kwargs)

        def unexpected_realtime(**kwargs):
            raise AssertionError("Segmented mode must not open a Realtime socket")

        app = create_app(
            Path(self.temporary.name), transcriber_factory=unexpected_realtime,
            chunked_transcriber_factory=speech_factory, translator_factory=translator_factory,
        )
        self.client = TestClient(app, base_url=self.origin)
        self.client.__enter__()
        response = self.client.post("/api/settings", json={
            "api_base_url": "https://unused-shared.vendor.example/v1",
            "api_key": self.shared_key,
            "transcription_mode": "segmented",
            "transcription_base_url": self.speech_base,
            "transcription_api_key": self.speech_key,
            "transcription_model": "vendor/speech:latest",
            "translation_base_url": self.translation_base,
            "translation_api_key": self.translation_key,
            "translation_api": "chat_completions",
            "translation_model": "vendor/translator-v3",
            "segment_seconds": 3,
            "glossary": "配信 = 直播",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_keys(response.text)

    def tearDown(self):
        # Injected clients are caller-owned. Close them on the same event loop
        # after providers finish, before the TestClient portal is shut down.
        for client in self.http_clients:
            self.client.portal.call(client.aclose)
            self.assertTrue(client.is_closed)
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def assert_no_keys(self, value):
        for key in (self.speech_key, self.translation_key, self.shared_key):
            self.assertNotIn(key, value)

    def respond(self, request):
        self.requests.append(request)
        if str(request.url) == self.speech_base + "/audio/transcriptions":
            self.assertEqual(request.headers["authorization"], "Bearer " + self.speech_key)
            header = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
            message = BytesParser(policy=policy.default).parsebytes(header + request.content)
            fields = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                for part in message.iter_parts()
            }
            self.assertEqual(fields["model"], b"vendor/speech:latest")
            self.assertEqual(fields["language"], b"ja")
            self.assertEqual(fields["response_format"], b"json")
            self.assertIn("配信", fields["prompt"].decode("utf-8"))
            self.uploaded_wave = fields["file"]
            with wave.open(io.BytesIO(self.uploaded_wave), "rb") as reader:
                self.assertEqual((reader.getnchannels(), reader.getframerate(), reader.getnframes()), (1, 24000, 28800))
            if self.asr_failure_status:
                return httpx.Response(self.asr_failure_status, json={
                    "error": {"code": "invalid_api_key" if self.asr_failure_status == 401 else "unsupported_endpoint", "message": self.speech_key},
                })
            return httpx.Response(200, json={"text": "皆さん、こんばんは。"})
        if str(request.url) == self.translation_base + "/chat/completions":
            self.assertEqual(request.headers["authorization"], "Bearer " + self.translation_key)
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "vendor/translator-v3")
            self.assertFalse(payload["stream"])
            data = json.loads(payload["messages"][1]["content"])
            self.assertEqual(data["current_text"], "皆さん、こんばんは。")
            self.assertEqual(data["glossary"], "配信 = 直播")
            is_correction = "字幕校对员" in payload["messages"][0]["content"]
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop", "message": {"role": "assistant", "content":
                    "皆さん、こんばんは。" if is_correction else "大家晚上好。"},
            }]})
        raise AssertionError("Unexpected destination: " + str(request.url))

    def record(self):
        response = self.client.post("/api/sessions", json={
            "title": "第三方直播字幕测试", "sample_rate": 48000, "channels": 2,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_keys(response.text)
        session = response.json()
        url = f"ws://127.0.0.1:8765/ws/{session['id']}?token={session['token']}"
        with self.client.websocket_connect(url, headers={"origin": self.origin}) as socket:
            events = []
            for _ in range(10):
                event = socket.receive_json()
                events.append(event)
                if event.get("type") == "ready":
                    self.assertEqual(event["recognition_mode"], "segmented")
                    break
            else:
                self.fail("Missing ready event")
            # Two seconds of silence followed by one second of speech. The ASR
            # receives a 200 ms preroll; the cue must retain its original time.
            stereo = b"\x00\x00\x00\x00" * 96000 + struct.pack("<hh", 1200, -1200) * 48000
            mono = b"\x00\x00" * 48000 + struct.pack("<h", 1200) * 24000
            socket.send_bytes(b"\x00" + stereo)
            socket.send_bytes(b"\x01" + mono)
            socket.send_json({"type": "stop"})
            for _ in range(30):
                event = socket.receive_json()
                events.append(event)
                if event.get("type") == "saved":
                    saved = event["session"]
                    break
            else:
                self.fail("Missing saved event")
            self.assert_no_keys(json.dumps(events))
        return saved, events

    def assert_audio_is_preserved(self, saved):
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["duration_ms"], 3000)
        files = {item["name"]: item["url"] for item in saved["files"]}
        response = self.client.get(files["audio.wav"])
        self.assertEqual(response.status_code, 200)
        with wave.open(io.BytesIO(response.content), "rb") as reader:
            self.assertEqual((reader.getnchannels(), reader.getframerate(), reader.getnframes()), (2, 48000, 144000))
            self.assertEqual(reader.readframes(1), b"\x00\x00\x00\x00")
            reader.setpos(96000)
            self.assertEqual(reader.readframes(1), struct.pack("<hh", 1200, -1200))
        return files

    def test_real_adapters_produce_separate_audio_and_timed_chinese_subtitles(self):
        saved, events = self.record()
        files = self.assert_audio_is_preserved(saved)
        self.assertEqual(len(saved["segments"]), 1)
        self.assertEqual((saved["segments"][0]["start_ms"], saved["segments"][0]["end_ms"]), (1800, 3000))
        self.assertTrue(any(event["type"] == "translation" for event in events))
        chinese = self.client.get(files["zh-CN.srt"]).content.decode("utf-8")
        bilingual = self.client.get(files["bilingual.srt"]).content.decode("utf-8")
        self.assertIn("00:00:01,800 --> 00:00:03,000", chinese)
        self.assertIn("大家晚上好。", chinese)
        self.assertIn("皆さん、こんばんは。", bilingual)
        self.assertEqual([str(request.url) for request in self.requests], [
            self.speech_base + "/audio/transcriptions", self.translation_base + "/chat/completions",
            self.translation_base + "/chat/completions",
        ])
        self.assertEqual(saved["segments"][0]["correction_status"], "done")
        stages = [event for event in events if event["type"] in ("correction", "translation")]
        self.assertEqual([event["type"] for event in stages], ["correction", "translation"])
        self.assertEqual(stages[0]["segment"]["translation_status"], "pending")
        self.assertEqual(stages[0]["segment"]["text_stage"], "translating")
        self.assertEqual(stages[0]["segment"]["zh"], "")
        self.assertEqual(stages[1]["segment"]["text_stage"], "completed")
        for endpoint in ("/api/settings", "/api/sessions", files["session.json"]):
            self.assert_no_keys(self.client.get(endpoint).text)
        self.assertTrue(all(not client.is_closed for client in self.http_clients))

    def test_asr_auth_or_endpoint_error_keeps_wav_and_never_falls_back(self):
        for status in (401, 404):
            with self.subTest(status=status):
                self.requests.clear()
                self.asr_failure_status = status
                saved, events = self.record()
                files = self.assert_audio_is_preserved(saved)
                self.assertEqual(saved["segments"], [])
                self.assertTrue(saved["warnings"])
                self.assertTrue(any(event["type"] == "warning" for event in events))
                self.assertEqual(self.client.get(files["ja.srt"]).content, b"")
                self.assertEqual(self.client.get(files["zh-CN.srt"]).content, b"")
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(str(self.requests[0].url), self.speech_base + "/audio/transcriptions")
                self.assertNotEqual(self.requests[0].url.host, "api.openai.com")


if __name__ == "__main__":
    unittest.main()
