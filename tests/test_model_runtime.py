import asyncio
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest

from yakiniku.model_runtime import LiveModelRuntime, normalize_result
from yakiniku.provider import ProviderError


class ModelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "mlx-whisper-small"
        self.path.mkdir()
        (self.path / "config.json").write_text("{}")
        (self.path / "weights.npz").write_bytes(b"fake-test-weight")
        (self.path / "model-source.json").write_text(json.dumps({"revision": "fixed-test-revision"}))
        self.calls = []
        self.loads = []
        outer = self

        class FakeEngine:
            version = "test-version"

            def __init__(self, path):
                outer.loads.append(path)

            def transcribe(self, audio, prompt):
                outer.calls.append((audio.copy(), prompt, threading.get_ident()))
                return {"text": "短い返事。", "segments": []}

        self.runtime = LiveModelRuntime(root=self.root, loaders={"mlx": FakeEngine})

    def tearDown(self):
        self.runtime.shutdown()
        self.temporary.cleanup()

    async def test_files_are_not_ready_until_actual_warmup_and_model_is_reused(self):
        state = self.runtime.status()
        self.assertTrue(state["prepared_files"])
        self.assertEqual(state["state"], "not_prepared")
        with self.assertRaisesRegex(ProviderError, "预热"):
            await self.runtime.transcribe_pcm(struct.pack("<h", 1000) * 24000)
        state = await self.runtime.prepare()
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["version"], "test-version")
        self.assertEqual(state["source"]["revision"], "fixed-test-revision")
        self.assertTrue(state["source"]["weights_verified"])
        self.assertEqual(state["source"]["weights_sha256"], hashlib.sha256(b"fake-test-weight").hexdigest())
        await self.runtime.prepare()
        first = await self.runtime.transcribe_pcm(struct.pack("<h", 1000) * 24000, prompt="人名")
        second = await self.runtime.transcribe_pcm(struct.pack("<h", 1000) * 24000)
        self.assertEqual(first["status"], "done")
        self.assertEqual(second["ja"], "短い返事。")
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(len(self.calls), 3)  # One warmup, two recognitions.
        self.assertEqual(self.calls[1][0].shape, (16000,))
        self.assertEqual(self.calls[1][0].dtype.name, "float32")
        self.assertEqual(self.calls[1][1], "人名")
        self.assertEqual(len({row[2] for row in self.calls}), 1)

    async def test_silence_terminal_is_not_an_empty_response_error(self):
        await self.runtime.prepare()
        result = await self.runtime.transcribe_pcm(b"\0\0" * 1000)
        self.assertEqual(result["status"], "no_speech")
        self.assertEqual(result["ja"], "")
        self.assertEqual(len(self.calls), 1)
        self.runtime._engine.transcribe = lambda audio, prompt: {"text": "", "segments": []}
        with self.assertRaisesRegex(ProviderError, "有声片段"):
            await self.runtime.transcribe_pcm(struct.pack("<h", 1000) * 1000)

    async def test_missing_model_failure_never_calls_loader_or_download(self):
        with self.assertRaisesRegex(ProviderError, "不会下载"):
            await self.runtime.prepare(model="mlx-whisper-large-v3-turbo")
        self.assertEqual(self.runtime.status(model="mlx-whisper-large-v3-turbo")["state"], "failed")
        self.assertEqual(self.loads, [])

    async def test_load_failure_sanitizes_private_exception_and_records_failed_state(self):
        def fail(path):
            raise RuntimeError("private-customer-path-token")
        self.runtime._loaders["mlx"] = fail
        with self.assertRaises(ProviderError) as error:
            await self.runtime.prepare()
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.runtime.status()["state"], "failed")

    async def test_manifest_mismatch_prevents_model_loading(self):
        (self.path / "model-source.json").write_text(json.dumps({"weights_sha256": "0" * 64}))
        with self.assertRaisesRegex(ProviderError, "SHA256.*不一致"):
            await self.runtime.prepare()
        self.assertEqual(self.runtime.status()["state"], "failed")
        self.assertEqual(self.loads, [])

    async def test_valid_manifest_records_verified_hash(self):
        digest = hashlib.sha256(b"fake-test-weight").hexdigest()
        (self.path / "model-source.json").write_text(json.dumps({"weights_sha256": digest, "revision": "fixed"}))
        state = await self.runtime.prepare()
        self.assertEqual(state["source"]["weights_sha256"], digest)
        self.assertTrue(state["source"]["weights_verified"])

    async def test_ready_engine_keeps_its_verified_source_after_manifest_is_edited(self):
        original = await self.runtime.prepare()
        (self.path / "model-source.json").write_text(json.dumps({"revision": "different-on-disk", "weights_sha256": "0" * 64}))
        current = self.runtime.status()
        self.assertEqual(current["state"], "ready")
        self.assertEqual(current["source"], original["source"])
        cached = await self.runtime.prepare()
        self.assertEqual(cached["source"], original["source"])

    async def test_legacy_tiny_records_stat_without_claiming_a_verified_hash(self):
        tiny = self.root / "tiny"
        tiny.mkdir()
        weights = tiny / "model.bin"
        weights.write_bytes(b"tiny-test-weight")
        self.runtime._loaders["tiny"] = self.runtime._loaders["mlx"]
        state = await self.runtime.prepare("tiny", "tiny")
        self.assertEqual(state["source"]["weights_file"], "model.bin")
        self.assertEqual(state["source"]["weights_bytes"], weights.stat().st_size)
        self.assertEqual(state["source"]["weights_mtime_ns"], weights.stat().st_mtime_ns)
        self.assertFalse(state["source"]["weights_verified"])

    async def test_cancelled_inference_cannot_overlap_next_job_or_release_worker(self):
        await self.runtime.prepare()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow(audio, prompt):
            calls.append(prompt)
            if prompt == "old":
                started.set()
                release.wait(3)
            return {"text": prompt, "segments": []}

        self.runtime._engine.transcribe = slow
        pcm = struct.pack("<h", 1000) * 1000
        old = asyncio.create_task(self.runtime.transcribe_pcm(pcm, prompt="old"))
        await asyncio.to_thread(started.wait, 1)
        old.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await old
        newer = asyncio.create_task(self.runtime.transcribe_pcm(pcm, prompt="new"))
        await asyncio.sleep(0.02)
        self.assertEqual(calls, ["old"])
        self.assertEqual(self.runtime.status()["pending_count"], 2)
        release.set()
        result = await newer
        self.assertEqual(result["ja"], "new")
        self.assertEqual(calls, ["old", "new"])

    async def test_duplicate_prepare_waits_for_same_native_warmup(self):
        first, second = await asyncio.gather(self.runtime.prepare(), self.runtime.prepare())
        self.assertEqual(first["state"], "ready")
        self.assertEqual(second["state"], "ready")
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(len(self.calls), 1)

    async def test_native_crash_marks_model_failed_without_leaking_exception(self):
        await self.runtime.prepare()
        def fail(audio, prompt):
            raise RuntimeError("private GPU traceback")
        self.runtime._engine.transcribe = fail
        with self.assertRaises(ProviderError) as error:
            await self.runtime.transcribe_pcm(struct.pack("<h", 1000) * 1000)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.runtime.status()["state"], "failed")

    async def test_same_model_reprepare_closes_crashed_engine_before_constructing_replacement(self):
        active = []
        events = []

        class TrackingEngine:
            version = "test"

            def __init__(self, path):
                if active:
                    raise AssertionError("Two engines existed at once")
                self.crashed = False
                active.append(self)
                events.append(("load", threading.get_ident()))

            def transcribe(self, audio, prompt):
                if self.crashed:
                    raise RuntimeError("GPU failed")
                return {"text": "はい", "segments": []}

            def close(self):
                active.remove(self)
                events.append(("close", threading.get_ident()))

        self.runtime._loaders["mlx"] = TrackingEngine
        await self.runtime.prepare()
        old = active[0]
        old.crashed = True
        with self.assertRaises(ProviderError):
            await self.runtime.transcribe_pcm(struct.pack("<h", 1000) * 1000)
        self.assertEqual(self.runtime.status()["state"], "failed")
        state = await self.runtime.prepare()
        self.assertEqual(state["state"], "ready")
        self.assertEqual(len(active), 1)
        self.assertIsNot(active[0], old)
        self.assertEqual([event[0] for event in events], ["load", "close", "load"])
        self.assertEqual(len({event[1] for event in events}), 1)
        result = await self.runtime.transcribe_pcm(struct.pack("<h", 1000) * 1000)
        self.assertEqual(result["ja"], "はい")

    async def test_warmup_failure_releases_unpublished_engine_and_cannot_remain_ready(self):
        active = []

        class BrokenWarmup:
            def __init__(self, path):
                active.append(self)

            def transcribe(self, audio, prompt):
                raise RuntimeError("private native warmup failure")

            def close(self):
                active.remove(self)

        self.runtime._loaders["mlx"] = BrokenWarmup
        with self.assertRaises(ProviderError) as error:
            await self.runtime.prepare()
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(active, [])
        self.assertIsNone(self.runtime._engine)
        self.assertIsNone(self.runtime._active_key)
        self.assertEqual(self.runtime.status()["state"], "failed")

    async def test_failed_reprepare_validation_also_releases_old_engine(self):
        await self.runtime.prepare()
        old = self.runtime._engine
        closed = []
        old.close = lambda: closed.append(True)
        old.transcribe = lambda audio, prompt: (_ for _ in ()).throw(RuntimeError("native crash"))
        with self.assertRaises(ProviderError):
            await self.runtime.transcribe_pcm(struct.pack("<h", 1000) * 1000)
        (self.path / "model-source.json").write_text(json.dumps({"weights_sha256": "0" * 64}))
        with self.assertRaisesRegex(ProviderError, "SHA256"):
            await self.runtime.prepare()
        self.assertEqual(closed, [True])
        self.assertIsNone(self.runtime._engine)
        self.assertIsNone(self.runtime._active_key)
        self.assertEqual(self.runtime.status()["state"], "failed")

    async def test_worker_backlog_has_a_hard_limit_even_when_caller_cancelled(self):
        await self.runtime.prepare()
        self.runtime.max_pending = 1
        started, release = threading.Event(), threading.Event()
        def slow(audio, prompt):
            started.set()
            release.wait(2)
            return {"text": "はい", "segments": []}
        self.runtime._engine.transcribe = slow
        pcm = struct.pack("<h", 1000) * 1000
        old = asyncio.create_task(self.runtime.transcribe_pcm(pcm))
        await asyncio.to_thread(started.wait, 1)
        old.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await old
        try:
            with self.assertRaisesRegex(ProviderError, "已满"):
                await self.runtime.transcribe_pcm(pcm)
        finally:
            release.set()


class NormalizeResultTests(unittest.TestCase):
    def test_confirmed_no_speech_is_terminal(self):
        self.assertEqual(normalize_result({"text": "", "segments": [
            {"no_speech_prob": 0.99, "avg_logprob": -2.0}]}),
            {"status": "no_speech", "ja": ""})

    def test_short_confident_answer_is_preserved(self):
        result = normalize_result({"text": "はい。", "segments": [
            {"no_speech_prob": 0.8, "avg_logprob": -0.1}]})
        self.assertEqual(result["ja"], "はい。")

    def test_invalid_or_unexplained_empty_results_are_errors(self):
        for result in (None, [], {}, {"text": "", "segments": []}):
            with self.assertRaises(ProviderError):
                normalize_result(result)

    def test_repetitive_model_hallucination_is_not_published_as_speech(self):
        with self.assertRaisesRegex(ProviderError, "异常重复"):
            normalize_result({"text": "同じ文" * 60, "segments": [
                {"compression_ratio": 35.2, "avg_logprob": -0.1, "no_speech_prob": 0.08}]})


if __name__ == "__main__":
    unittest.main()
