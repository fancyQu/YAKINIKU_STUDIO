"""Browser capture is bound to one task and never publishes capture secrets."""
import asyncio
import base64
import copy
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from yakiniku.server import create_app
from yakiniku.space_downloads import SpaceDownloadManager


SPACE = "1dKrPrwgXpXJX"
OTHER_SPACE = "1wxWjjYejaRJQ"
ORIGIN = "http://127.0.0.1:8765"
EXTENSION = "chrome-extension://" + "a" * 32
CDN = "https://prod-test.video.pscp.tv/Transcoding/v1/hls/test/audio-space/playlist.m3u8?token=private-playlist-token"
KEY_URI = "https://x.com/i/live_video_stream/key?broadcast_id=" + SPACE + "&stream_name=private-stream"
KEY = base64.b64encode(bytes(range(16))).decode()


def playlist(key_uri=KEY_URI):
    return ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="' + key_uri + '",IV=0x00000000000000000000000000000001\n'
            "#EXTINF:1,\naudio-0.aac\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="' + key_uri + '",IV=0x00000000000000000000000000000002\n'
            "#EXTINF:2,\naudio-1.aac\n#EXT-X-ENDLIST\n")


class SpaceDownloadAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.calls = []
        self.behavior = "success"

        async def runner(capture, output):
            self.calls.append(copy.deepcopy(capture))
            (output / "audio.mp3").write_bytes(b"fixture-transcoded-mp3")
            if self.behavior == "wait":
                await asyncio.sleep(60)
            if self.behavior == "fail":
                raise RuntimeError(KEY + " " + CDN)
            report = {"segments": 2, "duration_seconds": 3, "reencoded": True,
                      "ffmpeg_full_decode_passed": True, "secret": KEY, "url": CDN}
            (output / "audio.json").write_text(json.dumps(report))
            return report

        self.runner = runner
        self.app = create_app(data_root=self.root, space_download_runner=runner)
        self.app.state.space_downloads.watchdog_interval = .01
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def create(self, identifier=SPACE):
        result = self.client.post("/api/space-downloads", json={"url": "https://x.com/i/spaces/" + identifier + "?s=20"})
        self.assertEqual(result.status_code, 201, result.text)
        job = result.json()
        self.assertEqual(job["source_url"], "https://x.com/i/spaces/" + identifier)
        self.assertEqual(job["stage"], "waiting_browser")
        self.assertTrue(job["capture_token"])
        return job

    def capture(self, job, event, token=None, origin=EXTENSION):
        return self.client.post("/api/space-downloads/" + job["id"] + "/capture", json=event,
                                headers={"Origin": origin, "X-Space-Capture-Token": token or job["capture_token"]})

    def key(self, job, **changes):
        event = {"kind": "key", "url": KEY_URI, "key_base64": KEY}
        event.update(changes)
        return self.capture(job, event)

    def manifest(self, job, text=None, **changes):
        event = {"kind": "playlist", "url": CDN, "text": text or playlist()}
        event.update(changes)
        return self.capture(job, event)

    def wait_for(self, job, stage):
        for _ in range(100):
            result = self.client.get("/api/space-downloads/" + job["id"])
            self.assertEqual(result.status_code, 200)
            if result.json()["stage"] == stage:
                return result.json()
            time.sleep(.01)
        self.fail("Job did not reach " + stage + ": " + result.text)

    def test_key_and_playlist_automatically_start_verified_download_without_secret_disclosure(self):
        job = self.create()
        self.assertEqual(self.key(job).json()["stage"], "capturing")
        accepted = self.manifest(job)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        finished = self.wait_for(job, "completed")
        self.assertEqual(finished["segments"], 2)
        self.assertEqual(finished["duration_ms"], 3000)
        self.assertEqual({f["name"] for f in finished["files"]}, {"audio.mp3", "audio.json"})
        self.assertEqual(self.calls[0]["keys"], {KEY_URI: KEY})
        public = self.client.get("/api/space-downloads").text
        saved = (self.root / "space-downloads" / job["id"] / "job.json").read_text()
        report = self.client.get("/api/space-downloads/" + job["id"] + "/files/audio.json")
        self.assertEqual(report.status_code, 200)
        for value in (public, saved, report.text):
            for secret in (KEY, CDN, "private-playlist-token", "private-stream", job["capture_token"]):
                self.assertNotIn(secret, value)
        self.assertFalse(self.app.state.space_downloads.captures)
        self.assertEqual(self.key(job).status_code, 403)
        self.assertEqual(self.client.get("/api/space-downloads/" + job["id"] + "/files/audio.mp3").content,
                         b"fixture-transcoded-mp3")

    def test_capture_requires_job_token_and_rejects_other_space_key(self):
        job, other = self.create(), self.create(OTHER_SPACE)
        event = {"kind": "key", "url": KEY_URI, "key_base64": KEY}
        self.assertEqual(self.capture(job, event, token=other["capture_token"]).status_code, 403)
        self.assertEqual(self.key(job, url=KEY_URI.replace(SPACE, OTHER_SPACE)).status_code, 400)
        self.assertEqual(self.manifest(job, playlist(KEY_URI.replace(SPACE, OTHER_SPACE))).status_code, 400)
        self.assertFalse(self.calls)
        self.assertEqual(self.client.get("/api/space-downloads/" + job["id"]).json()["stage"], "waiting_browser")

    def test_extension_cors_is_only_available_to_token_protected_capture_route(self):
        job = self.create()
        path = "/api/space-downloads/" + job["id"] + "/capture"
        response = self.client.options(path, headers={"Origin": EXTENSION, "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Headers": "content-type,x-space-capture-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], EXTENSION)
        self.assertEqual(self.client.get("/api/space-downloads", headers={"Origin": EXTENSION}).status_code, 403)
        self.assertEqual(self.client.post("/api/settings", headers={"Origin": EXTENSION}, json={}).status_code, 403)
        self.assertEqual(self.capture(job, {"kind": "key", "url": KEY_URI, "key_base64": KEY}, origin="https://x.com").status_code, 403)
        bad = self.client.options(path, headers={"Origin": EXTENSION, "Access-Control-Request-Method": "POST",
                                                "Access-Control-Request-Headers": "Cookie"})
        self.assertEqual(bad.status_code, 403)
        no_token = self.client.post(path, headers={"Origin": EXTENSION}, json={"kind": "key", "url": KEY_URI, "key_base64": KEY})
        self.assertEqual(no_token.status_code, 403)

    def test_ssrf_directory_escape_live_gap_and_drm_never_reach_downloader(self):
        job = self.create()
        malformed = [playlist().replace("audio-1.aac", "https://127.0.0.1/private.aac"),
                     playlist().replace("audio-1.aac", "../outside.aac"),
                     playlist().replace("audio-1.aac", "%2e%2e/outside.aac"),
                     playlist().replace("#EXT-X-ENDLIST", ""),
                     playlist().replace("#EXTINF:2,", "#EXT-X-GAP\n#EXTINF:2,"),
                     playlist().replace("METHOD=AES-128", "METHOD=SAMPLE-AES")]
        for text in malformed:
            with self.subTest(text=text[-50:]):
                response = self.manifest(job, text)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.manifest(job, url="https://private.example/playlist.m3u8").status_code, 400)
        self.assertFalse(self.calls)

    def test_invalid_links_and_invalid_key_fields_return_sanitized_errors(self):
        for url in ("https://x.com:invalid/i/spaces/" + SPACE, "https://x.com/i/spaces/not-valid", "http://x.com/i/spaces/" + SPACE,
                    "https://private@x.com/i/spaces/" + SPACE, "https://127.0.0.1/i/spaces/" + SPACE):
            with self.subTest(url=url):
                self.assertEqual(self.client.post("/api/space-downloads", json={"url": url}).status_code, 400)
        job = self.create()
        self.assertEqual(self.key(job, key_base64="wrong").status_code, 400)
        invalid = self.client.post("/api/space-downloads/" + job["id"] + "/capture",
                                   json={"kind": "key", "url": KEY_URI, "key_base64": KEY * 10},
                                   headers={"X-Space-Capture-Token": job["capture_token"]})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(KEY, invalid.text)

    def test_runner_failure_cleans_partial_outputs_and_hides_exception_details(self):
        self.behavior = "fail"
        job = self.create()
        self.key(job)
        self.manifest(job)
        failed = self.wait_for(job, "failed")
        self.assertEqual(failed["files"], [])
        self.assertNotIn(KEY, json.dumps(failed))
        self.assertNotIn(CDN, json.dumps(failed))
        self.assertFalse((self.root / "space-downloads" / job["id"] / "audio.mp3").exists())
        self.assertFalse(self.app.state.space_downloads.captures)

    def test_cancel_stops_running_task_and_removes_partial_audio(self):
        self.behavior = "wait"
        job = self.create()
        self.key(job)
        self.manifest(job)
        for _ in range(50):
            if self.calls:
                break
            time.sleep(.01)
        response = self.client.post("/api/space-downloads/" + job["id"] + "/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stage"], "cancelled")
        self.assertFalse(self.app.state.space_downloads.tasks)
        self.assertFalse(self.app.state.space_downloads.captures)
        self.assertFalse((self.root / "space-downloads" / job["id"] / "audio.mp3").exists())

    def test_restart_keeps_only_public_metadata_and_invalidates_old_capture_token(self):
        job = self.create()
        self.key(job)
        self.client.__exit__(None, None, None)
        self.app = create_app(data_root=self.root, space_download_runner=self.runner)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        recovered = self.client.get("/api/space-downloads/" + job["id"]).json()
        self.assertEqual(recovered["stage"], "interrupted")
        self.assertEqual(self.key(job).status_code, 403)
        serialized = (self.root / "space-downloads" / job["id"] / "job.json").read_text()
        self.assertNotIn(KEY, serialized)
        self.assertNotIn(job["capture_token"], serialized)

    def test_watchdog_expires_browser_capture_without_any_new_client_request(self):
        manager = self.app.state.space_downloads
        manager.capture_ttl = .04
        job = self.create()
        self.key(job)
        private_capture = manager.captures[job["id"]]
        time.sleep(.08)
        self.assertNotIn(job["id"], manager.captures)
        self.assertEqual(private_capture["keys"], {})
        self.assertEqual(private_capture["token"], "")
        self.assertIsNone(private_capture["playlist"])
        self.assertEqual(self.client.get("/api/space-downloads/" + job["id"]).json()["stage"], "failed")
        self.assertEqual(self.key(job).status_code, 403)

    def test_restart_removes_crashed_key_work_directories_without_following_symlinks(self):
        job = self.create()
        folder = self.root / "space-downloads" / job["id"]
        for relative in (".capture-crashed/output/.hls-audio-crashed", ".hls-audio-old"):
            work = folder / relative
            work.mkdir(parents=True)
            (work / "key.bin").write_text(KEY)
        external = self.root / "unrelated"
        external.mkdir()
        sentinel = external / "preserve.txt"
        sentinel.write_text("user file")
        (folder / ".capture-symlink").symlink_to(external, target_is_directory=True)
        self.client.__exit__(None, None, None)
        self.app = create_app(data_root=self.root, space_download_runner=self.runner)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["job.json"])
        self.assertEqual(sentinel.read_text(), "user file")


@unittest.skipUnless(os.name == "posix", "Process-group cancellation is used on this Mac")
class SpaceDownloadProcessCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_stops_child_process_and_removes_all_private_work_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            script = project / "tools/hls-audio-download/scripts/download_hls.py"
            script.parent.mkdir(parents=True)
            ready = root / "child.pid"
            script.write_text(
                "import argparse, os, signal, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "parser=argparse.ArgumentParser()\n"
                "parser.add_argument('--output-dir')\n"
                "args,_=parser.parse_known_args()\n"
                "work=Path(args.output_dir)/'.hls-audio-child'\n"
                "work.mkdir()\n"
                "(work/'key.bin').write_bytes(b'0123456789abcdef')\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                "def stop(signum, frame):\n"
                "    child.wait(timeout=2)\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                "Path(%r).write_text(str(child.pid))\n"
                "time.sleep(60)\n" % str(ready)
            )
            binary = project / "tools/hls-audio-download/bin/ffmpeg"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o700)
            output = root / "job"
            output.mkdir()
            manager = SpaceDownloadManager(root / "unused")
            capture = {"playlist_url": CDN, "playlist_text": playlist(), "keys": {KEY_URI: KEY}}
            child_pid = None
            with patch("yakiniku.space_downloads.PROJECT", project):
                task = asyncio.create_task(manager._run_download(capture, output))
                try:
                    for _ in range(100):
                        if ready.exists():
                            child_pid = int(ready.read_text())
                            break
                        if task.done():
                            await task
                        await asyncio.sleep(.01)
                    self.assertIsNotNone(child_pid, "Fake FFmpeg child did not start")
                    self.assertTrue(any(output.glob(".capture-*/output/.hls-audio-child/key.bin")))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                    self.assertEqual(list(output.iterdir()), [])
                finally:
                    if not task.done():
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    if child_pid:
                        try:
                            os.kill(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
