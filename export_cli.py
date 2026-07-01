#!/usr/bin/env python3
"""Local export CLI for the desktop editor.

Reads a JSON spec describing the master source + edited reels + export options,
renders each reel from the FULL-RES master via the existing Node/FFmpeg engine,
and prints machine-readable progress lines the app parses:

    PROGRESS <index> <total> <status> <message>
    DONE <output_path>
    REEL_DONE <index> <output_path>
    ERROR <message>

Spec JSON shape:
{
  "source": "C:/path/master.mp4",
  "outDir": "C:/path/exports",
  "format": "mp4",
  "options": { "resolution": {"width":1080,"height":1920}, "fps":30, "quality":"high" },
  "reels": [
    { "id":1, "title":"...", "editor_cut_sheet":[...], "timestamped_words":[...],
      "options": { "cutFillersFromVideo":true, "cutSilences":false,
                   "canvas":{"cropX":0.5,"cropY":0.5,"zoom":1.0},
                   "music":{"path":"...","volume":0.25} } }
  ]
}
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from export_pipeline import _resolve_max_workers, export_reel_mp4_ex  # noqa: E402


def _emit(*parts) -> None:
    print(*parts, flush=True)


def _safe_name(reel_id: int, title: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(title or f"reel_{reel_id}"))[:48].strip("_")
    return f"reel_{reel_id:02d}_{safe or 'reel'}_916"


def main() -> None:
    if len(sys.argv) < 2:
        _emit("ERROR", "missing spec path")
        raise SystemExit(2)

    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    source = Path(spec["source"]).expanduser()
    if not source.is_file():
        _emit("ERROR", f"source not found: {source}")
        raise SystemExit(1)

    out_dir = Path(spec["outDir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "mov" if str(spec.get("format") or "mp4").lower() == "mov" else "mp4"
    global_opts = spec.get("options") or {}
    reels = spec.get("reels") or []
    total = len(reels)
    if not total:
        _emit("ERROR", "no reels to export")
        raise SystemExit(1)

    def _export_one(i: int, reel: dict) -> Path:
        opts = {**global_opts, **(reel.get("options") or {})}
        title = reel.get("title") or f"reel_{reel.get('id', i)}"
        out_path = out_dir / f"{_safe_name(int(reel.get('id', i)), title)}.{ext}"
        export_reel_mp4_ex(reel, source, out_path, opts)
        return out_path

    # Reels are independent exports (separate ffmpeg processes), so run several
    # concurrently instead of one-at-a-time. Bounded by core count by default;
    # override with REEL_MAX_WORKERS on bigger production hardware.
    max_workers = _resolve_max_workers(total)
    outputs: list[str | None] = [None] * total
    had_error = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_index = {}
        for i, reel in enumerate(reels, start=1):
            title = reel.get("title") or f"reel_{reel.get('id', i)}"
            _emit("PROGRESS", i, total, "exporting", title)
            future_to_index[pool.submit(_export_one, i, reel)] = i

        for future in concurrent.futures.as_completed(future_to_index):
            i = future_to_index[future]
            try:
                out_path = future.result()
            except Exception as exc:  # surface a clear, per-reel failure
                _emit("ERROR", f"reel {i}: {exc}")
                had_error = True
                continue
            outputs[i - 1] = str(out_path)
            _emit("REEL_DONE", i, out_path)

    if had_error:
        raise SystemExit(1)

    _emit("DONE", out_dir)


if __name__ == "__main__":
    main()
