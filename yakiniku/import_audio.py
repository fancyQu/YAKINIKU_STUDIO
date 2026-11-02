"""Bounded audio import, resumable file recognition and subtitle translation."""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import threading
import wave
from pathlib import Path
from urllib.parse import unquote

from .audio import SegmentedWaveWriter
from .config import effective_provider
from .file_transcriber import FileTranscriber, RESPONSE_FORMATS
from .local_transcriber import _load_model, local_model_path, local_model_ready
from .text_pipeline import create_text_processor, process_segment, text_options, validate_text_settings
from .provider import ProviderError
from .storage import SOURCE_EXTENSIONS, _is_audio_name
from .subtitle_split import split_subtitle_cues
from .refinement import resolve_refinement_settings, refinement_sources

MAX_IMPORT_BYTES = 8 * 1024 ** 3
IMPORT_CHUNK_SECONDS = 30
CLOUD_IMPORT_CHUNK_SECONDS = 120
FORMATS = {
    "wav": "wav", "mp3": "mp3", "m4a": "mov", "mp4": "mov",
    "flac": "flac", "ogg": "ogg", "opus": "ogg", "aac": "aac",
    "webm": "matroska", "wma": "asf", "aif": "aiff", "aiff": "aiff",
}


class ImportAudioError(Exception):
    """A message safe to expose in local progress and API responses."""


def import_filename(header):
    value = unquote(str(header or "")).replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:240]
    extension = Path(value).suffix.lower().lstrip(".")
    if not value or extension not in SOURCE_EXTENSIONS or extension not in FORMATS:
        raise ImportAudioError("请选择 WAV、MP3、M4A、FLAC、OGG 等支持的音频文件。")
    return value, "source." + extension


def _open_audio(path):
    import av
    extension = Path(path).suffix.lower().lstrip(".")
    # Force the selected media demuxer and deny network protocols. A renamed
    # playlist must not cause FFmpeg to follow remote URLs or another file.
    return av.open(str(path), format=FORMATS[extension],
                   options={"protocol_whitelist": "file", "enable_drefs": "0"})


def probe_audio(path):
    """Validate an actual decoded audio frame without reading a whole file."""
    try:
        with _open_audio(path) as container:
            stream = next(iter(container.streams.audio), None)
            if stream is None:
                raise ImportAudioError("文件中没有可读取的音轨，请选择有效的音频文件。")
            frame = next(container.decode(stream), None)
            if frame is None or frame.samples <= 0:
                raise ImportAudioError("音频文件为空或无法解码，请重新选择文件。")
            seconds = (float(stream.duration * stream.time_base) if stream.duration is not None
                       and stream.time_base else float(container.duration or 0) / 1000000)
            return {"source_duration_ms": max(0, round(seconds * 1000))}
    except ImportAudioError:
        raise
    except Exception:
        raise ImportAudioError("音频文件无效或格式无法解码，请选择有效的音频文件。") from None


def decode_audio(source, directory, progress, stopped):
    """Decode with PyAV, writing ordinary 48 kHz stereo WAV parts incrementally."""
    return decode_audio_sources([source], directory, progress, stopped)


def decode_audio_sources(sources, directory, progress, stopped):
    """Normalize saved WAV volumes in order without changing their source files."""
    import av
    writer = SegmentedWaveWriter(directory, sample_rate=48000, channels=2)
    try:
        for source in sources:
            with _open_audio(source) as container:
                stream = next(iter(container.streams.audio), None)
                if stream is None:
                    raise ImportAudioError("文件中没有可读取的音轨。")
                resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
                for frame in container.decode(stream):
                    if stopped.is_set():
                        raise ImportAudioError("导入已中断，原文件已保留，可以继续处理。")
                    for converted in resampler.resample(frame):
                        writer.write(converted.to_ndarray().tobytes())
                    progress["duration_ms"] = writer.duration_ms
                    progress["audio_parts"] = writer.parts
                for converted in resampler.resample(None):
                    writer.write(converted.to_ndarray().tobytes())
        if writer.duration_ms <= 0:
            raise ImportAudioError("音频文件中没有可用的音频采样。")
        return {"duration_ms": writer.duration_ms, "audio_parts": writer.parts,
                "sample_rate": 48000, "channels": 2}
    except ImportAudioError:
        raise
    except Exception:
        raise ImportAudioError("音频解码未完成，原文件和已生成的音频已保留，可重试。") from None
    finally:
        writer.close()
        progress["duration_ms"] = writer.duration_ms
        progress["audio_parts"] = writer.parts


def transcription_chunks(paths, resume_ms=0, chunk_seconds=IMPORT_CHUNK_SECONDS):
    """Read bounded WAV windows; only recognizer copies become mono 16 kHz."""
    import av
    import numpy as np
    elapsed_frames = 0
    resume_frames = round(max(0, resume_ms) * 48)
    for path in paths:
        with wave.open(str(path), "rb") as reader:
            if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (48000, 2, 2):
                raise ImportAudioError("已保存的预览音频格式异常，请保留原文件后重新导入。")
            count = reader.getnframes()
            skip = min(count, max(0, resume_frames - elapsed_frames))
            reader.setpos(skip)
            while reader.tell() < count:
                start_frame = elapsed_frames + reader.tell()
                pcm = reader.readframes(round(chunk_seconds * 48000))
                if not pcm:
                    break
                array = np.frombuffer(pcm, dtype="<i2").reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(array, format="s16", layout="stereo")
                frame.sample_rate = 48000
                resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
                converted = resampler.resample(frame) + resampler.resample(None)
                audio = np.concatenate([item.to_ndarray().reshape(-1) for item in converted]).astype(np.float32)
                end_frame = elapsed_frames + reader.tell()
                yield {"start_ms": round(start_frame / 48), "end_ms": round(end_frame / 48), "audio": audio}
            elapsed_frames += count


def recognize_chunk(model, chunk, glossary=""):
    import numpy as np
    audio = chunk["audio"]
    # Whisper may fabricate speech on silence. Purely quiet windows advance the
    # checkpoint without creating subtitles; VAD additionally handles pauses.
    if not len(audio) or float(np.sqrt(np.mean(audio ** 2))) < .0005:
        return []
    segments, _ = model.transcribe(
        audio, language="ja", task="transcribe", beam_size=3,
        condition_on_previous_text=False, vad_filter=True, word_timestamps=True,
        initial_prompt=glossary[:1400] or None, no_speech_threshold=.6,
    )
    cues = []
    offset = chunk["start_ms"]
    chunk_end = chunk["end_ms"]

    def add(text, start, end):
        text = str(text or "").strip()
        if text:
            start_ms = min(chunk_end - 1, max(offset, offset + round(float(start) * 1000)))
            end_ms = max(start_ms + 1, min(chunk_end, offset + round(float(end) * 1000)))
            cues.append({"start_ms": start_ms, "end_ms": end_ms, "ja": text})

    for segment in segments:
        words = getattr(segment, "words", None)
        if not words:
            add(segment.text, segment.start, segment.end)
            continue
        sentence = ""
        sentence_start = None
        sentence_end = None
        for word in words:
            if sentence_start is None:
                sentence_start = word.start
            sentence += word.word
            sentence_end = word.end
            if re.search(r"[。！？!?][\s\u3000]*$", word.word):
                add(sentence, sentence_start, sentence_end)
                sentence, sentence_start = "", None
        if sentence:
            add(sentence, sentence_start, sentence_end)
    return cues


def _next_chunk(iterator):
    # StopIteration cannot pass through an asyncio Future.
    return next(iterator, None)


async def _read_chunk(iterator):
    task = asyncio.create_task(asyncio.to_thread(_next_chunk, iterator))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Closing a generator while its decoder thread runs would race the
        # wave reader. Let this bounded window finish reading before closing.
        with contextlib.suppress(Exception):
            await task
        raise


class AudioImportManager:
    def __init__(self, store, settings_store, translator_factory, *, model_loader=None,
                 worker_count=3, request_timeout=120, file_transcriber_factory=FileTranscriber):
        self.store = store
        self.settings_store = settings_store
        self.translator_factory = translator_factory
        self.model_loader = model_loader or _load_model
        self.file_transcriber_factory = file_transcriber_factory
        self.worker_count = worker_count
        self.request_timeout = request_timeout
        self.tasks = {}
        self.stops = {}
        self.uploading = False

    def is_active(self, identifier=None):
        if identifier is not None:
            task = self.tasks.get(identifier)
            return task is not None and not task.done()
        return self.uploading or any(not task.done() for task in self.tasks.values())

    def validate_settings(self, session=None):
        try:
            settings = (resolve_refinement_settings(self.settings_store, session["refinement_snapshot"])
                        if session and session.get("refinement_source_id") else self.settings_store.load())
            validate_text_settings(settings)
        except ProviderError as exc:
            raise ImportAudioError(str(exc)) from None
        if (session is not None and not session.get("import_transcription_mode")
                and session.get("transcription_mode") in (None, "local")):
            # Older local projects must never start uploading audio just because
            # new imports now use a cloud service.
            model = str(session.get("transcription_model") or settings.get("transcription_model") or "tiny")
            if (not session.get("import_transcription_complete") and self.model_loader is _load_model
                    and not local_model_ready(model)):
                raise ImportAudioError("本机语音模型尚未准备完成，请先完成模型下载。")
        else:
            if session is not None and session.get("import_transcription_mode") != "cloud":
                raise ImportAudioError("该项目的语音识别方式不明确，请重新导入并选择语音服务。")
            if session is not None and session.get("import_transcription_complete"):
                # Retrying only translation needs no new audio upload. Preserve
                # the model that actually generated these saved transcripts.
                return settings, str(session.get("transcription_model") or "")
            speech = effective_provider(settings, "import_transcription")
            if not speech["api_key"] or not speech["base_url"]:
                raise ImportAudioError("请先填写文件语音转写 API 地址和密钥。")
            model = str(settings.get("import_transcription_model") or "").strip()
            if not model:
                raise ImportAudioError("请先选择平台支持文件语音转写的模型，文字翻译模型不能代替语音模型。")
            if settings.get("import_transcription_response_format", "verbose_json") not in RESPONSE_FORMATS:
                raise ImportAudioError("请选择文件语音转写服务支持的响应格式。")
            if settings.get("import_transcription_api", "openai") not in ("openai", "minimax"):
                raise ImportAudioError("请选择文件语音识别服务支持的接口协议。")
        return settings, model

    def public(self, session):
        return {**session, "files": self.store.files(session)}

    def save_progress(self, session, stage, progress, label):
        session.update(import_stage=stage, import_progress=min(100, max(0, round(progress, 1))),
                       import_progress_label=label)
        self.store.save(session)

    def start(self, session):
        identifier = session["id"]
        if self.is_active():
            raise ImportAudioError("已有音频导入正在处理，请等待完成。")
        session.update(status="processing", import_stage="queued", import_error="",
                       import_progress_label="等待音频处理")
        self.store.save(session)
        stopped = threading.Event()
        self.stops[identifier] = stopped
        task = asyncio.create_task(self.run(session, stopped))
        self.tasks[identifier] = task

        def finished(done):
            if self.tasks.get(identifier) is done:
                self.tasks.pop(identifier, None)
                self.stops.pop(identifier, None)

        task.add_done_callback(finished)
        return self.public(session)

    async def _decode(self, session, source, stopped):
        # A failed decode has no ASR checkpoint. Rebuild only generated WAVs;
        # the untouched uploaded source remains available for recovery/download.
        for item in self.store.files(session):
            if _is_audio_name(item["name"]):
                self.store.file_path(session["id"], item["name"]).unlink()
        progress = {}
        decoder = decode_audio_sources if isinstance(source, list) else decode_audio
        task = asyncio.create_task(asyncio.to_thread(
            decoder, source, self.store.directory(session["id"]), progress, stopped,
        ))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=.3)
                decoded = progress.get("duration_ms", 0)
                total = max(decoded, session.get("source_duration_ms", 0), 1)
                self.save_progress(session, "decoding", 1 + 19 * decoded / total,
                                   f"正在解码音频，已处理 {decoded // 1000} 秒")
            result = await task
            session.update(result, import_decoded=True)
            self.store.save(session)
        except asyncio.CancelledError:
            stopped.set()
            with contextlib.suppress(Exception):
                await task
            raise
        finally:
            if progress.get("audio_parts"):
                session["audio_parts"] = progress["audio_parts"]
                session["duration_ms"] = progress.get("duration_ms", 0)

    async def _transcribe(self, session, settings, model_name):
        self.save_progress(session, "transcribing", 20, "正在加载本机语音模型")
        try:
            model = await asyncio.to_thread(self.model_loader, local_model_path(model_name))
        except Exception:
            raise ImportAudioError("本机语音模型加载失败，原文件已保留，请检查模型后重试。") from None
        paths = [self.store.file_path(session["id"], part["name"]) for part in session["audio_parts"]]
        iterator = transcription_chunks(paths, session.get("import_transcribed_ms", 0))
        try:
            while True:
                chunk = await _read_chunk(iterator)
                if chunk is None:
                    break
                try:
                    cues = await asyncio.to_thread(recognize_chunk, model, chunk, settings.get("glossary", ""))
                except Exception:
                    raise ImportAudioError("本机识别未完成，已保存之前的字幕，可从断点继续重试。") from None
                cues = self._split_cues(session, cues)
                for index, cue in enumerate(cues):
                    cue.update(id=f"import-{chunk['start_ms']}-{index}", zh="", translation_status="pending")
                    self.store.append_segment(session, cue)
                session["import_transcribed_ms"] = chunk["end_ms"]
                self.save_progress(session, "transcribing",
                                   20 + 50 * chunk["end_ms"] / max(1, session["duration_ms"]),
                                   f"本机识别 {chunk['end_ms'] // 1000} / {session['duration_ms'] // 1000} 秒")
                self.store.export(session)
        finally:
            iterator.close()
        session["import_transcription_complete"] = True
        self.store.save(session)

    @staticmethod
    def _split_cues(session, cues):
        # A snapshot belongs to new projects only. Old checkpoints must keep
        # their original boundaries when a user resumes a previous import.
        config = session.get("subtitle_split_settings")
        if config is not None:
            cues = split_subtitle_cues(cues, **config)
        for cue in cues:
            if cue.get("alignment") != "approximate":
                continue
            session["import_timestamps_approximate"] = True
            warning = ("长语音段已拆成短字幕；细分时间按文字长度估算，未做精确声学对齐。"
                       if cue.get("timestamp_source") == "estimated_split" else
                       "语音服务仅返回文字；部分字幕时间按文字长度估算，未做精确句级对齐。")
            warnings = session.setdefault("warnings", [])
            if warning not in warnings:
                warnings.append(warning)
        return cues

    async def _transcribe_cloud(self, session, settings, model_name):
        self.save_progress(session, "transcribing", 20, "正在分块上传到所选文件语音转写服务")
        transcriber = self.file_transcriber_factory(
            **effective_provider(settings, "import_transcription"), model=model_name,
            response_format=settings.get("import_transcription_response_format", "verbose_json"),
            api_format=settings.get("import_transcription_api", "openai"),
            prompt=settings.get("glossary", ""),
        )
        paths = [self.store.file_path(session["id"], part["name"]) for part in session["audio_parts"]]
        iterator = transcription_chunks(paths, session.get("import_transcribed_ms", 0),
                                        chunk_seconds=CLOUD_IMPORT_CHUNK_SECONDS)
        try:
            while True:
                chunk = await _read_chunk(iterator)
                if chunk is None:
                    break
                try:
                    cues = await transcriber.transcribe(chunk["audio"], start_ms=chunk["start_ms"], end_ms=chunk["end_ms"])
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ProviderError) else "文件语音转写未完成，已有字幕与原文件已保留，可从断点继续。"
                    raise ImportAudioError(message) from None
                cues = self._split_cues(session, cues)
                for index, cue in enumerate(cues):
                    cue.update(id=f"import-{chunk['start_ms']}-{index}", zh="", translation_status="pending",
                               transcription_model=model_name,
                               transcription_response_format=settings.get("import_transcription_response_format", "verbose_json"))
                    self.store.append_segment(session, cue)
                session["import_transcribed_ms"] = chunk["end_ms"]
                self.save_progress(session, "transcribing",
                                   20 + 50 * chunk["end_ms"] / max(1, session["duration_ms"]),
                                   f"文件语音转写 {chunk['end_ms'] // 1000} / {session['duration_ms'] // 1000} 秒")
                self.store.export(session)
        finally:
            iterator.close()
            with contextlib.suppress(Exception):
                await transcriber.close()
        session["import_transcription_complete"] = True
        self.store.save(session)

    async def _translate(self, session, settings):
        missing = [dict(segment) for segment in session["segments"]
                   if segment.get("ja") and not segment.get("zh")]
        total = len(missing)
        if not total:
            return
        self.save_progress(session, "translating", 70, f"正在校正和翻译，共 {total} 句")
        translator = await create_text_processor(settings, self.translator_factory)
        queue = asyncio.Queue()
        for segment in missing:
            queue.put_nowait(segment)
        completed = 0
        failures = 0

        async def worker():
            nonlocal completed, failures
            while True:
                try:
                    segment = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    context = [dict(cue) for cue in session["segments"]
                               if cue.get("zh") and cue["start_ms"] < segment["start_ms"]][-4:]
                    def checkpoint(value):
                        self.store.append_segment(session, value)
                        self.store.export(session)

                    await asyncio.wait_for(process_segment(translator, segment, context, checkpoint=checkpoint),
                                           timeout=self.request_timeout)
                    segment["translation_status"] = "done"
                except Exception:
                    failures += 1
                    segment["translation_status"] = "failed"
                finally:
                    queue.task_done()
                self.store.append_segment(session, segment)
                completed += 1
                session.update(import_translation_done=completed, import_translation_total=total)
                self.save_progress(session, "translating", 70 + 29 * completed / total,
                                   f"校正和翻译 {completed} / {total} 句")
                self.store.export(session)

        workers = [asyncio.create_task(worker()) for _ in range(self.worker_count)]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            with contextlib.suppress(Exception):
                await translator.close()
        if failures:
            raise ImportAudioError(f"有 {failures} 句翻译未完成，日语与音频已保存，可重试缺失的翻译。")

    def validate_source(self, session):
        if not session.get("import_upload_complete"):
            raise ImportAudioError("原音频上传未完成，请重新导入完整的音频文件。")
        if session.get("refinement_source_id"):
            if not session.get("import_decoded"):
                try:
                    return refinement_sources(self.store, session)
                except (ValueError, OSError, ProviderError) as exc:
                    raise ImportAudioError(str(exc) if isinstance(exc, ProviderError) else "精准校验的原直播录音缺失，请检查文件。") from None
            paths = [self.store.file_path(session["id"], part["name"]) for part in session.get("audio_parts", [])]
            if not paths or any(not path.is_file() for path in paths):
                raise ImportAudioError("精准校验项目的音频分卷缺失，请保留原直播录音并检查文件。")
            for path in paths:
                probe_audio(path)
            return paths
        source = self.store.file_path(session["id"], session.get("source_file", ""))
        if not source.is_file():
            raise ImportAudioError("找不到完整原音频，请重新导入文件。")
        probe_audio(source)
        return source

    async def run(self, session, stopped):
        try:
            settings, model_name = self.validate_settings(session)
            cloud = session.get("import_transcription_mode") == "cloud"
            session.update(transcription_mode="cloud_file" if cloud else "local",
                           translation_model=settings["translation_model"],
                           translation_api=settings.get("translation_api", "responses"))
            if (not session.get("correction_model") or any(
                    cue.get("ja") and not cue.get("zh") and cue.get("correction_status") != "done"
                    for cue in session.get("segments", []))):
                correction = text_options(settings, "correction")
                session.update(correction_model=correction["model"], correction_api=correction["api_format"])
            if not session.get("import_transcription_complete"):
                session["transcription_model"] = model_name
                if cloud:
                    session["import_transcription_response_format"] = settings.get("import_transcription_response_format", "verbose_json")
            source = await asyncio.to_thread(self.validate_source, session)
            if not session.get("import_decoded"):
                await self._decode(session, source, stopped)
            if not session.get("import_transcription_complete"):
                if cloud:
                    await self._transcribe_cloud(session, settings, model_name)
                else:
                    await self._transcribe(session, settings, model_name)
            await self._translate(session, settings)
            session.update(status="completed", import_error="")
            self.save_progress(session, "completed", 100, "处理完成，音频和字幕已分开保存")
        except asyncio.CancelledError:
            stopped.set()
            session.update(status="interrupted", import_stage="interrupted",
                           import_progress_label="处理已中断，可以从已保存的进度继续",
                           import_error="导入处理已中断，原文件和已生成字幕已保留。")
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, ImportAudioError) else "音频导入未完成，原文件和已有字幕已保留，请重试。"
            session.update(status="failed", import_stage="failed", import_error=message,
                           import_progress_label="处理未完成，可以重试")
        finally:
            self.store.export(session)

    async def shutdown(self):
        for stopped in self.stops.values():
            stopped.set()
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
