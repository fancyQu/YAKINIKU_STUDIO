"""Benchmark the actual offline live model adapter, without translation or upload.

Results contain timing/counts only. They do not establish transcription accuracy
or real live subtitle latency. --pace can measure wall-clock streaming stability.
"""
import argparse
import asyncio
import json
from pathlib import Path
import resource
import statistics
import sys
import time
import wave

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from yakiniku.model_runtime import LiveModelRuntime


def percentile(values, amount):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * amount
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return round(values[lower] + (values[upper] - values[lower]) * (index - lower), 3)


async def benchmark(args):
    runtime = LiveModelRuntime()
    started = time.perf_counter()
    state = await runtime.prepare(args.backend, args.model)
    cold_ms = (time.perf_counter() - started) * 1000
    records = []
    audio_start = args.start
    playback_origin = time.perf_counter()
    with wave.open(str(args.audio.resolve()), "rb") as reader:
        if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
            raise ValueError("Only uncompressed PCM16 WAV is supported")
        rate, channels = reader.getframerate(), reader.getnchannels()
        first = round(args.start * rate)
        if first >= reader.getnframes():
            raise ValueError("start is beyond the file")
        reader.setpos(first)
        elapsed_audio = 0
        while elapsed_audio < args.seconds:
            duration = min(args.segment_seconds, args.seconds - elapsed_audio)
            pcm = reader.readframes(round(duration * rate))
            if not pcm:
                break
            actual = len(pcm) / (2 * channels * rate)
            elapsed_audio += actual
            if args.pace:
                await asyncio.sleep(max(0, playback_origin + elapsed_audio - time.perf_counter()))
            turn_started = time.perf_counter()
            try:
                result = await runtime.transcribe_pcm(pcm, rate, channels,
                                                      backend=args.backend, model=args.model)
                records.append({"start_seconds": round(audio_start, 3), "duration_seconds": actual,
                                "status": result["status"], "characters": len(result["ja"]),
                                "asr_ms": result["elapsed_ms"],
                                "call_ms": round((time.perf_counter() - turn_started) * 1000, 3)})
            except Exception as error:
                records.append({"start_seconds": round(audio_start, 3), "duration_seconds": actual,
                                "status": "failed", "error_type": type(error).__name__,
                                "call_ms": round((time.perf_counter() - turn_started) * 1000, 3)})
            audio_start += actual
            print(json.dumps(records[-1]), flush=True)
    runtime.shutdown()
    timings = [row["asr_ms"] for row in records if row["status"] == "done"]
    report = {
        "backend": args.backend, "model": args.model, "model_path": state["path"],
        "source": state["source"], "version": state["version"], "offline": True,
        "cold_prepare_ms": round(cold_ms, 3), "audio_seconds": sum(row["duration_seconds"] for row in records),
        "segment_seconds": args.segment_seconds, "paced": args.pace,
        "segments": records, "done_count": sum(row["status"] == "done" for row in records),
        "no_speech_count": sum(row["status"] == "no_speech" for row in records),
        "failed_count": sum(row["status"] == "failed" for row in records),
        "asr_p50_ms": percentile(timings, 0.5), "asr_p95_ms": percentile(timings, 0.95),
        "attempt_p50_ms": percentile([row["call_ms"] for row in records], 0.5),
        "attempt_p95_ms": percentile([row["call_ms"] for row in records], 0.95),
        "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "accuracy_verified": False,
        "limitations": "Offline ASR only; asr timings are successful speech results, attempt timings include failed/no-speech; no translation/UI latency, accuracy check or model-quality comparison",
    }
    if args.backend == "mlx":
        import mlx.core as mx
        report["mlx_peak_memory_bytes"] = mx.get_peak_memory()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "segments"}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--backend", choices=("mlx", "tiny"), default="mlx")
    parser.add_argument("--model", default="mlx-whisper-small")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--segment-seconds", type=float, choices=(3, 5), default=5)
    parser.add_argument("--pace", action="store_true", help="Wait for each audio chunk's real playback time")
    parser.add_argument("--report", type=Path, default=PROJECT / "data/diagnostics/live-asr-benchmark.json")
    args = parser.parse_args()
    if args.start < 0 or args.seconds <= 0:
        parser.error("start must be nonnegative and seconds positive")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
