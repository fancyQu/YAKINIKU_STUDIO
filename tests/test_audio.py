import struct
import tempfile
import unittest
import wave
from pathlib import Path

from yakiniku.audio import AudioSegmenter, SegmentedWaveWriter, WaveWriter


def pcm(milliseconds, amplitude=1200):
    return struct.pack("<h", amplitude) * round(24000 * milliseconds / 1000)


class WaveWriterTests(unittest.TestCase):
    def test_recording_is_readable_before_close_and_preserves_stereo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            writer = WaveWriter(path, 48000, 2)
            stereo = struct.pack("<hh", 1234, -5678) * 4800
            writer.write(stereo)
            with wave.open(str(path), "rb") as reader:
                self.assertEqual(reader.getnchannels(), 2)
                self.assertEqual(reader.getframerate(), 48000)
                self.assertEqual(reader.getnframes(), 4800)
                self.assertEqual(reader.readframes(1), struct.pack("<hh", 1234, -5678))
            self.assertEqual(writer.duration_ms, 100)
            writer.write(stereo)
            writer.close()
            writer.close()
            with wave.open(str(path), "rb") as reader:
                self.assertEqual(reader.getnframes(), 9600)

    def test_incomplete_frames_are_rejected_without_corrupting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = WaveWriter(Path(directory) / "audio.wav")
            with self.assertRaises(ValueError):
                writer.write(b"\x00\x00")
            self.assertEqual(writer.duration_ms, 0)
            writer.close()


class SegmentedWaveWriterTests(unittest.TestCase):
    def test_split_retains_all_frames_and_global_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = SegmentedWaveWriter(Path(directory), 48000, 2, part_seconds=.1)
            source = struct.pack("<hh", 1234, -5678) * 12000
            # The input chunks deliberately do not align to part boundaries.
            writer.write(source[:12348])
            writer.write(source[12348:])
            self.assertEqual(writer.duration_ms, 250)
            self.assertEqual(writer.parts, [
                {"name": "audio.wav", "start_ms": 0, "end_ms": 100},
                {"name": "audio-002.wav", "start_ms": 100, "end_ms": 200},
                {"name": "audio-003.wav", "start_ms": 200, "end_ms": 250},
            ])
            writer.close()
            captured = []
            for part in writer.parts:
                with wave.open(str(Path(directory) / part["name"]), "rb") as reader:
                    self.assertLessEqual(reader.getnframes(), 4800)
                    captured.append(reader.readframes(reader.getnframes()))
            self.assertEqual(b"".join(captured), source)

    def test_exact_boundary_does_not_create_an_empty_trailing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = SegmentedWaveWriter(Path(directory), 24000, 1, part_seconds=.1)
            writer.write(pcm(200))
            writer.close()
            self.assertEqual([part["name"] for part in writer.parts], ["audio.wav", "audio-002.wav"])
            self.assertFalse((Path(directory) / "audio-003.wav").exists())


class SegmenterTests(unittest.TestCase):
    def test_silence_is_skipped_without_shifting_later_subtitle_time(self):
        segmenter = AudioSegmenter()
        self.assertEqual(segmenter.process(pcm(2000, 0)), [])
        actions = segmenter.process(pcm(1000))
        actions += segmenter.process(pcm(600, 0))
        commits = [action for action in actions if action["type"] == "commit"]
        self.assertEqual(commits, [{"type": "commit", "start_ms": 1800, "end_ms": 3600}])
        self.assertEqual(segmenter.process(pcm(2000, 0)), [])
        actions = segmenter.process(pcm(300)) + segmenter.flush()
        self.assertEqual(actions[-1], {"type": "commit", "start_ms": 5400, "end_ms": 5900})
        self.assertEqual(segmenter.flush(), [])

    def test_continuous_speech_has_bounded_turns_and_no_lost_samples(self):
        segmenter = AudioSegmenter(segment_seconds=1)
        source = pcm(2500)
        actions = segmenter.process(source) + segmenter.flush()
        commits = [action for action in actions if action["type"] == "commit"]
        self.assertEqual([(action["start_ms"], action["end_ms"]) for action in commits],
                         [(0, 1000), (1000, 2000), (2000, 2500)])
        output = b"".join(action["pcm"] for action in actions if action["type"] == "append")
        self.assertEqual(output, source)

    def test_short_tail_is_padded_for_provider_but_keeps_original_timing(self):
        segmenter = AudioSegmenter(preroll_ms=0)
        source = pcm(20)
        actions = segmenter.process(source) + segmenter.flush()
        output = b"".join(action["pcm"] for action in actions if action["type"] == "append")
        self.assertTrue(output.startswith(source))
        self.assertEqual(len(output), len(pcm(100)))
        self.assertEqual(actions[-1], {"type": "commit", "start_ms": 0, "end_ms": 20})

    def test_arbitrary_websocket_chunk_boundaries_keep_sample_clock(self):
        segmenter = AudioSegmenter(preroll_ms=0)
        source = pcm(1234)
        actions = []
        for offset in range(0, len(source), 722):
            actions += segmenter.process(source[offset:offset + 722])
        actions += segmenter.flush()
        self.assertEqual(actions[-1]["end_ms"], 1234)
        self.assertEqual(b"".join(action["pcm"] for action in actions if action["type"] == "append"), source)
        with self.assertRaises(ValueError):
            segmenter.process(b"\x00")


if __name__ == "__main__":
    unittest.main()
