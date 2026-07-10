"""Validate reel cut quality after the cutter resolves segment ids."""

from __future__ import annotations

import re

from src.cutter import (
    CONTEXT_OPENER_WORDS,
    MAX_REEL_SECONDS,
    SELF_CONTAINED_OPENERS,
    _segment_text_dangles,
)

LEAD_DEAD_AIR_THRESHOLD = 0.35


def validate_reel(reel: dict, *, max_len: float = MAX_REEL_SECONDS) -> list[str]:
    """Return a list of issue strings; empty list means OK."""
    issues: list[str] = []
    rank = reel.get("rank") or reel.get("id") or "?"

    dur = float(reel.get("duration_seconds") or reel.get("duration_sec") or 0)
    if dur > max_len + 0.5:
        issues.append(f"Reel {rank}: duration {dur:.1f}s exceeds {max_len:.0f}s cap")

    sheet = reel.get("editor_cut_sheet") or []
    if not sheet:
        issues.append(f"Reel {rank}: missing editor_cut_sheet")
        return issues

    words = reel.get("timestamped_words") or []
    if words:
        first_word_start = float(words[0].get("start") or words[0].get("time") or 0)
        reel_start = float(sheet[0].get("start_time_seconds") or 0)
        dead_air = first_word_start - reel_start
        if dead_air > LEAD_DEAD_AIR_THRESHOLD:
            issues.append(f"Reel {rank}: {dead_air:.2f}s dead air before first word")

    first_text = _first_spoken_text(reel, words)
    if first_text and _opening_needs_context(first_text):
        issues.append(f"Reel {rank}: unresolved context opening — '{first_text[:60]}'")

    last_text = _last_spoken_text(reel, words)
    if last_text and _segment_text_dangles(last_text):
        issues.append(f"Reel {rank}: dangling ending — '{last_text[-60:]}'")

    speaker_issue = _multi_speaker_issue(words)
    if speaker_issue:
        issues.append(f"Reel {rank}: {speaker_issue}")

    return issues


def _multi_speaker_issue(words: list[dict]) -> str:
    """Non-empty string if the reel's attached words span more than one speaker.

    Mirrors the "ONE SPEAKER ONLY" prompt rule and the mechanical filter in
    `_normalize_cut_sheets` (src/analyzer.py) — this is the independent,
    after-the-fact check: if either of those ever regresses, this is what
    would actually catch a multi-speaker reel making it into the output.
    """
    speakers = {int(w.get("speaker", 0) or 0) for w in words if isinstance(w, dict)}
    if len(speakers) <= 1:
        return ""
    return f"multiple speakers in one reel: {sorted(speakers)}"


def validate_analysis(analysis: dict, *, max_len: float = MAX_REEL_SECONDS) -> dict[str, list[str]]:
    """Validate all reels; returns {reel_rank: [issues]} for reels with problems."""
    out: dict[str, list[str]] = {}
    for reel in analysis.get("reels") or []:
        issues = validate_reel(reel, max_len=max_len)
        if issues:
            key = str(reel.get("rank") or reel.get("id") or len(out) + 1)
            out[key] = issues
    return out


def validate_summary(analysis: dict, *, max_len: float = MAX_REEL_SECONDS) -> str:
    """Human-readable summary for logs."""
    problems = validate_analysis(analysis, max_len=max_len)
    if not problems:
        return f"All {len(analysis.get('reels') or [])} reels OK"
    lines = [f"{len(problems)} reel(s) with issues:"]
    for rank, issues in sorted(problems.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        for issue in issues:
            lines.append(f"  - {issue}")
    return "\n".join(lines)


def _last_spoken_text(reel: dict, words: list[dict]) -> str:
    if words:
        tail = " ".join(str(w.get("word") or "") for w in words[-8:])
        return tail.strip()
    sheet = reel.get("editor_cut_sheet") or []
    if sheet:
        return str(sheet[-1].get("note") or "").strip()
    return str(reel.get("last_word") or "").strip()


def _first_spoken_text(reel: dict, words: list[dict]) -> str:
    if words:
        head = " ".join(str(w.get("word") or "") for w in words[:8])
        return head.strip()
    sheet = reel.get("editor_cut_sheet") or []
    if sheet:
        return str(sheet[0].get("note") or "").strip()
    return str(reel.get("first_word") or "").strip()


def _opening_needs_context(text: str) -> bool:
    """True when the reel opens on an unresolved reference (context lives elsewhere)."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    tokens = [re.sub(r"[^a-z0-9']+", "", t.lower()) for t in cleaned.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    first = tokens[0]
    if first in SELF_CONTAINED_OPENERS:
        return False
    return first in CONTEXT_OPENER_WORDS
