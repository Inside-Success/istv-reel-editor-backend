# Reel Story & Cut Logic — Pipeline Reference

How a raw Rev.ai transcript becomes a set of scored, captioned, exportable
"reels." Every prompt quoted here is copy-pasted verbatim from source (line
references given) — nothing paraphrased — so you can diff this against the
code if either drifts.

## Pipeline at a glance

```
Rev.ai audio job
  → src/transcription.py           parse into word list (index/word/start/end/speaker)
  → src/transcript_cleanup.py      Claude: fix ASR mishears (1:1 token swap only, timing untouched)
  → src/transcript_segments.py     group words into numbered sentence segments
  → src/analyzer.py                Claude: pick reels BY SEGMENT ID (the core "story" prompt)
  → src/cutter.py                  segment ids → precise start/end seconds (padding, dangling-end trim, context bridging)
  → src/analyzer.py                attach verbatim text + word timestamps to each reel
  → src/caption_builder.py         remap word timings to playback timeline, build karaoke caption blocks
  → export_pipeline.py / media.cjs render final MP4 (see theupdatelog.md for that half)
```

Claude is called at **three** points: transcript cleanup (per-chunk, best-effort),
reel selection (the main "story" call), and brand-story extraction. All three
now retry on transient failures / malformed JSON (`_call_with_retries` in
`src/analyzer.py`, added this session).

---

## 1. Transcription (`src/transcription.py`)

`transcribe_audio()` submits to Rev.ai (`language="en"`, diarization and
punctuation both on) and polls to completion. `_parse()` (lines 58–90)
converts Rev.ai's `monologues[].elements[]` into the app's word format:

```python
{
  "index": 0,          # 0-based, counts only text elements (not punctuation)
  "word": "Today",
  "start": 0.25,        # Rev.ai "ts"
  "end": 0.65,          # Rev.ai "end_ts"
  "confidence": 0.98,
  "speaker": 0,
}
```

Punctuation elements are folded into `full_text` but never become their own
word entry. Disfluencies ("um"/"uh") are **kept** by default — `remove_disfluencies`
defaults to `False` — so filler removal happens later, in the UI/caption layer,
not by discarding data at transcription time.

---

## 2. Transcript cleanup (`src/transcript_cleanup.py`)

`correct_transcript_words()` sends the transcript to Claude in chunks of
`CHUNK_TOKENS = 220` words (with `CONTEXT_TOKENS = 25` words of surrounding
context that aren't corrected, just shown for reference). This is a **1:1 token
substitution only** — never adds/removes/splits/merges words, so timing stays
exact. Exact system prompt (`_CLEAN_SYSTEM`, lines 17–34):

```
You fix automatic-speech-recognition (ASR) errors in an interview transcript.

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

Return ONLY valid JSON: {"fixes": [{"i": <index>, "w": "<corrected token>"}]}
```

The per-chunk user message adds a speaker-name hint when available: `"Speaker
name (spell exactly like this when it appears): {speaker_name}\n\n"`. A fix is
only applied if it's a single token (no spaces/tabs) and actually differs from
the original word. If a chunk's Claude call fails even after retry
(`_CHUNK_RETRY_ATTEMPTS = 2`), that chunk is skipped — cleanup is best-effort,
never blocks the pipeline.

---

## 3. Sentence segmentation (`src/transcript_segments.py`)

`build_sentence_segments()` turns the flat word list into numbered segments —
the atomic unit Claude actually picks from. A segment breaks on:
- **speaker change**
- **long pause** (gap > 0.65s)
- **capitalized word after a gap > 0.35s** (sentence-boundary heuristic) —
  unless the next word is in `ALWAYS_CAP = {"I", "I'm", "I'll", "I've", "I'd"}`
  (these don't end a sentence just because they're capitalized)
- **too long** (> 12.0s or ≥ 40 words)

1-word fragments get merged into a neighboring same-speaker segment
(`_merge_micro_fragments`) so Claude never sees a stray isolated word as its
own pick-able unit.

Each segment carries `id`, `start`, `end`, `text` (verbatim, space-joined, no
punctuation), plus `word_start_index`/`word_end_index` linking back to the
original word list, and `speaker`.

`format_segments_for_claude()` renders this as the exact text block Claude
sees — this is the entire "transcript" from Claude's point of view for reel
selection:

```
[0] start=0 end=2.5 "I started building this when I was 22 years old"
[1] start=2.65 end=5.12 "And I had no idea what I was doing"
[2] start=5.3 end=9.8 "But I knew I had to try something different"
```

Claude **only ever sees and outputs segment ids** — never raw seconds. All
timing precision comes later, in `cutter.py`, from the id list Claude returns.

---

## 4. Reel selection — the core "story" prompt (`src/analyzer.py`)

This is the prompt that actually decides what becomes a reel, in what order,
and why. `select_reels()` builds it from four pieces stitched together via
placeholder substitution: `ANALYZER_PROMPT` with `{{STORY_MODE_BLOCK}}`,
`{{ISTV_ACCOUNTS}}`, `{{LENGTH_RULE}}`, and `{{NUM_REELS}}` all substituted in.

### 4a. Story mode block

**OFF** (`STORY_OFF_BLOCK`, lines 87–88) — the default (`story_mode=False` is
what `generate_reels.py` and the backend both actually call with today):

```
STORY MODE: OFF.
Generate all {{NUM_REELS}} reels as INDEPENDENT, standalone reels — each self-contained and covering a DIFFERENT moment from the documentary. No "Part N" titles. Set "series_part": null on every reel.
```

**ON** (`STORY_ON_BLOCK`, lines 90–96) — turns 4-5 of the N reels into a
sequential, postable-in-order mini-series while the rest stay standalone:

```
STORY MODE: ON.
Among the {{NUM_REELS}} reels, designate 4-5 of them as a SEQUENTIAL SERIES that tells one connected story across multiple posts:
- Pick consecutive, naturally connected story beats so each part leads into the next (Part 1 sets it up, the middle parts build, the last resolves).
- Title them "Part 1: ...", "Part 2: ...", etc., and set "series_part": 1, 2, 3... in order.
- They are meant to be posted in sequence — reflect that in best_posting_time (consecutive days/slots).
The REMAINING reels (to reach {{NUM_REELS}} total) are INDEPENDENT standalone reels covering different moments; set "series_part": null on those.
All {{NUM_REELS}} reels together must still cover DIFFERENT parts of the documentary — standalone reels must not repeat what the series covers.
```

**To verify which one is active for a given run:** grep the `story_mode` kwarg
at the `analyze_with_claude(...)` / `select_reels(...)` call site you're
checking — it's not an env var, it's passed explicitly per call.

### 4b. Length rule (`_length_rule()`, lines 184–203)

Built dynamically from `reel_min_seconds()` / `reel_max_seconds()` (from
`cutter.py`, env-overridable — see §8) and `REEL_END_TOLERANCE_SECONDS = 10.0`.
With defaults (30s/90s), it renders as:

```
LENGTH: target 30-90 seconds, and use the FULL range when the story is rich — do not crowd everything into short 30-60s clips. A COMPLETE, satisfying ending and full opening context ALWAYS beat hitting the window: if (and only if) the story demands it, you may run up to ~10s under 30s or up to ~10s over 90s so the thought lands and nothing feels cut off. Never end a thought early just to stay under 90s, and never pad with filler to reach 30s. When in doubt, INCLUDE the sentence that finishes the thought rather than cutting on a rising or unfinished line.
```

The philosophy stated explicitly in code comments: **completeness beats
hitting the target window** — a reel is allowed to run outside 30–90s if
that's what a clean hook/landing requires.

### 4c. Full analyzer prompt (`ANALYZER_PROMPT`, lines 98–165)

```
You are a Social Media Strategist, Growth Marketer, and Content Director for ISTV. You turn a documentary into a complete short-form marketing package. You do NOT think like a clipping tool.

# Input
The transcript (in the user message) is pre-split into numbered SENTENCE segments:
[id] start=<sec.dec> end=<sec.dec> "one full sentence, verbatim"
You SELECT WHOLE SEGMENTS BY ID, in playback order. You never invent timestamps and never start or end inside a segment. Times are resolved downstream from the ids you choose.

# Who these reels are for
ISTV is an interview-documentary company. This subject is a PAYING CLIENT — a founder, entrepreneur, lawyer, doctor, woman entrepreneur, veteran-turned-CEO, or similar professional — and THEY will post these reels to THEIR OWN social accounts. The reels are this person's personal brand, not generic clips. Two consequences:
- Every reel must be something the subject would be PROUD to attach their name to and share: it should make them look credible, human, and worth following.
- Across the set, a viewer should come away understanding both WHO this person is (their story, values) AND WHAT they do (their company, work, or field). Aim for at least a few reels that touch on their business or work — drawn only from what they actually said, never invented.

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
- END ON A LANDED BEAT: the last segment MUST end on a FULLY finished thought the speaker has delivered — the viewer should feel the idea is complete and nothing is left hanging, NOT that the speaker was about to say more. Never end mid-phrase, while the voice is still rising, on a connective ("and/but/so/that/or"), or on dangling setup ("I felt", "she was", "into my", "a lot of that", "to scope it"). If the thought finishes in the following segment (even one trailing word like "career" after "into my"), INCLUDE that segment so the reel resolves — it is far better to run a few seconds long than to cut a beat early. The reel must END on closure, not a hard cut that sounds like the sentence kept going.
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
You output segment ids only — never seconds.
```

`{{ISTV_ACCOUNTS}}` (lines 79–85) — the fixed niche-show roster Claude picks
exactly one of:

```
- istv-people — broad human-interest: personal journeys, origin stories, deeply human and relatable moments. The widest net.
- istv-legacymakers — founders/leaders building something enduring: a brand, institution, or movement meant to last.
- istv-operationceo — ONLY for military/army veterans, armed-forces service members, or people who transitioned from active service into entrepreneurship. Do NOT use for regular CEOs or operators who never served.
- istv-womeninpower — women founders, leaders, and changemakers.
- istv-mompreneurs — mothers building businesses; the intersection of parenthood and entrepreneurship.
```

### 4d. V2 quality-bar addendum (`V2_PROMPT_ADDENDUM`, lines 170–177)

Appended to the prompt only when `REEL_PROFILE` resolves to v2 (`_profile_is_v2()`,
checks env var values `"v2"`/`"2"`/`"updated_v2"` — this is the profile
`generate_reels.py`'s `DEFAULT_PROFILE` actually sets, so it's on by default
for the CLI batch path):

```
# V2 QUALITY BAR (strict — this set is judged for post-readiness)
- HARD HOOK: the FIRST spoken line must be a complete, standalone sentence that stops the scroll on its own. Never start mid-sentence or on a fragment ("Was the first girl...", "amazing is...", "Most importantly is..."). If the strongest hook is a few lines in, lead with it.
- CLEAN LANDING: the LAST spoken line must complete the thought with finality — it should feel like a deliberate closing line, the kind that earns a beat of silence after it, never like the speaker was interrupted or had more coming. Never end on a preposition/filler ("...to", "...I would say", "...you know"), a trailing clause, or a line that only makes sense if the next sentence follows. If finishing the thought needs one more sentence, include it even if the reel runs slightly over the target.
- ZERO REPETITION: never include two segments that say essentially the same thing. Pick the single strongest phrasing.
- ON-TOPIC ONLY: drop any aside, tangent, or self-referential line (e.g. "in my videos and my contents") that does not serve THIS reel's single idea.
- TIGHT: every included segment must earn its place; if removing it doesn't hurt the arc, remove it.
```

### 4e. Valid content types (`VALID_CONTENT_TYPES`)

```
emotional moment, turning point, powerful statement, inspirational moment,
educational insight, highly shareable moment, strong hook
```
Any value Claude returns outside this set gets coerced to `"strong hook"` by
`_normalize_claude_reel`.

### 4f. Retry wrapper

Both the reel-selection call and the brand-story call (§6) are wrapped in
`_call_with_retries()` (lines 48–67): up to 3 attempts, exponential backoff
(2s, 4s, ...), retrying on `APIConnectionError`, `APITimeoutError`,
`RateLimitError`, `InternalServerError`, and malformed-JSON errors
(`ValueError`/`JSONDecodeError`) — since Claude's non-deterministic sampling
often produces valid JSON on a same-prompt retry.

---

## 5. Cut-boundary math (`src/cutter.py`)

Claude only returns **segment ids**. This module is what turns those ids into
the actual `start_time_seconds`/`end_time_seconds` pairs baked into the
`editor_cut_sheet`, including all the "don't cut mid-word" polish. Key knobs:

| Constant | Value | Purpose |
|---|---|---|
| `MIN_REEL_SECONDS` / `MAX_REEL_SECONDS` | 15.0 / 60.0 (defaults; `generate_reels.py` overrides to 30/90 via env) | soft target floor/ceiling |
| `REEL_END_TOLERANCE_SECONDS` | 10.0 | how far a reel may run past the ceiling to land cleanly |
| `LEAD_PAD_SECONDS` / `TAIL_PAD_SECONDS` | 0.10 / 0.45 | padding added before first word / after last word of a cut |
| `NATURAL_TAIL_PAUSE` | 0.28 | breath room after speech ends |
| `CONTINUATION_GAP_SEC` | 0.42 | max gap to pull in a trailing word that's really part of the same phrase |
| `MAX_WORD_CONTINUATION` | 6 | cap on how many extra words can be pulled in this way |
| `MAX_REEL_SPANS` | 5 | max number of non-contiguous stitched clips per reel |
| `MAX_CONTEXT_BRIDGE_SEGMENTS` | 4 | max skipped segments auto-inserted to bridge a gap |
| `MAX_CONTEXT_HEAD_PREPENDS` | 2 | max setup sentences auto-prepended before a dependent opener |

`build_reel_cut_sheet()` runs, per reel, roughly in this order:
1. Extract segment ids from Claude's output; dedupe near-repeats (v2 only).
2. Sort by `order_mode` (`chronological`, or leave as-is for `hook_pull`
   cold-opens).
3. **Context-head extension** — if the first segment opens on a dependent
   reference (pronoun/connector — see `CONTEXT_OPENER_WORDS` vs
   `SELF_CONTAINED_OPENERS`), prepend the prior segment(s) that resolve it.
4. **Bridge gaps** — fill in skipped segments between two picks if they're
   close enough to still fit the length budget.
5. **Extend dangling tails** — if the reel ends on an incomplete thought
   (`DANGLING_END_WORDS`, `DANGLING_END_PHRASES`, `INCOMPLETE_END_WORDS` — e.g.
   "and", "into my", "-ing" verbs), pull in the next segment so it resolves.
6. Cap to max length, then re-check the tail-extension (a cap could re-expose
   a dangling end), then trim any tail that's still weak.
7. Split into contiguous runs (playback spans), resolve each run's precise
   padded start/end time (`_resolve_run`), including the continuation-word
   pull-in and an "emotional landing" clamp that trims stray trailing
   connector words the tail padding accidentally swept in.
8. Assign roles: first row = `HOOK`, last row (if more than one) = `PAYOFF`,
   everything between = `BODY`.

If you want to sanity-check a specific reel's cut, this is the file to read
line-by-line against `STRONG_END_WORDS` / `INCOMPLETE_END_WORDS` /
`DANGLING_END_PHRASES` — those word lists are literally the rulebook for "does
this cut land cleanly."

---

## 6. Brand story extraction (`src/analyzer.py`, `_extract_brand_story`)

A separate Claude call (also wrapped in `_call_with_retries`) that isn't
reel-selection at all — it produces a founder/brand narrative package for the
marketing doc (`src/marketing_doc.py` renders this into the `.docx`/`.html`
output). Asks for, in one JSON response:
- SEO headline (55–65 chars) + meta description (150–160 chars)
- A 250–375 word first-person brand story (1.5–2.5 min at 150wpm), structured
  as 5 fixed paragraphs: Hook/Origin → World Before → Leap & Struggle →
  Breakthrough → Today & Vision
- 8–12 SEO keywords
- A **documentary cut sheet** — 4–7 rows forming a 90–150 second highlight
  reel (hard rule: the summed row durations must fall in that range),
  separate from the individual reels above.

This doesn't affect reel selection/timing at all — it's a parallel output for
the marketing document, built from the same segmented transcript.

---

## 7. Attaching text/words back to reels (`src/analyzer.py`)

Once cut sheets exist:
- `_attach_verbatim_for_segments()` — stamps each cut-sheet row with the
  verbatim transcript text covering that time window.
- `_attach_words()` — collects every Rev.ai word whose time falls inside a
  reel's cut-sheet windows into `reel["timestamped_words"]`, deduplicated by
  `(rounded_time, word)` (matters at stitch points where two spans could
  otherwise double-count a word).

---

## 8. Playback timeline + karaoke captions (`src/caption_builder.py`)

`build_playback_words()` is the piece that makes stitched (non-contiguous)
reels play back as one continuous clip instead of jumping around: for each
cut-sheet span, it remaps every word's original transcript time to a
**playback-local time** (`localTime = running_offset + (word.start - span.start)`),
accumulating `running_offset` by each span's duration as it goes. This is
what the desktop editor's word-highlight subtitles and the final karaoke
burn-in are both driven by.

`correct_speaker_name()` re-applies the speaker's correct spelling to any
word Rev.ai/cleanup still got wrong, using `REEL_SPEAKER_NAME` (exact
spelling) and `REEL_NAME_ALIASES` (explicit `wrong=Right` pairs, built by
`generate_reels.py`'s `detect_name_aliases()` via fuzzy string matching,
threshold 0.7) — falls back to a 0.72-threshold fuzzy match if no explicit
alias covers a given mis-transcription.

`make_caption_blocks()` groups playback words into chunks (`REEL_CAPTION_CHUNK`
env var, default 2 words) for the karaoke display, always starting a new
block on a speaker change.

---

## 9. Profiles & environment knobs (`generate_reels.py`)

`DEFAULT_PROFILE` (applied at the start of every CLI run via `apply_profile()`):

| Env var | Default | Effect |
|---|---|---|
| `REEL_PROFILE` | `v2` | turns on the V2 quality-bar addendum (§4d) |
| `REEL_CONTEXT_AWARE` | `1` | enables auto-prepending setup sentences (§5 step 3) |
| `REEL_MIN_SECONDS` / `REEL_MAX_SECONDS` | `30` / `90` | the length window fed into `{{LENGTH_RULE}}` |
| `REEL_CAPTION_CHUNK` | `4` | karaoke words-per-block (CLI default overrides caption_builder's own default of 2) |
| `REEL_TEXT_OVERLAYS` | `0` | on-screen text-hook/speaker-name overlays during export |

`--name`/`--title` CLI flags feed `detect_name_aliases()` and, downstream,
`REEL_SPEAKER_NAME`/`REEL_NAME_ALIASES`/`REEL_SPEAKER_TITLE` for caption
correction and (if enabled) on-screen name overlays.

The desktop app's backend (`backend/app.py`) calls `analyze_with_claude(...,
story_mode=False)` directly — always standalone reels, never the story-series
mode, regardless of `generate_reels.py`'s CLI defaults. If you want story mode
for a desktop-driven job, that's the one call site that would need a real
code change (not just an env var) to expose it.

---

## Where to look if something seems off

| Symptom | Look at |
|---|---|
| Reel opens on a confusing pronoun/fragment | `ANALYZER_PROMPT`'s "OPEN ON A SELF-CONTAINED HOOK" rule, then `CONTEXT_OPENER_WORDS`/`SELF_CONTAINED_OPENERS` in `cutter.py` |
| Reel ends mid-sentence | "END ON A LANDED BEAT" rule, then `DANGLING_END_WORDS`/`DANGLING_END_PHRASES`/`STRONG_END_WORDS` in `cutter.py` |
| Reel too short/long vs. expectation | `_length_rule()` output + `REEL_MIN_SECONDS`/`REEL_MAX_SECONDS`/`REEL_END_TOLERANCE_SECONDS` |
| Wrong/missing series structure | confirm `story_mode` at the actual call site — desktop backend always passes `False` |
| Speaker name misspelled in captions | `REEL_SPEAKER_NAME`/`REEL_NAME_ALIASES`, `detect_name_aliases()`, `correct_speaker_name()` |
| A clearly-wrong ASR word slipped through | `_CLEAN_SYSTEM` prompt scope (only fixes "clearly misheard" words, on purpose leaves ambiguous ones) |
| Claude picked a duplicate/repetitive segment | only guarded against under V2 profile ("ZERO REPETITION" rule + `_dedupe_repeated_segments`) |
