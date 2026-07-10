import json
import os
import re
import time

import anthropic
from anthropic import Anthropic
from src.transcription import fmt_time
from src.cutter import (
    MAX_REEL_SECONDS,
    NEXT_WORD_GUARD_SEC,
    REEL_END_TOLERANCE_SECONDS,
    REEL_FLOOR_TOLERANCE_SECONDS,
    build_reel_cut_sheet,
    extract_reel_segment_ids,
    normalize_order_mode,
    normalize_word_timings,
    reel_max_seconds,
    reel_min_seconds,
    resolve_brand_row_bounds,
    _segment_text_dangles,
)
from src.transcript_segments import build_sentence_segments, format_segments_for_claude
from src.transcript_snippets import verbatim_from_words
from src.marketing_doc import normalize_recommendations

# Project default: Claude Opus 4.8 only
CLAUDE_MODELS = {
    "claude-opus-4-8": "Claude Opus 4.8",
}

DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"

VALID_NUM_REELS = {3, 5, 10, 12, 15}
DEFAULT_NUM_REELS = 15
MIN_REEL_DURATION = 15.0
MAX_REEL_DURATION = MAX_REEL_SECONDS

# Errors worth retrying: transient network/server issues, and malformed JSON —
# Claude's sampling is non-deterministic, so a same-prompt retry often just works.
#
# Anthropic's SDK adds new *status-code-specific* subclasses of APIStatusError
# over time (e.g. OverloadedError for HTTP 529, "Overloaded" — extremely common
# under load with Opus) that are siblings of, not subclasses of,
# InternalServerError — and several of them (OverloadedError,
# ServiceUnavailableError, DeadlineExceededError) aren't even re-exported from
# the public `anthropic` namespace, only from the private `anthropic._exceptions`
# module. Naming individual classes is a losing game — a status-code check
# against the public APIStatusError base class catches all of them, present
# and future, without depending on SDK internals.
_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}

# Bounds a stalled streaming response (connection opens, then no bytes ever
# arrive) so it surfaces as `APITimeoutError` — retryable via
# `_is_retryable_claude_error` above — instead of hanging indefinitely. Found
# live: a reel-selection call over a long transcript stalled for 25+ minutes
# with the process still alive and no error, which from the app's
# perspective looks like the pipeline being permanently stuck with no
# recourse short of restarting the backend. 10 minutes is well above any
# real generation time at max_tokens=12000, so this only ever fires on an
# actual stall.
_CLAUDE_CLIENT_TIMEOUT_SECONDS = 600.0


class _StreamDeadlineExceeded(TimeoutError):
    """A streamed Claude call ran past our own wall-clock cap.

    httpx's read timeout on a *streamed* response only bounds the gap between
    individual chunks, not the call's total duration — a steady trickle of
    SSE events (large transcript, big max_tokens) can keep each gap well
    under the client's configured timeout while the call as a whole runs
    right past Vercel's hard maxDuration, which then kills the connection
    outright (a bare ECONNRESET the caller can't catch or retry). Watching
    wall-clock time ourselves turns that into a normal exception this code
    can catch and retry across polls instead, well before the platform pulls
    the plug.
    """


def _is_retryable_claude_error(exc: Exception) -> bool:
    if isinstance(exc, _StreamDeadlineExceeded):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    if isinstance(exc, anthropic.APIConnectionError):  # covers APITimeoutError too (subclass)
        return True
    return isinstance(exc, (ValueError, json.JSONDecodeError))


# Fallback only for callers that hand in a client with no explicit timeout
# configured (e.g. the long-running Render/desktop path, which can afford to
# hold the process open much longer). Callers with a real wall-clock budget
# (backend/app_serverless.py) set client timeout=... themselves, and that's
# what actually governs the deadline below.
_STREAM_WALL_CLOCK_CAP_S = 200.0


def _client_deadline_s(client: Anthropic, default: float = _STREAM_WALL_CLOCK_CAP_S) -> float:
    """Reuse whatever request timeout the caller already configured on `client`.

    Keeps this in lockstep with e.g. backend/app_serverless.py's
    `_CLAUDE_CALL_TIMEOUT_S` without duplicating that number here.
    """
    timeout = getattr(client, "timeout", None)
    if isinstance(timeout, (int, float)):
        return float(timeout)
    read = getattr(timeout, "read", None)
    return float(read) if isinstance(read, (int, float)) else default


def _stream_final_text(stream, *, deadline_s: float) -> tuple[str, bool]:
    """Consume `stream.text_stream`, enforcing a real total-duration deadline.

    httpx's own read timeout on a streamed response only bounds the gap
    between chunks, not the call's total duration (see
    _StreamDeadlineExceeded) — this is what actually bounds the latter.

    Returns (text, deadline_hit) instead of raising outright: the deadline
    check runs after every chunk including the one that completes the
    message, so a call that happens to finish just past the cap must not
    have its already-complete answer thrown away. Whether that's actually a
    problem depends on whether `text` parses as a complete response, which
    only the caller can judge — so we hand back what we have and let it
    decide.
    """
    start = time.monotonic()
    chunks: list[str] = []
    deadline_hit = False
    for text in stream.text_stream:
        chunks.append(text)
        if time.monotonic() - start > deadline_s:
            deadline_hit = True
            break
    return "".join(chunks), deadline_hit


def _call_with_retries(fn, *, attempts: int = 6, base_delay: float = 2.0, max_delay: float = 30.0, progress_cb=None, label: str = "Claude call"):
    """Run `fn()`, retrying on transient failures / malformed JSON with backoff.

    A single dropped connection, one "Overloaded" response, or one bad JSON
    sample used to fail the whole analysis job outright. Claude's output is
    non-deterministic, so retrying the same prompt after a backoff frequently
    succeeds without any other change. Attempts default higher than a typical
    web-request retry because overload conditions on Anthropic's side can take
    tens of seconds to clear — a long transcript (bigger source video, more
    Claude calls) has more chances to hit one, so this needs to actually ride
    it out rather than give up after a couple of quick tries.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_retryable_claude_error(exc):
                raise
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            if progress_cb:
                progress_cb(f"{label} failed ({exc}); retrying in {delay:.0f}s ({attempt}/{attempts})...")
            time.sleep(delay)
    raise last_exc

VALID_CONTENT_TYPES = frozenset({
    "emotional moment",
    "turning point",
    "powerful statement",
    "inspirational moment",
    "educational insight",
    "highly shareable moment",
    "strong hook",
})

ISTV_ACCOUNTS = """
- istv-people — broad human-interest: personal journeys, origin stories, deeply human and relatable moments. The widest net.
- istv-legacymakers — founders/leaders building something enduring: a brand, institution, or movement meant to last.
- istv-operationceo — ONLY for military/army veterans, armed-forces service members, or people who transitioned from active service into entrepreneurship. Do NOT use for regular CEOs or operators who never served.
- istv-womeninpower — women founders, leaders, and changemakers.
- istv-mompreneurs — mothers building businesses; the intersection of parenthood and entrepreneurship.
""".strip()

STORY_OFF_BLOCK = """STORY MODE: OFF.
Generate all {{NUM_REELS}} reels as INDEPENDENT, standalone reels — each self-contained and covering a DIFFERENT moment from the documentary. No "Part N" titles. Set "series_part": null on every reel."""

STORY_ON_BLOCK = """STORY MODE: ON.
Among the {{NUM_REELS}} reels, designate 4-5 of them as a SEQUENTIAL SERIES that tells one connected story across multiple posts:
- Pick consecutive, naturally connected story beats so each part leads into the next (Part 1 sets it up, the middle parts build, the last resolves).
- Title them "Part 1: ...", "Part 2: ...", etc., and set "series_part": 1, 2, 3... in order.
- They are meant to be posted in sequence — reflect that in best_posting_time (consecutive days/slots).
The REMAINING reels (to reach {{NUM_REELS}} total) are INDEPENDENT standalone reels covering different moments; set "series_part": null on those.
All {{NUM_REELS}} reels together must still cover DIFFERENT parts of the documentary — standalone reels must not repeat what the series covers."""

ANALYZER_PROMPT = """You are a Social Media Strategist, Growth Marketer, and Content Director for ISTV. You turn a documentary into a complete short-form marketing package. You do NOT think like a clipping tool.

# Input
The transcript (in the user message) is pre-split into numbered SENTENCE segments:
[id] start=<sec.dec> end=<sec.dec> speaker=<n> "one full sentence, verbatim"
You SELECT WHOLE SEGMENTS BY ID, in playback order. You never invent timestamps and never start or end inside a segment. Times are resolved downstream from the ids you choose. The speaker field tells you who said each segment — see the ONE SPEAKER ONLY rule below; this is not decorative, you must actually use it.

# Who these reels are for
ISTV is an interview-documentary company. This subject is a PAYING CLIENT — a founder, entrepreneur, lawyer, doctor, woman entrepreneur, veteran-turned-CEO, or similar professional — and THEY will post these reels to THEIR OWN social accounts. The reels are this person's personal brand, not generic clips. Two consequences:
- Every reel must be something the subject would be PROUD to attach their name to and share: it should make them look credible, human, and worth following.
- Across the set, a viewer should come away understanding both WHO this person is (their story, values) AND WHAT they do (their company, work, or field). Aim for at least a few reels that touch on their business or work — drawn only from what they actually said, never invented.

# ONE SPEAKER ONLY (hard rule)
Speaker {{MAIN_SPEAKER_ID}} is the main subject — identified as whoever talks the most across the transcript. EVERY segment in EVERY reel must be Speaker {{MAIN_SPEAKER_ID}}'s own words. Never select a segment from any other speaker: not the interviewer's questions, not another interviewee's answer (an employee, spouse, friend, colleague, or anyone else who appears in this transcript), not a reaction or interjection from someone else — even if it seems to bridge, resolve, or add color to the story. Check the "speaker=" field on every segment id before selecting it. The only exception is genuine voice-over narration read over unrelated visual footage (not another person answering their own question) — which essentially never appears in an interview transcript like this one; when in doubt, exclude the other speaker's segment.

# Brand / promotional reels (required, every run)
Reserve exactly 1-2 of the {{NUM_REELS}} reels as BRAND/PROMOTIONAL reels — content the client can keep and post specifically to promote their business, not just their personal story. Build these from segments where the subject talks about their company, product, service, offer, clients, or mission: what they do, who they help, what makes them different, results they've delivered. They should still open with a real hook and land cleanly like every other reel — the difference is WHAT they're about, not a lower bar. Mark these by setting "is_brand_reel": true in the JSON (every other reel: false). If the transcript genuinely contains little-to-no direct talk about the business/work, pick the 1-2 reels that come closest and still mark them true — never invent business details that were never said.

# THE #1 RULE — cover the WHOLE documentary
Do NOT take the best 2-3 minute segment and split it into 10 clips. Analyze the ENTIRE documentary and build {{NUM_REELS}} reels from DIFFERENT parts of it across the full runtime. Never rely on a single section or on where the energy spikes.

# What to find (the {{NUM_REELS}} reels must span a MIX of these, not all one type)
emotional moment · turning point · powerful statement · inspirational moment · educational insight · highly shareable moment · strong hook.
Tag each reel with its primary "content_type" from that list.

{{STORY_MODE_BLOCK}}

# THE CLIENT-POST TEST (apply to every reel before you keep it)
Before finalizing any reel, ask: "Would this client want to post this on their own page?" Keep it only if it makes them look credible, inspiring, or genuinely human. DROP any reel that makes them look incompetent, bitter, arrogant, negative about a named person, or off-message.
Vulnerability is NOT disqualifying — a struggle, a low point, a moment they almost quit is exactly what a client is proud to post, AS LONG AS the reel resolves into strength, insight, or resilience by its end (not left on the low note). This gate decides WHICH moments qualify; the rules below decide HOW to build the ones that pass.

# How to build each reel
- A reel = an ordered list of segment ids forming a complete little arc (hook -> point -> payoff).
- CONTEXT COMPLETE: a cold viewer must understand who is speaking, what happened, and why it matters. Include bridging setup segments — never stitch a payoff to a hook while skipping the sentences in between. If you skip segment ids, the reel will feel random. No pronoun or reference inside the reel may point to a person/thing that only appears in a skipped or un-included sentence.
- OPEN ON A SELF-CONTAINED HOOK: the first segment must stop a thumb — a bold claim, question, number, or stake — AND must NOT depend on an earlier, un-included sentence. Never open on an unresolved reference: a bare pronoun ("She did it...", "They told me...", "He left...", "It changed everything..."), a demonstrative ("This was the moment...", "That's when..."), or a connector ("And so...", "But then...", "Because of that..."). The subject must be introduced by name, role, or clear noun WITHIN the reel. If the hook references something set up earlier, INCLUDE that setup segment so the context is resolved inside the reel. Never open on filler/continuation ("for a very long time I mean...", "After like three maybe..."). If a later line is a stronger opener, lead with it (cold-open) and set "order_mode":"hook_pull" — it must still be understandable with zero prior context.
- END ON A LANDED BEAT (this outranks every other rule, including length): the last segment MUST end on a FULLY finished thought the speaker has delivered — the viewer should feel the idea is complete and nothing is left hanging, NOT that the speaker was about to say more. Never end mid-phrase, while the voice is still rising, on a connective ("and/but/so/that/or"), or on dangling setup ("I felt", "she was", "into my", "a lot of that", "to scope it"). If the thought finishes in the following segment (even one trailing word like "career" after "into my"), INCLUDE that segment so the reel resolves — it is far better to run well past the target length than to cut a beat early. When you're unsure whether a candidate ending is complete, resolve the doubt by extending, not by cutting. The reel must END on closure, not a hard cut that sounds like the sentence kept going — an abrupt ending makes the whole reel unusable no matter how strong the rest of it is.
  GRAMMATICALLY COMPLETE IS NOT THE SAME AS STORY COMPLETE: a sentence can be a full, correctly-punctuated sentence and still leave the STORY unresolved. If the last segment says something happened, changed, was decided, or was realized — WITHOUT actually revealing what it was — that is still building, not landed, even though the sentence itself is grammatically finished (e.g. "and that's the moment everything changed" tells the viewer a change happened but not what it was; "so I made a decision right then" names a decision but not what it was). Keep including segments until the actual content of that outcome is on-screen, not just the announcement that an outcome occurred.
  MULTI-SPEAKER ENDINGS: if a different speaker's line falls at or near the end of the reel (a reaction, interjection, laugh, or half-sentence cutting in on the previous speaker), that is NOT a landed beat by default — and per ONE SPEAKER ONLY above, you should not have selected it in the first place. Either (a) that speaker's contribution is itself a real, complete, meaningful line — include enough of it that it resolves on its own, or (b) it isn't — in which case end the reel on the PREVIOUS speaker's last landed line instead and drop the interjection entirely. Never let a reel trail off on someone else's half-reaction just because it happened to come next chronologically.
  THE LOOK-AHEAD CHECK (do this explicitly for every reel before finalizing it): take the segment you're about to end on and read the NEXT segment in the transcript (same speaker only, per ONE SPEAKER ONLY). Ask: does that next segment complete or resolve the thought? If YES — include it, extend the reel to end there instead. If NO — the next segment starts a new thought, changes topic, or is itself incomplete — then do NOT end on your original candidate either if it was borderline; instead walk BACKWARD to the last segment that was already a clean, fully landed thought, and end there. Never leave the final segment as a coin-flip guess — always explicitly check one segment ahead, then either extend into it or retreat to the last segment you were already confident about. A reel should never simply stop because a segment id was reached; it should stop because that is where the thought actually finished.
- BLEND if it helps: you may stitch up to ~5 non-contiguous segments into one reel if they genuinely connect and flow when spoken aloud.
- {{LENGTH_RULE}}
- NO FILLER at the edges ("um/uh/you know/and and").
- SELF-CONTAINED and DISTINCT: each reel understandable alone; no two reels covering the same ground.
- ACCURATE: titles/captions must be true to what's said — never embellish or overclaim. Don't build on transcription garble.

# Score each reel (0-100, plus hook/flow/value out of 10).

# For EACH reel, write the full marketing package:
- title (Reel Title): short, engaging, scroll-stopping. Use curiosity/payoff formulas ("Why...", "How she...", "The [surprising] reason...", a stark number) — never a flat description. Frame the person as the story — a title the subject would proudly repost, never a stranger's hot take at their expense. For series reels, prefix "Part N: ". <=60 chars.
- caption: social-media-ready — hook line, 1-2 lines of story, one soft CTA. Story-first and credible. NO hype/sales/MLM language ("change your life","DM me","financial freedom").
- seo_title: search-optimized for Instagram / YouTube Shorts / TikTok. Front-load the searchable keywords (topic, name, niche). Distinct from the hooky Reel Title. <=70 chars.
- best_posting_time: a specific day + time to publish (e.g., "Tuesday 11:00 AM"). Spread the {{NUM_REELS}} reels across ~2 weeks so the set forms a posting calendar; put the highest-scoring reels in prime windows (Tue-Thu late morning or 7-9 PM); vary the days; don't stack two reels in the same slot. For a series, schedule the parts on consecutive days/slots in order.
- hashtags: 5-9, platform-agnostic, mix of broad and niche.
- spoken_hook: the first sentence the viewer hears.
- text_hook: a 1-3s on-screen overlay that creates a curiosity gap; true to the clip.
- content_type: one of the 7 types above.
- series_part: integer for series reels, else null.
- why_it_works: one line.

# Then write ONE strategist recommendation for the whole package:
Pick the SINGLE best-fit ISTV niche show for this documentary — chosen ONLY from this roster. Infer from the actual story; never guess or default generically:
{{ISTV_ACCOUNTS}}
Return exactly ONE handle (e.g. "istv-legacymakers"). Use istv-operationceo ONLY if the subject is a military/armed-forces veteran who built a business after service. Use istv-mompreneurs ONLY if motherhood + entrepreneurship is central. If nothing fits tightly, use istv-people.

# Output: ONLY valid JSON, no preamble, no markdown fences. Reels sorted by score desc (Reel # = rank). If a series exists, its parts keep their Part order via series_part but still take their place in the ranked list.
{
  "documentary_summary": "1-2 sentences",
  "recommendations": {
    "istv_collaboration": "istv-..."
  },
  "reels": [
    {
      "rank": 1, "score": 0, "score_breakdown": {"hook":0,"flow":0,"value":0},
      "content_type": "", "series_part": null, "is_brand_reel": false,
      "order_mode": "chronological",
      "segment_ids": [0,1],
      "first_segment_id": 0, "last_segment_id": 1,
      "first_word": "", "last_word": "",
      "title": "", "caption": "", "seo_title": "",
      "best_posting_time": "",
      "hashtags": ["",""],
      "spoken_hook": "", "text_hook": "", "why_it_works": ""
    }
  ]
}
You output segment ids only — never seconds."""

# Backward-compatible alias
REEL_SELECTION_SYSTEM = ANALYZER_PROMPT

V2_PROMPT_ADDENDUM = """

# V2 QUALITY BAR (strict — this set is judged for post-readiness)
- HARD HOOK: the FIRST spoken line must be a complete, standalone sentence that stops the scroll on its own. Never start mid-sentence or on a fragment ("Was the first girl...", "amazing is...", "Most importantly is..."). If the strongest hook is a few lines in, lead with it.
- CLEAN LANDING: the LAST spoken line must complete the thought with finality — it should feel like a deliberate closing line, the kind that earns a beat of silence after it, never like the speaker was interrupted or had more coming. Never end on a preposition/filler ("...to", "...I would say", "...you know"), a trailing clause, or a line that only makes sense if the next sentence follows. If finishing the thought needs one more sentence, include it even if the reel runs slightly over the target.
- ZERO REPETITION: never include two segments that say essentially the same thing. Pick the single strongest phrasing.
- ON-TOPIC ONLY: drop any aside, tangent, or self-referential line (e.g. "in my videos and my contents") that does not serve THIS reel's single idea.
- TIGHT: every included segment must earn its place; if removing it doesn't hurt the arc, remove it."""


def _profile_is_v2() -> bool:
    return str(os.getenv("REEL_PROFILE", "")).strip().lower() in ("v2", "2", "updated_v2")


def _length_rule() -> str:
    """Build the LENGTH guidance line from the active duration window (env-configurable).

    Asymmetric, TIERED priority (not one flat "anything goes" range):
      1st priority: {lo}-{hi}s (defaults 45-90s)
      2nd priority (only if the story genuinely demands it): {floor}-{ceiling}s
        (defaults 30-110s — floor tolerance is smaller than ceiling tolerance,
        REEL_FLOOR_TOLERANCE_SECONDS=15 vs REEL_END_TOLERANCE_SECONDS=20)
    {ceiling}s is also a REAL hard mechanical ceiling enforced by the cutter
    (src/cutter.py) — segment ids past it get trimmed off the end regardless of
    content, so Claude needs to self-limit to it, not just be told the window
    is flexible in the abstract.
    """
    lo = int(round(reel_min_seconds()))
    hi = int(round(reel_max_seconds()))
    floor_flex = int(round(REEL_FLOOR_TOLERANCE_SECONDS))
    ceiling_flex = int(round(REEL_END_TOLERANCE_SECONDS))
    ceiling = hi + ceiling_flex
    floor = max(1, lo - floor_flex)
    return (
        f"LENGTH: an abrupt ending, or a story that's still building with no resolution, is a "
        f"FAILED reel, full stop — no score is high enough to excuse it. Completing the thought "
        f"matters more than hitting a number — but there IS a priority order to the window: "
        f"{lo}-{hi}s is the FIRST PRIORITY, the range most reels should land in. Only if the story "
        f"genuinely demands more or less room, {floor}-{ceiling}s is the second-priority, still-fine "
        f"range — not a rare exception, but lower priority than {lo}-{hi}s, so reach for it only when "
        f"{lo}-{hi}s truly isn't enough to land the thought cleanly. Neither {lo}s nor {hi}s is a strict "
        f"requirement — these are illustrations of a principle, not target numbers to hit: a reel that "
        f"completes its story somewhat under {lo}s is DONE, full stop — do not extend it just to "
        f"approach {lo}s. A reel that genuinely needs somewhat more than {hi}s to properly resolve is "
        f"equally fine. "
        f"The ONE hard number that matters: {ceiling}s is the true ceiling — segments beyond it get "
        f"mechanically trimmed off the end regardless of what they contain, which can chop off the "
        f"very resolution you were building toward. So budget for this yourself: if finishing the "
        f"story properly would run past {ceiling}s, that reel's scope is too broad — start it later, "
        f"narrower, or on a smaller beat that actually resolves within {ceiling}s, rather than picking "
        f"a big arc and letting the cutter truncate its ending for you. "
        f"A COMPLETE ending is not just a grammatically finished sentence — the STORY BEAT itself must "
        f"resolve. If a segment states that something happened, changed, or was decided WITHOUT showing "
        f"what it actually was, INCLUDE the following segment(s) that reveal it, even if that means "
        f"landing near {ceiling}s. Never end a thought early just to stay under {hi}s, and never pad "
        f"with filler to reach {lo}s. When in doubt, INCLUDE the next sentence that finishes the thought "
        f"rather than cutting on a rising, unfinished, or merely-grammatically-complete line. "
        f"Do not pick a single short segment expecting it to be stretched to {lo}s afterward — reaching "
        f"{lo}s+ must come from real, connected content you selected (blend multiple segments if one alone "
        f"isn't enough), never from padding tacked on to hit a number."
    )


# ── Public entry point ─────────────────────────────────────────────────────────

def prepare_segments(transcript: dict, words: list[dict] | None = None):
    """Pure-Python prep (no Claude calls): normalize word timings, build
    sentence segments, and format them for the Claude prompt.

    Split out of analyze_with_claude so it can be called more than once (e.g.
    once per resumable serverless step, since it's cheap) without repeating
    any of the LLM work. Returns (normalized_words, segments, segmented_text,
    duration_str).
    """
    raw_words = words if words is not None else (transcript.get("words") or [])
    norm_words = normalize_word_timings(raw_words)
    segments = build_sentence_segments(norm_words, float(transcript.get("duration") or 0))
    segmented_text = format_segments_for_claude(segments)
    duration_str = fmt_time(transcript.get("duration") or 0)
    return norm_words, segments, segmented_text, duration_str


def finalize_analysis(
    reels_raw: list,
    documentary_summary: str,
    recommendations: dict,
    brand: dict,
    words: list[dict],
    segments: list[dict],
    duration_seconds: float,
    *,
    num_reels: int = DEFAULT_NUM_REELS,
    story_mode: bool = False,
    main_speaker_id: int | None = None,
) -> dict:
    """Turn raw Claude output (reel selection + brand story) into the final
    analysis dict. Pure Python, no further LLM calls — split out of
    analyze_with_claude so a caller that ran the two Claude calls as separate
    bounded steps (see backend/app_serverless.py) can finalize once both are
    in hand, same as the single-shot path below does inline.
    """
    reels = [_normalize_claude_reel(row, idx + 1) for idx, row in enumerate(reels_raw)]
    reels.sort(key=lambda r: int(r.get("rank") or r.get("id") or 999))
    for idx, reel in enumerate(reels, start=1):
        reel["id"] = int(reel.get("rank") or idx)
    reels = reels[:num_reels]

    analysis = {
        "reels": reels,
        "brand_story": brand,
        "documentary_summary": documentary_summary,
        "recommendations": normalize_recommendations(recommendations or {}),
        "story_mode": story_mode,
        "segment_count": len(segments),
        "utterance_segments": segments,
        "sentence_segments": segments,
    }
    _normalize_cut_sheets(
        analysis,
        words,
        segments,
        float(duration_seconds or 0),
        story_mode=story_mode,
        main_speaker_id=main_speaker_id,
    )
    _attach_verbatim_for_segments(analysis, {"words": words})
    _attach_words(analysis, words)
    return analysis


def analyze_with_claude(
    transcript: dict,
    model: str,
    api_key: str,
    progress_cb=None,
    *,
    num_reels: int = DEFAULT_NUM_REELS,
    story_mode: bool = False,
    raw_response_cb=None,
) -> dict:
    """
    Claude reel selection from segmented transcript + optional brand story.
    Returns analysis with editor_cut_sheet per reel and word timestamps attached.

    `raw_response_cb`, if given, is forwarded to `select_reels()` and receives
    Claude's exact raw response text (before any JSON parsing) — for archiving
    what was actually said on each attempt, independent of the parsed result.
    """
    if num_reels not in VALID_NUM_REELS:
        num_reels = DEFAULT_NUM_REELS

    # Merge resolution: keep their prepare_segments() refactor (shared with the
    # serverless step path) AND our request timeout + main-speaker identity.
    client = Anthropic(api_key=api_key, timeout=_CLAUDE_CLIENT_TIMEOUT_SECONDS)
    words, segments, segmented_text, duration = prepare_segments(transcript)
    main_speaker_id = _identify_main_speaker(segments)

    _log(progress_cb, f"Built {len(segments)} sentence segments from {len(words):,} words")
    _log(progress_cb, f"Main speaker identified: Speaker {main_speaker_id}")
    mode_label = "ON (4-5 part series)" if story_mode else "OFF (all standalone)"
    _log(progress_cb, f"Story mode {mode_label}")
    _log(progress_cb, f"Selecting {num_reels} reels with Claude ({model})...")
    selection = _call_with_retries(
        lambda: select_reels(
            segments,
            story_mode=story_mode,
            num_reels=num_reels,
            model=model,
            client=client,
            segmented_text=segmented_text,
            main_speaker_id=main_speaker_id,
            raw_response_cb=raw_response_cb,
        ),
        progress_cb=progress_cb,
        label="Reel selection",
    )
    reels_raw = selection.get("reels") or []
    documentary_summary = str(selection.get("documentary_summary") or "").strip()
    recommendations = selection.get("recommendations") or {}

    _log(progress_cb, "Crafting brand story...")
    brand = _call_with_retries(
        lambda: extract_brand_story(client, model, segmented_text, duration),
        progress_cb=progress_cb,
        label="Brand story extraction",
    )

    _log(progress_cb, "Filling verbatim transcript text for each cut window...")
    analysis = finalize_analysis(
        reels_raw,
        documentary_summary,
        recommendations,
        brand,
        words,
        segments,
        float(transcript.get("duration") or 0),
        num_reels=num_reels,
        story_mode=story_mode,
        main_speaker_id=main_speaker_id,
    )
    _log(progress_cb, "Attaching Rev.ai word timestamps to each reel...")

    return analysis


def select_reels(
    segments: list[dict],
    *,
    story_mode: bool = False,
    num_reels: int = DEFAULT_NUM_REELS,
    model: str = DEFAULT_CLAUDE_MODEL,
    client: Anthropic | None = None,
    segmented_text: str | None = None,
    api_key: str | None = None,
    main_speaker_id: int | None = None,
    raw_response_cb=None,
) -> dict:
    """Call Claude with the marketing-package analyzer prompt.

    `raw_response_cb`, if given, receives the exact raw response text before
    JSON parsing/cleanup — lets a caller archive what Claude actually said,
    independent of how `_parse_json_object` interprets it.
    """
    if num_reels not in VALID_NUM_REELS:
        num_reels = DEFAULT_NUM_REELS

    if client is None:
        if not api_key:
            raise ValueError("api_key or client required for select_reels")
        client = Anthropic(api_key=api_key, timeout=_CLAUDE_CLIENT_TIMEOUT_SECONDS)

    if segmented_text is None:
        segmented_text = format_segments_for_claude(segments)

    if main_speaker_id is None:
        main_speaker_id = _identify_main_speaker(segments)

    block = STORY_ON_BLOCK if story_mode else STORY_OFF_BLOCK
    system = (
        ANALYZER_PROMPT.replace("{{STORY_MODE_BLOCK}}", block.replace("{{NUM_REELS}}", str(num_reels)))
        .replace("{{ISTV_ACCOUNTS}}", ISTV_ACCOUNTS)
        .replace("{{LENGTH_RULE}}", _length_rule())
        .replace("{{MAIN_SPEAKER_ID}}", str(main_speaker_id))
        .replace("{{NUM_REELS}}", str(num_reels))
    )
    if _profile_is_v2():
        system += V2_PROMPT_ADDENDUM

    deadline_s = _client_deadline_s(client)
    with client.messages.stream(
        model=model,
        max_tokens=12000,
        system=system,
        messages=[{"role": "user", "content": segmented_text}],
    ) as stream:
        full_text, deadline_hit = _stream_final_text(stream, deadline_s=deadline_s)

    if raw_response_cb:
        raw_response_cb(full_text)

    text = full_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = _parse_json_object(text)
    except (ValueError, json.JSONDecodeError):
        if deadline_hit:
            raise _StreamDeadlineExceeded(f"Claude stream exceeded {deadline_s:.0f}s wall-clock cap")
        raise
    return parsed


# ── Pass 1: Reel extraction (legacy wrapper) ───────────────────────────────────

def _extract_reels(
    client: Anthropic, model: str, segmented_text: str, num_reels: int, *, story_mode: bool = False
) -> tuple[list, str, dict]:
    parsed = select_reels(
        [],
        story_mode=story_mode,
        num_reels=num_reels,
        model=model,
        client=client,
        segmented_text=segmented_text,
    )
    reels_raw = parsed.get("reels") or []
    summary = str(parsed.get("documentary_summary") or "").strip()
    recommendations = parsed.get("recommendations") or {}
    reels = [_normalize_claude_reel(row, idx + 1) for idx, row in enumerate(reels_raw)]
    reels.sort(key=lambda r: int(r.get("rank") or r.get("id") or 999))
    for idx, reel in enumerate(reels, start=1):
        reel["id"] = int(reel.get("rank") or idx)
    return reels[:num_reels], summary, recommendations


def _normalize_claude_reel(raw: dict, fallback_rank: int) -> dict:
    """Map Claude reel-selection JSON to internal editor/cutter fields."""
    rank = int(raw.get("rank") or fallback_rank)
    score_breakdown = raw.get("score_breakdown") if isinstance(raw.get("score_breakdown"), dict) else {}
    hashtags = [str(h).strip() for h in (raw.get("hashtags") or []) if str(h).strip()]
    caption = str(raw.get("caption") or "").strip()
    spoken_hook = str(raw.get("spoken_hook") or "").strip()
    text_hook = str(raw.get("text_hook") or "").strip()
    title = str(raw.get("title") or "").strip()
    why = str(raw.get("why_it_works") or "").strip()
    description = str(raw.get("description") or "").strip()
    seo_title = str(raw.get("seo_title") or "").strip()
    best_posting_time = str(raw.get("best_posting_time") or "").strip()
    content_type = str(raw.get("content_type") or "").strip().lower()
    if content_type not in VALID_CONTENT_TYPES:
        content_type = content_type or "strong hook"
    series_part = raw.get("series_part")
    if series_part is not None:
        try:
            series_part = int(series_part)
        except (TypeError, ValueError):
            series_part = None
    is_brand_reel = bool(raw.get("is_brand_reel"))
    excerpt = str(raw.get("transcript_excerpt") or "").strip()
    nested = raw.get("segments") or []
    segment_ids = raw.get("segment_ids") or []
    if not isinstance(segment_ids, list):
        segment_ids = []
    segment_ids = [_safe_int(x) for x in segment_ids]
    segment_ids = [x for x in segment_ids if x is not None]

    if not segment_ids and isinstance(nested, list):
        for spec in nested:
            if not isinstance(spec, dict):
                continue
            for value in spec.get("segment_ids") or []:
                sid = _safe_int(value)
                if sid is not None:
                    segment_ids.append(sid)
            if not excerpt and spec.get("verbatim"):
                excerpt = str(spec.get("verbatim") or "").strip()

    first_segment_id = _safe_int(raw.get("first_segment_id"))
    last_segment_id = _safe_int(raw.get("last_segment_id"))
    if not segment_ids and first_segment_id is not None and last_segment_id is not None:
        segment_ids = list(range(first_segment_id, last_segment_id + 1))

    order_mode = normalize_order_mode(raw.get("order_mode") or raw.get("assembly_mode"))

    seo_caption = caption
    if hashtags:
        tag_line = " ".join(h if h.startswith("#") else f"#{h.lstrip('#')}" for h in hashtags)
        seo_caption = f"{caption}\n\n{tag_line}".strip() if caption else tag_line

    return {
        "id": rank,
        "rank": rank,
        "score": _float_safe(raw.get("score")),
        "score_breakdown": score_breakdown,
        "segment_ids": segment_ids,
        "first_segment_id": first_segment_id,
        "last_segment_id": last_segment_id,
        "first_word": str(raw.get("first_word") or "").strip(),
        "last_word": str(raw.get("last_word") or "").strip(),
        "segments": nested if isinstance(nested, list) else [],
        "order_mode": order_mode,
        "assembly_mode": order_mode,
        "duration_sec": _float_safe(raw.get("duration_sec")),
        "transcript_excerpt": excerpt,
        "spoken_hook": spoken_hook,
        "text_hook": text_hook,
        "title": title,
        "caption": caption,
        "description": description,
        "seo_title": seo_title,
        "best_posting_time": best_posting_time,
        "content_type": content_type,
        "series_part": series_part,
        "is_brand_reel": is_brand_reel,
        "hashtags": hashtags,
        "theme": why,
        "hook_type": _score_hook_type(score_breakdown),
        "hook_line": spoken_hook,
        "key_quote_1": spoken_hook,
        "key_quote_2": excerpt.split(".")[1].strip() if "." in excerpt else "",
        "suggested_text_overlay": text_hook,
        "suggested_caption": caption,
        "seo_caption": seo_caption,
        "why_will_perform": why,
        "why_it_works": why,
        "editor_cut_sheet": [],
        "assembly_note": (
            "Playback = whole sentence segments in chronological spoken order"
            if order_mode == "chronological"
            else "Playback = hook_pull order (whole sentences reordered)"
        ),
    }


def _score_hook_type(breakdown: dict) -> str:
    hook = _float_safe(breakdown.get("hook"))
    value = _float_safe(breakdown.get("value"))
    if hook >= 8:
        return "HOOK"
    if value >= 8:
        return "VALUE"
    return "STORY"


def _segments_to_cut_sheet(segments: list) -> list[dict]:
    """Legacy fallback when only start/end seconds are available (no word list)."""
    rows: list[dict] = []
    for idx, seg in enumerate(segments[:3]):
        if not isinstance(seg, dict):
            continue
        start = _float_safe(seg.get("start", seg.get("start_time_seconds")))
        end = _float_safe(seg.get("end", seg.get("end_time_seconds")))
        if end <= start:
            continue
        if idx == 0:
            role, label = "HOOK", "HOOK"
        elif idx == len(segments[:3]) - 1 and len(segments[:3]) > 1:
            role, label = "PAYOFF", "PAYOFF"
        else:
            role, label = "BODY", f"BODY {idx}"
        rows.append(
            {
                "order": len(rows) + 1,
                "role": role,
                "label": label,
                "start_time_seconds": start,
                "end_time_seconds": end,
                "first_word_index": seg.get("first_word_index"),
                "last_word_index": seg.get("last_word_index"),
                "note": str(seg.get("note") or seg.get("description") or "")[:500],
            }
        )
    return rows


# ── Pass 2: Brand story ────────────────────────────────────────────────────────

def extract_brand_story(
    client: Anthropic, model: str, segmented_text: str, duration: str
) -> dict:
    prompt = f"""You are an elite brand storyteller, SEO copywriter, and content strategist.
You specialize in founder narratives that perform on Google, LinkedIn, and press features.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MISSION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Extract and craft a 1.5–2.5 minute SEO-optimized brand story from this founder interview.
The client will copy-paste this directly into:
  → Their website About/Origin page
  → LinkedIn Featured section
  → Press kit founder bio
  → Marketing material / pitch decks

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DELIVERABLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

① SEO HEADLINE (55–65 characters)
   — Contains primary keyword + founder name/brand + transformation/claim
   — Click-worthy but honest (no clickbait)
   — Example: "From $0 to 7 Figures: How [Name] Built [X] Without VC Funding"

② META DESCRIPTION (150–160 characters)
   — Summarizes the story arc in one punchy sentence
   — Includes primary keyword naturally
   — Ends with value proposition or intrigue
   — This goes in <meta name="description"> and LinkedIn/press summaries

③ FULL BRAND STORY (250–375 words — exactly 1.5–2.5 min spoken at 150 wpm)
   — Written in FIRST PERSON as if the founder is speaking (polished but authentic)
   — NOT corporate PR speak — raw, human, specific
   — STRUCTURE:
     PARAGRAPH 1 (Hook/Origin): Drop into a specific moment or bold statement. Grab attention.
     PARAGRAPH 2 (The World Before): What problem did they see? What was broken?
     PARAGRAPH 3 (The Leap & The Struggle): What made them start? What almost stopped them?
     PARAGRAPH 4 (Breakthrough): The moment of proof/turning point. Be specific.
     PARAGRAPH 5 (Today & Vision): Where are they now? Where are they taking this?
   — Naturally weave in 5–8 SEO keywords from their actual story
   — Avoid vague statements — use NUMBERS, NAMES, PLACES, DATES when present

④ SEO KEYWORDS (8–12 terms)
   — Mix short-tail (1–2 words) and long-tail (3–5 words)
   — Based entirely on THEIR actual content, industry, expertise
   — These should feel natural, not stuffed

⑤ DOCUMENTARY CUT SHEET — **1.5–2.5 minute sizzle ONLY (NOT the full interview)**
   — HARD RULE: The SUM of (end_time_seconds − start_time_seconds) across ALL rows MUST be **≥ 90** and **≤ 150** seconds. This is a short joined preview arc, not full documentary coverage.
   — 4–7 rows. Each row = one contiguous master clip with ABSOLUTE `start_time_seconds` / `end_time_seconds` (same timebase as transcript).
   — `sequence` = 1,2,3… is join order. `cut_instruction` = one line what to pull.

⑥ `key_moments` may be an empty array [].

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRANSCRIPT ({duration} total runtime)
Each line: [id] start=<seconds> end=<seconds> "verbatim text"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{segmented_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT — Return ONLY valid JSON. No markdown. No explanation.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "brand_story": {{
    "founder_name": "Name if mentioned, else empty string",
    "company_name": "Company/brand if mentioned, else empty string",
    "industry": "Their industry / niche (e.g. 'SaaS / B2B Tech', 'E-commerce', 'Health & Wellness')",
    "seo_headline": "SEO headline (55–65 chars)",
    "meta_description": "SEO meta description (150–160 chars)",
    "full_story": "Complete 250–375 word brand story in founder's first-person voice",
    "word_count": 0,
    "estimated_read_time_seconds": 0,
    "seo_keywords": [
      "keyword 1", "keyword 2", "keyword 3", "keyword 4",
      "keyword 5", "keyword 6", "keyword 7", "keyword 8"
    ],
    "journey_arc": {{
      "origin": "What was their world before? What problem did they see?",
      "leap": "What made them start / take the risk?",
      "struggle": "What almost stopped them? (specific, not generic)",
      "breakthrough": "The exact turning point — be specific",
      "today": "Where are they now?",
      "vision": "Where are they taking this?"
    }},
    "documentary_cut_sheet": [
      {{
        "sequence": 1,
        "section_title": "e.g. OPEN / STRUGGLE / TURN / TODAY",
        "start_time_seconds": 0.0,
        "end_time_seconds": 0.0,
        "cut_instruction": "One line — what to pull from master for this segment"
      }}
    ],
    "cut_sheet_assembly_note": "One line reminder: follow sequence numbers when joining",
    "key_moments": [],
    "emotional_themes": ["theme1", "theme2", "theme3"]
  }}
}}"""

    deadline_s = _client_deadline_s(client)
    with client.messages.stream(
        model=model,
        max_tokens=6000,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        full_text, deadline_hit = _stream_final_text(stream, deadline_s=deadline_s)
    try:
        result = _parse_json(full_text, "brand_story")
    except (ValueError, json.JSONDecodeError):
        if deadline_hit:
            raise _StreamDeadlineExceeded(f"Claude stream exceeded {deadline_s:.0f}s wall-clock cap")
        raise
    # Normalise — the JSON root key may or may not be nested
    if isinstance(result, dict) and "brand_story" in result:
        return result["brand_story"]
    return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_ids_from_sheet(sheet: list[dict]) -> list[int]:
    ids: list[int] = []
    for row in sheet:
        for value in row.get("segment_ids") or []:
            sid = _safe_int(value)
            if sid is not None:
                ids.append(sid)
    return ids


def _float_safe(x, default: float = 0.0) -> float:
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _row_duration(row: dict) -> float:
    a = _float_safe(row.get("start_time_seconds"))
    b = _float_safe(row.get("end_time_seconds"))
    return max(0.0, b - a)


def _clamp_segment_row(row: dict, tmax: float) -> None:
    a = _float_safe(row.get("start_time_seconds"))
    b = _float_safe(row.get("end_time_seconds"))
    if tmax > 0:
        a = max(0.0, min(a, tmax))
        b = max(0.0, min(b, tmax))
    if b <= a:
        b = min(a + 0.25, tmax) if tmax > 0 else a + 0.25
    row["start_time_seconds"], row["end_time_seconds"] = a, b


def _playback_total_seconds(rows: list[dict]) -> float:
    return sum(_row_duration(r) for r in rows)


def _cap_playback_rows(rows: list[dict], max_total: float) -> list[dict]:
    """Trim overlong reels by dropping body spans — never shave the payoff mid-sentence."""
    rows = [r for r in rows if _row_duration(r) > 0.001]
    if not rows:
        return rows

    while _playback_total_seconds(rows) > max_total + 0.02 and len(rows) > 1:
        if len(rows) > 2:
            rows.pop(-2)
            continue
        first = rows[0]
        a = _float_safe(first.get("start_time_seconds"))
        b = _float_safe(first.get("end_time_seconds"))
        dur = max(0.0, b - a)
        excess = _playback_total_seconds(rows) - max_total
        if dur - excess >= 0.35:
            first["end_time_seconds"] = b - excess
            break
        rows.pop(0)
    return rows


def _maybe_extend_playback_rows(
    rows: list[dict],
    min_total: float,
    transcript_end: float,
    utterance_segments: list[dict] | None = None,
    main_speaker_id: int | None = None,
) -> list[dict]:
    """Extend a too-short single-segment reel up to the minimum duration.

    Prefers pulling in whole subsequent SENTENCE segments (the same
    sentence-bounded units Claude picks reels from, via `utterance_segments`)
    over blindly adding raw seconds. Raw-second padding has no idea where a
    word or sentence ends, so it could — and did — land mid-word or mid-phrase
    just to hit the numeric floor, producing an ending that's arbitrary rather
    than a real completed thought. Sentence segments are already boundary-safe
    by construction (see build_sentence_segments), so walking forward through
    them one at a time guarantees the extension never splits a word/sentence.

    Never extends past the start of a different speaker's segment. This
    function mutates the ROW's end_time_seconds directly by walking forward
    through time — it never touches `reel["segment_ids"]` and runs AFTER the
    mechanical single-speaker filter in `_normalize_cut_sheets`, so without
    this check it would silently pad straight through (or land the tail
    inside) a foreign speaker's segment purely because it was next
    chronologically, completely undoing that filter for any reel under the
    length floor — found live via real-footage testing: `segment_ids` looked
    correctly single-speaker while the actual rendered clip wasn't.
    """
    if not rows or transcript_end <= 0:
        return rows
    deficit = min_total - _playback_total_seconds(rows)
    if deficit <= 0.02:
        return rows
    last = rows[-1]
    b = _float_safe(last.get("end_time_seconds"))

    ceiling = transcript_end
    if utterance_segments and main_speaker_id is not None:
        for seg in sorted(utterance_segments, key=lambda s: _float_safe(s.get("start"))):
            if _float_safe(seg.get("start")) < b - 0.05:
                continue
            if int(seg.get("speaker", main_speaker_id) or main_speaker_id) != main_speaker_id:
                # Guard below the foreign segment's actual start, not exactly
                # at it — a same-speaker segment can end with ZERO gap before
                # a different speaker's segment (a real interruption/overlap
                # in the source audio, seen live: speaker A trails off with
                # no pause and speaker B talks over them). Landing the row's
                # end_time_seconds exactly on that boundary let the foreign
                # speaker's first word slip into `timestamped_words` anyway,
                # since `_attach_words`'s inclusion check is `ws > end`
                # (inclusive of an exact tie) — this keeps a real gap so that
                # check reliably excludes it.
                ceiling = min(ceiling, _float_safe(seg.get("start")) - NEXT_WORD_GUARD_SEC)
                break

    if utterance_segments:
        candidates = sorted(
            (
                s for s in utterance_segments
                if _float_safe(s.get("start")) >= b - 0.05 and _float_safe(s.get("start")) < ceiling - 0.01
            ),
            key=lambda s: _float_safe(s.get("start")),
        )
        new_end = b
        last_clean_end = b  # best boundary seen so far that ends cleanly
        deficit_met = False
        for seg in candidates:
            seg_end = _float_safe(seg.get("end"))
            if seg_end <= new_end + 0.01:
                continue
            new_end = min(seg_end, ceiling)
            if not deficit_met and new_end - b >= deficit - 0.02:
                deficit_met = True
            # Once the numeric floor is satisfied, keep walking through
            # whole same-speaker segments until landing on one that actually
            # ends cleanly — otherwise this stops the instant the deficit
            # math clears, with zero regard for whether that segment's own
            # text is a landed thought. That's how a reel ended on "...give
            # me a male's mentality," (a comma mid-list) instead of the very
            # next segment, "but a woman's fortitude." — a segment that was
            # already same-speaker and well within budget. Same principle as
            # cutter.py's tail-extension: run past the target length rather
            # than cut a beat early.
            clean = not _segment_text_dangles(str(seg.get("text") or ""))
            if clean:
                last_clean_end = new_end
            if deficit_met and clean:
                break
        else:
            # Ran out of same-speaker candidates (hit the ceiling) without
            # ever landing on a clean ending past the deficit — e.g. the
            # speaker trails off right as a different speaker interrupts,
            # with no room left to extend into. Fall back to the best clean
            # boundary seen (possibly `b` itself, i.e. no extension at all)
            # rather than accept a dangling ending just to hit the floor —
            # `b` is already guaranteed clean, since this function only runs
            # on rows that already passed through cutter.py's
            # `_trim_dangling_tail_ids` as the last step before this.
            new_end = last_clean_end
        if new_end > b + 0.01:
            last["end_time_seconds"] = new_end
            return rows
        if candidates:
            # There WAS same-speaker material to consider, but none of it
            # ended cleanly (the `else` branch above already tried and
            # fell back to `b`) — don't drop down to raw-second padding
            # in that case, since it has no completeness check at all and
            # would happily undo this decision by padding mid-sentence
            # through the exact same dangling material. Better to stay
            # under the floor than fabricate a worse ending.
            return rows
        # No same-speaker sentence segments available to extend into at all
        # (e.g. this is already the last segment in the transcript, or a
        # different speaker starts immediately) — fall through to raw-second
        # padding below, still bounded by `ceiling`, rather than leaving the
        # reel under the floor.

    room = ceiling - b
    if room <= 0.02:
        return rows
    last["end_time_seconds"] = min(b + deficit, b + room, ceiling)
    if _float_safe(last.get("end_time_seconds")) <= b + 0.01:
        return rows
    return rows


def _seg_order_key(x: dict) -> int:
    o = x.get("order")
    if o is None:
        return 0
    try:
        return int(float(o))
    except (TypeError, ValueError):
        return 0


def _build_editor_cut_sheet_from_playback(reel: dict) -> list[dict] | None:
    if reel.get("editor_cut_sheet"):
        return reel.get("editor_cut_sheet")
    segments = reel.get("segments")
    if isinstance(segments, list) and segments:
        return _segments_to_cut_sheet(segments)
    pbs = reel.get("playback_segments")
    if not isinstance(pbs, list) or not pbs:
        return None
    ordered = sorted(pbs, key=_seg_order_key)
    rows: list[dict] = []
    for seg in ordered:
        role = str(seg.get("role") or "BODY").strip().upper() or "BODY"
        if role not in ("HOOK", "BODY", "PAYOFF"):
            role = "BODY"
        try:
            a = float(seg.get("start_time_seconds") or 0)
            b = float(seg.get("end_time_seconds") or 0)
        except (TypeError, ValueError):
            a, b = 0.0, 0.0
        rows.append(
            {
                "order": len(rows) + 1,
                "role": role,
                "label": "HOOK" if role == "HOOK" else ("PAYOFF" if role == "PAYOFF" else "BODY"),
                "start_time_seconds": a,
                "end_time_seconds": b,
                "note": (seg.get("description") or "")[:500],
            }
        )
    for i, row in enumerate(rows, start=1):
        row["order"] = i
    return rows


def _normalize_cut_sheets(
    analysis: dict,
    words: list[dict],
    utterance_segments: list[dict],
    transcript_duration: float = 0.0,
    *,
    story_mode: bool = False,
    main_speaker_id: int | None = None,
) -> None:
    """Build word-accurate cut sheets from v2 sentence segment picks."""
    tmax = max(0.0, float(transcript_duration or 0.0))
    words = normalize_word_timings(words)
    protect_ends = True
    max_dur = reel_max_seconds()
    min_dur = reel_min_seconds()
    soft_dur = max_dur + REEL_END_TOLERANCE_SECONDS
    # Asymmetric: floor tolerance is smaller than ceiling tolerance (15s vs
    # 20s at defaults) — a reel that already completes its story a bit under
    # min_dur is fine as-is; only pad reels shorter than THIS toward min_dur,
    # so a genuinely short-but-complete story doesn't get mechanically
    # stretched just to hit the target number.
    soft_min = max(1.0, min_dur - REEL_FLOOR_TOLERANCE_SECONDS)
    seg_by_id = {int(s["id"]): s for s in utterance_segments if s.get("id") is not None}
    if main_speaker_id is None:
        main_speaker_id = _identify_main_speaker(utterance_segments)

    for reel in analysis.get("reels", []):
        order_mode = normalize_order_mode(reel.get("order_mode") or reel.get("assembly_mode"))

        # Mechanical safety net for the "ONE SPEAKER ONLY" prompt rule: drop
        # any segment id Claude picked that belongs to a different speaker
        # than the identified main subject. Only applied if it leaves at
        # least one segment — an imperfect reel beats an empty one.
        raw_ids = extract_reel_segment_ids(reel)
        if raw_ids:
            same_speaker_ids = [
                sid for sid in raw_ids
                if seg_by_id.get(sid, {}).get("speaker", main_speaker_id) == main_speaker_id
            ]
            if same_speaker_ids:
                reel["segment_ids"] = same_speaker_ids

        sheet = build_reel_cut_sheet(
            words,
            reel,
            utterance_segments,
            video_duration=tmax,
            protect_ends=protect_ends,
            max_len=max_dur,
        )

        if not sheet:
            sheet = _build_editor_cut_sheet_from_playback(reel) or reel.get("editor_cut_sheet") or []
        if not isinstance(sheet, list):
            sheet = []

        for row in sheet:
            _clamp_segment_row(row, tmax)
            if _row_duration(row) <= 0.001:
                a = _float_safe(row.get("start_time_seconds"))
                row["end_time_seconds"] = a + 6.0
                _clamp_segment_row(row, tmax)

        sheet = [r for r in sheet if _row_duration(r) > 0.001]
        if order_mode == "hook_pull":
            sheet = sorted(sheet, key=lambda x: _seg_order_key(x))
        else:
            sheet = sorted(
                sheet,
                key=lambda x: (_float_safe(x.get("start_time_seconds")), _seg_order_key(x)),
            )
        for i, row in enumerate(sheet, start=1):
            row["order"] = i

        if not sheet:
            rs = _float_safe(reel.get("start_time_seconds"))
            re_ = _float_safe(reel.get("end_time_seconds"))
            if re_ <= rs:
                re_ = rs + 60.0
            if tmax > 0:
                re_ = min(re_, tmax)
            sheet = [
                {
                    "order": 1,
                    "role": "HOOK",
                    "label": "HOOK",
                    "start_time_seconds": max(0.0, rs),
                    "end_time_seconds": re_,
                    "note": (reel.get("spoken_hook") or reel.get("hook_line") or "")[:500],
                }
            ]
            for row in sheet:
                _clamp_segment_row(row, tmax)

        hook_row = next((r for r in sheet if str(r.get("role") or "").upper() == "HOOK"), sheet[0])
        reel["hook_line_start_seconds"] = _float_safe(hook_row.get("start_time_seconds"))
        reel["hook_line_end_seconds"] = _float_safe(hook_row.get("end_time_seconds"))
        if reel["hook_line_end_seconds"] <= reel["hook_line_start_seconds"]:
            reel["hook_line_end_seconds"] = reel["hook_line_start_seconds"] + 5.0

        sheet = _cap_playback_rows(sheet, soft_dur)
        if _playback_total_seconds(sheet) < soft_min and len(sheet) == 1 and tmax > 0:
            sheet = _maybe_extend_playback_rows(
                sheet, min_dur, tmax, utterance_segments, main_speaker_id=main_speaker_id
            )
            sheet = _cap_playback_rows(sheet, soft_dur)

        reel["segment_ids"] = extract_reel_segment_ids(reel) or extract_ids_from_sheet(sheet)

        reel["editor_cut_sheet"] = sheet
        reel["order_mode"] = order_mode
        reel["assembly_mode"] = order_mode
        all_starts = [_float_safe(r.get("start_time_seconds")) for r in sheet]
        all_ends = [_float_safe(r.get("end_time_seconds")) for r in sheet]
        reel["start_time_seconds"] = min(all_starts) if all_starts else 0.0
        reel["end_time_seconds"] = max(all_ends) if all_ends else reel["start_time_seconds"] + 60.0
        reel["duration_seconds"] = round(_playback_total_seconds(sheet), 1)
        reel["duration_sec"] = reel["duration_seconds"]

    brand = analysis.get("brand_story") or {}
    dcs = brand.get("documentary_cut_sheet") or brand.get("cut_sheet")
    if not isinstance(dcs, list):
        dcs = []
    if not dcs:
        kms = brand.get("key_moments")
        if isinstance(kms, list) and kms:
            sorted_km = sorted(kms, key=lambda m: _float_safe(m.get("timestamp_seconds")))
            cap_rows = 5
            budget = 120.0
            n = min(cap_rows, len(sorted_km)) or 1
            per = min(24.0, budget / n)
            for i, m in enumerate(sorted_km[:cap_rows]):
                ts = _float_safe(m.get("timestamp_seconds"))
                end_ts = min(ts + per, ts + 28.0)
                if tmax > 0:
                    end_ts = min(end_ts, tmax)
                dcs.append(
                    {
                        "sequence": i + 1,
                        "section_title": m.get("moment_type") or "BEAT",
                        "start_time_seconds": ts,
                        "end_time_seconds": max(end_ts, ts + 5.0),
                        "cut_instruction": (
                            m.get("narrative_significance") or m.get("quote") or ""
                        )[:500],
                    }
                )
    else:
        dcs = sorted(
            dcs,
            key=lambda x: (_seg_order_key({**x, "order": x.get("sequence")}), _float_safe(x.get("start_time_seconds"))),
        )
        for i, row in enumerate(dcs, start=1):
            row["sequence"] = i

    for row in dcs:
        if words and utterance_segments:
            start, end = resolve_brand_row_bounds(
                words, row, utterance_segments, video_duration=tmax
            )
            row["start_time_seconds"] = start
            row["end_time_seconds"] = end
        _clamp_segment_row(row, tmax)

    while dcs:
        cur = sum(
            max(0.0, _float_safe(r.get("end_time_seconds")) - _float_safe(r.get("start_time_seconds")))
            for r in dcs
        )
        if cur <= 150.0 + 0.01:
            break
        last = dcs[-1]
        a = _float_safe(last.get("start_time_seconds"))
        b = _float_safe(last.get("end_time_seconds"))
        dur = max(0.0, b - a)
        excess = cur - 150.0
        if dur - excess >= 1.0:
            last["end_time_seconds"] = b - excess
            break
        dcs.pop()

    if dcs and tmax > 0:
        cur = sum(
            max(0.0, _float_safe(r.get("end_time_seconds")) - _float_safe(r.get("start_time_seconds")))
            for r in dcs
        )
        if cur < 90.0 - 0.5:
            deficit = 90.0 - cur
            last = dcs[-1]
            b = _float_safe(last.get("end_time_seconds"))
            room = tmax - b
            if room > 0.5:
                last["end_time_seconds"] = min(b + deficit, b + room, tmax)

    while dcs:
        cur = sum(
            max(0.0, _float_safe(r.get("end_time_seconds")) - _float_safe(r.get("start_time_seconds")))
            for r in dcs
        )
        if cur <= 150.0 + 0.01:
            break
        last = dcs[-1]
        a = _float_safe(last.get("start_time_seconds"))
        b = _float_safe(last.get("end_time_seconds"))
        dur = max(0.0, b - a)
        excess = cur - 150.0
        if dur - excess >= 1.0:
            last["end_time_seconds"] = b - excess
            break
        dcs.pop()

    brand["documentary_cut_sheet"] = dcs
    brand.setdefault(
        "cut_sheet_assembly_note",
        "Cut and join segments in SEQUENCE order on your timeline (brand sizzle target 1.5–2.5 min total).",
    )
    analysis["brand_story"] = brand


def _attach_verbatim_for_segments(analysis: dict, transcript: dict) -> None:
    """Add verbatim_transcript from Rev.ai words for brand rows and reel cut-sheet parts."""
    words = transcript.get("words") or []
    brand = analysis.get("brand_story") or {}
    for row in brand.get("documentary_cut_sheet") or []:
        try:
            a = float(row.get("start_time_seconds") or 0)
            b = float(row.get("end_time_seconds") or 0)
        except (TypeError, ValueError):
            a, b = 0.0, 0.0
        row["verbatim_transcript"] = verbatim_from_words(words, a, b)

    for reel in analysis.get("reels", []):
        try:
            hs = float(reel.get("hook_line_start_seconds") or reel.get("start_time_seconds") or 0)
            he = float(reel.get("hook_line_end_seconds") or hs)
        except (TypeError, ValueError):
            hs, he = 0.0, 0.0
        reel["hook_line_verbatim"] = verbatim_from_words(words, hs, he)
        for part in reel.get("editor_cut_sheet") or []:
            try:
                a = float(part.get("start_time_seconds") or 0)
                b = float(part.get("end_time_seconds") or 0)
            except (TypeError, ValueError):
                a, b = 0.0, 0.0
            part["verbatim_transcript"] = verbatim_from_words(words, a, b)


def _attach_words(analysis: dict, words: list) -> None:
    """Attach Rev.ai word objects across stitched reel segments (playback order)."""
    for reel in analysis.get("reels", []):
        collected: list[dict] = []
        parts = sorted(
            reel.get("editor_cut_sheet") or [],
            key=lambda x: (_seg_order_key(x), _float_safe(x.get("start_time_seconds"))),
        )
        if parts:
            for part in parts:
                start = _float_safe(part.get("start_time_seconds"))
                end = _float_safe(part.get("end_time_seconds"))
                for w in words:
                    ws = _float_safe(w.get("start"))
                    we = _float_safe(w.get("end"), ws)
                    # Window is [start, end) — exclusive on BOTH sides, not
                    # just the end. `ws >= end` excludes a word starting
                    # exactly at the window's close (a same-speaker segment
                    # ending with zero gap before a different speaker's
                    # segment, seen live). `we <= start` (not `<`) excludes
                    # the mirror case: a word ending exactly where THIS
                    # window starts — happens when `_resolve_run`'s
                    # lead-in padding clamps a row's start to exactly
                    # `prev_word.end`, and that previous word belongs to a
                    # DIFFERENT speaker's segment (also seen live: a
                    # one-word foreign interjection right before a
                    # same-speaker payoff span leaked in this way).
                    if we <= start or ws >= end:
                        continue
                    collected.append(
                        {
                            "word": w.get("word", ""),
                            "time": ws,
                            "start": ws,
                            "end": we,
                            "index": w.get("index"),
                            "ts": fmt_time(ws),
                            "speaker": w.get("speaker", 0),
                        }
                    )
        else:
            start = _float_safe(reel.get("start_time_seconds"))
            end = _float_safe(reel.get("end_time_seconds"))
            for w in words:
                ws = _float_safe(w.get("start"))
                we = _float_safe(w.get("end"), ws)
                if we <= start or ws >= end:  # see boundary note above
                    continue
                collected.append(
                    {
                        "word": w.get("word", ""),
                        "time": ws,
                        "start": ws,
                        "end": we,
                        "index": w.get("index"),
                        "ts": fmt_time(ws),
                        "speaker": w.get("speaker", 0),
                    }
                )
        collected.sort(key=lambda x: x["time"])
        seen: set[tuple[float, str]] = set()
        uniq: list[dict] = []
        for item in collected:
            key = (round(float(item["time"]), 4), str(item.get("word", "")))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(item)
        reel["timestamped_words"] = uniq


def _parse_json_object(text: str) -> dict:
    """Extract and parse the first JSON object from a Claude response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        # Log the raw response server-side only — never surface model output in
        # the exception message, since it propagates verbatim to job["error"]
        # and gets shown to the user in the desktop app / web frontend.
        print(f"[analyzer] Claude returned no valid JSON. Response preview:\n{text[:500]}", flush=True)
        raise ValueError("Claude returned an unparsable response. Please try again.")
    parsed = json.loads(match.group())
    if not isinstance(parsed, dict):
        raise ValueError("Claude JSON root must be an object.")
    return parsed


def _parse_json(text: str, expected_key: str):
    """Extract and parse the first JSON object from a Claude response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        print(
            f"[analyzer] Claude returned no valid JSON for '{expected_key}'. "
            f"Response preview:\n{text[:500]}",
            flush=True,
        )
        raise ValueError(f"Claude returned an unparsable response for '{expected_key}'. Please try again.")
    parsed = json.loads(match.group())
    # Unwrap top-level key if present
    if expected_key in parsed:
        return parsed[expected_key]
    return parsed


def _log(cb, msg: str):
    if cb:
        cb(msg)


def _identify_main_speaker(segments: list[dict]) -> int:
    """Speaker id with the most total speaking time — the presumed documentary
    subject (the "main speaker"), as opposed to an interviewer, another
    interviewee, a spouse, employee, or friend who also appears in the raw
    transcript. Used both to tell Claude who to select from and as the basis
    for the mechanical single-speaker filter in `_normalize_cut_sheets`."""
    totals: dict[int, float] = {}
    for seg in segments:
        sp = int(seg.get("speaker", 0) or 0)
        dur = max(0.0, float(seg.get("end") or 0) - float(seg.get("start") or 0))
        totals[sp] = totals.get(sp, 0.0) + dur
    if not totals:
        return 0
    return max(totals.items(), key=lambda kv: kv[1])[0]
