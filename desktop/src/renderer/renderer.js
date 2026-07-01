"use strict";

/* Phase 1 renderer: open a local master clip, build/load its proxy, and provide
 * frame-accurate scrubbing + transport against the proxy. No editing yet. */

const $ = (id) => document.getElementById(id);

const state = {
  srcPath: null,
  proxyUrl: null,
  meta: null, // { durationSec, width, height, fps, codec, hasAudio }
  fps: 30,
  pxPerSec: 0, // base fit-to-width
  zoom: 1,
  transcript: null, // set after Generate Reels (Phase 2)
  reels: [], // reference reels (Phase 3)
  selectedReelId: null,
  undo: [],
  redo: [],
  // Multi-camera (optional, additive) — see plan doc / theupdatelog.md.
  // referenceAudioPath: the dedicated recorder file; only this is transcribed.
  // cameras: [{ id, label, path, offsetSec, confidence }] — offsetSec/confidence
  // are set once "Sync" runs (see runCameraSync). Empty for ordinary
  // single-camera projects, which behave exactly as before.
  referenceAudioPath: null,
  cameras: [],
};

const editor = { previewMode: "916" }; // '916' (default) | 'fit'

const video = $("video");

// ── Helpers ───────────────────────────────────────────────────────────────

function frameDur() {
  return 1 / (state.fps || 30);
}

/** Snap a time to the nearest frame boundary. */
function snapFrame(t) {
  const f = Math.round(t * state.fps);
  return f / state.fps;
}

function fmtTC(sec) {
  sec = Math.max(0, sec || 0);
  const fps = state.fps || 30;
  const totalFrames = Math.round(sec * fps);
  const f = totalFrames % Math.round(fps);
  const totalSecs = Math.floor(totalFrames / fps);
  const s = totalSecs % 60;
  const m = Math.floor(totalSecs / 60) % 60;
  const h = Math.floor(totalSecs / 3600);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(h)}:${p(m)}:${p(s)}:${p(f)}`;
}

function setStatus(msg) {
  $("status").textContent = msg;
}

// ── Open project ─────────────────────────────────────────────────────────────

/** Clear everything scoped to the previously-open project before loading a new
 * one — otherwise its reels, cameras, speaker name, and undo history would
 * silently carry over and mix with the next project. */
function resetProjectState() {
  state.transcript = null;
  state.reels = [];
  state.selectedReelId = null;
  state.undo = [];
  state.redo = [];
  state.referenceAudioPath = null;
  state.cameras = [];

  $("nameInput").value = "";
  $("reelInspector").classList.add("hidden");
  $("sourceInspector").classList.remove("hidden");
  renderReelsPanel();
  resetPipeline();
  $("pipelineOverlay").classList.add("hidden");
  $("exportOverlay").classList.add("hidden");
  $("camerasOverlay").classList.add("hidden");
  initHistory();
}

async function openProject() {
  const filePath = await window.api.openProjectDialog();
  if (!filePath) return;
  if (dirty && !confirm("You have unsaved changes in the current project. Open a different project anyway?")) {
    return;
  }
  resetProjectState();
  state.srcPath = filePath;

  $("proxyOverlay").classList.remove("hidden");
  $("proxyBar").style.width = "0%";
  $("proxyPct").textContent = "0%";
  setStatus("Probing source…");

  const off = window.api.onProxyProgress((p) => {
    const pct = Math.round(p * 100);
    $("proxyBar").style.width = pct + "%";
    $("proxyPct").textContent = pct + "%";
    $("proxySub").textContent =
      p >= 1 ? "Finalizing…" : "Building low-res proxy for smooth scrubbing";
  });

  try {
    const meta = await window.api.probeMedia(filePath);
    state.meta = meta;
    state.fps = meta.fps || 30;
    setStatus("Building proxy…");

    const res = await window.api.generateProxy(filePath);
    state.proxyUrl = res.proxyUrl;
    state.meta = res.meta || meta;
    state.fps = state.meta.fps || 30;

    loadMaster(res);
    setStatus(
      res.cached
        ? "Master loaded (cached proxy). Ready to edit."
        : "Master loaded. Proxy built. Ready to edit."
    );
  } catch (err) {
    setStatus("Failed to open: " + err.message);
    alert("Could not open project:\n\n" + err.message);
  } finally {
    off();
    $("proxyOverlay").classList.add("hidden");
  }
}

function loadMaster(res) {
  const m = state.meta;

  // Inspector metadata
  $("mFile").textContent = state.srcPath.split(/[\\/]/).pop();
  $("mFile").title = state.srcPath;
  $("mRes").textContent = m.width && m.height ? `${m.width}×${m.height}` : "—";
  $("mDur").textContent = fmtTC(m.durationSec);
  $("mFps").textContent = (m.fps || 0).toFixed(3).replace(/\.?0+$/, "") + " fps";
  $("mCodec").textContent = m.codec || "—";
  $("mProxy").textContent = res.cached ? "cached" : "built";

  $("clipName").textContent = state.srcPath.split(/[\\/]/).pop();
  $("tcTotal").textContent = fmtTC(m.durationSec);

  // Swap viewer state
  $("dropHint").classList.add("hidden");
  $("stage").classList.remove("hidden");

  // Load proxy into the player
  video.src = state.proxyUrl;
  video.load();
  video.currentTime = 0;

  buildTimeline();
  layoutTimeline();
  updatePlayhead();

  // Default to the 9:16 preview so the editor shows the export framing.
  $("previewMode").classList.toggle("active", editor.previewMode === "916");
  applyReframe();

  $("genBtn").disabled = !state.meta.hasAudio;
  $("genBtn").title = state.meta.hasAudio ? "" : "Source has no audio track";
  $("nameInput").disabled = false;
  $("camerasBtn").disabled = false;
}

// ── Generate Reels pipeline (Phase 2 + 3) ──────────────────────────────────────

const PIPELINE_STEPS = ["connect", "extract", "compress", "upload", "transcribe", "select", "cut"];

function resetPipeline() {
  for (const step of PIPELINE_STEPS) {
    const li = document.querySelector(`.pipeline li[data-step="${step}"]`);
    li.classList.remove("active", "done", "error");
    li.querySelector(".pl-ico").textContent = "○";
    li.querySelector(".pl-msg").textContent = "";
    li.querySelector(".pl-bar > div").style.width = "0%";
  }
  $("pipelineClose").disabled = true;
}

function applyPipelineEvent(e) {
  const li = document.querySelector(`.pipeline li[data-step="${e.step}"]`);
  if (!li) return;
  li.classList.remove("active", "done", "error");
  li.classList.add(e.status);

  const ico = li.querySelector(".pl-ico");
  ico.textContent = e.status === "done" ? "✓" : e.status === "error" ? "✕" : "◐";

  const msg = li.querySelector(".pl-msg");
  if (typeof e.progress === "number") {
    li.querySelector(".pl-bar > div").style.width = Math.round(e.progress * 100) + "%";
    if (!e.message) msg.textContent = Math.round(e.progress * 100) + "%";
  }
  if (e.message) msg.textContent = e.message;
  if (e.step === "transcribe" && e.status === "active" && e.elapsed) {
    msg.textContent = `${e.message || "Transcribing…"} (${e.elapsed}s)`;
  }
  if (e.status === "done") li.querySelector(".pl-bar > div").style.width = "100%";
}

async function generateReels() {
  if (!state.srcPath) return;
  $("genBtn").disabled = true;
  resetPipeline();
  $("pipelineOverlay").classList.remove("hidden");
  setStatus("Generating reels…");

  const off = window.api.onPipelineEvent(applyPipelineEvent);
  try {
    const name = $("nameInput").value.trim();
    const res = await window.api.generateReels(state.srcPath, name);
    state.transcript = res.transcript;
    state.reels = res.reels || [];
    // Derive each reel's subtitle words from the full transcript so trimming works.
    state.reels.forEach(recomputeReelWords);
    initHistory();
    renderReelsPanel();
    setStatus(
      `${state.reels.length} reels ready from ${res.transcript.word_count.toLocaleString()} words. ` +
        `Click a reel to load it.`
    );
    if (state.reels.length) selectReel(state.reels[0].id);
  } catch (err) {
    setStatus("Generate Reels failed: " + err.message);
  } finally {
    off();
    $("pipelineClose").disabled = false;
    $("genBtn").disabled = !state.meta || !state.meta.hasAudio;
  }
}

// ── Reels panel (Phase 3) ──────────────────────────────────────────────────────

function renderReelsPanel() {
  const list = $("reelsList");
  if (!state.reels.length) {
    list.innerHTML = '<div class="empty">No reels yet.<br />Open a project, then Generate Reels.</div>';
    return;
  }
  list.innerHTML = "";
  for (const reel of state.reels) {
    const card = document.createElement("div");
    card.className = "reel-card" + (reel.id === state.selectedReelId ? " selected" : "");
    card.dataset.id = reel.id;
    card.innerHTML =
      `<div class="rc-top"><span class="rc-rank">#${reel.rank}</span>` +
      `<span class="rc-dur">${reel.durationSec.toFixed(1)}s</span></div>` +
      `<div class="rc-title"></div>` +
      `<div class="rc-type"></div>`;
    card.querySelector(".rc-title").textContent = reel.title;
    card.querySelector(".rc-type").textContent =
      reel.contentType + (reel.segments.length > 1 ? ` · ${reel.segments.length} spans` : "");
    card.addEventListener("click", () => selectReel(reel.id));
    list.appendChild(card);
  }
}

function currentReel() {
  return state.reels.find((r) => r.id === state.selectedReelId) || null;
}

function selectReel(id) {
  state.selectedReelId = id;
  const reel = currentReel();
  renderReelsPanel();
  if (!reel) return;
  $("clipName").textContent = `${reel.title}  (${reel.durationSec.toFixed(1)}s)`;
  video.pause();
  video.currentTime = snapFrame(reel.inSec);
  $("sourceInspector").classList.add("hidden");
  $("reelInspector").classList.remove("hidden");
  openReelEditor(reel);
  drawReelRegion(reel);
  $("subOverlay").classList.toggle("hidden", !reel.settings.subtitles);
  setStatus(`Reel #${reel.rank}: ${reel.title} — in ${fmtTC(reel.inSec)}, out ${fmtTC(reel.outSec)}`);
}

// ── Reel edit model (non-destructive: just moves in/out numbers) ───────────────

// Pure cut/extend + subtitle helpers live in model.js (testable without a DOM).
const { recomputeReel, visibleWords, isFiller, editedText } = window.ReelModel;

/** Words from the FULL transcript that fall within the reel's current spans.
 *  Recomputed on every trim so extending/cutting adds/drops subtitle words. */
function wordsInSegments(allWords, segments) {
  const out = [];
  for (const w of allWords) {
    const ws = Number(w.start) || 0;
    const we = w.end != null ? Number(w.end) : ws;
    if (segments.some((s) => we > s.startSec && ws < s.endSec)) {
      out.push({ index: w.index, word: w.word, start: ws, end: we });
    }
  }
  out.sort((a, b) => a.start - b.start);
  return out;
}
function recomputeReelWords(reel) {
  const all = (state.transcript && state.transcript.words) || [];
  if (!all.length) return; // keep stored words if transcript unavailable
  reel.words = wordsInSegments(all, reel.segments);
}

function setReelIn(reel, t) {
  window.ReelModel.setReelIn(reel, t);
  recomputeReelWords(reel);
}
function setReelOut(reel, t) {
  window.ReelModel.setReelOut(reel, t, state.meta.durationSec);
  recomputeReelWords(reel);
}

// ── Undo / redo / dirty ────────────────────────────────────────────────────────

let committedState = null;
let dirty = false;

function initHistory() {
  committedState = JSON.stringify(state.reels);
  state.undo = [];
  state.redo = [];
  dirty = false;
  updateHistoryButtons();
}
function commit() {
  if (committedState !== null) state.undo.push(committedState);
  committedState = JSON.stringify(state.reels);
  state.redo = [];
  dirty = true;
  updateHistoryButtons();
}
function undo() {
  if (!state.undo.length) return;
  state.redo.push(JSON.stringify(state.reels));
  const prev = state.undo.pop();
  state.reels = JSON.parse(prev);
  committedState = prev;
  afterHistory();
}
function redo() {
  if (!state.redo.length) return;
  state.undo.push(JSON.stringify(state.reels));
  const next = state.redo.pop();
  state.reels = JSON.parse(next);
  committedState = next;
  afterHistory();
}
function afterHistory() {
  dirty = true;
  renderReelsPanel();
  const r = currentReel();
  if (r) {
    openReelEditor(r);
    drawReelRegion(r);
  }
  updateHistoryButtons();
}
function updateHistoryButtons() {
  $("undoBtn").disabled = !state.undo || !state.undo.length;
  $("redoBtn").disabled = !state.redo || !state.redo.length;
  $("saveBtn").disabled = !state.reels.length;
  $("saveBtn").textContent = dirty ? "Save *" : "Save";
  $("exportBtn").disabled = !state.reels.length;
}

// ── Inspector population ────────────────────────────────────────────────────────

function openReelEditor(reel) {
  recomputeReelWords(reel);
  $("iTitle").textContent = reel.title;
  $("iIO").textContent = `in ${fmtTC(reel.inSec)} / out ${fmtTC(reel.outSec)}`;
  $("iDur").textContent = reel.durationSec.toFixed(1) + "s";

  $("optSubtitles").checked = reel.settings.subtitles;
  $("optFillers").checked = reel.settings.removeFillers;
  $("optSilences").checked = reel.settings.removeSilences;

  $("optZoom").value = reel.settings.reframe.zoom;
  $("optZoomVal").textContent = Number(reel.settings.reframe.zoom).toFixed(2) + "×";

  populateReelCameraSelect(reel);

  const music = reel.settings.music;
  $("musicName").textContent = music ? music.name : "No track";
  $("removeMusic").classList.toggle("hidden", !music);
  $("musicVolRow").style.display = music ? "flex" : "none";
  if (music) {
    $("musicVol").value = music.volume;
    $("musicVolVal").textContent = Math.round(music.volume * 100) + "%";
  }

  buildSubEditor(reel);
  applyReframe();
}

function buildSubEditor(reel) {
  const box = $("subEditor");
  box.innerHTML = "";
  if (!reel.words.length) {
    box.innerHTML = '<span class="dim" style="font-size:12px">No words in this range.</span>';
    return;
  }
  reel.words.forEach((w) => {
    const key = w.index; // stable global word index — survives re-trimming
    const chip = document.createElement("span");
    chip.className = "sub-word";
    chip.dataset.i = key;
    if (isFiller(w.word)) chip.classList.add("filler");
    const edit = reel.settings.subtitleEdits[key];
    if (edit != null) chip.classList.add("edited");
    const text = editedText(reel, key, w.word);
    const removed = String(text).trim() === "";
    if (removed) chip.classList.add("removed");
    // Keep blanked/removed words visible & clickable via a marker.
    chip.textContent = removed ? "·····" : text;
    chip.title = removed
      ? `${fmtTC(w.start)} — removed (click to restore/edit)`
      : `${fmtTC(w.start)} — click to edit`;
    chip.addEventListener("click", () => editWordChip(reel, chip, key, w.word));
    box.appendChild(chip);
  });
}

function editWordChip(reel, chip, i, original) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = editedText(reel, i, original);
  input.className = "name-input";
  input.style.width = Math.max(40, input.value.length * 9) + "px";
  chip.replaceWith(input);
  input.focus();
  input.select();
  const commitEdit = () => {
    // Do NOT trim: empty / whitespace is a valid edit meaning "remove this word"
    // from the subtitles. Only revert to original when the text is unchanged.
    const val = input.value;
    if (val === original) delete reel.settings.subtitleEdits[i];
    else reel.settings.subtitleEdits[i] = val;
    commit();
    buildSubEditor(reel);
  };
  input.addEventListener("blur", commitEdit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") input.blur();
    if (e.key === "Escape") buildSubEditor(reel);
  });
}

// ── 9:16 reframe ────────────────────────────────────────────────────────────────

function applyReframe() {
  const reel = currentReel();
  const frame = $("frame");
  if (editor.previewMode === "916") {
    frame.classList.remove("frame-fit");
    frame.classList.add("frame-916");
    if (reel) {
      const r = reel.settings.reframe;
      video.style.objectPosition = `${r.cropX * 100}% ${r.cropY * 100}%`;
      video.style.transform = `scale(${r.zoom})`;
    }
  } else {
    frame.classList.add("frame-fit");
    frame.classList.remove("frame-916");
    video.style.objectPosition = "";
    video.style.transform = "";
  }
}

function togglePreviewMode() {
  editor.previewMode = editor.previewMode === "916" ? "fit" : "916";
  $("previewMode").classList.toggle("active", editor.previewMode === "916");
  applyReframe();
}

// ── Reel region + trim handles on the master timeline ──────────────────────────

function drawReelRegion(reel) {
  const pps = state.pxPerSec * state.zoom;
  document.querySelectorAll(".reel-region").forEach((n) => n.remove());
  const track = $("track");
  const segs = reel.segments;
  segs.forEach((seg, idx) => {
    const region = document.createElement("div");
    region.className = "reel-region editable";
    region.style.left = seg.startSec * pps + "px";
    region.style.width = Math.max(4, (seg.endSec - seg.startSec) * pps) + "px";
    if (idx === 0) region.appendChild(makeHandle("in", reel, region));
    if (idx === segs.length - 1) region.appendChild(makeHandle("out", reel, region));
    track.appendChild(region);
  });
}

function makeHandle(side, reel, regionEl) {
  const h = document.createElement("div");
  h.className = "reel-handle " + side;
  h.title = side === "in" ? "Drag to trim/extend the start" : "Drag to trim/extend the end";
  h.addEventListener("mousedown", (e) => {
    e.stopPropagation();
    e.preventDefault();
    document.body.style.cursor = "ew-resize";

    const move = (ev) => {
      const pps = state.pxPerSec * state.zoom;
      const rect = $("track").getBoundingClientRect();
      const t = (ev.clientX - rect.left + scroller.scrollLeft) / pps;
      if (side === "in") setReelIn(reel, t);
      else setReelOut(reel, t);
      // Update this region's geometry IN PLACE — never recreate the element being
      // dragged, or the drag breaks. Full redraw happens once on release.
      const seg = side === "in" ? reel.segments[0] : reel.segments[reel.segments.length - 1];
      regionEl.style.left = seg.startSec * pps + "px";
      regionEl.style.width = Math.max(4, (seg.endSec - seg.startSec) * pps) + "px";
      $("iIO").textContent = `in ${fmtTC(reel.inSec)} / out ${fmtTC(reel.outSec)}`;
      $("iDur").textContent = reel.durationSec.toFixed(1) + "s";
      video.currentTime = snapFrame(side === "in" ? reel.inSec : reel.outSec);
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
      commit();
      drawReelRegion(reel);
      buildSubEditor(reel); // refresh the editable word list for the new range
      renderReelsPanel();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  });
  return h;
}

// ── Subtitle overlay (karaoke word-highlight) during playback ──────────────────

function updateSubtitles() {
  const reel = currentReel();
  const ov = $("subOverlay");
  if (!reel || !reel.settings.subtitles) {
    ov.classList.add("hidden");
    return;
  }
  ov.classList.remove("hidden");
  const ct = video.currentTime;
  const vis = visibleWords(reel);
  if (!vis.length) {
    ov.innerHTML = "";
    return;
  }
  let act = vis.findIndex((w) => ct >= w.start && ct < w.end);
  if (act < 0) {
    act = vis.findIndex((w) => w.start > ct);
    if (act < 0) act = vis.length - 1;
  }
  const chunkStart = Math.floor(act / 4) * 4;
  const chunk = vis.slice(chunkStart, chunkStart + 4);
  ov.innerHTML = "";
  for (const w of chunk) {
    const span = document.createElement("span");
    span.className = "w" + (ct >= w.start && ct < w.end ? " active" : "");
    span.textContent = w.text + " ";
    ov.appendChild(span);
  }
  // Mirror the active word in the side editor.
  document.querySelectorAll(".sub-word.playing").forEach((n) => n.classList.remove("playing"));
  if (chunk[0]) {
    const activeWord = vis.find((w) => ct >= w.start && ct < w.end);
    if (activeWord) {
      const chip = document.querySelector(`.sub-word[data-i="${activeWord.i}"]`);
      if (chip) chip.classList.add("playing");
    }
  }
}

/** Keep playback inside the reel's spans (skip gaps; stop at out-point). */
function enforceReelPlayback() {
  const reel = currentReel();
  if (!reel || video.paused) return;
  const ct = video.currentTime;
  const segs = reel.segments;
  const inSeg = segs.some((s) => ct >= s.startSec - 0.03 && ct < s.endSec);
  if (inSeg) return;
  const next = segs.find((s) => s.startSec > ct);
  if (next) video.currentTime = next.startSec;
  else {
    video.pause();
    video.currentTime = Math.max(0, segs[segs.length - 1].endSec - frameDur());
  }
}

// ── Music ────────────────────────────────────────────────────────────────────────

async function addMusic() {
  const m = await window.api.pickAudio();
  if (!m) return;
  const reel = currentReel();
  reel.settings.music = { path: m.path, name: m.name, url: m.url, volume: 0.25 };
  commit();
  openReelEditor(reel);
}
function removeMusic() {
  const reel = currentReel();
  reel.settings.music = null;
  commit();
  openReelEditor(reel);
}

// ── Timeline ─────────────────────────────────────────────────────────────────

let scroller = null;

function ensureScroller() {
  if (scroller) return scroller;
  const ruler = $("ruler");
  const track = $("track");
  scroller = document.createElement("div");
  scroller.className = "tl-scroll";
  scroller.style.overflowX = "auto";
  scroller.style.overflowY = "hidden";
  ruler.parentNode.insertBefore(scroller, ruler);
  scroller.appendChild(ruler);
  scroller.appendChild(track);
  return scroller;
}

function timelineWidth() {
  return Math.max(1, state.meta.durationSec * state.pxPerSec * state.zoom);
}

function buildTimeline() {
  ensureScroller();
  // Base fit-to-width: fit whole duration into the visible scroller width.
  const visible = $("ruler").parentElement.clientWidth || 760;
  state.pxPerSec = visible / Math.max(1, state.meta.durationSec);
}

function layoutTimeline() {
  const w = timelineWidth();
  const ruler = $("ruler");
  const track = $("track");
  ruler.style.width = w + "px";
  track.style.width = w + "px";
  $("zoomLabel").textContent = Math.round(state.zoom * 100) + "%";
  drawRuler();
  updatePlayhead();
  const reel = state.reels.find((r) => r.id === state.selectedReelId);
  if (reel) drawReelRegion(reel);
}

function drawRuler() {
  const ruler = $("ruler");
  ruler.innerHTML = "";
  const dur = state.meta.durationSec;
  const pps = state.pxPerSec * state.zoom;

  // Choose a tick interval that yields ~80px spacing.
  const targetPx = 90;
  const rawStep = targetPx / pps;
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  let step = steps.find((s) => s >= rawStep) || 600;

  for (let t = 0; t <= dur; t += step) {
    const tick = document.createElement("div");
    tick.className = "tick";
    tick.style.left = t * pps + "px";
    const label = document.createElement("span");
    label.textContent = fmtTC(t).slice(3); // drop hours for compactness
    tick.appendChild(label);
    ruler.appendChild(tick);
  }
}

function updatePlayhead() {
  if (!state.meta) return;
  const pps = state.pxPerSec * state.zoom;
  $("playhead").style.left = (video.currentTime || 0) * pps + "px";
  $("tc").textContent = fmtTC(video.currentTime || 0);
}

function seekFromClientX(clientX) {
  const track = $("track");
  const rect = track.getBoundingClientRect();
  const x = clientX - rect.left + scroller.scrollLeft;
  const pps = state.pxPerSec * state.zoom;
  const t = Math.max(0, Math.min(state.meta.durationSec, x / pps));
  video.currentTime = snapFrame(t);
}

// ── Transport ─────────────────────────────────────────────────────────────────

function togglePlay() {
  if (video.paused) video.play();
  else video.pause();
}

function stepFrame(dir) {
  video.pause();
  const t = snapFrame(video.currentTime) + dir * frameDur();
  video.currentTime = Math.max(0, Math.min(state.meta.durationSec, t));
}

// ── Playhead sync loop (frame-accurate via rVFC when available) ────────────────

function syncTick() {
  enforceReelPlayback();
  updatePlayhead();
  updateSubtitles();
}

function startSyncLoop() {
  if (video.requestVideoFrameCallback) {
    const cb = () => {
      syncTick();
      video.requestVideoFrameCallback(cb);
    };
    video.requestVideoFrameCallback(cb);
  } else {
    const raf = () => {
      syncTick();
      requestAnimationFrame(raf);
    };
    requestAnimationFrame(raf);
  }
}

// ── Export (Phase 5) ───────────────────────────────────────────────────────────

const exportState = {
  scope: "selected",
  resolution: "1080p",
  fps: "30",
  quality: "Recommended",
  format: "mp4",
  losslessAudio: false,
  outDir: null,
};

function openExportDialog() {
  if (!state.reels.length) return;
  $("exportProgress").classList.add("hidden");
  $("exportForm").classList.remove("hidden");
  $("exportOverlay").classList.remove("hidden");
  updateExportStartEnabled();
}

function updateExportStartEnabled() {
  $("exportStart").disabled = !exportState.outDir;
}

function wireSeg(containerId, key) {
  const c = $(containerId);
  c.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-v]");
    if (!btn) return;
    c.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    exportState[key] = btn.dataset.v;
  });
}

async function runExport() {
  const reels =
    exportState.scope === "all"
      ? state.reels
      : state.reels.filter((r) => r.id === state.selectedReelId);
  if (!reels.length) {
    setStatus("Nothing to export.");
    return;
  }

  $("exportForm").classList.add("hidden");
  $("exportProgress").classList.remove("hidden");
  $("exportStart").disabled = true;
  $("exportCancel").disabled = true;
  $("expBar").style.width = "0%";
  $("expPct").textContent = "—";
  $("expProgMsg").textContent = "Preparing…";

  const total = reels.length;
  const progressEl = $("expBar").parentElement;
  // Rendering gives no sub-reel %, so show an animated bar + elapsed timer per
  // reel — a single long reel no longer looks frozen at 0%.
  let current = { index: 1, title: reels[0].title, startedAt: Date.now() };
  progressEl.classList.add("indeterminate");
  const timer = setInterval(() => {
    const secs = Math.round((Date.now() - current.startedAt) / 1000);
    $("expProgMsg").textContent = `Exporting ${current.index}/${total}: ${current.title} — ${secs}s`;
    $("expPct").textContent = `${current.index - 1}/${total} done`;
  }, 1000);

  const off = window.api.onExportEvent((e) => {
    if (e.status === "exporting") {
      current = { index: e.index, title: e.message, startedAt: Date.now() };
      $("expProgMsg").textContent = `Exporting ${e.index}/${total}: ${e.message} — 0s`;
      $("expPct").textContent = `${e.index - 1}/${total} done`;
    } else if (e.status === "reel-done") {
      $("expPct").textContent = `${e.index}/${total} done`;
    } else if (e.status === "error") {
      $("expProgMsg").textContent = "Error: " + e.message;
    }
  });

  try {
    const res = await window.api.exportReels({
      srcPath: state.srcPath,
      outDir: exportState.outDir,
      reels,
      dialog: {
        resolution: exportState.resolution,
        fps: exportState.fps === "source" ? "source" : Number(exportState.fps),
        quality: exportState.quality,
        format: exportState.format,
        losslessAudio: exportState.losslessAudio,
      },
      cameras: state.cameras,
    });
    progressEl.classList.remove("indeterminate");
    $("expBar").style.width = "100%";
    $("expPct").textContent = "100%";
    $("expProgMsg").textContent = `Done — ${res.outputs.length} file(s) in ${exportState.outDir}`;
    setStatus(`Exported ${res.outputs.length} reel(s) to ${exportState.outDir}`);
  } catch (err) {
    progressEl.classList.remove("indeterminate");
    $("expBar").style.width = "0%";
    $("expProgMsg").textContent = "Export failed: " + err.message;
    setStatus("Export failed: " + err.message);
  } finally {
    clearInterval(timer);
    off();
    $("exportCancel").disabled = false;
  }
}

async function saveProject() {
  if (!state.reels.length) return;
  const project = {
    // v2 adds referenceAudioPath/cameras — both optional, so a v1 project
    // (no cameras) round-trips identically. (Project *loading* isn't wired to
    // any UI action yet in this app — window.api.loadProject exists but
    // nothing calls it — so there's no restore path to update here either.)
    version: 2,
    srcPath: state.srcPath,
    master: state.meta,
    reels: state.reels,
    referenceAudioPath: state.referenceAudioPath,
    cameras: state.cameras,
  };
  try {
    const p = await window.api.saveProject(project);
    if (p) {
      dirty = false;
      updateHistoryButtons();
      setStatus("Project saved: " + p);
    }
  } catch (e) {
    setStatus("Save failed: " + e.message);
  }
}

// ── Multi-camera sync ───────────────────────────────────────────────────────

/** Next unused single-letter camera id (A, B, C, ...). */
function nextCameraId() {
  const used = new Set(state.cameras.map((c) => c.id));
  for (let code = 65; code < 91; code += 1) {
    const id = String.fromCharCode(code);
    if (!used.has(id)) return id;
  }
  return `CAM${state.cameras.length + 1}`;
}

function updateCamSyncEnabled() {
  $("camSyncStart").disabled = !state.referenceAudioPath || !state.cameras.length;
}

function renderCamList() {
  const box = $("camList");
  box.innerHTML = "";
  if (!state.cameras.length) {
    box.innerHTML = '<span class="dim" style="font-size:12px">No cameras added yet.</span>';
  }
  state.cameras.forEach((cam) => {
    const row = document.createElement("div");
    row.className = "cam-row";

    const label = document.createElement("span");
    label.className = "cam-label";
    label.textContent = `Cam ${cam.id}`;

    const p = document.createElement("span");
    p.className = "cam-path";
    p.textContent = cam.path;
    p.title = cam.path;

    const offsetWrap = document.createElement("span");
    offsetWrap.className = "cam-offset";
    const offsetLabel = document.createElement("span");
    offsetLabel.textContent = "offset";
    const offsetInput = document.createElement("input");
    offsetInput.type = "number";
    offsetInput.step = "0.001";
    offsetInput.value = cam.offsetSec != null ? cam.offsetSec : 0;
    offsetInput.title = "Manual offset override (seconds) — camera_time = reference_time + offset";
    offsetInput.addEventListener("change", () => {
      cam.offsetSec = Number(offsetInput.value) || 0;
      dirty = true;
      updateHistoryButtons();
    });
    offsetWrap.appendChild(offsetLabel);
    offsetWrap.appendChild(offsetInput);
    const secLabel = document.createElement("span");
    secLabel.textContent = "s";
    offsetWrap.appendChild(secLabel);

    const confidence = document.createElement("span");
    if (cam.confidence != null) {
      confidence.className = "cam-confidence " + (cam.confidence >= 10 ? "high" : "low");
      confidence.textContent =
        cam.confidence >= 10 ? `synced (conf ${cam.confidence.toFixed(1)})` : `low confidence (${cam.confidence.toFixed(1)}) — check manually`;
    } else {
      confidence.className = "cam-confidence low";
      confidence.textContent = "not synced";
    }

    const removeBtn = document.createElement("button");
    removeBtn.className = "cam-remove";
    removeBtn.title = "Remove camera";
    removeBtn.textContent = "✕";
    removeBtn.addEventListener("click", () => {
      state.cameras = state.cameras.filter((c) => c.id !== cam.id);
      renderCamList();
      updateCamSyncEnabled();
      if (state.selectedReelId != null) {
        const reel = state.reels.find((r) => r.id === state.selectedReelId);
        if (reel) populateReelCameraSelect(reel);
      }
      dirty = true;
      updateHistoryButtons();
    });

    row.appendChild(label);
    row.appendChild(p);
    row.appendChild(offsetWrap);
    row.appendChild(confidence);
    row.appendChild(removeBtn);
    box.appendChild(row);
  });
  updateCamSyncEnabled();
}

/** Rebuild the per-reel "Camera" picker options from state.cameras. */
function populateReelCameraSelect(reel) {
  const group = $("reelCameraGroup");
  const select = $("reelCamera");
  if (!state.cameras.length) {
    group.classList.add("hidden");
    return;
  }
  group.classList.remove("hidden");
  select.innerHTML = "";
  const defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = "Primary (default)";
  select.appendChild(defaultOpt);
  state.cameras.forEach((cam) => {
    const opt = document.createElement("option");
    opt.value = cam.id;
    opt.textContent = `Cam ${cam.id}`;
    select.appendChild(opt);
  });
  select.value = reel.settings.camera || "";
  select.onchange = () => {
    reel.settings.camera = select.value || null;
    commit();
  };
}

function openCamerasDialog() {
  $("camReferencePath").textContent = state.referenceAudioPath || "—";
  renderCamList();
  $("camSyncProgress").classList.add("hidden");
  $("camerasOverlay").classList.remove("hidden");
}

async function pickReferenceAudio() {
  const p = await window.api.pickReferenceAudio();
  if (!p) return;
  state.referenceAudioPath = p;
  $("camReferencePath").textContent = p;
  updateCamSyncEnabled();
  dirty = true;
  updateHistoryButtons();
}

async function addCamera() {
  const p = await window.api.addCameraDialog();
  if (!p) return;
  state.cameras.push({ id: nextCameraId(), path: p, offsetSec: 0, confidence: null });
  renderCamList();
  if (state.selectedReelId != null) {
    const reel = state.reels.find((r) => r.id === state.selectedReelId);
    if (reel) populateReelCameraSelect(reel);
  }
  dirty = true;
  updateHistoryButtons();
}

async function runCameraSync() {
  if (!state.referenceAudioPath || !state.cameras.length) return;
  $("camSyncStart").disabled = true;
  $("camSyncProgress").classList.remove("hidden");
  $("camSyncMsg").textContent = "Syncing…";
  $("camSyncBar").style.width = "10%";

  const off = window.api.onSyncEvent((e) => {
    if (e.status === "syncing") {
      $("camSyncMsg").textContent = `Syncing camera ${e.cameraId}…`;
    } else if (e.status === "camera-done") {
      const cam = state.cameras.find((c) => c.id === e.cameraId);
      if (cam) {
        cam.offsetSec = e.offsetSec;
        cam.confidence = e.confidence;
      }
      renderCamList();
      $("camSyncMsg").textContent = `Camera ${e.cameraId}: offset ${e.offsetSec.toFixed(3)}s`;
    } else if (e.status === "error") {
      $("camSyncMsg").textContent = "Error: " + e.message;
    }
  });

  try {
    await window.api.syncCameras({
      referenceAudioPath: state.referenceAudioPath,
      cameras: state.cameras.map((c) => ({ id: c.id, path: c.path })),
    });
    $("camSyncBar").style.width = "100%";
    $("camSyncMsg").textContent = "Sync complete.";
    setStatus("Cameras synced.");
    dirty = true;
    updateHistoryButtons();
  } catch (err) {
    $("camSyncMsg").textContent = "Sync failed: " + err.message;
    setStatus("Camera sync failed: " + err.message);
  } finally {
    off();
    updateCamSyncEnabled();
  }
}

// ── Wiring ─────────────────────────────────────────────────────────────────────

function wire() {
  $("openBtn").addEventListener("click", openProject);
  $("openBtn2").addEventListener("click", openProject);
  $("genBtn").addEventListener("click", generateReels);
  $("pipelineClose").addEventListener("click", () =>
    $("pipelineOverlay").classList.add("hidden")
  );

  // History + save
  $("undoBtn").addEventListener("click", undo);
  $("redoBtn").addEventListener("click", redo);
  $("saveBtn").addEventListener("click", saveProject);

  // 9:16 preview toggle
  $("previewMode").addEventListener("click", togglePreviewMode);

  // Trim buttons (cut/extend in/out)
  document.querySelectorAll(".trim-btns [data-trim]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const reel = currentReel();
      if (!reel) return;
      const d = Number(btn.dataset.d);
      if (btn.dataset.trim === "in") setReelIn(reel, reel.inSec + d);
      else setReelOut(reel, reel.outSec + d);
      commit();
      openReelEditor(reel);
      drawReelRegion(reel);
      renderReelsPanel();
      video.currentTime = snapFrame(btn.dataset.trim === "in" ? reel.inSec : reel.outSec);
    });
  });

  // Locked-option toggles
  const toggleSetting = (id, key, after) => {
    $(id).addEventListener("change", () => {
      const reel = currentReel();
      if (!reel) return;
      reel.settings[key] = $(id).checked;
      commit();
      if (after) after(reel);
    });
  };
  toggleSetting("optSubtitles", "subtitles", (r) =>
    $("subOverlay").classList.toggle("hidden", !r.settings.subtitles)
  );
  toggleSetting("optFillers", "removeFillers", (r) => buildSubEditor(r));
  toggleSetting("optSilences", "removeSilences");

  // Reframe zoom
  $("optZoom").addEventListener("input", () => {
    const reel = currentReel();
    if (!reel) return;
    reel.settings.reframe.zoom = Number($("optZoom").value);
    $("optZoomVal").textContent = reel.settings.reframe.zoom.toFixed(2) + "×";
    applyReframe();
  });
  $("optZoom").addEventListener("change", () => commit());
  $("reframeReset").addEventListener("click", () => {
    const reel = currentReel();
    if (!reel) return;
    reel.settings.reframe = { cropX: 0.5, cropY: 0.5, panX: 0, panY: 0, zoom: 1 };
    commit();
    openReelEditor(reel);
  });

  // Drag inside 9:16 preview to reposition (pan crop focus).
  let panning = false;
  $("frame").addEventListener("mousedown", (e) => {
    if (editor.previewMode !== "916" || !currentReel()) return;
    panning = true;
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!panning) return;
    const reel = currentReel();
    const rect = $("frame").getBoundingClientRect();
    reel.settings.reframe.cropX = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    reel.settings.reframe.cropY = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    applyReframe();
  });
  window.addEventListener("mouseup", () => {
    if (panning) {
      panning = false;
      commit();
    }
  });

  // Cameras dialog
  $("camerasBtn").addEventListener("click", openCamerasDialog);
  $("camerasClose").addEventListener("click", () => $("camerasOverlay").classList.add("hidden"));
  $("camPickReference").addEventListener("click", pickReferenceAudio);
  $("camAdd").addEventListener("click", addCamera);
  $("camSyncStart").addEventListener("click", runCameraSync);

  // Export dialog
  $("exportBtn").addEventListener("click", openExportDialog);
  $("exportCancel").addEventListener("click", () => $("exportOverlay").classList.add("hidden"));
  $("exportStart").addEventListener("click", runExport);
  wireSeg("expScope", "scope");
  wireSeg("expRes", "resolution");
  wireSeg("expFps", "fps");
  wireSeg("expQuality", "quality");
  wireSeg("expFormat", "format");
  $("expLossless").addEventListener("change", (e) => {
    exportState.losslessAudio = e.target.checked;
    // PCM audio isn't broadly compatible inside MP4 — steer the Format toggle to
    // MOV while lossless is on so the UI matches what actually gets exported.
    const formatSeg = $("expFormat");
    formatSeg.querySelectorAll("button").forEach((b) => {
      b.disabled = e.target.checked && b.dataset.v === "mp4";
    });
    if (e.target.checked && exportState.format !== "mov") {
      formatSeg.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      formatSeg.querySelector('button[data-v="mov"]').classList.add("active");
      exportState.format = "mov";
    }
  });
  $("expPickDir").addEventListener("click", async () => {
    const dir = await window.api.pickExportDir();
    if (dir) {
      exportState.outDir = dir;
      $("expDirPath").textContent = dir;
      updateExportStartEnabled();
    }
  });

  // Music
  $("addMusic").addEventListener("click", addMusic);
  $("removeMusic").addEventListener("click", removeMusic);
  $("musicVol").addEventListener("input", () => {
    const reel = currentReel();
    if (!reel || !reel.settings.music) return;
    reel.settings.music.volume = Number($("musicVol").value);
    $("musicVolVal").textContent = Math.round(reel.settings.music.volume * 100) + "%";
  });
  $("musicVol").addEventListener("change", () => commit());

  $("playPause").addEventListener("click", togglePlay);
  $("stepBack").addEventListener("click", () => stepFrame(-1));
  $("stepFwd").addEventListener("click", () => stepFrame(1));
  $("toStart").addEventListener("click", () => {
    video.pause();
    video.currentTime = 0;
  });
  $("toEnd").addEventListener("click", () => {
    video.pause();
    video.currentTime = Math.max(0, state.meta.durationSec - frameDur());
  });

  video.addEventListener("play", () => {
    $("playPause").textContent = "❚❚";
    // If a reel is selected and the playhead is outside its span, start at the in-point.
    const reel = currentReel();
    if (reel) {
      const ct = video.currentTime;
      const inside = reel.segments.some((s) => ct >= s.startSec - 0.03 && ct < s.endSec);
      if (!inside) video.currentTime = snapFrame(reel.inSec);
    }
  });
  video.addEventListener("pause", () => ($("playPause").textContent = "▶"));
  video.addEventListener("timeupdate", updatePlayhead);
  video.addEventListener("seeked", updatePlayhead);
  video.addEventListener("loadedmetadata", updatePlayhead);

  // Scrub by clicking / dragging the track.
  let dragging = false;
  const track = $("track");
  track.addEventListener("mousedown", (e) => {
    dragging = true;
    video.pause();
    seekFromClientX(e.clientX);
  });
  window.addEventListener("mousemove", (e) => {
    if (dragging) seekFromClientX(e.clientX);
  });
  window.addEventListener("mouseup", () => (dragging = false));

  // Zoom
  $("zoomIn").addEventListener("click", () => {
    state.zoom = Math.min(40, state.zoom * 1.5);
    layoutTimeline();
  });
  $("zoomOut").addEventListener("click", () => {
    state.zoom = Math.max(1, state.zoom / 1.5);
    layoutTimeline();
  });

  // Keyboard: Space = play/pause, arrows = frame step, Ctrl+Z/Y = undo/redo, Ctrl+S = save.
  window.addEventListener("keydown", (e) => {
    if (e.target && (e.target.tagName === "INPUT" || e.target.isContentEditable)) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      e.shiftKey ? redo() : undo();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
      e.preventDefault();
      redo();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveProject();
      return;
    }
    if (!state.meta) return;
    if (e.code === "Space") {
      e.preventDefault();
      togglePlay();
    } else if (e.code === "ArrowLeft") {
      e.preventDefault();
      stepFrame(-1);
    } else if (e.code === "ArrowRight") {
      e.preventDefault();
      stepFrame(1);
    }
  });

  // Re-fit timeline on window resize (keep zoom factor).
  window.addEventListener("resize", () => {
    if (state.meta) {
      buildTimeline();
      layoutTimeline();
    }
  });

  startSyncLoop();
}

wire();
