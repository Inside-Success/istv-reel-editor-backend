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

from src.transcription import transcribe_audio  # noqa: E402
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

# ── In-memory job registry ───────────────────────────────────────────────────
# Adequate for a single-instance local/hosted service. Swap for Redis if scaled.
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _set(job_id: str, **fields) -> None:
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)


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

    try:
        _set(job_id, status="transcribing", message="Uploading to Rev.ai…")
        transcript = transcribe_audio(audio_path, key, progress_cb=cb)
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


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _run_selection(job_id: str, transcript: dict, name: str, num_reels: int) -> None:
    """Clean the transcript + run Claude reel selection (v2_test2 profile)."""
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

        _set(job_id, status="selecting", message="Cleaning transcript…")
        fixed_words, _n = correct_transcript_words(
            transcript.get("words") or [],
            model=DEFAULT_CLAUDE_MODEL,
            api_key=key,
            progress_cb=cb,
            speaker_name=name,
        )
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


@app.post("/transcribe")
async def transcribe(request: Request) -> dict:
    """Accept a compressed audio file (raw octet-stream) and start a Rev.ai job.

    The filename is passed via the ``X-Filename`` header so we keep the right
    extension; the body is the raw bytes (no multipart needed → trivial upload
    progress on the client).
    """
    fname = request.headers.get("x-filename", "audio.mp3")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    suffix = os.path.splitext(fname)[1] or ".mp3"
    fd, path = tempfile.mkstemp(prefix="istv-audio-", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(body)

    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[job_id] = {
            "status": "queued",
            "message": "Queued",
            "elapsed": 0,
            "transcript": None,
            "error": None,
            "bytes": len(body),
        }
    threading.Thread(target=_run_transcription, args=(job_id, path), daemon=True).start()
    return {"job_id": job_id, "bytes": len(body)}


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
    with _LOCK:
        _JOBS[job_id] = {
            "status": "queued",
            "message": "Queued",
            "elapsed": 0,
            "analysis": None,
            "error": None,
        }
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
