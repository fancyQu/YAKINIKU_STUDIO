"""Offline MLX environment/model smoke check; not the live-ASR implementation."""

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import wave
from importlib.metadata import version


PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=PROJECT / "data/models/mlx-whisper-small")
    parser.add_argument("--audio", type=Path, help="Optional local PCM16 WAV for a short ASR check")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--report", type=Path, default=PROJECT / "data/diagnostics/mlx-asr-check.json")
    args = parser.parse_args()
    if args.start < 0 or not 0 < args.seconds <= 30 or not 1 <= args.runs <= 10:
        parser.error("start must be nonnegative, seconds in (0,30], runs in [1,10]")

    # No credentials, Hub request, or implicit download is needed for this check.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ.setdefault("NUMBA_CACHE_DIR", str(PROJECT / "data/runtime-cache/numba"))
    model = args.model.resolve()
    if not (model / "config.json").is_file() or not any(
        (model / filename).is_file() for filename in ("weights.safetensors", "weights.npz")
    ):
        parser.error("MLX model config/weights are missing; prepare the model before running")

    import mlx.core as mx
    import mlx_whisper
    import numpy as np
    from scipy.signal import resample_poly

    if not mx.metal.is_available():
        raise RuntimeError("Metal GPU is unavailable in this process")
    mx.set_default_device(mx.gpu)
    probe = mx.sum(mx.array([1.0, 2.0, 3.0]) ** 2)
    mx.eval(probe)
    report = {
        "python": sys.version.split()[0],
        "architecture": platform.machine(),
        "mlx": version("mlx"),
        "mlx_whisper": version("mlx-whisper"),
        "metal_available": True,
        "device": str(mx.default_device()),
        "gpu_smoke_result": probe.item(),
        "model_path": str(model),
        "network": "offline",
        "accuracy_verified": False,
    }
    if args.audio:
        with wave.open(str(args.audio.resolve()), "rb") as reader:
            if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
                parser.error("This check expects uncompressed PCM16 WAV")
            rate, channels = reader.getframerate(), reader.getnchannels()
            first = round(args.start * rate)
            if first >= reader.getnframes():
                parser.error("start is beyond the audio duration")
            reader.setpos(first)
            pcm = reader.readframes(round(args.seconds * rate))
        audio = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels).astype(np.float32).mean(axis=1) / 32768
        divisor = math.gcd(rate, 16000)
        audio = resample_poly(audio, 16000 // divisor, rate // divisor).astype(np.float32)
        report.update(audio_path=str(args.audio.resolve()), start_seconds=args.start,
                      audio_seconds=len(audio) / 16000, audio_rms=float(np.sqrt(np.mean(audio ** 2))))
    else:
        audio = np.zeros(16000, dtype=np.float32)
        report.update(audio_seconds=1, input="synthetic silence; model-load smoke check only")

    def transcribe():
        return mlx_whisper.transcribe(
            audio, path_or_hf_repo=str(model), language="ja", task="transcribe",
            temperature=0.0, condition_on_previous_text=False,
            word_timestamps=False, verbose=None,
        )

    started = time.perf_counter()
    result = transcribe()
    report["cold_call_seconds"] = round(time.perf_counter() - started, 4)
    print(json.dumps({"cold_call_seconds": report["cold_call_seconds"],
                      "text_characters": len(result["text"].strip())}), flush=True)
    elapsed = []
    for index in range(args.runs):
        started = time.perf_counter()
        result = transcribe()
        elapsed.append(time.perf_counter() - started)
        print(json.dumps({"warm_run": index + 1, "seconds": round(elapsed[-1], 4),
                          "text_characters": len(result["text"].strip())}), flush=True)
    report.update(
        warm_run_seconds=[round(value, 4) for value in elapsed],
        warm_median_seconds=round(statistics.median(elapsed), 4),
        real_time_factor=round(statistics.median(elapsed) / report["audio_seconds"], 4),
        text=result["text"].strip(),
        segments=result.get("segments", []),
        language=result.get("language"),
        limitations="Short local smoke check; no live capture, translation latency, long-run stability or accuracy comparison",
    )
    if hasattr(mx, "get_peak_memory"):
        report["mlx_peak_memory_bytes"] = mx.get_peak_memory()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.report.resolve()), "warm_median_seconds": report["warm_median_seconds"],
                      "real_time_factor": report["real_time_factor"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
