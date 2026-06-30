"""Build word-synced karaoke caption blocks for export."""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any

MIN_BLOCK_GAP_SEC = 0.03
MIN_WORD_DISPLAY_SEC = 0.04
MIN_BLOCK_DURATION_SEC = 0.25
DEFAULT_KARAOKE_CHUNK_SIZE = 2

# Min fuzzy ratio for a transcript token to be treated as a (mis-spelled) name.
NAME_MATCH_THRESHOLD = 0.72
_NAME_TOKEN_RE = re.compile(r"^(\W*)(.*?)(\W*)$", re.DOTALL)


def _name_targets() -> list[str]:
    """Correct-spelling name tokens from REEL_SPEAKER_NAME (e.g. 'Caylene Salii')."""
    raw = str(os.getenv("REEL_SPEAKER_NAME") or "").strip()
    if not raw:
        return []
    return [tok for tok in re.split(r"\s+", raw) if len(tok) >= 3]


def _name_aliases() -> dict[str, str]:
    """Explicit 'wrong=Right' overrides from REEL_NAME_ALIASES (comma-separated)."""
    raw = str(os.getenv("REEL_NAME_ALIASES") or "").strip()
    out: dict[str, str] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        if "=" in pair:
            wrong, right = pair.split("=", 1)
            wrong = wrong.strip().lower()
            right = right.strip()
            if wrong and right:
                out[wrong] = right
    return out


def _match_case(correct: str, sample: str) -> str:
    if sample.isupper():
        return correct.upper()
    if sample[:1].isupper():
        return correct[:1].upper() + correct[1:]
    return correct


def correct_speaker_name(text: str) -> str:
    """Replace a mis-transcribed name token with its correct spelling, if configured."""
    targets = _name_targets()
    aliases = _name_aliases()
    if not targets and not aliases:
        return text

    m = _NAME_TOKEN_RE.match(text or "")
    if not m:
        return text
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    if not core:
        return text

    low = core.lower()
    if low in aliases:
        return f"{lead}{_match_case(aliases[low], core)}{trail}"

    for target in targets:
        if low == target.lower():
            # Correct spelling already; ensure proper-noun capitalization.
            return f"{lead}{_match_case(target, core) if core[:1].isupper() else target}{trail}"
        if len(core) < 4:
            continue
        ratio = SequenceMatcher(None, low, target.lower()).ratio()
        if ratio >= NAME_MATCH_THRESHOLD:
            return f"{lead}{_match_case(target, core)}{trail}"
    return text


def segment_duration(segment: dict[str, Any]) -> float:
    start = float(segment.get("start_time_seconds") or 0)
    end = float(segment.get("end_time_seconds") or start)
    return max(0.0, end - start)


def timeline_total(segments: list[dict[str, Any]]) -> float:
    return max(1.0, sum(segment_duration(seg) for seg in segments))


def build_playback_words(reel: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_words = reel.get("timestamped_words") or []
    if not isinstance(source_words, list):
        return []

    if not segments:
        raw = []
        offset = 0.0
        for word in source_words:
            if not isinstance(word, dict):
                continue
            ws = float(word.get("time") if "time" in word else word.get("start") or 0)
            we = float(word.get("end") or ws)
            local_start = offset + ws
            local_end = offset + we
            raw.append(
                {
                    **word,
                    "word": str(word.get("word") or "").strip(),
                    "time": ws,
                    "end": local_end,
                    "sourceEnd": we,
                    "localTime": local_start,
                }
            )
        return normalize_word_timeline(raw)

    offset = 0.0
    playback: list[dict[str, Any]] = []
    for segment in segments:
        seg_start = float(segment.get("start_time_seconds") or 0)
        seg_end = float(segment.get("end_time_seconds") or seg_start)
        for word in source_words:
            if not isinstance(word, dict):
                continue
            ws = float(word.get("time") if "time" in word else word.get("start") or 0)
            we = float(word.get("end") or ws)
            if we < seg_start or ws > seg_end:
                continue
            local_start = offset + max(0.0, ws - seg_start)
            local_end = offset + max(0.0, min(we, seg_end) - seg_start)
            if local_end <= local_start:
                local_end = local_start + MIN_WORD_DISPLAY_SEC
            playback.append(
                {
                    "word": str(word.get("word") or "").strip(),
                    "time": ws,
                    "end": local_end,
                    "sourceEnd": we,
                    "speaker": word.get("speaker", 0),
                    "localTime": local_start,
                }
            )
        offset += max(0.0, seg_end - seg_start)
    return normalize_word_timeline(playback)


def normalize_word_timeline(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep Rev.ai word timings on playback timeline — no artificial shifting."""
    cleaned: list[dict[str, Any]] = []
    for raw in sorted(words, key=lambda w: float(w.get("localTime") or w.get("time") or 0)):
        text = str(raw.get("word") or "").strip()
        if not text:
            continue
        text = correct_speaker_name(text)
        start = max(0.0, float(raw.get("localTime") if "localTime" in raw else raw.get("time") or 0))
        end = float(raw.get("end") or start)
        if end <= start:
            end = start + MIN_WORD_DISPLAY_SEC
        cleaned.append({**raw, "word": text, "localTime": round(start, 4), "end": round(end, 4)})
    return cleaned


def finalize_caption_timing(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Block bounds follow word times with a minimum gap between consecutive blocks."""
    sorted_blocks = sorted(blocks, key=lambda b: float(b.get("start_time_seconds") or 0))
    prev_end = 0.0
    for index, block in enumerate(sorted_blocks):
        words = block.get("words") or []
        if words:
            start = max(prev_end + MIN_BLOCK_GAP_SEC, float(words[0].get("localTime") or 0))
            end = max(start + MIN_BLOCK_DURATION_SEC, float(words[-1].get("end") or start))
            if index + 1 < len(sorted_blocks):
                next_words = sorted_blocks[index + 1].get("words") or []
                next_start = (
                    float(next_words[0].get("localTime") or 0)
                    if next_words
                    else float(sorted_blocks[index + 1].get("start_time_seconds") or 0)
                )
                end = min(end, max(start + MIN_BLOCK_DURATION_SEC, next_start - MIN_BLOCK_GAP_SEC))
            else:
                end = max(end, float(words[-1].get("end") or start) + 0.28)
            block["start_time_seconds"] = round(start, 3)
            block["end_time_seconds"] = round(end, 3)
        block["text"] = " ".join(
            str(w.get("word") or "").strip() for w in words if str(w.get("word") or "").strip()
        ) or str(block.get("text") or "").strip()
        prev_end = float(block.get("end_time_seconds") or prev_end)
    return sorted_blocks


def make_caption_blocks(words: list[dict[str, Any]], total: float, *, chunk_size: int = DEFAULT_KARAOKE_CHUNK_SIZE) -> list[dict[str, Any]]:
    words = normalize_word_timeline(words)
    blocks: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []

    def flush() -> None:
        if not chunk:
            return
        start = float(chunk[0].get("localTime") or 0)
        end = float(chunk[-1].get("end") or start + MIN_BLOCK_DURATION_SEC)
        end = max(start + MIN_BLOCK_DURATION_SEC, end)
        blocks.append(
            {
                "start_time_seconds": round(start, 3),
                "end_time_seconds": round(end, 3),
                "text": " ".join(str(w.get("word") or "").strip() for w in chunk if str(w.get("word") or "").strip()),
                "words": [dict(w) for w in chunk],
                "speaker": chunk[0].get("speaker", 0),
            }
        )
        chunk.clear()

    for word in words[:360]:
        speaker = word.get("speaker", 0)
        if chunk and chunk[0].get("speaker", 0) != speaker:
            flush()
        chunk.append(word)
        if len(chunk) >= chunk_size:
            flush()
    flush()
    return finalize_caption_timing(blocks)


def build_captions_for_reel(reel: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words = build_playback_words(reel, segments)
    total = timeline_total(segments)
    reel_id = reel.get("id") or "reel"
    return [
        {
            **block,
            "id": f"cap-{reel_id}-{idx}-{int(block['start_time_seconds'] * 1000)}",
        }
        for idx, block in enumerate(make_caption_blocks(words, total))
    ]


DEFAULT_EXPORT_CANVAS: dict[str, Any] = {
    "aspectRatio": "9:16",
    "fit": "cover",
    "zoom": 1.0,
    "panX": 0,
    "panY": 0,
    "cropX": 0.5,
    "cropY": 0.5,
    "captionStyle": "karaoke",
    "captionSize": 135,
    "captionX": 50,
    "captionY": 86,
    "captionWordGap": 0.34,
    "hideFillersInSubtitles": False,
    "cutFillersFromVideo": False,
    "cutSilences": False,
}
