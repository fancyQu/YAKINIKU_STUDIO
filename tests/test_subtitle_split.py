"""Readable subtitle boundaries retain text and disclose estimated timestamps."""

from copy import deepcopy
import unittest

from yakiniku.subtitle_split import split_subtitle_cues


def cue(text, start=0, end=6000, **metadata):
    return {"ja": text, "start_ms": start, "end_ms": end,
            "alignment": "provider", "timestamp_source": "api_segments", **metadata}


class SubtitleSplitTests(unittest.TestCase):
    def assert_valid_times(self, children, parent, maximum_ms):
        previous_end = parent["start_ms"]
        for child in children:
            self.assertGreaterEqual(child["start_ms"], previous_end)
            self.assertGreater(child["end_ms"], child["start_ms"])
            self.assertLessEqual(child["end_ms"], parent["end_ms"])
            self.assertLessEqual(child["end_ms"] - child["start_ms"], maximum_ms)
            previous_end = child["end_ms"]

    def assert_estimated(self, children, parent):
        for child in children:
            self.assertEqual(child["alignment"], "approximate")
            self.assertEqual(child["timestamp_source"], "estimated_split")
            self.assertEqual(child["parent_start_ms"], parent["start_ms"])
            self.assertEqual(child["parent_end_ms"], parent["end_ms"])

    def test_sentence_punctuation_is_preferred_and_no_text_is_lost(self):
        parent = cue("こんにちは。今日は配信です！楽しんでいますか？", end=6000)
        result = split_subtitle_cues([parent], max_chars=24, max_seconds=6)
        self.assertEqual([item["ja"] for item in result],
                         ["こんにちは。", "今日は配信です！", "楽しんでいますか？"])
        self.assertEqual("".join(item["ja"] for item in result), parent["ja"])
        self.assert_estimated(result, parent)
        self.assert_valid_times(result, parent, 6000)
        self.assertEqual(result[0]["start_ms"], 0)
        self.assertEqual(result[-1]["end_ms"], 6000)

    def test_long_unpunctuated_text_obeys_character_and_duration_limits(self):
        parent = cue("あいうえおかきくけこ" * 12, start=1300, end=25300)
        result = split_subtitle_cues([parent], max_chars=24, max_seconds=6)
        self.assertEqual("".join(item["ja"] for item in result), parent["ja"])
        self.assertTrue(all(len(item["ja"]) <= 24 for item in result))
        self.assert_valid_times(result, parent, 6000)
        self.assert_estimated(result, parent)
        self.assertEqual(result[0]["start_ms"], parent["start_ms"])
        self.assertEqual(result[-1]["end_ms"], parent["end_ms"])

    def test_duration_limit_can_split_before_character_limit(self):
        parent = cue("あいうえおかきくけこ" * 6, end=30000)
        result = split_subtitle_cues([parent], max_chars=80, max_seconds=6)
        self.assertEqual(len(result), 5)
        self.assertEqual("".join(item["ja"] for item in result), parent["ja"])
        self.assert_valid_times(result, parent, 6000)
        self.assertEqual(result[-1]["end_ms"], 30000)

    def test_short_unchanged_cue_keeps_provider_provenance_and_is_copied(self):
        parent = cue("短い字幕です。", words=[{"word": "短い字幕です。", "start_ms": 0, "end_ms": 6000}])
        original = deepcopy(parent)
        result = split_subtitle_cues([parent])
        self.assertEqual(result, [original])
        self.assertIsNot(result[0], parent)
        result[0]["words"][0]["word"] = "変更"
        self.assertEqual(parent, original)

    def test_splitting_does_not_mutate_cues_or_nested_metadata(self):
        parent = cue("字幕を分割します。元の情報は残ります。", details={"provider": ["fixture"]})
        original = deepcopy(parent)
        result = split_subtitle_cues([parent], max_chars=8)
        self.assertEqual(parent, original)
        self.assertTrue(all(item is not parent for item in result))
        result[0]["details"]["provider"].append("changed")
        self.assertEqual(parent, original)
        self.assertEqual(result[1]["details"], original["details"])

    def test_disabled_mode_returns_independent_copies_without_resegmenting(self):
        parent = cue("長い字幕。" * 20, end=30000, words=[{"word": "fixture"}])
        result = split_subtitle_cues([parent], enabled=False)
        self.assertEqual(result, [parent])
        self.assertIsNot(result[0], parent)
        result[0]["words"][0]["word"] = "changed"
        self.assertEqual(parent["words"][0]["word"], "fixture")

    def test_sparse_long_speech_uses_gaps_instead_of_duplicate_text(self):
        parent = cue("はい", start=2000, end=62000)
        result = split_subtitle_cues([parent], max_chars=24, max_seconds=6)
        self.assertEqual("".join(item["ja"] for item in result), "はい")
        self.assertLessEqual(len(result), 2)
        self.assert_valid_times(result, parent, 6000)
        self.assert_estimated(result, parent)
        self.assertLess(result[-1]["end_ms"], parent["end_ms"])

    def test_single_character_long_parent_is_capped_and_marked_estimated(self):
        parent = cue("あ", end=60000)
        result = split_subtitle_cues([parent], max_seconds=6)
        self.assertEqual([item["ja"] for item in result], ["あ"])
        self.assert_valid_times(result, parent, 6000)
        self.assert_estimated(result, parent)

    def test_submillisecond_density_preserves_text_and_positive_nonoverlapping_cues(self):
        parent = cue("あいうえお" * 10, start=1000, end=1004)
        result = split_subtitle_cues([parent], max_chars=2, max_seconds=6)
        self.assertEqual("".join(item["ja"] for item in result), parent["ja"])
        self.assertLessEqual(len(result), 4, "one millisecond is the smallest representable cue")
        self.assert_valid_times(result, parent, 6000)

    def test_combining_kana_and_joined_emoji_are_never_cut_in_half(self):
        graphemes = ["か\u3099", "👩\u200d💻", "👍🏽"] * 4
        parent = cue("".join(graphemes), end=6000)
        result = split_subtitle_cues([parent], max_chars=2, max_seconds=6)
        self.assertEqual([item["ja"] for item in result],
                         ["".join(graphemes[index:index + 2]) for index in range(0, len(graphemes), 2)])
        self.assertEqual("".join(item["ja"] for item in result), parent["ja"])
        self.assert_valid_times(result, parent, 6000)

    def test_keycap_combining_enclosure_remains_with_its_digit(self):
        graphemes = ["1\ufe0f\u20e3", "2\u20e3", "3\ufe0f\u20e3"]
        parent = cue("".join(graphemes), end=3000)
        result = split_subtitle_cues([parent], max_chars=1)
        self.assertEqual([item["ja"] for item in result], graphemes)
        self.assert_valid_times(result, parent, 6000)

    def test_japanese_ideographic_variation_selector_remains_with_its_character(self):
        graphemes = ["葛\U000e0100", "飴\U000e0101", "日"]
        parent = cue("".join(graphemes), end=3000)
        result = split_subtitle_cues([parent], max_chars=1)
        self.assertEqual([item["ja"] for item in result], graphemes)
        self.assert_valid_times(result, parent, 6000)

    def test_regional_indicator_pairs_remain_complete_flags(self):
        parent = cue("🇯🇵🇨🇳🇰🇷", end=3000)
        result = split_subtitle_cues([parent], max_chars=1)
        self.assertEqual([item["ja"] for item in result], ["🇯🇵", "🇨🇳", "🇰🇷"])
        self.assert_valid_times(result, parent, 6000)

    def test_complete_supplied_word_edges_keep_provider_timestamp_provenance(self):
        parent = cue("日本語文", end=3000, words=[
            {"word": "日", "start_ms": 100, "end_ms": 350},
            {"word": "本", "start_ms": 400, "end_ms": 800},
            {"word": "語", "start_ms": 1100, "end_ms": 1600},
            {"word": "文", "start_ms": 2000, "end_ms": 2700},
        ])
        result = split_subtitle_cues([parent], max_chars=2, max_seconds=3)
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in result],
                         [(100, 800), (1100, 2700)])
        self.assertTrue(all(item["alignment"] == "provider" and item["timestamp_source"] == "api_words"
                            for item in result))
        self.assertTrue(all("words" not in item for item in result))
        self.assert_valid_times(result, parent, 3000)

    def test_variable_speech_rate_splits_at_real_word_edges_without_display_gaps(self):
        text = "あいうえおかきくけこさしすせそた"
        words = []
        position = 0
        for index, char in enumerate(text):
            duration = 1750 if index < 8 else 250
            words.append({"word": char, "start_ms": position, "end_ms": position + duration})
            position += duration
        parent = cue(text, end=16000, words=words)
        result = split_subtitle_cues([parent], max_chars=8, max_seconds=6)
        self.assertEqual("".join(item["ja"] for item in result), text)
        self.assertEqual([len(item["ja"]) for item in result], [3, 3, 6, 4])
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in result],
                         [(0, 5250), (5250, 10500), (10500, 15000), (15000, 16000)])
        self.assertTrue(all(item["alignment"] == "provider" and item["timestamp_source"] == "api_words"
                            for item in result))
        self.assert_valid_times(result, parent, 6000)
        self.assertEqual(result[0]["start_ms"], parent["start_ms"])
        self.assertEqual(result[-1]["end_ms"], parent["end_ms"])
        for previous, following in zip(result, result[1:]):
            self.assertEqual(previous["end_ms"], following["start_ms"])

    def test_missing_or_invalid_word_timestamps_fall_back_to_estimated_boundaries(self):
        examples = [
            [{"word": "日本"}, {"word": "語文"}],
            [{"word": "日本", "start_ms": 0, "end_ms": 1000}, {"word": "語文"}],
            [{"word": "日本", "start_ms": -10, "end_ms": 1000},
             {"word": "語文", "start_ms": 1000, "end_ms": 3000}],
            [{"word": "日本", "start": "invalid", "end": 1},
             {"word": "語文", "start": 1, "end": 3}],
            ["invalid word metadata"],
        ]
        for words in examples:
            with self.subTest(words=words):
                parent = cue("日本語文", end=3000, words=words)
                result = split_subtitle_cues([parent], max_chars=2, max_seconds=3)
                self.assertEqual("".join(item["ja"] for item in result), parent["ja"])
                self.assert_estimated(result, parent)
                self.assert_valid_times(result, parent, 3000)

    def test_disjoint_parent_segments_preserve_silence_and_text_order(self):
        parents = [cue("最初の長い字幕です。次の文です。", end=5000),
                   cue("二番目の字幕です。", start=9000, end=11000)]
        result = split_subtitle_cues(parents, max_chars=8, max_seconds=6)
        self.assertEqual("".join(item["ja"] for item in result), "".join(item["ja"] for item in parents))
        for parent in parents:
            children = [item for item in result if parent["start_ms"] <= item["start_ms"] < parent["end_ms"]]
            self.assertTrue(children)
            self.assert_valid_times(children, parent, 6000)
        self.assertFalse(any(item["start_ms"] < 9000 and item["end_ms"] > 5000 for item in result))

    def test_invalid_character_and_duration_limits_fail_explicitly(self):
        for value in (0, -1, True, 2.5, "24"):
            with self.subTest(max_chars=value), self.assertRaises(ValueError):
                split_subtitle_cues([cue("字幕")], max_chars=value)
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(max_seconds=value), self.assertRaises(ValueError):
                split_subtitle_cues([cue("字幕")], max_seconds=value)


if __name__ == "__main__":
    unittest.main()
