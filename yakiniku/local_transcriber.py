"""Offline Japanese recognition. PCM and model execution stay on this Mac."""
from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from pathlib import Path

from .provider import ProviderError

MODEL_ROOT = Path(__file__).resolve().parent.parent / "data" / "models"
_MODELS = {}
_MODEL_LOCK = None


def local_model_path(model="tiny"):
    override = os.environ.get("YAKINIKU_LOCAL_MODEL_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    value = str(model or "tiny")
    path = Path(value).expanduser()
    return path if path.is_absolute() or path.is_dir() else MODEL_ROOT / value


def local_model_ready(model="tiny"):
    path = local_model_path(model)
    return path.is_dir() and (path / "model.bin").is_file()


def _load_model(path):
    import threading
    global _MODEL_LOCK
    if _MODEL_LOCK is None:
        _MODEL_LOCK = threading.Lock()
    with _MODEL_LOCK:
        key = str(path.resolve())
        if key not in _MODELS:
            from faster_whisper import WhisperModel
            _MODELS[key] = WhisperModel(key, device="cpu", compute_type="int8",
                                       cpu_threads=min(6, os.cpu_count() or 4),
                                       num_workers=1, local_files_only=True)
        return _MODELS[key]


class LocalTranscriber:
    sample_rate = 24000
    max_turn_bytes = 30 * sample_rate * 2

    def __init__(self, api_key="", model="tiny", prompt="", on_event=None, *,
                 base_url="", model_loader=None, max_queued=12):
        self.model = model or "tiny"
        self.prompt = prompt
        self._on_event = on_event
        self._loader = model_loader or _load_model
        self._model = None
        self._buffer = bytearray()
        self._queue = asyncio.Queue(maxsize=max_queued)
        self._jobs = {}
        self._drained = asyncio.Event()
        self._drained.set()
        self._worker_task = None
        self._closing = False
        self._had_errors = False
        self.failed = False
        self.failure_message = ""

    @property
    def pending_count(self):
        return len(self._jobs)

    async def _emit(self, event):
        if self._on_event is not None:
            await self._on_event(event)

    async def connect(self):
        if self._worker_task is not None or self._closing:
            raise ProviderError("本机识别已启动或关闭，请创建新的连接。")
        path = local_model_path(self.model)
        if self._loader is _load_model and not local_model_ready(self.model):
            raise ProviderError("本机语音模型尚未准备完成，请等待模型下载完成后再开始识别；原始录音仍会保存。")
        try:
            self._model = await asyncio.to_thread(self._loader, path)
        except ImportError:
            raise ProviderError("本机识别依赖尚未安装，请重新启动程序完成安装。") from None
        except Exception:
            raise ProviderError("本机语音模型加载失败，请检查模型文件是否下载完整。") from None
        self._worker_task = asyncio.create_task(self._worker())
        await self._emit({"type": "connection", "status": "connected", "mode": "local"})

    def _check_ready(self):
        if self.failed or self._closing or self._worker_task is None:
            raise ProviderError(self.failure_message or "本机语音识别尚未就绪。")

    async def append(self, pcm):
        self._check_ready()
        if len(pcm) % 2:
            raise ValueError("PCM16 data must contain complete samples")
        if len(self._buffer) + len(pcm) > self.max_turn_bytes:
            raise ProviderError("单次本机识别音频超过 30 秒，已保留原始音频。")
        self._buffer.extend(pcm)

    async def commit(self, start_ms, end_ms):
        self._check_ready()
        if end_ms <= start_ms:
            raise ValueError("Audio segment end must follow start")
        if not self._buffer:
            return
        if self._queue.full():
            self.failed = True
            self.failure_message = "本机识别处理积压，已暂停识别；完整录音仍在保存。"
            await self._emit({"type": "warning", "message": self.failure_message})
            raise ProviderError(self.failure_message)
        identifier = "local_" + uuid.uuid4().hex
        job = {"id": identifier, "start_ms": int(start_ms), "end_ms": int(end_ms),
               "pcm": bytes(self._buffer)}
        self._buffer.clear()
        self._jobs[identifier] = job
        self._drained.clear()
        self._queue.put_nowait(job)

    def _transcribe(self, pcm):
        import numpy as np
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        count = max(1, round(len(samples) * 16000 / 24000))
        audio = np.interp(np.arange(count) * 1.5, np.arange(len(samples)), samples).astype(np.float32)
        segments, _ = self._model.transcribe(
            audio, language="ja", task="transcribe", beam_size=1,
            condition_on_previous_text=False, vad_filter=False,
            initial_prompt=self.prompt[:1400] or None,
            no_speech_threshold=0.6,
        )
        return "".join(segment.text for segment in segments).strip()

    async def _worker(self):
        while True:
            job = await self._queue.get()
            try:
                try:
                    text = await asyncio.to_thread(self._transcribe, job["pcm"])
                except Exception:
                    self._had_errors = True
                    await self._emit({"type": "warning", "message": "一个本机识别片段处理失败，完整录音仍已保存。",
                                      "start_ms": job["start_ms"], "end_ms": job["end_ms"]})
                else:
                    if text:
                        await self._emit({"type": "transcript", "id": job["id"],
                                          "start_ms": job["start_ms"], "end_ms": job["end_ms"], "ja": text})
            finally:
                self._jobs.pop(job["id"], None)
                self._queue.task_done()
                if not self._jobs:
                    self._drained.set()

    async def finish(self, timeout=20):
        if self._buffer:
            return False
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._emit({"type": "warning", "message": "等待本机识别超时，未完成字幕可能缺失；完整音频已保存。"})
            return False
        return not self.failed and not self._had_errors

    async def close(self):
        if self._closing:
            return
        self._closing = True
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._buffer.clear()
        self._jobs.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._drained.set()
