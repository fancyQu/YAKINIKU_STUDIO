"""Readable file subtitles, with explicit provenance for estimated boundaries."""
from __future__ import annotations

import math
import unicodedata
from copy import deepcopy

DEFAULT_SPLIT = {"enabled": True, "max_chars": 24, "max_seconds": 6.0}


def subtitle_settings(settings):
    return {"enabled": bool(settings.get("subtitle_split_enabled", True)),
            "max_chars": int(settings.get("subtitle_max_chars", 24)),
            "max_seconds": float(settings.get("subtitle_max_seconds", 6.0))}


def _clusters(text):
    """Do not cut combining kana, emoji modifiers or joined emoji in half."""
    result = []
    for char in text:
        joined = (unicodedata.category(char).startswith("M") or char == "\u200d"
                  or 0xE0100 <= ord(char) <= 0xE01EF
                  or 0x1F3FB <= ord(char) <= 0x1F3FF
                  or result and result[-1].endswith("\u200d"))
        if result and joined:
            result[-1] += char
        elif (result and 0x1F1E6 <= ord(char) <= 0x1F1FF and len(result[-1]) == 1
              and 0x1F1E6 <= ord(result[-1]) <= 0x1F1FF):
            result[-1] += char
        else:
            result.append(char)
    return result


def _ranges(clusters, limit):
    start, count = 0, len(clusters)
    closers = set("」』）)]】”’\"")
    while start < count:
        stop = min(count, start + limit)
        hard = next((i + 1 for i in range(start, stop)
                     if clusters[i] in "。！？!?\n" or (clusters[i] == "." and not (
                         i and i + 1 < count and clusters[i - 1].isdigit() and clusters[i + 1].isdigit()))), None)
        if hard is not None:
            stop = hard
            while stop < count and stop - start < limit and clusters[stop] in closers:
                stop += 1
        elif stop < count:
            soft = [i + 1 for i in range(start, stop) if clusters[i] in "、，,；;：: \t"
                    and i + 1 - start >= max(1, limit // 2)]
            if soft:
                stop = soft[-1]
        yield start, stop
        start = stop


def _word_boundaries(cue, text):
    """Use exact supplied word edges where a split lands on a word boundary."""
    words = cue.get("words") or []
    if not isinstance(words, list) or not words or any(not isinstance(w, dict) for w in words):
        return {}, {}
    if "".join(str(w.get("word", w.get("text", ""))) for w in words) != text:
        return {}, {}
    starts, ends, position = {}, {}, 0
    for word in words:
        value = str(word.get("word", word.get("text", "")))
        begin, end = word.get("start_ms"), word.get("end_ms")
        try:
            if begin is None or end is None:
                # Adapters may retain provider-relative seconds instead of ms.
                begin = cue["start_ms"] + round(float(word["start"]) * 1000)
                end = cue["start_ms"] + round(float(word["end"]) * 1000)
            if (not math.isfinite(float(begin)) or not math.isfinite(float(end))
                    or not cue["start_ms"] <= begin < end <= cue["end_ms"]):
                return {}, {}
        except (KeyError, TypeError, ValueError, OverflowError):
            return {}, {}
        starts[position] = round(begin)
        position += len(value)
        ends[position] = round(end)
    return starts, ends


def _timed_ranges(ranges, offsets, starts, ends, maximum_ms):
    """Refine long word-timed spans at real word edges, before estimating."""
    output = []
    for left, right in ranges:
        while (offsets[left] in starts and offsets[right] in ends
               and ends[offsets[right]] - starts[offsets[left]] > maximum_ms):
            candidates = [edge for edge in range(left + 1, right)
                          if offsets[edge] in starts and offsets[edge] in ends
                          and 0 < ends[offsets[edge]] - starts[offsets[left]] <= maximum_ms]
            if not candidates:
                break
            edge = candidates[-1]
            output.append((left, edge))
            left = edge
        output.append((left, right))
    return output


def split_subtitle_cues(cues, max_chars=24, max_seconds=6.0, enabled=True):
    """Split before GPT processing; never slice Japanese and Chinese independently.

    Parent segment timing alone cannot locate the spoken short sentences. Such
    new boundaries are estimates, not claimed to be exact acoustic alignment.
    """
    if not enabled:
        return deepcopy(cues)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("每句字数必须是正整数。")
    if not math.isfinite(float(max_seconds)) or max_seconds <= 0:
        raise ValueError("每句显示时长必须大于零。")
    maximum_ms = max(1, round(float(max_seconds) * 1000))
    output = []
    for original in cues:
        text = str(original.get("ja") or "")
        if not text:
            continue
        clusters = _clusters(text)
        begin, finish = int(original["start_ms"]), int(original["end_ms"])
        duration = finish - begin
        if duration <= 0:
            raise ValueError("字幕时间范围无效。")
        # Sparse speech may not contain enough characters to fill all time
        # windows. Leave gaps instead of duplicating text or displaying forever.
        limit = min(max_chars, max(1, math.floor(maximum_ms * len(clusters) / duration)))
        ranges = list(_ranges(clusters, limit))
        word_starts, word_ends = _word_boundaries(original, text)
        offsets = [0]
        for cluster in clusters:
            offsets.append(offsets[-1] + len(cluster))
        ranges = _timed_ranges(ranges, offsets, word_starts, word_ends, maximum_ms)
        packed = len(ranges) > duration
        if packed:
            # One positive millisecond per cue is the minimum SRT can encode.
            # Keep all text rather than inventing overlapping zero-length cues.
            ranges = ranges[:duration - 1] + [(ranges[duration - 1][0], len(clusters))]
        if len(ranges) == 1 and duration <= maximum_ms:
            output.append(deepcopy(original))
            continue
        previous_end = begin
        children = []
        for index, (left, right) in enumerate(ranges):
            cue = deepcopy(original)
            chunk = "".join(clusters[left:right])
            start = begin + round(duration * left / len(clusters))
            end = begin + round(duration * right / len(clusters))
            exact = offsets[left] in word_starts and offsets[right] in word_ends
            if exact:
                start, end = word_starts[offsets[left]], word_ends[offsets[right]]
            remaining = len(ranges) - index - 1
            adjusted_start = max(begin, previous_end, min(finish - remaining - 1, start))
            adjusted_end = min(finish - remaining, max(adjusted_start + 1, end))
            if (adjusted_start, adjusted_end) != (start, end):
                exact = False
            start, end = adjusted_start, adjusted_end
            if end - start > maximum_ms:
                end, exact = start + maximum_ms, False
            cue.update(ja=chunk, start_ms=start, end_ms=end,
                       parent_start_ms=begin, parent_end_ms=finish,
                       split_parent_source=original.get("timestamp_source", ""))
            if packed:
                cue["split_limits_adjusted"] = True
            cue.pop("words", None)
            if not exact:
                cue.update(alignment="approximate", timestamp_source="estimated_split")
            else:
                cue.update(alignment="provider", timestamp_source="api_words")
            children.append(cue)
            previous_end = end
        for index, cue in enumerate(children):
            cue.update(split_index=index, split_count=len(children))
        output.extend(children)
    return output
