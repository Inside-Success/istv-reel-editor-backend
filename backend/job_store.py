"""Durable job registry backing `_JOBS` in app.py.

The in-memory dict alone loses every in-flight job (transcription, Claude
selection) if the process crashes or restarts — for a long transcription that
can mean redoing an hour of work. This writes job state through to a small
SQLite file on every update, so `app.py` can reload it on startup and either
reattach to the still-running external job (Rev.ai) or, where no external job
exists to reattach to (Claude selection), re-run it from the saved input
instead of losing the request entirely.

SQLite (not Redis) is deliberate here: it's stdlib, needs no extra service to
run/deploy, and comfortably handles this load (a handful of writes per job per
poll interval). Swap for Redis only once this runs as more than one process.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "jobs.db"

_log = logging.getLogger(__name__)
_lock = threading.Lock()


_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        data TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
"""


def _connect() -> sqlite3.Connection:
    """Open the job DB, self-healing if the file itself is corrupted.

    This whole module exists to survive crashes — it would be self-defeating if
    a corrupted DB file (itself the product of a crash mid-write) instead took
    down the entire backend at import time. Quarantine the bad file and start
    fresh rather than refuse to start.
    """
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
        return conn
    except sqlite3.DatabaseError as exc:
        _log.error("jobs.db is corrupted (%s); quarantining and starting fresh", exc)
        if conn is not None:
            # Must close before rename/unlink — Windows keeps an open file locked,
            # so a still-open handle here would make the rename below fail silently
            # and leave us re-opening the exact same corrupted file.
            conn.close()
        quarantine = DB_PATH.with_name(f"jobs.db.corrupt-{int(time.time())}")
        try:
            DB_PATH.rename(quarantine)
        except OSError:
            # Couldn't even move it aside — fall back to deleting it outright so
            # a fresh DB can still be created at the same path.
            try:
                DB_PATH.unlink()
            except OSError:
                pass
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
        return conn


_conn = _connect()


def save(job_id: str, kind: str, data: dict) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO jobs (job_id, kind, data, updated_at) VALUES (?, ?, ?, strftime('%s','now'))\n"
            "ON CONFLICT(job_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (job_id, kind, json.dumps(data)),
        )
        _conn.commit()


def load_all() -> list[tuple[str, str, dict]]:
    """Load all persisted jobs, skipping any row whose JSON is corrupted rather
    than letting one bad row (e.g. a partial write from the crash this store
    exists to survive) take down the whole backend on startup."""
    with _lock:
        rows = _conn.execute("SELECT job_id, kind, data FROM jobs").fetchall()
    out: list[tuple[str, str, dict]] = []
    bad_ids: list[str] = []
    for job_id, kind, data in rows:
        try:
            out.append((job_id, kind, json.loads(data)))
        except json.JSONDecodeError:
            _log.error("job %s has corrupted data; dropping it", job_id)
            bad_ids.append(job_id)
    for job_id in bad_ids:
        delete(job_id)
    return out


def delete(job_id: str) -> None:
    with _lock:
        _conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        _conn.commit()


_IN_FLIGHT_STATUSES = {"queued", "transcribing", "selecting"}


def purge_older_than(max_age_seconds: int = 86400) -> None:
    """Drop finished/stale job rows so the table doesn't grow unbounded.

    Never deletes a row whose status is still in-flight, no matter how old —
    this used to run before `load_all()` in `_bootstrap_jobs`, so a job whose
    `updated_at` hadn't ticked in 24h+ (backend down over a weekend, a crash
    mid-run) would get silently deleted before it ever got a chance to be
    resumed; the client polling it just saw a 404 with no explanation.
    """
    with _lock:
        rows = _conn.execute(
            "SELECT job_id, data FROM jobs WHERE updated_at < strftime('%s','now') - ?",
            (max_age_seconds,),
        ).fetchall()
        stale_ids: list[str] = []
        for job_id, data in rows:
            try:
                status = json.loads(data).get("status")
            except json.JSONDecodeError:
                stale_ids.append(job_id)  # corrupted row — load_all() would drop it anyway
                continue
            if status not in _IN_FLIGHT_STATUSES:
                stale_ids.append(job_id)
        if stale_ids:
            _conn.executemany("DELETE FROM jobs WHERE job_id = ?", [(j,) for j in stale_ids])
            _conn.commit()
