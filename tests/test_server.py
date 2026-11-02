import asyncio
import io
import json
import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from yakiniku.provider import ProviderError
from yakiniku.server import LiveSession, create_app
from yakiniku.storage import DEFAULT_SETTINGS, SessionStore


class FakeTranscriber:
    def __init__(self, api_key, model, prompt, on_event, **options):
        self.on_event = on_event
        self.appended = bytearray()
        self.commits = []
        self.pending = []
        self.closed = False
        self.failed = False
        self.api_key = api_key
        self.model = model
        self.options = options

    async def connect(self):
        await self.on_event({"type": "connection", "status": "connected"})

    async def append(self, pcm):
        self.appended.extend(pcm)

    async def commit(self, start_ms, end_ms):
        self.commits.append((start_ms, end_ms))
        identifier = f"fake-{id(self)}-item-{len(self.commits)}"

        async def complete():
            await asyncio.sleep(0.001)
            await self.on_event({
                "type": "transcript", "id": identifier,
                "start_ms": start_ms, "end_ms": end_ms, "ja": "これはテストです。",
            })

        self.pending.append(asyncio.create_task(complete()))

    async def finish(self, timeout=20):
        if self.pending:
            await asyncio.wait_for(asyncio.gather(*self.pending), timeout)
        return True

    async def close(self):
        self.closed = True


class FakeTranslator:
    def __init__(self, api_key, model, glossary, fail=False, **options):
        self.fail = fail
        self.closed = False
        self.api_key = api_key
        self.model = model
        self.options = options

    async def translate(self, text, context=None):
        await asyncio.sleep(0.001)
        if self.fail:
            raise ProviderError("测试用翻译失败")
        return "这是测试。"

    async def close(self):
        self.closed = True


class ServerTests(unittest.TestCase):
    origin = "http://127.0.0.1:8765"
    secret = "sk-private-test-secret"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.transcribers = []
        self.translators = []
        self.translation_fails = False

        def transcriber_factory(**kwargs):
            provider = FakeTranscriber(**kwargs)
            self.transcribers.append(provider)
            return provider

        def translator_factory(*args, **kwargs):
            provider = FakeTranslator(*args, fail=self.translation_fails, **kwargs)
            self.translators.append(provider)
            return provider

        self.app = create_app(
            Path(self.temporary.name), transcriber_factory=transcriber_factory,
            translator_factory=translator_factory,
        )
        self.client = TestClient(self.app, base_url=self.origin)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def test_ui_navigation_never_returns_a_stale_html_page(self):
        for path in ("/", "/static/import.html", "/static/providers.html?section=asr", "/static/preview.html"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIn("/static/navigation.js?v=ui-navigation-1", response.text)
        for path in ("/static/navigation.js?v=ui-navigation-1", "/static/navigation.css?v=ui-navigation-1"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-cache")
            cached = self.client.get(path, headers={"If-None-Match": response.headers["etag"]})
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.headers["cache-control"], "no-cache")
        self.assertEqual(self.client.get("/api/health").headers["cache-control"], "no-store")

    def configure(self):
        response = self.client.post("/api/settings", json={
            "api_key": self.secret, "api_base_url": "https://api.openai.com/v1",
            "transcription_mode": "realtime",
            "transcription_model": "fixture-speech-model",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["key_configured"])
        self.assertNotIn(self.secret, response.text)

    def create_session(self):
        response = self.client.post("/api/sessions", json={"title": "字幕测试", "sample_rate": 48000, "channels": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(self.secret, response.text)
        return response.json()

    def receive_until(self, socket, kind):
        events = []
        for _ in range(30):
            event = socket.receive_json()
            events.append(event)
            if event.get("type") == kind:
                return event, events
        self.fail("Expected websocket event " + kind)

    def record_one_tail(self):
        session = self.create_session()
        with self.client.websocket_connect(
            "ws://127.0.0.1:8765/ws/" + session["id"] + "?token=" + session["token"], headers={"origin": self.origin},
        ) as socket:
            ready, _ = self.receive_until(socket, "ready")
            self.assertEqual(ready["mode"], "live")
            stereo = struct.pack("<hh", 1234, -5678) * 57600  # 1.2 s at 48 kHz.
            mono = struct.pack("<h", 1200) * 28800  # Same time at 24 kHz.
            socket.send_bytes(b"\x00" + stereo)
            socket.send_bytes(b"\x01" + mono)
            socket.send_json({"type": "stop"})
            saved, events = self.receive_until(socket, "saved")
            self.assertNotIn(self.secret, json.dumps(events))
        return session, saved["session"], events

    def test_recording_tail_exports_separate_wav_and_utf8_subtitles(self):
        self.configure()
        original, saved, events = self.record_one_tail()
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["duration_ms"], 1200)
        self.assertEqual(self.transcribers[0].commits, [(0, 1200)])
        self.assertEqual(len(self.transcribers[0].appended), 28800 * 2)
        self.assertTrue(self.transcribers[0].closed)
        self.assertTrue(self.translators[0].closed)
        self.assertTrue(any(event["type"] == "translation" for event in events))
        files = {item["name"]: item["url"] for item in saved["files"]}
        self.assertTrue({"audio.wav", "ja.srt", "zh-CN.srt", "bilingual.srt", "bilingual.vtt", "transcript.txt"}.issubset(files))
        audio = self.client.get(files["audio.wav"])
        self.assertEqual(audio.status_code, 200)
        with wave.open(io.BytesIO(audio.content), "rb") as reader:
            self.assertEqual((reader.getnchannels(), reader.getframerate(), reader.getnframes()), (2, 48000, 57600))
            self.assertEqual(reader.readframes(1), struct.pack("<hh", 1234, -5678))
        chinese = self.client.get(files["zh-CN.srt"]).content.decode("utf-8")
        bilingual = self.client.get(files["bilingual.srt"]).content.decode("utf-8")
        self.assertIn("00:00:00,000 --> 00:00:01,200", chinese)
        self.assertIn("这是测试。", chinese)
        self.assertIn("これはテストです。", bilingual)
        self.assertIn("这是测试。", bilingual)
        for url in ("/api/settings", "/api/sessions", files["session.json"]):
            response = self.client.get(url)
            self.assertNotIn(self.secret, response.text)
        summaries = self.client.get("/api/sessions").json()
        self.assertEqual(summaries[0]["id"], original["id"])
        self.assertEqual(summaries[0]["segment_count"], 1)
        self.assertEqual(self.client.get("/api/health").json()["active"], False)

    def test_translation_failure_preserves_audio_and_japanese_and_can_retry(self):
        self.configure()
        self.translation_fails = True
        original, saved, _ = self.record_one_tail()
        self.assertEqual(saved["status"], "completed")
        segment = saved["segments"][0]
        self.assertEqual(segment["ja"], "これはテストです。")
        self.assertEqual(segment["zh"], "")
        self.assertEqual(segment["translation_status"], "failed")
        files = {item["name"]: item["url"] for item in saved["files"]}
        self.assertGreater(len(self.client.get(files["audio.wav"]).content), 44)
        self.assertIn("これはテストです。", self.client.get(files["ja.srt"]).content.decode("utf-8"))
        self.assertEqual(self.client.get(files["zh-CN.srt"]).content, b"")
        self.translation_fails = False
        response = self.client.post(f"/api/sessions/{original['id']}/retry-translations")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["segments"][0]["zh"], "这是测试。")
        self.assertIn("这是测试。", self.client.get(files["zh-CN.srt"]).content.decode("utf-8"))

    def test_origin_host_token_and_file_paths_are_rejected(self):
        self.assertEqual(self.client.get("/api/settings", headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/settings", headers={"origin": "null"}, json={"api_key": self.secret}).status_code, 403)
        self.assertEqual(self.client.get("/api/settings", headers={"host": "evil.example"}).status_code, 400)
        self.configure()
        session = self.create_session()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"ws://127.0.0.1:8765/ws/{session['id']}?token=wrong", headers={"origin": self.origin}):
                pass
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"ws://127.0.0.1:8765/ws/{session['id']}?token={session['token']}", headers={"origin": "https://evil.example"}):
                pass
        for url in (
            f"/api/sessions/{session['id']}/files/settings.json",
            f"/api/sessions/{session['id']}/files/%2e%2e%2fsettings.json",
            "/api/sessions/not-a-valid-id/files/audio.wav",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_missing_api_key_and_parallel_reservation_are_clear_errors(self):
        response = self.client.post("/api/sessions", json={})
        self.assertEqual(response.status_code, 400)
        self.configure()
        self.create_session()
        self.assertEqual(self.client.post("/api/sessions", json={}).status_code, 409)

    def test_connections_can_be_saved_before_models_but_recording_requires_both(self):
        response = self.client.post("/api/settings", json={
            "api_key": self.secret, "api_base_url": "https://chosen.example/v1",
            "transcription_model": "", "translation_model": "",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["configuration_ready"])
        response = self.client.post("/api/sessions", json={})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("尚未选择语音识别模型", response.json()["detail"])
        self.client.post("/api/settings", json={"transcription_model": "fixture-speech-model"})
        response = self.client.post("/api/sessions", json={})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("尚未选择翻译模型", response.json()["detail"])
        self.assertEqual(self.transcribers, [])
        self.assertEqual(self.client.get("/api/sessions").json(), [])

    def test_invalid_key_field_validation_never_echoes_input_secrets(self):
        private_marker = "sk-validation-must-remain-private"
        for field in ("api_key", "transcription_api_key", "translation_api_key"):
            for value in (private_marker * 30, {"untrusted": private_marker}):
                with self.subTest(field=field, value_type=type(value).__name__):
                    response = self.client.post("/api/settings", json={field: value})
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn("detail", response.json())
                    self.assertNotIn(private_marker, response.text)
                    self.assertNotIn('"input"', response.text)
        self.assertFalse(self.client.get("/api/settings").json()["key_configured"])

    def test_long_session_rotation_flushes_packet_tail_and_keeps_global_clock(self):
        async def scenario():
            class Socket:
                async def send_json(self, event):
                    pass

            providers = []

            def factory(**kwargs):
                provider = FakeTranscriber(**kwargs)
                providers.append(provider)
                return provider

            store = SessionStore(Path(self.temporary.name) / "rotation")
            session = store.create("Long session")
            settings = {**DEFAULT_SETTINGS, "api_key": self.secret, "api_base_url": "https://api.openai.com/v1", "transcription_mode": "realtime", "transcription_model": "fixture-speech-model"}
            live = LiveSession(session, settings, store, Socket(), factory, FakeTranslator)
            await live.start()
            live.connected_at -= 2401  # Cross the rotation threshold without waiting.
            stereo = struct.pack("<hh", 1234, -5678) * (48000 * 514 // 100)
            await live.receive_audio(b"\x00" + stereo)
            # A single packet contains a 5-second commit and 40 ms of the next
            # turn. Both must be committed before the first provider is closed.
            await live.receive_audio(b"\x01" + struct.pack("<h", 1200) * (24000 * 504 // 100))
            await asyncio.wait_for(live.audio_queue.join(), timeout=1)
            self.assertEqual(len(providers), 2)
            self.assertEqual(providers[0].commits, [(0, 5000), (5000, 5040)])
            self.assertTrue(providers[0].closed)
            await live.receive_audio(b"\x01" + struct.pack("<h", 1200) * 2400)
            await live.finish()
            self.assertEqual(providers[1].commits, [(5040, 5140)])
            self.assertEqual(session["duration_ms"], 5140)
            self.assertEqual([(item["start_ms"], item["end_ms"]) for item in session["segments"]],
                             [(0, 5000), (5000, 5040), (5040, 5140)])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
