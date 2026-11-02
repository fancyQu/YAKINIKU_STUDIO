"""Explicit, non-destructive cloud refinement of a saved live recording."""
from __future__ import annotations

import hashlib
import json
import re
import wave
from copy import deepcopy

from .config import normalize_base_url
from .provider import ProviderError
from .service_config import connection_key, private_configuration
from .storage import _is_audio_name

ROLES = (("file_asr", "音频重新识别"), ("correction", "日语校对"), ("translation", "中文翻译"))
SAVED_STATES = {"completed", "partial", "interrupted"}


def _fingerprint(profile, speech=False):
    fields = {"base_url": profile.get("base_url", "")}
    if speech:
        fields["speech_api"] = profile.get("speech_api", "openai")
    else:
        fields.update(text_api=profile.get("text_api", "chat_completions"),
                      anthropic_auth=profile.get("anthropic_auth", "api_key"))
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def refinement_snapshot(settings_store):
    """Pin destinations and models while leaving keys solely in settings."""
    settings = settings_store.load()
    profiles, routes = private_configuration(settings_store)
    pins = []
    for role, label in ROLES:
        route = routes.get(role) or {}
        profile = next((p for p in profiles if p.get("id") == route.get("provider_id")), {})
        model = str(route.get("model") or "").strip()
        if not (profile.get("enabled", True) and profile.get("base_url") and connection_key(profile) and model):
            raise ProviderError(f"请先在 API 配置中保存{label}服务的地址、密钥和模型。")
        normalize_base_url(profile["base_url"])
        if role == "file_asr" and profile.get("speech_api") != "minimax":
            raise ProviderError("精准校验使用 MiniMax 音频识别，请在 ASR 配置中选择 MiniMax 原生语音服务。")
        if role != "file_asr" and not re.search(r"(?:^|/)gpt(?:[-_.]|$)", model, re.I):
            raise ProviderError(f"请为{label}选择服务商实际支持的 GPT 模型。")
        pins.append({"role": role, "label": label, "provider_id": profile["id"],
                     "provider": profile.get("name") or profile["id"], "model": model,
                     "fingerprint": _fingerprint(profile, role == "file_asr"),
                     "response_format": route.get("response_format", "verbose_json")})
    return {"version": 1, "chain": pins, "glossary": settings.get("glossary", ""),
            "language": "ja", "target_language": "zh-CN"}


def resolve_refinement_settings(settings_store, snapshot):
    """Resume the pinned chain; a changed destination must never receive audio."""
    settings = deepcopy(settings_store.load())
    profiles, _ = private_configuration(settings_store)
    settings.update(service_profiles_enabled=False, glossary=snapshot.get("glossary", ""))
    pins = snapshot.get("chain") or []
    if snapshot.get("version") != 1 or {p.get("role") for p in pins} != {r for r, _ in ROLES}:
        raise ProviderError("精准校验服务记录不完整，请保留原录音并检查配置。")
    for pin in pins:
        role = pin["role"]
        profile = next((p for p in profiles if p.get("id") == pin.get("provider_id")), {})
        if not profile or _fingerprint(profile, role == "file_asr") != pin.get("fingerprint"):
            raise ProviderError(f"本项目的{pin['label']}连接已删除或地址、协议已改变，请恢复原连接后继续精准校验。")
        if not profile.get("enabled", True) or not connection_key(profile):
            raise ProviderError(f"请恢复本项目的{pin['label']}连接和密钥后继续。")
        prefix = "import_transcription" if role == "file_asr" else role
        settings.update({f"{prefix}_base_url": normalize_base_url(profile["base_url"]),
                         f"{prefix}_api_key": connection_key(profile), f"{prefix}_model": pin["model"]})
        if role == "file_asr":
            settings.update(import_transcription_api="minimax",
                            import_transcription_response_format=pin["response_format"])
        else:
            settings[f"{prefix}_api"] = profile.get("text_api", "chat_completions")
            settings[f"{prefix}_anthropic_auth"] = profile.get("anthropic_auth", "api_key")
    return settings


def saved_audio_manifest(store, source):
    """Read WAV headers only and reject missing parts, links or unfinished captures."""
    if source.get("source_type") == "import" or source.get("status") not in SAVED_STATES:
        raise ProviderError("请先结束直播录制并保存音频，再开始精准校验。")
    names = [item["name"] for item in store.files(source) if _is_audio_name(item["name"])]
    declared = [part.get("name") for part in source.get("audio_parts", [])]
    if not names or declared and names != declared:
        raise ProviderError("直播音频分卷不完整，请保留原文件并检查录音。")
    result = []
    try:
        for name in names:
            path = store.file_path(source["id"], name)
            with path.open("rb") as raw, wave.open(raw, "rb") as reader:
                frames, rate = reader.getnframes(), reader.getframerate()
                channels, width = reader.getnchannels(), reader.getsampwidth()
                if frames <= 0 or not 8000 <= rate <= 192000 or channels not in (1, 2) or width != 2:
                    raise ValueError("invalid PCM")
                stat = path.stat()
                size = stat.st_size
                if raw.tell() + frames * channels * width > size:
                    raise ValueError("truncated WAV")
                result.append({"name": name, "size": size, "frames": frames,
                               "sample_rate": rate, "channels": channels, "sample_width": width,
                               "modified_ns": stat.st_mtime_ns})
    except (OSError, ValueError, EOFError, wave.Error):
        raise ProviderError("已保存的直播音频缺失、格式异常或不可读取，请检查原文件。") from None
    return result


def refinement_sources(store, session):
    source = store.load(session["refinement_source_id"])
    manifest = saved_audio_manifest(store, source)
    if manifest != session.get("refinement_source_parts"):
        raise ProviderError("原直播音频已改变，请保留当前项目并检查文件，不能自动继续上传。")
    return [store.file_path(source["id"], item["name"]) for item in manifest]


def existing_refinement(store, identifier):
    # Derived projects carry the link, keeping the original JSON and exports intact.
    return next((item for item in store.list() if item.get("refinement_source_id") == identifier), None)


def public_chain(snapshot):
    return [{k: pin[k] for k in ("role", "label", "provider", "model")} for pin in snapshot.get("chain", [])]
