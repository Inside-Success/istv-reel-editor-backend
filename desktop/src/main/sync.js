"use strict";

/**
 * Camera sync orchestrator. Spawns the repo's `sync_cameras_cli.py`, which
 * computes each camera's fixed time offset against a dedicated reference
 * audio recording via FFT cross-correlation (`src/camera_sync.py`). Mirrors
 * `export.js`'s spawn/parse pattern so the two share the same shape.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const ffmpeg = require("./ffmpeg");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");
const SYNC_CLI = path.join(REPO_ROOT, "sync_cameras_cli.py");

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

/**
 * @param {{referenceAudioPath: string, cameras: {id: string, path: string}[], onEvent: Function}} args
 * @returns {Promise<Record<string, {offsetSec: number, confidence: number}>>}
 */
function syncCameras({ referenceAudioPath, cameras, onEvent }) {
  return new Promise((resolve, reject) => {
    const spec = {
      referenceAudioPath,
      cameras: Object.fromEntries(cameras.map((c) => [c.id, c.path])),
    };
    const specDir = fs.mkdtempSync(path.join(os.tmpdir(), "istv-sync-"));
    const specPath = path.join(specDir, "spec.json");
    fs.writeFileSync(specPath, JSON.stringify(spec), "utf8");

    const results = {};
    let stderr = "";
    const py = resolvePython();
    const child = spawn(py, [SYNC_CLI, specPath], {
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
        onEvent({ status: "syncing", cameraId: parts[1] });
      } else if (tag === "CAMERA_DONE") {
        const cameraId = parts[1];
        const offsetSec = Number(parts[2]);
        const confidence = Number(parts[3]);
        results[cameraId] = { offsetSec, confidence };
        onEvent({ status: "camera-done", cameraId, offsetSec, confidence });
      } else if (tag === "ERROR") {
        onEvent({ status: "error", message: parts.slice(1).join(" ") });
      }
    }

    child.on("error", (err) => {
      try { fs.rmSync(specDir, { recursive: true, force: true }); } catch (_e) {}
      reject(new Error(`Could not start sync (${err.message}). Is Python available?`));
    });
    child.on("close", (code) => {
      try { fs.rmSync(specDir, { recursive: true, force: true }); } catch (_e) {}
      if (code === 0) {
        resolve(results);
      } else {
        reject(new Error(stderr.trim().slice(-400) || `Sync exited ${code}`));
      }
    });
  });
}

module.exports = { syncCameras };
