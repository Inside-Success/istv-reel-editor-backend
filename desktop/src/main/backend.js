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

/**
 * Upload an audio file to POST /transcribe as raw bytes, reporting upload
 * progress (0..1). Resolves to the backend's { job_id }.
 */
function uploadAudio(audioPath, { onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(`${BACKEND_URL}/transcribe`);
    const total = fs.statSync(audioPath).size;
    let sent = 0;

    const req = lib(u).request(u, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "Content-Length": total,
        "X-Filename": path.basename(audioPath),
      },
    });

    req.on("response", (res) => {
      let body = "";
      res.on("data", (d) => (body += d));
      res.on("end", () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try {
            resolve(JSON.parse(body));
          } catch (e) {
            reject(new Error("Bad JSON from /transcribe: " + e.message));
          }
        } else {
          reject(new Error(`Upload failed ${res.statusCode}: ${body.slice(0, 300)}`));
        }
      });
    });
    req.on("error", reject);

    const stream = fs.createReadStream(audioPath);
    stream.on("data", (chunk) => {
      sent += chunk.length;
      if (onProgress) onProgress(Math.min(1, sent / total));
    });
    stream.on("error", reject);
    stream.pipe(req);
  });
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

/**
 * Poll GET /jobs/{id} until status is done/error. Calls onStatus each tick.
 * Resolves with the full final status object (has .transcript or .analysis).
 */
async function pollJob(jobId, { onStatus, intervalMs = 2500, timeoutMs = 30 * 60 * 1000 } = {}) {
  const start = Date.now();
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const s = await getJSON(`${BACKEND_URL}/jobs/${jobId}`);
    if (onStatus) onStatus(s);
    if (s.status === "done") return s;
    if (s.status === "error") throw new Error(s.error || "Job failed");
    if (Date.now() - start > timeoutMs) throw new Error("Job timed out");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

/** POST /select then poll → resolves with the analysis (reels + metadata). */
async function selectReels(transcript, name, numReels, { onStatus } = {}) {
  const { job_id } = await postJSON("/select", { transcript, name, num_reels: numReels });
  const final = await pollJob(job_id, { onStatus });
  return final.analysis;
}

module.exports = { health, uploadAudio, pollJob, selectReels, BACKEND_URL };
