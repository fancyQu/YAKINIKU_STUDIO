"""PCM recording and lightweight client-timed speech segmentation."""

import math
import os
import struct
import wave
from pathlib import Path
from typing import Any, Dict, List


class WaveWriter:
    """An ordinary WAV whose header remains readable while recording."""

    def __init__(self, path: Path, sample_rate: int = 48000, channels: int = 2):
        self.path = Path(path)
        if channels not in (1, 2) or not 8000 <= int(sample_rate) <= 192000:
            raise ValueError("不支持的音频采样格式")
        if self.path.is_symlink():
            raise ValueError("音频文件不能是符号链接")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.frames = 0
        self.closed = False
        self._file = self.path.open("w+b")
        os.chmod(self.path, 0o600)
        self._wave = wave.open(self._file, "wb")
        self._wave.setnchannels(self.channels)
        self._wave.setsampwidth(2)
        self._wave.setframerate(self.sample_rate)
        self._wave.writeframes(b"")
        self._file.flush()

    @property
    def duration_ms(self) -> int:
        return round(self.frames * 1000 / self.sample_rate)

    def write(self, pcm: bytes) -> None:
        if self.closed:
            raise ValueError("录音文件已关闭")
        if len(pcm) % (2 * self.channels):
            raise ValueError("PCM 数据不是完整的音频帧")
        if 36 + self.frames * self.channels * 2 + len(pcm) > 0xFFFFFFFF:
            # Check before writing: wave.writeframes otherwise writes the audio
            # then fails to encode an overflowing RIFF header.
            raise ValueError("WAV 已接近 4 GiB 上限，请结束保存后开始新录制")
        self._wave.writeframes(pcm)
        self._file.flush()
        self.frames += len(pcm) // (2 * self.channels)

    def close(self) -> None:
        if not self.closed:
            try:
                self._wave.close()
            finally:
                self._file.close()
                self.closed = True

    def sync(self) -> None:
        """Confirm samples are durable before a resumable ASR task references them."""
        if not self.closed:
            self._file.flush()
            os.fsync(self._file.fileno())


class SegmentedWaveWriter:
    """Keep long captures in ordinary editor-friendly WAV files below 4 GiB.

    Part boundaries are exact audio frames. Subtitle timestamps continue on the
    original global timeline; ``parts`` gives each WAV's position on that line.
    """

    def __init__(self, directory: Path, sample_rate: int = 48000, channels: int = 2,
                 part_seconds: float = 10800):
        self.directory = Path(directory)
        if not math.isfinite(float(part_seconds)) or part_seconds <= 0:
            raise ValueError("音频分卷时长必须大于零")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        if self.channels not in (1, 2) or not 8000 <= self.sample_rate <= 192000:
            raise ValueError("不支持的音频采样格式")
        self._part_frames = min(
            max(1, round(float(part_seconds) * self.sample_rate)),
            (0xFFFFFFFF - 36) // (2 * self.channels),
        )
        self._total_frames = 0
        self._completed = []
        self._index = 1
        self._part_start = 0
        self.closed = False
        self._writer = self._new_writer()

    def _new_writer(self) -> WaveWriter:
        name = "audio.wav" if self._index == 1 else f"audio-{self._index:03d}.wav"
        path = self.directory / name
        if path.exists() or path.is_symlink():
            raise ValueError("录音文件已存在，不能覆盖已保存的音频")
        return WaveWriter(path, self.sample_rate, self.channels)

    @property
    def duration_ms(self) -> int:
        return round(self._total_frames * 1000 / self.sample_rate)

    @property
    def parts(self) -> List[Dict[str, Any]]:
        return [dict(part) for part in self._completed] + [{
            "name": self._writer.path.name,
            "start_ms": round(self._part_start * 1000 / self.sample_rate),
            "end_ms": self.duration_ms,
        }]

    def write(self, pcm: bytes) -> None:
        if self.closed:
            raise ValueError("录音文件已关闭")
        frame_bytes = 2 * self.channels
        if len(pcm) % frame_bytes:
            raise ValueError("PCM 数据不是完整的音频帧")
        offset = 0
        while offset < len(pcm):
            if self._writer.frames == self._part_frames:
                completed = self.parts[-1]
                self._writer.close()
                self._index += 1
                self._part_start = self._total_frames
                self._writer = self._new_writer()
                self._completed.append(completed)
            count = min(
                len(pcm) - offset,
                (self._part_frames - self._writer.frames) * frame_bytes,
            )
            self._writer.write(pcm[offset:offset + count])
            self._total_frames += count // frame_bytes
            offset += count

    def close(self) -> None:
        if not self.closed:
            self._writer.close()
            self.closed = True

    def sync(self) -> None:
        self._writer.sync()


class AudioSegmenter:
    """Turn mono 24 kHz PCM into bounded speech turns, preserving source time.

    Quiet audio is still recorded by the caller. Only the recognizer input is
    gated here. A small preroll preserves consonants at speech onset.
    """

    sample_rate = 24000

    def __init__(self, segment_seconds: float = 5, threshold: float = .004,
                 silence_ms: int = 600, preroll_ms: int = 200, collect_timing: bool = False):
        if not .1 <= float(segment_seconds) <= 30:
            raise ValueError("分段时长必须在 0.1 到 30 秒之间")
        if not 0 <= threshold <= 1 or silence_ms < 0 or preroll_ms < 0:
            raise ValueError("无效的语音分段参数")
        self.max_samples = round(float(segment_seconds) * self.sample_rate)
        self.threshold = float(threshold)
        self.silence_samples = round(silence_ms * self.sample_rate / 1000)
        self.preroll_samples = round(preroll_ms * self.sample_rate / 1000)
        self.sample_count = 0
        self._preroll = bytearray()
        self._active = False
        self._start = 0
        self._sent_samples = 0
        self._quiet_samples = 0
        self.collect_timing = collect_timing
        self._last_voiced_sample = 0

    @staticmethod
    def _append(actions: List[Dict[str, Any]], pcm: bytes) -> None:
        if not pcm:
            return
        if actions and actions[-1]["type"] == "append":
            actions[-1]["pcm"] += pcm
        else:
            actions.append({"type": "append", "pcm": pcm})

    def _commit(self, actions: List[Dict[str, Any]]) -> None:
        if not self._active or not self._sent_samples:
            return
        # Realtime input commits require at least 100 ms of audio. Padding is
        # recognition-only and never changes the captured audio or timestamps.
        minimum = self.sample_rate // 10
        if self._sent_samples < minimum:
            self._append(actions, b"\x00\x00" * (minimum - self._sent_samples))
        start = round(self._start * 1000 / self.sample_rate)
        end = max(start + 1, round(self.sample_count * 1000 / self.sample_rate))
        actions.append({"type": "commit", "start_ms": start, "end_ms": end})
        if self.collect_timing:
            actions[-1].update(speech_end_ms=round(self._last_voiced_sample * 1000 / self.sample_rate),
                               continuous=self._sent_samples >= self.max_samples)
        self._active = False
        self._sent_samples = 0
        self._quiet_samples = 0
        self._preroll.clear()

    def process(self, pcm: bytes) -> List[Dict[str, Any]]:
        if len(pcm) % 2:
            raise ValueError("PCM 数据不是完整的 16 位采样")
        actions = []
        # 20 ms analysis frames keep silence detection independent of websocket
        # chunk sizes; timestamps count samples instead of wall-clock latency.
        analysis_bytes = 480 * 2
        for offset in range(0, len(pcm), analysis_bytes):
            frame = pcm[offset:offset + analysis_bytes]
            samples = len(frame) // 2
            square_sum = sum(value[0] ** 2 for value in struct.iter_unpack("<h", frame))
            rms = math.sqrt(square_sum / samples) / 32768
            voiced = rms >= self.threshold and rms > 0
            frame_start = self.sample_count
            self.sample_count += samples
            if voiced:
                self._last_voiced_sample = self.sample_count
            if not self._active:
                if not voiced:
                    self._preroll.extend(frame)
                    keep_bytes = self.preroll_samples * 2
                    if keep_bytes:
                        del self._preroll[:-keep_bytes]
                    else:
                        self._preroll.clear()
                    continue
                self._active = True
                self._start = frame_start - len(self._preroll) // 2
                self._sent_samples = len(self._preroll) // 2
                self._append(actions, bytes(self._preroll))
                self._preroll.clear()
            self._append(actions, frame)
            self._sent_samples += samples
            self._quiet_samples = 0 if voiced else self._quiet_samples + samples
            if self._sent_samples >= self.max_samples or (
                not voiced and self._quiet_samples >= self.silence_samples
            ):
                self._commit(actions)
        return actions

    def flush(self) -> List[Dict[str, Any]]:
        actions = []
        self._commit(actions)
        self._preroll.clear()
        return actions
