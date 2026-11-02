#!/usr/bin/env python3
"""Download an ended HLS playlist and encode playable audio with FFmpeg."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


class DownloadError(Exception):
    pass


ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))(?:,|$)')


def attributes(line):
    return {m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
            for m in ATTRIBUTE.finditer(line.partition(":")[2])}


def remote_url(value, base=None):
    value = urljoin(base, value) if base else value
    if urlparse(value).scheme not in ("https", "http") or not urlparse(value).hostname:
        raise DownloadError("播放列表仅支持 HTTP/HTTPS 资源。")
    return value


def parse_key(data):
    if len(data) == 16:
        return data
    try:
        text = data.decode("ascii").strip()
        if re.fullmatch(r"(?:0x)?[0-9a-fA-F]{32}", text):
            key = bytes.fromhex(text.removeprefix("0x"))
        else:
            key = base64.b64decode(text, validate=True)
        if len(key) == 16:
            return key
    except (ValueError, UnicodeError):
        pass
    raise DownloadError("AES-128 密钥需要 16 字节原始文件，或对应的 Hex/Base64 文本。")


def parse_input(path):
    try:
        text = html.unescape(Path(path).read_text(encoding="utf-8-sig")).replace("\\&", "&")
    except (OSError, UnicodeError):
        raise DownloadError("无法读取粘贴文本文件，请使用 UTF-8 文本。") from None
    candidates = re.findall(r'https?://[^\s<>"\'\]\)]+', text)
    url = next((x.rstrip(".,;，。；") for x in candidates if ".m3u8" in x.lower()), None)
    if not url:
        raise DownloadError("粘贴文本里没有 M3U8 地址；仅分片地址不足以保留密钥和 IV 信息。")
    key = None
    for kind, pattern in (
        ("hex", r"(?:密钥|key)\s*[（(]\s*Hex\s*[）)]\s*[:：]\s*((?:0x)?[0-9a-f]{32})"),
        ("base64", r"(?:密钥|key)\s*[（(]\s*Base64\s*[）)]\s*[:：]\s*([A-Za-z0-9+/]+={0,2})"),
    ):
        match = re.search(pattern, text, re.I)
        if match:
            key = parse_key(match.group(1).encode("ascii"))
            break
    return remote_url(url), key


class Fetcher:
    def __init__(self, retries, timeout):
        self.retries, self.timeout = retries, timeout

    def get(self, url, destination=None, limit=None, with_url=False):
        remote_url(url)
        for attempt in range(self.retries):
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0 HLS-Audio-Downloader/1.0"})
                with urlopen(req, timeout=self.timeout) as response:
                    remote_url(response.url)
                    if response.status != 200:
                        raise DownloadError("资源未返回完整的 HTTP 200 响应。")
                    if destination is None:
                        data = response.read((limit + 1) if limit else -1)
                        if not data or (limit is not None and len(data) > limit):
                            raise DownloadError("远程资源为空或超过允许大小。")
                        return (data, response.url) if with_url else data
                    with destination.open("wb") as output:
                        os.chmod(destination, 0o600)
                        shutil.copyfileobj(response, output)
                    if destination.stat().st_size == 0:
                        raise DownloadError("音频资源为空。")
                    expected = response.headers.get("Content-Length")
                    if expected and destination.stat().st_size != int(expected):
                        raise OSError("Incomplete response")
                    return destination.stat().st_size
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise DownloadError("远程资源需要授权或链接已过期（HTTP %s）；请提供新的链接或猫抓导出的密钥。" % exc.code) from None
                reason = "HTTP %s" % exc.code
            except (URLError, TimeoutError, OSError, HTTPException):
                reason = "连接失败或超时"
            if attempt + 1 < self.retries:
                time.sleep(min(2 ** attempt, 4))
        raise DownloadError("资源下载失败（%s），已达到重试上限；没有跳过分片。" % reason)


def resolve_playlist(fetcher, url):
    for _ in range(6):
        try:
            data, url = fetcher.get(url, limit=8 * 1024 * 1024, with_url=True)
            text = data.decode("utf-8-sig")
        except UnicodeError:
            raise DownloadError("远程地址没有返回 UTF-8 M3U8 播放列表。") from None
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if not lines or lines[0] != "#EXTM3U":
            raise DownloadError("远程地址没有返回有效的 M3U8 播放列表。")
        renditions = [attributes(x) for x in lines if x.startswith("#EXT-X-MEDIA:")]
        audio = [x for x in renditions if x.get("TYPE") == "AUDIO" and x.get("URI")]
        if audio:
            chosen = next((x for x in audio if x.get("DEFAULT") == "YES"), audio[0])
            url = remote_url(chosen["URI"], url)
            continue
        variants = []
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:"):
                if index + 1 >= len(lines) or lines[index + 1].startswith("#"):
                    raise DownloadError("主播放列表的变体地址无效。")
                attrs = attributes(line)
                codecs = attrs.get("CODECS", "").lower()
                audio_only = not attrs.get("RESOLUTION") and not any(
                    token in codecs for token in ("avc", "hev", "hvc", "vp9", "vp09", "av01"))
                try:
                    bandwidth = int(attrs.get("BANDWIDTH", "0"))
                except ValueError:
                    bandwidth = 0
                variants.append((audio_only, bandwidth, lines[index + 1]))
        if variants:
            url = remote_url(max(variants, key=lambda x: x[:2])[2], url)
            continue
        return url, lines
    raise DownloadError("播放列表嵌套过深或循环引用。")


def localize(lines, playlist_url, work, fetcher, manual_key, workers, key_map=None):
    if "#EXT-X-ENDLIST" not in lines:
        raise DownloadError("播放列表尚未结束，可能是直播滚动窗口；本工具不会把当前片段称为完整回放。请提供已结束的回放链接。")
    if "#EXT-X-GAP" in lines:
        raise DownloadError("播放列表明确标记了缺失分片（EXT-X-GAP），不能作为完整回放交付。")
    if any(x.startswith(("#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:")) for x in lines):
        raise DownloadError("暂不支持低延迟 HLS 的部分分片清单，请提供普通完整回放清单。")
    durations = []
    for line in lines:
        if line.startswith("#EXTINF:"):
            try:
                value = float(line.partition(":")[2].split(",")[0])
                if not 0 < value < 86400:
                    raise ValueError
                durations.append(value)
            except ValueError:
                raise DownloadError("播放列表包含无效分片时长。") from None
    if not durations:
        raise DownloadError("播放列表里没有完整音频分片。")
    sequence = next((x.partition(":")[2] for x in lines if x.startswith("#EXT-X-MEDIA-SEQUENCE:")), "0")
    try:
        sequence = int(sequence)
        if sequence < 0:
            raise ValueError
    except ValueError:
        raise DownloadError("无效的媒体序号。") from None
    key_urls = set()
    for line in lines:
        if not line.startswith("#EXT-X-KEY:"):
            continue
        attrs = attributes(line)
        method = attrs.get("METHOD")
        if method not in ("NONE", "AES-128") or attrs.get("KEYFORMAT", "identity") != "identity":
            raise DownloadError("此清单不是普通 AES-128 HLS；不支持 DRM 或 SAMPLE-AES。")
        if method == "AES-128":
            if not attrs.get("URI"):
                raise DownloadError("AES-128 声明缺少密钥地址。")
            key_urls.add(remote_url(attrs["URI"], playlist_url))
    if manual_key and len(key_urls) > 1:
        raise DownloadError("清单包含多个轮换密钥地址，单个手动密钥不足；请提供可访问的对应密钥地址。")
    keys = {}
    for index, url in enumerate(sorted(key_urls)):
        if key_map is not None:
            if url not in key_map:
                raise DownloadError("尚未捕获到播放列表所需的全部密钥，请继续播放本场回放。")
            key = key_map[url]
        else:
            key = manual_key if manual_key is not None else fetcher.get(url, limit=16)
        if len(key) != 16:
            raise DownloadError("密钥响应不是 AES-128 所需的 16 字节。")
        path = work / ("key-%03d.key" % index)
        path.write_bytes(key)
        os.chmod(path, 0o600)
        keys[url] = path.name
    resources = {}

    def local_resource(uri):
        url = remote_url(uri, playlist_url)
        if url not in resources:
            suffix = Path(urlparse(url).path).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix or ""):
                suffix = ".bin"
            resources[url] = work / ("resource-%05d" % len(resources) + suffix)
        return resources[url].name

    localized, effective_ivs = [], set()
    current_key, segment_count = None, 0
    for line in lines:
        if line.startswith("#EXT-X-KEY:"):
            current_key = attributes(line)
            if current_key.get("METHOD") == "AES-128":
                name = keys[remote_url(current_key["URI"], playlist_url)]
                line = re.sub(r'URI="[^"]*"', 'URI="%s"' % name, line)
        elif line.startswith("#EXT-X-MAP:"):
            attrs = attributes(line)
            if not attrs.get("URI"):
                raise DownloadError("初始化段缺少 URI。")
            if current_key and current_key.get("METHOD") == "AES-128" and not current_key.get("IV"):
                raise DownloadError("加密初始化段缺少显式 IV。")
            line = re.sub(r'URI="[^"]*"', 'URI="%s"' % local_resource(attrs["URI"]), line)
        elif not line.startswith("#"):
            if current_key and current_key.get("METHOD") == "AES-128":
                try:
                    iv = int(current_key["IV"], 16) if "IV" in current_key else sequence + segment_count
                    if not 0 <= iv < 2 ** 128:
                        raise ValueError
                except ValueError:
                    raise DownloadError("分片 IV 无效。") from None
                effective_ivs.add(iv)
            line = local_resource(line)
            segment_count += 1
        elif "URI=" in line:
            raise DownloadError("清单包含暂不支持的额外 URI 标签。")
        localized.append(line)
    if segment_count != len(durations):
        raise DownloadError("分片地址数与时长声明数不一致。")
    print("已解析 %d 个分片，时长 %.3f 秒，开始下载。" % (segment_count, sum(durations)), flush=True)
    completed, total_bytes = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(fetcher.get, url, path) for url, path in resources.items()]
        for job in as_completed(jobs):
            total_bytes += job.result()
            completed += 1
            if completed % 10 == 0 or completed == len(jobs):
                print("已下载 %d/%d 个资源。" % (completed, len(jobs)), flush=True)
    path = work / "local.m3u8"
    path.write_text("\n".join(localized) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path, {"segments": segment_count, "resources_downloaded": completed,
                  "duration_seconds": sum(durations), "distinct_IVs": len(effective_ivs),
                  "distinct_key_urls": len(key_urls), "downloaded_bytes": total_bytes,
                  "playlist_ended": True}


def find_ffmpeg(explicit):
    candidates = [explicit] if explicit else [
        str(Path(__file__).resolve().parents[1] / "bin" / "ffmpeg"),
        shutil.which("ffmpeg"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise DownloadError("找不到 FFmpeg；请使用 --ffmpeg 指定可运行的 FFmpeg。")


def run_ffmpeg(ffmpeg, arguments, label):
    try:
        result = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                                 "-xerror", "-progress", "pipe:1", "-nostats"] + arguments,
                                capture_output=True, text=True)
    except OSError:
        raise DownloadError("无法启动 FFmpeg，请检查可执行文件。") from None
    if result.returncode:
        # No remote URL or key is ever passed to FFmpeg; avoid exposing stderr anyway.
        raise DownloadError("FFmpeg %s失败（退出码 %s）；请检查密钥、输入格式及 FFmpeg 编码器。" % (label, result.returncode))
    times = re.findall(r"^out_time_us=(\d+)$", result.stdout, re.M)
    if not times:
        raise DownloadError("FFmpeg 没有报告有效音频时长。")
    return int(times[-1]) / 1000000


def execute(args):
    url, embedded_key = (parse_input(args.input) if args.input else (remote_url(args.url), None))
    manual_key = embedded_key
    if args.key_file:
        try:
            data = Path(args.key_file).read_bytes()
        except OSError:
            raise DownloadError("无法读取密钥文件。") from None
        manual_key = parse_key(data)
    key_map = None
    if args.key_map_file:
        try:
            raw = json.loads(Path(args.key_map_file).read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or len(raw) > 64:
                raise ValueError
            key_map = {remote_url(url): parse_key(value.encode("ascii"))
                       for url, value in raw.items() if isinstance(url, str) and isinstance(value, str)}
            if len(key_map) != len(raw):
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            raise DownloadError("无法读取捕获密钥映射；需要密钥地址到 Base64 密钥的 JSON 对象。") from None
        if manual_key is not None:
            raise DownloadError("请使用单个密钥或捕获密钥映射中的一种方式。")
    if not re.fullmatch(r"[^/\\\x00-\x1f]+", args.name) or args.name in (".", ".."):
        raise DownloadError("输出名称必须是普通文件名，不能包含目录或控制字符。")
    output = Path(args.output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    names = [args.name + ".mp3", args.name + ".json"] + ([args.name + ".wav"] if args.wav else [])
    if any(os.path.lexists(str(output / name)) for name in names):
        raise DownloadError("输出文件已存在；请更换 --name 或输出目录，工具不会覆盖。")
    ffmpeg = find_ffmpeg(args.ffmpeg)
    fetcher = Fetcher(args.retries, args.timeout)
    if args.playlist_file:
        try:
            text = Path(args.playlist_file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise DownloadError("无法读取浏览器捕获的播放列表。") from None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U" or any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
            raise DownloadError("需要浏览器捕获的有效媒体播放列表，而不是主播放列表。")
    else:
        url, lines = resolve_playlist(fetcher, url)
    published = []
    try:
        with tempfile.TemporaryDirectory(prefix=".hls-audio-", dir=str(output)) as temp:
            work = Path(temp)
            playlist, report = localize(lines, url, work, fetcher, manual_key, args.workers, key_map)
            mp3 = work / (args.name + ".mp3")
            wav = work / (args.name + ".wav")
            command = ["-n", "-protocol_whitelist", "file,crypto,data", "-allowed_extensions", "ALL",
                       "-err_detect", "explode", "-i", str(playlist), "-map", "0:a:0", "-vn",
                       "-af", "asetpts=N/SR/TB", "-c:a", "libmp3lame", "-b:a", "192k",
                       "-ar", "44100", "-ac", "2", "-id3v2_version", "3", str(mp3)]
            if args.wav:
                command += ["-map", "0:a:0", "-vn", "-af", "asetpts=N/SR/TB", "-c:a", "pcm_s16le",
                            "-ar", "48000", "-ac", "2", str(wav)]
            print("分片齐全，正在用 FFmpeg 重新编码并检查。", flush=True)
            run_ffmpeg(ffmpeg, command, "转码")
            validations = {}
            for path in [mp3] + ([wav] if args.wav else []):
                seconds = run_ffmpeg(ffmpeg, ["-err_detect", "explode", "-i", str(path), "-map", "0:a:0",
                                             "-f", "null", "-"], "完整解码检查")
                if abs(seconds - report["duration_seconds"]) > 2:
                    raise DownloadError("转码音频时长与播放列表相差超过 2 秒，已停止交付以避免不完整音频。")
                validations[path.name] = {"bytes": path.stat().st_size, "decoded_seconds": seconds,
                                          "ffmpeg_full_decode_passed": True}
            report.update(ffmpeg_full_decode_passed=True, reencoded=True, output_files=validations,
                          audio_format="MP3, 192 kb/s, 44.1 kHz stereo")
            report_path = work / (args.name + ".json")
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            # Hard links publish atomically on this same filesystem without overwriting.
            for name in names:
                source, destination = work / name, output / name
                os.chmod(source, 0o600)
                os.link(source, destination)
                published.append(destination)
        print("完成：%s" % (output / (args.name + ".mp3")), flush=True)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description="解析已结束的 M3U8 回放，逐片下载并由 FFmpeg 转码为 MP3。")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="M3U8 地址")
    source.add_argument("--input", help="猫抓粘贴文本文件（可含 Hex/Base64 密钥）")
    parser.add_argument("--key-file", help="16 字节原始密钥，或 Hex/Base64 文本文件")
    parser.add_argument("--playlist-file", help="浏览器捕获的媒体播放列表，--url 用于解析相对资源地址")
    parser.add_argument("--key-map-file", help="密钥地址到 Base64 密钥的 JSON 文件，不再请求远程密钥")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--name", default="audio")
    parser.add_argument("--ffmpeg", help="FFmpeg 可执行文件路径")
    parser.add_argument("--wav", action="store_true", help="额外输出 48 kHz 双声道 PCM WAV")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--retries", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not 0 < args.timeout <= 120:
        parser.error("--timeout 必须大于 0 且不超过 120 秒")
    try:
        execute(args)
    except DownloadError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消；临时密钥和未完成文件已清理。", file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError):
        print("错误：本地文件操作或媒体处理失败；未交付未完成音频。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
