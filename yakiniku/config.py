"""Normalize compatible API configuration without disclosing credentials."""

import ipaddress
import os
import re
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit, urlunsplit


DEFAULT_API_BASE_URL = ""
_ENDPOINT_SUFFIXES = (
    "/chat/completions", "/responses", "/audio/transcriptions", "/realtime", "/messages", "/speech_to_text",
)


def normalize_base_url(value: str) -> str:
    """Validate a base URL, preserving gateway prefixes and version paths.

    Remote endpoints require TLS. Plain HTTP is permitted only for a local
    loopback service; DNS names such as localhost are checked syntactically.
    """
    if not isinstance(value, str) or not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ) or "\\" in value:
        raise ValueError("请填写有效的 API 基础地址，地址中不能包含空白字符。")
    if "?" in value or "#" in value:
        raise ValueError("API 基础地址不能包含查询参数或片段。")
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.netloc.startswith("[") and not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc):
            raise ValueError
        if not parsed.netloc.startswith("[") and any(character in parsed.netloc for character in "[]"):
            raise ValueError
        hostname = parsed.hostname
        port = parsed.port
        if not hostname or (port is not None and not 1 <= port <= 65535):
            raise ValueError
        # A dangling colon is not a usable explicit port.
        if parsed.netloc.endswith(":"):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("API 基础地址无效，且不能在地址中填写用户名、密码或密钥。") from None

    loopback = False
    try:
        address = ipaddress.ip_address(hostname)
        if "%" in hostname:
            raise ValueError
        loopback = address.is_loopback
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            loopback = loopback or address.ipv4_mapped.is_loopback
        normalized_host = f"[{address.compressed}]" if address.version == 6 else str(address)
    except ValueError:
        if parsed.netloc.startswith("[") or re.fullmatch(r"[0-9.]+", hostname):
            raise ValueError("API 基础地址的主机名无效。") from None
        try:
            normalized_host = hostname.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            raise ValueError("API 基础地址的主机名无效。") from None
        domain = normalized_host[:-1] if normalized_host.endswith(".") else normalized_host
        labels = domain.split(".")
        if len(domain) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("API 基础地址的主机名无效。")
        loopback = domain == "localhost"

    if parsed.scheme.lower() == "http" and not loopback:
        raise ValueError("远程 API 必须使用 HTTPS；本机 localhost 或回环 IP 可使用 HTTP。")
    path = parsed.path.rstrip("/")
    if any(path.lower().endswith(suffix) for suffix in _ENDPOINT_SUFFIXES):
        raise ValueError("请填写 API 基础地址，例如 https://服务商域名/v1，而不是完整的请求接口地址。")
    netloc = normalized_host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def endpoint_url(base_url: str, path: str) -> str:
    return normalize_base_url(base_url) + "/" + path.lstrip("/")


def anthropic_endpoint_url(base_url: str, path: str = "messages") -> str:
    """Anthropic SDK bases omit /v1; explicit versioned proxy bases also work."""
    base = normalize_base_url(base_url)
    return base + ("/" if base.endswith("/v1") else "/v1/") + path.lstrip("/")


def anthropic_messages_url(base_url: str) -> str:
    return anthropic_endpoint_url(base_url, "messages")


def websocket_url(base_url: str) -> str:
    endpoint = urlsplit(endpoint_url(base_url, "realtime"))
    return urlunsplit((
        "wss" if endpoint.scheme == "https" else "ws",
        endpoint.netloc, endpoint.path, "intent=transcription", "",
    ))


def effective_provider(settings: Mapping[str, Any], role: str) -> Dict[str, str]:
    """Resolve independent role credentials, falling back to shared settings."""
    if role not in ("transcription", "translation", "import_transcription", "correction"):
        raise ValueError("无效的 API 用途。")
    task = {"import_transcription": "file_asr", "correction": "correction", "translation": "translation"}.get(role)
    if task and settings.get("service_profiles_enabled"):
        route = (settings.get("task_routes") or {}).get(task) or {}
        selected = next((item for item in settings.get("service_profiles", [])
                         if item.get("id") == route.get("provider_id") and item.get("enabled", True)), {})
        base = selected.get("base_url") or ""
        key = (os.environ.get("OPENAI_API_KEY", "") if selected.get("use_environment_key") else selected.get("api_key")) or ""
        return {"api_key": str(key).strip(), "base_url": normalize_base_url(base) if base else ""}
    if role == "correction":
        translation = effective_provider(settings, "translation")
        base = settings.get("correction_base_url") or translation["base_url"]
        return {"api_key": str(settings.get("correction_api_key") or translation["api_key"]).strip(),
                "base_url": normalize_base_url(base) if base else ""}
    base_url = settings.get(f"{role}_base_url") or settings.get("api_base_url") or DEFAULT_API_BASE_URL
    own_key = str(settings.get(f"{role}_api_key") or "").strip()
    shared_key = str(settings.get("api_key") or "").strip()
    return {
        "api_key": own_key or shared_key,
        "base_url": normalize_base_url(base_url) if base_url else "",
    }


def materialize_task_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Expose task model/protocol fields to existing pipelines without copying keys."""
    if not settings.get("service_profiles_enabled"):
        return settings
    providers = {item["id"]: item for item in settings.get("service_profiles", [])}
    routes = settings.get("task_routes") or {}
    for task, prefix in (("file_asr", "import_transcription"), ("correction", "correction"),
                         ("translation", "translation")):
        route = routes.get(task) or {}
        provider = providers.get(route.get("provider_id"), {})
        settings[f"{prefix}_model"] = route.get("model") or ""
        if task == "file_asr":
            settings["import_transcription_api"] = provider.get("speech_api", "openai")
            settings["import_transcription_response_format"] = route.get("response_format", "verbose_json")
        else:
            settings[f"{prefix}_api"] = provider.get("text_api", "chat_completions")
            settings[f"{prefix}_anthropic_auth"] = provider.get("anthropic_auth", "api_key")
    return settings
