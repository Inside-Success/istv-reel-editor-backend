"use strict";

const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");
const os = require("os");
const C = require("./channels");
const ffmpeg = require("./ffmpeg");
const backend = require("./backend");
const { toReferenceReels } = require("./reels");
const exporter = require("./export");
const camSync = require("./sync");

const isSmoke = process.argv.includes("--smoke");

/** Per-machine cache dir for generated proxies (outside the project tree). */
function proxyCacheDir() {
  const dir = path.join(app.getPath("userData"), "proxies");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

// Bump when the proxy encode settings change, so stale cached proxies rebuild.
const PROXY_VERSION = "v2-audio";

/** Stable proxy filename derived from source path + mtime + size + encode version. */
function proxyPathFor(srcPath) {
  const stat = fs.statSync(srcPath);
  const key = `${srcPath}|${stat.size}|${Math.round(stat.mtimeMs)}|${PROXY_VERSION}`;
  const hash = crypto.createHash("sha1").update(key).digest("hex").slice(0, 16);
  return path.join(proxyCacheDir(), `proxy_${hash}.mp4`);
}

let mainWindow = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 680,
    backgroundColor: "#16181d",
    show: false,
    title: "ISTV Reel Editor",
    webPreferences: {
      preload: path.join(__dirname, "..", "preload", "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "..", "renderer", "index.html"));

  mainWindow.once("ready-to-show", () => {
    if (isSmoke) {
      // Smoke test: confirm the window + binaries load, then exit cleanly.
      try {
        ffmpeg.assertBinaries();
        console.log("SMOKE_OK ffmpeg+window loaded");
      } catch (e) {
        console.error("SMOKE_FAIL", e.message);
        app.exit(1);
        return;
      }
      setTimeout(() => app.quit(), 300);
    } else {
      mainWindow.show();
    }
  });

  mainWindow.on("closed", () => (mainWindow = null));
}

// ── IPC handlers ────────────────────────────────────────────────────────────

ipcMain.handle(C.OPEN_PROJECT_DIALOG, async () => {
  const res = await dialog.showOpenDialog(mainWindow, {
    title: "Open documentary",
    properties: ["openFile"],
    filters: [
      { name: "Video", extensions: ["mp4", "mov", "mkv", "avi", "webm", "m4v"] },
      { name: "All files", extensions: ["*"] },
    ],
  });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

ipcMain.handle(C.PROBE_MEDIA, async (_evt, filePath) => {
  return ffmpeg.probe(filePath);
});

ipcMain.handle(C.PICK_AUDIO, async () => {
  const res = await dialog.showOpenDialog(mainWindow, {
    title: "Add background music",
    properties: ["openFile"],
    filters: [
      { name: "Audio", extensions: ["mp3", "m4a", "aac", "wav", "ogg", "flac"] },
      { name: "All files", extensions: ["*"] },
    ],
  });
  if (res.canceled || !res.filePaths.length) return null;
  const p = res.filePaths[0];
  const { pathToFileURL } = require("url");
  return { path: p, name: path.basename(p), url: pathToFileURL(p).href };
});

ipcMain.handle(C.SAVE_PROJECT, async (_evt, project) => {
  const res = await dialog.showSaveDialog(mainWindow, {
    title: "Save project",
    defaultPath: "reels-project.istv.json",
    filters: [{ name: "ISTV Project", extensions: ["istv.json", "json"] }],
  });
  if (res.canceled || !res.filePath) return null;
  fs.writeFileSync(res.filePath, JSON.stringify(project, null, 2), "utf8");
  return res.filePath;
});

ipcMain.handle(C.LOAD_PROJECT, async () => {
  const res = await dialog.showOpenDialog(mainWindow, {
    title: "Open saved project",
    properties: ["openFile"],
    filters: [{ name: "ISTV Project", extensions: ["istv.json", "json"] }],
  });
  if (res.canceled || !res.filePaths.length) return null;
  return JSON.parse(fs.readFileSync(res.filePaths[0], "utf8"));
});

ipcMain.handle(C.TO_FILE_URL, async (_evt, filePath) => {
  // Build a file:// URL that survives spaces / non-ASCII on both platforms.
  const { pathToFileURL } = require("url");
  return pathToFileURL(filePath).href;
});

ipcMain.handle(C.GENERATE_PROXY, async (evt, srcPath) => {
  if (!srcPath || !fs.existsSync(srcPath)) {
    throw new Error(`Source not found: ${srcPath}`);
  }
  const outPath = proxyPathFor(srcPath);
  const meta = await ffmpeg.probe(srcPath);

  // Reuse a cached proxy if present (non-destructive, idempotent import).
  if (fs.existsSync(outPath) && fs.statSync(outPath).size > 1024) {
    evt.sender.send(C.PROXY_PROGRESS, 1);
    const { pathToFileURL } = require("url");
    return { proxyPath: outPath, proxyUrl: pathToFileURL(outPath).href, meta, cached: true };
  }

  await ffmpeg.generateProxy(srcPath, outPath, {
    onProgress: (p) => evt.sender.send(C.PROXY_PROGRESS, p),
  });
  evt.sender.send(C.PROXY_PROGRESS, 1);
  const { pathToFileURL } = require("url");
  return { proxyPath: outPath, proxyUrl: pathToFileURL(outPath).href, meta, cached: false };
});

/**
 * Phase 2 pipeline: extract audio -> compress -> upload ONLY audio -> Rev.ai
 * word-level transcript. Emits granular per-step events so the renderer's
 * pipeline panel shows live progress and any failure surfaces clearly.
 */
ipcMain.handle(C.GENERATE_REELS, async (evt, srcPath, name) => {
  const speakerName = String(name || "").trim();
  const emit = (step, status, extra = {}) =>
    evt.sender.send(C.PIPELINE_EVENT, { step, status, ...extra });

  if (!srcPath || !fs.existsSync(srcPath)) {
    throw new Error(`Source not found: ${srcPath}`);
  }

  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "istv-reels-"));
  const wavPath = path.join(workDir, "audio.wav");
  const mp3Path = path.join(workDir, "audio.mp3");

  const fail = (step, err) => {
    emit(step, "error", { message: err.message });
    cleanupDir(workDir);
    throw err;
  };

  // Step 0: confirm the backend is reachable before doing local work.
  emit("connect", "active", { message: backend.BACKEND_URL });
  try {
    const h = await backend.health();
    if (!h.revai_key) throw new Error("Backend has no Rev.ai key configured");
    emit("connect", "done", { message: "Backend ready" });
  } catch (e) {
    return fail("connect", new Error(`Cannot reach backend: ${e.message}`));
  }

  // Step 1: extract audio.
  emit("extract", "active", { progress: 0 });
  try {
    await ffmpeg.extractAudio(srcPath, wavPath, {
      onProgress: (p) => emit("extract", "active", { progress: p }),
    });
    emit("extract", "done");
  } catch (e) {
    return fail("extract", e);
  }

  // Step 2: compress to mono 16 kHz (the only thing that leaves the machine).
  emit("compress", "active", { progress: 0 });
  let bytes = 0;
  try {
    const r = await ffmpeg.compressAudio(wavPath, mp3Path, {
      onProgress: (p) => emit("compress", "active", { progress: p }),
    });
    bytes = r.bytes;
    emit("compress", "done", { message: `${(bytes / 1024 / 1024).toFixed(1)} MB` });
  } catch (e) {
    return fail("compress", e);
  }

  // Step 3: upload only the compressed audio.
  let jobId;
  emit("upload", "active", { progress: 0 });
  try {
    const r = await backend.uploadAudio(mp3Path, {
      onProgress: (p) => emit("upload", "active", { progress: p }),
    });
    jobId = r.job_id;
    emit("upload", "done", { message: `${(bytes / 1024 / 1024).toFixed(1)} MB sent` });
  } catch (e) {
    return fail("upload", e);
  }

  // Step 4: transcribe (Rev.ai word-level).
  emit("transcribe", "active", { message: "Submitting to Rev.ai…" });
  let transcript;
  try {
    // pollJob resolves with the full job-status object; the transcript is on .transcript
    const finalStatus = await backend.pollJob(jobId, {
      onStatus: (s) =>
        emit("transcribe", "active", {
          message: s.message,
          elapsed: s.elapsed,
        }),
    });
    transcript = finalStatus.transcript;
    if (!transcript || transcript.word_count == null) {
      throw new Error("Backend returned no transcript");
    }
    emit("transcribe", "done", {
      message: `${transcript.word_count.toLocaleString()} words · ${Math.round(
        transcript.duration
      )}s`,
    });
  } catch (e) {
    return fail("transcribe", e);
  }

  cleanupDir(workDir);

  // Step 5: reel selection (Claude) — returns cut instructions.
  emit("select", "active", { message: "Selecting reel moments…" });
  let analysis;
  try {
    analysis = await backend.selectReels(transcript, speakerName || "", 10, {
      onStatus: (s) => emit("select", "active", { message: s.message }),
    });
    const n = (analysis.reels || []).length;
    emit("select", "done", { message: `${n} reels selected` });
  } catch (e) {
    return fail("select", e);
  }

  // Step 6: cut into non-destructive in/out references — step through each reel.
  emit("cut", "active", { progress: 0 });
  const reels = toReferenceReels(analysis);
  reels.forEach((reel, i) => {
    emit("cut", "active", {
      progress: (i + 1) / reels.length,
      message: `Reel ${i + 1}/${reels.length}: ${reel.title}`,
    });
  });
  emit("cut", "done", { message: `${reels.length} reels cut (in/out references)` });

  return { transcript, analysis, reels };
});

function cleanupDir(dir) {
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch (_e) {
    /* best-effort */
  }
}

// ── Export (Phase 5) ────────────────────────────────────────────────────────────

ipcMain.handle(C.PICK_EXPORT_DIR, async () => {
  const res = await dialog.showOpenDialog(mainWindow, {
    title: "Choose export destination",
    properties: ["openDirectory", "createDirectory"],
  });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

ipcMain.handle(C.EXPORT_REELS, async (evt, { srcPath, outDir, reels, dialog: dlg, cameras }) => {
  if (!srcPath || !fs.existsSync(srcPath)) throw new Error(`Master not found: ${srcPath}`);
  if (!outDir) throw new Error("No export destination chosen");
  if (!reels || !reels.length) throw new Error("No reels to export");

  const outputs = await exporter.exportReels({
    srcPath,
    outDir,
    reels,
    dialog: dlg,
    cameras,
    onEvent: (e) => evt.sender.send(C.EXPORT_EVENT, e),
  });
  return { outputs };
});

// ── Multi-camera sync ────────────────────────────────────────────────────────

ipcMain.handle(C.PICK_REFERENCE_AUDIO, async () => {
  const res = await dialog.showOpenDialog(mainWindow, {
    title: "Add reference audio recorder file",
    properties: ["openFile"],
    filters: [
      { name: "Audio", extensions: ["mp3", "m4a", "aac", "wav", "ogg", "flac"] },
      { name: "All files", extensions: ["*"] },
    ],
  });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

ipcMain.handle(C.ADD_CAMERA_DIALOG, async () => {
  const res = await dialog.showOpenDialog(mainWindow, {
    title: "Add camera video file",
    properties: ["openFile"],
    filters: [
      { name: "Video", extensions: ["mp4", "mov", "mkv", "avi", "webm", "m4v"] },
      { name: "All files", extensions: ["*"] },
    ],
  });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

ipcMain.handle(C.SYNC_CAMERAS, async (evt, { referenceAudioPath, cameras }) => {
  if (!referenceAudioPath || !fs.existsSync(referenceAudioPath)) {
    throw new Error(`Reference audio not found: ${referenceAudioPath}`);
  }
  if (!cameras || !cameras.length) throw new Error("No cameras to sync");
  for (const cam of cameras) {
    if (!fs.existsSync(cam.path)) throw new Error(`Camera "${cam.id}" file not found: ${cam.path}`);
  }
  const results = await camSync.syncCameras({
    referenceAudioPath,
    cameras,
    onEvent: (e) => evt.sender.send(C.SYNC_EVENT, e),
  });
  return { results };
});

// ── App lifecycle ─────────────────────────────────────────────────────────────

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
