"""Explicit model downloads use tiny local fakes; no network or real weights."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from yakiniku.model_downloads import ModelDownloadManager
from yakiniku.provider import ProviderError


class ModelDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.payloads = {"config.json": b'{"n_mels":80}', "weights.npz": b"fixture-weight-bytes"}
        self.spec = {"repo_id": "fixture/public-model", "revision": "a" * 40,
                     "files": [{"name": name, "size_bytes": len(value),
                                "sha256": hashlib.sha256(value).hexdigest()}
                               for name, value in self.payloads.items()]}
        self.specs = {name: self.spec for name in ("mlx-whisper-small", "mlx-whisper-large-v3-turbo")}
        self.calls = []
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_download = False
        self.corrupt = False
        self.truncate = False

        async def downloader(spec, file_spec, destination, on_progress):
            self.calls.append((spec["revision"], file_spec["name"]))
            self.entered.set()
            if self.block:
                await self.release.wait()
            if self.fail_download:
                raise RuntimeError("https://example.invalid/?private-signed-url-token=not-for-output")
            value = self.payloads[file_spec["name"]]
            if self.corrupt and file_spec["name"] == "weights.npz":
                value = b"x" * len(value)
            if self.truncate:
                value = value[:-1]
            destination.write_bytes(value)
            on_progress(len(value))
            await asyncio.sleep(0)

        self.manager = ModelDownloadManager(root=self.root, downloader=downloader, specs=self.specs)

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.temporary.cleanup()

    async def finish(self, model="mlx-whisper-small"):
        for _ in range(300):
            if not self.manager.is_active(model):
                return self.manager.status("mlx", model)
            await asyncio.sleep(.005)
        self.fail("fake download did not stop")

    async def test_status_and_construction_never_download_or_create_model_directory(self):
        for _ in range(3):
            state = self.manager.status()
            self.assertEqual(state["state"], "missing")
            self.assertFalse(state["installed"])
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(self.manager.is_active())

    async def test_explicit_start_installs_complete_model_atomically_with_pinned_source(self):
        self.block = True
        state = await self.manager.start()
        self.assertEqual(state["state"], "downloading")
        await self.entered.wait()
        self.assertFalse((self.root / "mlx-whisper-small").exists())
        self.assertTrue(self.manager.is_active())
        self.release.set()
        state = await self.finish()
        self.assertEqual(state["state"], "downloaded")
        self.assertTrue(state["installed"])
        self.assertEqual(state["progress"], 100)
        self.assertEqual(state["downloaded_bytes"], sum(map(len, self.payloads.values())))
        path = self.root / "mlx-whisper-small"
        source = json.loads((path / "model-source.json").read_text())
        self.assertEqual(source["repo_id"], self.spec["repo_id"])
        self.assertEqual(source["revision"], self.spec["revision"])
        self.assertEqual(source["weights_sha256"], hashlib.sha256(self.payloads["weights.npz"]).hexdigest())
        self.assertEqual(source["downloaded_by"], "explicit_user_request")
        self.assertFalse(any(item.name.startswith(".download-") for item in self.root.iterdir()))

    async def test_readiness_does_not_require_or_trigger_warmup(self):
        await self.manager.start()
        result = await self.finish()
        self.assertEqual(result["state"], "downloaded")
        self.assertNotIn("ready", result)
        self.assertIn("预热", result["message"])

    async def test_removed_model_becomes_missing_without_automatic_redownload(self):
        await self.manager.start()
        await self.finish()
        (self.root / "mlx-whisper-small" / "weights.npz").unlink()
        previous_calls = len(self.calls)
        result = self.manager.status()
        self.assertEqual(result["state"], "missing")
        self.assertFalse(result["installed"])
        self.assertEqual(len(self.calls), previous_calls)

    async def test_existing_complete_model_is_preserved_without_network(self):
        directory = self.root / "mlx-whisper-small"
        directory.mkdir()
        for name, content in self.payloads.items():
            (directory / name).write_bytes(content)
        before = {name: (directory / name).stat().st_mtime_ns for name in self.payloads}
        result = await self.manager.start()
        self.assertEqual(result["state"], "downloaded")
        self.assertFalse(self.manager.is_active())
        self.assertEqual(self.calls, [])
        self.assertEqual(before, {name: (directory / name).stat().st_mtime_ns for name in self.payloads})

    async def test_unsupported_model_backend_and_paths_are_rejected_without_io(self):
        for backend, model in (("tiny", "tiny"), ("mlx", "/tmp/custom-model"), ("mlx", "../arbitrary"), ("mlx", "unknown")):
            with self.subTest(backend=backend, model=model):
                with self.assertRaises(ProviderError):
                    await self.manager.start(backend, model)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_duplicate_start_reuses_job_and_other_model_is_blocked(self):
        self.block = True
        await self.manager.start()
        await self.entered.wait()
        await self.manager.start()
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(ProviderError, "另一个"):
            await self.manager.start("mlx", "mlx-whisper-large-v3-turbo")
        self.release.set()
        self.assertEqual((await self.finish())["state"], "downloaded")

    async def test_failed_download_is_sanitized_and_never_exposes_partial_install(self):
        self.fail_download = True
        await self.manager.start()
        result = await self.finish()
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("token", result["error"])
        self.assertFalse((self.root / "mlx-whisper-small").exists())
        self.assertEqual(list(self.root.iterdir()), [])
        self.fail_download = False
        await self.manager.start()
        self.assertEqual((await self.finish())["state"], "downloaded")

    async def test_hash_mismatch_or_truncation_never_installs(self):
        for mode in ("corrupt", "truncate"):
            with self.subTest(mode=mode):
                setattr(self, mode, True)
                await self.manager.start()
                result = await self.finish()
                self.assertEqual(result["state"], "failed")
                self.assertFalse(result["installed"])
                self.assertEqual(list(self.root.iterdir()), [])
                setattr(self, mode, False)

    async def test_cancel_removes_partial_files_and_never_restarts_on_status_read(self):
        self.block = True
        await self.manager.start()
        await self.entered.wait()
        result = await self.manager.cancel()
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse(self.manager.is_active())
        self.assertEqual(list(self.root.iterdir()), [])
        previous = len(self.calls)
        self.manager.status()
        await asyncio.sleep(.01)
        self.assertEqual(len(self.calls), previous)

    async def test_cancel_before_coroutine_runs_is_also_terminal(self):
        await self.manager.start()
        result = await self.manager.cancel()
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_old_cancel_cannot_overwrite_new_explicit_download_for_same_model(self):
        self.block = True
        await self.manager.start()
        await self.entered.wait()
        original_gather = asyncio.gather
        old_task_finished = asyncio.Event()
        finish_old_cancel = asyncio.Event()

        async def delayed_cancel_cleanup(*tasks, **options):
            result = await original_gather(*tasks, **options)
            old_task_finished.set()
            await finish_old_cancel.wait()
            return result

        with patch("yakiniku.model_downloads.asyncio.gather", side_effect=delayed_cancel_cleanup):
            cancellation = asyncio.create_task(self.manager.cancel())
            await old_task_finished.wait()
            self.assertFalse(self.manager.is_active())
            restarted = await self.manager.start()
            self.assertEqual(restarted["state"], "downloading")
            finish_old_cancel.set()
            result = await cancellation
        self.assertTrue(self.manager.is_active())
        self.assertEqual(result["state"], "downloading")
        self.assertEqual(self.manager.status()["state"], "downloading")
        self.release.set()
        self.assertEqual((await self.finish())["state"], "downloaded")

    async def test_active_status_follows_global_download_independently_of_selected_model(self):
        self.assertIsNone(self.manager.active_status())
        self.block = True
        await self.manager.start("mlx", "mlx-whisper-large-v3-turbo")
        await self.entered.wait()
        self.assertEqual(self.manager.status("mlx", "mlx-whisper-small")["state"], "missing")
        active = self.manager.active_status()
        self.assertEqual(active["backend"], "mlx")
        self.assertEqual(active["model"], "mlx-whisper-large-v3-turbo")
        self.assertEqual(active["state"], "downloading")
        await self.manager.cancel("mlx", "mlx-whisper-large-v3-turbo")
        self.assertIsNone(self.manager.active_status())

    async def test_shutdown_cancels_job_and_preserves_offline_environment(self):
        self.block = True
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}):
            await self.manager.start()
            await self.entered.wait()
            await self.manager.shutdown()
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
        self.assertEqual(self.manager.status()["state"], "cancelled")
        self.assertFalse(self.manager.is_active())
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_existing_incomplete_directory_is_not_overwritten(self):
        path = self.root / "mlx-whisper-small"
        path.mkdir()
        (path / "user-note.txt").write_text("preserve")
        await self.manager.start()
        result = await self.finish()
        self.assertEqual(result["state"], "failed")
        self.assertEqual((path / "user-note.txt").read_text(), "preserve")
        self.assertEqual(self.calls, [])

    async def test_default_http_downloader_uses_pinned_public_files_without_credentials(self):
        requests = []
        native_client = httpx.AsyncClient

        def respond(request):
            requests.append(request)
            filename = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, content=self.payloads[filename])

        def client(**options):
            return native_client(transport=httpx.MockTransport(respond), **options)

        self.manager = ModelDownloadManager(root=self.root, specs=self.specs)
        with patch("yakiniku.model_downloads.httpx.AsyncClient", side_effect=client), patch.dict(
                os.environ, {"HF_HUB_OFFLINE": "1", "HF_TOKEN": "private-fixture-token"}):
            await self.manager.start()
            self.assertEqual((await self.finish())["state"], "downloaded")
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.url.host, "huggingface.co")
            self.assertIn("/resolve/" + self.spec["revision"] + "/", request.url.path)
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("private-fixture-token", str(request.headers))
