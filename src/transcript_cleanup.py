"""LLM transcript cleanup that fixes STT spelling/mishears WITHOUT changing timing.

Corrections are strictly 1:1 token substitutions keyed by word index, so every
word keeps its original start/end time (karaoke stays in sync). Words are never
added, removed, split, or merged here.
"""
from __future__ import annotations

import json
import re

from anthropic import Anthropic

CHUNK_TOKENS = 220
CONTEXT_TOKENS = 25

_CLEAN_SYSTEM = """You fix automatic-speech-recognition (ASR) errors in an interview transcript.

You are given numbered tokens "i: word". Return ONLY tokens that are clearly MISHEARD
homophones or wrong words — things a listener would hear differently from what was spoken
(e.g. "height" -> "fight" when context is fight-or-flight, "often" -> "off", "Sali" -> "Salii").

DO NOT fix grammar, pluralization, or "correct" the speaker's natural speech.
If the audio likely said "loss" (singular), keep "loss" even if grammar sounds odd.
If unsure, leave the token unchanged.

HARD RULES:
- Each fix replaces ONE token with ONE token. Never add spaces, never split or merge.
- Do NOT paraphrase, censor, or rewrite style.
- Do NOT change filler words ("um", "uh").
- Do NOT change words that are merely grammatically awkward but phonetically correct.
- If nothing is a clear mishear, return an empty list.

Return ONLY valid JSON: {"fixes": [{"i": <index>, "w": "<corrected token>"}]}"""


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {"fixes": []}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {"fixes": []}


def correct_transcript_words(
    words: list[dict],
    *,
    model: str,
    api_key: str,
    progress_cb=None,
    speaker_name: str = "",
) -> tuple[list[dict], int]:
    """Return (corrected_words, num_fixes). Timing/indices preserved exactly."""
    if not words:
        return words, 0

    client = Anthropic(api_key=api_key)
    out = [dict(w) for w in words]
    n = len(out)
    total_fixes = 0
    hint = f"Speaker name (spell exactly like this when it appears): {speaker_name}\n\n" if speaker_name else ""

    start = 0
    while start < n:
        end = min(n, start + CHUNK_TOKENS)
        ctx_lo = max(0, start - CONTEXT_TOKENS)
        ctx_hi = min(n, end + CONTEXT_TOKENS)
        numbered = "\n".join(f"{i}: {out[i].get('word','')}" for i in range(ctx_lo, ctx_hi))
        user = (
            f"{hint}Correct ONLY indices {start}..{end - 1} (surrounding lines are context).\n\n"
            f"{numbered}"
        )
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=2000,
                system=_CLEAN_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
            fixes = _parse_json(raw).get("fixes") or []
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            if progress_cb:
                progress_cb(f"transcript cleanup chunk {start}-{end} skipped: {exc}")
            fixes = []

        for fix in fixes:
            try:
                i = int(fix.get("i"))
                w = str(fix.get("w") or "").strip()
            except (TypeError, ValueError):
                continue
            if not (start <= i < end):
                continue
            if not w or " " in w or "\t" in w:
                continue  # must stay a single token to preserve timing
            if w == str(out[i].get("word") or ""):
                continue
            out[i]["word"] = w
            total_fixes += 1

        start = end

    if progress_cb:
        progress_cb(f"transcript cleanup: {total_fixes} token fix(es)")
    return out, total_fixes
