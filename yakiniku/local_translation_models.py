"""Pinned, explicitly downloaded local translation models; safe to import on CPU.

This module never imports MLX/Transformers. Status and catalog reads perform no
network requests. Only a user's explicit start() may download allowlisted files.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from urllib.parse import quote

import httpx

from .model_downloads import ModelDownloadManager
from .model_runtime import MODEL_ROOT
from .provider import ProviderError


TRANSLATION_MODEL_ROOT = MODEL_ROOT / "translation"
DEFAULT_MODEL = "qwen3-4b-instruct-2507-4bit"
DOWNLOAD_SOURCE = "modelscope"
SOURCE_LABEL = "阿里 ModelScope（国内源）"
MEMORY_ESTIMATE_NOTE = (
    "估算：4bit 模型、单请求、短上下文（约 2K tokens 以内），含 KV 缓存和推理运行时；"
    "不含语音识别模型、系统和浏览器。实际峰值需本机测量，不保证延迟。"
)
_MODEL_DESCRIPTIONS = (
    {"id": "qwen3-4b-instruct-2507-4bit", "label": "Qwen3 4B Instruct 2507 · 4bit · 推荐",
     "estimated_runtime_memory_gb": [3.5, 5.5], "recommended": True,
     "description": "非思考指令模型；优先作为直播本机日译中候选，需与 ASR 同机实测。"},
    {"id": "qwen2.5-3b-instruct-4bit", "label": "Qwen2.5 3B Instruct · 4bit · 省内存",
     "estimated_runtime_memory_gb": [2.5, 4.0], "recommended": False,
     "description": "较省内存的折中候选；需人工核对日语口语、人名和省略句。"},
    {"id": "qwen2.5-1.5b-instruct-4bit", "label": "Qwen2.5 1.5B Instruct · 4bit · 最省内存",
     "estimated_runtime_memory_gb": [1.5, 2.5], "recommended": False,
     "description": "资源占用较低；复杂口语、人物称谓和省略句的翻译质量更需核对。"},
    {"id": "qwen2.5-7b-instruct-4bit", "label": "Qwen2.5 7B Instruct · 4bit · 质量对照",
     "estimated_runtime_memory_gb": [5.5, 8.0], "recommended": False,
     "description": "用于质量对照；与本机 ASR 共用 GPU，16GB 设备须测峰值内存和排队。"},
)

# Pinned ModelScope file trees were checked against every required file's size
# and SHA256. Only MLX weights/tokenizer/configuration are downloaded, no code.
DOWNLOAD_SPECS = {'qwen2.5-3b-instruct-4bit': {'repo_id': 'mlx-community/Qwen2.5-3B-Instruct-4bit',
                              'revision': '14c8e78c967af2d31c54c2b6840547ee057ccc5a',
                              'files': ({'name': 'added_tokens.json',
                                         'size_bytes': 605,
                                         'sha256': '58b54bbe36fc752f79a24a271ef66a0a0830054b4dfad94bde757d851968060b'},
                                        {'name': 'config.json',
                                         'size_bytes': 785,
                                         'sha256': 'ceb97c46fe17f523ac42c0f0254fd18d9589f8725f0bbbe0bef541f8ef84547a'},
                                        {'name': 'merges.txt',
                                         'size_bytes': 1671853,
                                         'sha256': '8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5'},
                                        {'name': 'model.safetensors',
                                         'size_bytes': 1736293090,
                                         'sha256': 'f212cf6fb9923281a09c135e05d43a052ee5ef7121f5b1dc0b0fb2de80f97cfd'},
                                        {'name': 'model.safetensors.index.json',
                                         'size_bytes': 66290,
                                         'sha256': '7edb73f1af3c860b1344ece4abe04b258246afdfeba4cb39d3b84a0459890bee'},
                                        {'name': 'special_tokens_map.json',
                                         'size_bytes': 613,
                                         'sha256': '76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd'},
                                        {'name': 'tokenizer.json',
                                         'size_bytes': 7031673,
                                         'sha256': 'a8506e7111b80c6d8635951a02eab0f4e1a8e4e5772da83846579e97b16f61bf'},
                                        {'name': 'tokenizer_config.json',
                                         'size_bytes': 7308,
                                         'sha256': 'f7c61e32b7a17d19bf8e7037dcb74079a833e53ea9801f24008cac68458f03b7'},
                                        {'name': 'vocab.json',
                                         'size_bytes': 2776833,
                                         'sha256': 'ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910'})},
 'qwen2.5-1.5b-instruct-4bit': {'repo_id': 'mlx-community/Qwen2.5-1.5B-Instruct-4bit',
                                'revision': '8701bd22cfa15a4d828a301fe33959feb3480052',
                                'files': ({'name': 'added_tokens.json',
                                           'size_bytes': 605,
                                           'sha256': '58b54bbe36fc752f79a24a271ef66a0a0830054b4dfad94bde757d851968060b'},
                                          {'name': 'config.json',
                                           'size_bytes': 784,
                                           'sha256': '636d3e2a15e8914b8cf82b05cc2288a811f9bd93c3bf1afc00cab701a70b47c0'},
                                          {'name': 'merges.txt',
                                           'size_bytes': 1671853,
                                           'sha256': '8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5'},
                                          {'name': 'model.safetensors',
                                           'size_bytes': 868628559,
                                           'sha256': '0979f33d1bc58afcf696d13f57977644e7b11a6f0eec3e631d8e9463d18c0717'},
                                          {'name': 'model.safetensors.index.json',
                                           'size_bytes': 51569,
                                           'sha256': '6b98634d5044f0e2ad45228a374f8445904e571f1082392d08a6ce54f5d517ca'},
                                          {'name': 'special_tokens_map.json',
                                           'size_bytes': 613,
                                           'sha256': '76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd'},
                                          {'name': 'tokenizer.json',
                                           'size_bytes': 7031673,
                                           'sha256': 'a8506e7111b80c6d8635951a02eab0f4e1a8e4e5772da83846579e97b16f61bf'},
                                          {'name': 'tokenizer_config.json',
                                           'size_bytes': 7308,
                                           'sha256': 'f7c61e32b7a17d19bf8e7037dcb74079a833e53ea9801f24008cac68458f03b7'},
                                          {'name': 'vocab.json',
                                           'size_bytes': 2776833,
                                           'sha256': 'ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910'})},
 'qwen2.5-7b-instruct-4bit': {'repo_id': 'mlx-community/Qwen2.5-7B-Instruct-4bit',
                              'revision': 'c21a57bf298cb8ab1de2c26b74cbf27984b29d6c',
                              'files': ({'name': 'added_tokens.json',
                                         'size_bytes': 605,
                                         'sha256': '58b54bbe36fc752f79a24a271ef66a0a0830054b4dfad94bde757d851968060b'},
                                        {'name': 'config.json',
                                         'size_bytes': 787,
                                         'sha256': '1661a349986919d13820d3981623776138d50783d13e247cdc5b075a22b62698'},
                                        {'name': 'merges.txt',
                                         'size_bytes': 1671853,
                                         'sha256': '8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5'},
                                        {'name': 'model.safetensors',
                                         'size_bytes': 4284346255,
                                         'sha256': '86110f368236b53cf4c2336f991a85703b17bcc60bb75f292b4002ec0219f071'},
                                        {'name': 'model.safetensors.index.json',
                                         'size_bytes': 51711,
                                         'sha256': '23cd562592dd96686d2e799eb02e4b578a4038b6045eaefe860d270cc060e24f'},
                                        {'name': 'special_tokens_map.json',
                                         'size_bytes': 613,
                                         'sha256': '76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd'},
                                        {'name': 'tokenizer.json',
                                         'size_bytes': 7031673,
                                         'sha256': 'a8506e7111b80c6d8635951a02eab0f4e1a8e4e5772da83846579e97b16f61bf'},
                                        {'name': 'tokenizer_config.json',
                                         'size_bytes': 7280,
                                         'sha256': '4921d4cbae33349a9cb3b2ad3a7622efdea10a5d208c9f333c77b154aa714888'},
                                        {'name': 'vocab.json',
                                         'size_bytes': 2776833,
                                         'sha256': 'ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910'})},
 'qwen3-4b-instruct-2507-4bit': {'repo_id': 'mlx-community/Qwen3-4B-Instruct-2507-4bit',
                                 'revision': 'ec7ca96008a45bebdd03ad3d09d86f6e61a3e4af',
                                 'files': ({'name': 'added_tokens.json',
                                            'size_bytes': 707,
                                            'sha256': 'c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680'},
                                           {'name': 'chat_template.jinja',
                                            'size_bytes': 4040,
                                            'sha256': '40c21f34cf67d8c760ef72f8ad3ae5afad514299d4b06e91dd9a8d705af7b541'},
                                           {'name': 'config.json',
                                            'size_bytes': 938,
                                            'sha256': '574349e5a343236546fda55e4744a76e181f534182d7dc60ff1bad7e7a502849'},
                                           {'name': 'generation_config.json',
                                            'size_bytes': 238,
                                            'sha256': '835fffe355c9438e7a25be099b3fccaa98350b83451f9fd2d99512e74f1ade48'},
                                           {'name': 'merges.txt',
                                            'size_bytes': 1671853,
                                            'sha256': '8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5'},
                                           {'name': 'model.safetensors',
                                            'size_bytes': 2263022417,
                                            'sha256': '2a73c6c248601ab904e035548abd8e6abb65ea27dcb5f342fb0a8910eb44173f'},
                                           {'name': 'model.safetensors.index.json',
                                            'size_bytes': 63964,
                                            'sha256': '388d811b8b7c2608dd04cce1bcb04a8bf715d19b42790894e6d3427ff429a777'},
                                           {'name': 'special_tokens_map.json',
                                            'size_bytes': 613,
                                            'sha256': '76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd'},
                                           {'name': 'tokenizer.json',
                                            'size_bytes': 11422654,
                                            'sha256': 'aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4'},
                                           {'name': 'tokenizer_config.json',
                                            'size_bytes': 5440,
                                            'sha256': '4397cc477eb6d79715ccd2000accd6b3531928f30029665832fa1b255f24d2b9'},
                                           {'name': 'vocab.json',
                                            'size_bytes': 2776833,
                                            'sha256': 'ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910'})}}

for _spec in DOWNLOAD_SPECS.values():
    _spec.update(download_source=DOWNLOAD_SOURCE, source_label=SOURCE_LABEL,
                 source_url=f"https://modelscope.cn/models/{_spec['repo_id']}")


def download_source(spec):
    return {"download_source": DOWNLOAD_SOURCE, "source_label": SOURCE_LABEL,
            "source_url": f"https://modelscope.cn/models/{spec['repo_id']}"}


def modelscope_file_url(spec, file_spec):
    """Official ModelScope repo API; no Hub fallback or arbitrary download URL."""
    if spec.get("download_source", DOWNLOAD_SOURCE) != DOWNLOAD_SOURCE:
        raise ProviderError("Qwen 翻译模型仅从阿里 ModelScope 国内源下载。")
    repo = quote(spec["repo_id"], safe="/")
    return httpx.URL(f"https://modelscope.cn/api/v1/models/{repo}/repo",
                     params={"Revision": spec["revision"], "FilePath": file_spec["name"]})


async def require_modelscope_host(request):
    # Check every redirect before sending it. ModelScope may serve LFS via its
    # own CDN or Alibaba OSS, but must never redirect a Qwen request to HF.
    url = request.url
    host = url.host.lower()
    allowed = any(host == suffix or host.endswith("." + suffix)
                  for suffix in ("modelscope.cn", "aliyuncs.com"))
    if url.scheme != "https" or not allowed or url.username or url.password:
        raise ProviderError("ModelScope 下载返回了非阿里来源，已停止；不会回退到 Hugging Face。")


def _catalog():
    result = []
    for description in _MODEL_DESCRIPTIONS:
        item = deepcopy(description)
        spec = DOWNLOAD_SPECS.get(item["id"], {})
        item.update(backend="mlx", repo_id=spec.get("repo_id", ""),
                    revision=spec.get("revision", ""), quantization_bits=4,
                    disk_bytes=sum(row["size_bytes"] for row in spec.get("files", [])),
                    memory_estimate_note=MEMORY_ESTIMATE_NOTE)
        item.update(download_source(spec))
        result.append(item)
    return tuple(result)


MODEL_CATALOG = _catalog()
SUPPORTED_MODELS = frozenset(item["id"] for item in _MODEL_DESCRIPTIONS)


def describe_models():
    return deepcopy(list(MODEL_CATALOG))


def model_spec(model=DEFAULT_MODEL, specs=None):
    available = DOWNLOAD_SPECS if specs is None else specs
    if model not in SUPPORTED_MODELS or model not in available:
        raise ProviderError("请选择列表中的本机翻译模型。")
    return available[model]


def model_path(model=DEFAULT_MODEL, root=None):
    if model not in SUPPORTED_MODELS:
        raise ProviderError("请选择列表中的本机翻译模型，不能使用任意路径或仓库。")
    return Path(root or TRANSLATION_MODEL_ROOT).resolve() / model


def files_present(path, spec):
    """Cheap presence/size checks only; readiness requires explicit full hashing."""
    path = Path(path)
    try:
        if not path.is_dir() or path.is_symlink():
            return False
        return bool(spec.get("files")) and all(
            (path / item["name"]).is_file() and not (path / item["name"]).is_symlink()
            and (path / item["name"]).stat().st_size == item["size_bytes"]
            for item in spec["files"])
    except OSError:
        return False


def verify_files(path, spec, cancellation=None):
    if not files_present(path, spec):
        raise ProviderError("本机翻译模型文件不完整，未安装或加载残缺文件，请重新下载。")
    verified = []
    for item in spec["files"]:
        if cancellation is not None and cancellation.is_set():
            raise InterruptedError("model verification cancelled")
        file_path = Path(path) / item["name"]
        before = file_path.stat()
        digest = hashlib.sha256()
        with file_path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                if cancellation is not None and cancellation.is_set():
                    raise InterruptedError("model verification cancelled")
                digest.update(block)
        after = file_path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ProviderError("本机翻译模型文件在校验期间变化，请停止修改后重新准备。")
        if digest.hexdigest() != item["sha256"]:
            raise ProviderError("本机翻译模型 SHA256 校验失败，请重新下载完整模型。")
        verified.append({"name": item["name"], "size_bytes": item["size_bytes"],
                         "sha256": digest.hexdigest()})
    for name in ("config.json", "tokenizer_config.json"):
        try:
            value = json.loads((Path(path) / name).read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not value:
                raise ValueError("invalid configuration")
        except (OSError, ValueError, UnicodeError):
            raise ProviderError("本机翻译模型配置无效，请重新下载完整模型。") from None
    source = {"repo_id": spec["repo_id"], "revision": spec["revision"],
              "format": "mlx-lm", "files": verified,
              "files_sha256": {item["name"]: item["sha256"] for item in verified},
              "weights_verified": True}
    source.update(download_source(spec))
    weights = [item for item in verified if item["name"].endswith(".safetensors")]
    if len(weights) == 1:
        source["weights_sha256"] = weights[0]["sha256"]
    return source


class TranslationModelDownloadManager(ModelDownloadManager):
    """Reuse ASR's HTTP transport/cancel API, with LLM-specific atomic install.

    downloader(spec,file_spec,destination,on_progress) can be a local async fake.
    No root directory is created until explicit start().
    """

    def __init__(self, *, root=None, downloader=None, specs=None):
        super().__init__(root=root or TRANSLATION_MODEL_ROOT, downloader=downloader,
                         specs=DOWNLOAD_SPECS if specs is None else specs)

    def _spec(self, backend, model):
        if backend != "mlx":
            raise ProviderError("本机翻译模型下载仅支持 MLX 后端。")
        return model_spec(model, self.specs)

    def status(self, backend="mlx", model=DEFAULT_MODEL):
        spec = self._spec(backend, model)
        path = model_path(model, self.root)
        total = sum(item["size_bytes"] for item in spec["files"])
        installed = files_present(path, spec)
        state = dict(self._states.get(model) or {})
        if installed and not self.is_active(model):
            state = {"state": "downloaded", "downloaded_bytes": total,
                     "message": "翻译模型文件已在本机，可加载并预热；不会重复下载。", "error": ""}
        elif not state or state.get("state") == "downloaded":
            state = {"state": "missing", "downloaded_bytes": 0,
                     "message": "翻译模型尚未下载。仅点击下载后才会联网。", "error": ""}
        state.update(backend="mlx", model=model, path=str(path), total_bytes=total,
                     disk_bytes=total, repo_id=spec["repo_id"], revision=spec["revision"],
                     installed=installed,
                     progress=round(min(100, 100 * state.get("downloaded_bytes", 0) / max(1, total)), 1))
        state.update(download_source(spec))
        return state

    def describe_models(self):
        return [{**item, **self.status("mlx", item["id"])} for item in describe_models()
                if item["id"] in self.specs]

    async def start(self, backend="mlx", model=DEFAULT_MODEL):
        spec = self._spec(backend, model)
        if self.is_active(model):
            return self.status(backend, model)
        if self.is_active():
            raise ProviderError("另一个本机翻译模型正在下载，请完成或取消后再下载。")
        if files_present(model_path(model, self.root), spec):
            return self.status(backend, model)
        self._states[model] = {"state": "downloading", "downloaded_bytes": 0,
                               "message": "正在从阿里 ModelScope 国内源下载所选翻译模型。", "error": ""}
        self._tasks[model] = asyncio.create_task(self._run(model))
        return self.status(backend, model)

    async def cancel(self, backend="mlx", model=DEFAULT_MODEL):
        return await super().cancel(backend, model)

    async def _download_file(self, spec, file_spec, destination, on_progress):
        url = modelscope_file_url(spec, file_spec)
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                        timeout=httpx.Timeout(45, connect=15), follow_redirects=True,
                        max_redirects=5, headers={"Accept-Encoding": "identity"},
                        event_hooks={"request": [require_modelscope_host]}) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        written = 0
                        with destination.open("xb") as stream:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                written += len(chunk)
                                if written > file_spec["size_bytes"]:
                                    raise ProviderError("ModelScope 文件大小与固定版本不一致，请稍后重试。")
                                stream.write(chunk)
                                on_progress(written)
                            stream.flush()
                            os.fsync(stream.fileno())
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (
                        408, 429, 500, 502, 503, 504):
                    raise
                if attempt == 2:
                    raise
                destination.unlink(missing_ok=True)
                on_progress(0)
                await asyncio.sleep(.5 * (2 ** attempt))

    async def _run(self, model):
        spec = self.specs[model]
        stage = None
        state = self._states[model]
        cancellation = threading.Event()
        verification = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            destination = model_path(model, self.root)
            if destination.exists() or destination.is_symlink():
                raise ProviderError("翻译模型目录已存在但文件不完整；为保留原文件，请先检查或移开目录后重试。")
            stage = Path(tempfile.mkdtemp(prefix=f".download-{model}-", dir=self.root))
            os.chmod(stage, 0o700)
            completed_bytes = 0
            for file_spec in spec["files"]:
                def progress(written, offset=completed_bytes):
                    state["downloaded_bytes"] = offset + written
                await self.downloader(spec, file_spec, stage / file_spec["name"], progress)
                completed_bytes += file_spec["size_bytes"]
                state["downloaded_bytes"] = completed_bytes
            state.update(state="verifying", message="翻译模型已下载，正在校验全部文件。")
            verification = asyncio.create_task(asyncio.to_thread(verify_files, stage, spec, cancellation))
            source = await asyncio.shield(verification)
            source["downloaded_by"] = "explicit_user_request"
            (stage / "model-source.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
            if destination.exists() or destination.is_symlink():
                raise ProviderError("翻译模型目录在下载期间发生变化，原文件已保留，请重新检查。")
            os.rename(stage, destination)
            stage = None
            state.update(state="downloaded", message="翻译模型下载完成并通过校验，请加载并预热。", error="")
        except asyncio.CancelledError:
            cancellation.set()
            if verification is not None:
                # A native hash worker must stop touching staged files before
                # cancellation removes them or a new explicit start reuses paths.
                await asyncio.gather(verification, return_exceptions=True)
            state.update(state="cancelled", message="翻译模型下载已取消，已有文件保持原样。", error="")
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "翻译模型下载失败，请检查网络与磁盘空间后手动重试。"
            state.update(state="failed", message=message, error=message)
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
