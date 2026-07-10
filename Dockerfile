# Backend API container — see backend/README.md for local (non-Docker) usage.
# The desktop editor lives in its own repo (istv-reel-editor-desktop); it is not part of this image.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r backend/requirements.txt

COPY paths.py generate_reels.py export_pipeline.py ./
COPY src ./src
COPY backend ./backend

# Render (and most PaaS hosts) inject PORT at runtime; 8722 is the local dev default.
ENV PORT=8722
EXPOSE 8722

CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8722}"]
