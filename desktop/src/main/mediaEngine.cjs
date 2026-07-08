"use strict";

/**
 * Self-contained 9:16 karaoke export engine for the desktop app.
 *
 * This is a vendored, packaging-safe copy of the repo's `export/media.cjs`
 * engine. The two differences from that file exist specifically so export works
 * in a *packaged* build (the previous path spawned the repo's Python `.venv`,
 * which does not exist on an end user's machine — the "Is Python available?"
 * failure):
 *
 *   1. It lives INSIDE `desktop/src/main/` so it is bundled into app.asar with
 *      the rest of the app (a `require("../../../export/media.cjs")` would point
 *      outside the app root and not be packaged).
 *   2. It invokes ffmpeg/ffprobe via the bundled ffmpeg-static / ffprobe-static
 *      binaries (resolved through ./ffmpeg, which rewrites app.asar ->
 *      app.asar.unpacked) instead of relying on a bare `ffmpeg`/`ffprobe` on
 *      PATH — so rendering needs no system FFmpeg install.
 *
 * Keep the karaoke/reframe/encode logic here in sync with export/media.cjs if
 * that engine changes; the visual output is meant to be identical.
 */

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { ffmpegPath, ffprobePath } = require("./ffmpeg");

const FILLER_WORDS = new Set(["um", "uh", "umm", "uhh", "erm", "hmm", "ah", "like"]);

const STALE_EXPORT_DIR_MAX_AGE_MS = 6 * 60 * 60 * 1000; // 6h

// The normal cleanup path is the `finally` block in exportReel, but a hard kill
// (OS-level terminate, e.g. from an export timeout that doesn't let Node run
// its finally) can leave an `istv-export-*` dir behind. Rather than a separate
// cron/service, opportunistically sweep old ones on every export — best-effort,
// never lets a sweep failure block the actual export.
function cleanupStaleExportDirs() {
  try {
    const tmp = os.tmpdir();
    const now = Date.now();
    for (const name of fs.readdirSync(tmp)) {
      if (!name.startsWith("istv-export-")) continue;
      const full = path.join(tmp, name);
      try {
        const stat = fs.statSync(full);
        if (stat.isDirectory() && now - stat.mtimeMs > STALE_EXPORT_DIR_MAX_AGE_MS) {
          fs.rmSync(full, { recursive: true, force: true });
        }
      } catch (_) {
        // Another process may be using it, or it vanished already — ignore.
      }
    }
  } catch (_) {
    // Best-effort only; never let sweep failures block a real export.
  }
}

/**
 * Run a binary, capture stdout/stderr, resolve on exit 0.
 * `timeoutMs` (optional) kills a runaway ffmpeg and rejects with a clear
 * timeout message instead of letting a hung render block the export forever.
 */
function run(cmd, args, options = {}) {
  const { timeoutMs, ...spawnOpts } = options;
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd, args, { ...spawnOpts, windowsHide: true });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    let timer = null;
    if (timeoutMs && timeoutMs > 0) {
      timer = setTimeout(() => {
        timedOut = true;
        try {
          proc.kill("SIGKILL");
        } catch (_) {}
      }, timeoutMs);
    }
    proc.stdout?.on("data", (d) => {
      stdout += d.toString();
    });
    proc.stderr?.on("data", (d) => {
      stderr += d.toString();
    });
    proc.on("error", (err) => {
      if (timer) clearTimeout(timer);
      reject(err);
    });
    proc.on("close", (code) => {
      if (timer) clearTimeout(timer);
      if (timedOut) {
        reject(new Error(`ffmpeg timed out after ${Math.round(timeoutMs / 1000)}s`));
      } else if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        reject(new Error(stderr.slice(-1200) || `ffmpeg exited ${code}`));
      }
    });
  });
}

async function probeVideo(filePath) {
  const { stdout } = await run(ffprobePath(), [
    "-v",
    "error",
    "-show_streams",
    "-show_entries",
    "stream=codec_type,width,height,duration,r_frame_rate",
    "-of",
    "json",
    filePath,
  ]);
  const data = JSON.parse(stdout || "{}");
  const streams = data.streams || [];
  const vstream = streams.find((stream) => stream.codec_type === "video");
  if (!vstream) {
    return { width: 0, height: 0, duration: 0, fps: 0, hasVideo: false };
  }
  const fpsParts = String(vstream.r_frame_rate || "30/1").split("/");
  const fps = Number(fpsParts[0]) / (Number(fpsParts[1]) || 1);
  return {
    width: Number(vstream.width) || 0,
    height: Number(vstream.height) || 0,
    duration: Number(vstream.duration) || 0,
    fps: fps || 30,
    hasVideo: true,
  };
}

function segmentDuration(seg) {
  return Math.max(0, Number(seg.end_time_seconds) - Number(seg.start_time_seconds));
}

function resequenceSegments(segments) {
  return segments
    .filter((s) => segmentDuration(s) >= 0.1)
    .map((s, i) => ({ ...s, order: i + 1 }));
}

function removeSilencesFromSegments(segments, words, threshold = 0.45) {
  const next = [];
  segments.forEach((segment) => {
    const segStart = Number(segment.start_time_seconds) || 0;
    const segEnd = Number(segment.end_time_seconds) || segStart;
    const segWords = (words || [])
      .filter((w) => {
        const t = Number(w.time ?? w.start) || 0;
        return t >= segStart && t <= segEnd;
      })
      .sort((a, b) => (Number(a.time ?? a.start) || 0) - (Number(b.time ?? b.start) || 0));

    if (segWords.length < 2) {
      next.push(segment);
      return;
    }

    let cursor = segStart;
    for (let i = 0; i < segWords.length - 1; i += 1) {
      const wEnd = Number(segWords[i].end ?? segWords[i].time) || 0;
      const nextStart = Number(segWords[i + 1].time ?? segWords[i + 1].start) || 0;
      if (nextStart - wEnd >= threshold) {
        if (wEnd - cursor >= 0.15) {
          next.push({
            ...segment,
            start_time_seconds: cursor,
            end_time_seconds: wEnd,
          });
        }
        cursor = nextStart;
      }
    }
    if (segEnd - cursor >= 0.15) {
      next.push({
        ...segment,
        start_time_seconds: cursor,
        end_time_seconds: segEnd,
      });
    }
  });
  return resequenceSegments(next);
}

const CAPTION_GAP_SEC = 0.03;
const MIN_WORD_SEC = 0.04;
const MIN_CAPTION_SEC = 0.25;

// Opus-style karaoke: white base, golden active word, soft shadow
const KARAOKE_PRIMARY = "&H0050B4E6&"; // gold #E6B450 (ASS BBGGRR)
const KARAOKE_SECONDARY = "&H00FFFFFF&"; // white upcoming
const KARAOKE_SHADOW = "&H80000000&";
const KARAOKE_FONT = "Segoe UI Black";
const KARAOKE_BASE_SIZE = 135;
const KARAOKE_CHUNK_SIZE = 2;
const KARAOKE_LINE_HOLD_SEC = 0.12;
const KARAOKE_FINAL_HOLD_SEC = 0.28;
const ORPHAN_WORDS = new Set([
  "her", "him", "his", "the", "and", "but", "or", "a", "an", "to", "of", "in", "on",
  "at", "my", "your", "their", "our", "its", "it", "i", "me", "we", "they", "them",
  "he", "she", "that", "this", "those", "these", "ther",
]);

function strictWordTimeline(words = []) {
  return [...words]
    .filter((w) => w && String(w.word || "").trim())
    .sort((a, b) => (Number(a.localTime ?? a.time) || 0) - (Number(b.localTime ?? b.time) || 0))
    .map((raw) => {
      const start = Math.max(0, Number(raw.localTime ?? raw.time) || 0);
      const endRaw = Number(raw.end);
      const end = endRaw > start ? endRaw : start + MIN_WORD_SEC;
      return { ...raw, word: String(raw.word || "").trim(), localTime: start, end };
    });
}

function chunkWords(words, chunkSize = KARAOKE_CHUNK_SIZE) {
  const chunks = [];
  for (let i = 0; i < words.length; i += chunkSize) {
    chunks.push(words.slice(i, i + chunkSize));
  }
  return chunks;
}

function mergeOrphanChunks(chunks) {
  if (chunks.length <= 1) return chunks;
  const merged = [];
  chunks.forEach((chunk) => {
    const visible = chunk.filter((w) => {
      const text = String(w.word || "").trim();
      if (!text) return false;
      const clean = text.toLowerCase().replace(/[^a-z]/g, "");
      return clean.length > 0;
    });
    const lone = visible.length === 1
      && ORPHAN_WORDS.has(String(visible[0].word || "").toLowerCase().replace(/[^a-z]/g, ""));
    if (lone && merged.length) {
      merged[merged.length - 1] = merged[merged.length - 1].concat(chunk);
    } else {
      merged.push(chunk);
    }
  });
  return merged;
}

function buildKaraokeDialogueLines(playbackWords, hideFillers, posTag, toAssTime, chunkSize = KARAOKE_CHUNK_SIZE) {
  const words = strictWordTimeline(playbackWords);
  const lines = [];
  const chunks = mergeOrphanChunks(chunkWords(words, Math.max(1, Number(chunkSize) || KARAOKE_CHUNK_SIZE)));
  let prevEnd = 0;
  chunks.forEach((chunk, index) => {
    if (!chunk.length) return;
    const lineStart = Math.max(prevEnd + CAPTION_GAP_SEC, chunk[0].localTime);
    let lineEnd = chunk[chunk.length - 1].end + KARAOKE_LINE_HOLD_SEC;
    const isLast = index + 1 >= chunks.length;
    if (isLast) {
      lineEnd = chunk[chunk.length - 1].end + KARAOKE_FINAL_HOLD_SEC;
    } else {
      const nextStart = chunks[index + 1][0].localTime;
      lineEnd = Math.min(lineEnd, Math.max(lineStart + MIN_WORD_SEC, nextStart - CAPTION_GAP_SEC));
    }
    const parts = [];
    let relCursor = 0;
    chunk.forEach((w) => {
      const text = String(w.word || "").trim();
      if (!text) return;
      const clean = text.toLowerCase().replace(/[^a-z]/g, "");
      if (hideFillers && FILLER_WORDS.has(clean)) return;
      const relStart = Math.max(0, w.localTime - lineStart);
      const relEnd = Math.max(relStart + MIN_WORD_SEC, w.end - lineStart);
      const gapCs = Math.max(0, Math.round((relStart - relCursor) * 100));
      const durCs = Math.max(1, Math.round((relEnd - relStart) * 100));
      if (gapCs > 0) parts.push(`{\\kf${gapCs}}`);
      parts.push(`{\\kf${durCs}}${text.replace(/\{/g, "\\{").replace(/\}/g, "\\}")} `);
      relCursor = relEnd;
    });
    if (!parts.length) return;
    lines.push(
      `Dialogue: 0,${toAssTime(lineStart)},${toAssTime(lineEnd)},Default,,0,0,0,,${posTag}${parts.join("").trim()}`,
    );
    prevEnd = lineEnd;
  });
  return lines;
}

function normalizeCaptionTiming(captions = []) {
  const sorted = [...captions]
    .filter((cap) => cap && (cap.text || cap.words?.length))
    .sort((a, b) => (Number(a.start_time_seconds) || 0) - (Number(b.start_time_seconds) || 0));
  let prevEnd = 0;
  sorted.forEach((cap, index) => {
    const words = strictWordTimeline(cap.words || []);
    let start;
    let end;
    if (words.length) {
      start = Math.max(prevEnd + CAPTION_GAP_SEC, Number(words[0].localTime) || 0);
      end = Math.max(start + MIN_CAPTION_SEC, Number(words[words.length - 1].end) || start + MIN_CAPTION_SEC);
      cap.words = words;
    } else {
      start = Math.max(prevEnd + CAPTION_GAP_SEC, Number(cap.start_time_seconds) || 0);
      end = Math.max(start + MIN_CAPTION_SEC, Number(cap.end_time_seconds) || start + MIN_CAPTION_SEC);
    }
    if (index + 1 < sorted.length) {
      const nextWords = sorted[index + 1].words || [];
      const nextStart = nextWords.length
        ? Number(nextWords[0].localTime) || Number(sorted[index + 1].start_time_seconds) || 0
        : Number(sorted[index + 1].start_time_seconds) || 0;
      end = Math.min(end, Math.max(start + MIN_CAPTION_SEC, nextStart - CAPTION_GAP_SEC));
    }
    cap.start_time_seconds = start;
    cap.end_time_seconds = end;
    if (words.length) {
      cap.text = words.map((w) => String(w.word || "").trim()).filter(Boolean).join(" ");
    }
    prevEnd = end;
  });
  return sorted;
}

function buildAssSubtitles(captions, words, style, size, hideFillers, canvas = {}, outW = 1080, outH = 1920, opts = {}) {
  const captionX = Number(canvas.captionX ?? 50);
  const captionY = Number(canvas.captionY ?? 86);
  const scaledSize = Math.max(KARAOKE_BASE_SIZE, Math.round(Number(size || KARAOKE_BASE_SIZE) * (outH / 1920)));
  const posX = Math.round(outW * (captionX / 100));
  const posY = Math.round(outH * (captionY / 100));
  const posTag = `{\\an2\\pos(${posX},${posY})}`;
  const chunkSize = Number(opts.captionChunkSize) || KARAOKE_CHUNK_SIZE;
  const timedCaptions = normalizeCaptionTiming(captions);

  const hookSize = Math.round(scaledSize * 0.68);
  const nameSize = Math.round(scaledSize * 0.46);
  const topCenterY = Math.round(outH * 0.11);
  const topRightX = Math.round(outW * 0.92);
  const topRightY = Math.round(outH * 0.09);
  const header = `[Script Info]
ScriptType: v4.00+
PlayResX: ${outW}
PlayResY: ${outH}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,${KARAOKE_FONT},${scaledSize},${style === "karaoke" ? KARAOKE_PRIMARY : "&H00FFFFFF&"},${style === "karaoke" ? KARAOKE_SECONDARY : "&H00E6B450&"},${KARAOKE_SHADOW},&H00000000&,-1,0,0,0,100,100,0,0,1,0,4,2,40,40,100,1
Style: TextHook,${KARAOKE_FONT},${hookSize},&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&H64000000&,-1,0,0,0,100,100,0,0,1,5,3,8,60,60,80,1
Style: NameTag,${KARAOKE_FONT},${nameSize},&H0050B4E6&,&H0050B4E6&,&H00000000&,&H64000000&,-1,0,0,0,100,100,0,0,1,3,2,9,60,60,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
`;

  const lines = [header];
  const toAssTime = (sec) => {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    const cs = Math.floor((sec % 1) * 100);
    return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}.${cs.toString().padStart(2, "0")}`;
  };

  const escapeAss = (t) => String(t || "").replace(/\{/g, "(").replace(/\}/g, ")").replace(/\n/g, " ").trim();
  const overlayLines = [];
  const textHook = escapeAss(opts.textHook);
  const speakerName = escapeAss(opts.speakerName);
  const hookTag = `{\\an8\\pos(${Math.round(outW / 2)},${topCenterY})}`;
  const nameTag = `{\\an9\\pos(${topRightX},${topRightY})}`;

  // Sequential top overlays — never stack with lower-third karaoke captions.
  if (textHook) {
    overlayLines.push(
      `Dialogue: 1,${toAssTime(0)},${toAssTime(2.15)},TextHook,,0,0,0,,${hookTag}{\\fad(160,200)}${textHook}`,
    );
  }
  if (speakerName) {
    const title = escapeAss(opts.speakerTitle);
    const nameText = title ? `${speakerName}  |  ${title}` : speakerName;
    const nameStart = textHook ? 2.25 : 0.4;
    const nameEnd = Math.max(nameStart + 2.0, 5.0);
    overlayLines.push(
      `Dialogue: 1,${toAssTime(nameStart)},${toAssTime(nameEnd)},NameTag,,0,0,0,,${nameTag}{\\fad(180,240)}${nameText}`,
    );
  }

  if (style === "karaoke") {
    const playback = strictWordTimeline(words?.length ? words : []);
    if (playback.length) {
      lines.push(...buildKaraokeDialogueLines(playback, hideFillers, posTag, toAssTime, chunkSize));
      lines.push(...overlayLines);
      return lines.join("\n");
    }
    timedCaptions.forEach((cap) => {
      const capWords = strictWordTimeline(cap.words?.length ? cap.words : []);
      if (!capWords.length) return;
      lines.push(
        ...buildKaraokeDialogueLines(capWords, hideFillers, posTag, toAssTime, chunkSize),
      );
    });
    lines.push(...overlayLines);
    return lines.join("\n");
  }

  timedCaptions.forEach((cap) => {
    let text = String(cap.text || "");
    if (hideFillers) {
      text = text
        .split(/\s+/)
        .filter((w) => !FILLER_WORDS.has(w.toLowerCase().replace(/[^a-z]/g, "")))
        .join(" ");
    }
    if (!text.trim()) return;
    const start = Number(cap.start_time_seconds) || 0;
    const end = Number(cap.end_time_seconds) || start + 1;
    lines.push(
      `Dialogue: 0,${toAssTime(start)},${toAssTime(end)},Default,,0,0,0,,${posTag}${text.replace(/\{/g, "\\{").replace(/\}/g, "\\}")}`,
    );
  });
  lines.push(...overlayLines);
  return lines.join("\n");
}

function buildCropFilter(srcW, srcH, canvas, outW = 1080, outH = 1920, options = {}) {
  const targetAR = outW / outH;
  const srcAR = srcW / srcH;
  const zoom = Math.max(1, Number(canvas?.zoom) || 1);
  const focusX = Math.max(0, Math.min(1, Number(canvas?.cropX ?? 0.5) + (Number(canvas?.panX) || 0) / 200));
  const focusY = Math.max(0, Math.min(1, Number(canvas?.cropY ?? 0.5) + (Number(canvas?.panY) || 0) / 200));
  const scaleFlags = "flags=lanczos+accurate_rnd+full_chroma_int";
  let cropW;
  let cropH;
  if (srcAR > targetAR) {
    cropH = Math.round(srcH / zoom);
    cropW = Math.round(cropH * targetAR);
  } else {
    cropW = Math.round(srcW / zoom);
    cropH = Math.round(cropW / targetAR);
  }
  cropW = Math.max(2, Math.min(cropW, srcW));
  cropH = Math.max(2, Math.min(cropH, srcH));
  const maxX = Math.max(0, srcW - cropW);
  const maxY = Math.max(0, srcH - cropH);
  const cx = Math.round(maxX * focusX);
  const cy = Math.round(maxY * focusY);
  // "Original" resolution: crop only, no scale filter, so no pixel is invented (no upscale)
  // and no source detail is downsampled away (no downscale) — the crop itself is native-res.
  if (options.noScale) {
    return `crop=${cropW}:${cropH}:${cx}:${cy},setsar=1`;
  }
  return `crop=${cropW}:${cropH}:${cx}:${cy},scale=${outW}:${outH}:${scaleFlags},setsar=1`;
}

function parseBitrate(value, fallback = "20M") {
  const raw = String(value || fallback).trim();
  const match = raw.match(/^(\d+(?:\.\d+)?)([kKmM])?$/);
  if (!match) return raw;
  const amount = Number(match[1]);
  const unit = (match[2] || "M").toUpperCase();
  if (unit === "K") return `${Math.round(amount)}k`;
  return `${amount}M`;
}

function buildExportEncodeArgs({
  quality = "high",
  bitrate = "20M",
  fps,
  sourceFps = 30,
  presetOverride = null,
  losslessAudio = false,
}) {
  const targetFps = fps && fps !== "source" ? Number(fps) : Math.round(sourceFps) || 30;
  const presets = {
    high: {
      preset: "slow",
      crf: "16",
      maxrate: parseBitrate(bitrate, "22M"),
      bufsize: "44M",
      audioBitrate: "320k",
      profile: "high",
      tune: "film",
    },
    medium: {
      preset: "medium",
      crf: "19",
      maxrate: parseBitrate(bitrate, "15M"),
      bufsize: "30M",
      audioBitrate: "256k",
      profile: "high",
      tune: "film",
    },
    low: {
      preset: "fast",
      crf: "22",
      maxrate: parseBitrate(bitrate, "8M"),
      bufsize: "16M",
      audioBitrate: "192k",
      profile: "main",
      tune: null,
    },
  };
  const cfg = presets[quality] || presets.high;
  const preset = presetOverride || cfg.preset; // desktop export may request a faster preset
  const videoArgs = [
    "-c:v",
    "libx264",
    "-preset",
    preset,
    "-profile:v",
    cfg.profile,
    "-level",
    "4.2",
    "-pix_fmt",
    "yuv420p",
    "-crf",
    cfg.crf,
    "-maxrate",
    cfg.maxrate,
    "-bufsize",
    cfg.bufsize,
    "-colorspace",
    "bt709",
    "-color_primaries",
    "bt709",
    "-color_trc",
    "bt709",
  ];
  if (cfg.tune) {
    videoArgs.push("-tune", cfg.tune);
  }
  if (fps && fps !== "source") {
    videoArgs.push("-r", String(targetFps));
  }
  // Lossless mode: PCM is uncompressed, so this removes the one remaining lossy
  // step (AAC quantization). -ar/-ac still normalize to 48kHz stereo so the
  // concat/amix filter graph stays consistent across segments and music tracks;
  // that's an inaudible format match, not a compression loss.
  const audioArgs = losslessAudio
    ? ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
    : ["-c:a", "aac", "-b:a", cfg.audioBitrate, "-ar", "48000", "-ac", "2"];
  return { videoArgs, audioArgs, targetFps };
}

/** Seconds of source footage a reel actually cuts — export runtime scales with
 * this (single-pass ffmpeg only touches the segments it needs), not file size. */
function reelCutDuration(segments) {
  return (segments || []).reduce((total, seg) => total + segmentDuration(seg), 0);
}

/** Scale the ffmpeg timeout with the reel's cut duration so a long reel isn't
 * killed mid-render while a short one doesn't wait needlessly to fail a hang.
 * Tunable via the same env vars as the batch pipeline. */
function exportTimeoutMs(segments) {
  const multiplier = Number(process.env.REEL_EXPORT_TIMEOUT_MULTIPLIER) || 8;
  const minimum = Number(process.env.REEL_EXPORT_TIMEOUT_MIN) || 300;
  return Math.round(Math.max(minimum, reelCutDuration(segments) * multiplier) * 1000);
}

async function exportReel(sourcePath, outputPath, payload) {
  const {
    segments,
    captions,
    words,
    canvas = {},
    musicPath,
    musicVolume = 0.25,
    cutSilences = false,
    hideFillersInSubtitles = false,
    captionStyle = "bold",
    captionSize = 42,
    captionChunkSize,
    textHook = "",
    speakerName = "",
    speakerTitle = "",
    quality = "high",
    bitrate = "22M",
    fps = "source",
    resolution = { width: 1080, height: 1920 },
    encodePreset = null,
    losslessAudio = false,
    // Multi-camera (optional, additive): { camera_id: { path, offsetSec } }.
    // A segment with `camera` set to one of these keys pulls its footage from
    // that camera's own file instead of `sourcePath`, seeking at
    // `segment.start_time_seconds + offsetSec`.
    sources = {},
  } = payload;

  // mkdtempSync (not Date.now()-based naming) guarantees a unique dir even when
  // several reels export concurrently and land in the same millisecond.
  cleanupStaleExportDirs();
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "istv-export-"));

  try {
    const sourceProbe = await probeVideo(sourcePath);
    if (!sourceProbe.hasVideo) {
      throw new Error(
        "Source file has no video stream. Use the original documentary video file for export.",
      );
    }

    let segs = resequenceSegments(segments || []);

    const usedCameraIds = new Set(
      segs.map((seg) => seg.camera).filter((camId) => camId && sources[camId]),
    );
    for (const camId of usedCameraIds) {
      const camProbe = await probeVideo(sources[camId].path);
      if (!camProbe.hasVideo) {
        throw new Error(`Camera "${camId}" source file has no video stream: ${sources[camId].path}`);
      }
    }

    if (cutSilences) {
      segs = removeSilencesFromSegments(segs, words || [], 0.45);
    }
    if (!segs.length) throw new Error("No segments to export");

    const assPath = path.join(workDir, "subs.ass");
    const playbackWords = words?.length ? words : (payload.playbackWords || []);

    // "Original" resolution: keep the source's native pixel density.
    const isOriginalRes = String(resolution.width).toLowerCase() === "original";
    let outW = resolution.width || 1080;
    let outH = resolution.height || 1920;
    if (isOriginalRes) {
      const targetAR = 9 / 16;
      const srcAR = sourceProbe.width / sourceProbe.height;
      if (srcAR > targetAR) {
        outH = sourceProbe.height;
        outW = Math.round(outH * targetAR);
      } else {
        outW = sourceProbe.width;
        outH = Math.round(outW / targetAR);
      }
      outW -= outW % 2;
      outH -= outH % 2;
    }
    fs.writeFileSync(
      assPath,
      buildAssSubtitles(
        captions,
        playbackWords,
        captionStyle,
        captionSize,
        hideFillersInSubtitles,
        canvas,
        outW,
        outH,
        { captionChunkSize, textHook, speakerName, speakerTitle },
      ),
      "utf8",
    );

    const crop = buildCropFilter(sourceProbe.width, sourceProbe.height, canvas, outW, outH, {
      noScale: isOriginalRes,
    });
    const assEscaped = assPath.replace(/\\/g, "/").replace(/:/g, "\\:");
    const { videoArgs, audioArgs } = buildExportEncodeArgs({
      quality,
      bitrate,
      fps,
      sourceFps: sourceProbe.fps || 30,
      presetOverride: encodePreset,
      losslessAudio,
    });

    // Single pass: fast per-segment seek + trim -> concat -> crop -> subtitles -> one final encode.
    const args = ["-y", "-hide_banner", "-loglevel", "error"];
    const filterParts = [];
    segs.forEach((seg, i) => {
      const camSource = seg.camera && sources[seg.camera] ? sources[seg.camera] : null;
      const segSourcePath = camSource ? camSource.path : sourcePath;
      const offsetSec = camSource ? Number(camSource.offsetSec) || 0 : 0;
      const refStart = Number(seg.start_time_seconds) || 0;
      const refEnd = Number(seg.end_time_seconds) || refStart;
      const start = refStart + offsetSec;
      const end = refEnd + offsetSec;
      if (start < 0) {
        throw new Error(
          `Camera "${seg.camera}" has no footage yet at reference time ${refStart.toFixed(3)}s ` +
            `(camera starts ${(-offsetSec).toFixed(3)}s after the reference timeline).`,
        );
      }
      const duration = Math.max(0.08, end - start);
      args.push("-ss", start.toFixed(3), "-t", duration.toFixed(3), "-i", segSourcePath);
      filterParts.push(`[${i}:v]setpts=PTS-STARTPTS[v${i}]`);
      filterParts.push(`[${i}:a]asetpts=PTS-STARTPTS[a${i}]`);
    });

    const hasMusic = Boolean(musicPath && fs.existsSync(musicPath));
    const musicIndex = segs.length;
    if (hasMusic) {
      args.push("-i", musicPath);
    }

    const concatInputs = segs.map((_, i) => `[v${i}][a${i}]`).join("");
    filterParts.push(`${concatInputs}concat=n=${segs.length}:v=1:a=1[vcat][acat]`);
    filterParts.push(`[vcat]${crop}[vcrop]`);
    filterParts.push(`[vcrop]subtitles='${assEscaped}'[vout]`);

    let audioLabel = "[acat]";
    if (hasMusic) {
      filterParts.push(`[${musicIndex}:a]volume=${musicVolume}[am1]`);
      filterParts.push("[acat]volume=1[am0]");
      filterParts.push("[am0][am1]amix=inputs=2:duration=first:dropout_transition=0[aout]");
      audioLabel = "[aout]";
    }

    args.push("-filter_complex", filterParts.join(";"));
    args.push("-map", "[vout]", "-map", audioLabel);
    args.push(
      ...videoArgs,
      "-aspect",
      `${outW}:${outH}`,
      ...audioArgs,
      "-movflags",
      "+faststart",
      outputPath,
    );

    await run(ffmpegPath(), args, { timeoutMs: exportTimeoutMs(segs) });
    return { outputPath, segments: segs, quality, sourceFps: sourceProbe.fps };
  } finally {
    try {
      fs.rmSync(workDir, { recursive: true, force: true });
    } catch (_) {}
  }
}

module.exports = {
  probeVideo,
  exportReel,
  buildAssSubtitles,
  removeSilencesFromSegments,
};
