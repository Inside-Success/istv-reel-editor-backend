"""ISTV Reel Editor — Vercel-compatible backend.

Same API surface as backend/app.py (health, transcribe, select, jobs/{id}) but
built for serverless: no background threads, no in-memory job dict, no local
disk. Every request does a bounded amount of work and persists state to
Postgres (job_store_pg.py) so the *next* request — which may land on a
completely different function instance — can pick up where the last one left
off.

  POST /transcribe   raw audio body -> submits a Rev.ai job, returns immediately
  GET  /jobs/{id}    each call does ONE bounded Claude call at most (check
                     Rev.ai status, correct ONE transcript-cleanup chunk, run
                     reel selection, or run brand-story extraction) instead of
                     blocking for the whole job's duration — a full cleanup
                     pass alone is 15-20+ sequential Claude calls for a long
                     transcript, far more than one request should ever do
  POST /select       transcript + params -> queues the Claude reel-selection job
  GET  /health       liveness + which keys/DB are configured

Use this behind api/index.py on Vercel. backend/app.py (SQLite + background
threads) is the Render/Docker version, which can hold a process open — that
one is unaffected by anything in this file.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from backend import job_store_pg as store  # noqa: E402
from src.transcription import check_transcription_job_once, submit_transcription_job  # noqa: E402
from src.transcript_cleanup import correct_transcript_words_step  # noqa: E402
from src.analyzer import (  # noqa: E402
    DEFAULT_CLAUDE_MODEL,
    _is_retryable_claude_error,
    extract_brand_story,
    finalize_analysis,
    prepare_segments,
    select_reels,
)
from generate_reels import DEFAULT_PROFILE, apply_profile, detect_name_aliases  # noqa: E402
from anthropic import Anthropic  # noqa: E402

app = FastAPI(title="ISTV Reel Editor Backend (serverless)", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "istv-reel-editor-backend-serverless",
        "revai_key": bool((os.getenv("REVAI_API_KEY") or "").strip()),
        "claude_key": bool((os.getenv("CLAUDE_API_KEY") or "").strip()),
        "database": store.healthcheck(),
    }


def _start_transcribe_job(body: bytes, fname: str) -> dict:
    """Submit audio to Rev.ai and return immediately (no blocking wait).

    Unlike backend/app.py, this never spawns a thread to wait out the
    transcription — Rev.ai already runs the job on its own servers, so we
    just record its id and check status on future polls.
    """
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    key = (os.getenv("REVAI_API_KEY") or "").strip()
    if not key:
        raise HTTPException(status_code=500, detail="REVAI_API_KEY is not configured on the server")

    suffix = os.path.splitext(fname)[1] or ".mp3"
    fd, path = tempfile.mkstemp(prefix="istv-audio-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        revai_job_id = submit_transcription_job(path, key)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    job_id = uuid.uuid4().hex[:12]
    store.save(
        job_id,
        "transcribe",
        {
            "status": "transcribing",
            "message": "Transcribing…",
            "error": None,
            "transcript": None,
            "revai_job_id": revai_job_id,
        },
    )
    return {"job_id": job_id, "bytes": len(body)}


@app.post("/transcribe")
async def transcribe(request: Request) -> dict:
    """Submit a small audio file in a single request.

    The filename is passed via the ``X-Filename`` header; the body is the raw
    bytes (no multipart needed). Only safe for files under Vercel's ~4.5 MB
    serverless payload cap — larger files must use /transcribe/init +
    /transcribe/chunk + /transcribe/finish instead.
    """
    fname = request.headers.get("x-filename", "audio.mp3")
    body = await request.body()
    return _start_transcribe_job(body, fname)


@app.post("/transcribe/init")
async def transcribe_init() -> dict:
    """Start a chunked upload, returning an id to tag subsequent chunks with.

    Exists because Vercel serverless functions hard-cap a request body at
    ~4.5 MB — well under a typical compressed-audio file — so the client
    splits the upload into chunks instead of sending it in one request.
    """
    return {"upload_id": uuid.uuid4().hex[:12]}


@app.post("/transcribe/chunk/{upload_id}")
async def transcribe_chunk(upload_id: str, request: Request) -> dict:
    """Store one chunk of a chunked upload (see /transcribe/init)."""
    idx_header = request.headers.get("x-chunk-index")
    if idx_header is None:
        raise HTTPException(status_code=400, detail="Missing X-Chunk-Index header")
    body = await request.body()
    store.save_chunk(upload_id, int(idx_header), body)
    return {"ok": True}


@app.post("/transcribe/finish/{upload_id}")
async def transcribe_finish(upload_id: str, request: Request) -> dict:
    """Concatenate the chunks from /transcribe/chunk and submit to Rev.ai.

    Body JSON: { "filename": "audio.mp3" }
    """
    payload = await request.json()
    fname = str(payload.get("filename") or "audio.mp3")
    body = store.load_chunks(upload_id)
    store.delete_chunks(upload_id)
    return _start_transcribe_job(body, fname)


@app.post("/select")
async def select(request: Request) -> dict:
    """Receive a word-level transcript + params, queue Claude reel selection.

    Body JSON: { "transcript": {...}, "name": "Speaker Name", "num_reels": 10 }
    Does no Claude work itself — the first /jobs/{id} poll runs the cleanup
    step, the next one runs reel selection. See _advance_select.
    """
    payload = await request.json()
    transcript = payload.get("transcript")
    if not isinstance(transcript, dict) or not transcript.get("words"):
        raise HTTPException(status_code=400, detail="Missing transcript.words")
    name = str(payload.get("name") or "").strip()
    try:
        num_reels = int(payload.get("num_reels") or 10)
    except (TypeError, ValueError):
        num_reels = 10

    job_id = uuid.uuid4().hex[:12]
    store.save(
        job_id,
        "select",
        {
            "status": "queued",
            "message": "Queued",
            "error": None,
            "analysis": None,
            "transcript": transcript,
            "name": name,
            "num_reels": num_reels,
            "cleaned_words": None,
            "cleanup_index": 0,
            "selection_raw": None,
            "cleaning_attempts": 0,
            "select_attempts": 0,
            "brand_attempts": 0,
        },
    )
    return {"job_id": job_id}


def _advance_transcribe(job: dict) -> dict:
    key = (os.getenv("REVAI_API_KEY") or "").strip()
    if not key:
        job.update(status="error", error="REVAI_API_KEY is not configured on the server")
        return job
    try:
        result = check_transcription_job_once(job["revai_job_id"], key)
    except Exception as exc:  # surface any failure to the client clearly
        job.update(status="error", error=str(exc))
        return job
    if result["status"] == "done":
        transcript = result["transcript"]
        job.update(
            status="done",
            transcript=transcript,
            message=f"{transcript['word_count']:,} words, {transcript['duration']:.0f}s",
        )
    return job


# A single "selecting_reels" or "brand_story" poll makes one synchronous
# Claude call. Vercel hard-kills a function at its configured maxDuration
# (see vercel.json) by dropping the connection outright — the client sees a
# bare ECONNRESET, not a JSON error, and the job's status never advances past
# this step, so every subsequent poll retries the exact same slow call and
# gets killed the exact same way. Capping the Claude call itself well under
# that ceiling turns a platform-level kill (silent, unrecoverable) into a
# normal APITimeoutError this code can catch and retry across polls instead
# of within one request.
_CLAUDE_CALL_TIMEOUT_S = 240.0
_MAX_STEP_ATTEMPTS = 6


def _advance_select(job: dict) -> dict:
    key = (os.getenv("CLAUDE_API_KEY") or "").strip()
    if not key:
        job.update(status="error", error="CLAUDE_API_KEY is not configured on the server")
        return job

    transcript = job["transcript"]
    name = job.get("name") or ""
    num_reels = job.get("num_reels") or 10

    # Match the proven updated_v2_test2 reel behavior used by the CLI tool.
    apply_profile(DEFAULT_PROFILE)
    if name:
        os.environ["REEL_SPEAKER_NAME"] = name
        aliases = detect_name_aliases(transcript.get("words") or [], name)
        if aliases:
            os.environ["REEL_NAME_ALIASES"] = ",".join(f"{k}={v}" for k, v in aliases.items())

    try:
        if job["status"] in ("queued", "cleaning"):
            # Step 1 — ONE cleanup chunk per poll (not the whole transcript;
            # see correct_transcript_words_step), checkpointing progress in
            # cleaned_words/cleanup_index so the next poll picks up where this
            # one left off. `cleaned_words` starts as None on a fresh job.
            # A capped-timeout client + max_retries=0/raise_on_failure=True
            # (see selecting_reels/brand_story below for why) makes this a
            # single bounded attempt per poll instead of transcript_cleanup's
            # default blocking sleep-based retry loop, which could otherwise
            # run this one poll well past Vercel's maxDuration and get killed.
            words = job.get("cleaned_words")
            if words is None:
                words = transcript.get("words") or []
            start = int(job.get("cleanup_index") or 0)
            client = Anthropic(api_key=key, timeout=_CLAUDE_CALL_TIMEOUT_S)
            try:
                updated, next_start, done = correct_transcript_words_step(
                    words, start, model=DEFAULT_CLAUDE_MODEL, api_key=key, speaker_name=name,
                    client=client, max_retries=0, raise_on_failure=True,
                )
            except Exception as exc:
                attempts = int(job.get("cleaning_attempts") or 0) + 1
                job["cleaning_attempts"] = attempts
                if _is_retryable_claude_error(exc) and attempts < _MAX_STEP_ATTEMPTS:
                    job.update(message=f"Cleaning transcript… (retry {attempts}/{_MAX_STEP_ATTEMPTS} after {exc})")
                    return job
                raise
            job["cleaning_attempts"] = 0
            job["cleaned_words"] = updated
            job["cleanup_index"] = next_start
            if done:
                job.update(status="selecting_reels", message="Selecting reels with Claude…")
            else:
                job.update(status="cleaning", message=f"Cleaning transcript… ({next_start}/{len(updated)} words)")
        elif job["status"] == "selecting_reels":
            # Step 2 — the reel-selection call, using step 1's checkpoint.
            words, segments, segmented_text, _duration = prepare_segments(transcript, job.get("cleaned_words"))
            client = Anthropic(api_key=key, timeout=_CLAUDE_CALL_TIMEOUT_S)
            try:
                selection = select_reels(
                    segments,
                    story_mode=False,
                    num_reels=num_reels,
                    model=DEFAULT_CLAUDE_MODEL,
                    client=client,
                    segmented_text=segmented_text,
                )
            except Exception as exc:
                attempts = int(job.get("select_attempts") or 0) + 1
                job["select_attempts"] = attempts
                if _is_retryable_claude_error(exc) and attempts < _MAX_STEP_ATTEMPTS:
                    job.update(message=f"Selecting reels with Claude… (retry {attempts}/{_MAX_STEP_ATTEMPTS} after {exc})")
                    return job
                raise
            job["selection_raw"] = selection
            job.update(status="brand_story", message="Crafting brand story…")
        elif job["status"] == "brand_story":
            # Step 3 — the brand-story call, then finalize (pure Python, no
            # further Claude calls) using both raw results.
            words, segments, segmented_text, duration_str = prepare_segments(transcript, job.get("cleaned_words"))
            client = Anthropic(api_key=key, timeout=_CLAUDE_CALL_TIMEOUT_S)
            try:
                brand = extract_brand_story(client, DEFAULT_CLAUDE_MODEL, segmented_text, duration_str)
            except Exception as exc:
                attempts = int(job.get("brand_attempts") or 0) + 1
                job["brand_attempts"] = attempts
                if _is_retryable_claude_error(exc) and attempts < _MAX_STEP_ATTEMPTS:
                    job.update(message=f"Crafting brand story… (retry {attempts}/{_MAX_STEP_ATTEMPTS} after {exc})")
                    return job
                raise
            selection = job.get("selection_raw") or {}
            analysis = finalize_analysis(
                selection.get("reels") or [],
                str(selection.get("documentary_summary") or "").strip(),
                selection.get("recommendations") or {},
                brand,
                words,
                segments,
                float(transcript.get("duration") or 0),
                num_reels=num_reels,
                story_mode=False,
            )
            job.update(
                status="done",
                analysis=analysis,
                cleaned_words=None,
                selection_raw=None,
                message=f"{len(analysis.get('reels') or [])} reels selected",
            )
    except Exception as exc:
        job.update(status="error", error=str(exc))
    return job


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = store.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Each poll advances the job by exactly one bounded step (if it isn't
    # already finished/failed) instead of relying on a background thread that
    # can't survive between separate serverless invocations.
    if job["status"] not in ("done", "error"):
        if job["kind"] == "transcribe":
            job = _advance_transcribe(job)
        elif job["kind"] == "select":
            job = _advance_select(job)
        store.save(job_id, job["kind"], {k: v for k, v in job.items() if k != "kind"})

    out = {
        "status": job["status"],
        "message": job.get("message"),
        "error": job.get("error"),
    }
    if job["status"] == "done":
        # A "select" job's `transcript` is just an echo of what the client
        # already sent in its POST /select body (kept around only so
        # _advance_select has it to work with across polls) — the caller
        # never reads it back (see desktop/src/main/backend.js's selectReels,
        # which only uses .analysis). For a long transcript this word-level
        # blob can be hundreds of KB to multiple MB, so including it made the
        # one poll that finally returns "done" dramatically bigger than every
        # preceding ~150-byte status ping — exactly the poll most likely to
        # get cut off by a flaky connection ("fails at the last moment").
        if job["kind"] == "transcribe" and job.get("transcript") is not None:
            out["transcript"] = job["transcript"]
        if job.get("analysis") is not None:
            out["analysis"] = job["analysis"]
    return out
