"use strict";

/**
 * Export orchestrator. Renders edited reels from the FULL-RES master by spawning
 * the repo's `export_cli.py`, which reuses the proven caption builder + Node/
 * FFmpeg engine (`media.cjs`). We reuse rather than re-implement so exported
 * reels match the tool's approved karaoke/reframe output exactly.
 *
 * The bundled ffmpeg/ffprobe directories are prepended to PATH for the spawned
 * process so the engine's `ffmpeg`/`ffprobe` calls resolve even without a system
 * install. All paths are passed as argv / JSON — never shell-concatenated.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const ffmpeg = require("./ffmpeg");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");
const EXPORT_CLI = path.join(REPO_ROOT, "export_cli.py");

const RESOLUTIONS = {
  "720p": { width: 720, height: 1280 },
  "1080p": { width: 1080, height: 1920 },
  "2K": { width: 1440, height: 2560 },
  "4K": { width: 2160, height: 3840 },
  // Sentinel: export at the source's native resolution (crop only, no scale)
  // instead of a fixed pixel target — see media.cjs exportReel.
  Original: { width: "original", height: "original" },
};
const QUALITY = { Lower: "low", Recommended: "medium", Higher: "high" };
// Faster x264 presets for snappier desktop exports (CRF still governs quality).
const PRESET = { Lower: "veryfast", Recommended: "faster", Higher: "medium" };

function resolvePython() {
  const win = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
  const nix = path.join(REPO_ROOT, ".venv", "bin", "python");
  if (fs.existsSync(win)) return win;
  if (fs.existsSync(nix)) return nix;
  return process.platform === "win32" ? "python" : "python3";
}

function spawnEnv() {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  const extra = [path.dirname(ffmpeg.ffmpegPath()), path.dirname(ffmpeg.ffprobePath())];
  env.PATH = extra.join(path.delimiter) + path.delimiter + (env.PATH || "");
  return env;
}

/** Convert an app reel (in/out references + settings) into the CLI's reel spec. */
function toExportReel(reel) {
  // Multi-camera (optional): reel.settings.camera picks which camera this
  // WHOLE reel pulls footage from — applied uniformly to every segment. The
  // editor doesn't yet expose per-segment (per-cut) camera switching, but the
  // export/media.cjs layer already supports it per-segment; a future editor UI
  // could set `camera` per-span without any pipeline changes.
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
      // "Remove fillers" only hides filler words from the burned-in captions
      // (always on server-side, see media.cjs hideFillersInSubtitles) and from
      // the transcript editor preview (model.js) — it no longer cuts them out
      // of the video, since word-level cuts risked exploding the ffmpeg command
      // line on filler-heavy reels. reel.settings.removeFillers still drives the
      // editor preview; there's nothing left for it to control at export time.
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

/**
 * Build the spec, spawn the exporter, stream progress events.
 * @returns {Promise<string[]>} exported file paths
 */
function exportReels({ srcPath, outDir, reels, dialog, cameras, onEvent }) {
  return new Promise((resolve, reject) => {
    const losslessAudio = !!dialog.losslessAudio;
    // Raw PCM audio has poor compatibility inside an MP4 container — MOV (QuickTime)
    // supports it properly, so force the container when lossless audio is requested.
    const format = losslessAudio ? "mov" : dialog.format || "mp4";
    const options = {
      resolution: RESOLUTIONS[dialog.resolution] || RESOLUTIONS["1080p"],
      fps: dialog.fps || "source",
      quality: QUALITY[dialog.quality] || "medium",
      encodePreset: PRESET[dialog.quality] || "faster",
      losslessAudio,
    };
    // Multi-camera (optional): {camera_id: {path, offsetSec}} — omitted entirely
    // for ordinary single-camera projects, which export exactly as before.
    if (cameras && cameras.length) {
      options.sources = Object.fromEntries(
        cameras.map((c) => [c.id, { path: c.path, offsetSec: Number(c.offsetSec) || 0 }]),
      );
    }
    const spec = {
      source: srcPath,
      outDir,
      format,
      options,
      reels: reels.map(toExportReel),
    };

    const specPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "istv-export-")), "spec.json");
    fs.writeFileSync(specPath, JSON.stringify(spec), "utf8");

    const outputs = [];
    let stderr = "";
    const py = resolvePython();
    console.log(`[export] spawning: ${py} ${EXPORT_CLI} (${spec.reels.length} reel(s) -> ${outDir})`);
    const child = spawn(py, [EXPORT_CLI, specPath], {
      cwd: REPO_ROOT,
      env: spawnEnv(),
      windowsHide: true,
    });

    let buf = "";
    child.stdout.on("data", (d) => {
      buf += d.toString();
      const lines = buf.split(/\r?\n/);
      buf = lines.pop();
      for (const line of lines) handleLine(line.trim());
    });
    child.stderr.on("data", (d) => (stderr += d.toString()));

    function handleLine(line) {
      if (!line) return;
      const parts = line.split(" ");
      const tag = parts[0];
      if (tag === "PROGRESS") {
        onEvent({
          status: "exporting",
          index: Number(parts[1]),
          total: Number(parts[2]),
          message: parts.slice(4).join(" "),
        });
      } else if (tag === "REEL_DONE") {
        outputs.push(parts.slice(2).join(" "));
        onEvent({ status: "reel-done", index: Number(parts[1]) });
      } else if (tag === "ERROR") {
        onEvent({ status: "error", message: parts.slice(1).join(" ") });
      }
    }

    child.on("error", (err) => {
      console.error(`[export] spawn error: ${err.message}`);
      try { fs.rmSync(path.dirname(specPath), { recursive: true, force: true }); } catch (_e) {}
      reject(new Error(`Could not start exporter (${err.message}). Is Python available?`));
    });
    child.on("close", (code) => {
      try { fs.rmSync(path.dirname(specPath), { recursive: true, force: true }); } catch (_e) {}
      if (code === 0) {
        console.log(`[export] done: ${outputs.length} file(s)`);
        resolve(outputs);
      } else {
        console.error(`[export] exited ${code}: ${stderr.trim().slice(-800)}`);
        reject(new Error(stderr.trim().slice(-400) || `Exporter exited ${code}`));
      }
    });
  });
}

module.exports = { exportReels, RESOLUTIONS, QUALITY };
