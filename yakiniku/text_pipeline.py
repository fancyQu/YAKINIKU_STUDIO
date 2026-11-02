"""Separate Japanese proofreading and translation with durable stage checkpoints."""
from __future__ import annotations

import inspect

from .config import effective_provider
from .provider import ProviderError


def text_options(settings, role):
    model = settings.get(f"{role}_model") or ""
    if not model and not settings.get("service_profiles_enabled"):
        model = settings.get("translation_model") or ""
    options = {
        **effective_provider(settings, role),
        "model": model,
        "api_format": settings.get(f"{role}_api") or settings.get("translation_api") or "responses",
        "glossary": settings.get("glossary", ""),
    }
    if options["api_format"] == "anthropic":
        options["anthropic_auth"] = settings.get(f"{role}_anthropic_auth") or "api_key"
    return options


def validate_text_settings(settings):
    for role, label in (("correction", "日语校对"), ("translation", "中文翻译")):
        options = text_options(settings, role)
        if not options["base_url"] or not options["api_key"] or not str(options["model"]).strip():
            raise ProviderError(f"请先配置{label}服务的 API 地址、密钥和模型。")
        if options["api_format"] not in ("responses", "chat_completions", "anthropic"):
            raise ProviderError(f"请选择{label}服务支持的文字接口格式。")
        if options.get("anthropic_auth", "api_key") not in ("api_key", "bearer"):
            raise ProviderError(f"请选择{label}服务支持的 Anthropic 鉴权方式。")


async def _checkpoint(callback, segment):
    if callback is not None:
        result = callback(segment)
        if inspect.isawaitable(result):
            await result


class TextPipeline:
    def __init__(self, corrector, translator):
        self.corrector = corrector
        self.translator = translator

    async def process_segment(self, segment, context, checkpoint=None):
        segment.setdefault("ja_raw", segment["ja"])
        if segment.get("correction_status") != "done":
            segment.update(correction_status="pending", text_stage="correcting")
            try:
                corrected = await self.corrector.correct(segment["ja_raw"], context=context)
                if not isinstance(corrected, str) or not corrected.strip():
                    raise ProviderError("日语校对服务未返回有效原文，已有字幕和音频已保留。")
            except BaseException:
                segment.update(correction_status="failed", text_stage="correction_failed")
                raise
            segment.update(ja=corrected.strip(), correction_status="done",
                           correction_model=getattr(self.corrector, "model", ""),
                           correction_api=getattr(self.corrector, "api_format", ""),
                           text_stage="translating", translation_status="pending")
            # Save the corrected Japanese before the next network operation.
            await _checkpoint(checkpoint, segment)
        if not segment.get("zh"):
            segment.update(text_stage="translating", translation_status="pending")
            try:
                translated = await self.translator.translate(segment["ja"], context=context)
                if not isinstance(translated, str) or not translated.strip():
                    raise ProviderError("中文翻译服务未返回有效译文，校对后的日语已保留。")
            except BaseException:
                segment.update(text_stage="translation_failed", translation_status="failed")
                raise
            segment.update(zh=translated.strip(),
                           translation_model=getattr(self.translator, "model", ""),
                           translation_api=getattr(self.translator, "api_format", ""))
        segment.update(text_stage="completed", translation_status="done")

    async def close(self):
        try:
            await self.translator.close()
        finally:
            if self.corrector is not self.translator:
                await self.corrector.close()


async def create_text_processor(settings, translator_factory):
    """Keep injected legacy adapters usable while real clients run two stages."""
    translation_options = text_options(settings, "translation")
    correction_options = text_options(settings, "correction")
    translator = translator_factory(**translation_options)
    if not callable(getattr(translator, "correct", None)):
        return translator
    try:
        corrector = translator_factory(**correction_options)
    except BaseException:
        await translator.close()
        raise
    return TextPipeline(corrector, translator)


async def process_segment(processor, segment, context, checkpoint=None):
    segment.setdefault("ja_raw", segment["ja"])
    if callable(getattr(processor, "process_segment", None)):
        await processor.process_segment(segment, context, checkpoint=checkpoint)
    elif callable(getattr(processor, "translate_and_correct", None)):
        # Compatibility for older injected adapters; production uses TextPipeline.
        result = await processor.translate_and_correct(segment["ja_raw"], context=context)
        segment.update(ja=result["ja"], zh=result["zh"])
    else:
        segment["zh"] = await processor.translate(segment["ja"], context=context)
