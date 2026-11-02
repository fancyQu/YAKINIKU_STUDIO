"""Explicit, cancellable MLX downloads. Reading status never accesses the network."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

import httpx

from .model_runtime import MODEL_ROOT
from .provider import ProviderError


# Public repositories and pinned revisions are populated from provider metadata.
# This mapping is the entire download allowlist; HTTP input cannot add a URL.
DOWNLOAD_SPECS = {
    "mlx-whisper-small": {
        "repo_id": "mlx-community/whisper-small-mlx",
        "revision": "45f3915923c7a79a5a5b5a7d909d39aeb0e5630e",
        "files": (
            {"name": "config.json", "size_bytes": 266,
             "sha256": "e8f58e638208af66d5d5d67801259dc7a12d199e971967a9f9d33a8e3635668e"},
            {"name": "weights.npz", "size_bytes": 481307592,
             "sha256": "55b6674c9b339702d486e2b1573839a66f8ec8f821ed2886993ef717a86b09f5"},
        ),
    },
    "mlx-whisper-large-v3-turbo": {
        "repo_id": "mlx-community/whisper-large-v3-turbo",
        "revision": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
        "files": (
            {"name": "config.json", "size_bytes": 268,
             "sha256": "b34fc29e4e11e0a25e812775dd67f4dd16fc2c8eb43d28ae25ff7d660ecb6379"},
            {"name": "weights.safetensors", "size_bytes": 1613977612,
             "sha256": "951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6"},
        ),
    },
}
SUPPORTED_MODELS = frozenset({"mlx-whisper-small", "mlx-whisper-large-v3-turbo"})


def _complete(path):
    try:
        return ((path / "config.json").is_file() and (path / "config.json").stat().st_size > 0
                and any((path / name).is_file() and (path / name).stat().st_size > 0
                        for name in ("weights.npz", "weights.safetensors")))
    except OSError:
        return False


class ModelDownloadManager:
    """One explicit job at a time; installation becomes visible only when complete.

    ``downloader`` may be an async local fake accepting ``spec, file_spec,
    destination, on_progress``. Progress reports bytes written for that file.
    No construction, status read, cancellation or application startup downloads.
    """

    def __init__(self, *, root=None, downloader=None, specs=None):
        self.root = Path(root or MODEL_ROOT).resolve()
        self.specs = deepcopy(DOWNLOAD_SPECS if specs is None else specs)
        self.downloader = downloader or self._download_file
        self._states = {}
        self._tasks = {}

    def _spec(self, backend, model):
        if backend != "mlx" or model not in SUPPORTED_MODELS or model not in self.specs:
            raise ProviderError("请选择列表中支持手动下载的 MLX 模型。")
        return self.specs[model]

    def status(self, backend="mlx", model="mlx-whisper-small"):
        spec = self._spec(backend, model)
        path = self.root / model
        total = sum(item["size_bytes"] for item in spec["files"])
        installed = _complete(path)
        state = dict(self._states.get(model) or {})
        if installed and not self.is_active(model):
            state = {"state": "downloaded", "downloaded_bytes": total,
                     "message": "模型文件已在本机，可加载并预热；不会重复下载。", "error": ""}
        elif not state or state.get("state") == "downloaded":
            state = {"state": "missing", "downloaded_bytes": 0,
                     "message": "模型尚未下载。仅点击「下载模型」后才会联网下载。", "error": ""}
        state.update(backend=backend, model=model, path=str(path), total_bytes=total,
                     repo_id=spec["repo_id"], revision=spec["revision"], installed=installed,
                     progress=round(min(100, 100 * state.get("downloaded_bytes", 0) / max(1, total)), 1))
        return state

    def is_active(self, model=None):
        if model is not None:
            task = self._tasks.get(model)
            return task is not None and not task.done()
        return any(not task.done() for task in self._tasks.values())

    def active_status(self):
        """Return the active model even if another dropdown model is selected."""
        for model, task in self._tasks.items():
            if not task.done():
                return self.status("mlx", model)
        return None

    async def start(self, backend="mlx", model="mlx-whisper-small"):
        self._spec(backend, model)
        if self.is_active(model):
            return self.status(backend, model)
        if self.is_active():
            raise ProviderError("另一个 MLX 模型正在下载，请完成或取消后再下载。")
        if _complete(self.root / model):
            return self.status(backend, model)
        self._states[model] = {"state": "downloading", "downloaded_bytes": 0,
                               "message": "正在从 Hugging Face 下载所选模型。", "error": ""}
        self._tasks[model] = asyncio.create_task(self._run(model))
        return self.status(backend, model)

    async def cancel(self, backend="mlx", model="mlx-whisper-small"):
        self._spec(backend, model)
        task = self._tasks.get(model)
        if task is not None and not task.done():
            state = self._states[model]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Cancellation before the coroutine first runs must also publish
            # a terminal state; no staged directory exists in that case.
            # A new explicit start may already own this model after the old
            # task finished. Never overwrite its progress with the old cancel.
            if self._tasks.get(model) is task:
                state.update(state="cancelled", message="模型下载已取消，已有模型保持原样。", error="")
        return self.status(backend, model)

    async def shutdown(self):
        await asyncio.gather(*(self.cancel("mlx", model) for model in list(self._tasks)), return_exceptions=True)

    async def _download_file(self, spec, file_spec, destination, on_progress):
        url = f"https://huggingface.co/{spec['repo_id']}/resolve/{spec['revision']}/{file_spec['name']}"
        timeout = httpx.Timeout(45, connect=15)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"Accept-Encoding": "identity"}) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                written = 0
                with destination.open("xb") as stream:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        written += len(chunk)
                        if written > file_spec["size_bytes"]:
                            raise ProviderError("模型文件大小与固定版本记录不一致，请稍后重试。")
                        stream.write(chunk)
                        on_progress(written)
                    stream.flush()
                    os.fsync(stream.fileno())

    @staticmethod
    def _verify(directory, spec):
        weights_digest = ""
        for item in spec["files"]:
            path = directory / item["name"]
            if not path.is_file() or path.is_symlink() or path.stat().st_size != item["size_bytes"]:
                raise ProviderError("模型下载不完整，未安装残缺文件；请重新点击下载。")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(block)
            actual = digest.hexdigest()
            if item.get("sha256") and actual != item["sha256"]:
                raise ProviderError("模型文件 SHA256 校验失败，未安装；请重新下载。")
            if item["name"] in {"weights.npz", "weights.safetensors"}:
                weights_digest = actual
        try:
            config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
            if not isinstance(config, dict) or not config:
                raise ValueError("invalid configuration")
        except (OSError, ValueError, UnicodeError):
            raise ProviderError("模型配置文件无效，未安装；请重新下载。") from None
        return weights_digest

    async def _run(self, model):
        spec = self.specs[model]
        stage = None
        state = self._states[model]
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            destination = self.root / model
            if destination.is_symlink() or destination.exists():
                raise ProviderError("模型目录已存在但文件不完整；为保留原文件，请先检查或移开该目录后重试。")
            stage = Path(tempfile.mkdtemp(prefix=f".download-{model}-", dir=self.root))
            os.chmod(stage, 0o700)
            completed_bytes = 0
            for file_spec in spec["files"]:
                def progress(written, offset=completed_bytes):
                    state["downloaded_bytes"] = offset + written
                await self.downloader(spec, file_spec, stage / file_spec["name"], progress)
                completed_bytes += file_spec["size_bytes"]
                state["downloaded_bytes"] = completed_bytes
            state.update(state="verifying", message="模型已下载，正在校验文件完整性。")
            digest = await asyncio.to_thread(self._verify, stage, spec)
            source = {"repo_id": spec["repo_id"], "revision": spec["revision"],
                      "weights_sha256": digest, "format": "mlx-whisper",
                      "downloaded_by": "explicit_user_request"}
            (stage / "model-source.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
            # A second program may have installed it while this job downloaded.
            # Never replace another complete or partial model directory.
            if destination.exists() or destination.is_symlink():
                raise ProviderError("模型目录在下载期间发生变化，原文件已保留；请重新检查模型状态。")
            os.rename(stage, destination)
            stage = None
            state.update(state="downloaded", message="模型下载完成并通过校验。请点击加载并预热。", error="")
        except asyncio.CancelledError:
            state.update(state="cancelled", message="模型下载已取消，已有模型保持原样。", error="")
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "模型下载失败。请检查网络与可用磁盘空间，再手动重试。"
            state.update(state="failed", message=message, error=message)
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
