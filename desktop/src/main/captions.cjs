"use strict";

/**
 * Word-synced karaoke caption + export-payload builder for the desktop app.
 *
 * A faithful JS port of the repo's Python `src/caption_builder.py` +
 * `export_pipeline.export_reel_mp4_ex` payload assembly. It exists so the
 * desktop export runs entirely in Node (see mediaEngine.cjs) and no longer
 * spawns the repo's Python `.venv` — which isn't present in a packaged build
 * (the "Is Python available?" failure). The output payload is byte-for-byte
 * equivalent to what the Python path produced, so exported reels look the same.
 *
 * Keep in sync with src/caption_builder.py if the karaoke logic changes there.
 */

const MIN_WORD_DISPLAY_SEC = 0.04;
const MIN_BLOCK_GAP_SEC = 0.03;
const MIN_BLOCK_DURATION_SEC = 0.25;
const DEFAULT_KARAOKE_CHUNK_SIZE = 2;
const NAME_MATCH_THRESHOLD = 0.72;

const DEFAULT_EXPORT_CANVAS = {
  aspectRatio: "9:16",
  fit: "cover",
  zoom: 1.0,
  panX: 0,
  panY: 0,
  cropX: 0.5,
  cropY: 0.5,
  captionStyle: "karaoke",
  captionSize: 135,
  captionX: 50,
  captionY: 86,
  captionWordGap: 0.34,
  hideFillersInSubtitles: false,
  cutFillersFromVideo: false,
  cutSilences: false,
};

const QUALITY_BITRATE = { low: "10M", medium: "16M", high: "24M" };

const round = (value, digits) => {
  const f = 10 ** digits;
  return Math.round((Number(value) || 0) * f) / f;
};

// ── Speaker-name correction (inert unless REEL_SPEAKER_NAME/REEL_NAME_ALIASES
// are set — the desktop flow doesn't set them, matching the previous Python
// path, but the logic is ported for full fidelity). ──────────────────────────

function nameTargets() {
  const raw = String(process.env.REEL_SPEAKER_NAME || "").trim();
  if (!raw) return [];
  return raw.split(/\s+/).filter((tok) => tok.length >= 3);
}

function nameAliases() {
  const raw = String(process.env.REEL_NAME_ALIASES || "").trim();
  const out = {};
  if (!raw) return out;
  for (const pair of raw.split(",")) {
    const eq = pair.indexOf("=");
    if (eq === -1) continue;
    const wrong = pair.slice(0, eq).trim().toLowerCase();
    const right = pair.slice(eq + 1).trim();
    if (wrong && right) out[wrong] = right;
  }
  return out;
}

function matchCase(correct, sample) {
  if (sample === sample.toUpperCase() && sample !== sample.toLowerCase()) return correct.toUpperCase();
  if (sample.slice(0, 1) === sample.slice(0, 1).toUpperCase() && sample.slice(0, 1) !== sample.slice(0, 1).toLowerCase()) {
    return correct.slice(0, 1).toUpperCase() + correct.slice(1);
  }
  return correct;
}

// Ratcliff/Obershelp similarity, matching Python's difflib.SequenceMatcher.ratio().
function longestMatch(a, b, alo, ahi, blo, bhi) {
  let besti = alo;
  let bestj = blo;
  let bestsize = 0;
  for (let i = alo; i < ahi; i += 1) {
    for (let j = blo; j < bhi; j += 1) {
      let k = 0;
      while (i + k < ahi && j + k < bhi && a[i + k] === b[j + k]) k += 1;
      if (k > bestsize) {
        besti = i;
        bestj = j;
        bestsize = k;
      }
    }
  }
  return [besti, bestj, bestsize];
}

function matchingChars(a, b, alo, ahi, blo, bhi) {
  const [i, j, k] = longestMatch(a, b, alo, ahi, blo, bhi);
  if (k === 0) return 0;
  return k + matchingChars(a, b, alo, i, blo, j) + matchingChars(a, b, i + k, ahi, j + k, bhi);
}

function seqRatio(a, b) {
  const total = a.length + b.length;
  if (!total) return 0;
  return (2 * matchingChars(a, b, 0, a.length, 0, b.length)) / total;
}

const NAME_TOKEN_RE = /^(\W*)([\s\S]*?)(\W*)$/;

function correctSpeakerName(text) {
  const targets = nameTargets();
  const aliases = nameAliases();
  if (!targets.length && !Object.keys(aliases).length) return text;

  const m = NAME_TOKEN_RE.exec(text || "");
  if (!m) return text;
  const [, lead, core, trail] = m;
  if (!core) return text;

  const low = core.toLowerCase();
  if (low in aliases) return `${lead}${matchCase(aliases[low], core)}${trail}`;

  for (const target of targets) {
    if (low === target.toLowerCase()) {
      const firstUpper = core.slice(0, 1) === core.slice(0, 1).toUpperCase() && core.slice(0, 1) !== core.slice(0, 1).toLowerCase();
      return `${lead}${firstUpper ? matchCase(target, core) : target}${trail}`;
    }
    if (core.length < 4) continue;
    if (seqRatio(low, target.toLowerCase()) >= NAME_MATCH_THRESHOLD) {
      return `${lead}${matchCase(target, core)}${trail}`;
    }
  }
  return text;
}

// ── Playback words + caption blocks ──────────────────────────────────────────

function segmentDuration(segment) {
  const start = Number(segment.start_time_seconds) || 0;
  const end = Number(segment.end_time_seconds) || start;
  return Math.max(0, end - start);
}

function timelineTotal(segments) {
  return Math.max(1, segments.reduce((sum, seg) => sum + segmentDuration(seg), 0));
}

function normalizeWordTimeline(words) {
  const cleaned = [];
  const sorted = [...words].sort(
    (a, b) => (Number(a.localTime ?? a.time) || 0) - (Number(b.localTime ?? b.time) || 0),
  );
  for (const raw of sorted) {
    let text = String(raw.word || "").trim();
    if (!text) continue;
    text = correctSpeakerName(text);
    const start = Math.max(0, Number("localTime" in raw ? raw.localTime : raw.time) || 0);
    let end = Number(raw.end) || start;
    if (end <= start) end = start + MIN_WORD_DISPLAY_SEC;
    cleaned.push({ ...raw, word: text, localTime: round(start, 4), end: round(end, 4) });
  }
  return cleaned;
}

function buildPlaybackWords(reel, segments) {
  const sourceWords = Array.isArray(reel.timestamped_words) ? reel.timestamped_words : [];

  if (!segments.length) {
    const raw = [];
    for (const word of sourceWords) {
      if (!word || typeof word !== "object") continue;
      const ws = Number("time" in word ? word.time : word.start) || 0;
      const we = Number(word.end) || ws;
      raw.push({
        ...word,
        word: String(word.word || "").trim(),
        time: ws,
        end: we,
        sourceEnd: we,
        localTime: ws,
      });
    }
    return normalizeWordTimeline(raw);
  }

  let offset = 0;
  const playback = [];
  for (const segment of segments) {
    const segStart = Number(segment.start_time_seconds) || 0;
    const segEnd = Number(segment.end_time_seconds) || segStart;
    for (const word of sourceWords) {
      if (!word || typeof word !== "object") continue;
      const ws = Number("time" in word ? word.time : word.start) || 0;
      const we = Number(word.end) || ws;
      if (we < segStart || ws > segEnd) continue;
      const localStart = offset + Math.max(0, ws - segStart);
      let localEnd = offset + Math.max(0, Math.min(we, segEnd) - segStart);
      if (localEnd <= localStart) localEnd = localStart + MIN_WORD_DISPLAY_SEC;
      playback.push({
        word: String(word.word || "").trim(),
        time: ws,
        end: localEnd,
        sourceEnd: we,
        speaker: word.speaker ?? 0,
        localTime: localStart,
      });
    }
    offset += Math.max(0, segEnd - segStart);
  }
  return normalizeWordTimeline(playback);
}

function finalizeCaptionTiming(blocks) {
  const sorted = [...blocks].sort(
    (a, b) => (Number(a.start_time_seconds) || 0) - (Number(b.start_time_seconds) || 0),
  );
  let prevEnd = 0;
  sorted.forEach((block, index) => {
    const words = block.words || [];
    if (words.length) {
      const start = Math.max(prevEnd + MIN_BLOCK_GAP_SEC, Number(words[0].localTime) || 0);
      let end = Math.max(start + MIN_BLOCK_DURATION_SEC, Number(words[words.length - 1].end) || start);
      if (index + 1 < sorted.length) {
        const nextWords = sorted[index + 1].words || [];
        const nextStart = nextWords.length
          ? Number(nextWords[0].localTime) || 0
          : Number(sorted[index + 1].start_time_seconds) || 0;
        end = Math.min(end, Math.max(start + MIN_BLOCK_DURATION_SEC, nextStart - MIN_BLOCK_GAP_SEC));
      } else {
        end = Math.max(end, (Number(words[words.length - 1].end) || start) + 0.28);
      }
      block.start_time_seconds = round(start, 3);
      block.end_time_seconds = round(end, 3);
    }
    block.text =
      words
        .map((w) => String(w.word || "").trim())
        .filter(Boolean)
        .join(" ") || String(block.text || "").trim();
    prevEnd = Number(block.end_time_seconds) || prevEnd;
  });
  return sorted;
}

function makeCaptionBlocks(words, chunkSize = DEFAULT_KARAOKE_CHUNK_SIZE) {
  const normalized = normalizeWordTimeline(words);
  const blocks = [];
  let chunk = [];

  const flush = () => {
    if (!chunk.length) return;
    const start = Number(chunk[0].localTime) || 0;
    let end = Number(chunk[chunk.length - 1].end) || start + MIN_BLOCK_DURATION_SEC;
    end = Math.max(start + MIN_BLOCK_DURATION_SEC, end);
    blocks.push({
      start_time_seconds: round(start, 3),
      end_time_seconds: round(end, 3),
      text: chunk.map((w) => String(w.word || "").trim()).filter(Boolean).join(" "),
      words: chunk.map((w) => ({ ...w })),
      speaker: chunk[0].speaker ?? 0,
    });
    chunk = [];
  };

  for (const word of normalized.slice(0, 360)) {
    const speaker = word.speaker ?? 0;
    if (chunk.length && (chunk[0].speaker ?? 0) !== speaker) flush();
    chunk.push(word);
    if (chunk.length >= chunkSize) flush();
  }
  flush();
  return finalizeCaptionTiming(blocks);
}

function buildCaptionsForReel(reel, segments) {
  const words = buildPlaybackWords(reel, segments);
  const reelId = reel.id ?? "reel";
  return makeCaptionBlocks(words).map((block, idx) => ({
    ...block,
    id: `cap-${reelId}-${idx}-${Math.trunc(block.start_time_seconds * 1000)}`,
  }));
}

// ── Segment sanitization (port of export_pipeline.sanitize_segments) ─────────

function sanitizeSegments(rows) {
  const out = [];
  (rows || []).forEach((row, idx) => {
    if (!row || typeof row !== "object") return;
    const start = Number(row.start_time_seconds) || 0;
    let end = Number(row.end_time_seconds) || start;
    if (end <= start) end = start + 0.12;
    let role = String(row.role || row.label || "BODY").toUpperCase();
    if (!["HOOK", "BODY", "PAYOFF"].includes(role)) role = "BODY";
    const label = String(row.label || role);
    const seg = {
      order: idx + 1,
      role,
      label: label.slice(0, 60),
      start_time_seconds: round(Math.max(0, start), 3),
      end_time_seconds: round(Math.max(0, end), 3),
      note: String(row.note || row.description || "").slice(0, 500),
    };
    if (row.camera) seg.camera = String(row.camera);
    out.push(seg);
  });
  return out;
}

// ── Full export payload (port of export_pipeline.export_reel_mp4_ex) ─────────

/**
 * Turn one editor reel spec + merged export options into the exact payload
 * mediaEngine.exportReel expects. Mirrors export_reel_mp4_ex 1:1.
 */
function buildExportPayload(reel, options = {}) {
  const segments = sanitizeSegments(reel.editor_cut_sheet || []);
  const captions = buildCaptionsForReel(reel, segments);
  const words = buildPlaybackWords(reel, segments);

  const canvas = { ...DEFAULT_EXPORT_CANVAS, ...(options.canvas || {}) };

  const resolution = options.resolution || { width: 1080, height: 1920 };
  const isOriginalRes = String(resolution.width).toLowerCase() === "original";
  const resolutionPayload = isOriginalRes
    ? { width: "original", height: "original" }
    : { width: Math.trunc(Number(resolution.width)), height: Math.trunc(Number(resolution.height)) };

  const quality = String(options.quality || "high").toLowerCase();
  const bitrate = options.bitrate || QUALITY_BITRATE[quality] || "22M";
  const fps = options.fps || "source";
  const chunk = Math.trunc(Number(options.captionChunkSize) || Number(process.env.REEL_CAPTION_CHUNK) || 4);

  const payload = {
    segments,
    captions,
    words,
    playbackWords: words,
    canvas,
    captionStyle: "karaoke",
    captionSize: Math.trunc(Number(canvas.captionSize) || 135),
    captionChunkSize: chunk,
    hideFillersInSubtitles: true,
    cutSilences: Boolean(options.cutSilences),
    quality,
    bitrate,
    fps,
    resolution: resolutionPayload,
    losslessAudio: Boolean(options.losslessAudio),
  };

  if (options.sources && Object.keys(options.sources).length) {
    payload.sources = options.sources;
  }
  if (options.encodePreset) {
    payload.encodePreset = String(options.encodePreset);
  }
  const music = options.music;
  if (music && music.path) {
    payload.musicPath = String(music.path);
    payload.musicVolume = Number(music.volume ?? 0.25);
  }
  return payload;
}

module.exports = {
  DEFAULT_EXPORT_CANVAS,
  QUALITY_BITRATE,
  buildPlaybackWords,
  buildCaptionsForReel,
  sanitizeSegments,
  buildExportPayload,
};
