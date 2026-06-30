"use strict";

/**
 * Cross-platform FFmpeg / FFprobe access for the main process.
 *
 * Binaries are bundled per-platform via the ffmpeg-static / ffprobe-static
 * packages. When the app is packaged the binaries live inside app.asar, which
 * cannot be exec'd directly, so we rewrite the path to the unpacked copy. Paths
 * are never assumed to use a particular separator and are always passed as
 * argv entries (never concatenated into a shell string), so spaces are safe.
 */

const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

function resolveUnpacked(p) {
  if (!p) return p;
  // In a packaged build the module sits in app.asar; the real binary is in
  // app.asar.unpacked (configured via electron-builder asarUnpack later).
  return p.replace(`app.asar${path.sep}`, `app.asar.unpacked${path.sep}`);
}

function ffmpegPath() {
  // ffmpeg-static exports the absolute path to the binary as its module value.
  const raw = require("ffmpeg-static");
  return resolveUnpacked(raw);
}

function ffprobePath() {
  const raw = require("ffprobe-static").path;
  return resolveUnpacked(raw);
}

function assertBinaries() {
  for (const [name, p] of [
    ["ffmpeg", ffmpegPath()],
    ["ffprobe", ffprobePath()],
  ]) {
    if (!p || !fs.existsSync(p)) {
      throw new Error(`${name} binary not found (resolved: ${p || "null"})`);
    }
  }
}

/** Run a binary, capture stdout/stderr, resolve on exit 0. */
function run(bin, args, { onStderr } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, args, { windowsHide: true });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => {
      const s = d.toString();
      stderr += s;
      if (onStderr) onStderr(s);
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${path.basename(bin)} exited ${code}: ${stderr.slice(-800)}`));
    });
  });
}

/** Probe a media file -> { durationSec, width, height, fps, hasAudio, codec }. */
async function probe(filePath) {
  const args = [
    "-v", "error",
    "-print_format", "json",
    "-show_format",
    "-show_streams",
    filePath,
  ];
  const { stdout } = await run(ffprobePath(), args);
  const info = JSON.parse(stdout);
  const streams = info.streams || [];
  const v = streams.find((s) => s.codec_type === "video");
  const a = streams.find((s) => s.codec_type === "audio");

  let fps = 30;
  if (v && v.avg_frame_rate && v.avg_frame_rate !== "0/0") {
    const [n, d] = v.avg_frame_rate.split("/").map(Number);
    if (d) fps = n / d;
  }

  return {
    durationSec: parseFloat((info.format && info.format.duration) || (v && v.duration) || 0) || 0,
    width: v ? Number(v.width) : 0,
    height: v ? Number(v.height) : 0,
    fps: Math.round(fps * 1000) / 1000,
    codec: v ? v.codec_name : "",
    hasAudio: Boolean(a),
  };
}

/**
 * Generate a low-res, keyframe-dense proxy for smooth scrubbing.
 * Preserves source aspect ratio, scales the long edge down to `targetHeight`.
 * Dense GOP (-g) + faststart gives near-frame-accurate seeking in the player.
 * Reports progress (0..1) via onProgress using the source duration.
 */
async function generateProxy(srcPath, outPath, { targetHeight = 540, onProgress } = {}) {
  const meta = await probe(srcPath);
  const totalSec = meta.durationSec || 0;

  // Even dimensions required by yuv420p; scale height, keep AR, round width to /2.
  const vf = `scale=-2:${targetHeight}`;
  const gop = Math.max(6, Math.round(meta.fps / 2)); // ~2 keyframes / sec

  const args = [
    "-y",
    "-i", srcPath,
    "-vf", vf,
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "26",
    "-g", String(gop),
    "-keyint_min", String(gop),
    "-pix_fmt", "yuv420p",
    // Keep audio in the proxy: an editor must HEAR the clip to find cut points.
    // Low-bitrate stereo AAC adds negligible size and stays in sync on scrub.
    "-c:a", "aac",
    "-b:a", "128k",
    "-ac", "2",
    "-movflags", "+faststart",
    outPath,
  ];

  await run(ffmpegPath(), args, {
    onStderr: (s) => {
      if (!onProgress || !totalSec) return;
      const m = s.match(/time=(\d+):(\d+):(\d+\.?\d*)/);
      if (m) {
        const sec = Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
        onProgress(Math.max(0, Math.min(1, sec / totalSec)));
      }
    },
  });

  if (!fs.existsSync(outPath) || fs.statSync(outPath).size < 1024) {
    throw new Error("Proxy generation produced no output");
  }
  return { proxyPath: outPath, meta };
}

/** Parse ffmpeg "time=HH:MM:SS.ss" progress against a known total. */
function makeTimeProgress(totalSec, onProgress) {
  return (s) => {
    if (!onProgress || !totalSec) return;
    const m = s.match(/time=(\d+):(\d+):(\d+\.?\d*)/);
    if (m) {
      const sec = Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
      onProgress(Math.max(0, Math.min(1, sec / totalSec)));
    }
  };
}

/**
 * Extract the audio track from the master to an uncompressed WAV (step 1).
 * Kept lossless here so compression is a distinct, visible pipeline step.
 */
async function extractAudio(srcPath, outWavPath, { onProgress } = {}) {
  const meta = await probe(srcPath);
  if (!meta.hasAudio) throw new Error("Source has no audio track to transcribe");
  const args = [
    "-y",
    "-i", srcPath,
    "-vn",
    "-acodec", "pcm_s16le",
    "-ar", "44100",
    "-ac", "1",
    outWavPath,
  ];
  await run(ffmpegPath(), args, { onStderr: makeTimeProgress(meta.durationSec, onProgress) });
  if (!fs.existsSync(outWavPath)) throw new Error("Audio extraction produced no output");
  return { path: outWavPath, durationSec: meta.durationSec };
}

/**
 * Compress to a small, transcription-friendly file (step 2): mono, 16 kHz MP3.
 * This is the ONLY artifact that leaves the machine.
 */
async function compressAudio(wavPath, outMp3Path, { onProgress } = {}) {
  const meta = await probe(wavPath);
  const args = [
    "-y",
    "-i", wavPath,
    "-ac", "1",
    "-ar", "16000",
    "-b:a", "64k",
    "-codec:a", "libmp3lame",
    outMp3Path,
  ];
  await run(ffmpegPath(), args, { onStderr: makeTimeProgress(meta.durationSec, onProgress) });
  if (!fs.existsSync(outMp3Path) || fs.statSync(outMp3Path).size < 256) {
    throw new Error("Audio compression produced no output");
  }
  return { path: outMp3Path, bytes: fs.statSync(outMp3Path).size };
}

module.exports = {
  ffmpegPath,
  ffprobePath,
  assertBinaries,
  probe,
  generateProxy,
  extractAudio,
  compressAudio,
  run,
};
