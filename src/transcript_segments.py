"""
Pre-process Rev.ai words into numbered SENTENCE segments for reel selection.

One sentence (or tight self-contained clause) per segment with word-accurate times.
The selection model picks whole segments by id — never mid-sentence cuts.
"""

from __future__ import annotations

import re

SILENCE_TOKENS = {"<silence>", "[silence]", "(silence)"}
ALWAYS_CAP = frozenset({"I", "I'm", "I'll", "I've", "I'd"})


def build_sentence_segments(
    words: list[dict],
    video_duration: float = 0.0,
) -> list[dict]:
    """
    Group words into sentence-bounded segments with stable 0-based ids.
    Times come from first/last word — never rounded.
    """
    del video_duration  # reserved for future clamping
    if not words:
        return []

    work: list[dict] = []
    for i, raw in enumerate(words):
        row = {
            **raw,
            "index": int(raw.get("index", i)),
            "word": str(raw.get("word") or "").strip(),
            "start": float(raw.get("start", 0.0) or 0.0),
            "end": raw.get("end"),
            "speaker": int(raw.get("speaker", 0) or 0),
        }
        if _is_silence(row["word"]):
            continue
        work.append(row)

    _fill_word_ends(work)

    segs: list[list[int]] = []
    cur: list[int] = []

    for i, w in enumerate(work):
        cur.append(i)
        last = i == len(work) - 1
        gap = (work[i + 1]["start"] - w["end"]) if not last else 999.0
        nxt_word = work[i + 1]["word"] if not last else ""
        speaker_change = (not last) and work[i + 1]["speaker"] != w["speaker"]
        cap_start = (
            not last
            and gap > 0.35
            and len(nxt_word) > 0
            and nxt_word[:1].isupper()
            and nxt_word not in ALWAYS_CAP
        )
        too_long = (w["end"] - work[cur[0]]["start"]) > 12.0 or len(cur) >= 40

        if last or speaker_change or gap > 0.65 or cap_start or too_long:
            segs.append(cur)
            cur = []

    if cur:
        segs.append(cur)

    out: list[dict] = []
    for idxs in segs:
        first = work[idxs[0]]
        last_w = work[idxs[-1]]
        out.append(
            {
                "id": len(out),
                "start": float(first["start"]),
                "end": float(last_w["end"]),
                "text": " ".join(work[k]["word"] for k in idxs),
                "word_start_index": int(first["index"]),
                "word_end_index": int(last_w["index"]),
                "speaker": int(first.get("speaker", 0) or 0),
                "first_word_index": int(first["index"]),
                "last_word_index": int(last_w["index"]),
            }
        )

    out = _merge_micro_fragments(out, work)
    for sid, seg in enumerate(out):
        seg["id"] = sid
    return out


def build_utterance_segments(
    words: list[dict],
    *,
    pause_threshold: float = 0.6,
    min_segment_duration: float = 0.15,
    video_duration: float = 0.0,
) -> list[dict]:
    """Backward-compatible alias."""
    del pause_threshold, min_segment_duration
    return build_sentence_segments(words, video_duration=video_duration)


def utterance_segment_map(segments: list[dict]) -> dict[int, dict]:
    return {int(seg["id"]): seg for seg in segments if seg.get("id") is not None}


def _fill_word_ends(work: list[dict]) -> None:
    for i, w in enumerate(work):
        end = w.get("end")
        if end is None or float(end) <= w["start"]:
            nxt = work[i + 1]["start"] if i + 1 < len(work) else w["start"] + 0.4
            w["end"] = min(nxt - 0.02, w["start"] + 1.0)
        else:
            w["end"] = float(end)


def _merge_micro_fragments(segments: list[dict], work: list[dict]) -> list[dict]:
    """Merge 1-word fragments into the same-speaker neighbor with the smaller gap."""
    if len(segments) <= 1:
        return segments

    word_to_work = {int(w["index"]): w for w in work}

    def word_count(seg: dict) -> int:
        return int(seg["word_end_index"]) - int(seg["word_start_index"]) + 1

    def gap_between(left: dict, right: dict) -> float:
        lw = word_to_work.get(int(left["word_end_index"]))
        rw = word_to_work.get(int(right["word_start_index"]))
        if not lw or not rw:
            return 999.0
        return max(0.0, float(rw["start"]) - float(lw["end"]))

    def merge_pair(left: dict, right: dict) -> dict:
        return {
            "id": left["id"],
            "start": float(left["start"]),
            "end": float(right["end"]),
            "text": f"{left['text']} {right['text']}".strip(),
            "word_start_index": int(left["word_start_index"]),
            "word_end_index": int(right["word_end_index"]),
            "speaker": int(left["speaker"]),
            "first_word_index": int(left["word_start_index"]),
            "last_word_index": int(right["word_end_index"]),
        }

    current = list(segments)
    while True:
        merged_any = False
        for i, seg in enumerate(current):
            if word_count(seg) > 1:
                continue
            prev_seg = current[i - 1] if i > 0 else None
            next_seg = current[i + 1] if i + 1 < len(current) else None
            options: list[tuple[str, float, int, int]] = []
            if prev_seg and prev_seg["speaker"] == seg["speaker"]:
                options.append(("prev", gap_between(prev_seg, seg), i - 1, i))
            if next_seg and next_seg["speaker"] == seg["speaker"]:
                options.append(("next", gap_between(seg, next_seg), i, i + 1))
            if not options:
                continue
            options.sort(key=lambda row: row[1])
            direction, _, left_i, right_i = options[0]
            if direction == "prev":
                current[left_i] = merge_pair(current[left_i], current[right_i])
                current.pop(right_i)
            else:
                current[right_i] = merge_pair(current[left_i], current[right_i])
                current.pop(left_i)
            merged_any = True
            break
        if not merged_any:
            break
    return current


def _is_silence(token: str) -> bool:
    return str(token or "").strip().lower() in SILENCE_TOKENS


def _format_time(seconds: float) -> str:
    value = float(seconds or 0.0)
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if "." in text else f"{text}.0"


def format_segments_for_claude(segments: list[dict]) -> str:
    """[id] start=<sec.dec> end=<sec.dec> speaker=<n> \"sentence text\"

    Speaker id is included so Claude can actually tell who said what — without
    it, a "main speaker only" instruction has nothing to act on, since every
    segment would look identical regardless of who's talking.
    """
    lines: list[str] = []
    for seg in segments:
        text = str(seg.get("text") or "").replace('"', "'")
        start = _format_time(seg.get("start", 0.0))
        end = _format_time(seg.get("end", 0.0))
        speaker = int(seg.get("speaker", 0) or 0)
        lines.append(f'[{seg["id"]}] start={start} end={end} speaker={speaker} "{text}"')
    return "\n".join(lines)
