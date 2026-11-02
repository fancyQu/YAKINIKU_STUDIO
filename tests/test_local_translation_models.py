"""Pinned model download/install behavior, with local byte fixtures only."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx

from yakiniku.local_translation_models import (
    DEFAULT_MODEL, DOWNLOAD_SPECS, MODEL_CATALOG,
    TranslationModelDownloadManager,
    modelscope_file_url,
)
from yakiniku.provider import ProviderError


OTHER = "qwen2.5-3b-instruct-4bit"


class LocalTranslationDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "models"
        self.payloads = {"config.json": b'{"model_type":"qwen2"}',
                         "tokenizer_config.json": b'{"chat_template":"fixture"}',
                         "tokenizer.json": b'{"version":"1.0"}',
                         "model.safetensors": b"fixture-weight"}
        self.spec = {"repo_id": "fixture/translation", "revision": "a" * 40,
                     "files": [{"name": name, "size_bytes": len(payload),
                                "sha256": hashlib.sha256(payload).hexdigest()}
                               for name, payload in self.payloads.items()]}
        self.specs = {DEFAULT_MODEL: self.spec, OTHER: self.spec}
        self.calls = []
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.corrupt = None
        self.fail_download = False

        async def download(spec, file_spec, destination, on_progress):
            self.calls.append(file_spec["name"])
            self.entered.set()
            if self.block:
                await self.release.wait()
            if self.fail_download:
                raise RuntimeError("private signed-token URL")
            payload = self.payloads[file_spec["name"]]
            if self.corrupt == file_spec["name"]:
                payload = b"x" * len(payload)
            destination.write_bytes(payload)
            on_progress(len(payload))
            await asyncio.sleep(0)

        self.manager = TranslationModelDownloadManager(root=self.root, downloader=download, specs=self.specs)

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.temporary.cleanup()

    async def finish(self, model=DEFAULT_MODEL):
        for _ in range(400):
            if not self.manager.is_active(model):
                return self.manager.status("mlx", model)
            await asyncio.sleep(.005)
        self.fail("fixture download did not complete")

    async def test_status_and_constructor_do_not_download_or_create_root(self):
        self.assertEqual(self.manager.status()["state"], "missing")
        self.assertEqual(self.manager.status("mlx", OTHER)["state"], "missing")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.root.exists())
        self.assertFalse(self.manager.is_active())

    async def test_explicit_download_installs_atomically_and_records_all_digests(self):
        self.block = True
        await self.manager.start()
        await self.entered.wait()
        self.assertFalse((self.root / DEFAULT_MODEL).exists())
        self.release.set()
        state = await self.finish()
        self.assertEqual(state["state"], "downloaded")
        self.assertTrue(state["installed"])
        self.assertEqual(state["progress"], 100)
        self.assertEqual(state["disk_bytes"], sum(map(len, self.payloads.values())))
        source = json.loads((self.root / DEFAULT_MODEL / "model-source.json").read_text())
        self.assertEqual(source["repo_id"], self.spec["repo_id"])
        self.assertEqual(source["revision"], self.spec["revision"])
        self.assertEqual(source["format"], "mlx-lm")
        self.assertEqual(source["downloaded_by"], "explicit_user_request")
        self.assertEqual(len(source["files_sha256"]), len(self.payloads))
        self.assertFalse(any(item.name.startswith(".download-") for item in self.root.iterdir()))
        calls = list(self.calls)
        await self.manager.start()
        self.assertEqual(self.calls, calls)
        self.assertFalse(self.manager.is_active())

    async def test_hash_corruption_in_weights_or_tokenizer_never_installs(self):
        for name in ("model.safetensors", "tokenizer.json"):
            with self.subTest(file=name):
                self.corrupt = name
                await self.manager.start()
                result = await self.finish()
                self.assertEqual(result["state"], "failed")
                self.assertIn("SHA256", result["error"])
                self.assertFalse(result["installed"])
                self.assertEqual(list(self.root.iterdir()), [])

    async def test_cancel_cleans_stage_and_status_never_restarts_it(self):
        self.block = True
        await self.manager.start()
        await self.entered.wait()
        result = await self.manager.cancel()
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse(self.manager.is_active())
        self.assertEqual(list(self.root.iterdir()), [])
        before = len(self.calls)
        self.manager.status()
        await asyncio.sleep(.01)
        self.assertEqual(len(self.calls), before)

    async def test_immediate_cancel_is_terminal(self):
        await self.manager.start()
        result = await self.manager.cancel()
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.root.exists())

    async def test_duplicate_start_reuses_job_and_active_status_tracks_other_model(self):
        self.block = True
        await self.manager.start("mlx", OTHER)
        await self.entered.wait()
        await self.manager.start("mlx", OTHER)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.manager.active_status()["model"], OTHER)
        with self.assertRaisesRegex(ProviderError, "另一个"):
            await self.manager.start()
        self.release.set()
        self.assertEqual((await self.finish(OTHER))["state"], "downloaded")

    async def test_existing_partial_or_symlink_is_preserved(self):
        self.root.mkdir()
        destination = self.root / DEFAULT_MODEL
        destination.mkdir()
        original = destination / "user-file.txt"
        original.write_text("preserve this")
        await self.manager.start()
        self.assertEqual((await self.finish())["state"], "failed")
        self.assertEqual(original.read_text(), "preserve this")
        self.assertEqual(self.calls, [])

    async def test_unlisted_or_arbitrary_paths_are_rejected_without_io(self):
        for backend, model in (("tiny", DEFAULT_MODEL), ("mlx", "unknown"),
                               ("mlx", "../arbitrary"), ("mlx", "/tmp/model")):
            with self.subTest(model=model):
                with self.assertRaises(ProviderError):
                    await self.manager.start(backend, model)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.root.exists())

    async def test_download_error_is_sanitized_and_manual_retry_succeeds(self):
        self.fail_download = True
        await self.manager.start()
        failed = await self.finish()
        self.assertEqual(failed["state"], "failed")
        self.assertNotIn("token", failed["error"])
        self.fail_download = False
        await self.manager.start()
        self.assertEqual((await self.finish())["state"], "downloaded")

    async def test_removing_file_makes_it_missing_without_automatic_download(self):
        await self.manager.start()
        await self.finish()
        (self.root / DEFAULT_MODEL / "tokenizer.json").unlink()
        before = len(self.calls)
        self.assertEqual(self.manager.status()["state"], "missing")
        self.assertEqual(len(self.calls), before)


class TranslationCatalogTests(unittest.TestCase):
    def test_catalog_has_four_pinned_4bit_models_and_explicit_memory_estimates(self):
        self.assertEqual(len(MODEL_CATALOG), 4)
        self.assertEqual(set(DOWNLOAD_SPECS), {item["id"] for item in MODEL_CATALOG})
        self.assertTrue(next(item for item in MODEL_CATALOG if item["id"] == DEFAULT_MODEL)["recommended"])
        for model in MODEL_CATALOG:
            with self.subTest(model=model["id"]):
                spec = DOWNLOAD_SPECS[model["id"]]
                self.assertTrue(re.fullmatch(r"[a-f0-9]{40}", spec["revision"]))
                self.assertEqual(spec["download_source"], "modelscope")
                self.assertEqual(model["download_source"], "modelscope")
                self.assertIn("阿里 ModelScope", model["source_label"])
                self.assertEqual(httpx.URL(model["source_url"]).host, "modelscope.cn")
                self.assertGreater(model["disk_bytes"], 100_000_000)
                self.assertEqual(model["disk_bytes"], sum(item["size_bytes"] for item in spec["files"]))
                self.assertEqual(model["quantization_bits"], 4)
                self.assertEqual(len(model["estimated_runtime_memory_gb"]), 2)
                self.assertIn("估算", model["memory_estimate_note"])
                self.assertIn("不含语音识别", model["memory_estimate_note"])
                names = {item["name"] for item in spec["files"]}
                self.assertTrue({"config.json", "tokenizer_config.json", "tokenizer.json"} <= names)
                self.assertTrue(any(name.endswith(".safetensors") for name in names))
                for file_spec in spec["files"]:
                    self.assertTrue(re.fullmatch(r"[a-f0-9]{64}", file_spec["sha256"]))
                    self.assertGreater(file_spec["size_bytes"], 0)
                    self.assertNotIn("/", file_spec["name"])


class ModelScopeTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = TranslationModelDownloadManager(root=self.root)
        self.spec = DOWNLOAD_SPECS[DEFAULT_MODEL]
        self.file = {"name": "tokenizer config.json", "size_bytes": 7}
        self.destination = self.root / "download.json"
        self.calls = []

    def client_patch(self, handler):
        real_client = httpx.AsyncClient

        def factory(**options):
            return real_client(transport=httpx.MockTransport(handler), **options)

        return patch("yakiniku.local_translation_models.httpx.AsyncClient", side_effect=factory)

    def test_url_uses_official_domestic_api_and_immutable_revision(self):
        url = modelscope_file_url(self.spec, self.file)
        self.assertEqual(url.host, "modelscope.cn")
        self.assertEqual(url.path, f"/api/v1/models/{self.spec['repo_id']}/repo")
        self.assertEqual(url.params["Revision"], self.spec["revision"])
        self.assertEqual(url.params["FilePath"], self.file["name"])
        self.assertNotIn("huggingface", str(url))
        with self.assertRaises(ProviderError):
            modelscope_file_url({**self.spec, "download_source": "huggingface"}, self.file)

    async def test_domestic_cdn_redirect_streams_with_progress(self):
        def handler(request):
            self.calls.append(request)
            if request.url.host == "modelscope.cn":
                return httpx.Response(302, headers={"Location": "https://cdn-lfs-cn-1.modelscope.cn/test/weights"})
            return httpx.Response(200, content=b"fixture")

        progress = []
        with self.client_patch(handler):
            await self.manager._download_file(self.spec, self.file, self.destination, progress.append)
        self.assertEqual(self.destination.read_bytes(), b"fixture")
        self.assertEqual(progress[-1], 7)
        self.assertEqual([call.url.host for call in self.calls], ["modelscope.cn", "cdn-lfs-cn-1.modelscope.cn"])

    async def test_huggingface_redirect_is_rejected_before_contact(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(302, headers={"Location": "https://huggingface.co/unwanted"})

        with self.client_patch(handler):
            with self.assertRaisesRegex(ProviderError, "非阿里来源"):
                await self.manager._download_file(self.spec, self.file, self.destination, lambda _: None)
        self.assertEqual([call.url.host for call in self.calls], ["modelscope.cn"])
        self.assertFalse(self.destination.exists())

    async def test_http_redirect_is_rejected_before_contact(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(302, headers={"Location": "http://modelscope.cn/unwanted"})

        with self.client_patch(handler):
            with self.assertRaises(ProviderError):
                await self.manager._download_file(self.spec, self.file, self.destination, lambda _: None)
        self.assertEqual(len(self.calls), 1)

    async def test_transient_retry_stays_on_modelscope(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(503 if len(self.calls) == 1 else 200, content=b"fixture")

        with self.client_patch(handler):
            await self.manager._download_file(self.spec, self.file, self.destination, lambda _: None)
        self.assertEqual(self.destination.read_bytes(), b"fixture")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(call.url.host == "modelscope.cn" for call in self.calls))

    async def test_not_found_has_no_other_source_fallback(self):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(404)

        with self.client_patch(handler):
            with self.assertRaises(httpx.HTTPStatusError):
                await self.manager._download_file(self.spec, self.file, self.destination, lambda _: None)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
