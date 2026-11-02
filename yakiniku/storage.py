"""Private local settings, recordings, and subtitle exports."""

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Union

from .config import DEFAULT_API_BASE_URL, effective_provider, materialize_task_settings


DEFAULT_SETTINGS = {
    "api_key": "",
    "api_base_url": DEFAULT_API_BASE_URL,
    "transcription_base_url": "",
    "transcription_api_key": "",
    "translation_base_url": "",
    "translation_api_key": "",
    "import_transcription_base_url": "",
    "import_transcription_api_key": "",
    "import_transcription_model": "",
    "import_transcription_api": "openai",
    "import_transcription_response_format": "verbose_json",
    "subtitle_split_enabled": True,
    "subtitle_max_chars": 24,
    "subtitle_max_seconds": 6.0,
    "correction_base_url": "",
    "correction_api_key": "",
    "correction_model": "",
    "correction_api": "",
    "correction_anthropic_auth": "api_key",
    "translation_anthropic_auth": "api_key",
    "service_profiles_enabled": False,
    "service_profiles": [],
    "task_routes": {},
    "live_settings": {},
    "transcription_mode": "local",
    "translation_api": "responses",
    "transcription_model": "tiny",
    "translation_model": "gpt-4.1-mini",
    "glossary": "",
    "segment_seconds": 5,
}
EXPORT_NAMES = (
    "audio.wav",
    "ja.srt",
    "zh-CN.srt",
    "bilingual.srt",
    "bilingual.vtt",
    "transcript.txt",
    "session.json",
)
SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
AUDIO_PART_NAME = re.compile(r"^audio-([0-9]{3,})\.wav$")
SOURCE_EXTENSIONS = frozenset({
    "wav", "mp3", "m4a", "flac", "ogg", "opus", "aac", "mp4", "webm", "wma", "aiff", "aif",
})


def _is_source_name(name: str) -> bool:
    return name.startswith("source.") and name[7:] in SOURCE_EXTENSIONS


def _is_audio_name(name: str) -> bool:
    match = AUDIO_PART_NAME.fullmatch(name)
    return name == "audio.wav" or bool(
        match and int(match.group(1)) >= 2
        and name == f"audio-{int(match.group(1)):03d}.wav"
    )


def _audio_order(name: str) -> int:
    return 1 if name == "audio.wav" else int(AUDIO_PART_NAME.fullmatch(name).group(1))


def _write_text(path: Path, value: str) -> None:
    """Replace atomically so a crash cannot truncate the last saved version."""
    descriptor, temporary = tempfile.mkstemp(prefix=".saving-", dir=str(path.parent))
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _subtitle_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\n\s*\n", "\n", text).strip()


def _milliseconds(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def timestamp(milliseconds: int, vtt: bool = False) -> str:
    """Format elapsed time, including recordings longer than 24 hours."""
    whole, fraction = divmod(_milliseconds(milliseconds), 1000)
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{'.' if vtt else ','}{fraction:03d}"


class SettingsStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "settings.json"

    def _stored(self) -> Dict[str, Any]:
        result = dict(DEFAULT_SETTINGS)
        if self.path.is_symlink():
            raise ValueError("设置文件不能是符号链接")
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                saved = {}
            if isinstance(saved, dict):
                result.update({key: value for key, value in saved.items() if key in DEFAULT_SETTINGS})
        return result

    def load(self) -> Dict[str, Any]:
        result = self._stored()
        environment_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if environment_key:
            result["api_key"] = environment_key
        return materialize_task_settings(result)

    def save(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        # Do not persist an environment variable merely because settings changed.
        result = self._stored()
        result.update({key: value for key, value in updates.items() if key in DEFAULT_SETTINGS})
        _write_json(self.path, result)
        return self.load()

    def public(self) -> Dict[str, Any]:
        result = self.load()
        translation = effective_provider(result, "translation")
        speech = effective_provider(result, "import_transcription")
        correction = effective_provider(result, "correction")
        profiles = result.get("service_profiles", [])
        result["service_profiles"] = [
            {**{k: v for k, v in item.items() if k != "api_key"},
             "key_configured": bool(item.get("api_key") or item.get("use_environment_key") and os.environ.get("OPENAI_API_KEY", "").strip())}
            for item in profiles
        ]
        shared_key = str(result.pop("api_key", "") or "").strip()
        transcription_key = str(result.pop("transcription_api_key", "") or "").strip()
        translation_key = str(result.pop("translation_api_key", "") or "").strip()
        import_key = str(result.pop("import_transcription_api_key", "") or "").strip()
        result.pop("correction_api_key", None)
        result["shared_key_configured"] = bool(shared_key)
        result["transcription_key_configured"] = bool(transcription_key or shared_key)
        result["translation_key_configured"] = bool(translation["api_key"])
        result["correction_key_configured"] = bool(correction["api_key"])
        correction_model = (result.get("correction_model") if result.get("service_profiles_enabled")
                            else result.get("correction_model") or result.get("translation_model"))
        result["correction_configuration_ready"] = bool(correction["api_key"] and correction["base_url"] and correction_model)
        result["transcription_has_own_key"] = bool(transcription_key)
        result["translation_has_own_key"] = bool(translation_key)
        result["import_transcription_key_configured"] = bool(speech["api_key"])
        result["import_transcription_has_own_key"] = bool(import_key)
        result["import_effective_base_url"] = speech["base_url"]
        result["import_configuration_ready"] = bool(
            result["import_transcription_key_configured"] and result["import_effective_base_url"]
            and str(result.get("import_transcription_model") or "").strip()
            and result["translation_key_configured"]
            and translation["base_url"] and result["correction_configuration_ready"]
            and str(result.get("translation_model") or "").strip()
        )
        local = result.get("transcription_mode") == "local"
        result["key_configured"] = result["translation_key_configured"] and (local or result["transcription_key_configured"])
        from .local_transcriber import local_model_ready
        result["local_model_ready"] = local_model_ready(result.get("transcription_model") or "tiny")
        result["configuration_ready"] = bool(result["key_configured"] and
            (local and result["local_model_ready"] or not local and (result.get("transcription_base_url") or result.get("api_base_url"))) and
            translation["base_url"] and result["correction_configuration_ready"] and
            str(result.get("transcription_model") or "").strip() and
            str(result.get("translation_model") or "").strip())
        result["key_from_environment"] = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        return result


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("无效的录制编号")
        directory = self.root / session_id
        if directory.is_symlink() or directory.resolve().parent != self.root:
            raise ValueError("录制目录不能是符号链接")
        return directory

    def file_path(self, session_id: str, name: str) -> Path:
        if name not in EXPORT_NAMES and not _is_audio_name(name) and not _is_source_name(name):
            raise ValueError("无效的导出文件名")
        path = self.directory(session_id) / name
        if path.is_symlink():
            raise ValueError("不能读取符号链接")
        return path

    def create(self, title: str, sample_rate: int = 48000, channels: int = 2) -> Dict[str, Any]:
        title = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]", " ", str(title or "")).strip()[:120]
        now = datetime.now().astimezone()
        session = {
            "id": uuid.uuid4().hex,
            "title": title or now.strftime("直播录制 %Y-%m-%d %H:%M"),
            "created_at": now.isoformat(timespec="seconds"),
            "status": "recording",
            "duration_ms": 0,
            "sample_rate": int(sample_rate),
            "channels": int(channels),
            "segments": [],
            "warnings": [],
        }
        self.directory(session["id"]).mkdir(mode=0o700)
        self.save(session)
        return session

    def load(self, session_id: str) -> Dict[str, Any]:
        path = self.file_path(session_id, "session.json")
        session = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(session, dict) or session.get("id") != session_id:
            raise ValueError("录制信息损坏")
        return session

    def save(self, session: Dict[str, Any]) -> Dict[str, Any]:
        path = self.file_path(session["id"], "session.json")
        if not path.parent.is_dir():
            raise FileNotFoundError("找不到录制目录")
        # File sizes are derived at read time, avoiding recursive metadata sizes.
        saved = {key: value for key, value in session.items() if key != "files"}
        _write_json(path, saved)
        return session

    def files(self, session_or_id: Union[Dict[str, Any], str]) -> List[Dict[str, Any]]:
        session_id = session_or_id if isinstance(session_or_id, str) else session_or_id["id"]
        files = []
        directory = self.directory(session_id)
        audio_names = sorted(
            (path.name for path in directory.iterdir() if _is_audio_name(path.name)),
            key=_audio_order,
        ) if directory.is_dir() else []
        source_names = sorted(path.name for path in directory.iterdir() if _is_source_name(path.name)) if directory.is_dir() else []
        names = audio_names + source_names + [name for name in EXPORT_NAMES if name != "audio.wav"]
        for name in names:
            try:
                path = self.file_path(session_id, name)
                if path.is_file():
                    files.append({
                        "name": name,
                        "url": f"/api/sessions/{session_id}/files/{name}",
                        "size": path.stat().st_size,
                    })
            except (OSError, ValueError):
                continue
        return files

    def list(self) -> List[Dict[str, Any]]:
        sessions = []
        for directory in self.root.iterdir():
            if not SESSION_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                session = self.load(directory.name)
                summary = {key: value for key, value in session.items() if key not in ("segments", "files")}
                summary["segment_count"] = len(session.get("segments", []))
                summary["failed_translation_count"] = sum(
                    1 for segment in session.get("segments", [])
                    if str(segment.get("ja", "") or "").strip()
                    and not str(segment.get("zh", "") or "").strip()
                )
                summary["pending_subtitle_count"] = sum(
                    1 for segment in session.get("segments", [])
                    if segment.get("asr_status") in ("pending", "running", "deferred", "failed")
                    or (segment.get("ja") and not segment.get("zh")
                        and segment.get("translation_status") != "skipped")
                )
                summary["files"] = self.files(session)
                sessions.append(summary)
            except (OSError, ValueError, KeyError):
                continue
        return sorted(sessions, key=lambda item: (item.get("created_at", ""), item["id"]), reverse=True)

    def append_segment(self, session: Dict[str, Any], segment: Dict[str, Any]) -> None:
        segments = session.setdefault("segments", [])
        for index, existing in enumerate(segments):
            if segment.get("id") and existing.get("id") == segment["id"]:
                segments[index] = {**existing, **segment}
                return
        segments.append(dict(segment))

    def export(self, session: Dict[str, Any]) -> Dict[str, Any]:
        segments = []
        for raw in session.get("segments", []):
            segment = dict(raw)
            segment["start_ms"] = _milliseconds(segment.get("start_ms", 0))
            segment["end_ms"] = max(segment["start_ms"] + 1, _milliseconds(segment.get("end_ms")))
            segment["ja"] = _subtitle_text(segment.get("ja"))
            segment["zh"] = _subtitle_text(segment.get("zh"))
            # Unsuccessful text never becomes a Chinese cue. Older projects
            # without a stage field retain their already saved translations.
            if segment.get("translation_status") in ("pending", "running", "deferred", "failed", "skipped"):
                segment["zh"] = ""
            segments.append(segment)
        segments.sort(key=lambda item: (item["start_ms"], item["end_ms"], str(item.get("id", ""))))
        session["segments"] = segments

        def subtitle(language: str, vtt: bool = False) -> str:
            cues = []
            for segment in segments:
                if language == "bilingual":
                    # Chinese first, Japanese underneath; retain Japanese if translation failed.
                    text = "\n".join(line for line in (segment["zh"], segment["ja"]) if line)
                else:
                    text = segment[language]
                if not text:
                    continue
                cue = (
                    f"{len(cues) + 1}\n"
                    f"{timestamp(segment['start_ms'], vtt)} --> {timestamp(segment['end_ms'], vtt)}\n"
                    f"{text}\n"
                )
                cues.append(cue)
            return ("WEBVTT\n\n" if vtt else "") + "\n".join(cues)

        for name, language, vtt in (
            ("ja.srt", "ja", False),
            ("zh-CN.srt", "zh", False),
            ("bilingual.srt", "bilingual", False),
            ("bilingual.vtt", "bilingual", True),
        ):
            _write_text(self.file_path(session["id"], name), subtitle(language, vtt))
        transcript = []
        for segment in segments:
            if segment["ja"] or segment["zh"]:
                transcript.append(
                    f"[{timestamp(segment['start_ms'], True)} → {timestamp(segment['end_ms'], True)}]\n"
                    f"日语：{segment['ja']}\n中文：{segment['zh']}\n"
                )
        _write_text(self.file_path(session["id"], "transcript.txt"), "\n".join(transcript))
        self.save(session)
        return {**session, "files": self.files(session)}

    def recover(self) -> int:
        count = 0
        for summary in self.list():
            imported = summary.get("source_type") == "import"
            import_pending = imported and summary.get("import_stage") in (
                "uploading", "queued", "decoding", "transcribing", "translating",
            )
            if import_pending or summary.get("status") in ("preparing", "recording", "stopping", "processing", "finalizing"):
                session = self.load(summary["id"])
                session["status"] = "interrupted"
                if imported:
                    session["import_stage"] = "interrupted"
                    session["import_error"] = "上次音频处理已中断，已保存的音频和字幕仍可预览。请点击继续处理。"
                    session["import_progress_label"] = "处理已中断，可继续处理"
                for segment in session.get("segments", []):
                    if not imported and session.get("live_snapshot"):
                        if segment.get("asr_status") in ("pending", "running"):
                            segment["asr_status"] = "deferred"
                        if segment.get("translation_status") in ("pending", "running"):
                            segment["translation_status"] = "deferred"
                    elif segment.get("translation_status") in ("pending", "running"):
                        segment["translation_status"] = "failed"
                warning = "上次音频处理已中断，已恢复已保存的音频和字幕。" if imported else "上次录制意外中断，已恢复已保存的音频和字幕。"
                if warning not in session.setdefault("warnings", []):
                    session["warnings"].append(warning)
                audio_parts = []
                duration_ms = 0
                for item in self.files(session):
                    if not _is_audio_name(item["name"]):
                        continue
                    audio = self.file_path(session["id"], item["name"])
                    try:
                        import wave
                        with wave.open(str(audio), "rb") as stream:
                            part_ms = stream.getnframes() * 1000 / stream.getframerate()
                        audio_parts.append({
                            "name": item["name"],
                            "start_ms": round(duration_ms),
                            "end_ms": round(duration_ms + part_ms),
                        })
                        duration_ms += part_ms
                    except (OSError, EOFError, wave.Error):
                        session["warnings"].append(f"{item['name']} 未能自动校验，请保留文件以便恢复。")
                if audio_parts:
                    session["audio_parts"] = audio_parts
                    session["duration_ms"] = max(session.get("duration_ms", 0), round(duration_ms)) if imported else round(duration_ms)
                if not imported and session.get("live_snapshot"):
                    session["audio_saved"] = bool(audio_parts)
                    session["subtitle_status"] = "interrupted"
                    session["live_recovery_from_ms"] = session.get("live_committed_until_ms", 0)
                self.export(session)
                count += 1
        return count
