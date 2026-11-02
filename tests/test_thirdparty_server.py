"""Compatible-service routing tests; all cloud traffic uses local fakes."""

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

import httpx
from fastapi.testclient import TestClient

from yakiniku.config import effective_provider
from yakiniku.server import create_app
from yakiniku.storage import SettingsStore


class CapturingTranscriber:
    def __init__(self, api_key, model, prompt, on_event, **options):
        self.api_key = api_key
        self.model = model
        self.options = options
        self.on_event = on_event
        self.appended = bytearray()
        self.commits = []
        self.pending = []
        self.closed = False
        self.failed = False

    async def connect(self):
        await self.on_event({"type": "connection", "status": "connected"})

    async def append(self, pcm):
        self.appended.extend(pcm)

    async def commit(self, start_ms, end_ms):
        self.commits.append((start_ms, end_ms))
        identifier = f"compatible-item-{len(self.commits)}"

        async def complete():
            await asyncio.sleep(.001)
            await self.on_event({
                "type": "transcript", "id": identifier,
                "start_ms": start_ms, "end_ms": end_ms, "ja": "第三者APIを確認します。",
            })

        self.pending.append(asyncio.create_task(complete()))

    async def finish(self, timeout=20):
        if self.pending:
            await asyncio.wait_for(asyncio.gather(*self.pending), timeout)
        return True

    async def close(self):
        self.closed = True


class CapturingTranslator:
    def __init__(self, api_key, model, glossary, **options):
        self.api_key = api_key
        self.model = model
        self.glossary = glossary
        self.options = options
        self.closed = False

    async def translate(self, text, context=None):
        await asyncio.sleep(.001)
        return "正在检查第三方 API。"

    async def close(self):
        self.closed = True


class ThirdPartyServerTests(unittest.TestCase):
    origin = "http://127.0.0.1:8765"
    shared_key = "sk-shared-private-fixture"
    speech_key = "sk-speech-private-fixture"
    translation_key = "sk-translation-private-fixture"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.realtime_instances = []
        self.segmented_instances = []
        self.translation_instances = []
        self.probe_requests = []
        self.probe_options = []
        self.probe_handler = lambda request: httpx.Response(200, json={"data": []})

        def realtime_factory(**kwargs):
            provider = CapturingTranscriber(**kwargs)
            self.realtime_instances.append(provider)
            return provider

        def segmented_factory(**kwargs):
            provider = CapturingTranscriber(**kwargs)
            self.segmented_instances.append(provider)
            return provider

        def translation_factory(**kwargs):
            provider = CapturingTranslator(**kwargs)
            self.translation_instances.append(provider)
            return provider

        def mock_transport(request):
            self.probe_requests.append(request)
            return self.probe_handler(request)

        def probe_client_factory(**kwargs):
            self.probe_options.append(kwargs)
            return httpx.AsyncClient(transport=httpx.MockTransport(mock_transport), **kwargs)

        self.app = create_app(
            Path(self.temporary.name), transcriber_factory=realtime_factory,
            chunked_transcriber_factory=segmented_factory, translator_factory=translation_factory,
            probe_client_factory=probe_client_factory,
        )
        self.client = TestClient(self.app, base_url=self.origin)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def assert_private(self, value):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for secret in (self.shared_key, self.speech_key, self.translation_key):
            self.assertNotIn(secret, rendered)

    def settings(self, **overrides):
        values = {
            "api_key": self.shared_key,
            "api_base_url": "https://shared.example/gateway/v1",
            "transcription_base_url": "https://speech.example/team/asr/v1",
            "transcription_api_key": self.speech_key,
            "translation_base_url": "https://translation.example/proxy/v1",
            "translation_api_key": self.translation_key,
            "transcription_mode": "segmented",
            "translation_api": "chat_completions",
            "transcription_model": "openai/whisper-1",
            "translation_model": "vendor/翻译 模型 2",
            "segment_seconds": 3,
            "glossary": "配信 = 直播",
        }
        values.update(overrides)
        response = self.client.post("/api/settings", json=values)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_private(response.text)
        return response.json()

    def receive_until(self, socket, kind):
        events = []
        for _ in range(30):
            event = socket.receive_json()
            events.append(event)
            if event.get("type") == kind:
                return event, events
        self.fail("Expected websocket event " + kind)

    def record_tail(self):
        response = self.client.post("/api/sessions", json={"title": "第三方服务测试", "sample_rate": 48000, "channels": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_private(response.text)
        session = response.json()
        with self.client.websocket_connect(
            f"ws://127.0.0.1:8765/ws/{session['id']}?token={session['token']}",
            headers={"origin": self.origin},
        ) as socket:
            ready, initial_events = self.receive_until(socket, "ready")
            socket.send_bytes(b"\x00" + struct.pack("<hh", 1234, -5678) * 57600)
            socket.send_bytes(b"\x01" + struct.pack("<h", 1200) * 28800)
            socket.send_json({"type": "stop"})
            saved, final_events = self.receive_until(socket, "saved")
            self.assert_private(initial_events + final_events)
        return ready, saved["session"]

    def test_segmented_capture_routes_role_settings_and_exports_final_tail(self):
        configured = self.settings()
        self.assertEqual(configured["transcription_model"], "openai/whisper-1")
        self.assertEqual(configured["translation_model"], "vendor/翻译 模型 2")
        ready, saved = self.record_tail()
        self.assertEqual(ready["recognition_mode"], "segmented")
        self.assertEqual(ready["segment_seconds"], 3)
        self.assertEqual(len(self.realtime_instances), 0)
        self.assertEqual(len(self.segmented_instances), 1)
        speech = self.segmented_instances[0]
        self.assertEqual(speech.api_key, self.speech_key)
        self.assertEqual(speech.model, "openai/whisper-1")
        self.assertEqual(speech.options["base_url"], "https://speech.example/team/asr/v1")
        self.assertEqual(speech.commits, [(0, 1200)])
        self.assertEqual(len(speech.appended), 57600)
        translation = self.translation_instances[0]
        self.assertEqual(translation.api_key, self.translation_key)
        self.assertEqual(translation.model, "vendor/翻译 模型 2")
        self.assertEqual(translation.options["base_url"], "https://translation.example/proxy/v1")
        self.assertEqual(translation.options["api_format"], "chat_completions")
        self.assertTrue(speech.closed and translation.closed)
        self.assertEqual(saved["duration_ms"], 1200)
        self.assertEqual(saved["segments"][0]["zh"], "正在检查第三方 API。")
        files = {item["name"]: item["url"] for item in saved["files"]}
        with wave.open(io.BytesIO(self.client.get(files["audio.wav"]).content), "rb") as stream:
            self.assertEqual((stream.getnchannels(), stream.getframerate(), stream.getnframes()), (2, 48000, 57600))
        chinese = self.client.get(files["zh-CN.srt"]).text
        self.assertIn("00:00:00,000 --> 00:00:01,200", chinese)
        self.assertIn("正在检查第三方 API。", chinese)
        for url in ("/api/settings", "/api/sessions", files["session.json"]):
            self.assert_private(self.client.get(url).text)

    def test_independent_keys_allow_capture_without_shared_key(self):
        configured = self.settings(api_key="")
        self.assertFalse(configured["shared_key_configured"])
        self.assertTrue(configured["key_configured"])
        self.assertTrue(configured["transcription_key_configured"])
        self.assertTrue(configured["translation_key_configured"])
        _, saved = self.record_tail()
        self.assertEqual(saved["status"], "completed")

    def test_blank_key_save_preserves_all_keys_and_explicit_clear_uses_shared(self):
        self.settings()
        response = self.client.post("/api/settings", json={
            "api_key": "", "transcription_api_key": "", "translation_api_key": "",
            "glossary": "新术语",
        })
        self.assertEqual(response.status_code, 200, response.text)
        stored = SettingsStore(Path(self.temporary.name)).load()
        self.assertEqual(stored["api_key"], self.shared_key)
        self.assertEqual(stored["transcription_api_key"], self.speech_key)
        self.assertEqual(stored["translation_api_key"], self.translation_key)
        response = self.client.post("/api/settings", json={
            "clear_transcription_api_key": True, "clear_translation_api_key": True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        public = response.json()
        self.assertFalse(public["transcription_has_own_key"])
        self.assertFalse(public["translation_has_own_key"])
        self.assertTrue(public["key_configured"])
        self.assert_private(response.text)
        stored = SettingsStore(Path(self.temporary.name)).load()
        self.assertEqual(effective_provider(stored, "transcription")["api_key"], self.shared_key)
        self.assertEqual(effective_provider(stored, "translation")["api_key"], self.shared_key)

    def test_cross_origin_role_change_requires_explicit_key_entry(self):
        self.settings()
        target = "https://new-speech.example/v1"
        response = self.client.post("/api/settings", json={"transcription_base_url": target, "transcription_api_key": ""})
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_private(response.text)
        self.assertEqual(self.client.get("/api/settings").json()["transcription_base_url"], "https://speech.example/team/asr/v1")
        response = self.client.post("/api/settings", json={"transcription_base_url": target, "transcription_api_key": self.speech_key})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["transcription_base_url"], target)

    def test_shared_destination_change_requires_key_but_same_origin_prefix_keeps_it(self):
        self.settings(transcription_base_url="", translation_base_url="", transcription_api_key="", translation_api_key="")
        response = self.client.post("/api/settings", json={"api_base_url": "https://shared.example:443/new-prefix/v1/", "api_key": ""})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["api_base_url"], "https://shared.example:443/new-prefix/v1")
        self.assertEqual(SettingsStore(Path(self.temporary.name)).load()["api_key"], self.shared_key)
        response = self.client.post("/api/settings", json={"api_base_url": "https://new-shared.example/v1", "api_key": ""})
        self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post("/api/settings", json={"api_base_url": "https://new-shared.example/v1", "api_key": self.shared_key})
        self.assertEqual(response.status_code, 200, response.text)

    def test_diagnostics_use_saved_hosts_and_role_authentication_only(self):
        self.settings()

        def handler(request):
            expected = {
                "speech.example": (self.speech_key, "openai/whisper-1", "/team/asr/v1/models"),
                "translation.example": (self.translation_key, "vendor/翻译 模型 2", "/proxy/v1/models"),
            }
            self.assertIn(request.url.host, expected)
            key, model, path = expected[request.url.host]
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.content, b"")
            self.assertEqual(request.url.path, path)
            self.assertEqual(request.headers["authorization"], "Bearer " + key)
            return httpx.Response(200, json={"data": [{"id": model}]})

        self.probe_handler = handler
        # The diagnostic endpoint checks saved configuration, ignoring an
        # unrelated form body rather than sending credentials to that address.
        response = self.client.post("/api/settings/test", json={"api_base_url": "https://unconfigured.example/v1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_private(response.text)
        self.assertEqual(len(self.probe_requests), 2)
        self.assertEqual(self.probe_options, [{"timeout": 12, "follow_redirects": False}])
        for role in ("transcription", "translation"):
            self.assertTrue(response.json()[role]["ok"])
            self.assertTrue(response.json()[role]["model_found"])

    def test_validation_errors_do_not_echo_any_api_key(self):
        secret = "never-echo-this-secret" * 30
        for field in ("api_key", "transcription_api_key", "translation_api_key"):
            with self.subTest(field=field):
                response = self.client.post("/api/settings", json={field: secret})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(secret, response.text)
                self.assertNotIn("never-echo", response.text)
                self.assertNotIn('"input"', response.text)
        response = self.client.post("/api/settings", json={"api_key": {"secret": secret}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("never-echo", response.text)

    def test_diagnostics_deduplicate_shared_base_and_key(self):
        self.settings(transcription_base_url="", translation_base_url="", transcription_api_key="", translation_api_key="")
        self.probe_handler = lambda request: httpx.Response(200, json={"data": [
            {"id": "openai/whisper-1"}, {"id": "vendor/翻译 模型 2"},
        ]})
        response = self.client.post("/api/settings/test")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.probe_requests), 1)
        request = self.probe_requests[0]
        self.assertEqual(str(request.url), "https://shared.example/gateway/v1/models")
        self.assertEqual(request.headers["authorization"], "Bearer " + self.shared_key)
        self.assertTrue(response.json()["transcription"]["model_found"])
        self.assertTrue(response.json()["translation"]["model_found"])

    def test_diagnostics_model_absence_is_advisory_and_404_explains_limit(self):
        self.settings(transcription_base_url="", translation_base_url="", transcription_api_key="", translation_api_key="")
        self.probe_handler = lambda request: httpx.Response(200, json={"data": [{"id": "unrelated-model"}]})
        response = self.client.post("/api/settings/test")
        for role in ("transcription", "translation"):
            self.assertTrue(response.json()[role]["ok"])
            self.assertFalse(response.json()[role]["model_found"])
            self.assertIn("未列出", response.json()[role]["message"])
        self.probe_handler = lambda request: httpx.Response(404, json={"error": {"message": self.shared_key}})
        response = self.client.post("/api/settings/test")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_private(response.text)
        for role in ("transcription", "translation"):
            self.assertFalse(response.json()[role]["ok"])
            self.assertIn("模型列表", response.json()[role]["message"])
            self.assertIn("无法自动检查", response.json()[role]["message"])


if __name__ == "__main__":
    unittest.main()
