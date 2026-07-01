#!/usr/bin/env python3
"""Local camera-sync CLI for the desktop editor.

Reads a JSON spec describing a reference audio recording + a set of camera
video files, computes each camera's fixed time offset against the reference
via audio cross-correlation (src/camera_sync.py), and prints machine-readable
progress lines the app parses:

    PROGRESS <cameraId>
    CAMERA_DONE <cameraId> <offsetSec> <confidence>
    ERROR <message>
    DONE

Spec JSON shape:
{
  "referenceAudioPath": "C:/path/reference.wav",
  "cameras": { "A": "C:/path/cam_a.mp4", "B": "C:/path/cam_b.mp4" },
  "windowSec": 300
}

Output: prints one CAMERA_DONE line per camera as it finishes, then DONE.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.camera_sync import DEFAULT_WINDOW_SEC, sync_cameras  # noqa: E402


def _emit(*parts) -> None:
    print(*parts, flush=True)


def main() -> None:
    if len(sys.argv) < 2:
        _emit("ERROR", "missing spec path")
        raise SystemExit(2)

    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    reference_audio_path = spec.get("referenceAudioPath")
    if not reference_audio_path or not Path(reference_audio_path).is_file():
        _emit("ERROR", f"reference audio not found: {reference_audio_path}")
        raise SystemExit(1)

    cameras = spec.get("cameras") or {}
    if not cameras:
        _emit("ERROR", "no cameras to sync")
        raise SystemExit(1)
    missing = [cam_id for cam_id, path in cameras.items() if not Path(path).is_file()]
    if missing:
        _emit("ERROR", f"camera file(s) not found: {', '.join(missing)}")
        raise SystemExit(1)

    window_sec = int(spec.get("windowSec") or DEFAULT_WINDOW_SEC)

    def on_progress(msg: str) -> None:
        # src.camera_sync logs "Syncing camera X..." right before starting each
        # camera — surface that as a PROGRESS line the app can show live.
        if msg.startswith("Syncing camera "):
            cam_id = msg[len("Syncing camera "):].rstrip(".").strip()
            _emit("PROGRESS", cam_id)

    with tempfile.TemporaryDirectory(prefix="istv-sync-") as work_dir:
        try:
            results = sync_cameras(
                reference_audio_path,
                cameras,
                window_sec=window_sec,
                work_dir=work_dir,
                progress_cb=on_progress,
            )
        except Exception as exc:  # surface a clear failure to the app
            _emit("ERROR", str(exc))
            raise SystemExit(1)

    for cam_id, result in results.items():
        _emit("CAMERA_DONE", cam_id, result["offset_sec"], result["confidence"])

    _emit("DONE")


if __name__ == "__main__":
    main()
