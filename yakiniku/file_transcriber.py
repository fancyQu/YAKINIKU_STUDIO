"""Explicit file ASR adapters, with honest timestamp provenance.

MiniMax protocol: https://platform.minimax.io/docs/api-reference/speech-to-text
"""
from __future__ import annotations

import asyncio
import io
import math
import re
import wave

import httpx

from .config import endpoint_url, normalize_base_url
from .provider import ProviderError, _error_message

MAX_TRANSCRIPTION_BYTES = 25_000_000
RESPONSE_FORMATS = frozenset({"verbose_json", "json", "srt"})
API_FORMATS = frozenset({"openai", "minimax"})


def wav_payload(audio):
    import numpy as np
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim != 1 or not len(samples) or not np.isfinite(samples).all():
        raise ProviderError("待转写音频片段无效，原始音频已保留。")
    if len(samples) * 2 + 44 >= MAX_TRANSCRIPTION_BYTES:
        raise ProviderError("语音片段超过文件转写大小上限，请使用更短的音频分块。")
    pcm = (np.clip(samples, -1, 1) * 32767).round().astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(pcm.tobytes())
    return output.getvalue()


def _cue(text, start, end, start_ms, end_ms, source):
    if not isinstance(text, str):
        raise ProviderError("语音服务返回的字幕内容格式无效，已有字幕已保留。")
    text = text.strip()
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError, OverflowError):
        raise ProviderError("语音服务返回的时间戳无效，已有字幕已保留。") from None
    if not math.isfinite(start) or not math.isfinite(end) or end < start:
        raise ProviderError("语音服务返回的时间戳无效，已有字幕已保留。")
    cue_start, cue_end = start_ms + round(start * 1000), start_ms + round(end * 1000)
    if not text or cue_end <= start_ms or cue_start >= end_ms:
        return None
    cue_start = max(start_ms, cue_start)
    cue_end = min(end_ms, max(cue_start + 1, cue_end))
    return {"ja": text, "start_ms": cue_start, "end_ms": cue_end,
            "timestamp_source": source,
            "alignment": "approximate" if source == "approximate" else "provider"}


def _word_cues(words, start_ms, end_ms):
    if not isinstance(words, list):
        raise ProviderError("语音服务返回的词级时间戳格式无效。")
    cues, text = [], ""
    first = last = None

    def flush():
        nonlocal text, first, last
        if text:
            cue = _cue(text, first, last, start_ms, end_ms, "api_words")
            if cue:
                cues.append(cue)
        text, first, last = "", None, None

    for item in words:
        if not isinstance(item, dict) or not isinstance(item.get("word", item.get("text")), str):
            raise ProviderError("语音服务返回的词级时间戳格式无效。")
        word = item.get("word", item.get("text"))
        try:
            begin, finish = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            raise ProviderError("语音服务返回的词级时间戳无效。") from None
        if not math.isfinite(begin) or not math.isfinite(finish) or finish < begin:
            raise ProviderError("语音服务返回的词级时间戳无效。")
        if text and (begin - last > .8 or len(text) >= 60):
            flush()
        if first is None:
            first = begin
        text += word
        last = finish
        if re.search(r"[。！？!?][\s\u3000]*$", word):
            flush()
    flush()
    return cues


def _approximate_cues(text, start_ms, end_ms):
    """Estimate from text length only, and label every estimate explicitly."""
    if not isinstance(text, str):
        raise ProviderError("语音服务未返回有效的转写文字。")
    text = text.strip()
    if not text:
        return []
    sentences = [value.strip() for value in re.findall(r"[^。！？!?\n]+[。！？!?]*|[。！？!?]+", text) if value.strip()]
    if not sentences:
        sentences = [text]
    total = sum(len(value) for value in sentences)
    duration = (end_ms - start_ms) / 1000
    cues, consumed = [], 0
    for sentence in sentences:
        begin = duration * consumed / total
        consumed += len(sentence)
        cue = _cue(sentence, begin, duration * consumed / total, start_ms, end_ms, "approximate")
        if cue:
            cues.append(cue)
    return cues


def parse_json_cues(body, start_ms, end_ms):
    if not isinstance(body, dict):
        raise ProviderError("语音服务未返回有效的 JSON 转写结果。")
    if "error" in body:
        raise ProviderError("语音服务未完成文件转写，请检查模型、接口格式和账户状态。")
    segments = body.get("segments")
    if segments is not None and not isinstance(segments, list):
        raise ProviderError("语音服务返回的分段时间戳格式无效。")
    if segments:
        cues = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise ProviderError("语音服务返回的分段时间戳格式无效。")
            if segment.get("words"):
                cues.extend(_word_cues(segment["words"], start_ms, end_ms))
                continue
            if "start" not in segment or "end" not in segment:
                raise ProviderError("语音服务返回的分段时间戳缺失，无法精确对齐。")
            cue = _cue(segment.get("text"), segment["start"], segment["end"],
                       start_ms, end_ms, "api_segments")
            if cue:
                cues.append(cue)
        return sorted(cues, key=lambda cue: (cue["start_ms"], cue["end_ms"]))
    if body.get("words"):
        return _word_cues(body["words"], start_ms, end_ms)
    if isinstance(body.get("text"), str):
        return _approximate_cues(body["text"], start_ms, end_ms)
    if segments == []:
        return []
    raise ProviderError("语音服务未返回转写文字或有效时间戳。")


_SRT_TIME = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)[,.](\d{3})\s*-->\s*(\d+):([0-5]\d):([0-5]\d)[,.](\d{3})(?:\s+.*)?$")


def parse_srt_cues(value, start_ms, end_ms):
    if not isinstance(value, str):
        raise ProviderError("语音服务未返回所选 SRT 格式。")
    value = value.lstrip("\ufeff").replace("\r\n", "\n").strip()
    if not value:
        return []
    cues = []
    for block in re.split(r"\n\s*\n", value):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        match = _SRT_TIME.fullmatch(lines[0].strip()) if lines else None
        if not match or len(lines) < 2:
            raise ProviderError("语音服务未返回有效的所选 SRT 格式，请核对服务支持的响应格式。")
        numbers = [int(number) for number in match.groups()]
        begin = numbers[0] * 3600 + numbers[1] * 60 + numbers[2] + numbers[3] / 1000
        finish = numbers[4] * 3600 + numbers[5] * 60 + numbers[6] + numbers[7] / 1000
        cue = _cue("\n".join(lines[1:]), begin, finish, start_ms, end_ms, "api_srt")
        if cue:
            cues.append(cue)
    return sorted(cues, key=lambda cue: (cue["start_ms"], cue["end_ms"]))


class FileTranscriber:
    def __init__(self, api_key, model, *, base_url, response_format="verbose_json", prompt="",
                 api_format="openai", client=None):
        if not isinstance(model, str) or not model.strip():
            raise ProviderError("请先选择平台支持文件语音转写的模型。")
        if response_format not in RESPONSE_FORMATS:
            raise ProviderError("请选择 verbose_json、json 或 srt 文件转写响应格式。")
        if api_format not in API_FORMATS:
            raise ProviderError("请选择 OpenAI 兼容或 MiniMax 原生语音识别接口。")
        if not api_key:
            raise ProviderError("请先填写文件语音转写 API 密钥。")
        self.api_key = api_key
        self.model = model.strip()
        self.base_url = normalize_base_url(base_url)
        self.response_format = response_format
        self.api_format = api_format
        self.prompt = str(prompt or "")[:1400]
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False)
        self.owns_client = client is None

    async def transcribe(self, audio, *, start_ms, end_ms):
        if end_ms <= start_ms:
            raise ProviderError("文件转写片段的时间范围无效。")
        payload = wav_payload(audio)
        data = {"model": self.model, "response_format": self.response_format}
        headers = {"Authorization": "Bearer " + self.api_key}
        if self.api_format == "minimax":
            if len(payload) - 44 > 500 * 16000 * 2:
                raise ProviderError("MiniMax 每次转写的音频不能超过 500 秒，请分块后重试。")
            # MiniMax places the language hint in an HTTP header and has its
            # own timestamp parameter. It does not document OpenAI's prompt,
            # language form field, or timestamp_granularities[].
            path = "speech_to_text"
            headers["language"] = "ja"
            if self.response_format in ("verbose_json", "srt"):
                data["timestamp_level"] = "sentence"
        else:
            path = "audio/transcriptions"
            # The documented gpt-transcribe family uses a language-hint array;
            # legacy and other compatible transcription models use language.
            if self.model.startswith("gpt-transcribe"):
                data["languages[]"] = "ja"
            else:
                data["language"] = "ja"
            if self.prompt:
                data["prompt"] = self.prompt
            if self.response_format == "verbose_json":
                data["timestamp_granularities[]"] = "segment"
        try:
            response = await asyncio.wait_for(self.client.post(
                endpoint_url(self.base_url, path),
                headers=headers, data=data,
                files={"file": (f"audio-{start_ms}.wav", payload, "audio/wav")}, follow_redirects=False,
            ), timeout=135)
        except (httpx.HTTPError, asyncio.TimeoutError):
            raise ProviderError("文件语音转写请求超时或网络中断，原文件与已完成字幕已保留，可重试。") from None
        if response.status_code >= 300:
            if response.status_code == 402:
                raise ProviderError("语音识别 API 余额或套餐额度不足，请检查该服务账户；原文件与已有字幕已保留。")
            raise ProviderError(_error_message(status=response.status_code))
        if self.response_format == "srt":
            return parse_srt_cues(response.text, start_ms, end_ms)
        try:
            body = response.json()
        except (ValueError, TypeError):
            raise ProviderError("语音服务未返回所选 JSON 格式，请核对响应格式与模型支持。") from None
        return parse_json_cues(body, start_ms, end_ms)

    async def close(self):
        if self.owns_client:
            await self.client.aclose()
