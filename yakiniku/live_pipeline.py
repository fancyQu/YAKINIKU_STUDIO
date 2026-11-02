"""Durable live subtitles: sampled audio -> one local ASR -> one text request.

Queues contain references to synced WAV ranges, never an unbounded PCM backlog.
The recording receiver does not wait for either inference or translation.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
import wave

from .audio import AudioSegmenter, SegmentedWaveWriter
from .provider import ProviderError, Translator
from .text_pipeline import text_options

ASR_QUEUE_LIMIT = 3
TRANSLATION_QUEUE_LIMIT = 6
LIVE_QUEUE_BUDGET = 3.0
LIVE_TRANSLATION_TIMEOUT = 4.0
BACKFILL_TRANSLATION_TIMEOUT = 35.0
TRANSLATION_WORKERS = 3
TERMINAL_ASR = {"done", "no_speech", "skipped"}


def validate_model_snapshot(session, state):
    expected = (session.get("live_snapshot") or {}).get("model_source") or {}
    for field in ("path", "version"):
        if expected.get(field) and expected[field] != state.get(field):
            raise ProviderError("本场直播的本机模型路径或版本已改变，请恢复原模型后补全；不会自动改用其他模型。")
    source = expected.get("source") or {}
    fields = ["revision", "weights_sha256", "weights_file", "weights_bytes"]
    if not source.get("weights_sha256"):
        fields.append("weights_mtime_ns")
    for field in fields:
        if source.get(field) and source[field] != (state.get("source") or {}).get(field):
            raise ProviderError("本场直播的本机权重版本已改变，请恢复原权重后补全。")


def pending_count(session):
    return sum(1 for s in session.get("segments", []) if
               s.get("asr_status") not in TERMINAL_ASR or
               (s.get("ja") and not s.get("zh") and s.get("translation_status") != "skipped"))


def read_audio_range(store, session, start_ms, end_ms):
    """Read global time across WAV volumes, returning native PCM and format."""
    pieces = []
    rate = int(session.get("sample_rate", 48000))
    channels = int(session.get("channels", 2))
    parts = session.get("audio_parts") or [{"name": "audio.wav", "start_ms": 0,
                                            "end_ms": session.get("duration_ms", 0)}]
    for part in parts:
        start = max(float(start_ms), float(part["start_ms"]))
        end = min(float(end_ms), float(part["end_ms"]))
        if end <= start:
            continue
        with wave.open(str(store.file_path(session["id"], part["name"])), "rb") as reader:
            if reader.getsampwidth() != 2 or reader.getframerate() != rate or reader.getnchannels() != channels:
                raise ProviderError("本场音频格式与记录不一致，请保留原始文件。")
            first = round((start - part["start_ms"]) * rate / 1000)
            last = round((end - part["start_ms"]) * rate / 1000)
            reader.setpos(min(first, reader.getnframes()))
            pieces.append(reader.readframes(max(0, last - first)))
    pcm = b"".join(pieces)
    if not pcm:
        raise ProviderError("找不到本段已保存的音频，无法补全识别。")
    return pcm, rate, channels


def new_segment(session, start_ms, end_ms):
    sequence = max((s.get("sequence", i + 1) for i, s in enumerate(session.get("segments", []))), default=0) + 1
    identifier = "live_" + uuid.uuid4().hex
    return {"id": identifier, "segment_id": identifier, "session_id": session["id"],
            "sequence": sequence, "start_ms": int(start_ms), "end_ms": int(end_ms),
            "ja_raw": "", "ja": "", "zh": "", "asr_status": "pending",
            "translation_status": "pending", "asr_attempts": 0, "translation_attempts": 0,
            "error": "", "metrics": {}}


def make_live_translator(settings, factory, *, realtime=True):
    if settings.get("translation_backend") == "local":
        from .local_translation import LocalTranslator, get_local_translation_runtime
        runtime = settings.get("_local_translation_runtime") or get_local_translation_runtime()
        return LocalTranslator(runtime, settings["local_translation_model"], settings.get("glossary", ""))
    options = text_options(settings, "translation")
    if factory is Translator:
        options.update(request_timeout=LIVE_TRANSLATION_TIMEOUT if realtime else BACKFILL_TRANSLATION_TIMEOUT,
                       max_attempts=1)
    return factory(**options)


def translation_workers(settings):
    # Local ASR and text generation share one GPU worker. Never enqueue three
    # native generations that keep running after their coroutine times out.
    return 1 if settings.get("translation_backend") == "local" else TRANSLATION_WORKERS


def validate_translation_snapshot(session, state):
    expected = (session.get("live_snapshot") or {}).get("translation_model_source") or {}
    for field in ("path", "version"):
        if expected.get(field) and expected[field] != state.get(field):
            raise ProviderError("本场直播的本地翻译模型路径或版本已改变，请恢复原模型后补全。")
    source = expected.get("source") or {}
    actual_source = state.get("source") or {}
    for field in ("weights_sha256", "files_sha256"):
        if source.get(field) and source[field] != actual_source.get(field):
            raise ProviderError("本场直播的本地翻译权重已改变，请恢复原权重后补全。")

    def complete_hashes(value):
        # Runtime manifests cover every verified file, including the tokenizer.
        # Do not treat an empty/partial digest record as proof of equal content.
        required = {"config.json", "tokenizer_config.json", "tokenizer.json"}
        return (isinstance(value, dict) and required <= value.keys()
                and any(isinstance(name, str) and name.endswith(".safetensors") for name in value)
                and all(isinstance(name, str) and isinstance(digest, str)
                        and len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
                        for name, digest in value.items()))

    expected_hashes = source.get("files_sha256")
    actual_hashes = actual_source.get("files_sha256")
    same_files = (complete_hashes(expected_hashes) and complete_hashes(actual_hashes)
                  and expected_hashes == actual_hashes)
    # Mirror repositories have different Git revisions even when all model and
    # tokenizer bytes match. Legacy snapshots without full hashes stay strict.
    if source.get("revision") and source["revision"] != actual_source.get("revision") and not same_files:
        raise ProviderError("本场直播的本地翻译权重已改变，请恢复原权重后补全。")


async def translate_once(translator, session, segment, timeout=LIVE_TRANSLATION_TIMEOUT, source_text=None):
    # Do not await the preceding turn. Include its Japanese even if its Chinese
    # has not arrived; completed pairs remain correctly associated by turn.
    context = sorted((s for s in session["segments"] if s.get("ja") and
                      s["start_ms"] < segment["start_ms"]), key=lambda s: s["start_ms"])[-4:]
    segment["translation_attempts"] = segment.get("translation_attempts", 0) + 1
    started = time.monotonic()
    result = await asyncio.wait_for(translator.translate(source_text if source_text is not None else segment.get("ja_raw") or segment["ja"],
                                                         context=context), timeout)
    if not isinstance(result, str) or not result.strip():
        raise ProviderError("中文翻译服务返回空结果；日语与音频已保留。")
    segment.update(zh=result.strip(), translation_status="done", error="")
    segment.setdefault("metrics", {})["translation_request_ms"] = round((time.monotonic() - started) * 1000, 2)


def error_summary(exc, stage):
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"{stage}超时，已保留音频与日语，可在停止后补全。"
    if isinstance(exc, ProviderError):
        return str(exc)
    return f"{stage}未完成，已保留本机音频，可在项目中重试。"


class DurableLiveSession:
    def __init__(self, session, settings, store, socket, runtime, translator_factory=Translator):
        self.session, self.settings, self.store = session, settings, store
        self.socket, self.runtime, self.translator_factory = socket, runtime, translator_factory
        self.writer = None
        self.translator = None
        self.segmenter = AudioSegmenter(segment_seconds=settings.get("segment_seconds", 5),
                                       silence_ms=settings.get("silence_ms", 600), preroll_ms=250, collect_timing=True)
        self.asr_queue = asyncio.Queue(maxsize=ASR_QUEUE_LIMIT)
        self.translation_queue = asyncio.Queue(maxsize=TRANSLATION_QUEUE_LIMIT)
        self.send_lock = asyncio.Lock()
        self.asr_task = None
        self.translation_tasks = []
        self.finishing = False
        self.recognition_enabled = True
        self._submitted = {}
        self._last_progress = 0
        self._last_checkpoint = 0
        self._waiting_for_audio = []
        self._display_sent = {}

    async def emit(self, event):
        if event.get("type") == "translation" and event.get("segment", {}).get("translation_status") == "done":
            self._display_sent[event["segment"]["id"]] = time.monotonic()
            while len(self._display_sent) > 64:
                self._display_sent.pop(next(iter(self._display_sent)))
        async with self.send_lock:
            with contextlib.suppress(Exception):
                await self.socket.send_json({**event, "session_id": self.session["id"]})

    async def warning(self, message):
        warnings = self.session.setdefault("warnings", [])
        if message not in warnings:
            warnings.append(message)
            self.store.save(self.session)
        await self.emit({"type": "warning", "message": message})

    def _save_segment(self, segment, export=True):
        self.store.append_segment(self.session, segment)
        if export:
            self.store.export(self.session)
        else:
            self.store.save(self.session)

    async def start(self):
        self.writer = SegmentedWaveWriter(self.store.directory(self.session["id"]),
                                          self.session["sample_rate"], self.session["channels"])
        self.translator = make_live_translator(self.settings, self.translator_factory)
        self.session.update(status="recording", audio_saved=False, subtitle_status="recording")
        self.store.save(self.session)
        self.asr_task = asyncio.create_task(self._asr_worker())
        self.translation_tasks = [asyncio.create_task(self._translation_worker()) for _ in range(translation_workers(self.settings))]
        await self.emit({"type": "ready", "mode": "live", "recognition_mode": self.settings["live_asr_backend"],
                         "segment_seconds": self.settings.get("segment_seconds", 5), "session": self.session})

    async def progress(self):
        oldest = max((time.monotonic() - value for value in self._submitted.values()), default=0)
        await self.emit({"type": "progress", "duration_ms": self.session.get("duration_ms", 0),
                         "pending_count": pending_count(self.session), "asr_queue": self.asr_queue.qsize(),
                         "translation_queue": self.translation_queue.qsize(),
                         "oldest_age_ms": round(oldest * 1000),
                         "deferred_count": sum(s.get("asr_status") == "deferred" or s.get("translation_status") == "deferred"
                                               for s in self.session["segments"])})

    async def receive_audio(self, packet):
        if not packet or len(packet) > 1024 * 1024:
            raise ValueError("音频数据包大小异常。")
        kind, pcm = packet[0], packet[1:]
        if kind == 0:
            self.writer.write(pcm)
            self.session.update(duration_ms=self.writer.duration_ms, audio_parts=self.writer.parts)
            # Usually raw audio arrives before its recognition copy. Out-of-order
            # copies are not recoverable tasks until this watermark catches up.
            ready = [s for s in self._waiting_for_audio if s["end_ms"] <= self.writer.duration_ms]
            for segment in ready:
                self._waiting_for_audio.remove(segment)
                await self._durable_submit(segment)
            now = time.monotonic()
            if now - self._last_progress >= 1:
                await self.progress()
                self._last_progress = now
            if now - self._last_checkpoint >= 5:
                self.writer.sync()
                self.store.save(self.session)
                self._last_checkpoint = now
        elif kind == 1:
            if len(pcm) % 2:
                raise ValueError("识别副本不是完整的 PCM16 采样。")
            # This is only cheap segmentation; GPU and network work never run
            # inside the receiver. Its clock is cumulative 24 kHz samples.
            await self._actions(self.segmenter.process(pcm))
        else:
            raise ValueError("未知音频数据格式。")

    async def acknowledge_audio(self):
        self.writer.sync()
        frames = round(self.writer.duration_ms * self.session["sample_rate"] / 1000)
        # Use the exact source frame counter instead of rounded milliseconds.
        frames = self.writer._total_frames
        await self.emit({"type": "audio_ack", "frames": frames, "received_frames": frames})

    async def acknowledge_caption(self, control):
        if control.get("session_id") != self.session["id"]:
            return
        identifier = control.get("segment_id")
        sent = self._display_sent.pop(identifier, None)
        if sent is None:
            return
        for segment in self.session["segments"]:
            if segment["id"] != identifier or control.get("sequence") != segment.get("sequence"):
                continue
            metrics = segment.setdefault("metrics", {})
            metrics["display_ack_ms"] = round((time.monotonic() - sent) * 1000, 2)
            delay = control.get("render_delay_ms", 0)
            if isinstance(delay, (float, int)) and math.isfinite(delay) and 0 <= delay <= 60000:
                metrics["render_delay_ms"] = round(delay, 2)
            if "commit_to_translation_ms" in metrics:
                metrics["speech_end_to_display_ack_ms"] = round(metrics["commit_to_translation_ms"] +
                    metrics.get("segment_wait_ms", 0) + metrics["display_ack_ms"], 2)
            self.store.save(self.session)
            break

    async def _actions(self, actions):
        for action in actions:
            if action["type"] != "commit":
                continue
            segment = new_segment(self.session, action["start_ms"], action["end_ms"])
            segment["metrics"].update(segment_wait_ms=max(0, action["end_ms"] - action.get("speech_end_ms", action["end_ms"])),
                                      continuous_speech=action.get("continuous", False))
            if segment["end_ms"] > self.session.get("duration_ms", 0):
                self._waiting_for_audio.append(segment)
                if len(self._waiting_for_audio) > 2:
                    raise ValueError("识别副本超出已接收原始音频，已停止接收异常副本。")
            else:
                await self._durable_submit(segment)

    async def _durable_submit(self, segment):
        self.writer.sync()
        self.session["live_committed_until_ms"] = segment["end_ms"]
        segment["audio_durable"] = True
        self._save_segment(segment, export=False)
        self._submitted[segment["id"]] = time.monotonic()
        if not self.recognition_enabled or self.asr_queue.full():
            segment.update(asr_status="deferred", translation_status="deferred", error="待停止后补全识别")
            self._submitted.pop(segment["id"], None)
            self._save_segment(segment)
            await self.emit({"type": "segment_status", "segment": segment})
        else:
            self.asr_queue.put_nowait((segment, time.monotonic()))

    async def _recognize(self, segment):
        segment.update(asr_status="running", asr_attempts=segment.get("asr_attempts", 0) + 1)
        self._save_segment(segment, export=False)
        pcm, rate, channels = read_audio_range(self.store, self.session, segment["start_ms"], segment["end_ms"])
        started = time.monotonic()
        result = await self.runtime.transcribe_pcm(pcm, sample_rate=rate, channels=channels,
                                                   backend=self.settings["live_asr_backend"],
                                                   model=self.settings["live_asr_model"],
                                                   prompt=self.settings.get("glossary", ""))
        segment.setdefault("metrics", {})["asr_inference_ms"] = round((time.monotonic() - started) * 1000, 2)
        if segment["id"] in self._submitted:
            segment["metrics"]["commit_to_japanese_ms"] = round((time.monotonic() - self._submitted[segment["id"]]) * 1000, 2)
        if result.get("status") in ("no_speech", "skipped"):
            segment.update(asr_status=result["status"], translation_status="skipped", error="")
        else:
            text = result.get("ja", "").strip()
            if not text:
                raise ProviderError("本机识别返回异常空结果，请在项目中重试该段。")
            segment.update(ja_raw=text, ja=text, asr_status="done", translation_status="pending", error="")
        self._save_segment(segment)
        await self.emit({"type": "transcript" if segment.get("ja") else "segment_status", "segment": segment})

    async def _asr_worker(self):
        while True:
            item = await self.asr_queue.get()
            try:
                if item is None:
                    return
                segment, queued_at = item
                segment["metrics"]["asr_queue_ms"] = round((time.monotonic() - queued_at) * 1000, 2)
                if not self.finishing and time.monotonic() - queued_at > LIVE_QUEUE_BUDGET:
                    segment.update(asr_status="deferred", translation_status="deferred", error="超过实时识别预算，待停止后补全")
                    self._save_segment(segment)
                    await self.emit({"type": "segment_status", "segment": segment})
                    continue
                try:
                    await self._recognize(segment)
                except Exception as exc:
                    segment.update(asr_status="failed", translation_status="deferred", error=error_summary(exc, "本机识别"))
                    self._save_segment(segment)
                    await self.warning(segment["error"] + " 当前音频继续保存。")
                    await self.emit({"type": "segment_status", "segment": segment})
                    if self.runtime.status(backend=self.settings["live_asr_backend"], model=self.settings["live_asr_model"]).get("state") == "failed":
                        self.recognition_enabled = False
                        await self.emit({"type": "connection", "status": "failed", "mode": "recording_only"})
                else:
                    if segment["asr_status"] == "done":
                        if self.translation_queue.full():
                            segment.update(translation_status="deferred", error="待停止后补全翻译")
                            self._save_segment(segment)
                            await self.emit({"type": "segment_status", "segment": segment})
                        else:
                            self.translation_queue.put_nowait((segment, time.monotonic()))
            finally:
                if item is not None:
                    if item[0].get("translation_status") != "pending" or item[0].get("asr_status") != "done":
                        self._submitted.pop(item[0]["id"], None)
                self.asr_queue.task_done()

    async def _translation_worker(self):
        while True:
            item = await self.translation_queue.get()
            try:
                if item is None:
                    return
                segment, queued_at = item
                segment["metrics"]["translation_queue_ms"] = round((time.monotonic() - queued_at) * 1000, 2)
                if not self.finishing and time.monotonic() - queued_at > LIVE_QUEUE_BUDGET:
                    segment.update(translation_status="deferred", error="超过实时翻译预算，待停止后补全")
                else:
                    segment["translation_status"] = "running"
                    self._save_segment(segment, export=False)
                    try:
                        await translate_once(self.translator, self.session, segment)
                    except Exception as exc:
                        segment.update(translation_status="deferred" if isinstance(exc, asyncio.TimeoutError) else "failed",
                                       error=error_summary(exc, "中文翻译"))
                        await self.warning(segment["error"])
                submitted = self._submitted.get(segment["id"])
                if submitted:
                    segment["metrics"]["commit_to_translation_ms"] = round((time.monotonic() - submitted) * 1000, 2)
                self._save_segment(segment)
                await self.emit({"type": "translation", "segment": segment})
            finally:
                if item is not None:
                    self._submitted.pop(item[0]["id"], None)
                self.translation_queue.task_done()

    async def _backfill(self):
        # Deferred tasks only; an actual failed request is left for explicit retry
        # rather than repeated billing/retrying a persistent authentication error.
        for old in sorted(self.session["segments"], key=lambda s: s["start_ms"]):
            segment = dict(old)
            if segment.get("asr_status") == "deferred":
                try:
                    await self._recognize(segment)
                except Exception as exc:
                    segment.update(asr_status="failed", error=error_summary(exc, "补全识别"))
                    self._save_segment(segment)
            if segment.get("asr_status") == "done" and not segment.get("zh") and segment.get("translation_status") in ("pending", "deferred"):
                try:
                    segment["translation_status"] = "running"
                    self._save_segment(segment, export=False)
                    await translate_once(self.translator, self.session, segment, BACKFILL_TRANSLATION_TIMEOUT)
                except Exception as exc:
                    segment.update(translation_status="failed", error=error_summary(exc, "补全翻译"))
                self._save_segment(segment)
                await self.emit({"type": "translation", "segment": segment})
            await self.progress()

    async def finish(self, interrupted=False):
        if self.finishing:
            return
        self.finishing = True
        if self.writer is not None:
            await self._actions(self.segmenter.flush())
            for segment in self._waiting_for_audio:
                segment["end_ms"] = min(segment["end_ms"], self.writer.duration_ms)
                if segment["end_ms"] > segment["start_ms"]:
                    await self._durable_submit(segment)
            self._waiting_for_audio.clear()
            self.writer.sync()
            self.writer.close()
            self.session.update(duration_ms=self.writer.duration_ms, audio_parts=self.writer.parts,
                                audio_saved=True, status="finalizing", subtitle_status="backfilling")
            processed_ms = min(self.writer.duration_ms, round(self.segmenter.sample_count * 1000 / 24000))
            # A disconnect can arrive between the original stereo packet and
            # its mono copy. Segment the durable raw tail instead of losing it.
            if self.writer.duration_ms - processed_ms > 1:
                self.session["live_recovery_from_ms"] = processed_ms
            else:
                self.session["live_committed_until_ms"] = self.writer.duration_ms
        self.store.export(self.session)
        await self.emit({"type": "audio_saved", "session": self.session, "pending_count": pending_count(self.session)})
        try:
            if self.asr_task is not None:
                await self.asr_queue.put(None)
                await self.asr_task
            for _ in self.translation_tasks:
                await self.translation_queue.put(None)
            await asyncio.gather(*self.translation_tasks)
            if "live_recovery_from_ms" in self.session:
                # All mutating workers have drained. Recovery may now read and
                # segment a long raw tail off the event loop safely.
                await asyncio.to_thread(recover_tail, self.store, self.session, self.settings)
            if self.translator is not None:
                await self.translator.close()
                self.translator = make_live_translator(self.settings, self.translator_factory, realtime=False)
            await self._backfill()
        finally:
            if self.translator is not None:
                await self.translator.close()
            left = pending_count(self.session)
            self.session.update(status="interrupted" if interrupted else "partial" if left else "completed",
                                subtitle_status="partial" if left else "completed", pending_subtitle_count=left)
            exported = self.store.export(self.session)
            await self.emit({"type": "saved", "session": exported})


def recover_tail(store, session, settings):
    """Segment only uncommitted, durable audio after an interrupted capture."""
    start = int(session.pop("live_recovery_from_ms", session.get("live_committed_until_ms", session.get("duration_ms", 0))))
    end = int(session.get("duration_ms", 0))
    if start >= end:
        return
    import numpy as np
    try:
        from scipy.signal import resample_poly
    except ImportError:
        resample_poly = None
    segmenter = AudioSegmenter(settings.get("segment_seconds", 5), silence_ms=settings.get("silence_ms", 600), preroll_ms=250)
    before = len(session.get("segments", []))
    # Bounded blocks: recovery must not read a long recording into memory.
    for offset in range(start, end, 5000):
        pcm, rate, channels = read_audio_range(store, session, offset, min(offset + 5000, end))
        audio = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels).astype(np.float32).mean(axis=1)
        divisor = math.gcd(rate, 24000)
        if resample_poly is not None:
            samples = resample_poly(audio, 24000 // divisor, rate // divisor)
        else:
            count = max(1, round(len(audio) * 24000 / rate))
            samples = np.interp(np.arange(count) * rate / 24000, np.arange(len(audio)), audio)
        actions = segmenter.process(np.clip(samples, -32768, 32767).astype("<i2").tobytes())
        if offset + 5000 >= end:
            actions += segmenter.flush()
        for action in actions:
            if action["type"] == "commit":
                segment = new_segment(session, start + action["start_ms"], min(end, start + action["end_ms"]))
                segment.update(asr_status="deferred", translation_status="deferred", audio_durable=True)
                store.append_segment(session, segment)
    if len(session.get("segments", [])) == before:
        segment = new_segment(session, start, end)
        segment.update(asr_status="no_speech", translation_status="skipped", audio_durable=True)
        store.append_segment(session, segment)
    session["live_committed_until_ms"] = end
    store.export(session)


async def retry_live_subtitles(store, session, settings, runtime, translator_factory, *, legacy=False):
    """Idempotently fill missing stages, preserving original IDs and good text."""
    if not legacy:
        recover_tail(store, session, settings)
    translator = make_live_translator(settings, translator_factory, realtime=False)
    queue = asyncio.Queue(maxsize=TRANSLATION_QUEUE_LIMIT)
    failures = []
    session.update(status="processing", subtitle_status="backfilling")
    store.save(session)

    async def worker():
        while True:
            segment = await queue.get()
            try:
                if segment is None:
                    return
                segment.update(translation_status="running")
                store.append_segment(session, segment)
                store.save(session)
                try:
                    await translate_once(translator, session, segment, BACKFILL_TRANSLATION_TIMEOUT,
                                         source_text=segment.get("ja", "") if legacy else None)
                except Exception as exc:
                    segment.update(translation_status="failed", error=error_summary(exc, "中文翻译"))
                    failures.append(segment["error"])
                store.append_segment(session, segment)
                store.export(session)
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(translation_workers(settings))]
    try:
        for old in sorted(session["segments"], key=lambda s: s["start_ms"]):
            segment = dict(old)
            if segment.get("zh"):
                continue
            if segment.get("asr_status") in ("no_speech", "skipped"):
                continue
            if not segment.get("ja") and not legacy:
                segment.update(asr_status="running", asr_attempts=segment.get("asr_attempts", 0) + 1)
                store.append_segment(session, segment)
                store.save(session)
                try:
                    pcm, rate, channels = read_audio_range(store, session, segment["start_ms"], segment["end_ms"])
                    result = await runtime.transcribe_pcm(pcm, sample_rate=rate, channels=channels,
                                                         backend=settings["live_asr_backend"], model=settings["live_asr_model"],
                                                         prompt=settings.get("glossary", ""))
                    if result.get("status") in ("no_speech", "skipped"):
                        segment.update(asr_status=result["status"], translation_status="skipped", error="")
                    elif result.get("ja", "").strip():
                        segment.update(asr_status="done", ja_raw=result["ja"].strip(), ja=result["ja"].strip(), error="")
                    else:
                        raise ProviderError("本机识别返回异常空结果。")
                except Exception as exc:
                    segment.update(asr_status="failed", error=error_summary(exc, "本机识别"))
                    failures.append(segment["error"])
                store.append_segment(session, segment)
                store.export(session)
            if segment.get("ja"):
                segment.setdefault("asr_status", "done")
                await queue.put(segment)
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await translator.close()
        left = pending_count(session)
        session.update(status="partial" if left else "completed", subtitle_status="partial" if left else "completed",
                       pending_subtitle_count=left)
        for error in failures:
            if error not in session.setdefault("warnings", []):
                session["warnings"].append(error)
        store.export(session)
    return failures
