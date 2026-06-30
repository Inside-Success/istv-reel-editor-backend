"""
Word-accurate cutter — resolves reel cuts from sentence segment ids ONLY.

Never trusts model-emitted decimal times. Segment boundaries are sentence-bounded;
pads are applied against adjacent source words.
"""

from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

LEAD_PAD_SECONDS = 0.10
TAIL_PAD_SECONDS = 0.45
NATURAL_TAIL_PAUSE = 0.28
CONTINUATION_GAP_SEC = 0.42
NEXT_WORD_GUARD_SEC = 0.03
MAX_REEL_SECONDS = 60.0
MIN_REEL_SECONDS = 15.0
MAX_REEL_SPANS = 5
MAX_CONTEXT_BRIDGE_SEGMENTS = 4
MAX_WORD_CONTINUATION = 6
MAX_CONTEXT_HEAD_PREPENDS = 2

# Extra seconds the cutter may exceed the target max so endings land on a
# complete thought instead of a hard mid-sentence cut (soft duration window).
REEL_END_TOLERANCE_SECONDS = 5.0


def reel_max_seconds() -> float:
    """Target max reel length (env REEL_MAX_SECONDS overrides the default cap)."""
    try:
        return float(os.getenv("REEL_MAX_SECONDS") or MAX_REEL_SECONDS)
    except (TypeError, ValueError):
        return MAX_REEL_SECONDS


def reel_min_seconds() -> float:
    """Target min reel length (env REEL_MIN_SECONDS overrides the default floor)."""
    try:
        return float(os.getenv("REEL_MIN_SECONDS") or MIN_REEL_SECONDS)
    except (TypeError, ValueError):
        return MIN_REEL_SECONDS


def context_aware_enabled() -> bool:
    """Whether to auto-pull setup sentences so reels open self-contained.

    Default ON. Set REEL_CONTEXT_AWARE=0 for the legacy/baseline behavior.
    """
    return str(os.getenv("REEL_CONTEXT_AWARE", "1")).strip().lower() not in (
        "0", "false", "no", "off",
    )


def profile_is_v2() -> bool:
    """v2 profile enables stricter hook/ending hardening (v1 stays unchanged)."""
    return str(os.getenv("REEL_PROFILE", "")).strip().lower() in ("v2", "2", "updated_v2")


# v2-only: extra opener tokens that signal the line starts mid-sentence (no subject).
V2_EXTRA_OPENER_WORDS = frozenset({
    "was", "were", "is", "are", "got", "get", "gets", "spent", "went", "gone",
    "came", "come", "said", "had", "has", "did", "does", "made", "took", "began",
    "started", "kept", "felt", "knew", "saw", "thought", "wanted", "needed",
    "because", "since", "until", "unless", "whereas",
})

# v2-only: extra dangling end tokens (contractions / filler tails missed by v1).
V2_EXTRA_DANGLING_WORDS = frozenset({
    "whos", "thats", "theres", "heres", "wheres", "im", "ive", "id", "ill",
    "youre", "youve", "well", "weve", "theyre", "theyve", "hes", "shes", "its",
    "gonna", "wanna", "kinda", "sorta", "say", "mean", "guess", "think",
})

V2_EXTRA_DANGLING_PHRASES = (
    "i would say",
    "you know",
    "i guess",
    "i mean",
    "i think",
    "or something",
    "and stuff",
    "things like that",
    "stuff like that",
)

# Opener tokens that signal the first sentence depends on earlier, un-included context.
# Connectors (the thought started before) + 3rd-person/demonstrative references
# (who/what they point to was introduced in a prior sentence).
CONTEXT_OPENER_WORDS = frozenset({
    "and", "but", "so", "because", "which", "who", "or", "nor", "yet",
    "then", "plus", "anyway", "however", "therefore", "thus", "also",
    "besides", "meanwhile", "instead", "otherwise", "still", "though",
    "she", "he", "they", "them", "her", "him", "his", "their", "theirs",
    "it", "its", "this", "that", "these", "those",
})

# First/second person and openers that are fine to lead a standalone reel.
SELF_CONTAINED_OPENERS = frozenset({
    "i", "we", "you", "my", "our", "your", "let", "let's", "here", "there",
    "when", "what", "why", "how", "where", "if", "imagine", "picture", "everyone",
    "everybody", "nobody", "people", "most", "many",
})

DANGLING_END_WORDS = frozenset({
    "and", "or", "but", "so", "to", "the", "a", "an", "of", "in", "on", "at",
    "with", "for", "that", "which", "who", "when", "where", "as", "if", "because",
    "yeah", "um", "uh", "like", "you", "know",
    "my", "your", "their", "our", "his", "her", "its", "this", "that", "these", "those",
    "into", "from", "about", "through", "during", "before", "after", "between",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "will", "would", "could", "should", "can", "may", "might", "must",
    "do", "does", "did", "not", "just", "very", "really", "also", "then", "while",
    "even", "still", "more", "some", "any", "every", "each", "both", "all", "only",
    "stuck", "focused",
    "i", "me", "we", "they", "them", "he", "she", "it", "or",
})

DANGLING_END_PHRASES = (
    "i think that",
    "a lot of that",
    "and i think that",
    "so i take a lot of that",
    "she was definitely",
    "but yeah",
    "i felt",
    "i feel",
    "i was",
    "i am",
    "our mission is easy",
    "those barriers",
    "i knew this app",
    "the doctors are signing notes saying",
    "into my",
    "years into my",
    "in the middle of my",
    "too focused in the past or stuck",
    "trying to grieve",
)

# Words that can end a reel even without terminal punctuation.
STRONG_END_WORDS = frozenset({
    "present", "today", "done", "everything", "right", "true", "success", "career",
    "life", "world", "home", "love", "hope", "peace", "freedom", "power", "change",
    "possible", "enough", "forever", "always", "never", "here", "now", "yes", "no",
    "depressions", "depression", "anxiety", "father", "mother", "parents",
})

# Trailing words that signal the speaker is mid-thought / voice still rising.
INCOMPLETE_END_WORDS = frozenset({
    "stuck", "going", "trying", "wondering", "feeling", "thinking", "leaving",
    "building", "starting", "becoming", "learning", "growing", "waiting", "hoping",
    "working", "living", "moving", "looking", "talking", "saying", "meaning",
})


def normalize_word_timings(words: list[dict]) -> list[dict]:
    """Ensure every word has start and end."""
    if not words:
        return []

    out: list[dict] = []
    for i, raw in enumerate(words):
        start = _float(raw.get("start"), 0.0)
        end = _float(raw.get("end"), 0.0)
        if end <= start:
            if i + 1 < len(words):
                next_start = _float(words[i + 1].get("start"), start)
                end = max(start + 0.05, next_start - 0.02)
            else:
                end = start + 0.25
        out.append({**raw, "index": raw.get("index", i), "start": start, "end": end})
    return out


def normalize_order_mode(value) -> str:
    mode = str(value or "chronological").strip().lower()
    if mode in ("hook_pull", "hook-pull", "hookpull"):
        return "hook_pull"
    return "chronological"


def extract_reel_segment_ids(reel: dict) -> list[int]:
    """Pull segment ids from new schema or legacy nested segments."""
    raw = reel.get("segment_ids")
    if isinstance(raw, list) and raw:
        return [_int_id(x) for x in raw if _int_id(x) is not None]

    ids: list[int] = []
    nested = reel.get("segments") or []
    if isinstance(nested, list):
        order_mode = normalize_order_mode(reel.get("order_mode") or reel.get("assembly_mode"))
        specs = sorted(
            [s for s in nested if isinstance(s, dict)],
            key=lambda s: int(s.get("order") or 0) if order_mode == "hook_pull" else _int_id(s.get("segment_ids", [0])[0]) or 0,
        )
        for spec in specs:
            for value in spec.get("segment_ids") or []:
                sid = _int_id(value)
                if sid is not None:
                    ids.append(sid)

    if not ids:
        first_id = _int_id(reel.get("first_segment_id"))
        last_id = _int_id(reel.get("last_segment_id"))
        if first_id is not None and last_id is not None and last_id >= first_id:
            ids = list(range(first_id, last_id + 1))

    return ids


def _dedupe_repeated_segments(segment_ids: list[int], seg_by_id: dict[int, dict]) -> list[int]:
    """v2: drop near-duplicate sentences within a reel (tightens pacing/coherence).

    First and last segments are always kept so hooks and payoffs are never dropped.
    """
    if len(segment_ids) <= 2:
        return list(segment_ids)

    head, tail = segment_ids[0], segment_ids[-1]
    middle = segment_ids[1:-1]
    kept: list[int] = []
    kept_texts: list[str] = []
    for sid in middle:
        seg = seg_by_id.get(sid)
        text = re.sub(r"[^a-z0-9 ]+", "", str(seg.get("text") or "").lower()).strip() if seg else ""
        if text and len(text) >= 12:
            if any(SequenceMatcher(None, text, prev).ratio() >= 0.85 for prev in kept_texts):
                continue
            kept_texts.append(text)
        kept.append(sid)
    return [head, *kept, tail]


def _next_segment_after(tail_id: int, seg_by_id: dict[int, dict]) -> int | None:
    """Next sentence segment in transcript order (not only tail_id + 1)."""
    ordered = sorted(seg_by_id.keys())
    try:
        idx = ordered.index(tail_id)
    except ValueError:
        fallback = tail_id + 1
        return fallback if fallback in seg_by_id else None
    if idx + 1 < len(ordered):
        return ordered[idx + 1]
    return None


def _fill_context_bridges(
    segment_ids: list[int],
    seg_by_id: dict[int, dict],
    max_len: float,
) -> list[int]:
    """Insert short skipped segments between picks so stitched reels keep viewer context."""
    if len(segment_ids) < 2:
        return list(segment_ids)

    ids = list(segment_ids)
    filled: list[int] = [ids[0]]
    for sid in ids[1:]:
        prev = filled[-1]
        if sid <= prev:
            if sid not in filled:
                filled.append(sid)
            continue
        gap_ids = [g for g in range(prev + 1, sid) if g in seg_by_id]
        if gap_ids and len(gap_ids) <= MAX_CONTEXT_BRIDGE_SEGMENTS:
            trial = filled + gap_ids + [sid]
            if _ids_total_duration(trial, seg_by_id) <= max_len + 0.01:
                filled.extend(gap_ids)
            else:
                for bridge_id in gap_ids:
                    trial_bridge = filled + [bridge_id]
                    rest = [x for x in ids[ids.index(sid):] if x not in trial_bridge]
                    if _ids_total_duration(trial_bridge + rest, seg_by_id) <= max_len + 0.01:
                        filled.append(bridge_id)
        if filled[-1] != sid:
            filled.append(sid)
    return filled


def _prev_segment_before(head_id: int, seg_by_id: dict[int, dict]) -> int | None:
    """Previous sentence segment in transcript order (not only head_id - 1)."""
    ordered = sorted(seg_by_id.keys())
    try:
        idx = ordered.index(head_id)
    except ValueError:
        fallback = head_id - 1
        return fallback if fallback in seg_by_id else None
    if idx - 1 >= 0:
        return ordered[idx - 1]
    return None


def _segment_needs_context_head(text: str) -> bool:
    """True when the opening sentence relies on context from an earlier, un-included sentence."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    tokens = [_clean_token(t) for t in cleaned.split() if _clean_token(t)]
    if not tokens:
        return False
    first = tokens[0]
    if first in SELF_CONTAINED_OPENERS:
        return False
    if first in CONTEXT_OPENER_WORDS:
        return True
    if profile_is_v2() and first in V2_EXTRA_OPENER_WORDS:
        return True
    return False


def _provides_context(prev_seg: dict | None) -> bool:
    """A prior sentence supplies context only if it itself stands on its own."""
    if not prev_seg:
        return False
    text = str(prev_seg.get("text") or "").strip()
    if not text:
        return False
    # If the candidate setup line is itself a dependent opener, it doesn't anchor context.
    return True


def _extend_context_head_ids(
    segment_ids: list[int],
    seg_by_id: dict[int, dict],
    max_len: float,
) -> list[int]:
    """Prepend setup sentence(s) when the reel OPENS on an unresolved reference.

    Mirror of the tail extension: keeps reels self-contained (context-aware) so a
    cold viewer understands who/what is being discussed from the first line.
    """
    ids = list(segment_ids)
    if not ids:
        return ids

    prepends = 0
    while ids and prepends < MAX_CONTEXT_HEAD_PREPENDS:
        head_id = _contiguous_runs(ids)[0][0]
        head_seg = seg_by_id.get(head_id)
        if not head_seg:
            break
        if not _segment_needs_context_head(str(head_seg.get("text") or "")):
            break
        prev_id = _prev_segment_before(head_id, seg_by_id)
        if prev_id is None or prev_id in ids:
            break
        prev_seg = seg_by_id.get(prev_id)
        if not _provides_context(prev_seg):
            break
        trial = [prev_id] + ids
        if _ids_total_duration(trial, seg_by_id) > max_len + 0.01:
            break
        ids = trial
        prepends += 1
        # Stop once the new opener no longer depends on earlier context.
        if not _segment_needs_context_head(str(prev_seg.get("text") or "")):
            break

    return ids


def _extend_resolved_tail_ids(
    segment_ids: list[int],
    seg_by_id: dict[int, dict],
    max_len: float,
) -> list[int]:
    """Append the next sentence segment(s) when the reel ends on an incomplete thought."""
    ids = list(segment_ids)
    if not ids:
        return ids

    extensions = 0
    while ids and extensions < 3:
        tail_id = _last_run_tail_id(ids)
        if tail_id is None:
            break
        last_seg = seg_by_id.get(tail_id)
        if not last_seg:
            break
        last_text = str(last_seg.get("text") or "")
        if _segment_has_emotional_landing(last_text):
            break
        if not _segment_text_dangles(last_text):
            break
        next_id = _next_segment_after(tail_id, seg_by_id)
        if next_id is None or next_id in ids:
            break
        next_seg = seg_by_id.get(next_id)
        if not next_seg or _is_weak_continuation_segment(next_seg):
            break
        trial = ids + [next_id]
        if _ids_total_duration(trial, seg_by_id) > max_len + 0.01:
            break
        ids = trial
        extensions += 1
        if _tail_segment_resolves_bridge(last_seg, next_seg):
            break

    return ids


def _is_weak_continuation_segment(seg: dict) -> bool:
    """Filler/setup lines that do not resolve a cut — skip when extending endings."""
    text = str(seg.get("text") or "").strip().lower()
    if not text:
        return True
    weak_starts = (
        "i would say",
        "i mean",
        "you know",
        "and so",
        "but yeah",
        "so yeah",
        "like i",
        "and i mean",
    )
    return any(text.startswith(prefix) for prefix in weak_starts)


def _segment_has_emotional_landing(text: str) -> bool:
    """Detect when a segment already lands an emotional beat — stop extending."""
    lower = str(text or "").strip().lower()
    if not lower:
        return False
    landings = (
        "it was hard",
        "i had to leave",
        "at this point it was hard",
        "i went home",
        "i lost my",
        "both parents",
        "you have depressions",
        "you have to be present",
    )
    return any(phrase in lower[-55:] for phrase in landings)


def _tail_segment_resolves_bridge(prev_seg: dict, next_seg: dict) -> bool:
    """True when the next segment completes a phrase split across the boundary."""
    last_text = str(prev_seg.get("text") or "").strip()
    next_text = str(next_seg.get("text") or "").strip()
    if not last_text or not next_text:
        return False
    combined = f"{last_text} {next_text}".lower()
    bridge_phrases = (
        "into my career",
        "my career",
        "years into my career",
        "in the middle of my career",
        "had to leave",
    )
    if any(phrase in combined for phrase in bridge_phrases):
        return True
    first = _clean_token(next_text.split()[0])
    return first in STRONG_END_WORDS


def _last_run_tail_id(segment_ids: list[int]) -> int | None:
    runs = _contiguous_runs(segment_ids)
    if not runs:
        return None
    return runs[-1][-1]


def build_reel_cut_sheet(
    words: list[dict],
    reel: dict,
    sentence_segments: list[dict],
    *,
    video_duration: float = 0.0,
    protect_ends: bool = False,
    max_len: float | None = None,
) -> list[dict]:
    """
    Build editor_cut_sheet rows from whole sentence segment ids.
    Splits non-contiguous ids into separate spans (stitch / hook-pull).
    """
    cap = max_len if max_len is not None else reel_max_seconds()
    # Soft window: allow a few extra seconds so the reel ends on a finished thought.
    soft_cap = cap + REEL_END_TOLERANCE_SECONDS
    seg_by_id = {int(s["id"]): s for s in sentence_segments if s.get("id") is not None}
    order_mode = normalize_order_mode(reel.get("order_mode") or reel.get("assembly_mode"))
    segment_ids = extract_reel_segment_ids(reel)

    if not segment_ids or not seg_by_id:
        return []

    if profile_is_v2():
        segment_ids = _dedupe_repeated_segments(segment_ids, seg_by_id)

    if order_mode == "chronological":
        segment_ids = sorted(segment_ids)
        if context_aware_enabled():
            segment_ids = _extend_context_head_ids(segment_ids, seg_by_id, soft_cap)

    segment_ids = _fill_context_bridges(segment_ids, seg_by_id, cap)
    segment_ids = _extend_resolved_tail_ids(segment_ids, seg_by_id, soft_cap)
    segment_ids = _cap_segment_ids(segment_ids, seg_by_id, soft_cap, protect_ends=protect_ends)
    segment_ids = _extend_resolved_tail_ids(segment_ids, seg_by_id, soft_cap)
    segment_ids = _trim_dangling_tail_ids(segment_ids, seg_by_id)
    reel["segment_ids"] = segment_ids

    runs = _contiguous_runs(segment_ids)
    rows: list[dict] = []
    for run_ids in runs[:MAX_REEL_SPANS]:
        row = _resolve_run(words, run_ids, seg_by_id, video_duration)
        if row:
            rows.append(row)

    rows, trimmed = _cap_reel_duration(
        rows,
        segment_ids,
        seg_by_id,
        words,
        video_duration,
        max_len=soft_cap,
        protect_ends=protect_ends,
    )
    if trimmed:
        logger.warning(
            "Reel %s exceeded %.0fs — trimmed segment(s) to fit",
            reel.get("id") or reel.get("rank") or "?",
            cap,
        )

    for idx, row in enumerate(rows):
        if idx == 0:
            role, label = "HOOK", "HOOK"
        elif idx == len(rows) - 1 and len(rows) > 1:
            role, label = "PAYOFF", "PAYOFF"
        else:
            role, label = "BODY", f"BODY {idx}"
        row["order"] = idx + 1
        row["role"] = role
        row["label"] = label

    return rows


def resolve_reel(
    reel: dict,
    segments: list[dict],
    words: list[dict],
    duration: float,
    *,
    max_len: float = MAX_REEL_SECONDS,
    lead: float = LEAD_PAD_SECONDS,
    tail: float = TAIL_PAD_SECONDS,
    protect_ends: bool = False,
) -> tuple[list[dict], list[dict], float, list[int]]:
    """
    Resolve one reel to cut sheet rows. Returns (cut_sheet, timestamped_words, duration, segment_ids).
    timestamped_words is left empty — attach downstream via analyzer._attach_words.
    """
    del lead, tail
    words = normalize_word_timings(words)
    sheet = build_reel_cut_sheet(
        words,
        reel,
        segments,
        video_duration=duration,
        protect_ends=protect_ends,
        max_len=max_len,
    )
    ids = list(reel.get("segment_ids") or extract_reel_segment_ids(reel))
    dur = _total_duration(sheet)
    return sheet, [], dur, ids


def resolve_brand_row_bounds(
    words: list[dict],
    row: dict,
    sentence_segments: list[dict],
    *,
    video_duration: float = 0.0,
) -> tuple[float, float]:
    """Brand story rows may still carry approximate times — snap to overlapping segments."""
    seg_by_id = {int(s["id"]): s for s in sentence_segments if s.get("id") is not None}
    a = _float(row.get("start_time_seconds"))
    b = _float(row.get("end_time_seconds"))
    overlapping = [
        int(s["id"])
        for s in sentence_segments
        if _float(s.get("end")) >= a and _float(s.get("start")) <= b
    ]
    if overlapping:
        run = _resolve_run(words, overlapping, seg_by_id, video_duration)
        if run:
            return _float(run["start_time_seconds"]), _float(run["end_time_seconds"])
    return a, b


def _resolve_run(
    words: list[dict],
    run_ids: list[int],
    seg_by_id: dict[int, dict],
    video_duration: float,
) -> dict | None:
    segs = [seg_by_id[i] for i in run_ids if i in seg_by_id]
    if not segs:
        return None

    span_start = float(segs[0]["start"])
    span_end = float(segs[-1]["end"])
    wi_start = int(segs[0].get("word_start_index", segs[0].get("first_word_index", 0)))
    wi_end = int(segs[-1].get("word_end_index", segs[-1].get("last_word_index", wi_start)))

    words_by_index = {int(w.get("index", i)): w for i, w in enumerate(words)}
    prev_word = words_by_index.get(wi_start - 1)
    next_word = words_by_index.get(wi_end + 1)

    start = max(
        span_start - LEAD_PAD_SECONDS,
        _float(prev_word.get("end")) if prev_word else 0.0,
        0.0,
    )
    duration = video_duration if video_duration > 0 else span_end + TAIL_PAD_SECONDS
    next_start = _float(next_word.get("start")) if next_word else duration
    # Hard ceiling: never let the natural tail pad spill into the next sentence's first word.
    ceiling = (next_start - NEXT_WORD_GUARD_SEC) if next_word else duration
    ceiling = min(ceiling, duration)
    end = min(span_end + TAIL_PAD_SECONDS, ceiling)
    last_word = words_by_index.get(wi_end)
    if last_word:
        # Breath after the last selected word, but stop before the next word begins.
        end = max(end, min(_float(last_word.get("end")) + NATURAL_TAIL_PAUSE, ceiling))
        # Deliberate continuation may cross the boundary only when the phrase truly continues
        # (e.g. "into my" -> "career"); it has its own gap/continuation checks.
        end = _extend_end_through_continuation_words(
            words_by_index,
            wi_end,
            end,
            duration,
        )
        # Trim only a trailing connector pulled in PAST the selection — never truncate inside it.
        end = _clamp_end_to_emotional_landing(words_by_index, wi_start, wi_end, end)
    if end <= start:
        end = min(start + 0.25, duration)

    verbatim = " ".join(str(s.get("text") or "").strip() for s in segs).strip()
    return {
        "segment_ids": [int(s["id"]) for s in segs],
        "start_time_seconds": start,
        "end_time_seconds": end,
        "first_word_index": wi_start,
        "last_word_index": wi_end,
        "note": verbatim[:500],
    }


def _contiguous_runs(segment_ids: list[int]) -> list[list[int]]:
    if not segment_ids:
        return []
    runs: list[list[int]] = [[segment_ids[0]]]
    for sid in segment_ids[1:]:
        if sid == runs[-1][-1] + 1:
            runs[-1].append(sid)
        else:
            runs.append([sid])
    return runs


def _segment_duration(sid: int, seg_by_id: dict[int, dict]) -> float:
    seg = seg_by_id.get(sid)
    if not seg:
        return 0.0
    return max(0.0, _float(seg.get("end")) - _float(seg.get("start")))


def _ids_total_duration(ids: list[int], seg_by_id: dict[int, dict]) -> float:
    return sum(_segment_duration(sid, seg_by_id) for sid in ids)


def _cap_segment_ids(
    segment_ids: list[int],
    seg_by_id: dict[int, dict],
    max_len: float,
    *,
    protect_ends: bool = False,
) -> list[int]:
    """Trim segment id list to fit max_len; optionally protect hook + payoff ends."""
    ids = list(segment_ids)

    if protect_ends:
        while len(ids) > 2 and _ids_total_duration(ids, seg_by_id) > max_len + 0.01:
            interior = ids[1:-1]
            if not interior:
                break
            longest = max(interior, key=lambda s: _segment_duration(s, seg_by_id))
            ids.remove(longest)

    while len(ids) > 1 and _ids_total_duration(ids, seg_by_id) > max_len + 0.01:
        ids.pop()

    return ids


def _trim_dangling_tail_ids(segment_ids: list[int], seg_by_id: dict[int, dict]) -> list[int]:
    ids = list(segment_ids)
    while ids:
        last = seg_by_id.get(ids[-1])
        if not last:
            break
        text = str(last.get("text") or "")
        if _segment_has_emotional_landing(text):
            break
        if not _segment_text_dangles(text):
            break
        if len(ids) >= 2:
            prev = seg_by_id.get(ids[-2])
            if prev and _tail_segment_resolves_bridge(prev, last):
                break
        ids.pop()
    return ids


def _clamp_end_to_emotional_landing(
    words_by_index: dict[int, dict],
    wi_start: int,
    wi_end: int,
    end: float,
) -> float:
    """Trim a trailing connector word pulled in PAST the selected segment.

    The cut must always include the full selected content, so the floor is the last
    selected word's natural end — this never truncates inside the selection. Only
    words beyond ``wi_end`` (added by tail pad / continuation) are eligible for trimming.
    """
    del wi_start
    last_sel = words_by_index.get(wi_end)
    floor = _float(last_sel.get("end")) if last_sel else 0.0
    if end <= floor:
        return end

    # Find the last word that falls inside the current (floor, end] window.
    idx = wi_end + 1
    last_in: dict | None = None
    while True:
        word = words_by_index.get(idx)
        if not word or _float(word.get("start")) >= end:
            break
        last_in = word
        idx += 1

    if last_in is not None:
        tok = _clean_token(str(last_in.get("word") or ""))
        ends_clean = str(last_in.get("word") or "").rstrip()[-1:] in ".!?"
        if not ends_clean and (tok in DANGLING_END_WORDS or tok in INCOMPLETE_END_WORDS):
            return max(floor + NATURAL_TAIL_PAUSE, _float(last_in.get("start")) - NEXT_WORD_GUARD_SEC)
    return end


def _extend_end_through_continuation_words(
    words_by_index: dict[int, dict],
    wi_end: int,
    end: float,
    video_duration: float,
) -> float:
    """
    Extend a cut through tightly-coupled following words when the speaker is
    mid-phrase (e.g. 'into my' -> 'career') or the voice has not yet landed.
    """
    extended = end
    idx = wi_end
    for _ in range(MAX_WORD_CONTINUATION):
        cur = words_by_index.get(idx)
        nxt = words_by_index.get(idx + 1)
        if not cur or not nxt:
            break
        cur_tok = _clean_token(str(cur.get("word") or ""))
        nxt_tok = _clean_token(str(nxt.get("word") or ""))
        gap = _float(nxt.get("start")) - _float(cur.get("end"))
        if gap > CONTINUATION_GAP_SEC:
            break
        continues = (
            cur_tok in DANGLING_END_WORDS
            or cur_tok in INCOMPLETE_END_WORDS
            or _word_pair_continues(cur, nxt)
        )
        if not continues:
            break
        nxt_end = _float(nxt.get("end"))
        extended = min(max(extended, nxt_end + NATURAL_TAIL_PAUSE), video_duration)
        idx += 1
        if nxt_tok in STRONG_END_WORDS:
            break
        if str(nxt.get("word") or "").rstrip()[-1:] in ".!?":
            break
    return extended


def _word_pair_continues(cur: dict, nxt: dict) -> bool:
    """Detect split phrases across Rev.ai word boundaries."""
    cur_raw = str(cur.get("word") or "").strip().lower()
    nxt_raw = str(nxt.get("word") or "").strip().lower()
    pair = f"{_clean_token(cur_raw)} {_clean_token(nxt_raw)}"
    split_phrases = (
        "into my",
        "my career",
        "years into",
        "one year",
        "to leave",
        "had to",
        "in the",
        "of my",
        "or stuck",
    )
    return any(pair.endswith(phrase) or phrase in pair for phrase in split_phrases)


def _segment_text_dangles(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    if cleaned[-1] in ".!?":
        return False
    tokens = [_clean_token(t) for t in cleaned.split()]
    if not tokens:
        return True
    last = tokens[-1]
    if last in STRONG_END_WORDS:
        return False
    if last in DANGLING_END_WORDS or last in INCOMPLETE_END_WORDS:
        return True
    lower = cleaned.lower()
    if any(lower.endswith(phrase) for phrase in DANGLING_END_PHRASES):
        return True
    if profile_is_v2():
        if last in V2_EXTRA_DANGLING_WORDS:
            return True
        if any(lower.endswith(phrase) for phrase in V2_EXTRA_DANGLING_PHRASES):
            return True
    return False


def _cap_reel_duration(
    rows: list[dict],
    segment_ids: list[int],
    seg_by_id: dict[int, dict],
    words: list[dict],
    video_duration: float,
    *,
    max_len: float = MAX_REEL_SECONDS,
    protect_ends: bool = False,
) -> tuple[list[dict], bool]:
    trimmed = False
    ids = list(segment_ids)

    while ids and _total_duration(rows) > max_len + 0.01:
        before = list(ids)
        ids = _cap_segment_ids(ids, seg_by_id, max_len, protect_ends=protect_ends)
        if ids != before:
            trimmed = True
        elif len(ids) > 1:
            ids.pop()
            trimmed = True
        else:
            break

        runs = _contiguous_runs(ids)[:MAX_REEL_SPANS]
        rows = []
        for run_ids in runs:
            row = _resolve_run(words, run_ids, seg_by_id, video_duration)
            if row:
                rows.append(row)

    return rows, trimmed


def _total_duration(rows: list[dict]) -> float:
    return sum(
        max(0.0, _float(r.get("end_time_seconds")) - _float(r.get("start_time_seconds")))
        for r in rows
    )


def _clean_token(value: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", str(value or "").lower())


def _float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_id(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
