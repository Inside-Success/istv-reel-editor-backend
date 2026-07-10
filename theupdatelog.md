# The Update Log

Running changelog of work done on this project via Claude Code, starting from
the initial clone. Newest entries go on top. Keep adding a new dated section
each session instead of editing old ones.

---

## 2026-07-10 (evening) — Real-footage iteration loop: found and fixed the actual multi-speaker/dangling-ending root causes

After the punctuation/bridge/end-credits round earlier today, real exports
still showed the same problems. Instead of another prompt-wording pass, ran
the actual analyzer+cutter against two real interviews end-to-end
(`generated_data/prompt_iteration_2026-07-10/`) — David Christensen (real
Rev.ai transcription, ~24 min) and Patricia Gallagher (an existing
sentence-level transcript from a different tool, converted to our word-level
format with synthetic per-word timing so no Rev.ai re-run was needed) — up to
5 attempts each, reviewing actual Claude picks and real segment/word data
after every run, fixing the root cause found, and re-running. Every attempt's
raw Claude response, resolved analysis, and an automated scorecard
(speaker-purity + dangling-ending checks) are saved under each
`<video>/attempt_N/` folder. Final result: **0 validator issues on both
videos** (down from 3 multi-speaker + 2 dangling-ending issues on Patricia's
first attempt), reels exported to `FINAL/<video>/exported/`.

**Root causes found this round (all in `src/analyzer.py` unless noted) — the
mechanical single-speaker filter and prompt rules from earlier today were
real, but every one of these ran AFTER them and silently undid their work:**

1. **`_maybe_extend_playback_rows`** (the floor-padding step that stretches
   short reels toward 45s) walked forward through segments by TIME only,
   with no speaker check at all, and wrote straight to the row's
   `end_time_seconds` without ever touching `reel["segment_ids"]` — so the
   segment-id list looked perfectly single-speaker while the actual
   rendered clip played straight through a different speaker's segment.
   Added a `main_speaker_id` parameter and a `ceiling` that stops padding at
   the start of the next different-speaker segment.
2. Once padding respected the speaker ceiling, it still stopped the instant
   the numeric floor was satisfied, with no check for whether that landing
   segment was actually a complete thought — reproduced live: a reel ended
   on "...give me a male's mentality," (a comma mid-list) instead of the
   next segment, "but a woman's fortitude." Fixed to keep walking through
   same-speaker segments until landing on a clean one, and to fall back to
   the original (already-clean) boundary rather than accept a dangling one
   if nothing further ahead is clean.
3. **Zero-gap speaker boundaries**: when a same-speaker segment ends with NO
   gap before a different speaker's segment (a real interruption in the
   source audio — reproduced live twice, in both directions), the computed
   boundary landed EXACTLY on the foreign word's start/end time, and
   `_attach_words()`'s inclusion check (`ws > end` / `we < start`) was
   inclusive of that exact tie, letting one foreign word slip through
   despite every speaker guard. Fixed the boundary math to leave a real gap
   (`NEXT_WORD_GUARD_SEC`) and hardened `_attach_words()` / the identical
   helper `verbatim_from_words()` (`src/transcript_snippets.py`) to treat
   the window as properly exclusive on both ends.
4. **`_extend_end_through_continuation_words`** (`src/cutter.py`, word-level
   tail padding) strips punctuation before checking `DANGLING_END_WORDS` —
   so `"do."` (already a complete sentence) collapses to the bare token
   `"do"`, which IS in that list, and the cut got pulled into the next
   sentence anyway ("...you can do." → "...you can do. So the vision
   of..."). Now breaks immediately if the current word already ends with
   terminal punctuation — punctuation always wins over the bare-token
   heuristic.
5. **`select_reels()` / `correct_transcript_words()`** had no client-level
   timeout — a stalled streaming response (seen live, 25+ minutes with zero
   bytes and no error) hung indefinitely with no retry, which in the real
   app looks like the pipeline being permanently stuck. Added a bounded
   `timeout=` on the Anthropic client (600s for selection, 120s for
   cleanup chunks) so a stall surfaces as a retryable `APITimeoutError`
   instead.
6. **`select_reels()` / `analyze_with_claude()`** gained an optional
   `raw_response_cb` so every attempt's exact raw Claude response can be
   archived, independent of how it gets parsed — this is what let every
   attempt in this round save its literal `claude_raw_response_N.txt`.
7. **`src/validate.py`** had its own hand-rolled, drifted copy of the
   dangling-ending check (missing today's punctuation-awareness) and no
   speaker check at all — now imports `_segment_text_dangles` directly from
   `src/cutter.py` and gained a `_multi_speaker_issue()` check, so this
   round's scorecards were checking the real thing.
8. **`export_pipeline.py`**: exporting many reels concurrently from one
   large source file (Patricia's is ~95 minutes) let a handful of ffmpeg
   processes produce a valid-but-empty MP4 (0 media frames, exit code 0)
   under I/O contention — reproduced live, 6 of 15 reels came back as
   261-byte empty containers. Added a same-payload retry (with a timeout
   safety net so a slow retry can't crash the whole batch uncaught, and a
   brief re-check before declaring "too small" to rule out a Windows
   filesystem-flush race) to `export_reel_mp4` / `export_reel_mp4_ex`.

All fixes were verified two ways: targeted regression tests built directly
from the exact real segment/word data that exposed each bug (not synthetic
guesses), and full end-to-end re-runs against both real interviews.

**Final results**, in `generated_data/prompt_iteration_2026-07-10/`:
- **David Christensen**: 15/15 reels selected clean (0 validator issues) and
  exported successfully — `FINAL/david/exported/`.
- **Patricia Gallagher**: 15/15 reels selected clean (0 validator issues) —
  the selection/analysis logic is fully validated — but only 4/15 could
  actually be rendered to video. Found (via direct ffmpeg testing, confirmed
  independent of this tool's code) that this specific copy of her source
  file cannot be decoded past ~25 minutes in, via any seek strategy —
  a structural defect in the source video itself, not a bug in the prompt,
  analyzer, cutter, or export pipeline. Details and a path forward in
  `FINAL/patricia/KNOWN_ISSUE_source_video.txt`.
- Every attempt's raw Claude response, resolved analysis, and scorecard for
  both videos are preserved under `<video>/attempt_1..5/` for traceability.

Also fixed along the way: `export_pipeline.py`'s export retry didn't handle
`subprocess.TimeoutExpired` (a slow retry could crash an otherwise-successful
15-reel batch uncaught) and had a possible false-positive "too small" result
on a Windows filesystem-flush race — both patched with a proper catch +
brief re-check before giving up.

---

## 2026-07-10 (later) — Real punctuation loophole fixed, memorized-phrase bridge check fixed, end credits (export-only), project-wide bug sweep

After the single-speaker/look-ahead/tiered-length round above, real footage
still showed abrupt endings. Went first-principles through the whole
transcription -> segmentation -> cutting pipeline instead of tuning prompt
wording again.

**The actual root cause of the ending problem:** `_parse()` in
`src/transcription.py` captured Rev.ai's punctuation elements (periods,
question marks, etc.) only for `full_text` — they were NEVER attached to the
individual word tokens that sentence segments and Claude's prompt input are
built from. That means every completeness check in the entire pipeline
(`_segment_text_dangles()`'s "ends with `.!?`" check, and everything Claude
itself read) has been working with **zero punctuation, always** — the single
most reliable "is this ending actually finished" signal was silently dead
from day one. Every previous round of endings fixes (word lists, tolerance
constants, speaker checks) was patching around a completeness detector that
never had real information to work with.

**Fix:**
- `_parse()` now attaches Rev.ai's punctuation mark onto the word it follows
  (`words[-1]["word"] += mark`), so word text, sentence segment text, what
  Claude sees in the prompt, and burned-in captions all carry real
  punctuation for the first time.
- `_segment_text_dangles()` (`src/cutter.py`): now that punctuation is real,
  flipped the previously-unreached fallback case — text with no terminal
  punctuation AND no other completeness signal now defaults to "dangling"
  instead of "fine". Before, punctuation could never fire so this path was
  silently permissive; now it's the trustworthy general signal it should
  always have been. Only triggers the existing look-ahead-then-retreat
  machinery, never an outright drop.
- Added a floor guard in `_trim_dangling_tail_ids` so it can never retreat a
  reel down to zero segments even in a pathological case.
- Found and fixed a second, related memorized-phrase bug:
  `_tail_segment_resolves_bridge` (`src/cutter.py`) matched a fixed phrase
  list ("my career", "had to leave", etc. — leftovers from one specific past
  transcript) *anywhere* in the combined text of two segments, not at the
  actual boundary between them. Caught this live in testing: an unrelated
  earlier mention of "my career" inside segment 0 falsely satisfied the
  check and blocked a genuinely incomplete segment 1 from being trimmed.
  Rewrote it to check only the real seam (last word of the prior segment vs.
  first word of the next), reusing the existing general `_word_pair_continues`
  check instead of a second memorized list.
- Added one more `_extend_resolved_tail_ids` pass after the final trim in
  `build_reel_cut_sheet`, so retreating from a bad cap-induced ending gets a
  chance to look forward again into whatever room the retreat just freed up.
- Verified with synthetic word-level tests: multi-speaker interjection
  excluded, genuinely incomplete trailing clause correctly retreats, a
  genuinely complete trailing sentence is kept, and a short dangling segment
  correctly extends into a next segment that completes it.

**New feature — optional end credits (export-time only):** Export dialog
gained a checkbox + file picker ("Add end credits clip"). If checked, the
chosen video is appended after the reel content, but **only in the rendered
output file** — the editor timeline, segments, and reel data are never
touched, exactly as requested. Implementation:
- `export/media.cjs`: `exportReel` accepts an optional `endCreditsPath`.
  Captions are burned onto the main reel content first (`[vreel]`), then the
  credits clip is scaled+padded (never cropped — it's a designed asset, not
  raw footage) to match the export canvas and concatenated on afterward in
  the same single ffmpeg pass (no second encode). Handles credits clips with
  no audio track (synthesizes silence via `anullsrc`) and normalizes both
  sides to stereo/48kHz before the concat (ffmpeg's concat filter requires
  matching audio layouts).
- `export_pipeline.py` / `desktop/src/main/export.js`: thread
  `endCreditsPath` through from the export dialog's `options`, only applied
  when both the checkbox is on and a file is chosen.
- `desktop/src/main/main.js`: new file-picker IPC channel
  (`export:pick-end-credits`) mirroring the existing camera/music pickers.
- Verified end-to-end with real ffmpeg smoke tests: credits-with-audio,
  credits-without-audio, and the no-credits path (regression check) all
  produce correct combined-duration output.

**Bug sweep (before and after implementing end credits, as requested):**
dispatched a parallel Python + Electron audit, then fixed the clearest real
findings:
- `export_pipeline.py`: the marketing-doc/zip filename stem was computed via
  `video_stem.split("_")[1]`, which silently discarded most of the filename
  for any source video with more than one underscore in its name (e.g.
  "Jane_Doe_Interview.mp4" produced the stem "Doe"). Now uses the full stem.
- `backend/job_store.py`: `purge_older_than()` deleted rows purely by age,
  including still-in-flight jobs — combined with running before `load_all()`
  in `_bootstrap_jobs`, a job whose `updated_at` hadn't ticked in 24h+
  (backend down over a weekend, a crash mid-run) could get silently deleted
  before it ever got a chance to resume; the client just saw a 404. Now
  excludes in-flight statuses from the purge regardless of age.
- `desktop/src/renderer/renderer.js`: `wordsInSegments()` stored a word's
  global `index` with no fallback, unlike `model.js`'s `visibleWords()` and
  `export.js`'s `toExportReel()`, both of which already defend against a
  missing index. `buildSubEditor()` uses that index directly as the
  subtitle-edit key with no fallback of its own — if `index` were ever
  missing, every word in a reel would collapse onto the same edit key.
  Matched the existing fallback pattern.
- `desktop/src/renderer/renderer.js`: `generateReels()` / `retrySelectionOnly()`
  had no guard against a project switch mid-flight — since there's no backend
  job cancellation, opening a different project while a generate/retry call
  was still in flight would let the stale call's result silently overwrite
  the new project's transcript/reels once it finally resolved. This is the
  same bug class as the "opening a new project didn't reset state" issue
  fixed earlier — now both calls snapshot the project they belong to and
  discard their result if a different project is open by the time they
  resolve.
- `desktop/src/main/export.js`, `desktop/src/main/sync.js`: error messages
  surfaced to the user were truncated to the last 400 chars of ffmpeg/python
  stderr, which for filter-graph failures often cuts off the actual cause in
  favor of a generic tail line. Raised to 1200 chars (matching media.cjs's
  own internal truncation).

Restarted the backend and relaunched the desktop app to pick up all of the
above.

---

## 2026-07-10 — Single-speaker enforcement (found a major loophole), look-ahead endings, tiered length priority

Three requests: (1) reels should only ever contain the main speaker — no
interviewer, employee, spouse, or friend's voice; (2) endings should
explicitly look at the next sentence to decide whether to extend or retreat,
rather than just stopping wherever; (3) 45-90s as first priority, 30-110s as
a lower-priority fallback (still not a hard rule).

**Found the real loophole for #1:** `format_segments_for_claude()`
(`src/transcript_segments.py`) never included speaker identity in the text
shown to Claude — segments looked like `[id] start=X end=Y "text"` with no
speaker field at all. Claude was never given a way to tell who said what, so
it had no mechanism to follow a "main speaker only" rule even if told to —
every fix aimed at multi-speaker issues until now only patched the mechanical
cutter layer, not the root cause of Claude's own selection being speaker-blind.
Also found `speaker_prompt_summary()` (`src/transcription.py`) — a function
that computes per-speaker word share — was written but never called anywhere.

**Fix:**
- `format_segments_for_claude()` now includes `speaker=<n>` per segment line.
- New `_identify_main_speaker()` (`src/analyzer.py`) picks the speaker with
  the most total speaking TIME across segments — the presumed documentary
  subject.
- New `# ONE SPEAKER ONLY` hard rule in `ANALYZER_PROMPT`, with the actual
  identified speaker id substituted in (`{{MAIN_SPEAKER_ID}}`) so it's
  concrete, not abstract — explicitly telling Claude to check the `speaker=`
  field before selecting any segment, and that everyone else (interviewer,
  other interviewees, reactions) is excluded except genuine voice-over
  narration (which essentially never applies here).
- Mechanical safety net in `_normalize_cut_sheets`: after Claude returns
  segment ids, any id belonging to a different speaker than the identified
  main subject gets dropped before the cut sheet is built — guarantees
  compliance even if Claude's selection imperfectly follows the prompt (only
  applied if it leaves at least one segment, so an imperfect reel doesn't
  become an empty one).

**Fix for #2 (look-ahead endings):** added an explicit "THE LOOK-AHEAD CHECK"
procedure to the "END ON A LANDED BEAT" rule: before finalizing a reel, check
the next segment (same speaker) — if it resolves the thought, extend into it;
if it doesn't, don't end on the borderline segment either, retreat to the
last segment that was already a clean, fully landed thought. Frames ending
choice as a deliberate check, not a default stop-here.

**Fix for #3 (tiered length priority):** `REEL_FLOOR_TOLERANCE_SECONDS = 15.0`
added in `src/cutter.py`, separate from the ceiling's `REEL_END_TOLERANCE_SECONDS = 20.0`
— asymmetric on purpose (45-15=30 vs 90+20=110, matching the user's exact
numbers). `_length_rule()` rewritten to state an explicit priority order:
45-90s first priority, 30-110s second priority ("reach for it only when
45-90s truly isn't enough"), rather than presenting both ranges as equally
casual. `_normalize_cut_sheets`'s floor-extension trigger (`soft_min`) now
uses the new floor-specific tolerance instead of reusing the ceiling one.

**Verified:** `_identify_main_speaker` correctly picked the dominant speaker
in a synthetic 2-speaker transcript; the mechanical filter correctly dropped
an interviewer's segments from a mixed-speaker selection while keeping the
main speaker's, producing a valid 3-row cut sheet from only the correct
speaker's content (first attempt showed an empty result — traced to empty
test word data breaking downstream row-resolution, not the filter itself;
confirmed correct once realistic word data was used); full prompt
placeholder substitution confirmed clean end-to-end, including the new
`{{MAIN_SPEAKER_ID}}` token resolving correctly; legacy `_extract_reels()`
wrapper (which doesn't pass segments) confirmed to degrade safely rather than
crash. All new section headers confirmed to appear exactly once. Backend
restarted with everything loaded.

---

## 2026-07-09 — Target window raised to 45-90s

User wanted reels landing bigger — too many were falling on the smaller end
of the previous 30-90s window. Raised the floor: `REEL_MIN_SECONDS` 30→45 in
`generate_reels.py`'s `DEFAULT_PROFILE` (`REEL_MAX_SECONDS` stays 90). Same
"not a hard rule" philosophy as before — this is the one place both the CLI
and the backend (`backend/app.py`'s `_run_selection` calls `apply_profile(DEFAULT_PROFILE)`
before every selection) read from, and `_length_rule()`/the cutter's floor-extension
math both pull `REEL_MIN_SECONDS`/`REEL_MAX_SECONDS` dynamically at call time,
so this one change flows through the whole pipeline with nothing else to touch.

New effective ranges: 45-90s primary target, 25-110s "equally normal" range
(floor tolerance and ceiling unchanged at 20s), 110s hard ceiling unchanged.
**Verified** by rendering `_length_rule()` with the new env values — correct
numbers throughout (45-90s, 25-110s, 110s ceiling), and confirmed the
floor-extension trigger (`soft_min`) recalculates to 25s automatically.
Backend restarted with the change loaded.

---

## 2026-07-02 — First-principles fix: the final trim step was the real culprit

User reported "sudden endings" continuing even after the speaker-awareness
fix. Went back to first principles instead of another targeted patch: traced
every mechanical step that runs on Claude's segment picks after it returns
them, and looked hardest at the LAST one, `_trim_dangling_tail_ids`
(`src/cutter.py`) — it runs **unconditionally on every single reel**, so any
false positive in it silently downgrades Claude's chosen ending regardless of
how well the prompt/selection worked upstream.

**Root cause:** `DANGLING_END_WORDS` — the word list this trim step (and
`_segment_text_dangles`) uses to decide "does this ending look incomplete" —
was drastically over-broad. It included bare pronouns (`it`, `me`, `this`,
`that`, `he`, `she`, `they`), quantifiers/intensifiers (`all`, `really`,
`just`, `so`, `only`, `then`), and demonstratives — all extremely common as
the literal last word of a genuinely complete, often emphatic sentence
("that's me.", "that's all.", "I made it happen.", "I believe in this.").
Any reel whose correct, Claude-chosen ending happened to land on one of these
ordinary words got its real ending silently popped off by this check,
independent of prompt quality. Two more contributing issues in the same
function: `INCOMPLETE_END_WORDS` (gerunds — "building", "living", "growing")
treated as an automatic incompleteness signal, when a gerund ending a
sentence is not reliable (plenty of complete sentences end on one); and
`DANGLING_END_PHRASES` contained several phrases that were verbatim snippets
memorized from one specific past transcript ("our mission is easy", "those
barriers", "i knew this app", "the doctors are signing notes saying") rather
than general patterns — pure noise for every other transcript.

**Fix (`src/cutter.py`):**
- `DANGLING_END_WORDS` narrowed to near-universal incompleteness signals only:
  prepositions requiring an object, articles, connectors/subordinators,
  auxiliary/copula verbs, modals, possessive determiners, and genuine filler
  words (`yeah`, `um`, `uh`, `like`, `know`).
- `INCOMPLETE_END_WORDS` (the gerund list) removed from `_segment_text_dangles`'s
  check specifically — kept for its other two uses (word-level tail-padding
  decisions), which are lower-risk since being wrong there just means
  reconsidering one extra trailing word, not discarding an entire segment.
- `DANGLING_END_PHRASES` stripped of the transcript-specific memorized snippets,
  keeping only genuinely general phrase patterns.
- `V2_EXTRA_DANGLING_WORDS` (v2 profile is the default) had `say`/`mean`/`guess`/
  `think` removed — ordinary verbs that frequently end complete sentences
  ("that's what I think.", "that's my best guess.").

**Verified:** 8 direct test cases against `_segment_text_dangles` — 7 of 8
complete-sentence cases that previously would have false-positived now
correctly pass through untouched ("and that is me", "that is all", "i made it
happen", "i believe in this", "she never stopped fighting", "i finally
started living", "that is my best guess"), while genuinely incomplete endings
still correctly get flagged. One known remaining edge case: "that is exactly
who i am" still flags as dangling — a pre-existing phrase check for "i am"
(assuming it always needs a predicate) doesn't handle the "who/what X am"
completing construction. Rarer than the bare-pronoun case, left as a known
limitation rather than over-engineering a fix for it. Backend restarted with
everything loaded.

---

## 2026-07-02 — Found the real cause: speaker-blind mechanical extension in cutter.py

User reported the same three symptoms recurring despite several rounds of
prompt tuning: endings still not landing, a "mini sentence from another
person" cutting in, sentences still cut mid-way. Repeated recurrence after
prompt-only fixes was the signal that this wasn't a prompt problem — it
pointed at a mechanical step running *after* Claude's output that the prompt
has no control over.

**Root cause found in `src/cutter.py`:** two post-processing functions pull
in additional segments using pure chronological adjacency, with **zero check
on speaker identity**:
- `_extend_resolved_tail_ids` — if a reel's last segment looked "dangling" by
  a generic word list, it would automatically append whatever segment comes
  next in the transcript and treat it as resolving the thought — even if that
  next segment belongs to a completely different speaker (a reaction,
  interjection, or half-sentence). This is almost certainly the exact
  mechanism producing reels that end on someone else's voice cutting in: it
  happens regardless of what Claude selected or what the prompt says, because
  it's mechanical post-processing Claude never sees.
- `_fill_context_bridges` — when Claude picks two non-adjacent segments, this
  silently splices in everything sitting between them (up to
  `MAX_CONTEXT_BRIDGE_SEGMENTS`) with no speaker check either. If the gap
  spans a speaker change (e.g. an interviewer's question sitting between two
  of the subject's segments), it would get bridged in anyway.

**Fix:** both functions now check speaker identity before adding a segment.
`_extend_resolved_tail_ids` stops extending (rather than crossing into a
different speaker) via a direct `next_seg.get("speaker") != last_seg.get("speaker")`
check. `_fill_context_bridges` uses a new `_same_speaker_chain()` helper — a
gap only gets bridged if every segment involved (both picks plus everything
between them) shares one speaker; otherwise the gap is left unfilled and the
reel stitches straight to the next pick as a separate span instead.

**Verified:** four direct test cases against the real functions — a
cross-speaker dangling ending correctly stops instead of extending; a
same-speaker dangling ending still extends and resolves correctly; a
cross-speaker gap correctly doesn't get bridged; a same-speaker gap still
bridges as before. All four matched expectations exactly. File compiles.
Backend restarted with the fix loaded.

**Known remaining edge case, not addressed here:** Rev.ai's diarization
(speaker labeling) isn't perfect — if it ever misattributes a mid-sentence
continuation as a "speaker change" when it's actually the same person still
talking, this fix would (correctly, given the data it's given) treat that as
a different speaker and decline to extend/bridge across it. That would still
produce an incomplete-feeling ending, but the cause would be a diarization
error, not this logic — worth flagging if the "cut mid-sentence" symptom
persists after this fix, since that would point at Rev.ai's speaker labeling
rather than anything in this codebase.

---

## 2026-07-01 — Length-rule wording correction + docs resync

Follow-up correction: the previous entry's `_length_rule()` rewrite used
concrete numbers ("26s", "95-100s") to illustrate the completeness-over-window
principle — user pointed out those were meant purely as illustrative examples
in conversation, not values that should get baked into the prompt as if they
were literal targets. Re-worded `_length_rule()` (`src/analyzer.py`) to state
the principle relatively ("somewhat under {lo}s", "somewhat more than {hi}s")
instead of specific invented numbers, so Claude isn't anchored on any
particular figure that was never actually meaningful.

Also brought `reels-pipeline-logic.md` (the verification reference doc) back
in sync with everything changed this session — it had drifted since being
written: added the missing "Brand / promotional reels" prompt block, the
"GRAMMATICALLY COMPLETE IS NOT..." / "MULTI-SPEAKER ENDINGS" additions to the
landed-beat bullet, `is_brand_reel` in the JSON schema example, the corrected
`_length_rule()` quote, updated `cutter.py` constants table (tolerance
10→20s, continuation gap 0.42→0.6s, word continuation 6→10), the floor-side
sentence-boundary padding fix, the non-speech tag filter, and new rows in the
"Where to look if something seems off" table.

---

## 2026-07-01 — `<laugh>` caption leak, speaker cut-ins, and a too-rigid floor

User flagged a specific reel screenshot: literal `<laugh>` text burned into
the caption, and a reel ending on a different speaker's voice cutting in
mid-thought. Also reiterated the length philosophy in concrete terms: a
story finishing in 26s is fine, one properly needing 95-100s is fine —
30-90s was still coming across as too much of a rule rather than a guide.

**`<laugh>` fix (`src/transcription.py`, code-level — this is a data-hygiene
bug, not a prompt issue):** Rev.ai emits non-speech events (laughter, cough,
crosstalk, etc.) as bracket-wrapped "text" elements — `<laugh>`, `[cough]` —
which were flowing through untouched as if they were real spoken words, into
every downstream consumer: the sentence segments Claude picks from, the
karaoke captions, the attached word timings. Added a single filter at
`_parse()` — the one point ALL of those branch from — dropping any token
matching `^[<\[].*[>\]]$`. Pattern-based (not a hardcoded "laugh"/"cough"
list) so it catches whatever non-speech tag Rev.ai emits, not just the one
seen in the screenshot.

**Speaker cut-in (`src/analyzer.py`, prompt): new "MULTI-SPEAKER ENDINGS"
guidance** in the "END ON A LANDED BEAT" bullet: if a different speaker's
line falls at/near the reel's end, either their line is itself a complete,
meaningful beat (include enough of it to resolve), or it isn't — in which
case end on the previous speaker's landed line and drop the interjection
entirely. Never let a reel trail off on someone else's half-reaction just
because it came next chronologically.

**Floor was too rigid — a real mechanical bug, not just wording:**
`_normalize_cut_sheets` was triggering mechanical padding (see the prior
`_maybe_extend_playback_rows` entry) for ANY single-segment reel under 30s —
including a genuinely complete 26s story, which then got stretched anyway.
Added `soft_min = min_dur - REEL_END_TOLERANCE_SECONDS` (30-20=10s) as the
real trigger threshold, mirroring the ceiling's tolerance on the floor side —
only reels shorter than 10s (almost certainly an artifact, not an
intentional short story) get mechanically extended now. Also rewrote
`_length_rule()` with the user's own concrete numbers: "a reel that
completes its story in 26s is DONE... a reel that genuinely needs 95-100s
... is equally fine."

**Verified:** `_is_non_speech_tag` tested against `<laugh>`, `[cough]`,
`<crosstalk>`, `[inaudible]`, `[A]` (all correctly flagged) vs. `hello`,
`I<3`, `don't`, `(silence)` (correctly not flagged — `(silence)` stays
handled by the existing separate silence-token filter). Confirmed
`soft_min=10.0` at production settings, that a 26s reel no longer triggers
padding, and that a genuinely short 8s single-segment reel still does.
Full prompt placeholder substitution re-verified clean, all new section
headers appear exactly once, files compile. Backend restarted with
everything loaded.

---

## 2026-07-01 — Found why so many reels landed at exactly 30.0s

User pointed out a screenshot with several reels showing exactly "30.0s" —
that precision was the tell. Traced it to `_maybe_extend_playback_rows` in
`src/analyzer.py`: when Claude picks (or the cutter resolves to) a single
segment shorter than the 30s floor, this function was extending it by adding
**raw seconds** (`end_time_seconds = b + deficit`) with zero awareness of
word or sentence boundaries — landing exactly on the numeric minimum
regardless of whether that point fell mid-word, mid-phrase, or on a totally
unrelated tangent. That's a second, independent cause of "endings that feel
tacked on," separate from the earlier cutter-ceiling issue.

**Fix:** `_maybe_extend_playback_rows` now walks forward through whole
sentence segments (`utterance_segments` — the same sentence-bounded units
Claude picks reels from) instead of adding raw seconds, so any extension
lands on a real sentence end, never mid-word. Falls back to the old raw-second
behavior only if no sentence segments are available to extend into (e.g. the
reel is already at the end of the transcript). Also added an explicit prompt
line: Claude shouldn't pick a single short segment expecting it to be padded
afterward — reaching the 30s+ floor should come from real, connected content
it selected (blending multiple segments if needed), not from mechanical
padding.

**Verified:** three direct test cases against `_maybe_extend_playback_rows` —
(1) sentence-boundary extension lands exactly on a real sentence end, not an
arbitrary raw-second point; (2) omitting `utterance_segments` preserves the
exact old raw-time behavior (no regression for any other caller); (3) no
forward sentence segments available (end of transcript) correctly falls back
to raw-second padding rather than doing nothing. (First test run had a
shallow-copy bug in the test script itself, not the code — caught by the
numbers not matching expectations, fixed by using fresh dicts per case.)
Backend restarted with the fix loaded.

---

## 2026-07-01 — Reels still ending unresolved: found the real mechanical cause

Follow-up to the earlier ending-quality tuning — user reported reels were
still following 30-90s too strictly, with the story visibly still building
with no resolution at the cutoff.

**Root cause, not just prompt wording:** traced `src/cutter.py`'s
`_cap_segment_ids` — it enforces `soft_cap = REEL_MAX_SECONDS +
REEL_END_TOLERANCE_SECONDS` (90+20=110s) as a hard mechanical ceiling by
popping segments off the *end* of the list until the reel fits. So even if
Claude's prompt-following improved and it selected the right resolving
segment to complete a story arc, the cutter could still truncate that
resolution back off after the fact if the total ran past 110s — a structural
gap no amount of prompt wording alone could fix, since the cutter has no
narrative understanding, only word-list heuristics for grammatical dangling
endings (not "the story hasn't paid off yet").

**Fix (`src/analyzer.py`, prompt-only — the cutter's cap logic is correct as
enforcement, it just needed Claude to self-limit within it):**
- Rewrote `_length_rule()` to state the real ceiling explicitly (`{hi+flex}s`,
  e.g. 110s) as the ONE hard number, framing it as: if a complete story would
  run past that, the reel's *scope* is too broad (start later/narrower) —
  rather than picking a big arc and relying on the cutter to trim it, which
  silently reintroduces the abrupt-ending symptom.
- Reframed the extended window (`{lo-flex}-{hi+flex}s`, e.g. 10-110s) as
  "equally normal, ordinary range — not a rare exception" — the previous
  wording ("in the rare case a genuinely great reel needs it") likely read as
  an edge case rather than routine, undermining the flexibility it was meant
  to grant.
- Added an explicit grammatical-vs-narrative-completion distinction to the
  "END ON A LANDED BEAT" bullet: a sentence can be grammatically finished
  ("and that's the moment everything changed") while the actual outcome is
  still unrevealed — that still counts as unresolved, not landed, and Claude
  must keep including segments until the real content of the outcome is on
  screen.
- Numeric tolerance (`REEL_END_TOLERANCE_SECONDS`, cutter.py) left at 20s from
  the last round — already exceeds the ~10s flex the user asked for, so the
  gap was in how that flex was framed and enforced, not the number itself.

**Verified:** rendered the full prompt with production env values
(`REEL_MIN_SECONDS=30`/`REEL_MAX_SECONDS=90`) end-to-end — correct numbers
throughout (30-90s primary, 10-110s full range, 110s ceiling stated
explicitly), no leftover `{{...}}` placeholders, each new section header
confirmed to appear exactly once, file compiles. Backend restarted with the
updated prompt loaded.

---

## 2026-07-01 — Desktop "Generate Reels" no longer restarts from scratch on failure

**Symptom:** if "Select reel moments (Claude)" failed (e.g. a 529 Overloaded
that outlasted the backend's retries) after extract/compress/upload/transcribe
had already succeeded, clicking Generate Reels again re-ran the *entire*
pipeline — re-uploading and re-transcribing via Rev.ai (costing time and
money) even though a perfectly good transcript already existed.

**Fix (`desktop/src/main/main.js`, `channels.js`, `preload.js`, `renderer.js`):**
- Extracted the select+cut steps into a shared `selectAndCut()` function.
- The "transcribe" pipeline event now carries the transcript itself, so the
  renderer caches `state.transcript` the moment transcription succeeds —
  independent of whatever happens afterward.
- New IPC channel `SELECT_REELS_ONLY` + handler that runs just `selectAndCut()`
  from an already-obtained transcript, skipping extract/compress/upload/
  transcribe entirely.
- New "Retry selection only" button appears in the Generate Reels dialog
  whenever a failure happens with a transcript already cached — clicking it
  resumes straight into Claude reel selection.
- `resetProjectState()` (opening a different project) clears the cached
  transcript and hides the button, so a stale transcript from one project can
  never bleed into another.

**Verified:** cross-checked the new IPC channel name/handler/preload-expose/
renderer-call for consistency across all four files; full `node --check` on
all modified JS; HTML div-balance sanity check; existing 13-test model suite
still passes; Electron smoke test (`npm run smoke`) boots clean.

---

## 2026-07-01 — Ending quality, brand reels, 10→15 reels

Requested after reviewing real output: reels were ending abruptly, the
30-90s window felt like a hard rule rather than a guide, no reel was
dedicated to the client's business/brand, and the count should go from 10 to
15.

- **Abrupt endings:** `src/cutter.py` — `REEL_END_TOLERANCE_SECONDS` 10→20s,
  `CONTINUATION_GAP_SEC` 0.42→0.6s, `MAX_WORD_CONTINUATION` 6→10 (more room to
  pull in trailing words that complete a thought). `_length_rule()` and the
  "END ON A LANDED BEAT" bullet in `ANALYZER_PROMPT` (`src/analyzer.py`)
  rewritten to lead with "an abrupt ending is a FAILED reel" rather than the
  numeric window, explicitly calling 30-90s a "loose guide, not a hard rule."
- **Brand/promotional reels:** new required prompt section reserving exactly
  1-2 of the N reels for content built from segments where the subject talks
  about their business/work — marked via a new `is_brand_reel` field, parsed
  in `_normalize_claude_reel`, threaded through `desktop/src/main/reels.js`
  (`isBrandReel`), and surfaced as a small "BRAND" badge on the reel card in
  the desktop reels list (`renderer.js`/`styles.css`).
- **10→15 reels:** `VALID_NUM_REELS`/`DEFAULT_NUM_REELS` in `src/analyzer.py`,
  the hardcoded desktop call in `desktop/src/main/main.js`, both hardcoded
  fallbacks in `backend/app.py` (`/select` endpoint and the job-resume path),
  and `generate_reels.py`'s CLI `--num-reels` default — all four call sites
  updated together so nothing silently falls back to 10.
- **Verified:** rendered the full assembled prompt with placeholder
  substitution to confirm no leftover `{{...}}` tokens and correct 15-reel
  wiring; confirmed the em-dash "encoding issue" seen in one test print was
  purely a Windows console codepage display quirk, not actual file corruption
  (checked the raw UTF-8 bytes directly). All modified Python/JS files
  syntax-checked; `# How to build each reel` and the other new section
  headers each confirmed to appear exactly once in the prompt.

---

## 2026-07-01 — Fixed: "Select reel moments" failing on Claude 529 Overloaded

**Symptom:** Generate Reels failed at the "Select reel moments (Claude)" step
with `{'type': 'overloaded_error', 'message': 'Overloaded'}` — the retry logic
added earlier this session didn't actually retry it.

**Root cause:** the Anthropic SDK raises a distinct `OverloadedError` (HTTP
529) that is a *sibling* of `InternalServerError`, not a subclass — and it
(along with `ServiceUnavailableError`/`DeadlineExceededError`) isn't even
re-exported from the public `anthropic` top-level namespace, only from the
private `anthropic._exceptions` module. The retry list in `src/analyzer.py`
and `src/transcript_cleanup.py` named specific exception classes and simply
didn't include it.

**Fix:** replaced the exception-class allowlist with a status-code check
against the public `anthropic.APIStatusError` base class
(`_is_retryable_claude_error` in `src/analyzer.py`, `_is_retryable` in
`src/transcript_cleanup.py`) — retries on 408/409/429/500/502/503/504/529.
This catches `OverloadedError` and any future status-specific subclass
Anthropic adds without needing another code change, since they all inherit
from the same public base class. Also bumped the main reel-selection/brand-story
retry budget from 3 to 6 attempts with backoff capped at 30s (was uncapped
exponential) — 529s are common under load and can take longer than a couple
of quick retries to clear, and a bigger source video means more Claude calls
and more chances to hit one.

**Verified:** built a real `OverloadedError` (529) via `httpx.Response` and
confirmed both `_is_retryable_claude_error` and `_is_retryable` return `True`
for it and `False` for a genuine 400 (shouldn't be retried — retrying a bad
request just fails identically every time). Ran `_call_with_retries` against a
function that raises 529 twice then succeeds — recovered on the 3rd attempt
with correct backoff messages.

---

## 2026-07-01 — ANALYZER_PROMPT: client-post framing

- Documented the full reel-selection pipeline in `reels-pipeline-logic.md`
  (transcription → cleanup → segmentation → Claude reel selection → cut-boundary
  math → captions), quoting every prompt verbatim so it can be diffed against
  source if either drifts.
- Patched `ANALYZER_PROMPT` in `src/analyzer.py` with two additions, requested
  after reviewing that doc: a "Who these reels are for" block establishing that
  the subject is a paying client posting to their own accounts (so reels should
  cover both who they are and what they do), and a "CLIENT-POST TEST" gate
  applied per-reel before finalizing (drop anything that makes the client look
  bad; vulnerability is fine as long as the reel resolves into strength by the
  end). Also reframed the title-writing bullet toward "would the subject
  proudly repost this." Verified the `# How to build each reel` header still
  appears exactly once and the file compiles.
- **Did not** apply the requested backend "fix" for a claimed 15–60s vs.
  30–90s length-window bug — traced it first and the premise didn't hold:
  `backend/app.py`'s `_run_selection` (the only path to `analyze_with_claude`
  from the desktop backend) already calls `apply_profile(DEFAULT_PROFILE)`
  before selection runs, and `analyzer.py` passes `max_len=reel_max_seconds()`
  (a live env lookup) explicitly into `build_reel_cut_sheet`, not a frozen
  default parameter. The 30–90s window should already be active on the
  desktop path with no change needed. Flagged this instead of applying the
  snippet as given, which also called `apply_profile()` with zero arguments —
  the real signature requires the `profile` dict, so as written it would have
  raised `TypeError`.

---

## 2026-07-01 — Multi-camera (Cam A/B/C) sync + generate

Added support for shoots with multiple cameras plus a dedicated audio
recorder — import all camera files + the reference recorder, auto-sync them
via audio cross-correlation, pick which camera a reel uses, and export pulls
the correct footage from each camera at the correct time.

Design (confirmed via planning Q&A before building):
- **One transcript/timeline.** Only the dedicated reference audio gets
  transcribed; Claude's reel-selection logic (`src/analyzer.py`) needed zero
  changes — it already just picks time ranges from one transcript. Cameras are
  purely an edit/export-time overlay: a fixed time offset per camera,
  translated at export time.
- **Purely additive.** Existing single-camera projects/exports are completely
  unaffected — no schema break, no migration.
- **Constant-offset model only** — no clock-drift correction across
  multi-hour recordings (explicit non-goal, fine for a standardized studio
  setup where cameras don't drift meaningfully over an interview-length
  shoot).

### Sync engine (`src/camera_sync.py`, new)
- FFT-based audio cross-correlation (pure numpy, no scipy/librosa) computes
  each camera's offset against the reference audio: `camera_time =
  reference_time + offset_sec`.
- Extracts a short (5 min default), low-samplerate (4kHz) mono WAV window per
  file for correlation — fast regardless of source file size, since only a
  window is read.
- New `sync_cameras_cli.py` at repo root (same shape as `export_cli.py`) for
  the desktop app to spawn.
- **Verified:** synthetic signals with known offsets (including camera-before-
  reference, camera-after-reference, and zero-offset cases) all recovered
  exact offsets. Also verified through real ffmpeg-extracted audio/video files
  end-to-end, and through the actual `desktop/src/main/sync.js` spawn wrapper.
- One real bug caught during testing: an initial periodic test tone produced
  garbage results (periodic signals correlate with many shifted copies of
  themselves) — switched to aperiodic noise-like test signals, which is also
  the reminder for why real speech/room-tone correlates reliably but a pure
  tone wouldn't.

### Export generalization (`export/media.cjs`, `export_pipeline.py`)
- `exportReel` now accepts an optional `sources: {camera_id: {path,
  offsetSec}}` map; each segment can carry a `camera` field selecting which
  source to pull from (falls back to the primary source when unset — fully
  backward compatible). This was a small, surgical change since the earlier
  single-pass rewrite already builds one `-ss/-t -i <path>` input group per
  segment — it just needed a per-segment path instead of one shared path.
- **Verified:** a 2-camera export (visually distinct red/blue test videos, one
  segment per camera with a real offset) produced exactly the right footage at
  the right time, both calling `exportReel` directly and through the full
  `export_pipeline.py` → Node pipeline.
- Known v1 limitation (documented in code): crop geometry is computed once
  from the primary camera's resolution and reused for every segment, even
  ones sourced from a different camera — fine when all cameras share the same
  resolution/framing (a standardized studio setup), not handled if a secondary
  camera has a different native resolution.

### Desktop app (Electron)
- New "Cameras" dialog: add a reference audio file + Cam A/B/C video files,
  "Sync" button computes offsets live (per-camera progress, confidence score,
  manual override input for low-confidence results).
- New `desktop/src/main/sync.js` (mirrors `export.js`'s spawn/parse pattern)
  and 4 new IPC channels (`PICK_REFERENCE_AUDIO`, `ADD_CAMERA_DIALOG`,
  `SYNC_CAMERAS`, `SYNC_EVENT`).
- Project schema bumped to v2 (`referenceAudioPath` + `cameras[]`, both
  optional) — a v1 project with neither field loads/exports identically to
  before.
- **Scope adjustment from the original plan:** the plan called for a
  per-segment (per-cut) camera picker in the timeline editor. Exploration
  showed the existing editor doesn't have per-inner-segment editing UI at all
  today — reels are edited as one continuous in/out span (`setReelIn`/
  `setReelOut` in `model.js` only ever touch the first/last segment). Built a
  **per-reel** camera picker instead (applies to the whole reel), which
  matches the actual editing granularity that exists today. Nothing is lost
  architecturally — the export pipeline already supports full per-segment
  camera switching; a future finer-grained editor UI could set `camera` per
  span without any pipeline changes.
- **Verified:** IPC channel names cross-checked for consistency across
  `channels.js`/`main.js`/`preload.js`/`renderer.js` (all match). Existing
  `desktop/test/model.test.js` suite still passes (13/13, unaffected since
  `model.js` wasn't touched). Electron smoke test (`npm run smoke`) confirms
  the app boots cleanly with all UI changes loaded through the real renderer
  process. Full interactive click-through in the running app was **not**
  done — this sandboxed environment has no display for that; someone should
  manually run through: add cameras + reference audio → Sync → assign a
  camera to a reel → Generate → confirm the exported file cuts between the
  right angles at the right times.

---

## 2026-07-01 — Clone + export speed/quality overhaul + reliability pass

### Cloned the repo
- Cloned `srinith009/AI-reels-editor-` into this folder.

### Export pipeline: single-pass rewrite (speed)
- **Problem:** exports were slow — every clip was decoded and re-encoded *twice*
  (once per cut segment, then again for the final render), entirely on CPU
  (libx264, no GPU), one segment at a time.
- **Fix (`export/media.cjs`):** rewrote `exportReel` to do fast per-segment
  seeking (`-ss/-t` before each `-i`, same trick as before) directly into a
  single ffmpeg filter graph — trim → concat → crop → subtitle burn-in → one
  final encode. Removed the now-dead `cutSegment` / `concatSegments` /
  `renderReelPreview` functions.
- Result: one fewer full re-encode generation. Same quality settings as
  before, so output quality is unchanged or slightly better (no double
  quantization), just faster.
- Verified end-to-end with synthetic test clips (segment durations, crop, and
  captions all correct in the single-pass output).

### Parallel reel export
- **`export_pipeline.py` / `export_cli.py`** (used by the desktop app and the
  batch pipeline): reels now export concurrently via a bounded thread pool
  (`REEL_MAX_WORKERS` env var, default `min(4, cpu_count)`) instead of
  strictly one-at-a-time.

### New export options
- **"Original" resolution** — added alongside 720p/1080p/2K/4K. Crops to 9:16
  using the source's native pixel density with no `scale` filter at all — no
  upscale, no downscale. (4K/2K/1080p/720p still always upscale a smaller
  source, per explicit choice, for platform-compatibility reasons.)
- **Lossless audio** — new checkbox in the export dialog. Encodes audio as
  `pcm_s16le` (uncompressed) instead of AAC, auto-switches the container to
  `.mov` (MP4 doesn't support raw PCM reliably). Removes the one remaining
  lossy compression step; audio is still normalized to 48kHz/stereo for
  concat/mix compatibility (a transparent format match, not a compression
  loss).
- Both verified via direct `exportReel` smoke tests (native-resolution output
  dimensions, PCM codec + correct container).

### Production-scaling fixes
- **Configurable worker cap** — `REEL_MAX_WORKERS` env var overrides the
  `min(4, cpu_count)` default for exporting many reels concurrently on bigger
  hardware.
- **Duration-scaled export timeouts** — replaced flat 1200s/1800s
  `subprocess.run` timeouts with one that scales with the *reel's own cut
  duration* (not source file size — the single-pass export only touches the
  segments it needs). Tunable via `REEL_EXPORT_TIMEOUT_MULTIPLIER` (default 8s
  per second of cut) and `REEL_EXPORT_TIMEOUT_MIN` (default 300s floor).
- **Durable backend job registry** (`backend/job_store.py`, new) — SQLite-backed
  (no new service dependency). Every job update writes through to it. On
  startup, `backend/app.py` reloads persisted state and:
  - Reattaches to a still-running **Rev.ai** transcription job by its saved
    job id — no re-upload needed, Rev.ai keeps running server-side regardless
    of our process.
  - Re-runs **Claude selection** from the saved transcript/name/num_reels —
    there's no external job to reattach to there, so this is a from-scratch
    redo, but the request is never silently lost.
  - Restores already-finished jobs for polling with no wasted resume work.
  - Mid-run checkpoint: `_run_selection` persists `cleaned_words` right after
    the transcript-cleanup Claude call finishes (before the costlier
    reel-selection call), so a restart resumes straight into selection instead
    of redoing both Claude calls.
  - Capped at 5 auto-resume attempts (`resume_attempts`) — a persistently
    failing ("poison-pill") job now gives up permanently with a clear error
    instead of silently re-billing the Claude call forever across restarts.

### Failure-mode audit + fixes
Ran a full audit of where the app can fail (backend security/abuse, SQLite
concurrency, ffmpeg command-line limits, crash cleanup, race conditions, path
handling). Fixed the concrete, in-scope findings:
- **No retry on Claude failures** — added `_call_with_retries` in
  `src/analyzer.py` (exponential backoff; retries transient network errors and
  malformed JSON, since retrying the same prompt often just works) wrapping
  reel-selection and brand-story extraction. Added a matching per-chunk retry
  in `src/transcript_cleanup.py`.
- **`workDir` collision risk** (introduced by the parallel-export change) —
  `media.cjs` used to name each export's temp dir with `Date.now()`
  (millisecond resolution); with parallel exports now firing off back-to-back,
  two processes could collide and silently share (and race on) the same
  `subs.ass` file. Switched to `fs.mkdtempSync`, which is atomic and
  OS-guaranteed unique.
- **Orphaned temp dirs on hard-kill** — added `cleanupStaleExportDirs()` in
  `media.cjs`, sweeping `istv-export-*` dirs older than 6h on every export call
  (best-effort, never blocks the real export).
- **`job_store` corruption resilience** — `load_all()` now skips and drops
  individually corrupted rows instead of crashing the whole backend on
  startup; a fully corrupted DB file gets quarantined and replaced with a
  fresh one. (Caught and fixed a real Windows-specific bug here during
  testing: the corrupted connection has to be closed before the file can be
  renamed, or the rename silently fails and quarantine does nothing.)
- **Filler-word removal is now subtitle-only** — removed the
  `cutFillersFromVideo` video-cutting path entirely (word-level cuts could
  split one reel into 50-100+ tiny segments, risking the ffmpeg
  command-line-length limit on filler-heavy reels). Filler words are still
  hidden from burned-in captions via the existing `hideFillersInSubtitles`
  flag, and the desktop editor's live transcript-preview hiding
  (`model.js`) is untouched. The now-dead `removeFillersFromSegments`
  function was removed.

### Also discussed (not yet implemented)
- Scaling to multi-camera (Cam A/B/C) shoots — no concept of multiple sources
  exists yet anywhere in the data model; would need sync (timecode or
  waveform), a `source_id`/`camera` field per segment, and generalizing the
  single-pass filter graph to open the right source per segment.
- Scaling to 100GB+ combined footage — export itself scales fine (streams from
  disk, no full-file loads). **Correction from initial assessment:** a 100GB
  video file doesn't threaten Rev.ai's 200MB cap by itself — 100GB is a
  bitrate/resolution thing (4K/6K footage), not a duration thing, and the
  pipeline already extracts + compresses audio (down to ~64kbps mono) before
  it ever reaches Rev.ai, so a normal 1-3hr shoot at any file size is fine
  regardless of video size. The real ceiling is audio *duration* — a
  multi-camera shoot running 5-10+ hours of continuous audio would still need
  chunking across multiple transcription jobs, or a local ASR fallback (e.g.
  faster-whisper), to remove that ceiling entirely. Not a concern for typical
  documentary/interview-length shoots.
- A Premiere Pro plugin — feasible via Premiere's scripting API (place cuts on
  Premiere's own timeline, let it render natively); main tradeoff is losing
  the current custom karaoke caption fidelity (would need Premiere's Essential
  Graphics/captions instead of the ASS/ffmpeg burn-in), and a different tech
  stack (UXP/CEP, not Electron) for the panel UI.
- Backend has no auth (`CORSMiddleware` wide open, no API key check on
  `/transcribe` / `/select`) — flagged as the top remaining risk if this
  backend is ever deployed reachable from outside the local machine, since
  anyone could trigger billed API calls. Not yet fixed.
