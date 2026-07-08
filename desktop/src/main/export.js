"use strict";

/**
 * Export orchestrator. Renders edited reels from the FULL-RES master entirely in
 * Node — no Python. Each reel's caption/karaoke payload is built by captions.cjs
 * (a port of the repo's proven caption builder) and rendered by mediaEngine.cjs
 * (the bundled Node/FFmpeg engine driving the app's ffmpeg-static binaries), so
 * exported reels match the tool's approved karaoke/reframe output.
 *
 * This used to spawn the repo's `export_cli.py` via a local Python `.venv`,
 * which does not exist on an end user's machine — so export failed in a packaged
 * build with "Is Python available?". Running the render in-process removes that
 * dependency: everything it needs is bundled inside the app.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { buildExportPayload } = require("./captions.cjs");
const engine = require("./mediaEngine.cjs");

const RESOLUTIONS = {
  "720p": { width: 720, height: 1280 },
  "1080p": { width: 1080, height: 1920 },
  "2K": { width: 1440, height: 2560 },
  "4K": { width: 2160, height: 3840 },
  // Sentinel: export at the source's native resolution (crop only, no scale)
  // instead of a fixed pixel target — see mediaEngine.cjs exportReel.
  Original: { width: "original", height: "original" },
};
const QUALITY = { Lower: "low", Recommended: "medium", Higher: "high" };
// Faster x264 presets for snappier desktop exports (CRF still governs quality).
const PRESET = { Lower: "veryfast", Recommended: "faster", Higher: "medium" };

/** Convert an app reel (in/out references + settings) into the CLI's reel spec. */
function toExportReel(reel) {
  // Multi-camera (optional): reel.settings.camera picks which camera this
  // WHOLE reel pulls footage from — applied uniformly to every segment.
  const reelCamera = reel.settings.camera || null;
  const editorCutSheet = reel.segments.map((s, idx) => ({
    order: idx + 1,
    role: s.role || (idx === 0 ? "HOOK" : idx === reel.segments.length - 1 ? "PAYOFF" : "BODY"),
    start_time_seconds: s.startSec,
    end_time_seconds: s.endSec,
    ...(reelCamera ? { camera: reelCamera } : {}),
  }));
  const edits = reel.settings.subtitleEdits || {};
  const words = reel.words
    .map((w, pos) => {
      const key = w.index != null ? w.index : pos; // match the editor's global-index key
      const text = edits[key] != null ? edits[key] : w.word;
      return { word: text, start: w.start, end: w.end, time: w.start, speaker: 0 };
    })
    // Words blanked in the editor are removed from the burned-in subtitles.
    .filter((w) => String(w.word).trim() !== "");
  const rf = reel.settings.reframe || {};
  const music = reel.settings.music;
  return {
    id: reel.id,
    title: reel.title,
    editor_cut_sheet: editorCutSheet,
    timestamped_words: words,
    options: {
      // "Remove fillers" only hides filler words from the burned-in captions and
      // the transcript editor preview; it no longer cuts them out of the video.
      cutSilences: !!reel.settings.removeSilences,
      canvas: {
        cropX: rf.cropX != null ? rf.cropX : 0.5,
        cropY: rf.cropY != null ? rf.cropY : 0.5,
        zoom: rf.zoom || 1,
        panX: rf.panX || 0,
        panY: rf.panY || 0,
      },
      music: music && music.path ? { path: music.path, volume: music.volume } : null,
    },
  };
}

/** Filesystem-safe output basename for a reel (matches export_cli.py). */
function safeName(reelId, title) {
  const safe = String(title || `reel_${reelId}`)
    .replace(/[^a-zA-Z0-9_-]+/g, "_")
    .slice(0, 48)
    .replace(/^_+|_+$/g, "");
  const idStr = String(reelId).padStart(2, "0");
  return `reel_${idStr}_${safe || "reel"}_916`;
}

/** Bound concurrent reel exports — min(4, cpuCount, jobCount), REEL_MAX_WORKERS overrides. */
function resolveMaxWorkers(jobCount) {
  const override = Number(process.env.REEL_MAX_WORKERS);
  if (Number.isInteger(override) && override > 0) return Math.max(1, Math.min(override, jobCount));
  const cores = (os.cpus() || []).length || 4;
  return Math.max(1, Math.min(4, cores, jobCount));
}

/** Run `tasks` with at most `limit` in flight at once. */
async function runPool(items, limit, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  async function drain() {
    while (cursor < items.length) {
      const i = cursor;
      cursor += 1;
      results[i] = await worker(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, drain));
  return results;
}

/**
 * Build each reel's payload and render it via the bundled Node/FFmpeg engine,
 * streaming progress events. Reels render concurrently (bounded pool).
 * @returns {Promise<string[]>} exported file paths
 */
async function exportReels({ srcPath, outDir, reels, dialog, cameras, onEvent }) {
  const losslessAudio = !!dialog.losslessAudio;
  // Raw PCM audio has poor compatibility inside an MP4 container — MOV (QuickTime)
  // supports it properly, so force the container when lossless audio is requested.
  const format = losslessAudio ? "mov" : dialog.format || "mp4";
  const ext = String(format).toLowerCase() === "mov" ? "mov" : "mp4";

  const globalOptions = {
    resolution: RESOLUTIONS[dialog.resolution] || RESOLUTIONS["1080p"],
    fps: dialog.fps || "source",
    quality: QUALITY[dialog.quality] || "medium",
    encodePreset: PRESET[dialog.quality] || "faster",
    losslessAudio,
  };
  // Multi-camera (optional): {camera_id: {path, offsetSec}} — omitted entirely
  // for ordinary single-camera projects, which export exactly as before.
  if (cameras && cameras.length) {
    globalOptions.sources = Object.fromEntries(
      cameras.map((c) => [c.id, { path: c.path, offsetSec: Number(c.offsetSec) || 0 }]),
    );
  }

  if (!fs.existsSync(srcPath)) throw new Error(`Master not found: ${srcPath}`);
  fs.mkdirSync(outDir, { recursive: true });

  const specs = reels.map(toExportReel);
  const total = specs.length;
  const outputs = new Array(total).fill(null);
  const errors = [];

  console.log(`[export] rendering ${total} reel(s) -> ${outDir} (Node engine, no Python)`);

  await runPool(specs, resolveMaxWorkers(total), async (reelSpec, i) => {
    const index = i + 1;
    const title = reelSpec.title || `reel_${reelSpec.id ?? index}`;
    onEvent({ status: "exporting", index, total, message: title });

    // Per-reel options override the dialog-wide globals (matches the Python
    // {**global, **reel.options} merge).
    const mergedOptions = { ...globalOptions, ...(reelSpec.options || {}) };
    const outPath = path.join(outDir, `${safeName(reelSpec.id ?? index, title)}.${ext}`);

    try {
      const payload = buildExportPayload(reelSpec, mergedOptions);
      await engine.exportReel(srcPath, outPath, payload);
      if (!fs.existsSync(outPath) || fs.statSync(outPath).size < 20000) {
        throw new Error(`Export too small or missing: ${path.basename(outPath)}`);
      }
      outputs[i] = outPath;
      onEvent({ status: "reel-done", index });
      console.log(`[export] [${index}/${total}] OK ${path.basename(outPath)}`);
    } catch (err) {
      const message = `reel ${index} (${title}): ${err.message}`;
      errors.push(message);
      onEvent({ status: "error", message });
      console.error(`[export] ${message}`);
    }
  });

  const done = outputs.filter(Boolean);
  // If every reel failed, surface a hard error so the UI shows "Export failed"
  // rather than a misleading "Done — 0 files". A partial success still returns
  // the reels that did render (the UI reports the count).
  if (!done.length) {
    throw new Error(errors.join("; ") || "Export produced no files");
  }
  console.log(`[export] done: ${done.length}/${total} file(s)`);
  return done;
}

module.exports = { exportReels, RESOLUTIONS, QUALITY };
