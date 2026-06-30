# ISTV Reel Editor — Backend

FastAPI service that holds the API keys server-side and reuses the repo's proven
pipeline modules (`src/transcription.py`, `src/transcript_cleanup.py`,
`src/analyzer.py`). The desktop app uploads **only compressed audio**; the full
video never reaches this service.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | Liveness + which keys are configured |
| POST | `/transcribe` | Raw audio body (`octet-stream`, `X-Filename` header) → starts a Rev.ai job, returns `{job_id}` |
| POST | `/select` | JSON `{transcript, name, num_reels}` → starts Claude selection, returns `{job_id}` |
| GET  | `/jobs/{id}` | Poll status/progress; on `done` includes `transcript` or `analysis` |

Jobs run on background threads with an in-memory registry (single-instance;
swap for Redis to scale). The `/select` flow applies the same `updated_v2_test2`
profile the CLI uses, so reels match the approved behavior (incl. the ±10s
completeness tuning).

## Run

```bash
# from the repo root, using the project venv (keys come from the root .env)
.venv/Scripts/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8722
```

Install backend deps once: `pip install -r backend/requirements.txt`.

The desktop app points at `http://127.0.0.1:8722` by default; override with the
`ISTV_BACKEND_URL` environment variable.

## Keys

Reads `REVAI_API_KEY` and `CLAUDE_API_KEY` from the repo root `.env`. In a real
deployment, set these as server environment variables and restrict CORS to the
app's origin.
