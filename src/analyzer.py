import json
import os
import re

from anthropic import Anthropic
from src.transcription import fmt_time
from src.cutter import (
    MAX_REEL_SECONDS,
    REEL_END_TOLERANCE_SECONDS,
    build_reel_cut_sheet,
    extract_reel_segment_ids,
    normalize_order_mode,
    normalize_word_timings,
    reel_max_seconds,
    reel_min_seconds,
    resolve_brand_row_bounds,
)
from src.transcript_segments import build_sentence_segments, format_segments_for_claude
from src.transcript_snippets import verbatim_from_words
from src.marketing_doc import normalize_recommendations

# Project default: Claude Opus 4.8 only
CLAUDE_MODELS = {
    "claude-opus-4-8": "Claude Opus 4.8",
}

DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"

VALID_NUM_REELS = {3, 5, 10, 12}
DEFAULT_NUM_REELS = 10
MIN_REEL_DURATION = 15.0
MAX_REEL_DURATION = MAX_REEL_SECONDS

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
[id] start=<sec.dec> end=<sec.dec> "one full sentence, verbatim"
You SELECT WHOLE SEGMENTS BY ID, in playback order. You never invent timestamps and never start or end inside a segment. Times are resolved downstream from the ids you choose.

# THE #1 RULE — cover the WHOLE documentary
Do NOT take the best 2-3 minute segment and split it into 10 clips. Analyze the ENTIRE documentary and build {{NUM_REELS}} reels from DIFFERENT parts of it across the full runtime. Never rely on a single section or on where the energy spikes.

# What to find (the {{NUM_REELS}} reels must span a MIX of these, not all one type)
emotional moment · turning point · powerful statement · inspirational moment · educational insight · highly shareable moment · strong hook.
Tag each reel with its primary "content_type" from that list.

{{STORY_MODE_BLOCK}}

# How to build each reel
- A reel = an ordered list of segment ids forming a complete little arc (hook -> point -> payoff).
- CONTEXT COMPLETE: a cold viewer must understand who is speaking, what happened, and why it matters. Include bridging setup segments — never stitch a payoff to a hook while skipping the sentences in between. If you skip segment ids, the reel will feel random. No pronoun or reference inside the reel may point to a person/thing that only appears in a skipped or un-included sentence.
- OPEN ON A SELF-CONTAINED HOOK: the first segment must stop a thumb — a bold claim, question, number, or stake — AND must NOT depend on an earlier, un-included sentence. Never open on an unresolved reference: a bare pronoun ("She did it...", "They told me...", "He left...", "It changed everything..."), a demonstrative ("This was the moment...", "That's when..."), or a connector ("And so...", "But then...", "Because of that..."). The subject must be introduced by name, role, or clear noun WITHIN the reel. If the hook references something set up earlier, INCLUDE that setup segment so the context is resolved inside the reel. Never open on filler/continuation ("for a very long time I mean...", "After like three maybe..."). If a later line is a stronger opener, lead with it (cold-open) and set "order_mode":"hook_pull" — it must still be understandable with zero prior context.
- END ON A LANDED BEAT: the last segment MUST end on a FULLY finished thought the speaker has delivered — the viewer should feel the idea is complete and nothing is left hanging, NOT that the speaker was about to say more. Never end mid-phrase, while the voice is still rising, on a connective ("and/but/so/that/or"), or on dangling setup ("I felt", "she was", "into my", "a lot of that", "to scope it"). If the thought finishes in the following segment (even one trailing word like "career" after "into my"), INCLUDE that segment so the reel resolves — it is far better to run a few seconds long than to cut a beat early. The reel must END on closure, not a hard cut that sounds like the sentence kept going.
- BLEND if it helps: you may stitch up to ~5 non-contiguous segments into one reel if they genuinely connect and flow when spoken aloud.
- {{LENGTH_RULE}}
- NO FILLER at the edges ("um/uh/you know/and and").
- SELF-CONTAINED and DISTINCT: each reel understandable alone; no two reels covering the same ground.
- ACCURATE: titles/captions must be true to what's said — never embellish or overclaim. Don't build on transcription garble.

# Score each reel (0-100, plus hook/flow/value out of 10).

# For EACH reel, write the full marketing package:
- title (Reel Title): short, engaging, scroll-stopping. Use curiosity/payoff formulas ("Why...", "How she...", "The [surprising] reason...", a stark number) — never a flat description. For series reels, prefix "Part N: ". <=60 chars.
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
      "content_type": "", "series_part": null,
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

    Completeness-first: a finished thought + full context always beats hitting the
    window. The story may justify running a little under the floor or a little over
    the ceiling — but only when those extra seconds buy a complete ending or the
    setup a cold viewer needs.
    """
    lo = int(round(reel_min_seconds()))
    hi = int(round(reel_max_seconds()))
    flex = int(round(REEL_END_TOLERANCE_SECONDS))
    return (
        f"LENGTH: target {lo}-{hi} seconds, and use the FULL range when the story is rich — "
        f"do not crowd everything into short {lo}-{int((lo + hi) / 2)}s clips. "
        f"A COMPLETE, satisfying ending and full opening context ALWAYS beat hitting the window: "
        f"if (and only if) the story demands it, you may run up to ~{flex}s under {lo}s "
        f"or up to ~{flex}s over {hi}s so the thought lands and nothing feels cut off. "
        f"Never end a thought early just to stay under {hi}s, and never pad with filler to reach {lo}s. "
        f"When in doubt, INCLUDE the sentence that finishes the thought rather than cutting on a rising or unfinished line."
    )


# ── Public entry point ─────────────────────────────────────────────────────────

def analyze_with_claude(
    transcript: dict,
    model: str,
    api_key: str,
    progress_cb=None,
    *,
    num_reels: int = DEFAULT_NUM_REELS,
    story_mode: bool = False,
) -> dict:
    """
    Claude reel selection from segmented transcript + optional brand story.
    Returns analysis with editor_cut_sheet per reel and word timestamps attached.
    """
    if num_reels not in VALID_NUM_REELS:
        num_reels = DEFAULT_NUM_REELS

    client = Anthropic(api_key=api_key)
    words = normalize_word_timings(transcript.get("words") or [])
    transcript = {**transcript, "words": words}
    segments = build_sentence_segments(words, float(transcript.get("duration") or 0))
    segmented_text = format_segments_for_claude(segments)
    duration = fmt_time(transcript["duration"])

    _log(progress_cb, f"Built {len(segments)} sentence segments from {len(words):,} words")
    mode_label = "ON (4-5 part series)" if story_mode else "OFF (all standalone)"
    _log(progress_cb, f"Story mode {mode_label}")
    _log(progress_cb, f"Selecting {num_reels} reels with Claude ({model})...")
    selection = select_reels(
        segments,
        story_mode=story_mode,
        num_reels=num_reels,
        model=model,
        client=client,
        segmented_text=segmented_text,
    )
    reels_raw = selection.get("reels") or []
    documentary_summary = str(selection.get("documentary_summary") or "").strip()
    recommendations = normalize_recommendations(selection.get("recommendations") or {})

    reels = [_normalize_claude_reel(row, idx + 1) for idx, row in enumerate(reels_raw)]
    reels.sort(key=lambda r: int(r.get("rank") or r.get("id") or 999))
    for idx, reel in enumerate(reels, start=1):
        reel["id"] = int(reel.get("rank") or idx)
    reels = reels[:num_reels]

    _log(progress_cb, "Crafting brand story...")
    brand = _extract_brand_story(client, model, segmented_text, duration)

    analysis = {
        "reels": reels,
        "brand_story": brand,
        "documentary_summary": documentary_summary,
        "recommendations": recommendations,
        "story_mode": story_mode,
        "segment_count": len(segments),
        "utterance_segments": segments,
        "sentence_segments": segments,
    }
    _normalize_cut_sheets(
        analysis,
        words,
        segments,
        float(transcript.get("duration") or 0),
        story_mode=story_mode,
    )

    _log(progress_cb, "Filling verbatim transcript text for each cut window...")
    _attach_verbatim_for_segments(analysis, transcript)

    _log(progress_cb, "Attaching Rev.ai word timestamps to each reel...")
    _attach_words(analysis, words)

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
) -> dict:
    """Call Claude with the marketing-package analyzer prompt."""
    if num_reels not in VALID_NUM_REELS:
        num_reels = DEFAULT_NUM_REELS

    if client is None:
        if not api_key:
            raise ValueError("api_key or client required for select_reels")
        client = Anthropic(api_key=api_key)

    if segmented_text is None:
        segmented_text = format_segments_for_claude(segments)

    block = STORY_ON_BLOCK if story_mode else STORY_OFF_BLOCK
    system = (
        ANALYZER_PROMPT.replace("{{STORY_MODE_BLOCK}}", block.replace("{{NUM_REELS}}", str(num_reels)))
        .replace("{{ISTV_ACCOUNTS}}", ISTV_ACCOUNTS)
        .replace("{{LENGTH_RULE}}", _length_rule())
        .replace("{{NUM_REELS}}", str(num_reels))
    )
    if _profile_is_v2():
        system += V2_PROMPT_ADDENDUM

    with client.messages.stream(
        model=model,
        max_tokens=12000,
        system=system,
        messages=[{"role": "user", "content": segmented_text}],
    ) as stream:
        full_text = stream.get_final_text()

    text = full_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    parsed = _parse_json_object(text)
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

def _extract_brand_story(
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

    with client.messages.stream(
        model=model,
        max_tokens=6000,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        full_text = stream.get_final_text()
    result = _parse_json(full_text, "brand_story")
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
    rows: list[dict], min_total: float, transcript_end: float
) -> list[dict]:
    if not rows or transcript_end <= 0:
        return rows
    deficit = min_total - _playback_total_seconds(rows)
    if deficit <= 0.02:
        return rows
    last = rows[-1]
    a = _float_safe(last.get("start_time_seconds"))
    b = _float_safe(last.get("end_time_seconds"))
    room = transcript_end - b
    if room <= 0.02:
        return rows
    last["end_time_seconds"] = min(b + deficit, b + room, transcript_end)
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
) -> None:
    """Build word-accurate cut sheets from v2 sentence segment picks."""
    tmax = max(0.0, float(transcript_duration or 0.0))
    words = normalize_word_timings(words)
    protect_ends = True
    max_dur = reel_max_seconds()
    min_dur = reel_min_seconds()
    soft_dur = max_dur + REEL_END_TOLERANCE_SECONDS

    for reel in analysis.get("reels", []):
        order_mode = normalize_order_mode(reel.get("order_mode") or reel.get("assembly_mode"))

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
        if _playback_total_seconds(sheet) < min_dur and len(sheet) == 1 and tmax > 0:
            sheet = _maybe_extend_playback_rows(sheet, min_dur, tmax)
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
                    if we < start or ws > end:
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
                if we < start or ws > end:
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
        raise ValueError(f"Claude returned no valid JSON.\nResponse preview:\n{text[:500]}")
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
        raise ValueError(
            f"Claude returned no valid JSON for '{expected_key}'.\n"
            f"Response preview:\n{text[:500]}"
        )
    parsed = json.loads(match.group())
    # Unwrap top-level key if present
    if expected_key in parsed:
        return parsed[expected_key]
    return parsed


def _log(cb, msg: str):
    if cb:
        cb(msg)
