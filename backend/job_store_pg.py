"""Postgres-backed job registry for serverless hosts (Vercel).

`job_store.py` (SQLite) assumes a process that stays alive between requests —
a serverless function's filesystem is thrown away between invocations, and
there's no in-memory dict shared across them either. This module makes
Postgres the *only* source of truth: every request loads what it needs, does
a bounded amount of work, writes back, and returns. There is no background
thread anywhere in this path — see backend/app.py's serverless handlers.

Reads the connection string from DATABASE_URL, falling back to POSTGRES_URL
(the env var name Vercel's own Postgres/Neon integration sets).
"""
from __future__ import annotations

import os

import psycopg
from psycopg.types.json import Jsonb

_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        data JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""

# Backs chunked audio uploads (see /transcribe/init|chunk|finish in
# app_serverless.py). Vercel serverless functions hard-cap a request body at
# ~4.5 MB, well under a typical compressed-audio file, so the client splits
# the upload into chunks that land here and get concatenated at /finish.
_CREATE_CHUNKS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS upload_chunks (
        upload_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        data BYTEA NOT NULL,
        PRIMARY KEY (upload_id, idx)
    )
"""

_schema_ready = False


def _dsn() -> str:
    dsn = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL (or POSTGRES_URL) is not configured")
    return dsn


def _connect() -> psycopg.Connection:
    global _schema_ready
    conn = psycopg.connect(_dsn())
    if not _schema_ready:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
            cur.execute(_CREATE_CHUNKS_TABLE_SQL)
        conn.commit()
        _schema_ready = True
    return conn


def save(job_id: str, kind: str, data: dict) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (job_id, kind, data, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (job_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
            """,
            (job_id, kind, Jsonb(data)),
        )
        conn.commit()


def load(job_id: str) -> dict | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT kind, data FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    if not row:
        return None
    kind, data = row
    return {"kind": kind, **data}


def load_all() -> list[tuple[str, str, dict]]:
    """Load every persisted job — used by backend/app.py's startup bootstrap to
    reload in-memory state and resume in-flight jobs after a restart (the
    long-running-process model; contrast with `load`, used by the stateless
    per-request serverless handlers, which only ever need one job at a time)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT job_id, kind, data FROM jobs")
        rows = cur.fetchall()
    return [(job_id, kind, data) for job_id, kind, data in rows]


def delete(job_id: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        conn.commit()


def purge_older_than(max_age_seconds: int = 86400) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM jobs WHERE updated_at < now() - (%s || ' seconds')::interval",
            (max_age_seconds,),
        )
        conn.commit()


def save_chunk(upload_id: str, idx: int, data: bytes) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO upload_chunks (upload_id, idx, data)
            VALUES (%s, %s, %s)
            ON CONFLICT (upload_id, idx) DO UPDATE SET data = excluded.data
            """,
            (upload_id, idx, data),
        )
        conn.commit()


def load_chunks(upload_id: str) -> bytes:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM upload_chunks WHERE upload_id = %s ORDER BY idx", (upload_id,))
        rows = cur.fetchall()
    return b"".join(bytes(row[0]) for row in rows)


def delete_chunks(upload_id: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM upload_chunks WHERE upload_id = %s", (upload_id,))
        conn.commit()


def healthcheck() -> bool:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False
