"""Non-generation configuration probes. No audio or subtitle text is uploaded."""
import asyncio
import httpx

from .config import anthropic_endpoint_url, effective_provider, endpoint_url


def probe_provider(settings, role):
    provider = effective_provider(settings, role)
    if role in ("translation", "correction"):
        provider.update(text_api=settings.get(f"{role}_api") or settings.get("translation_api") or "responses",
                        anthropic_auth=settings.get(f"{role}_anthropic_auth") or "api_key")
    return provider


def _probe_identity(provider):
    anthropic = provider.get("text_api") == "anthropic"
    return (provider["base_url"], provider["api_key"], anthropic,
            provider.get("anthropic_auth", "api_key") if anthropic else None)


async def check_configuration(settings, client):
    local = settings.get("transcription_mode") == "local"
    roles = ("translation",) if local else ("transcription", "translation")
    requests = {}
    for role in roles:
        provider = probe_provider(settings, role)
        identity = _probe_identity(provider)
        if identity not in requests:
            requests[identity] = asyncio.create_task(fetch_available_models(provider, client))
    result = {}
    if local:
        from .local_transcriber import local_model_ready
        ready = local_model_ready(settings.get("transcription_model") or "tiny")
        result["transcription"] = {"ok": ready, "model_found": ready, "message":
            "本机语音模型文件已准备完成，音频在本机识别。" if ready else "本机语音模型尚未下载完成，请等待模型准备完成。"}
    for role in roles:
        provider = probe_provider(settings, role)
        probe = await requests[_probe_identity(provider)]
        result[role] = {key: value for key, value in probe.items() if key != "models"}
        if probe["ok"]:
            model = settings[f"{role}_model"]
            found = model in probe["models"]
            result[role]["model_found"] = found
            result[role]["message"] = (
                "地址与鉴权正常，已找到所选模型。实际接口支持和效果仍需试录确认。"
                if found else "地址与鉴权正常，但列表未列出所选模型；请核对平台模型名称并试录确认。"
            )
    return result


async def fetch_available_models(provider, client):
    if not provider["base_url"]:
        return {"ok": False, "message": "尚未填写此服务的 API 基础地址。"}
    if not provider["api_key"]:
        return {"ok": False, "message": "尚未配置此服务的 API 密钥。"}
    try:
        if provider.get("text_api") == "anthropic":
            url = anthropic_endpoint_url(provider["base_url"], "models")
            headers = {"anthropic-version": "2023-06-01"}
            if provider.get("anthropic_auth", "api_key") == "bearer":
                headers["Authorization"] = "Bearer " + provider["api_key"]
            else:
                headers["x-api-key"] = provider["api_key"]
        else:
            url = endpoint_url(provider["base_url"], "models")
            headers = {"Authorization": "Bearer " + provider["api_key"]}
        response = await client.get(
            url, headers=headers, follow_redirects=False,
        )
        if response.status_code in (401, 403):
            return {"ok": False, "message": "服务拒绝鉴权，请检查该平台的密钥与访问权限。"}
        if response.status_code in (404, 405):
            return {"ok": False, "message": "平台未提供此协议的模型列表，无法自动检查；请核对基础地址，或按平台文档手动填写模型名称。"}
        if response.is_redirect:
            return {"ok": False, "message": "服务地址返回跳转，请使用平台提供的最终 API 基础地址。"}
        if response.status_code >= 400:
            return {"ok": False, "message": f"配置检查未完成（HTTP {response.status_code}），请检查服务状态或额度。"}
        body = response.json()
        models = body.get("data") if isinstance(body, dict) else None
        if not isinstance(models, list):
            return {"ok": False, "message": "响应不是兼容的模型列表，请核对 API 基础地址；以实际试录为准。"}
        if body.get("has_more") is True or body.get("next_page") or body.get("next"):
            return {"ok": False, "message": "平台返回了分页模型列表，暂时无法获取完整列表。请手动填写模型名称。"}
        names = {
            item["id"] for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and item["id"].strip() and len(item["id"]) <= 256
            and not any(ord(character) < 32 or ord(character) == 127 for character in item["id"])
        }
        return {"ok": True, "models": names}
    except httpx.TimeoutException:
        return {"ok": False, "message": "获取模型列表超时，请稍后重试或检查 API 服务。"}
    except (httpx.HTTPError, ValueError, TypeError):
        return {"ok": False, "message": "无法完成配置检查，请检查网络、证书与 API 基础地址。"}
