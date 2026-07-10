"""LLM transcript cleanup that fixes STT spelling/mishears WITHOUT changing timing.

Corrections are strictly 1:1 token substitutions keyed by word index, so every
word keeps its original start/end time (karaoke stays in sync). Words are never
added, removed, split, or merged here.
"""
from __future__ import annotations

import json
import re
import time

import anthropic
from anthropic import Anthropic

CHUNK_TOKENS = 220
CONTEXT_TOKENS = 25

# A transient network blip (or an Anthropic-side "Overloaded" 529, common
# under load) shouldn't silently skip a whole chunk's worth of corrections —
# retry those specifically before giving up on the chunk. Status-code check
# against the public APIStatusError base class (rather than naming individual
# subclasses like OverloadedError, which isn't even part of the public
# `anthropic` namespace) so new transient-error subclasses are caught without
# code changes — see the matching helper in src/analyzer.py.
_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
_CHUNK_RETRY_ATTEMPTS = 4
_CHUNK_RETRY_DELAY = 2.0
_CHUNK_RETRY_MAX_DELAY = 20.0
# Bounds a stalled request so it surfaces as a retryable APITimeoutError
# instead of hanging indefinitely — see the matching constant/comment in
# src/analyzer.py. Chunks here are small (max_tokens=2000), so a much
# shorter bound than the reel-selection call is still generous.
_CLIENT_TIMEOUT_SECONDS = 120.0


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    return isinstance(exc, anthropic.APIConnectionError)  # covers APITimeoutError too (subclass)

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


def _fetch_chunk_fixes(
    client: Anthropic,
    out: list[dict],
    start: int,
    end: int,
    ctx_lo: int,
    ctx_hi: int,
    model: str,
    hint: str,
    progress_cb=None,
    max_retries: int | None = None,
    raise_on_failure: bool = False,
) -> list[dict]:
    numbered = "\n".join(f"{i}: {out[i].get('word','')}" for i in range(ctx_lo, ctx_hi))
    user = (
        f"{hint}Correct ONLY indices {start}..{end - 1} (surrounding lines are context).\n\n"
        f"{numbered}"
    )
    retries = _CHUNK_RETRY_ATTEMPTS if max_retries is None else max_retries
    fixes: list[dict] = []
    last_exc: Exception | None = None
    for retry in range(retries + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=2000,
                system=_CLEAN_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
            fixes = _parse_json(raw).get("fixes") or []
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 - non-retryable path falls through below
            if not _is_retryable(exc):
                last_exc = exc
                break
            last_exc = exc
            if retry < retries:
                delay = min(_CHUNK_RETRY_MAX_DELAY, _CHUNK_RETRY_DELAY * (retry + 1))
                if progress_cb:
                    progress_cb(f"transcript cleanup chunk {start}-{end}: {exc}; retrying in {delay:.0f}s...")
                time.sleep(delay)
    if last_exc is not None:
        if raise_on_failure:
            raise last_exc
        if progress_cb:
            progress_cb(f"transcript cleanup chunk {start}-{end} skipped: {last_exc}")
        return []
    return fixes


def correct_transcript_words_step(
    words: list[dict],
    start: int,
    *,
    model: str,
    api_key: str,
    speaker_name: str = "",
    client: Anthropic | None = None,
    progress_cb=None,
    max_retries: int | None = None,
    raise_on_failure: bool = False,
) -> tuple[list[dict], int, bool]:
    """Correct exactly ONE chunk (CHUNK_TOKENS words) starting at `start`.

    Returns (updated_words, next_start, done). Bounded to a single Claude call
    (plus its own retries) so a caller that can't hold a process open across
    the whole transcript — a serverless request with a hard execution-time
    limit — can make one poll do one chunk and resume from `next_start` on the
    next poll. `words` is expected to already carry any earlier chunks' fixes;
    it's copied and returned so callers can just re-save the result.

    `max_retries`/`raise_on_failure` let a caller with its own hard wall-clock
    budget (a serverless request) opt out of the default blocking, sleep-based
    in-process retry loop — e.g. `max_retries=0, raise_on_failure=True` makes
    this a single bounded attempt whose failure the caller can retry across
    separate polls instead of within one request.
    """
    out = [dict(w) for w in words]
    n = len(out)
    if n == 0 or start >= n:
        return out, n, True

    if client is None:
        # Same request timeout as the looping path — the serverless step driver
        # passes client=None and creates one here per chunk.
        client = Anthropic(api_key=api_key, timeout=_CLIENT_TIMEOUT_SECONDS)
    hint = f"Speaker name (spell exactly like this when it appears): {speaker_name}\n\n" if speaker_name else ""

    end = min(n, start + CHUNK_TOKENS)
    ctx_lo = max(0, start - CONTEXT_TOKENS)
    ctx_hi = min(n, end + CONTEXT_TOKENS)
    fixes = _fetch_chunk_fixes(
        client, out, start, end, ctx_lo, ctx_hi, model, hint, progress_cb,
        max_retries=max_retries, raise_on_failure=raise_on_failure,
    )

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

    return out, end, end >= n


def correct_transcript_words(
    words: list[dict],
    *,
    model: str,
    api_key: str,
    progress_cb=None,
    speaker_name: str = "",
) -> tuple[list[dict], int]:
    """Return (corrected_words, num_fixes). Timing/indices preserved exactly.

    Loops correct_transcript_words_step to completion — fine for a caller
    that can hold the process open for the whole transcript (a background
    thread; see backend/app.py). Serverless callers instead drive the step
    function directly, one poll per chunk — see backend/app_serverless.py.
    """
    if not words:
        return words, 0

    # Merge resolution: keep their step-loop refactor (the per-chunk hint and
    # fix-counting now live in correct_transcript_words_step) and add our
    # request timeout so a stalled cleanup chunk surfaces as a retryable
    # APITimeoutError instead of hanging.
    client = Anthropic(api_key=api_key, timeout=_CLIENT_TIMEOUT_SECONDS)
    out = list(words)
    start = 0
    while start < len(out):
        out, start, _done = correct_transcript_words_step(
            out,
            start,
            model=model,
            api_key=api_key,
            speaker_name=speaker_name,
            client=client,
            progress_cb=progress_cb,
        )

    total_fixes = sum(1 for a, b in zip(words, out) if a.get("word") != b.get("word"))
    if progress_cb:
        progress_cb(f"transcript cleanup: {total_fixes} token fix(es)")
    return out, total_fixes
