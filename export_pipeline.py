"""Export analyzed reels to 9:16 karaoke MP4s via FFmpeg (Node media engine)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from src.caption_builder import (
    DEFAULT_EXPORT_CANVAS,
    build_captions_for_reel,
    build_playback_words,
)
from src.marketing_doc import render_marketing_doc_docx
from paths import OUTPUT_ROOT, TOOL_ROOT

ROOT = TOOL_ROOT
CLI = ROOT / "export" / "export_reel_cli.cjs"


def sanitize_segments(rows: list) -> list[dict]:
    out: list[dict] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        start = float(row.get("start_time_seconds") or 0)
        end = float(row.get("end_time_seconds") or start)
        if end <= start:
            end = start + 0.12
        role = str(row.get("role") or row.get("label") or "BODY").upper()
        if role not in {"HOOK", "BODY", "PAYOFF"}:
            role = "BODY"
        label = str(row.get("label") or role)
        out.append(
            {
                "order": idx + 1,
                "role": role,
                "label": label[:60],
                "start_time_seconds": round(max(0.0, start), 3),
                "end_time_seconds": round(max(0.0, end), 3),
                "note": str(row.get("note") or row.get("description") or "")[:500],
            }
        )
    return out


def export_reel_mp4(reel: dict, source: Path, out_path: Path) -> None:
    segments = sanitize_segments(reel.get("editor_cut_sheet") or [])
    canvas = dict(DEFAULT_EXPORT_CANVAS)
    captions = build_captions_for_reel(reel, segments)
    words = build_playback_words(reel, segments)
    is_v2 = str(os.getenv("REEL_PROFILE", "")).strip().lower() in ("v2", "2", "updated_v2")
    payload = {
        "segments": segments,
        "captions": captions,
        "words": words,
        "playbackWords": words,
        "canvas": canvas,
        "captionStyle": "karaoke",
        "captionSize": int(canvas.get("captionSize") or 135),
        "hideFillersInSubtitles": bool(is_v2),
        "cutFillersFromVideo": False,
        "cutSilences": False,
        "quality": "high",
        "bitrate": "22M",
        "fps": "source",
        "resolution": {"width": 1080, "height": 1920},
    }
    if is_v2:
        payload["captionChunkSize"] = int(os.getenv("REEL_CAPTION_CHUNK", "4") or 4)
        if os.getenv("REEL_TEXT_OVERLAYS", "1") != "0":
            payload["textHook"] = str(reel.get("text_hook") or "").strip()
            payload["speakerName"] = str(os.getenv("REEL_SPEAKER_NAME") or "").strip()
            payload["speakerTitle"] = str(os.getenv("REEL_SPEAKER_TITLE") or "").strip()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(payload, tmp, ensure_ascii=False)
        payload_path = tmp.name
    try:
        proc = subprocess.run(
            ["node", str(CLI), str(source), payload_path, str(out_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=1200,
        )
    finally:
        Path(payload_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "export failed").strip()[:800])
    if not out_path.is_file() or out_path.stat().st_size < 20_000:
        raise RuntimeError(f"Export too small or missing: {out_path}")


QUALITY_BITRATE = {"low": "10M", "medium": "16M", "high": "24M"}


def export_reel_mp4_ex(reel: dict, source: Path, out_path: Path, options: dict) -> None:
    """Export one edited reel honoring the desktop editor's per-reel + dialog options.

    Reuses the same caption builder + Node/FFmpeg engine as export_reel_mp4, but
    every knob (resolution, fps, quality, filler/silence cuts, 9:16 crop, music)
    comes from `options` so the export bakes in exactly what the editor shows.
    """
    options = options or {}
    segments = sanitize_segments(reel.get("editor_cut_sheet") or [])
    captions = build_captions_for_reel(reel, segments)
    words = build_playback_words(reel, segments)

    canvas = dict(DEFAULT_EXPORT_CANVAS)
    canvas.update(options.get("canvas") or {})

    resolution = options.get("resolution") or {"width": 1080, "height": 1920}
    quality = str(options.get("quality") or "high").lower()
    bitrate = options.get("bitrate") or QUALITY_BITRATE.get(quality, "22M")
    fps = options.get("fps") or "source"
    chunk = int(options.get("captionChunkSize") or os.getenv("REEL_CAPTION_CHUNK", "4") or 4)

    payload = {
        "segments": segments,
        "captions": captions,
        "words": words,
        "playbackWords": words,
        "canvas": canvas,
        "captionStyle": "karaoke",
        "captionSize": int(canvas.get("captionSize") or 135),
        "captionChunkSize": chunk,
        "hideFillersInSubtitles": True,
        "cutFillersFromVideo": bool(options.get("cutFillersFromVideo")),
        "cutSilences": bool(options.get("cutSilences")),
        "quality": quality,
        "bitrate": bitrate,
        "fps": fps,
        "resolution": {"width": int(resolution["width"]), "height": int(resolution["height"])},
    }

    if options.get("encodePreset"):
        payload["encodePreset"] = str(options["encodePreset"])

    music = options.get("music")
    if music and music.get("path"):
        payload["musicPath"] = str(music["path"])
        payload["musicVolume"] = float(music.get("volume", 0.25))

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(payload, tmp, ensure_ascii=False)
        payload_path = tmp.name
    try:
        proc = subprocess.run(
            ["node", str(CLI), str(source), payload_path, str(out_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=1800,
        )
    finally:
        Path(payload_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "export failed").strip()[:800])
    if not out_path.is_file() or out_path.stat().st_size < 20_000:
        raise RuntimeError(f"Export too small or missing: {out_path}")


def export_all_reels(
    job_id: str,
    analysis: dict,
    source: Path,
    video_stem: str,
    *,
    bundle_dir: Path | None = None,
) -> list[Path]:
    bundle = bundle_dir or (OUTPUT_ROOT / job_id)
    exported = bundle / "exported"
    exported.mkdir(parents=True, exist_ok=True)
    reels = sorted(analysis.get("reels") or [], key=lambda r: int(r.get("id") or 0))
    if not reels:
        raise RuntimeError("No reels in analysis")

    stem = re.sub(r"[^a-zA-Z0-9]+", "", video_stem.split("_")[1] if "_" in video_stem else video_stem) or "reels"
    outputs: list[Path] = []

    for reel in reels:
        reel_id = int(reel.get("id") or 0)
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(reel.get("title") or f"reel_{reel_id}"))[:48].strip("_")
        out = exported / f"reel_{reel_id:02d}_{safe}_916_karaoke.mp4"
        print(f"[{reel_id}/{len(reels)}] Exporting {out.name} ...", flush=True)
        export_reel_mp4(reel, source, out)
        print(f"  OK ({out.stat().st_size // 1024} KB)", flush=True)
        outputs.append(out)

    doc_title = f"{stem} — Short-Form Marketing Package"
    docx_path = exported / f"{stem}_marketing_package.docx"
    render_marketing_doc_docx(analysis, docx_path, doc_title=doc_title)
    html_path = docx_path.with_suffix(".html")
    print(f"OK marketing doc: {docx_path.name}", flush=True)
    if html_path.is_file():
        print(f"OK marketing html: {html_path.name}", flush=True)

    zip_path = exported / f"{stem}_all_reels_916_karaoke.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for mp4 in outputs:
            archive.write(mp4, arcname=mp4.name)
        archive.write(docx_path, arcname=docx_path.name)
        if html_path.is_file():
            archive.write(html_path, arcname=html_path.name)
    print(f"OK zip: {zip_path.name} ({zip_path.stat().st_size // 1024} KB)", flush=True)
    return outputs
