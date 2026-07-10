"""Vercel entrypoint — re-exports the serverless-compatible FastAPI app.

Vercel's Python runtime auto-detects an ASGI `app` in api/*.py. The real
implementation lives in backend/app_serverless.py (not backend/app.py, which
is the Render/Docker version with background threads that Vercel can't run).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app_serverless import app  # noqa: E402, F401
