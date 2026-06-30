"""Record and resolve external source video paths (avoid copying multi-GB files)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

SOURCE_VIDEO_NAME = "source_video.mp4"
SOURCE_PATH_FILE = "source_video.path"
SOURCE_META_FILE = "source_video.json"


def record_source_path(bundle: Path | str, video_path: Path | str, *, archive: bool = False) -> Path:
    """
    Point a job bundle at a source video on disk.

    By default writes a sidecar only (no copy). Pass archive=True to copy into the bundle.
    """
    bundle = Path(bundle)
    bundle.mkdir(parents=True, exist_ok=True)
    src = Path(video_path).resolve()

    if archive:
        target = bundle / SOURCE_VIDEO_NAME
        if not target.is_file() or target.resolve() != src.resolve():
            if src.resolve() != target.resolve():
                shutil.copy2(src, target)
        src = target.resolve()

    (bundle / SOURCE_PATH_FILE).write_text(str(src), encoding="utf-8")
    (bundle / SOURCE_META_FILE).write_text(
        json.dumps({"path": str(src), "filename": src.name}, indent=2),
        encoding="utf-8",
    )
    return src


def read_recorded_path(bundle: Path | str) -> Path | None:
    bundle = Path(bundle)
    meta = bundle / SOURCE_META_FILE
    if meta.is_file():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            path = Path(str(data.get("path") or ""))
            if path.is_file():
                return path.resolve()
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    sidecar = bundle / SOURCE_PATH_FILE
    if sidecar.is_file():
        try:
            path = Path(sidecar.read_text(encoding="utf-8").strip())
            if path.is_file():
                return path.resolve()
        except OSError:
            pass
    return None
