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
        if we < lo or ws > hi:
            continue
        token = str(w.get("word") or "").strip()
        if token:
            parts.append(token)
    text = " ".join(parts).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text
