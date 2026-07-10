# ISTV Reel Editor (Desktop)

Cross-platform desktop editor (Windows + macOS) that turns a long-form
documentary into short-form 9:16 reels and lets an editor refine and export
them. Local-first: the full video never leaves the machine; only compressed
audio is uploaded for transcription.

Built **one phase at a time** — each phase is fully working and verified before
the next begins.

## Architecture

- **Shell:** Electron (renderer = UI only; all heavy work in the main process / child processes).
- **Media engine:** FFmpeg as a bundled per-platform sidecar (`ffmpeg-static` / `ffprobe-static`).
- **Data model (non-destructive):** one **master clip** (full-res local file). Each reel
  is a **reference** into the master — an in/out point plus subtitle/effect settings.
  "Cut" = move handles inward; "Extend" = move handles outward. Nothing is rendered
  until **export**, which renders from the full-res master with all settings baked in.
- **Proxy:** on import, a low-res keyframe-dense proxy is generated for smooth
  scrubbing; editing happens against the proxy, export against the master.

## Getting started (clone & run)

Requires **Node.js 20+** and **git**. Works on macOS and Windows.

```bash
git clone https://github.com/Inside-Success/istv-reel-editor-backend.git
cd istv-reel-editor-backend/desktop
npm install
npm start
```

To build a distributable installer for the OS you're on:

```bash
npm run dist   # macOS -> .dmg, Windows -> .exe (NSIS), written to desktop/dist/
```

> A macOS `.dmg` can only be built on a Mac and a Windows `.exe` only on Windows —
> electron-builder does not cross-build these.

> **Export needs more than Node.** Opening, editing and previewing reels runs on
> Node alone. The full **Generate Reels → Export** pipeline additionally spawns the
> repo's Python `.venv` and talks to the backend (see `../backend`).

## Run

```bash
cd desktop
npm install
npm start
```

> `npm start` uses `scripts/launch.js`, which strips `ELECTRON_RUN_AS_NODE`
> before spawning Electron. Some hosts (VS Code's terminal/extension host) set
> that variable, which would otherwise make Electron run as plain Node and crash
> the main process. The launcher makes `npm start` work everywhere.

`npm run smoke` boots the app, verifies the bundled FFmpeg/FFprobe binaries and
the window load, then exits 0 — used for headless CI-style checks.

## Phase status

- **Phase 1 — Shell + open project:** ✅ App launches; opens a local documentary
  as the master clip; generates a low-res proxy with live progress; frame-accurate
  scrubbing, transport (play/pause, frame step, start/end), timeline ruler with
  zoom, and source metadata inspector.
- **Phase 2 — Audio → cloud → transcript:** ✅ "Generate Reels" extracts audio →
  compresses to mono 16 kHz MP3 → uploads **only** that audio to the hosted
  backend → Rev.ai returns a verbatim word-level transcript. A pipeline panel
  steps through Connect → Extract → Compress → Upload → Transcribe with live
  progress; failures surface per-step. Backend lives in `../backend` (FastAPI,
  reuses `src/`, holds the API keys). Verified end-to-end against live Rev.ai.
- **Phase 3 — Local cut into reels:** ✅ The pipeline continues into Claude
  selection (backend `/select`, v2_test2 profile) and turns the returned cut
  instructions into **non-destructive in/out reference reels** (`src/main/reels.js`)
  — each reel is an ordered list of {startSec, endSec} spans into the master, not
  a rendered file. The pipeline panel steps through Select → Cut (per reel). The
  reels panel lists every reel; clicking one loads its span(s) onto the timeline.
  Verified end-to-end: 10 reels (30–93s) with correct spans + metadata.
- **Phase 4 — Editing (locked options):** ✅ Select a reel to open the editor:
  - **Cut / Extend** — drag the in/out handles on the reel in the timeline, or use
    the Trim buttons. Both just move two numbers against the master (non-destructive).
  - **Subtitles** — auto word-highlight overlay synced to playback; every word is
    editable in the side panel (click a word).
  - **Filler removal / Silence removal** — toggles stored per reel (applied at
    export by the engine's `cutFillersFromVideo` / `cutSilences`); fillers also drop
    from the subtitle display live.
  - **9:16 reframe** — "9:16 Preview" shows the vertical crop; drag to reposition,
    zoom slider to scale (maps to the engine's `cropX/cropY/zoom`).
  - **Add music** — pick a track + volume (mixed under the audio at export).
  - **Save** persists the project (`.istv.json`); **Undo/Redo** (Ctrl+Z / Ctrl+Y)
    across every edit.

  The cut/extend math, non-destructive guarantee, filler logic and subtitle edits
  are covered by `test/model.test.js` (`npm test`, 13 cases). Interactive GUI
  walkthrough (drag/click/playback) needs a display — see the checklist below.
- **Phase 5 — Export:** ✅ CapCut-style dialog: scope (selected / all reels),
  resolution (720p / 1080p / 2K / 4K vertical 9:16), frame rate (24/25/30/50/60),
  quality (Lower / Recommended / Higher), format (MP4 H.264 / MOV), and a
  destination folder picker, with live progress. Export renders from the
  **full-res master** by spawning `export_cli.py`, which reuses the same caption
  builder + `media.cjs` engine and bakes in **every** edit — cut/extend (in/out),
  edited word-highlight subtitles, filler removal, silence removal, 9:16 reframe,
  and music. The app prepends the bundled ffmpeg/ffprobe dirs to PATH for the
  spawned process so rendering works without a system FFmpeg install.

  > Export currently spawns the repo's Python (`.venv`) to drive the proven
  > engine. Bundling a frozen Python/Node export sidecar for a distributable
  > build is a packaging task (same bucket as the macOS build + signing).

## Manual GUI checklist (Phase 1–4, needs a display)

1. `npm start`, **Open Project**, pick a video → proxy builds, scrubbing is smooth.
2. Type a speaker name → **Generate Reels** → pipeline panel steps Connect → Extract
   → Compress → Upload → Transcribe → Select → Cut; reels fill the left panel.
3. Click a reel → editor opens. Drag the **in/out handles** (timeline) to cut and
   extend; the duration updates instantly.
4. Toggle **Subtitles**, **Remove fillers**, **Remove silences**; click a subtitle
   word and edit it. Press Space to play — the word-highlight follows the audio.
5. Click **9:16 Preview**, drag to reposition, move the **Zoom** slider.
6. **Add music…**, set volume.
7. **Undo/Redo** several edits; **Save** the project.

## Cross-platform notes

- FFmpeg/FFprobe binary paths are resolved at runtime (never hardcoded) and
  rewritten from `app.asar` → `app.asar.unpacked` for packaged builds.
- All file paths are passed as argv entries (never concatenated into shell
  strings) and converted to `file://` URLs via `pathToFileURL`, so spaces and
  non-ASCII names are safe on both Windows and macOS.
- macOS build/sign/notarize is a later pass (Windows-first per project decision).
