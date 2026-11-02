"""Loopback-only recording service. Audio durability is independent of cloud APIs."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audio import AudioSegmenter, SegmentedWaveWriter
from .config import effective_provider, normalize_base_url
from .import_audio import AudioImportManager, ImportAudioError, MAX_IMPORT_BYTES, import_filename, probe_audio
from .local_transcriber import LocalTranscriber, local_model_ready
from .model_runtime import MODEL_ROOT, get_live_model_runtime
from .model_downloads import ModelDownloadManager
from .local_translation import LocalTranslationRuntime
from .local_translation_models import TranslationModelDownloadManager, MODEL_CATALOG
from .live_config import LiveSettingsInput, public_live_settings, save_live_settings, resolve_live_settings, live_snapshot
from .live_pipeline import (DurableLiveSession, make_live_translator, translate_once, read_audio_range,
                            recover_tail, pending_count, error_summary, BACKFILL_TRANSLATION_TIMEOUT, retry_live_subtitles,
                            validate_model_snapshot, validate_translation_snapshot)
from .provider import ChunkedTranscriber, ProviderError, RealtimeTranscriber, Translator
from .storage import SessionStore, SettingsStore
from .subtitle_split import subtitle_settings
from .service_config import (ServiceConfigInput, ConnectionInput, SectionConfigInput, ConnectionModelsInput,
                             public_configuration, save_configuration, save_connection, save_section, discovery_provider,
                             ConfigurationConflict)
from .text_pipeline import create_text_processor, process_segment, text_options, validate_text_settings
from .refinement import (refinement_snapshot, resolve_refinement_settings, saved_audio_manifest,
                         existing_refinement, public_chain)
from .space_downloads import SpaceDownloadManager, space_download_router

PROJECT = Path(__file__).resolve().parent.parent
STATIC = PROJECT / "static"
TRANSLATION_WORKERS = 3
TRANSLATION_REQUEST_TIMEOUT = 120


class RefinementInput(BaseModel):
    consent: bool = False


class SubtitleSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)
    max_chars: int = Field(strict=True, ge=8, le=80)
    max_seconds: float = Field(strict=True, ge=2, le=15)


class SettingsInput(BaseModel):
    api_key: Optional[str] = Field(default=None, max_length=500)
    api_base_url: Optional[str] = Field(default=None, max_length=2000)
    transcription_base_url: Optional[str] = Field(default=None, max_length=2000)
    transcription_api_key: Optional[str] = Field(default=None, max_length=500)
    translation_base_url: Optional[str] = Field(default=None, max_length=2000)
    translation_api_key: Optional[str] = Field(default=None, max_length=500)
    correction_base_url: Optional[str] = Field(default=None, max_length=2000)
    correction_api_key: Optional[str] = Field(default=None, max_length=500)
    correction_model: Optional[str] = Field(default=None, max_length=256)
    correction_api: Optional[str] = None
    correction_anthropic_auth: Optional[Literal["api_key", "bearer"]] = None
    transcription_mode: Optional[str] = None
    translation_api: Optional[str] = None
    translation_anthropic_auth: Optional[Literal["api_key", "bearer"]] = None
    transcription_model: Optional[str] = Field(default=None, max_length=256)
    translation_model: Optional[str] = Field(default=None, max_length=256)
    segment_seconds: Optional[float] = Field(default=None, ge=3, le=10)
    glossary: Optional[str] = Field(default=None, max_length=4000)
    clear_transcription_api_key: bool = False
    clear_translation_api_key: bool = False


class ModelListInput(BaseModel):
    """One provider's connection draft, without generation settings."""
    role: Literal["translation", "transcription", "import_transcription"] = "translation"
    api_key: Optional[str] = Field(default=None, max_length=500)
    api_base_url: Optional[str] = Field(default=None, max_length=2000)
    transcription_base_url: Optional[str] = Field(default=None, max_length=2000)
    transcription_api_key: Optional[str] = Field(default=None, max_length=500)
    translation_base_url: Optional[str] = Field(default=None, max_length=2000)
    translation_api_key: Optional[str] = Field(default=None, max_length=500)
    translation_api: Optional[Literal["responses", "chat_completions", "anthropic"]] = None
    translation_anthropic_auth: Optional[Literal["api_key", "bearer"]] = None
    import_transcription_base_url: Optional[str] = Field(default=None, max_length=2000)
    import_transcription_api_key: Optional[str] = Field(default=None, max_length=500)
    clear_transcription_api_key: bool = False
    clear_translation_api_key: bool = False
    clear_import_transcription_api_key: bool = False


class ImportSettingsInput(BaseModel):
    import_transcription_base_url: Optional[str] = Field(default=None, max_length=2000)
    import_transcription_api_key: Optional[str] = Field(default=None, max_length=500)
    import_transcription_model: Optional[str] = Field(default=None, max_length=256)
    import_transcription_response_format: Optional[str] = None
    import_transcription_api: Optional[Literal["openai", "minimax"]] = None
    clear_import_transcription_api_key: bool = False


class SessionInput(BaseModel):
    title: str = Field(default="", max_length=100)
    sample_rate: int = 48000
    channels: int = 2


class ModelPrepareInput(BaseModel):
    backend: Literal["mlx", "tiny"] = "mlx"
    model: str = Field(default="mlx-whisper-small", max_length=256)


class ModelDownloadTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["mlx"] = "mlx"
    model: Literal["mlx-whisper-small", "mlx-whisper-large-v3-turbo"]


class ModelDownloadInput(ModelDownloadTarget):
    consent: bool = Field(default=False, strict=True)


class TranslationModelTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128)


class TranslationModelDownloadInput(TranslationModelTarget):
    consent: bool = Field(default=False, strict=True)


async def translate_segment(translator, segment, context, checkpoint=None):
    await process_segment(translator, segment, context, checkpoint=checkpoint)


class LiveSession:
    def __init__(self, session, settings, store, socket, transcriber_factory, translator_factory):
        self.session = session
        self.settings = settings
        self.store = store
        self.socket = socket
        self.transcriber_factory = transcriber_factory
        self.translator_factory = translator_factory
        self.transcriber = None
        self.translator = None
        self.writer = None
        self.audio_queue = asyncio.Queue(maxsize=600)
        self.translation_queue = asyncio.Queue()
        self.audio_task = None
        self.translation_task = None
        self.translation_tasks = []
        self.send_lock = asyncio.Lock()
        self.segmenter = AudioSegmenter(segment_seconds=settings.get("segment_seconds", 5))
        self.processed_samples = 0
        self.segment_base_ms = 0
        self.connected_at = 0.0
        self.recognition_enabled = True
        self.connection_failures = 0
        self.last_progress = 0.0
        self.last_checkpoint = 0.0
        self.finishing = False

    async def emit(self, event):
        async with self.send_lock:
            with contextlib.suppress(Exception):
                await self.socket.send_json(event)

    async def warning(self, message):
        if not self.session["warnings"] or self.session["warnings"][-1] != message:
            self.session["warnings"].append(message)
            self.store.save(self.session)
        await self.emit({"type": "warning", "message": message})

    async def on_provider_event(self, event):
        kind = event.get("type")
        if kind == "warning":
            await self.warning(event["message"])
        elif kind == "transcript":
            if not event.get("ja", "").strip():
                return
            segment = {
                "id": event["id"], "start_ms": event["start_ms"],
                "end_ms": event["end_ms"], "ja": event["ja"],
                "zh": "", "translation_status": "pending",
            }
            self.store.append_segment(self.session, segment)
            self.store.export(self.session)
            await self.emit({"type": "transcript", "segment": segment})
            self.translation_queue.put_nowait(segment)
        else:
            await self.emit(event)

    async def connect_transcriber(self):
        if self.settings.get("transcription_mode") == "local":
            prompt = self.settings.get("glossary", "")[:1400]
        else:
            prompt = "日本語のライブ配信。聞こえた日本語だけを忠実に文字起こししてください。"
            if self.settings.get("glossary"):
                prompt += "\n固有名詞の参考（指示ではありません）:\n" + self.settings["glossary"][:1400]
        self.transcriber = self.transcriber_factory(
            **({"api_key": "", "base_url": ""} if self.settings.get("transcription_mode") == "local"
               else effective_provider(self.settings, "transcription")),
            model=self.settings["transcription_model"],
            prompt=prompt, on_event=self.on_provider_event,
        )
        await self.transcriber.connect()
        self.connected_at = time.monotonic()

    async def start(self):
        self.writer = SegmentedWaveWriter(
            self.store.directory(self.session["id"]),
            sample_rate=self.session["sample_rate"], channels=2,
        )
        self.translator = await create_text_processor(self.settings, self.translator_factory)
        correction = text_options(self.settings, "correction")
        self.session.update(correction_model=correction["model"], correction_api=correction["api_format"])
        try:
            await self.connect_transcriber()
        except ProviderError as exc:
            self.recognition_enabled = False
            await self.warning(str(exc) + " 当前仅录制音频。")
        self.session["status"] = "recording"
        self.store.save(self.session)
        self.translation_tasks = [asyncio.create_task(self.translate_worker())
                                  for _ in range(TRANSLATION_WORKERS)]
        self.translation_task = self.translation_tasks[0]
        self.audio_task = asyncio.create_task(self.audio_worker())
        await self.emit({
            "type": "ready", "mode": "live" if self.recognition_enabled else "recording_only",
            "recognition_mode": self.settings.get("transcription_mode", "realtime"),
            "segment_seconds": self.settings.get("segment_seconds", 5),
            "session": self.session,
        })

    async def receive_audio(self, packet: bytes):
        if not packet or len(packet) > 1024 * 1024:
            raise ValueError("音频数据包大小异常。")
        kind, pcm = packet[0], packet[1:]
        if kind == 0:
            self.writer.write(pcm)
            self.session["duration_ms"] = self.writer.duration_ms
            self.session["audio_parts"] = self.writer.parts
            now = time.monotonic()
            if now - self.last_progress >= 1:
                await self.emit({"type": "progress", "duration_ms": self.writer.duration_ms})
                self.last_progress = now
            if now - self.last_checkpoint >= 10:
                self.store.save(self.session)
                self.last_checkpoint = now
        elif kind == 1:
            if len(pcm) % 2:
                raise ValueError("转写音频数据包不完整。")
            if self.recognition_enabled:
                try:
                    self.audio_queue.put_nowait(pcm)
                except asyncio.QueueFull:
                    self.recognition_enabled = False
                    await self.warning("识别处理已落后超过一分钟，已暂停语音识别；录音仍在完整保存。")
        else:
            raise ValueError("未知音频数据格式。")

    async def apply_actions(self, actions):
        for action in actions:
            if action["type"] == "append":
                await self.transcriber.append(action["pcm"])
            else:
                await self.transcriber.commit(
                    self.segment_base_ms + action["start_ms"],
                    self.segment_base_ms + action["end_ms"],
                )

    async def audio_worker(self):
        while True:
            pcm = await self.audio_queue.get()
            try:
                if pcm is None:
                    if self.recognition_enabled and self.transcriber is not None:
                        with contextlib.suppress(ProviderError):
                            await self.apply_actions(self.segmenter.flush())
                    return
                self.processed_samples += len(pcm) // 2
                if not self.recognition_enabled:
                    continue
                try:
                    actions = self.segmenter.process(pcm)
                    await self.apply_actions(actions)
                    # Rotate before the provider's long-session limit. The WAV
                    # receiver continues writing while queued speech catches up.
                    if self.settings.get("transcription_mode", "realtime") == "realtime" and time.monotonic() - self.connected_at >= 40 * 60 and any(
                        action["type"] == "commit" for action in actions
                    ):
                        # A 100 ms packet can contain another speech onset after
                        # its first commit. Drain that tail before changing socket.
                        await self.apply_actions(self.segmenter.flush())
                        await self.transcriber.finish(timeout=15)
                        await self.transcriber.close()
                        await self.emit({"type": "connection", "status": "reconnecting"})
                        await self.connect_transcriber()
                except ProviderError:
                    await self.reconnect()
            finally:
                self.audio_queue.task_done()

    async def reconnect(self):
        self.connection_failures += 1
        if self.transcriber is not None:
            await self.transcriber.close()
        self.segmenter = AudioSegmenter(segment_seconds=self.settings.get("segment_seconds", 5))
        self.segment_base_ms = self.processed_samples * 1000 // 24000
        await self.warning("转写连接中断，正在重连；断开前尚未确认的字幕可能缺失，原始音频已保留。")
        await self.emit({"type": "connection", "status": "reconnecting"})
        if self.connection_failures <= 3:
            try:
                await self.connect_transcriber()
                return
            except ProviderError as exc:
                await self.warning(str(exc))
        self.recognition_enabled = False
        message = ("本机识别暂不可用，当前继续录音；请在结束后检查本机模型。"
                   if self.settings.get("transcription_mode") == "local" else
                   "云端转写暂不可用，当前继续录音；请在结束后检查网络和 API 设置。")
        await self.warning(message)

    async def translate_worker(self):
        while True:
            segment = await self.translation_queue.get()
            try:
                if segment is None:
                    return
                context = [dict(s) for s in self.session["segments"] if
                           s["start_ms"] < segment["start_ms"] and s.get("zh")][-4:]
                try:
                    async def checkpoint(value):
                        self.store.append_segment(self.session, value)
                        self.store.export(self.session)
                        await self.emit({"type": "correction", "segment": dict(value)})

                    await asyncio.wait_for(
                        translate_segment(self.translator, segment, context, checkpoint=checkpoint),
                        timeout=TRANSLATION_REQUEST_TIMEOUT,
                    )
                    segment["translation_status"] = "done"
                except ProviderError as exc:
                    segment["translation_status"] = "failed"
                    await self.warning(str(exc) + " 原文已保留，可在录制文件中重试翻译。")
                except asyncio.TimeoutError:
                    segment["translation_status"] = "failed"
                    await self.warning("一个翻译片段处理超时，原文已保留，可在录制文件中重试翻译。")
                except Exception:
                    segment["translation_status"] = "failed"
                    await self.warning("一个翻译片段处理失败，原文已保留，其余片段会继续翻译。")
                self.store.append_segment(self.session, segment)
                self.store.export(self.session)
                await self.emit({"type": "translation", "segment": segment})
            finally:
                self.translation_queue.task_done()

    async def finish(self, interrupted=False):
        if self.finishing:
            return
        self.finishing = True
        if self.writer is not None:
            self.writer.close()
            self.session["duration_ms"] = self.writer.duration_ms
            self.session["audio_parts"] = self.writer.parts
        self.session["status"] = "finalizing"
        self.store.save(self.session)
        if self.audio_task is not None:
            # The sentinel follows all received PCM; the tail is committed first.
            if self.audio_queue.full():
                self.recognition_enabled = False
                while not self.audio_queue.empty():
                    self.audio_queue.get_nowait()
                    self.audio_queue.task_done()
            self.audio_queue.put_nowait(None)
            try:
                await asyncio.wait_for(self.audio_task, timeout=25)
            except (asyncio.TimeoutError, ProviderError):
                await self.warning("末段转写未及时完成，已先保存完整录音和收到的字幕。")
        if self.transcriber is not None:
            await self.transcriber.finish(timeout=35 if self.settings.get("transcription_mode") in ("local", "segmented") else 15)
            await self.transcriber.close()
        translation_tasks = self.translation_tasks or ([self.translation_task] if self.translation_task else [])
        if translation_tasks:
            # Recognition is drained before these sentinels. Each worker exits
            # only after every earlier queued turn has been taken; wait for all
            # in-flight requests before closing their shared HTTP client.
            for _ in translation_tasks:
                self.translation_queue.put_nowait(None)
            results = await asyncio.gather(*translation_tasks, return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                await self.warning("部分翻译处理未能完成，可在录制文件中重试；原文与音频已保存。")
        for segment in self.session["segments"]:
            if segment.get("translation_status") == "pending":
                segment["translation_status"] = "failed"
        if self.translator is not None:
            await self.translator.close()
        self.session["status"] = "interrupted" if interrupted else "completed"
        exported = self.store.export(self.session)
        await self.emit({"type": "saved", "session": exported})


def create_app(data_root=None, transcriber_factory=RealtimeTranscriber, translator_factory=Translator,
               chunked_transcriber_factory=ChunkedTranscriber, probe_client_factory=httpx.AsyncClient,
               local_transcriber_factory=LocalTranscriber, import_model_loader=None,
               import_file_transcriber_factory=None, live_model_runtime=None, space_download_runner=None,
               live_model_downloader=None, local_translation_runtime=None, local_translation_downloader=None):
    data = Path(data_root or os.environ.get("YAKINIKU_DATA_DIR", PROJECT / "data"))
    settings_store = SettingsStore(data)
    sessions = SessionStore(data / "recordings")
    active: Dict[str, LiveSession] = {}
    reservations: Dict[str, Dict[str, Any]] = {}
    retry_locks = set()
    model_preparations = set()
    runtime = live_model_runtime or get_live_model_runtime()
    model_downloads = (live_model_downloader if live_model_downloader is not None else
                       ModelDownloadManager(root=getattr(runtime, "root", None) or MODEL_ROOT))
    translation_runtime = (local_translation_runtime if local_translation_runtime is not None else
                           LocalTranslationRuntime(executor=getattr(runtime, "inference_executor", None)))
    translation_downloads = (local_translation_downloader if local_translation_downloader is not None else
                             TranslationModelDownloadManager(root=getattr(translation_runtime, "root", None)))

    def model_operations_active():
        return bool(model_preparations or model_downloads.is_active() or translation_downloads.is_active())
    # Legacy adapter injection remains available to integrations using the old
    # cloud speech interface. Ordinary application sessions use durable local ASR.
    modern_live = live_model_runtime is not None or (transcriber_factory is RealtimeTranscriber
                   and local_transcriber_factory is LocalTranscriber)
    imports = AudioImportManager(sessions, settings_store, translator_factory,
                                model_loader=import_model_loader, worker_count=TRANSLATION_WORKERS,
                                request_timeout=TRANSLATION_REQUEST_TIMEOUT,
                                **({"file_transcriber_factory": import_file_transcriber_factory}
                                   if import_file_transcriber_factory is not None else {}))
    space_downloads = SpaceDownloadManager(data / "space-downloads", runner=space_download_runner)

    @asynccontextmanager
    async def lifespan(app):
        sessions.recover()
        space_downloads.start()
        yield
        await model_downloads.shutdown()
        await translation_downloads.shutdown()
        await space_downloads.shutdown()
        await imports.shutdown()
        for live in list(active.values()):
            await live.finish(interrupted=True)
        if local_translation_runtime is None:
            await asyncio.to_thread(translation_runtime.shutdown)

    app = FastAPI(title="直播烤肉台", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.imports = imports
    app.state.live_runtime = runtime
    app.state.model_downloads = model_downloads
    app.state.translation_runtime = translation_runtime
    app.state.translation_downloads = translation_downloads
    app.state.space_downloads = space_downloads
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    def expire_reservations():
        now = time.monotonic()
        for identifier, reserved in list(reservations.items()):
            if now - reserved["created"] > 60:
                abandoned = sessions.load(identifier)
                abandoned["status"] = "interrupted"
                sessions.export(abandoned)
                reservations.pop(identifier, None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Pydantic's default 422 response includes the original input. A failed
        # key field must never echo credentials back into the browser or logs.
        detail = ("请检查 API 地址、密钥、模型名称和分段时长（3–10 秒）的填写格式。"
                  if request.url.path.startswith("/api/settings") else "请求参数格式不正确，请检查后重试。")
        return JSONResponse({"detail": detail}, status_code=422)

    def same_origin(origin, host):
        parsed = urlsplit(origin)
        return parsed.scheme == "http" and parsed.netloc == host and parsed.hostname in ("127.0.0.1", "localhost", "::1")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        origin = request.headers.get("origin")
        extension_capture = bool(origin and re.fullmatch(r"chrome-extension://[a-p]{32}", origin)
                                 and re.fullmatch(r"/api/space-downloads/[0-9a-f]{32}/capture", request.url.path))
        if extension_capture and request.method == "OPTIONS":
            requested = {name.strip().lower() for name in request.headers.get("access-control-request-headers", "").split(",") if name.strip()}
            if (request.headers.get("access-control-request-method", "").upper() != "POST"
                    or not requested.issubset({"content-type", "x-space-capture-token"})):
                return JSONResponse({"detail": "浏览器捕获请求不受支持。"}, status_code=403)
            return JSONResponse({}, headers={"Access-Control-Allow-Origin": origin,
                                            "Access-Control-Allow-Methods": "POST",
                                            "Access-Control-Allow-Headers": "Content-Type, X-Space-Capture-Token",
                                            "Vary": "Origin", "Cache-Control": "no-store"})
        if origin and not same_origin(origin, request.headers.get("host", "")) and not (extension_capture and request.method == "POST"):
            return JSONResponse({"detail": "仅允许本机页面访问。"}, status_code=403)
        response = await call_next(request)
        if extension_capture and request.method == "POST":
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; worker-src 'self'; "
            "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif response.headers.get("content-type", "").startswith("text/html"):
            # Local releases update files in place. A return link must not
            # resurrect an older page with incompatible buttons or modules.
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/") and request.url.path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health():
        return {"app": "yakiniku", "status": "ok", "active": bool(active),
                "import_active": imports.is_active(), "import_supported": True,
                "space_download_supported": True,
                "subtitle_active": bool(retry_locks), "model_preparing": bool(model_preparations),
                "model_downloading": model_downloads.is_active() or translation_downloads.is_active()}

    @app.get("/api/settings")
    async def get_settings():
        return settings_store.public()

    @app.get("/api/live-settings")
    async def get_live_settings():
        expire_reservations()
        return current_live_settings()

    def current_live_settings():
        value = public_live_settings(settings_store, bool(active or reservations or retry_locks or model_operations_active() or imports.is_active()))
        if value.get("translation_backend") == "local":
            state = translation_runtime.status(value["local_translation_model"])
            value.update(translation_ready=state.get("state") == "ready",
                         configuration_ready=state.get("state") == "ready", translation_model_state=state)
        return value

    @app.put("/api/live-settings")
    async def put_live_settings(body: LiveSettingsInput):
        ensure_config_idle()
        if retry_locks:
            raise HTTPException(409, "字幕仍在补全，请完成后修改直播设置。")
        try:
            save_live_settings(settings_store, body)
            return current_live_settings()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/live-model/status")
    async def get_live_model_status(backend: Optional[str] = None, model: Optional[str] = None):
        config = public_live_settings(settings_store)
        try:
            selected_backend, selected_model = backend or config["backend"], model or config["model"]
            state = runtime.status(backend=selected_backend, model=selected_model)
            download = (model_downloads.status(selected_backend, selected_model)
                        if selected_backend == "mlx" and selected_model in
                        ("mlx-whisper-small", "mlx-whisper-large-v3-turbo") else None)
            return {**state, "models": runtime.describe_models(), "download": download,
                    "active_download": model_downloads.active_status()}
        except (ValueError, ProviderError):
            raise HTTPException(400, "本机后端或模型选择无效。") from None

    @app.post("/api/live-model/download", status_code=202)
    async def download_live_model(body: ModelDownloadInput):
        if not body.consent:
            raise HTTPException(400, "请先点击「下载模型」，明确选择下载所选模型。")
        if model_downloads.is_active():
            current = model_downloads.status(body.backend, body.model)
            if current.get("state") in ("downloading", "verifying"):
                return current
            raise HTTPException(409, "已有模型正在下载，请等待完成或取消后再下载其他模型。")
        ensure_config_idle()
        try:
            return await model_downloads.start(body.backend, body.model)
        except (ValueError, ProviderError) as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "所选模型无法下载，请检查后重试。"
            raise HTTPException(400, message) from None

    @app.post("/api/live-model/download/cancel")
    async def cancel_live_model_download(body: ModelDownloadTarget):
        try:
            return await model_downloads.cancel(body.backend, body.model)
        except (ValueError, ProviderError):
            raise HTTPException(400, "所选模型下载状态无效，请重新读取后重试。") from None

    @app.post("/api/live-model/prepare")
    async def prepare_live_model(body: Optional[ModelPrepareInput] = None):
        ensure_config_idle()
        if retry_locks:
            raise HTTPException(409, "字幕仍在补全，请稍后准备模型。")
        config = public_live_settings(settings_store)
        task = asyncio.create_task(runtime.prepare(backend=body.backend if body else config["backend"],
                                                   model=body.model if body else config["model"]))
        model_preparations.add(task)
        def prepared(finished):
            model_preparations.discard(finished)
            if not finished.cancelled():
                finished.exception()
        task.add_done_callback(prepared)
        try:
            return await asyncio.shield(task)
        except (ValueError, ProviderError) as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "本机后端或模型选择无效。"
            raise HTTPException(400, message) from None

    def translation_model_id(model=None):
        selected = model or public_live_settings(settings_store)["local_translation_model"]
        if selected not in {item["id"] for item in MODEL_CATALOG}:
            raise HTTPException(400, "请选择本地翻译下载列表中的模型。")
        return selected

    def translation_status(model=None):
        selected = translation_model_id(model)
        return {**translation_runtime.status(selected), "model": selected,
                "download": translation_downloads.status("mlx", selected),
                "active_download": translation_downloads.active_status()}

    @app.get("/api/live-translation/models")
    async def get_translation_models():
        # Catalog and local file checks only; opening this page never fetches HF.
        choices = []
        for item in MODEL_CATALOG:
            memory = item.get("estimated_runtime_memory_gb", [None, None])
            choices.append({**item, **translation_status(item["id"]),
                            "memory_gb_min": memory[0], "memory_gb_max": memory[1]})
        try:
            total_memory = round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3, 1)
        except (ValueError, OSError):
            total_memory = None
        return {"models": choices, "active_download": translation_downloads.active_status(),
                "hardware": {"total_memory_gb": total_memory},
                "runtime_available": importlib.util.find_spec("mlx_lm") is not None,
                "locked": current_live_settings()["locked"]}

    @app.get("/api/live-translation/status")
    async def get_translation_status(model: Optional[str] = None):
        return translation_status(model)

    @app.post("/api/live-translation/download", status_code=202)
    async def download_translation_model(body: TranslationModelDownloadInput):
        selected = translation_model_id(body.model)
        if not body.consent:
            raise HTTPException(400, "请点击下载按钮，确认下载所选本地翻译模型。")
        if translation_downloads.is_active():
            state = translation_downloads.status("mlx", selected)
            if state.get("state") in ("downloading", "verifying"):
                return state
            raise HTTPException(409, "已有本地翻译模型正在下载，请先完成或取消。")
        ensure_config_idle()
        try:
            return await translation_downloads.start("mlx", selected)
        except (ValueError, ProviderError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/live-translation/download/cancel")
    async def cancel_translation_download(body: TranslationModelTarget):
        selected = translation_model_id(body.model)
        return await translation_downloads.cancel("mlx", selected)

    @app.post("/api/live-translation/prepare")
    async def prepare_translation_model(body: TranslationModelTarget):
        selected = translation_model_id(body.model)
        ensure_config_idle()
        task = asyncio.create_task(translation_runtime.prepare(selected))
        model_preparations.add(task)
        def prepared(finished):
            model_preparations.discard(finished)
            if not finished.cancelled():
                finished.exception()
        task.add_done_callback(prepared)
        try:
            return await asyncio.shield(task)
        except (ValueError, ProviderError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/service-config")
    async def get_service_configuration():
        expire_reservations()
        return public_configuration(settings_store, bool(active or reservations or retry_locks or model_operations_active() or imports.is_active()))

    @app.post("/api/service-config")
    async def save_service_configuration(body: ServiceConfigInput):
        expire_reservations()
        if active or reservations or retry_locks or model_operations_active() or imports.is_active():
            raise HTTPException(409, "请在当前录制或音频处理结束后修改服务商与流程。")
        try:
            save_configuration(settings_store, body)
        except ConfigurationConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return public_configuration(settings_store)

    def ensure_config_idle():
        expire_reservations()
        if active or reservations or retry_locks or model_operations_active() or imports.is_active():
            raise HTTPException(409, "请在当前录制或音频处理结束后修改服务商与流程。")

    @app.get("/api/subtitle-settings")
    async def get_subtitle_settings():
        expire_reservations()
        return {**subtitle_settings(settings_store.load()),
                "locked": bool(active or reservations or retry_locks or model_operations_active() or imports.is_active())}

    @app.put("/api/subtitle-settings")
    async def put_subtitle_settings(body: SubtitleSettingsInput):
        ensure_config_idle()
        settings_store.save({"subtitle_split_enabled": body.enabled,
                             "subtitle_max_chars": body.max_chars,
                             "subtitle_max_seconds": body.max_seconds})
        return {**subtitle_settings(settings_store.load()), "locked": False}

    @app.put("/api/service-config/provider")
    async def save_service_connection(body: ConnectionInput):
        ensure_config_idle()
        try:
            save_connection(settings_store, body)
        except ConfigurationConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return public_configuration(settings_store)

    @app.put("/api/service-config/section/{section}")
    async def save_service_section(section: Literal["asr", "text"], body: SectionConfigInput):
        ensure_config_idle()
        try:
            save_section(settings_store, section, body)
        except ConfigurationConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return public_configuration(settings_store)

    @app.post("/api/service-config/models")
    async def connection_models(body: ConnectionModelsInput):
        try:
            provider = discovery_provider(settings_store, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        from .diagnostics import fetch_available_models
        async with probe_client_factory(timeout=12, follow_redirects=False) as client:
            result = await fetch_available_models(provider, client)
        if not result["ok"]:
            raise HTTPException(502, result["message"])
        return {"models": sorted(result["models"])}

    def import_public_settings():
        public = settings_store.public()
        names = (
            "import_transcription_base_url", "import_transcription_model",
            "import_transcription_response_format", "import_transcription_key_configured",
            "import_transcription_has_own_key", "import_configuration_ready", "import_effective_base_url",
            "translation_key_configured",
            "import_transcription_api", "service_profiles_enabled",
        )
        return {name: public[name] for name in names}

    @app.get("/api/import-settings")
    async def get_import_settings():
        return import_public_settings()

    @app.post("/api/import-settings")
    async def save_import_settings(body: ImportSettingsInput):
        expire_reservations()
        if active or reservations or retry_locks or model_operations_active() or imports.is_active():
            raise HTTPException(409, "请在结束当前录制或音频导入后修改音频识别 API 设置。")
        if settings_store.load().get("service_profiles_enabled"):
            raise HTTPException(409, "请在「服务商与处理流程」中统一修改音频识别配置。")
        values = body.model_dump(exclude_none=True)
        clear = values.pop("clear_import_transcription_api_key", False)
        key = str(values.get("import_transcription_api_key") or "").strip()
        if key:
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key):
                raise HTTPException(400, "API 密钥中不能包含空白或控制字符。")
            values["import_transcription_api_key"] = key
        else:
            values.pop("import_transcription_api_key", None)
        if clear:
            values["import_transcription_api_key"] = ""
        if "import_transcription_base_url" in values:
            raw = values["import_transcription_base_url"].strip()
            try:
                values["import_transcription_base_url"] = normalize_base_url(raw) if raw else ""
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        if "import_transcription_model" in values:
            model = values["import_transcription_model"].strip()
            if any(ord(c) < 32 or ord(c) == 127 for c in model):
                raise HTTPException(400, "请填写平台实际提供的语音识别模型名称。")
            values["import_transcription_model"] = model
        if ("import_transcription_response_format" in values
                and values["import_transcription_response_format"] not in ("verbose_json", "json", "srt")):
            raise HTTPException(400, "请选择带时间戳 JSON、JSON 或 SRT 转写格式。")
        old = settings_store.load()
        candidate = {**old, **values}
        previous = effective_provider(old, "import_transcription")
        provider = effective_provider(candidate, "import_transcription")

        def origin(url):
            parsed = urlsplit(url)
            return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

        if (previous["base_url"] and previous["api_key"] and provider["api_key"] == previous["api_key"]
                and origin(provider["base_url"]) != origin(previous["base_url"])
                and not key and not clear):
            raise HTTPException(400, "音频识别服务地址已更换，请重新填写用于该服务的 API 密钥。")
        settings_store.save(values)
        return import_public_settings()

    @app.post("/api/settings")
    async def save_settings(body: SettingsInput):
        expire_reservations()
        if active or reservations or retry_locks or model_operations_active() or imports.is_active():
            raise HTTPException(409, "请在结束当前录制或音频导入后修改 API 设置。")
        values = body.model_dump(exclude_none=True)
        old_settings = settings_store.load()
        if old_settings.get("service_profiles_enabled"):
            raise HTTPException(409, "请在「服务商与处理流程」中统一修改 API 配置。")
        explicit_keys = set()
        for name in ("api_key", "transcription_api_key", "translation_api_key", "correction_api_key"):
            value = str(values.get(name) or "").strip()
            if not value:
                values.pop(name, None)
                continue
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
                raise HTTPException(400, "API 密钥中不能包含空白或控制字符。")
            values[name] = value
            explicit_keys.add(name)
        clears = {}
        for role in ("transcription", "translation"):
            clears[role] = values.pop(f"clear_{role}_api_key", False)
            if clears[role]:
                values[f"{role}_api_key"] = ""
        candidate = {**old_settings, **values}
        if os.environ.get("OPENAI_API_KEY", "").strip():
            candidate["api_key"] = os.environ["OPENAI_API_KEY"].strip()
        for name in ("api_base_url", "transcription_base_url", "translation_base_url", "correction_base_url"):
            raw = str(candidate.get(name, "")).strip()
            if not raw and (name != "api_base_url" or candidate.get("transcription_mode") == "local"):
                normalized = ""
            else:
                try:
                    normalized = normalize_base_url(raw)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from None
            candidate[name] = normalized
            if name in values:
                values[name] = normalized
        for name in ("transcription_model", "translation_model", "correction_model"):
            model = str(candidate.get(name) or "").strip()
            if any(ord(c) < 32 or ord(c) == 127 for c in model):
                raise HTTPException(400, "请填写平台提供的有效模型名称。")
            candidate[name] = model
            if name in values:
                values[name] = model
        if candidate.get("transcription_mode") not in ("local", "realtime", "segmented"):
            raise HTTPException(400, "请选择本机识别、实时语音或分段语音转写。")
        if candidate.get("translation_api") not in ("responses", "chat_completions", "anthropic"):
            raise HTTPException(400, "请选择 Responses、Chat Completions 或 Anthropic 翻译接口。")
        if candidate.get("correction_api") not in ("", "responses", "chat_completions", "anthropic"):
            raise HTTPException(400, "请选择 Responses、Chat Completions 或 Anthropic 校对接口。")
        for role, label in (("transcription", "语音转写"), ("translation", "中文翻译"),
                            ("import_transcription", "完整音频语音识别"), ("correction", "日语校对")):
            if role == "transcription" and candidate.get("transcription_mode") == "local":
                continue
            old_provider = effective_provider(old_settings, role)
            new_provider = effective_provider(candidate, role)
            def origin(url):
                parsed = urlsplit(url)
                return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            selected_key = f"{role}_api_key" if candidate.get(f"{role}_api_key") else "api_key"
            explicit = selected_key in explicit_keys or clears.get(role, False)
            if selected_key == "api_key" and os.environ.get("OPENAI_API_KEY", "").strip():
                explicit = clears.get(role, False)
            if (old_provider["base_url"] and old_provider["api_key"] and old_provider["api_key"] == new_provider["api_key"]
                    and origin(old_provider["base_url"]) != origin(new_provider["base_url"])
                    and not explicit):
                if role == "import_transcription":
                    raise HTTPException(400, "完整音频语音识别会改用新地址，请先在音频翻译页配置独立地址与密钥，再更换共用地址。")
                raise HTTPException(400, f"{label}的服务地址已更换，请重新填写用于该服务的密钥。")
        settings_store.save(values)
        return settings_store.public()

    @app.post("/api/settings/test")
    async def test_settings():
        settings = settings_store.load()
        from .diagnostics import check_configuration
        async with probe_client_factory(timeout=12, follow_redirects=False) as client:
            return await check_configuration(settings, client)

    @app.post("/api/settings/models")
    async def provider_models(body: ModelListInput):
        # Model selection is needed before the first save. Resolve this draft
        # only in memory, retaining the same secret precedence as saved settings.
        role = body.role
        label = "音频文件语音识别" if role == "import_transcription" else "语音识别" if role == "transcription" else "中文翻译"
        draft = body.model_dump(exclude_none=True)
        values = {name: draft[name] for name in (
            "api_key", "api_base_url", f"{role}_base_url", f"{role}_api_key",
        ) if name in draft}
        if role == "translation":
            values.update({name: draft[name] for name in ("translation_api", "translation_anthropic_auth") if name in draft})
        old_settings = settings_store.load()
        explicit_keys = set()
        for name in ("api_key", f"{role}_api_key"):
            key = str(values.get(name) or "").strip()
            if not key:
                values.pop(name, None)
                continue
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key):
                raise HTTPException(400, "API 密钥中不能包含空白或控制字符。")
            values[name] = key
            explicit_keys.add(name)
        clear_key = draft.get(f"clear_{role}_api_key", False)
        if clear_key:
            values[f"{role}_api_key"] = ""
        candidate = {**old_settings, **values}
        environment_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if environment_key:
            candidate["api_key"] = environment_key
        for name in ("api_base_url", f"{role}_base_url"):
            candidate[name] = str(candidate.get(name) or "").strip()
        try:
            provider = effective_provider(candidate, role)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if not provider["base_url"]:
            raise HTTPException(400, f"请先填写{label}服务的 API 基础地址。")
        if not provider["api_key"]:
            raise HTTPException(400, f"请先填写{label}服务的 API 密钥。")
        old_provider = effective_provider(old_settings, role)

        def origin(url):
            parsed = urlsplit(url)
            return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

        selected_key = f"{role}_api_key" if candidate.get(f"{role}_api_key") else "api_key"
        explicit = selected_key in explicit_keys or clear_key
        if selected_key == "api_key" and environment_key:
            explicit = clear_key
        if (old_provider["base_url"] and old_provider["api_key"] == provider["api_key"]
                and origin(old_provider["base_url"]) != origin(provider["base_url"])
                and not explicit):
            raise HTTPException(400, f"{label}的服务地址已更换，请重新填写用于该服务的密钥。")

        from .diagnostics import fetch_available_models, probe_provider
        provider = probe_provider(candidate, role)
        async with probe_client_factory(timeout=12, follow_redirects=False) as client:
            result = await fetch_available_models(provider, client)
        if not result["ok"]:
            raise HTTPException(502, result["message"])
        return {"models": sorted(result["models"])}

    @app.get("/api/sessions")
    async def list_sessions():
        return sessions.list()

    @app.get("/api/sessions/{identifier}")
    async def session_detail(identifier: str):
        try:
            return imports.public(sessions.load(identifier))
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "找不到这场录制或导入项目。") from None

    @app.get("/api/sessions/{identifier}/refinement")
    async def refinement_state(identifier: str):
        expire_reservations()
        try:
            source = sessions.load(identifier)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "找不到这场直播录制。") from None
        result = {"source_id": identifier, "eligible": False, "ready": False, "message": "",
                  "busy": bool(active or reservations or imports.is_active() or retry_locks or model_operations_active()),
                  "active": False, "project_id": "", "project_status": "", "stage": "",
                  "progress": 0, "progress_label": "", "chain": []}
        try:
            saved_audio_manifest(sessions, source)
            result["eligible"] = True
            child = existing_refinement(sessions, identifier)
            if child:
                result.update(project_id=child["id"], project_status=child.get("status", ""),
                              active=imports.is_active(child["id"]), stage=child.get("import_stage", ""),
                              progress=child.get("import_progress", 0),
                              progress_label=child.get("import_progress_label", ""))
                snapshot = child["refinement_snapshot"]
                result["chain"] = public_chain(snapshot)
                resolve_refinement_settings(settings_store, snapshot)
            else:
                snapshot = refinement_snapshot(settings_store)
                result["chain"] = public_chain(snapshot)
            result["ready"] = True
            result["message"] = (child.get("import_error") or "精准校验已中断，可以继续处理已保存的进度。"
                                 if child and child.get("status") in ("failed", "interrupted") else
                                 "当前有录制或处理任务，请等待结束后开始精准校验。" if result["busy"] else
                                 "配置已就绪。确认下方服务调用后，点击开始精准校验。")
        except (ProviderError, ValueError, KeyError, OSError) as exc:
            result["message"] = str(exc) if isinstance(exc, (ProviderError, ValueError)) else "精准校验项目资料不完整，请检查原录音和配置。"
        return result

    @app.post("/api/sessions/{identifier}/refine", status_code=202)
    async def refine_recording(identifier: str, body: RefinementInput):
        if not body.consent:
            raise HTTPException(400, "请先确认将录音发送到已配置的 MiniMax，并使用所选 GPT 服务校对和翻译。")
        expire_reservations()
        try:
            source = sessions.load(identifier)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "找不到这场直播录制。") from None
        try:
            manifest = saved_audio_manifest(sessions, source)
        except (ValueError, FileNotFoundError, ProviderError) as exc:
            raise HTTPException(400, str(exc)) from None
        child = existing_refinement(sessions, identifier)
        if child:
            # Repeated clicks, browser timeouts and reloads never bill twice.
            return imports.public(sessions.load(child["id"]))
        if active or reservations or imports.is_active() or retry_locks or model_operations_active():
            raise HTTPException(409, "请先结束当前录制或处理任务，再开始精准校验。")
        try:
            snapshot = refinement_snapshot(settings_store)
            settings = resolve_refinement_settings(settings_store, snapshot)
        except (ProviderError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        session = sessions.create(f"{source.get('title') or '直播录音'} · 精准校验")
        duration = round(sum(part["frames"] * 1000 / part["sample_rate"] for part in manifest))
        session.update(source_type="import", refinement_source_id=identifier,
                       refinement_source_parts=manifest, refinement_snapshot=snapshot,
                       original_filename="直播录音 · 精准校验", source_duration_ms=duration,
                       import_upload_complete=True, import_decoded=False,
                       import_transcription_mode="cloud", transcription_mode="cloud_file",
                       transcription_model=settings["import_transcription_model"],
                       import_transcription_api="minimax",
                       import_transcription_response_format=settings["import_transcription_response_format"],
                       translation_model=settings["translation_model"], import_transcribed_ms=0,
                       import_progress=0, import_error="",
                       subtitle_split_settings=subtitle_settings(settings))
        return imports.start(session)

    @app.post("/api/imports", status_code=202)
    async def import_file(request: Request):
        expire_reservations()
        if active or reservations or imports.is_active() or retry_locks or model_operations_active():
            raise HTTPException(409, "请先结束当前录制或处理任务，再导入音频。")
        try:
            settings, model = imports.validate_settings()
            filename, source_name = import_filename(request.headers.get("x-filename"))
        except (ImportAudioError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ImportAudioError) else "请检查翻译 API 设置后再导入。"
            raise HTTPException(400, message) from None
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/octet-stream" and not content_type.startswith(("audio/", "video/")):
            raise HTTPException(400, "请直接上传音频文件内容。")
        try:
            declared_size = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "上传文件大小无效。") from None
        if declared_size < 0:
            raise HTTPException(400, "上传文件大小无效。")
        if declared_size > MAX_IMPORT_BYTES:
            raise HTTPException(413, "音频文件超过 8 GiB，请先拆分文件后再导入。")
        title = unquote(request.headers.get("x-title", "")) or Path(filename).stem
        session = sessions.create(title)
        session.update(source_type="import", original_filename=filename, source_file=source_name,
                       status="processing", import_transcription_mode="cloud",
                       transcription_mode="cloud_file", transcription_model=model,
                       import_transcription_api=settings.get("import_transcription_api", "openai"),
                       import_transcription_response_format=settings.get("import_transcription_response_format", "verbose_json"),
                       translation_model=settings["translation_model"], import_stage="uploading",
                       import_progress=0, import_progress_label="正在保存上传的原音频", import_error="",
                       import_upload_complete=False, import_transcribed_ms=0,
                       subtitle_split_settings=subtitle_settings(settings))
        sessions.save(session)
        temporary = sessions.directory(session["id"]) / ".uploading-source"
        imports.uploading = True
        try:
            written = 0
            with temporary.open("xb") as target:
                os.chmod(temporary, 0o600)
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > MAX_IMPORT_BYTES:
                        raise HTTPException(413, "音频文件超过 8 GiB，请先拆分文件后再导入。")
                    if chunk:
                        await asyncio.to_thread(target.write, chunk)
                target.flush()
                os.fsync(target.fileno())
            if not written or declared_size and declared_size != written:
                raise ImportAudioError("音频上传为空或未完成，请重新导入完整文件。")
            os.replace(temporary, sessions.file_path(session["id"], source_name))
            session.update(import_upload_complete=True, source_size=written)
            sessions.save(session)
            session.update(await asyncio.to_thread(probe_audio, sessions.file_path(session["id"], source_name)))
            sessions.save(session)
        except asyncio.CancelledError:
            session.update(status="interrupted", import_stage="interrupted",
                           import_error="音频上传已中断，请重新导入完整文件。",
                           import_progress_label="上传已中断")
            sessions.save(session)
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, ImportAudioError) else "音频上传未完成，请重新导入完整文件。"
            session.update(status="failed", import_stage="failed", import_error=message,
                           import_progress_label="上传或文件校验未完成")
            sessions.export(session)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(400, message) from None
        finally:
            imports.uploading = False
        return imports.start(session)

    @app.get("/api/imports/{identifier}")
    async def import_detail(identifier: str):
        try:
            session = sessions.load(identifier)
            if session.get("source_type") != "import":
                raise ValueError("not an import")
            return imports.public(session)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "找不到这场音频导入。") from None

    @app.post("/api/imports/{identifier}/retry", status_code=202)
    async def retry_import(identifier: str):
        expire_reservations()
        if active or reservations or imports.is_active() or retry_locks or model_operations_active():
            raise HTTPException(409, "请先结束当前录制或处理任务，再继续导入。")
        try:
            session = sessions.load(identifier)
            if session.get("source_type") != "import":
                raise ValueError("not an import")
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "找不到这场音频导入。") from None
        imports.uploading = True
        try:
            imports.validate_settings(session)
            await asyncio.to_thread(imports.validate_source, session)
        except (ImportAudioError, ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, ImportAudioError) else "找不到完整原音频，请重新导入文件。"
            raise HTTPException(400, message) from None
        finally:
            imports.uploading = False
        return imports.start(session)

    @app.post("/api/sessions")
    async def new_session(body: SessionInput):
        expire_reservations()
        if active or reservations or imports.is_active() or retry_locks or model_operations_active():
            raise HTTPException(409, "已有录制或音频导入正在进行，请先结束当前任务。")
        if body.channels != 2 or body.sample_rate not in (24000, 44100, 48000, 88200, 96000):
            raise HTTPException(400, "浏览器音频采样率不受支持，请使用 Edge。")
        settings = settings_store.load()
        if modern_live:
            try:
                settings = resolve_live_settings(settings_store)
                state = runtime.status(backend=settings["live_asr_backend"], model=settings["live_asr_model"])
                if state.get("state") != "ready":
                    raise ProviderError("本机识别模型尚未就绪，请先点击「准备本机模型」完成加载和预热。")
                translation_state = None
                if settings.get("translation_backend") == "local":
                    translation_state = translation_runtime.status(settings["local_translation_model"])
                    if translation_state.get("state") != "ready":
                        raise ProviderError("本地翻译模型尚未就绪，请在直播设置中下载并加载所选翻译模型。")
                    settings["_local_translation_runtime"] = translation_runtime
            except (ProviderError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from None
            session = sessions.create(body.title, body.sample_rate, 2)
            session.update(source_type="live", status="preparing", transcription_mode="local",
                           transcription_backend=settings["live_asr_backend"],
                           transcription_model=settings["live_asr_model"],
                           translation_model=settings["translation_model"], translation_api=settings["translation_api"],
                           segment_seconds=settings["segment_seconds"],
                           translation_backend=settings.get("translation_backend", "cloud"),
                           live_snapshot=live_snapshot(settings, state, translation_state), live_committed_until_ms=0)
            sessions.save(session)
            token = secrets.token_urlsafe(32)
            reservations[session["id"]] = {"token": token, "created": time.monotonic(), "settings": settings, "durable": True}
            return dict(session, token=token)
        cloud_roles = ("translation",) if settings.get("transcription_mode") == "local" else ("transcription", "translation")
        if not all(all(effective_provider(settings, role).values()) for role in cloud_roles):
            label = "中文翻译" if settings.get("transcription_mode") == "local" else "语音转写和中文翻译"
            raise HTTPException(400, f"请先在 API 设置中填写{label}所用的 API 地址与密钥。")
        for role, label in (("transcription", "语音识别"), ("translation", "翻译")):
            if not str(settings.get(f"{role}_model") or "").strip():
                raise HTTPException(400, f"尚未选择{label}模型。请先从对应服务获取模型，或按服务商文档填写可用的模型名称。")
        try:
            validate_text_settings(settings)
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from None
        if (settings.get("transcription_mode") == "local" and local_transcriber_factory is LocalTranscriber
                and not local_model_ready(settings.get("transcription_model", "tiny"))):
            raise HTTPException(400, "本机语音模型尚未下载完成，请等待模型准备完成后开始录制。")
        session = sessions.create(body.title, body.sample_rate, 2)
        session.update({key: settings[key] for key in (
            "transcription_mode", "translation_api", "transcription_model", "translation_model", "segment_seconds",
            "correction_model", "correction_api",
        )})
        session["status"] = "preparing"
        sessions.save(session)
        token = secrets.token_urlsafe(32)
        reservations[session["id"]] = {"token": token, "created": time.monotonic(), "settings": settings}
        return dict(session, token=token)

    @app.get("/api/sessions/{identifier}/files/{name}")
    async def get_file(identifier: str, name: str):
        try:
            path = sessions.file_path(identifier, name)
            if not path.is_file():
                raise ValueError("missing")
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "文件不存在。") from None
        return FileResponse(path, filename=name)

    @app.post("/api/sessions/{identifier}/retry-translations")
    async def retry_translations(identifier: str):
        expire_reservations()
        if active or reservations or retry_locks or model_operations_active() or imports.is_active():
            raise HTTPException(409, "这场录制正在处理，请稍后再试。")
        try:
            session = sessions.load(identifier)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "找不到这场录制。") from None
        if session.get("source_type") != "import":
            legacy = not bool(session.get("live_snapshot"))
            try:
                settings = resolve_live_settings(settings_store, snapshot=session.get("live_snapshot"))
            except (ValueError, ProviderError) as exc:
                raise HTTPException(400, str(exc)) from None
            retry_locks.add(identifier)
            try:
                translation_state = None
                if settings.get("translation_backend") == "local":
                    translation_state = await translation_runtime.prepare(settings["local_translation_model"])
                    if translation_state.get("state") != "ready":
                        raise ProviderError("本场本地翻译模型无法准备，请检查模型文件和 MLX 环境。")
                    validate_translation_snapshot(session, translation_state)
                    settings["_local_translation_runtime"] = translation_runtime
                need_asr = not legacy and not session.get("legacy_translation_only") and (any(not s.get("ja") and s.get("asr_status") not in ("no_speech", "skipped")
                                               for s in session.get("segments", []))
                                         or session.get("live_recovery_from_ms", session.get("live_committed_until_ms", session.get("duration_ms", 0))) < session.get("duration_ms", 0))
                if need_asr:
                    state = await runtime.prepare(backend=settings["live_asr_backend"], model=settings["live_asr_model"])
                    if state.get("state") != "ready":
                        raise HTTPException(400, state.get("error") or "本场识别模型无法准备，请检查本机模型。")
                    validate_model_snapshot(session, state)
                if legacy:
                    session["live_snapshot"] = live_snapshot(settings, translation_state=translation_state)
                    # Old recordings preserve corrected Japanese and successful
                    # Chinese; first retry binds only their missing text stage.
                    session["legacy_translation_only"] = True
                    for segment in session.get("segments", []):
                        segment.setdefault("asr_status", "done" if segment.get("ja") else "skipped")
                    sessions.save(session)
                failures = await retry_live_subtitles(sessions, session, settings, runtime, translator_factory,
                                                      legacy=legacy or session.get("legacy_translation_only", False))
                if failures:
                    raise HTTPException(502, failures[0])
                return sessions.export(session)
            except ProviderError as exc:
                raise HTTPException(400, str(exc)) from None
            finally:
                retry_locks.discard(identifier)
        if session.get("refinement_source_id"):
            raise HTTPException(409, "请在精修项目中点击继续精准校验，保留原服务配置与处理进度。")
        settings = settings_store.load()
        provider = effective_provider(settings, "translation")
        if not provider["api_key"] or not provider["base_url"]:
            raise HTTPException(400, "请先填写翻译服务的 API 地址与密钥。")
        if not str(settings.get("translation_model") or "").strip():
            raise HTTPException(400, "请先选择翻译服务可用的模型。")
        retry_locks.add(identifier)
        translator = None
        workers = []
        try:
            try:
                validate_text_settings(settings)
            except ProviderError as exc:
                raise HTTPException(400, str(exc)) from None
            translator = await create_text_processor(settings, translator_factory)
            queue = asyncio.Queue()
            for segment in sorted(session["segments"], key=lambda item: item["start_ms"]):
                if not segment.get("zh") and segment.get("ja"):
                    queue.put_nowait(segment)
            failures = []

            async def retry_worker():
                while True:
                    try:
                        segment = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        context = sorted((s for s in session["segments"] if s.get("zh") and
                                          s["start_ms"] < segment["start_ms"]),
                                         key=lambda item: item["start_ms"])[-4:]
                        def checkpoint(value):
                            sessions.append_segment(session, value)
                            sessions.export(session)

                        await asyncio.wait_for(translate_segment(translator, segment, context, checkpoint=checkpoint),
                                               timeout=TRANSLATION_REQUEST_TIMEOUT)
                        segment["translation_status"] = "done"
                    except Exception as exc:
                        message = str(exc) if isinstance(exc, ProviderError) else "字幕翻译未完成，日语原文已保留，请稍后重试。"
                        segment["translation_status"] = "failed"
                        session["warnings"].append(message)
                        failures.append(message)
                    finally:
                        queue.task_done()
                    # export normalizes into new dictionaries; merge queued results by ID.
                    for current in session["segments"]:
                        if current["id"] == segment["id"]:
                            current.update(segment)
                            break
                    sessions.export(session)

            workers = [asyncio.create_task(retry_worker()) for _ in range(TRANSLATION_WORKERS)]
            await asyncio.gather(*workers)
            if failures:
                raise HTTPException(502, failures[0])
            return sessions.export(session)
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            try:
                if translator is not None:
                    await translator.close()
            finally:
                retry_locks.discard(identifier)

    @app.websocket("/ws/{identifier}")
    async def capture(socket: WebSocket, identifier: str, token: str = ""):
        host = socket.headers.get("host", "")
        origin = socket.headers.get("origin")
        reserved = reservations.get(identifier)
        if origin and not same_origin(origin, host):
            await socket.close(code=1008)
            return
        if not reserved or not secrets.compare_digest(token, reserved["token"]) or time.monotonic() - reserved["created"] > 60:
            await socket.close(code=1008)
            return
        reservations.pop(identifier)
        await socket.accept()
        settings = reserved["settings"]
        mode = settings.get("transcription_mode")
        factory = local_transcriber_factory if mode == "local" else chunked_transcriber_factory if mode == "segmented" else transcriber_factory
        live = (DurableLiveSession(sessions.load(identifier), settings, sessions, socket, runtime, translator_factory)
                if reserved.get("durable") else
                LiveSession(sessions.load(identifier), settings, sessions, socket, factory, translator_factory))
        active[identifier] = live
        interrupted = True
        try:
            await live.start()
            while True:
                packet = await socket.receive()
                if packet["type"] == "websocket.disconnect":
                    break
                if packet.get("bytes") is not None:
                    await live.receive_audio(packet["bytes"])
                elif packet.get("text"):
                    import json
                    control = json.loads(packet["text"])
                    if control.get("type") == "flush" and isinstance(live, DurableLiveSession):
                        await live.acknowledge_audio()
                    if control.get("type") == "caption_displayed" and isinstance(live, DurableLiveSession):
                        await live.acknowledge_caption(control)
                    if control.get("type") == "stop":
                        interrupted = False
                        break
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except (ValueError, OSError):
            await live.warning("采集连接或磁盘写入出现问题，已停止录制并保留现有文件。")
        finally:
            try:
                await live.finish(interrupted=interrupted)
            finally:
                active.pop(identifier, None)
                with contextlib.suppress(Exception):
                    await socket.close()

    app.include_router(space_download_router(space_downloads))
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
