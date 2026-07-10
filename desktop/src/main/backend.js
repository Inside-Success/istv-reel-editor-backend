"use strict";

/**
 * Client for the hosted backend. Uploads compressed audio (raw octet-stream so
 * we get trivial byte-level upload progress) and polls the transcription job.
 * Uses Node's built-in http/https — no extra deps, cross-platform.
 */

const fs = require("fs");
const path = require("path");
const http = require("http");
const https = require("https");
const { URL } = require("url");
const { BACKEND_URL } = require("./config");

function lib(u) {
  return u.protocol === "https:" ? https : http;
}

function getJSON(urlStr, { timeoutMs = 15000 } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr);
    const req = lib(u).request(
      u,
      { method: "GET", timeout: timeoutMs },
      (res) => {
        let body = "";
        res.on("data", (d) => (body += d));
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(body));
            } catch (e) {
              reject(new Error("Bad JSON from backend: " + e.message));
            }
          } else {
            reject(new Error(`Backend ${res.statusCode}: ${body.slice(0, 300)}`));
          }
        });
      }
    );
    req.on("timeout", () => req.destroy(new Error("Backend request timed out")));
    req.on("error", reject);
    req.end();
  });
}

/** GET /health — used to fail fast with a clear message if the server is down. */
async function health() {
  return getJSON(`${BACKEND_URL}/health`);
}

// Kept well under Vercel serverless functions' ~4.5 MB hard request-body cap
// (hit as "413 FUNCTION_PAYLOAD_TOO_LARGE" for anything bigger sent in one
// shot) — the Render backend has no such limit but accepts the same chunked
// API, so the client always chunks rather than needing to know which host
// it's talking to.
//
// 3 MB still tripped the 413 in practice: Vercel's Python runtime relays
// binary request bodies through an API-Gateway-style bridge that base64s
// them in transit, inflating a 3 MB chunk to ~4 MB before it's measured
// against the cap — almost no margin left once headers are added. Dropped
// to 1.5 MB so the post-encoding size stays well clear of the limit.
const UPLOAD_CHUNK_BYTES = 1.5 * 1024 * 1024;

// postBuffer's 30s default timeout is tuned for small JSON-ish payloads, not
// a 1.5 MB binary chunk — on an upload link slower than ~700 kbps (common on
// home wifi/VPNs), just transmitting one chunk can take 20-30s before the
// server even finishes reading it, tripping the timeout and failing the
// whole multi-chunk upload with no way to recover. Chunks get their own much
// longer budget, plus retries below since a slow link makes an occasional
// timeout or dropped connection expected rather than exceptional.
const CHUNK_UPLOAD_TIMEOUT_MS = 120 * 1000;
const CHUNK_UPLOAD_MAX_ATTEMPTS = 4;

/** Re-POST the same chunk on failure — chunk upload is idempotent (the
 * server keys each chunk by its index and overwrites on repeat), so retrying
 * after a timeout or dropped connection is always safe. */
async function postBufferWithRetry(pathName, buf, opts) {
  for (let attempt = 1; ; attempt++) {
    try {
      return await postBuffer(pathName, buf, opts);
    } catch (err) {
      if (attempt >= CHUNK_UPLOAD_MAX_ATTEMPTS) throw err;
      await new Promise((r) => setTimeout(r, 1000 * attempt));
    }
  }
}

/** POST a raw byte buffer to `${BACKEND_URL}${pathName}`, returning parsed JSON. */
function postBuffer(pathName, buf, { headers = {}, timeoutMs = 30000 } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(`${BACKEND_URL}${pathName}`);
    const req = lib(u).request(
      u,
      {
        method: "POST",
        timeout: timeoutMs,
        headers: { "Content-Type": "application/octet-stream", "Content-Length": buf.length, ...headers },
      },
      (res) => {
        let body = "";
        res.on("data", (d) => (body += d));
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(body));
            } catch (e) {
              reject(new Error(`Bad JSON from ${pathName}: ${e.message}`));
            }
          } else {
            reject(new Error(`Upload failed ${res.statusCode}: ${body.slice(0, 300)}`));
          }
        });
      }
    );
    req.on("timeout", () => req.destroy(new Error(`Request to ${pathName} timed out`)));
    req.on("error", reject);
    req.end(buf);
  });
}

/**
 * Upload an audio file in fixed-size chunks (POST /transcribe/init, then one
 * POST /transcribe/chunk/{id} per piece, then POST /transcribe/finish/{id}),
 * reporting upload progress (0..1). Resolves to the backend's { job_id }.
 */
async function uploadAudio(audioPath, { onProgress } = {}) {
  const filename = path.basename(audioPath);
  const data = fs.readFileSync(audioPath);
  const total = data.length;

  const { upload_id } = await postJSONWithRetry("/transcribe/init", {});

  let sent = 0;
  for (let offset = 0; offset < total; offset += UPLOAD_CHUNK_BYTES) {
    const index = offset / UPLOAD_CHUNK_BYTES;
    const chunk = data.subarray(offset, Math.min(offset + UPLOAD_CHUNK_BYTES, total));
    await postBufferWithRetry(`/transcribe/chunk/${upload_id}`, chunk, {
      headers: { "X-Chunk-Index": String(index) },
      timeoutMs: CHUNK_UPLOAD_TIMEOUT_MS,
    });
    sent += chunk.length;
    if (onProgress) onProgress(Math.min(1, sent / total));
  }

  return postJSONWithRetry(`/transcribe/finish/${upload_id}`, { filename });
}

/** POST JSON to a path, returning the parsed response. */
function postJSON(pathName, obj, { timeoutMs = 20000 } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(`${BACKEND_URL}${pathName}`);
    const data = Buffer.from(JSON.stringify(obj), "utf8");
    const req = lib(u).request(
      u,
      {
        method: "POST",
        timeout: timeoutMs,
        headers: { "Content-Type": "application/json", "Content-Length": data.length },
      },
      (res) => {
        let body = "";
        res.on("data", (d) => (body += d));
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(body));
            } catch (e) {
              reject(new Error("Bad JSON: " + e.message));
            }
          } else {
            reject(new Error(`Backend ${res.statusCode}: ${body.slice(0, 300)}`));
          }
        });
      }
    );
    req.on("timeout", () => req.destroy(new Error("Request timed out")));
    req.on("error", reject);
    req.end(data);
  });
}

// postJSON calls that only *enqueue* work (init an upload, or queue the
// /select job) are cheap, side-effect-light round trips with nothing like the
// long Claude call behind pollJob — so a bare ECONNRESET/timeout here is a
// plain network hiccup, not a sign the server is doing something slow. Unlike
// pollJob (below), postJSON itself has no retry, so one dropped connection on
// this initial call surfaced as a raw, unrecoverable "read ECONNRESET" and
// failed the whole step even though a retry moments later would have worked
// fine. Retrying a few times here costs nothing (no job exists yet, or the
// upload_id chunk store hasn't been touched) and matches the resilience the
// polling loop already has.
const POST_JSON_MAX_ATTEMPTS = 4;

async function postJSONWithRetry(pathName, obj, opts) {
  for (let attempt = 1; ; attempt++) {
    try {
      return await postJSON(pathName, obj, opts);
    } catch (err) {
      if (attempt >= POST_JSON_MAX_ATTEMPTS || !isTransientPollError(err)) throw err;
      await new Promise((r) => setTimeout(r, 1000 * attempt));
    }
  }
}

// On the serverless backend, a single GET /jobs/{id} poll can itself run one
// bounded Claude call synchronously (a transcript-cleanup chunk, the reel
// selection call, or the brand-story call — see backend/app_serverless.py's
// _advance_select) before responding, rather than just reading cached status.
// That call retries internally up to 6 times on transient Claude errors
// (_call_with_retries in src/analyzer.py) with backoff capped at 30s —
// 2+4+8+16+30+30 = 90s of backoff alone, before counting any of the actual
// Claude round-trips, which get slower as the source video (and prompt) grows.
// 90s was tripping on legitimately slow-but-healthy calls, so this now gives
// each poll request much more slack than getJSON's 15s default — otherwise a
// slow-but-healthy step reads as "Backend request timed out" and fails the
// whole selection step for nothing.
//
// Must stay comfortably above backend/app_serverless.py's vercel.json
// maxDuration (800s) — the reel-selection/brand-story Claude call there is
// allowed to run right up to that ceiling. If this client-side timeout were
// shorter, the client would destroy the socket and abort an in-flight,
// still-succeeding request on its own, recreating the exact "connection
// killed mid-call" failure this file's retry logic exists to work around,
// except self-inflicted instead of caused by the platform.
const POLL_REQUEST_TIMEOUT_MS = 15 * 60 * 1000;

// A poll tick can legitimately take minutes (the backend runs a synchronous
// Claude call inside that request), which gives an in-flight TCP connection
// a long window to get reset by a flaky wifi link, VPN, or intermediary
// proxy. That's a transient network blip, not a job failure — the backend
// call it interrupted already has its own retry/backoff (_call_with_retries
// in src/analyzer.py). Without this, one dropped connection anywhere in a
// 30-minute polling loop threw straight out and failed the whole step.
const POLL_TRANSIENT_CODES = new Set([
  "ECONNRESET",
  "ETIMEDOUT",
  "ECONNREFUSED",
  "EPIPE",
  "EAI_AGAIN",
  "ENOTFOUND",
]);
const POLL_MAX_CONSECUTIVE_ERRORS = 8;

function isTransientPollError(err) {
  return POLL_TRANSIENT_CODES.has(err.code) || /timed out/i.test(err.message || "");
}

/**
 * Poll GET /jobs/{id} until status is done/error. Calls onStatus each tick.
 * Resolves with the full final status object (has .transcript or .analysis).
 */
async function pollJob(jobId, { onStatus, intervalMs = 2500, timeoutMs = 30 * 60 * 1000 } = {}) {
  const start = Date.now();
  let consecutiveErrors = 0;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    let s;
    try {
      s = await getJSON(`${BACKEND_URL}/jobs/${jobId}`, { timeoutMs: POLL_REQUEST_TIMEOUT_MS });
      consecutiveErrors = 0;
    } catch (err) {
      consecutiveErrors += 1;
      if (!isTransientPollError(err)) throw err;
      if (consecutiveErrors >= POLL_MAX_CONSECUTIVE_ERRORS) {
        // Surface something a user can act on instead of a bare Node error
        // code — "read ECONNRESET" on its own gives no indication this was a
        // dropped connection during Claude's reel-selection call, nor that
        // retrying (via the app's "Retry" button, which reuses the cached
        // transcript — see SELECT_REELS_ONLY in main.js) is the right move.
        throw new Error(
          `Lost connection to the server ${consecutiveErrors} times in a row while waiting on Claude ` +
            `(${err.code || err.message}). This can happen with long/slow reel-selection runs. Try again — ` +
            `it usually succeeds on retry.`
        );
      }
      if (onStatus) {
        onStatus({ status: "active", message: `Connection hiccup (${err.code || err.message}); retrying...` });
      }
      await new Promise((r) => setTimeout(r, intervalMs));
      continue;
    }
    if (onStatus) onStatus(s);
    if (s.status === "done") return s;
    if (s.status === "error") throw new Error(s.error || "Job failed");
    if (Date.now() - start > timeoutMs) throw new Error("Job timed out");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

/** POST /select then poll → resolves with the analysis (reels + metadata). */
async function selectReels(transcript, name, numReels, { onStatus } = {}) {
  const { job_id } = await postJSONWithRetry("/select", { transcript, name, num_reels: numReels });
  const final = await pollJob(job_id, { onStatus });
  return final.analysis;
}

module.exports = { health, uploadAudio, pollJob, selectReels, BACKEND_URL };
