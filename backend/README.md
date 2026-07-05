# ISTV Reel Editor — Backend

FastAPI service that holds the API keys server-side and reuses the repo's proven
pipeline modules (`src/transcription.py`, `src/transcript_cleanup.py`,
`src/analyzer.py`). The desktop app uploads **only compressed audio**; the full
video never reaches this service.

There are **two implementations of the same API**, for two different hosting
models — pick whichever matches where you're deploying:

| | `backend/app.py` | `backend/app_serverless.py` |
|---|---|---|
| Host | Render / Docker / any persistent process | Vercel (via `api/index.py`) |
| Job execution | Background thread per job | One bounded step per `/jobs/{id}` poll — no threads |
| Job storage | In-memory dict + SQLite (`job_store.py`) | Postgres only (`job_store_pg.py`) — the *only* source of truth, since nothing persists between serverless invocations |
| Crash recovery | Resumes in-flight jobs on restart | N/A — every request is already stateless |

Both expose the same routes and response shapes, so the desktop app doesn't
care which one it's talking to.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | Liveness + which keys (and, on the serverless version, DB) are configured |
| POST | `/transcribe` | Raw audio body (`octet-stream`, `X-Filename` header) → starts a Rev.ai job, returns `{job_id}` |
| POST | `/select` | JSON `{transcript, name, num_reels}` → starts Claude selection, returns `{job_id}` |
| GET  | `/jobs/{id}` | Poll status/progress; on `done` includes `transcript` or `analysis` |

The `/select` flow applies the same `updated_v2_test2` profile the CLI uses, so
reels match the approved behavior (incl. the ±10s completeness tuning).

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

## Deploying (Render) — `backend/app.py`

This version holds state in memory and on local disk (background job threads,
`job_store.py`'s SQLite file) so it needs a host that keeps one process running
continuously. See the root [`Dockerfile`](../Dockerfile) and [`render.yaml`](../render.yaml).

1. Push this repo to GitHub (or GitLab).
2. In Render: **New → Blueprint**, point it at the repo — it reads `render.yaml`
   and provisions the web service plus a 1 GB disk for the job store.
3. Set `REVAI_API_KEY` and `CLAUDE_API_KEY` in the service's **Environment** tab
   (left blank in `render.yaml` on purpose — never commit real keys).
4. Once live, point the desktop app at it by building with
   `ISTV_BACKEND_URL=https://<your-service>.onrender.com`.

`JOB_STORE_DIR` (set to `/var/data` in `render.yaml`) puts `jobs.db` on the
mounted disk so in-flight jobs survive a redeploy — without it, the container's
own filesystem is wiped on every deploy. Local dev doesn't set this and falls
back to `backend/jobs.db`, as before.

Prefer Fly.io or Railway instead? Same Dockerfile works there — Fly needs a
`fly.toml` + volume, Railway can build the Dockerfile directly with no extra
config. Ask and I'll add the corresponding file.

## Deploying (Vercel) — `backend/app_serverless.py`

This version has no background threads and no local disk — every request does
one bounded unit of work and reads/writes its state to Postgres, since nothing
(memory or filesystem) survives between two serverless invocations. Routed via
[`api/index.py`](../api/index.py) + [`vercel.json`](../vercel.json).

**The `/select` flow now takes two polls to finish, not one:** the first
`/jobs/{id}` call after submitting runs the transcript-cleanup step and returns
`status: "selecting"`; the *next* call runs the actual Claude reel-selection
step and returns `status: "done"` with the analysis. This is what lets each
individual request stay short enough for a serverless function's time limit,
instead of one request blocking for the whole job like the Render version does.
`/transcribe` similarly just submits to Rev.ai and returns immediately — each
subsequent poll makes one quick "is it done yet" call to Rev.ai rather than
blocking until it is.

1. **Create a Postgres database** — any provider works (Vercel Postgres via
   Neon, Neon directly, Supabase, Render Postgres). You'll get a connection
   string.
2. Push this repo to GitHub, then in Vercel: **Add New → Project**, import it.
   Vercel should detect `vercel.json` and the Python entrypoint automatically.
3. In the project's **Environment Variables**, set:
   - `DATABASE_URL` — the Postgres connection string from step 1 (`POSTGRES_URL`
     also works — that's the name Vercel's own Postgres integration uses)
   - `REVAI_API_KEY`
   - `CLAUDE_API_KEY`
4. Deploy. Hit `<your-project>.vercel.app/health` — look for
   `"database": true` alongside both key checks.
5. Point the desktop app at it: build with
   `ISTV_BACKEND_URL=https://<your-project>.vercel.app`.

Verified locally end-to-end against a throwaway Postgres container: job
creation, the two-step `/select` state machine (real Claude calls, both the
cleanup and reel-selection steps), 404 handling, and the health check's DB
connectivity probe. `/transcribe`'s live Rev.ai path wasn't exercised the same
way (no sample audio file on hand) — it's a thin extraction of the exact same
`rev_ai` client calls `backend/app.py` already uses in production
(`submit_job_local_file` / `get_job_details` / `get_transcript_json`), just
split into submit-once and check-once instead of submit-and-block.

One behavior difference from the Render version: no live "(Ns) elapsed" ticker
during transcription (the desktop app already treats this as optional and
falls back cleanly), and no crash-recovery — if Postgres has no matching job
mid-flow, that's the same as it never having a background thread to resume in
the first place.
