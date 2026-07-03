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

## Deploying (Render)

This service holds state in memory and on local disk (background job threads,
`job_store.py`'s SQLite file) so it needs a host that keeps one process running
continuously — not a serverless platform like Vercel. See the root
[`Dockerfile`](../Dockerfile) and [`render.yaml`](../render.yaml).

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
