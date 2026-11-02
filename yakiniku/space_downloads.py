"""Task-scoped browser capture and local HLS audio downloads.

Only sanitized job metadata survives a restart. Captured playlists, AES keys,
and the per-job browser capability stay in memory or private temporary files.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime, timezone
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


PROJECT = Path(__file__).resolve().parent.parent
JOB_ID = re.compile(r"[0-9a-f]{32}")
ATTRIBUTES = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))(?:,|$)')
TERMINAL = {"completed", "failed", "interrupted", "cancelled"}
MAX_PLAYLIST = 2 * 1024 * 1024
MAX_KEYS = 16


class SpaceDownloadError(ValueError):
    """A deliberately public, credential-free explanation."""


class SpaceDownloadInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class SpaceCaptureInput(BaseModel):
    kind: str = Field(max_length=16)
    url: str = Field(min_length=1, max_length=8192)
    text: str | None = Field(default=None, max_length=MAX_PLAYLIST)
    key_base64: str | None = Field(default=None, max_length=64)


def canonical_space(value):
    try:
        parsed = urlsplit(str(value).strip())
        valid = (parsed.scheme == "https" and parsed.hostname in ("x.com", "twitter.com", "www.x.com", "www.twitter.com")
                 and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise SpaceDownloadError("请填写 HTTPS 的 X Space 网页链接。")
    match = re.fullmatch(r"/i/spaces/([A-Za-z0-9]{13})/?", parsed.path)
    if not match:
        raise SpaceDownloadError("请填写 /i/spaces/ 后带场次编号的 X Space 链接。")
    identifier = match.group(1)
    return identifier, "https://x.com/i/spaces/" + identifier


def _attributes(line):
    return {m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
            for m in ATTRIBUTES.finditer(line.partition(":")[2])}


def _https(value):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
                 and not parsed.password and parsed.port in (None, 443)
                 and not any(ord(c) < 32 for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise SpaceDownloadError("捕获资源必须是有效的 HTTPS 地址。")
    return parsed


def validate_key_url(value, space_id):
    parsed = _https(value)
    query = parse_qs(parsed.query)
    if (parsed.hostname != "x.com" or parsed.path != "/i/live_video_stream/key"
            or query.get("broadcast_id") != [space_id]):
        raise SpaceDownloadError("密钥地址不属于本次 Space，已拒绝接收。")
    return value


def validate_playlist(value, text, space_id):
    parsed = _https(value)
    if not parsed.hostname.endswith(".video.pscp.tv"):
        raise SpaceDownloadError("只接收本次 X 回放 CDN 的播放列表。")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_PLAYLIST:
        raise SpaceDownloadError("播放列表为空或过大。")
    lines = [line.strip() for line in text.lstrip("\ufeff").splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise SpaceDownloadError("捕获内容不是 M3U8 播放列表。")
    if "#EXT-X-ENDLIST" not in lines:
        raise SpaceDownloadError("当前清单尚未结束，请等待完整回放清单；不会交付直播滚动窗口。")
    if "#EXT-X-GAP" in lines or any(line.startswith(("#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:")) for line in lines):
        raise SpaceDownloadError("播放列表包含缺片或部分分片，不能作为完整回放下载。")
    root_path = unquote(parsed.path.rsplit("/", 1)[0]) + "/"

    def resource(uri):
        resolved = urljoin(value, uri)
        target = _https(resolved)
        decoded_path = unquote(target.path)
        if (target.hostname != parsed.hostname or not decoded_path.startswith(root_path)
                or any(part in (".", "..") for part in decoded_path.split("/"))):
            raise SpaceDownloadError("分片或初始化段离开了本次回放目录，已拒绝下载。")

    keys, segments, durations = set(), 0, []
    for line in lines:
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.partition(":")[2].split(",")[0])
            except ValueError:
                raise SpaceDownloadError("播放列表的分片时长无效。") from None
            if not math.isfinite(duration) or not 0 < duration < 86400:
                raise SpaceDownloadError("播放列表的分片时长无效。")
            durations.append(duration)
        elif line.startswith("#EXT-X-KEY:"):
            attrs = _attributes(line)
            if attrs.get("METHOD") == "NONE":
                continue
            if attrs.get("METHOD") != "AES-128" or attrs.get("KEYFORMAT", "identity") != "identity":
                raise SpaceDownloadError("仅支持普通 AES-128 回放，不支持 DRM 或 SAMPLE-AES。")
            if not attrs.get("URI"):
                raise SpaceDownloadError("加密声明缺少密钥地址。")
            keys.add(validate_key_url(urljoin(value, attrs["URI"]), space_id))
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _attributes(line)
            if not attrs.get("URI"):
                raise SpaceDownloadError("初始化段缺少地址。")
            resource(attrs["URI"])
        elif not line.startswith("#"):
            resource(line)
            segments += 1
        elif "URI=" in line:
            raise SpaceDownloadError("请等待音频媒体清单；该清单包含尚不支持的外部资源。")
    if not segments or segments != len(durations):
        raise SpaceDownloadError("请等待包含完整分片及对应时长的音频媒体清单。")
    if len(keys) > MAX_KEYS:
        raise SpaceDownloadError("该清单的轮换密钥过多，暂不支持。")
    return {"playlist_url": value, "playlist_text": "\n".join(lines) + "\n",
            "key_urls": keys, "segments": segments, "duration_seconds": sum(durations)}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _private_write(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


class SpaceDownloadManager:
    def __init__(self, root, runner=None, capture_ttl=600, clock=None):
        self.root = Path(root)
        self.jobs = {}
        self.captures = {}
        self.tasks = {}
        self.runner = runner or self._run_download
        self.semaphore = asyncio.Semaphore(1)
        self.closing = False
        self.capture_ttl = capture_ttl
        self.clock = clock or time.monotonic
        self.watchdog_interval = 1
        self.watchdog = None

    def start(self):
        self.recover()
        self.watchdog = asyncio.create_task(self._watch_captures())

    async def _watch_captures(self):
        while True:
            await asyncio.sleep(self.watchdog_interval)
            self.expire_captures()

    def expire_captures(self):
        for identifier, capture in list(self.captures.items()):
            if self.clock() - capture["started"] >= self.capture_ttl:
                self.captures.pop(identifier, None)
                capture["keys"].clear()
                capture["playlist"] = None
                capture["token"] = ""
                self._stage(self.jobs[identifier], "failed", "浏览器捕获已超过 10 分钟，临时凭据已清理；请重新提交链接。")

    @staticmethod
    def _clean_private_directories(folder):
        for entry in folder.iterdir():
            if not entry.name.startswith((".capture-", ".hls-audio-")):
                continue
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)

    def recover(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for folder in sorted(self.root.iterdir()):
            if not JOB_ID.fullmatch(folder.name) or not folder.is_dir() or folder.is_symlink():
                continue
            self._clean_private_directories(folder)
            try:
                stored = json.loads((folder / "job.json").read_text())
                space_id, source = canonical_space(stored["source_url"])
                job = {"id": folder.name, "space_id": space_id, "source_url": source,
                       "stage": stored.get("stage") if stored.get("stage") in TERMINAL else "interrupted",
                       "message": "已读取下载记录。", "created_at": stored.get("created_at", _now()),
                       "duration_ms": max(0, int(stored.get("duration_ms", 0))),
                       "segments": max(0, int(stored.get("segments", 0)))}
                if job["stage"] == "interrupted":
                    job["message"] = "下载已中断；请重新提交链接并在浏览器中捕获。"
                    self._remove_outputs(job)
                self.jobs[job["id"]] = job
                self._save(job)
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def _folder(self, identifier):
        if not JOB_ID.fullmatch(identifier):
            raise HTTPException(404, "下载任务不存在。")
        return self.root / identifier

    def get(self, identifier):
        self._folder(identifier)
        try:
            return self.jobs[identifier]
        except KeyError:
            raise HTTPException(404, "下载任务不存在。") from None

    def public(self, job):
        result = {key: job[key] for key in ("id", "space_id", "source_url", "stage", "message", "created_at", "duration_ms", "segments")}
        result["files"] = []
        if job["stage"] == "completed":
            for name in ("audio.mp3", "audio.json"):
                path = self._folder(job["id"]) / name
                if path.is_file() and not path.is_symlink():
                    result["files"].append({"name": name, "size": path.stat().st_size})
        return result

    def _save(self, job):
        folder = self._folder(job["id"])
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private_write(folder / "job.json", json.dumps(self.public(job), ensure_ascii=False, indent=2))

    def _stage(self, job, stage, message):
        job.update(stage=stage, message=message)
        self._save(job)

    def create(self, value):
        if self.closing:
            raise HTTPException(503, "本机服务正在停止。")
        if len(self.jobs) >= 64:
            raise HTTPException(409, "下载记录已达上限，请先整理本机下载目录。")
        space_id, source = canonical_space(value)
        job = {"id": secrets.token_hex(16), "space_id": space_id, "source_url": source,
               "stage": "waiting_browser", "message": "等待浏览器打开本场回放并捕获播放列表。",
               "created_at": _now(), "duration_ms": 0, "segments": 0}
        token = secrets.token_urlsafe(32)
        self.jobs[job["id"]] = job
        self.captures[job["id"]] = {"token": token, "keys": {}, "playlist": None, "started": self.clock()}
        self._save(job)
        return {**self.public(job), "capture_token": token}

    def capture(self, identifier, token, event):
        job = self.get(identifier)
        self.expire_captures()
        capture = self.captures.get(identifier)
        if not capture or not token or not secrets.compare_digest(str(token), capture["token"]):
            raise HTTPException(403, "捕获凭据无效或任务已结束。")
        if job["stage"] not in ("waiting_browser", "capturing"):
            raise HTTPException(409, "该任务已停止接收浏览器资源。")
        if event.kind == "playlist":
            capture["playlist"] = validate_playlist(event.url, event.text, job["space_id"])
            job["segments"] = capture["playlist"]["segments"]
            job["duration_ms"] = round(capture["playlist"]["duration_seconds"] * 1000)
        elif event.kind == "key":
            uri = validate_key_url(event.url, job["space_id"])
            try:
                key = base64.b64decode(event.key_base64 or "", validate=True)
            except (ValueError, TypeError):
                raise SpaceDownloadError("密钥必须是有效的 Base64 文本。") from None
            if len(key) != 16:
                raise SpaceDownloadError("AES-128 密钥长度必须是 16 字节。")
            if uri not in capture["keys"] and len(capture["keys"]) >= MAX_KEYS:
                raise SpaceDownloadError("捕获的密钥过多，请重新建立本次任务。")
            capture["keys"][uri] = base64.b64encode(key).decode("ascii")
        else:
            raise SpaceDownloadError("不支持的捕获事件。")
        playlist = capture["playlist"]
        if playlist and playlist["key_urls"].issubset(capture["keys"]):
            self._stage(job, "downloading", "播放列表与本场密钥齐全，正在下载并用 FFmpeg 转码。")
            snapshot = {key: playlist[key] for key in ("playlist_url", "playlist_text")}
            snapshot["keys"] = {key: capture["keys"][key] for key in playlist["key_urls"]}
            self.captures.pop(identifier, None)
            self.tasks[identifier] = asyncio.create_task(self._download(job, snapshot))
        else:
            message = "已取得完整播放列表，等待本场 AES 密钥。" if playlist else "已取得本场密钥，等待完整音频播放列表。"
            self._stage(job, "capturing", message)
        return self.public(job)

    def _remove_outputs(self, job):
        for name in ("audio.mp3", "audio.json"):
            (self._folder(job["id"]) / name).unlink(missing_ok=True)

    async def _download(self, job, snapshot):
        try:
            async with self.semaphore:
                returned = self.runner(snapshot, self._folder(job["id"]))
                report = await returned if inspect.isawaitable(returned) else returned
            path = self._folder(job["id"]) / "audio.mp3"
            if (not isinstance(report, dict) or report.get("ffmpeg_full_decode_passed") is not True
                    or report.get("reencoded") is not True or not path.is_file() or path.is_symlink()
                    or path.stat().st_size == 0 or int(report.get("segments", 0)) != job["segments"]):
                raise SpaceDownloadError("下载器没有返回完整转码验证结果。")
            duration = float(report.get("duration_seconds", 0))
            if not math.isfinite(duration) or duration <= 0 or abs(duration * 1000 - job["duration_ms"]) > 2000:
                raise SpaceDownloadError("下载音频时长与捕获清单不一致。")
            safe_report = {"segments": job["segments"], "duration_seconds": duration,
                           "ffmpeg_full_decode_passed": True, "reencoded": True,
                           "audio_format": "MP3, 192 kb/s, 44.1 kHz stereo", "bytes": path.stat().st_size}
            _private_write(self._folder(job["id"]) / "audio.json", json.dumps(safe_report, ensure_ascii=False, indent=2))
            os.chmod(path, 0o600)
            self._stage(job, "completed", "下载和 FFmpeg 转码完成，整段解码验证通过。")
        except asyncio.CancelledError:
            self._remove_outputs(job)
            if job["stage"] != "cancelled":
                self._stage(job, "interrupted", "下载已中断，请重新提交链接。")
            raise
        except Exception:
            self._remove_outputs(job)
            self._stage(job, "failed", "音频下载或转码失败；未交付残缺文件，请重新播放本场回放后重试。")
        finally:
            snapshot.clear()
            self.captures.pop(job["id"], None)
            self.tasks.pop(job["id"], None)

    async def _run_download(self, capture, output):
        script = PROJECT / "tools/hls-audio-download/scripts/download_hls.py"
        configured_ffmpeg = os.environ.get("YAKINIKU_FFMPEG", "").strip()
        candidates = ([Path(configured_ffmpeg).expanduser()] if configured_ffmpeg else
                      [PROJECT / "tools/hls-audio-download/bin/ffmpeg",
                       Path(shutil.which("ffmpeg") or PROJECT / ".ffmpeg-not-installed")])
        ffmpeg = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
        if not ffmpeg or not script.is_file():
            raise SpaceDownloadError("本机缺少 FFmpeg 下载器。请安装 FFmpeg 并加入 PATH，或设置 YAKINIKU_FFMPEG。")
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=str(output)) as private:
            folder = Path(private)
            playlist, keys = folder / "playlist.m3u8", folder / "keys.json"
            media = folder / "output"
            media.mkdir(mode=0o700)
            _private_write(playlist, capture["playlist_text"])
            _private_write(keys, json.dumps(capture["keys"]))
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(script), "--url", capture["playlist_url"],
                "--playlist-file", str(playlist), "--key-map-file", str(keys),
                "--ffmpeg", str(ffmpeg), "--output-dir", str(media), "--name", "audio",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=(os.name == "posix"))
            successful = False
            try:
                code = await process.wait()
                if code:
                    raise SpaceDownloadError("下载器未成功完成。")
                successful = True
            finally:
                if not successful:
                    await self._stop_process_tree(process)
            report = json.loads((media / "audio.json").read_text())
            audio = media / "audio.mp3"
            if (report.get("ffmpeg_full_decode_passed") is not True or report.get("reencoded") is not True
                    or not audio.is_file() or audio.is_symlink() or audio.stat().st_size == 0):
                raise SpaceDownloadError("下载器没有完成音频验证。")
            os.link(audio, output / "audio.mp3")
            return report

    @staticmethod
    async def _stop_process_tree(process):
        if os.name != "posix":
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
            await process.wait()
            return
        # The downloader owns this newly created process group. This includes
        # FFmpeg children, so cancellation cannot leave them writing secret
        # work directories after their Python parent exits.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            await process.wait()
            return
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(.05)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    async def cancel(self, identifier):
        job = self.get(identifier)
        if job["stage"] in TERMINAL:
            return self.public(job)
        self.captures.pop(identifier, None)
        self._stage(job, "cancelled", "已取消本次下载。")
        task = self.tasks.get(identifier)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._remove_outputs(job)
        return self.public(job)

    async def shutdown(self):
        self.closing = True
        if self.watchdog:
            self.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog
            self.watchdog = None
        for identifier in list(self.jobs):
            job = self.jobs[identifier]
            if job["stage"] not in TERMINAL:
                self.captures.pop(identifier, None)
                self._stage(job, "interrupted", "本机服务已停止，请重新提交链接。")
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.captures.clear()


def space_download_router(manager):
    router = APIRouter(prefix="/api/space-downloads")

    @router.get("")
    async def list_downloads():
        return {"jobs": [manager.public(job) for job in reversed(list(manager.jobs.values()))]}

    @router.post("", status_code=201)
    async def create_download(body: SpaceDownloadInput):
        try:
            return manager.create(body.url)
        except SpaceDownloadError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/{identifier}")
    async def get_download(identifier: str):
        return manager.public(manager.get(identifier))

    @router.post("/{identifier}/capture")
    async def capture_download(identifier: str, body: SpaceCaptureInput,
                               x_space_capture_token: str | None = Header(default=None)):
        try:
            return manager.capture(identifier, x_space_capture_token, body)
        except SpaceDownloadError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.post("/{identifier}/cancel")
    async def cancel_download(identifier: str):
        return await manager.cancel(identifier)

    @router.get("/{identifier}/files/{name}")
    async def download_file(identifier: str, name: str):
        job = manager.get(identifier)
        if job["stage"] != "completed" or name not in ("audio.mp3", "audio.json"):
            raise HTTPException(404, "可下载文件不存在。")
        path = manager._folder(identifier) / name
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "可下载文件不存在。")
        return FileResponse(path, filename=name, media_type="audio/mpeg" if name.endswith(".mp3") else "application/json")

    return router
