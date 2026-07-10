"""Helpers to pull verbatim words from Rev.ai-aligned word lists for time windows."""


def verbatim_from_words(
    words: list,
    t_start: float,
    t_end: float,
    *,
    max_chars: int = 4000,
) -> str:
    """Return spaced words whose timestamps overlap [t_start, t_end] on the master timeline."""
    if not words:
        return ""
    try:
        lo = float(t_start)
        hi = float(t_end)
    except (TypeError, ValueError):
        return ""
    lo, hi = min(lo, hi), max(lo, hi)
    parts: list[str] = []
    for w in words:
        try:
            ws = float(w.get("start") or 0.0)
            we = float(w.get("end") or ws)
        except (TypeError, ValueError):
            continue
        # Window is [lo, hi) — exclusive on both sides. `ws >= hi` excludes a
        # word starting exactly at the window's end; `we <= lo` (not `<`)
        # excludes the mirror case, a word ending exactly where this window
        # starts. Both matter when a same-speaker segment sits with zero gap
        # against a different speaker's segment on either side (a real
        # interruption/overlap in the source audio) — same boundary issue
        # fixed in analyzer.py's _attach_words.
        if we <= lo or ws >= hi:
            continue
        token = str(w.get("word") or "").strip()
        if token:
            parts.append(token)
    text = " ".join(parts).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text
