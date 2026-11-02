"""Offline, prewarmed live ASR on one process-wide inference thread.

The executor owns model loading, warmup and inference. Cancelling an asyncio
caller never cancels the native inference or releases its model. The next job
therefore cannot start a second GPU inference while an old job is still running.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import importlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import threading
import time

from .provider import ProviderError


MODEL_ROOT = Path(__file__).resolve().parents[1] / "data" / "models"
MODEL_CHOICES = (
    ("mlx", "mlx-whisper-small", "Small 多语言 · MLX / Apple GPU"),
    ("mlx", "mlx-whisper-large-v3-turbo", "Large v3 Turbo · MLX / Apple GPU"),
    ("tiny", "tiny", "Tiny · CPU 兼容模式"),
)


def model_path(backend="mlx", model="mlx-whisper-small", root=None):
    if backend not in {"mlx", "tiny", "local"}:
        raise ProviderError("不支持的本机识别后端，请明确选择 MLX 或 Tiny。")
    selected = str(model or "")
    path = Path(selected).expanduser()
    if path.is_absolute():
        return path.resolve()
    allowed = {item[1] for item in MODEL_CHOICES}
    if selected not in allowed or (backend in {"tiny", "local"} and selected != "tiny"):
        raise ProviderError("请选择已安装的本机模型，或填写模型的绝对路径。")
    return ((root or MODEL_ROOT) / selected).resolve()


def _files_present(path, backend):
    if backend in {"tiny", "local"}:
        return (path / "model.bin").is_file()
    return ((path / "config.json").is_file() and
            any((path / name).is_file() for name in ("weights.npz", "weights.safetensors")))


def _source(path):
    try:
        value = json.loads((path / "model-source.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        return {key: value[key] for key in ("repo_id", "revision", "weights_sha256", "format")
                if isinstance(value.get(key), str)}
    except (OSError, ValueError, TypeError):
        return {}


def _verify_source(path, backend):
    source = _source(path)
    if backend in {"tiny", "local"}:
        weights = path / "model.bin"
    else:
        weights = path / "weights.safetensors"
        if not weights.is_file():
            weights = path / "weights.npz"
    before = weights.stat()
    source.update(weights_file=weights.name, weights_bytes=before.st_size)
    expected = source.get("weights_sha256", "").lower()
    if backend in {"tiny", "local"} and not expected:
        # Keep old Tiny installations usable without an invented provenance claim.
        source.update(weights_mtime_ns=before.st_mtime_ns, weights_verified=False)
        return source
    if expected and (len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected)):
        raise ProviderError("模型来源记录的 SHA256 无效，请检查模型准备记录。")
    digest = hashlib.sha256()
    with weights.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    after = weights.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ProviderError("模型权重在校验时发生变化，请停止修改模型文件后重新准备。")
    actual = digest.hexdigest()
    if expected and actual != expected:
        raise ProviderError("模型权重 SHA256 与准备记录不一致，请重新准备完整模型；不会开始识别。")
    source.update(weights_sha256=actual, weights_verified=True)
    return source


def pcm_float32(pcm, sample_rate=24000, channels=1):
    """Convert the recognition copy, leaving the saved original PCM untouched."""
    import numpy as np

    if (not isinstance(pcm, (bytes, bytearray, memoryview)) or
            sample_rate <= 0 or channels <= 0 or len(pcm) % (2 * channels)):
        raise ProviderError("本机识别音频格式无效，需要完整的 PCM16 采样。")
    if not pcm:
        raise ProviderError("本机识别收到空音频，片段可稍后重试。")
    audio = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels).astype(np.float32).mean(axis=1) / 32768.0
    if sample_rate != 16000:
        try:
            from scipy.signal import resample_poly
        except ImportError:
            # The original Tiny environment intentionally has no SciPy dependency.
            count = max(1, round(len(audio) * 16000 / sample_rate))
            audio = np.interp(np.arange(count) * sample_rate / 16000,
                              np.arange(len(audio)), audio).astype(np.float32)
        else:
            divisor = math.gcd(sample_rate, 16000)
            audio = resample_poly(audio, 16000 // divisor, sample_rate // divisor).astype(np.float32)
    return audio


class _MLXEngine:
    def __init__(self, path):
        # Local validated paths and Hub offline mode prohibit implicit downloads.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        os.environ.setdefault("NUMBA_CACHE_DIR", str(MODEL_ROOT.parent / "runtime-cache" / "numba"))
        import mlx.core as mx
        import mlx_whisper
        if not mx.metal.is_available():
            raise ProviderError("当前进程无法使用 Apple Metal GPU；请使用 MLX 启动环境并检查 GPU 访问权限。")
        mx.set_default_device(mx.gpu)
        probe = mx.sum(mx.array([1.0, 2.0, 3.0]) ** 2)
        mx.eval(probe)
        if probe.item() != 14.0:
            raise ProviderError("Apple GPU 检查失败，模型尚未就绪。")
        self.mx = mx
        self.module = mlx_whisper
        self.path = str(path)
        self.version = version("mlx-whisper")

    def transcribe(self, audio, prompt=""):
        return self.module.transcribe(
            audio, path_or_hf_repo=self.path, language="ja", task="transcribe",
            temperature=0.0, condition_on_previous_text=False,
            word_timestamps=False, verbose=None, initial_prompt=prompt[:1400] or None,
        )

    def close(self):
        holder = importlib.import_module("mlx_whisper.transcribe").ModelHolder
        holder.model = None
        holder.model_path = None
        self.mx.clear_cache()


class _TinyEngine:
    def __init__(self, path):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(str(path), device="cpu", compute_type="int8",
                                  cpu_threads=min(6, os.cpu_count() or 4),
                                  num_workers=1, local_files_only=True)
        self.version = version("faster-whisper")

    def transcribe(self, audio, prompt=""):
        segments, info = self.model.transcribe(
            audio, language="ja", task="transcribe", beam_size=1,
            condition_on_previous_text=False, vad_filter=False,
            initial_prompt=prompt[:1400] or None, no_speech_threshold=0.6)
        rows = [{"text": row.text, "no_speech_prob": row.no_speech_prob,
                 "avg_logprob": row.avg_logprob} for row in segments]
        return {"text": "".join(row["text"] for row in rows), "segments": rows,
                "language": getattr(info, "language", "ja")}


def normalize_result(result):
    """Only this adapter sees Whisper's third-party dictionary structure."""
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise ProviderError("本机识别返回了无效结果，已保留音频以便重试。")
    text = result["text"].strip()
    rows = result.get("segments", [])
    if not isinstance(rows, list):
        raise ProviderError("本机识别返回了无效分段，已保留音频以便重试。")
    # A high no-speech probability alone must not suppress a confident short reply.
    silence = bool(rows) and all(
        isinstance(row, dict) and isinstance(row.get("no_speech_prob"), (int, float))
        and row["no_speech_prob"] > 0.6
        and isinstance(row.get("avg_logprob"), (int, float)) and row["avg_logprob"] < -1.0
        for row in rows)
    if silence:
        return {"status": "no_speech", "ja": ""}
    if not text:
        raise ProviderError("有声片段未返回识别文字，已保留音频，需稍后重试。")
    if len(text) > 40 and any(
        isinstance(row, dict) and isinstance(row.get("compression_ratio"), (int, float))
        and row["compression_ratio"] > 3.5 for row in rows
    ):
        raise ProviderError("本机识别结果出现异常重复，已保留音频，需稍后核对或重试。")
    return {"status": "done", "ja": text}


class LiveModelRuntime:
    def __init__(self, *, root=None, loaders=None, max_pending=4):
        self.root = root
        self._loaders = loaders or {"mlx": _MLXEngine, "tiny": _TinyEngine}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-asr")
        self._lock = threading.RLock()
        self._states = {}
        self._active_key = None
        self._engine = None
        self._preparing = {}
        self._verified_sources = {}
        self._pending = 0
        self.max_pending = max_pending

    @property
    def inference_executor(self):
        """Share native GPU scheduling with local text generation, keeping one job active."""
        return self._executor

    def _key(self, backend, model):
        backend = "tiny" if backend == "local" else backend
        return backend, str(model_path(backend, model, self.root))

    def status(self, backend="mlx", model="mlx-whisper-small"):
        key = self._key(backend, model)
        path = Path(key[1])
        with self._lock:
            state = dict(self._states.get(key, {"state": "not_prepared", "error": ""}))
            if state["state"] == "ready" and self._active_key != key:
                state.update(state="not_prepared", error="")
            state.update(backend=key[0], model=model, path=key[1],
                         prepared_files=_files_present(path, key[0]),
                         source=dict(self._verified_sources.get(key) or _source(path)), pending_count=self._pending)
            state.setdefault("version", "")
            return state

    def describe_models(self):
        return [{**self.status(backend, model), "label": label}
                for backend, model, label in MODEL_CHOICES]

    def _submit(self, callback, *args):
        with self._lock:
            if self._pending >= self.max_pending:
                raise ProviderError("本机识别任务已满，片段应保留并在停止后补全。")
            self._pending += 1
        try:
            future = self._executor.submit(callback, *args)
        except Exception:
            with self._lock:
                self._pending -= 1
            raise
        def completed(_):
            with self._lock:
                self._pending -= 1
        future.add_done_callback(completed)
        return future

    async def prepare(self, backend="mlx", model="mlx-whisper-small"):
        key = self._key(backend, model)
        with self._lock:
            if self.status(backend, model)["state"] == "ready":
                return self.status(backend, model)
            future = self._preparing.get(key)
            if future is None or future.done():
                self._states[key] = {"state": "loading", "error": ""}
                self._verified_sources.pop(key, None)
                future = self._submit(self._prepare, key)
                self._preparing[key] = future
        await asyncio.shield(asyncio.wrap_future(future))
        return self.status(backend, model)

    def _prepare(self, key):
        import numpy as np
        started = time.perf_counter()
        engine = None
        prepared = False
        try:
            # This runs after any prior inference on the same single worker.
            # A failed same-model engine must also release Whisper's native
            # cache before its replacement can be constructed.
            with self._lock:
                previous = self._engine
                self._engine = None
                self._active_key = None
            if previous is not None:
                close = getattr(previous, "close", None)
                if close:
                    close()
                del previous
            if not _files_present(Path(key[1]), key[0]):
                raise ProviderError("本机模型文件未下载，请先在直播设置点击「下载模型」，完成后再加载并预热；录制时不会下载或回退到其他模型。")
            verified_source = _verify_source(Path(key[1]), key[0])
            engine = self._loaders[key[0]](Path(key[1]))
            engine.transcribe(np.zeros(16000, dtype=np.float32), "")
            with self._lock:
                self._engine = engine
                self._active_key = key
                self._verified_sources[key] = verified_source
                self._states[key] = {"state": "ready", "error": "",
                                     "version": getattr(engine, "version", ""),
                                     "warmup_ms": round((time.perf_counter() - started) * 1000, 2)}
                prepared = True
        except ProviderError as exc:
            with self._lock:
                self._states[key] = {"state": "failed", "error": str(exc)}
            raise
        except ImportError:
            message = "本机识别依赖未安装，请使用 .venv-mlx 的 Python 启动程序。"
            with self._lock:
                self._states[key] = {"state": "failed", "error": message}
            raise ProviderError(message) from None
        except Exception:
            message = "本机模型加载或预热失败，请检查模型完整性、MLX 环境及 GPU 权限。"
            with self._lock:
                self._states[key] = {"state": "failed", "error": message}
            raise ProviderError(message) from None
        finally:
            if not prepared:
                with self._lock:
                    self._engine = None
                    self._active_key = None
                    self._verified_sources.pop(key, None)
                if engine is not None:
                    close = getattr(engine, "close", None)
                    if close:
                        # Keep the original sanitized load/warmup failure.
                        with contextlib.suppress(Exception):
                            close()

    async def transcribe_pcm(self, pcm, sample_rate=24000, channels=1, *,
                             backend="mlx", model="mlx-whisper-small", prompt=""):
        key = self._key(backend, model)
        if self.status(backend, model)["state"] != "ready":
            raise ProviderError("本机模型尚未加载并预热，请先准备模型。")
        future = self._submit(self._transcribe, key, bytes(pcm), sample_rate, channels, prompt)
        return await asyncio.shield(asyncio.wrap_future(future))

    def _transcribe(self, key, pcm, sample_rate, channels, prompt):
        import numpy as np
        started = time.perf_counter()
        if self._active_key != key or self._engine is None:
            raise ProviderError("本场模型已不在就绪状态，请重新准备相同模型。")
        audio = pcm_float32(pcm, sample_rate, channels)
        if not np.isfinite(audio).all():
            raise ProviderError("识别音频含无效采样，已保留原始录音。")
        if float(np.sqrt(np.mean(audio ** 2))) < 0.0003:
            result = {"status": "no_speech", "ja": ""}
        else:
            try:
                result = normalize_result(self._engine.transcribe(audio, prompt))
            except ProviderError:
                raise
            except Exception:
                message = "本机识别处理失败，音频仍保存；请重新准备模型后重试。"
                with self._lock:
                    self._states[key] = {"state": "failed", "error": message}
                raise ProviderError(message) from None
        return {**result, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}

    def shutdown(self, wait=True):
        self._executor.shutdown(wait=wait, cancel_futures=False)


_RUNTIME = None
_RUNTIME_LOCK = threading.Lock()


def get_live_model_runtime():
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = LiveModelRuntime()
        return _RUNTIME
