"""Named API connections and explicit task routing, with private credentials."""
import os
import re
import hashlib
import json
from copy import deepcopy
from typing import Dict, List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .config import effective_provider, normalize_base_url


PRESETS = [
    {"id": "custom", "name": "自定义 / OpenAI 兼容", "suggested_base_url": "",
     "text_api": "chat_completions", "speech_api": "openai",
     "help": "填写服务商的完整基础地址；文字与音频接口支持以该平台文档为准。"},
    {"id": "minimax", "name": "MiniMax", "suggested_base_url": "https://api.minimax.io/v1",
     "text_api": "chat_completions", "speech_api": "minimax", "asr_model_hint": "asr-1.0",
     "help": "原生音频识别支持日语。模型列表可能不含语音模型，可按官方文档手填 asr-1.0。",
     "docs_url": "https://platform.minimax.io/docs/api-reference/speech-to-text.md"},
    {"id": "minimax_anthropic", "name": "MiniMax · Anthropic 文字", "suggested_base_url": "https://api.minimax.io/anthropic",
     "text_api": "anthropic", "anthropic_auth": "bearer", "speech_api": "none",
     "help": "MiniMax 的 Anthropic 兼容文字接口。与原生语音识别使用不同基础地址，请分别添加文字与语音连接。",
     "docs_url": "https://platform.minimax.io/docs/api-reference/text-anthropic-api.md"},
    {"id": "anthropic", "name": "Anthropic", "suggested_base_url": "https://api.anthropic.com",
     "text_api": "anthropic", "anthropic_auth": "api_key", "speech_api": "none",
     "help": "Anthropic Messages 文字协议，使用 x-api-key 鉴权；可手动填写兼容服务地址与模型。",
     "docs_url": "https://platform.claude.com/docs/en/api/overview"},
    {"id": "openai", "name": "OpenAI", "suggested_base_url": "https://api.openai.com/v1",
     "text_api": "responses", "speech_api": "openai",
     "help": "分别选择账户实际可用的语音模型与文字模型。",
     "docs_url": "https://developers.openai.com/api/docs/guides/speech-to-text"},
    {"id": "deepseek", "name": "DeepSeek", "suggested_base_url": "https://api.deepseek.com",
     "text_api": "chat_completions", "speech_api": "none",
     "help": "使用官方 OpenAI 兼容文字接口，可独立用于日语校对或中文翻译。",
     "docs_url": "https://api-docs.deepseek.com/"},
    {"id": "text_compatible", "name": "其他文字服务（OpenAI 兼容）", "suggested_base_url": "",
     "text_api": "chat_completions", "speech_api": "none",
     "help": "适用于提供兼容文字接口的服务。未配置语音协议时不能用于音频识别。"},
]


class ConnectionInput(BaseModel):
    id: str = Field(max_length=80)
    name: str = Field(max_length=80)
    vendor: str = Field(default="custom", max_length=80)
    base_url: str = Field(default="", max_length=2000)
    api_key: Optional[str] = Field(default=None, max_length=500)
    clear_api_key: bool = False
    text_api: Literal["chat_completions", "responses", "anthropic"] = "chat_completions"
    anthropic_auth: Literal["api_key", "bearer"] = "api_key"
    speech_api: Literal["openai", "minimax", "none"] = "openai"
    enabled: bool = True
    scope: Optional[Literal["asr", "text", "shared"]] = None
    section: Optional[Literal["asr", "text"]] = None
    expected_revision: str = Field(default="", max_length=64)


class TaskRouteInput(BaseModel):
    provider_id: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=256)
    response_format: Literal["verbose_json", "json", "srt"] = "verbose_json"


class TaskRoutesInput(BaseModel):
    file_asr: TaskRouteInput = Field(default_factory=TaskRouteInput)
    correction: TaskRouteInput = Field(default_factory=TaskRouteInput)
    translation: TaskRouteInput = Field(default_factory=TaskRouteInput)


class ServiceConfigInput(BaseModel):
    providers: List[ConnectionInput] = Field(max_length=32)
    routes: TaskRoutesInput


class SectionConfigInput(BaseModel):
    providers: List[ConnectionInput] = Field(max_length=32)
    routes: Dict[str, TaskRouteInput] = Field(default_factory=dict)
    expected_revision: str = Field(default="", max_length=64)


class ConfigurationConflict(ValueError):
    """The current module changed after this page loaded its draft."""


class ConnectionModelsInput(BaseModel):
    provider_id: str = Field(default="", max_length=80)
    base_url: str = Field(default="", max_length=2000)
    api_key: Optional[str] = Field(default=None, max_length=500)
    clear_api_key: bool = False
    text_api: Optional[Literal["chat_completions", "responses", "anthropic"]] = None
    anthropic_auth: Optional[Literal["api_key", "bearer"]] = None


def _clean(value, label):
    value = str(value or "").strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label}不能包含控制字符。")
    return value


def _key(value):
    value = str(value or "").strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("API 密钥不能包含空白或控制字符。")
    return value


def _origin(value):
    p = urlsplit(value)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


def connection_key(connection):
    if connection.get("use_environment_key"):
        return os.environ.get("OPENAI_API_KEY", "").strip()
    return connection.get("api_key") or ""


def provider_scope(provider, routes):
    """Classify old connections without writing settings or copying credentials."""
    if provider.get("scope") in ("asr", "text", "shared"):
        return provider["scope"]
    identifier = provider.get("id")
    speech = bool(identifier and (routes.get("file_asr") or {}).get("provider_id") == identifier)
    text = any(identifier and (routes.get(task) or {}).get("provider_id") == identifier
               for task in ("correction", "translation"))
    if speech and text:
        return "shared"
    if speech:
        return "asr"
    if text:
        return "text"
    return "asr" if provider.get("speech_api", "none") != "none" else "text"


def _separate_shared_connections(profiles, routes):
    """Project old shared entries into independent modules without a settings write.

    Keep the original identity for text so live snapshots keep resolving it.
    Only the ASR route moves to a deterministic copy of the same connection.
    """
    profiles, routes = deepcopy(profiles), deepcopy(routes)
    used = {profile["id"] for profile in profiles}
    result = []
    for profile in profiles:
        if provider_scope(profile, routes) != "shared":
            result.append(profile)
            continue
        original_id = profile["id"]
        suffix = "-asr"
        identifier = original_id[:80 - len(suffix)] + suffix
        index = 2
        while identifier in used:
            suffix = "-asr-" + str(index)
            identifier = original_id[:80 - len(suffix)] + suffix
            index += 1
        used.add(identifier)
        speech = {**deepcopy(profile), "id": identifier, "scope": "asr",
                  "name": (str(profile.get("name") or original_id) + " · 音频识别")[:80]}
        profile["scope"] = "text"
        result.extend((profile, speech))
        if (routes.get("file_asr") or {}).get("provider_id") == original_id:
            routes["file_asr"]["provider_id"] = identifier
    return result, routes


def section_revision(profiles, routes, section):
    tasks = ("file_asr",) if section == "asr" else ("correction", "translation")
    payload = {"providers": [p for p in profiles if provider_scope(p, routes) == section],
               "routes": {task: routes.get(task) or {} for task in tasks}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_revision(profiles, routes, section, expected):
    if expected and section_revision(profiles, routes, section) != expected:
        raise ConfigurationConflict("本模块配置已在另一页面更新。当前输入已保留，请重新读取本模块后再保存。")


def private_configuration(store):
    """Preview a legacy migration without writing or persisting environment keys."""
    raw = store._stored()
    if raw.get("service_profiles_enabled"):
        return _separate_shared_connections(raw.get("service_profiles", []), raw.get("task_routes", {}))
    loaded = store.load()
    environment = os.environ.get("OPENAI_API_KEY", "").strip()
    profiles, routes, identities = [], {}, {}
    for task, role, label in (("translation", "translation", "现有翻译服务"),
                              ("correction", "correction", "现有校对服务"),
                              ("file_asr", "import_transcription", "现有音频服务")):
        provider = effective_provider(loaded, role)
        own = raw.get(f"{role}_api_key")
        if role == "correction" and not own:
            own = raw.get("translation_api_key")
        use_environment = bool(environment and not own)
        text_api = (loaded.get("correction_api") or loaded.get("translation_api") or "responses") if task == "correction" else loaded.get("translation_api", "responses")
        anthropic_auth = loaded.get(f"{role}_anthropic_auth", "api_key")
        speech_api = loaded.get("import_transcription_api", "openai")
        identity = (provider["base_url"], provider["api_key"], text_api, speech_api, anthropic_auth)
        identifier = identities.get(identity)
        if identifier is None:
            identifier = "existing-" + str(len(profiles) + 1)
            identities[identity] = identifier
            profiles.append({"id": identifier, "name": label, "vendor": "custom",
                             "base_url": provider["base_url"], "api_key": "" if use_environment else provider["api_key"],
                             "use_environment_key": use_environment, "text_api": text_api,
                             "speech_api": speech_api, "enabled": True,
                             "anthropic_auth": anthropic_auth})
        routes[task] = {"provider_id": identifier, "model": str(loaded.get(f"{role}_model") or
                        (loaded.get("translation_model") if task == "correction" else "") or "")}
        if task == "file_asr":
            routes[task]["response_format"] = loaded.get("import_transcription_response_format", "verbose_json")
    return _separate_shared_connections(profiles, routes)


def public_configuration(store, locked=False):
    profiles, routes = private_configuration(store)
    ready = {}
    for task in ("file_asr", "correction", "translation"):
        route = routes.get(task) or {}
        profile = next((p for p in profiles if p["id"] == route.get("provider_id")), {})
        ready[task] = bool(profile.get("enabled", True) and profile.get("base_url")
                           and connection_key(profile) and route.get("model")
                           and (task != "file_asr" or profile.get("speech_api") in ("openai", "minimax")))
    settings = store.load()
    return {"providers": [{**{k: v for k, v in p.items() if k not in ("api_key", "use_environment_key")},
                             "scope": provider_scope(p, routes),
                             "key_configured": bool(connection_key(p)),
                             "key_from_environment": bool(p.get("use_environment_key"))} for p in profiles],
            "routes": routes, "ready": ready, "locked": locked,
            "section_revisions": {section: section_revision(profiles, routes, section) for section in ("asr", "text")},
            "live": {"mode": settings.get("transcription_mode", "local"), "model": settings.get("transcription_model", "tiny")},
            "presets": PRESETS}


def _prepare_connection(draft, old, routes):
    value = draft.model_dump()
    value.pop("section", None)
    value.pop("expected_revision", None)
    identifier = value["id"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identifier):
        raise ValueError("服务商连接编号无效。")
    name = _clean(value["name"], "服务商名称")
    if not name:
        raise ValueError("请填写服务商名称。")
    base = normalize_base_url(value["base_url"].strip()) if value["base_url"].strip() else ""
    entered = _key(value.get("api_key"))
    clear = value.pop("clear_api_key")
    old_key = connection_key(old)
    if old_key and base and old.get("base_url") and _origin(base) != _origin(old["base_url"]) and not entered and not clear:
        raise ValueError("服务地址已更换，请重新填写用于该服务的 API 密钥。")
    old_scope = provider_scope(old, routes) if old else None
    scope = value.get("scope") or old_scope or provider_scope(value, routes)
    if old and scope != old_scope:
        raise ValueError("这个连接的所属模块已改变，请重新读取本模块后再保存；ASR 与文字连接不能互相覆盖。")
    value.update(name=name, vendor=_clean(value["vendor"], "厂商名称"), base_url=base, scope=scope,
                 api_key="" if clear else entered or old.get("api_key", ""),
                 use_environment_key=bool(not entered and not clear and old.get("use_environment_key")))
    return value


def _validate_configuration(profiles, routes):
    if len(profiles) > 32:
        raise ValueError("最多可以保存 32 个服务商连接。")
    by_id = {p["id"]: p for p in profiles}
    if len(by_id) != len(profiles):
        raise ValueError("服务商连接编号重复。")
    for task, route in routes.items():
        identifier = route.get("provider_id") or ""
        if identifier and identifier not in by_id:
            raise ValueError("处理流程引用了不存在的服务商连接。")
        selected = by_id.get(identifier, {})
        scope = provider_scope(selected, routes)
        if identifier and ((task == "file_asr" and scope == "text") or (task != "file_asr" and scope == "asr")):
            raise ValueError("ASR 和校对与翻译使用各自模块中的服务商，请重新选择。")
        if task == "file_asr" and route.get("model") and selected.get("speech_api") == "none":
            raise ValueError("所选连接只提供文字接口，请为音频识别选择支持的语音协议。")


def _persist_configuration(store, profiles, routes):
    profiles, routes = _separate_shared_connections(profiles, routes)
    _validate_configuration(profiles, routes)
    live_route = (store._stored().get("live_settings") or {}).get("translation_route") or {}
    live_provider_id = live_route.get("provider_id")
    if live_provider_id:
        referenced = next((p for p in profiles if p.get("id") == live_provider_id), {})
        if not referenced or provider_scope(referenced, routes) == "asr":
            raise ValueError("直播设置仍引用这个文字连接，请先在直播设置中选择其他连接或清空引用，再移除。")
    store.save({"service_profiles_enabled": True, "service_profiles": profiles, "task_routes": routes})


def save_configuration(store, body):
    old_profiles, _ = private_configuration(store)
    previous = {p["id"]: p for p in old_profiles}
    routes = body.routes.model_dump()
    for route in routes.values():
        route["model"] = _clean(route["model"], "模型名称")
    profiles = [_prepare_connection(draft, previous.get(draft.id, {}), routes) for draft in body.providers]
    _persist_configuration(store, profiles, routes)


def save_connection(store, body):
    """Save the editor currently on screen, leaving every task route untouched."""
    profiles, routes = private_configuration(store)
    old = next((p for p in profiles if p["id"] == body.id), {})
    section = body.section or (provider_scope(old, routes) if old else body.scope or provider_scope(body.model_dump(), routes))
    if section not in ("asr", "text"):
        raise ValueError("请在 ASR 或校对与翻译模块中分别保存服务商；旧版共用连接请重新读取后再编辑。")
    if body.scope == "shared" or old and provider_scope(old, routes) != section:
        raise ValueError("这是旧版共用或另一模块的连接，请重新读取本模块后再保存。")
    _check_revision(profiles, routes, section, body.expected_revision)
    if body.scope is None:
        body = body.model_copy(update={"scope": section})
    connection = _prepare_connection(body, old, routes)
    updated = [connection if p["id"] == body.id else p for p in profiles]
    if not old:
        updated.append(connection)
    _persist_configuration(store, updated, routes)


def save_section(store, section, body):
    """Commit one module; omitted connections and routes in the other survive."""
    if section not in ("asr", "text"):
        raise ValueError("请选择 ASR 或校对与翻译配置。")
    allowed_tasks = {"file_asr"} if section == "asr" else {"correction", "translation"}
    if set(body.routes) - allowed_tasks:
        raise ValueError("此保存操作只能修改当前模块的处理步骤。")
    profiles, old_routes = private_configuration(store)
    _check_revision(profiles, old_routes, section, body.expected_revision)
    routes = {task: dict(route) for task, route in old_routes.items()}
    for task, draft in body.routes.items():
        route = draft.model_dump()
        route["model"] = _clean(route["model"], "模型名称")
        routes[task] = route
    previous = {p["id"]: p for p in profiles}
    updates = {}
    for draft in body.providers:
        if draft.id in updates:
            raise ValueError("服务商连接编号重复。")
        old = previous.get(draft.id, {})
        old_scope = provider_scope(old, old_routes) if old else None
        if old and old_scope not in (section, "shared"):
            raise ValueError("不能在当前模块修改另一模块的服务商。")
        if not old and draft.scope is None:
            draft = draft.model_copy(update={"scope": section})
        connection = _prepare_connection(draft, old, old_routes)
        if connection["scope"] not in (section, "shared") or (connection["scope"] == "shared" and old_scope != "shared"):
            raise ValueError("请将新服务商保存在当前模块中。")
        updates[draft.id] = connection
    merged = []
    for old in profiles:
        identifier = old["id"]
        scope = provider_scope(old, old_routes)
        if scope not in (section, "shared"):
            merged.append(old)
        elif identifier in updates:
            merged.append(updates.pop(identifier))
        elif any((route or {}).get("provider_id") == identifier for task, route in old_routes.items() if task not in allowed_tasks):
            raise ValueError("这个旧连接仍由另一模块使用，请先解除另一模块的引用后再移除。")
    merged.extend(updates.values())
    _persist_configuration(store, merged, routes)


def discovery_provider(store, body):
    profiles, _ = private_configuration(store)
    old = next((p for p in profiles if p["id"] == body.provider_id), {})
    base = normalize_base_url(body.base_url.strip()) if body.base_url.strip() else ""
    entered = _key(body.api_key)
    key = "" if body.clear_api_key else entered or connection_key(old)
    if old.get("base_url") and connection_key(old) and base and _origin(old["base_url"]) != _origin(base) and not entered and not body.clear_api_key:
        raise ValueError("服务地址已更换，请重新填写用于该服务的 API 密钥。")
    return {"base_url": base, "api_key": key,
            "text_api": body.text_api or old.get("text_api", "chat_completions"),
            "anthropic_auth": body.anthropic_auth or old.get("anthropic_auth", "api_key")}
