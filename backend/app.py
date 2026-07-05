"""ISTV Reel Editor — hosted backend.

Holds the API keys server-side (the desktop app never sees them) and reuses the
proven pipeline modules from ``src/``:

  POST /transcribe   raw audio body (octet-stream) -> starts a Rev.ai job
  GET  /jobs/{id}    poll transcription status / progress / result
  POST /select       transcript + params -> Claude reel cut instructions (Phase 3)
  GET  /health       liveness + which keys are configured

Only compressed audio ever reaches this service. The full video stays on the
editor's machine.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

# Reuse the existing pipeline package from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from backend import job_store  # noqa: E402
from src.transcription import poll_transcription_job, transcribe_audio  # noqa: E402
from src.transcript_cleanup import correct_transcript_words  # noqa: E402
from src.analyzer import analyze_with_claude, DEFAULT_CLAUDE_MODEL  # noqa: E402
from generate_reels import DEFAULT_PROFILE, apply_profile, detect_name_aliases  # noqa: E402

app = FastAPI(title="ISTV Reel Editor Backend", version="0.1.0")

# The desktop app runs on the same machine in dev; allow any origin for the
# local file:// renderer. Tighten to the deployed origin in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Job registry ─────────────────────────────────────────────────────────────
# In-memory for hot-path reads, but every update is written through to a local
# SQLite file (backend/job_store.py) so a crash or restart doesn't silently
# lose an in-flight job. On startup we reload that state and, where possible,
# resume rather than restart: a transcription reattaches to the still-running
# Rev.ai job (no re-upload needed); a Claude selection has no external job to
# reattach to, so it re-runs from the saved transcript/params instead of
# losing the request. Swap for Redis only once this runs as more than one
# process — SQLite is plenty for a single instance.
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()

# Buffers chunked audio uploads (see /transcribe/init|chunk|finish). Not
# persisted through job_store since a chunk set is only needed transiently
# until /finish assembles it — a restart mid-upload just means the client
# retries, same as any other dropped connection.
_UPLOADS: dict[str, dict[int, bytes]] = {}
_UPLOAD_LOCK = threading.Lock()


def _set(job_id: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job_store.save(job_id, job["kind"], job)


def _create(job_id: str, kind: str, **fields) -> None:
    with _LOCK:
        _JOBS[job_id] = {"kind": kind, **fields}
        job_store.save(job_id, kind, _JOBS[job_id])


def _run_transcription(job_id: str, audio_path: str) -> None:
    key = (os.getenv("REVAI_API_KEY") or "").strip()
    if not key:
        _set(job_id, status="error", error="REVAI_API_KEY is not configured on the server")
        _cleanup(audio_path)
        return

    def cb(msg: str) -> None:
        fields = {"message": msg}
        m = re.search(r"(\d+)s elapsed", msg)
        if m:
            fields["elapsed"] = int(m.group(1))
        _set(job_id, **fields)

    def on_submitted(revai_job_id: str) -> None:
        _set(job_id, revai_job_id=revai_job_id)

    try:
        _set(job_id, status="transcribing", message="Uploading to Rev.ai…")
        transcript = transcribe_audio(audio_path, key, progress_cb=cb, on_submitted=on_submitted)
        _set(
            job_id,
            status="done",
            transcript=transcript,
            message=f"{transcript['word_count']:,} words, {transcript['duration']:.0f}s",
        )
    except Exception as exc:  # surface any failure to the client clearly
        _set(job_id, status="error", error=str(exc))
    finally:
        _cleanup(audio_path)


def _resume_transcription(job_id: str, revai_job_id: str) -> None:
    """Reattach to a Rev.ai job that was already submitted before a backend
    crash/restart — skips re-uploading audio entirely, since Rev.ai keeps
    running the job on its own servers regardless of our process lifetime."""
    key = (os.getenv("REVAI_API_KEY") or "").strip()
    if not key:
        _set(job_id, status="error", error="REVAI_API_KEY is not configured on the server")
        return

    def cb(msg: str) -> None:
        fields = {"message": msg}
        m = re.search(r"(\d+)s elapsed", msg)
        if m:
            fields["elapsed"] = int(m.group(1))
        _set(job_id, **fields)

    try:
        _set(job_id, status="transcribing", message="Reattached to Rev.ai job after restart…")
        transcript = poll_transcription_job(revai_job_id, key, progress_cb=cb)
        _set(
            job_id,
            status="done",
            transcript=transcript,
            message=f"{transcript['word_count']:,} words, {transcript['duration']:.0f}s",
        )
    except Exception as exc:
        _set(job_id, status="error", error=str(exc))


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _run_selection(
    job_id: str,
    transcript: dict,
    name: str,
    num_reels: int,
    *,
    cleaned_words: list | None = None,
) -> None:
    """Clean the transcript + run Claude reel selection (v2_test2 profile).

    `cleaned_words`, if given, is a checkpoint from a prior attempt (persisted
    right after transcript cleanup finishes, before the costlier reel-selection
    call) — skips redoing the Claude cleanup call on a resumed/retried run.
    """
    key = (os.getenv("CLAUDE_API_KEY") or "").strip()
    if not key:
        _set(job_id, status="error", error="CLAUDE_API_KEY is not configured on the server")
        return

    def cb(msg: str) -> None:
        _set(job_id, message=msg)

    try:
        # Match the proven updated_v2_test2 reel behavior used by the CLI tool.
        apply_profile(DEFAULT_PROFILE)
        if name:
            os.environ["REEL_SPEAKER_NAME"] = name
            aliases = detect_name_aliases(transcript.get("words") or [], name)
            if aliases:
                os.environ["REEL_NAME_ALIASES"] = ",".join(f"{k}={v}" for k, v in aliases.items())

        if cleaned_words:
            fixed_words = cleaned_words
            _set(job_id, status="selecting", message="Resumed after restart — transcript already cleaned, selecting reels…")
        else:
            _set(job_id, status="selecting", message="Cleaning transcript…")
            fixed_words, _n = correct_transcript_words(
                transcript.get("words") or [],
                model=DEFAULT_CLAUDE_MODEL,
                api_key=key,
                progress_cb=cb,
                speaker_name=name,
            )
            # Checkpoint: a crash during the reel-selection call below (the costlier,
            # longer LLM call) can then resume straight into it instead of re-running
            # cleanup too — see _bootstrap_jobs.
            _set(job_id, cleaned_words=fixed_words)

        active = {**transcript, "words": fixed_words}

        _set(job_id, status="selecting", message="Selecting reels with Claude…")
        analysis = analyze_with_claude(
            dict(active),
            DEFAULT_CLAUDE_MODEL,
            key,
            progress_cb=cb,
            num_reels=num_reels,
            story_mode=False,
        )
        _set(
            job_id,
            status="done",
            analysis=analysis,
            cleaned_words=None,  # checkpoint no longer needed once the job is done
            message=f"{len(analysis.get('reels') or [])} reels selected",
        )
    except Exception as exc:
        _set(job_id, status="error", error=str(exc))


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "istv-reel-editor-backend",
        "revai_key": bool((os.getenv("REVAI_API_KEY") or "").strip()),
        "claude_key": bool((os.getenv("CLAUDE_API_KEY") or "").strip()),
    }


def _start_transcription(body: bytes, fname: str) -> dict:
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    suffix = os.path.splitext(fname)[1] or ".mp3"
    fd, path = tempfile.mkstemp(prefix="istv-audio-", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(body)

    job_id = uuid.uuid4().hex[:12]
    _create(
        job_id,
        "transcribe",
        status="queued",
        message="Queued",
        elapsed=0,
        transcript=None,
        error=None,
        bytes=len(body),
        revai_job_id=None,
    )
    threading.Thread(target=_run_transcription, args=(job_id, path), daemon=True).start()
    return {"job_id": job_id, "bytes": len(body)}


@app.post("/transcribe")
async def transcribe(request: Request) -> dict:
    """Accept a compressed audio file (raw octet-stream) and start a Rev.ai job.

    The filename is passed via the ``X-Filename`` header so we keep the right
    extension; the body is the raw bytes (no multipart needed → trivial upload
    progress on the client). Same API surface as backend/app_serverless.py's
    chunked endpoints below — the desktop client always uses those instead so
    it doesn't need to know which host it's talking to.
    """
    fname = request.headers.get("x-filename", "audio.mp3")
    body = await request.body()
    return _start_transcription(body, fname)


@app.post("/transcribe/init")
async def transcribe_init() -> dict:
    upload_id = uuid.uuid4().hex[:12]
    with _UPLOAD_LOCK:
        _UPLOADS[upload_id] = {}
    return {"upload_id": upload_id}


@app.post("/transcribe/chunk/{upload_id}")
async def transcribe_chunk(upload_id: str, request: Request) -> dict:
    idx_header = request.headers.get("x-chunk-index")
    if idx_header is None:
        raise HTTPException(status_code=400, detail="Missing X-Chunk-Index header")
    body = await request.body()
    with _UPLOAD_LOCK:
        chunks = _UPLOADS.get(upload_id)
        if chunks is None:
            raise HTTPException(status_code=404, detail="Unknown upload_id (server may have restarted)")
        chunks[int(idx_header)] = body
    return {"ok": True}


@app.post("/transcribe/finish/{upload_id}")
async def transcribe_finish(upload_id: str, request: Request) -> dict:
    payload = await request.json()
    fname = str(payload.get("filename") or "audio.mp3")
    with _UPLOAD_LOCK:
        chunks = _UPLOADS.pop(upload_id, None)
    if chunks is None:
        raise HTTPException(status_code=404, detail="Unknown upload_id (server may have restarted)")
    body = b"".join(chunks[i] for i in sorted(chunks))
    return _start_transcription(body, fname)


@app.post("/select")
async def select(request: Request) -> dict:
    """Receive a word-level transcript + params, return Claude reel cut instructions.

    Body JSON: { "transcript": {...}, "name": "Speaker Name", "num_reels": 10 }
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
    _create(
        job_id,
        "select",
        status="queued",
        message="Queued",
        elapsed=0,
        analysis=None,
        error=None,
        # Saved (not just passed to the thread) so a restart can resume selection
        # instead of losing it — Claude has no resumable server-side job to
        # reattach to, but `cleaned_words` (set mid-run) lets a resume skip the
        # transcript-cleanup call and redo only the reel-selection call.
        transcript=transcript,
        name=name,
        num_reels=num_reels,
        cleaned_words=None,
        # Counts restarts that auto-resumed this job (see _bootstrap_jobs). Caps
        # a poison-pill transcript from re-billing the Claude call forever across
        # repeated crash/restart cycles.
        resume_attempts=0,
    )
    threading.Thread(
        target=_run_selection, args=(job_id, transcript, name, num_reels), daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        out = {
            "status": job["status"],
            "message": job["message"],
            "elapsed": job.get("elapsed", 0),
            "error": job["error"],
        }
        if job["status"] == "done":
            if job.get("transcript") is not None:
                out["transcript"] = job["transcript"]
            if job.get("analysis") is not None:
                out["analysis"] = job["analysis"]
        return out


# ── Startup: reload persisted jobs and resume in-flight ones ────────────────

_IN_FLIGHT = {"queued", "transcribing", "selecting"}
# A "select" job that keeps crashing/failing across restarts re-runs the Claude
# selection call (and, on the first resume, possibly the cleanup call) each time —
# real money. Give up permanently after this many auto-resumes instead of retrying
# a poison-pill transcript forever.
_MAX_SELECT_RESUME_ATTEMPTS = 5


def _bootstrap_jobs() -> None:
    job_store.purge_older_than()
    for job_id, kind, data in job_store.load_all():
        with _LOCK:
            _JOBS[job_id] = data
        if data.get("status") not in _IN_FLIGHT:
            continue  # done/error jobs are just restored so /jobs/{id} can still answer
        if kind == "transcribe":
            revai_job_id = data.get("revai_job_id")
            if revai_job_id:
                threading.Thread(
                    target=_resume_transcription, args=(job_id, revai_job_id), daemon=True
                ).start()
            else:
                # Crashed before Rev.ai accepted the upload — the source audio tempfile
                # is gone too, so there's nothing to reattach to or replay.
                _set(job_id, status="error", error="Server restarted before upload completed; please retry.")
        elif kind == "select":
            transcript = data.get("transcript")
            attempts = int(data.get("resume_attempts") or 0)
            if not transcript:
                _set(job_id, status="error", error="Server restarted before selection input was saved; please retry.")
            elif attempts >= _MAX_SELECT_RESUME_ATTEMPTS:
                _set(
                    job_id,
                    status="error",
                    error=f"Gave up after {attempts} restart(s) kept failing this job; please submit a new request.",
                )
            else:
                _set(job_id, resume_attempts=attempts + 1)
                threading.Thread(
                    target=_run_selection,
                    args=(job_id, transcript, data.get("name") or "", data.get("num_reels") or 10),
                    kwargs={"cleaned_words": data.get("cleaned_words")},
                    daemon=True,
                ).start()


_bootstrap_jobs()
