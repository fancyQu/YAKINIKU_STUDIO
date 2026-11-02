"""End-to-end checks using FFmpeg, AES test media, and a local HTTP server.

Run with HLS_TEST_FFMPEG=/path/to/ffmpeg python3 -m unittest discover \
    -s tools/hls-audio-download/tests -v
"""

import base64
import functools
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
import wave


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_hls.py"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.server.request_paths.append(urlsplit(self.path).path)
        if urlsplit(self.path).path == "/redirect.m3u8":
            self.send_response(302)
            self.send_header("Location", "/nested/replay.m3u8")
            self.end_headers()
            return
        if urlsplit(self.path).path == "/denied.key":
            self.send_error(403)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


class DownloadHLSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get("HLS_TEST_FFMPEG") or shutil.which("ffmpeg")
        cls.openssl = shutil.which("openssl")
        if not cls.ffmpeg or not cls.openssl:
            raise unittest.SkipTest("FFmpeg and OpenSSL are required for media integration tests")
        if not SCRIPT.is_file():
            raise unittest.SkipTest("Downloader script has not been created yet")
        cls.temp = tempfile.TemporaryDirectory(prefix="hls-audio-integration-")
        cls.root = Path(cls.temp.name)
        cls.web = cls.root / "web"
        cls.web.mkdir()
        cls.key = bytes(range(16))
        cls.key_hex = cls.key.hex()
        cls.key_b64 = base64.b64encode(cls.key).decode("ascii")
        cls.duration = 0.0
        cls.segment_durations = []
        for index in range(2):
            plain = cls.web / ("plain-%d.aac" % index)
            cls.run_media([
                "-f", "lavfi", "-i",
                "sine=frequency=%d:sample_rate=48000:duration=1" % (440 + index * 440),
                "-c:a", "aac", "-b:a", "96k", "-f", "adts", str(plain),
            ])
            decoded = cls.root / ("fixture-%d.wav" % index)
            cls.run_media(["-i", str(plain), "-c:a", "pcm_s16le", str(decoded)])
            with wave.open(str(decoded), "rb") as audio:
                seconds = audio.getnframes() / audio.getframerate()
            cls.segment_durations.append(seconds)
            cls.duration += seconds
            iv = (0x112233445566778899AABBCCDDEEFF00 + index).to_bytes(16, "big")
            subprocess.run([
                cls.openssl, "enc", "-aes-128-cbc", "-K", cls.key_hex,
                "-iv", iv.hex(), "-in", str(plain),
                "-out", str(cls.web / ("encrypted-%d.aac" % index)),
            ], check=True, capture_output=True)

        encrypted = [
            "#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2",
            "#EXT-X-MEDIA-SEQUENCE:17", "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        clear = encrypted.copy()
        for index, seconds in enumerate(cls.segment_durations):
            iv = (0x112233445566778899AABBCCDDEEFF00 + index).to_bytes(16, "big")
            encrypted.extend([
                '#EXT-X-KEY:METHOD=AES-128,URI="/denied.key",IV=0x' + iv.hex(),
                "#EXTINF:%.9f," % seconds, "encrypted-%d.aac" % index,
            ])
            clear.extend(["#EXTINF:%.9f," % seconds, "plain-%d.aac" % index])
        encrypted.append("#EXT-X-ENDLIST")
        clear.append("#EXT-X-ENDLIST")
        (cls.web / "encrypted.m3u8").write_text("\n".join(encrypted) + "\n")
        (cls.web / "clear.m3u8").write_text("\n".join(clear) + "\n")
        (cls.web / "missing.m3u8").write_text(
            "\n".join(clear).replace("plain-1.aac", "missing-segment.aac") + "\n"
        )
        (cls.web / "live.m3u8").write_text(
            "\n".join(x for x in clear if x not in ("#EXT-X-ENDLIST", "#EXT-X-PLAYLIST-TYPE:VOD")) + "\n"
        )
        gaps = clear.copy()
        gaps.insert(gaps.index("plain-1.aac"), "#EXT-X-GAP")
        (cls.web / "gap.m3u8").write_text("\n".join(gaps) + "\n")
        nested = cls.web / "nested"
        nested.mkdir()
        nested_playlist = "\n".join(clear) + "\n"
        for index in range(2):
            name = "only-nested-%d.aac" % index
            shutil.copyfile(cls.web / ("plain-%d.aac" % index), nested / name)
            nested_playlist = nested_playlist.replace("plain-%d.aac" % index, name)
        (nested / "replay.m3u8").write_text(nested_playlist)
        (cls.web / "master.m3u8").write_text(
            '#EXTM3U\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Original",DEFAULT=YES,AUTOSELECT=YES,URI="clear.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=5000000,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="sound"\n'
            'not-the-audio.m3u8\n'
        )
        handler = functools.partial(QuietHandler, directory=str(cls.web))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server.request_paths = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = "http://127.0.0.1:%d/" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=3)
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @classmethod
    def run_media(cls, args):
        return subprocess.run(
            [cls.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"] + args,
            check=True, capture_output=True,
        )

    def invoke(self, case, *args):
        output = self.root / case
        result = subprocess.run([
            sys.executable, str(SCRIPT), "--ffmpeg", self.ffmpeg,
            "--output-dir", str(output), "--name", case,
        ] + list(args), text=True, capture_output=True, timeout=60)
        self.assertNotIn(self.key_hex, result.stdout + result.stderr)
        self.assertNotIn(self.key_b64, result.stdout + result.stderr)
        self.assertNotIn(self.base_url, result.stdout + result.stderr)
        return output, result

    def assert_successful_mp3(self, output, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mp3s = list(output.glob("*.mp3"))
        self.assertEqual(len(mp3s), 1)
        self.assertGreater(mp3s[0].stat().st_size, 1000)
        decoded = self.root / (output.name + "-decoded.wav")
        self.run_media(["-xerror", "-i", str(mp3s[0]), "-c:a", "pcm_s16le", str(decoded)])
        with wave.open(str(decoded), "rb") as audio:
            self.assertEqual(audio.getframerate(), 44100)
            self.assertEqual(audio.getnchannels(), 2)
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), self.duration, delta=0.1)
        reports = list(output.glob("*.json"))
        self.assertEqual(len(reports), 1)
        text = reports[0].read_text()
        self.assertNotIn(self.key_hex, text)
        self.assertNotIn(self.key_b64, text)
        self.assertNotIn(self.base_url, text)
        report = json.loads(text)
        self.assertTrue(report["ffmpeg_full_decode_passed"])
        self.assertEqual(report["segments"], 2)
        return report

    def test_catgrab_key_decrypts_each_iv_without_contacting_key_endpoint(self):
        pasted = self.root / "catgrab.txt"
        pasted.write_text(
            "# 猫抓 m3u8解析器\nm3u8文件地址\n[播放列表](%s)\n"
            "密钥地址(KeyURL): %s\n加密算法(Method):AES-128\n"
            "密钥(Hex):%s\n密钥(Base64):%s\n"
            "偏移量(IV):0x112233445566778899aabbccddeeff01\n" % (
                self.base_url + "encrypted.m3u8", self.base_url + "denied.key",
                self.key_hex, self.key_b64,
            ), encoding="utf-8",
        )
        self.server.request_paths.clear()
        output, result = self.invoke("catgrab", "--input", str(pasted), "--wav")
        report = self.assert_successful_mp3(output, result)
        self.assertEqual(report["distinct_IVs"], 2)
        self.assertNotIn("/denied.key", self.server.request_paths)
        wavs = list(output.glob("*.wav"))
        self.assertEqual(len(wavs), 1)
        with wave.open(str(wavs[0]), "rb") as audio:
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), self.duration, delta=0.1)
        self.assertFalse(any(p.name == "key.bin" or p.suffix == ".key" for p in output.rglob("*")))

    def test_missing_segment_fails_without_delivering_mp3(self):
        output, result = self.invoke("missing", "--url", self.base_url + "missing.m3u8")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(output.glob("*.mp3")))

    def test_unfinished_live_playlist_is_not_claimed_as_complete(self):
        output, result = self.invoke("live", "--url", self.base_url + "live.m3u8")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(output.glob("*.mp3")))
        self.assertTrue("ENDLIST" in result.stdout + result.stderr or "直播" in result.stdout + result.stderr)

    def test_master_selects_separate_audio_rendition(self):
        self.server.request_paths.clear()
        output, result = self.invoke("master", "--url", self.base_url + "master.m3u8")
        self.assert_successful_mp3(output, result)
        self.assertIn("/clear.m3u8", self.server.request_paths)
        self.assertNotIn("/not-the-audio.m3u8", self.server.request_paths)

    def test_redirected_playlist_resolves_relative_segments_at_final_location(self):
        self.server.request_paths.clear()
        output, result = self.invoke("redirect", "--url", self.base_url + "redirect.m3u8")
        self.assert_successful_mp3(output, result)
        self.assertIn("/nested/only-nested-0.aac", self.server.request_paths)
        self.assertNotIn("/only-nested-0.aac", self.server.request_paths)

    def test_gap_tag_is_rejected_even_when_segment_file_exists(self):
        output, result = self.invoke("gap", "--url", self.base_url + "gap.m3u8")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(output.glob("*.mp3")))

    def test_browser_capture_uses_local_playlist_and_matching_rotation_keys(self):
        # Neither the original manifest endpoint nor either key endpoint needs
        # to be accessible from Python once the browser supplies their bodies.
        original = (self.web / "encrypted.m3u8").read_text()
        first, rest = original.split("encrypted-0.aac", 1)
        text = first.replace("/denied.key", "/rotation-0.key") + "encrypted-0.aac" + rest.replace("/denied.key", "/rotation-1.key")
        playlist = self.root / "browser-captured.m3u8"
        playlist.write_text(text)
        keys = self.root / "browser-captured-keys.json"
        keys.write_text(json.dumps({self.base_url + "rotation-0.key": self.key_b64,
                                    self.base_url + "rotation-1.key": self.key_b64}))
        self.server.request_paths.clear()
        output, result = self.invoke("browser", "--url", self.base_url + "never-fetch-this.m3u8",
                                     "--playlist-file", str(playlist), "--key-map-file", str(keys))
        report = self.assert_successful_mp3(output, result)
        self.assertEqual(report["distinct_key_urls"], 2)
        self.assertNotIn("/never-fetch-this.m3u8", self.server.request_paths)
        self.assertFalse(any("rotation-" in x for x in self.server.request_paths))


if __name__ == "__main__":
    unittest.main()
