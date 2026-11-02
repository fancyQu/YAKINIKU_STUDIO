"""Independent live routes. Persist references, never credentials in a session snapshot."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import normalize_base_url
from .provider import ProviderError
from .service_config import connection_key, private_configuration, provider_scope

DEFAULT_LOCAL_TRANSLATION_MODEL = "qwen3-4b-instruct-2507-4bit"

LIVE_MODELS = {
    "mlx": ("mlx-whisper-small", "mlx-whisper-large-v3-turbo"),
    "tiny": ("tiny",),
}


class LiveTranslationRouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=256)


class LiveSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["mlx", "tiny"] = "mlx"
    model: str = Field(default="mlx-whisper-small", max_length=256)
    translation_route: LiveTranslationRouteInput = Field(default_factory=LiveTranslationRouteInput)
    translation_backend: Literal["cloud", "local"] = "cloud"
    local_translation_model: str = Field(default=DEFAULT_LOCAL_TRANSLATION_MODEL, max_length=128)
    glossary: str = Field(default="", max_length=12000)
    segment_seconds: float = Field(default=5.0, ge=1.0, le=10.0)
    silence_ms: int = Field(default=600, ge=200, le=2000)


class _SettingsView:
    def __init__(self, settings):
        self.settings = settings

    def _stored(self):
        return self.settings

    def load(self):
        return self.settings


def _store(value):
    return value if hasattr(value, "load") and hasattr(value, "_stored") else _SettingsView(value)


def _clean(value, label):
    value = str(value or "").strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label}不能包含控制字符。")
    return value


def _configuration(store_or_settings):
    store = _store(store_or_settings)
    settings = store.load()
    profiles, routes = private_configuration(store)
    saved = settings.get("live_settings") or {}
    # Legacy fields are previewed only until the user first saves independent live
    # settings. Reading never mutates the file routes or copies an environment key.
    if not saved:
        selected = routes.get("translation") or {}
        saved = {
            "backend": "mlx", "model": "mlx-whisper-small",
            "translation_route": {"provider_id": selected.get("provider_id", ""),
                                  "model": selected.get("model", "")},
            "glossary": settings.get("glossary", ""),
            "segment_seconds": settings.get("segment_seconds", 5), "silence_ms": 600,
        }
    config = LiveSettingsInput.model_validate(saved).model_dump()
    return settings, config, profiles, routes


def _profile(profiles, routes, identifier):
    return next((p for p in profiles if p.get("id") == identifier and
                 provider_scope(p, routes) != "asr"), {})


def _fingerprint(profile):
    fields = {name: profile.get(name, default) for name, default in (
        ("base_url", ""), ("text_api", "chat_completions"), ("anthropic_auth", "api_key"))}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _translation_ready(profile, route):
    return bool(profile and profile.get("enabled", True) and profile.get("base_url")
                and connection_key(profile) and str(route.get("model") or "").strip())


def public_live_settings(store, locked=False):
    settings, config, profiles, routes = _configuration(store)
    route = config["translation_route"]
    selected = _profile(profiles, routes, route["provider_id"])
    ready = (bool(config["local_translation_model"] in _local_models()) if config["translation_backend"] == "local"
             else _translation_ready(selected, route))
    public_profiles = []
    fields = ("id", "name", "vendor", "base_url", "text_api", "anthropic_auth", "enabled")
    for profile in profiles:
        if provider_scope(profile, routes) == "asr":
            continue
        public_profiles.append({**{key: profile[key] for key in fields if key in profile},
                                "key_configured": bool(connection_key(profile))})
    return {**config, "providers": public_profiles, "translation_ready": ready,
            "configuration_ready": ready, "configured": bool(settings.get("live_settings")),
            "locked": bool(locked), "language": "ja", "target_language": "zh-CN"}


def save_live_settings(store, body):
    settings, _, profiles, routes = _configuration(store)
    value = body.model_dump() if isinstance(body, LiveSettingsInput) else LiveSettingsInput.model_validate(body).model_dump()
    value["model"] = _clean(value["model"], "本机模型名称")
    if value["model"] not in LIVE_MODELS[value["backend"]]:
        raise ValueError("所选模型与本机后端不匹配，请选择列表中的已安装模型。")
    route = value["translation_route"]
    route["provider_id"] = _clean(route["provider_id"], "翻译服务商编号")
    route["model"] = _clean(route["model"], "翻译模型名称")
    value["local_translation_model"] = _clean(value["local_translation_model"], "本地翻译模型")
    if value["local_translation_model"] not in _local_models():
        raise ValueError("请选择下载列表中的本地翻译模型。")
    if value["translation_backend"] == "cloud" and route["provider_id"] and not _profile(profiles, routes, route["provider_id"]):
        raise ValueError("请选择已有的文字服务商连接；文件 ASR 连接不能用于直播翻译。")
    # This is the only write: all existing file routes, keys and legacy fields stay
    # byte-for-value intact. A missing model may be saved and prepared later.
    store.save({"live_settings": value})
    return public_live_settings(store)


def resolve_live_settings(store_or_settings, snapshot=None, require_translation=True):
    settings, config, profiles, routes = _configuration(store_or_settings)
    if snapshot is not None:
        config = LiveSettingsInput.model_validate({name: snapshot[name] for name in (
            "backend", "model", "translation_route", "translation_backend", "local_translation_model",
            "glossary", "segment_seconds", "silence_ms")
            if name in snapshot}).model_dump()
    route = config["translation_route"]
    profile = _profile(profiles, routes, route["provider_id"])
    local = config["translation_backend"] == "local"
    if local and config["local_translation_model"] not in _local_models():
        raise ProviderError("本场引用的本地翻译模型不在支持列表中，请恢复原模型。")
    if not local and snapshot and snapshot.get("provider_fingerprint") and (
            not profile or _fingerprint(profile) != snapshot["provider_fingerprint"]):
        raise ProviderError("本场直播引用的翻译连接已删除或地址、协议已变更，请恢复原连接后重试。")
    if require_translation and not local and not _translation_ready(profile, route):
        raise ProviderError("请在直播设置中选择可用的翻译连接和模型；不需要配置文件 ASR 或独立校对。")
    base = profile.get("base_url") or ""
    result = deepcopy(settings)
    # A private per-session copy, not a settings write. Prevent file task routing
    # from overriding the explicitly resolved live translation credentials.
    result.update(config)
    result.update(service_profiles_enabled=False, transcription_mode="local",
                  transcription_model=config["model"], live_asr_backend=config["backend"],
                  live_asr_model=config["model"],
                  translation_model=config["local_translation_model"] if local else route["model"],
                  translation_base_url="" if local else normalize_base_url(base) if base else "",
                  translation_api_key="" if local else connection_key(profile),
                  translation_api="local_mlx" if local else profile.get("text_api", "chat_completions"),
                  translation_anthropic_auth=profile.get("anthropic_auth", "api_key"),
                  live_translation_route=deepcopy(route),
                  live_provider_fingerprint=_fingerprint(profile) if profile and not local else "")
    return result


def live_snapshot(resolved_settings, model_state=None, translation_state=None):
    """Allowlist only. Never serialize an effective settings object wholesale."""
    result = {
        "schema_version": 2,
        "backend": resolved_settings["live_asr_backend"],
        "model": resolved_settings["live_asr_model"],
        "translation_route": deepcopy(resolved_settings["live_translation_route"]),
        "translation_backend": resolved_settings.get("translation_backend", "cloud"),
        "local_translation_model": resolved_settings.get("local_translation_model", DEFAULT_LOCAL_TRANSLATION_MODEL),
        "provider_fingerprint": resolved_settings.get("live_provider_fingerprint", ""),
        "glossary": resolved_settings.get("glossary", ""),
        "segment_seconds": resolved_settings.get("segment_seconds", 5.0),
        "silence_ms": resolved_settings.get("silence_ms", 600),
        "pre_roll_ms": 250, "language": "ja", "target_language": "zh-CN",
    }
    if model_state:
        result["model_source"] = {name: deepcopy(model_state[name]) for name in
                                  ("path", "version", "source", "revision") if name in model_state}
    if translation_state and result["translation_backend"] == "local":
        result["translation_model_source"] = {name: deepcopy(translation_state[name]) for name in
                                              ("path", "version", "source") if name in translation_state}
    return result


def _local_models():
    # Metadata-only import: cloud/Tiny installations need no MLX dependency.
    from .local_translation_models import MODEL_CATALOG
    return {item["id"] for item in MODEL_CATALOG}
