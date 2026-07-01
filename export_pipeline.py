"""Export analyzed reels to 9:16 karaoke MP4s via FFmpeg (Node media engine)."""
from __future__ import annotations

import concurrent.futures
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


def _reel_cut_duration(reel: dict) -> float:
    """Total seconds of source footage this reel actually cuts (not source file length) —
    the single-pass ffmpeg export's runtime scales with this, not with the source's size."""
    total = 0.0
    for row in reel.get("editor_cut_sheet") or []:
        if not isinstance(row, dict):
            continue
        start = float(row.get("start_time_seconds") or 0)
        end = float(row.get("end_time_seconds") or start)
        total += max(0.0, end - start)
    return total


def _export_timeout_seconds(reel: dict) -> int:
    """Scale the ffmpeg subprocess timeout with the reel's own cut duration instead of a
    flat constant, so a legitimate long-form reel isn't killed mid-export while a short
    reel isn't stuck waiting needlessly long to fail on a real hang.

    Tunable via env vars for slower hardware / heavier encode presets:
      REEL_EXPORT_TIMEOUT_MULTIPLIER — seconds of timeout per second of cut duration (default 8)
      REEL_EXPORT_TIMEOUT_MIN        — floor in seconds, covers probe/startup overhead (default 300)
    """
    duration = _reel_cut_duration(reel)
    try:
        multiplier = float(os.getenv("REEL_EXPORT_TIMEOUT_MULTIPLIER", "8"))
    except ValueError:
        multiplier = 8.0
    try:
        minimum = float(os.getenv("REEL_EXPORT_TIMEOUT_MIN", "300"))
    except ValueError:
        minimum = 300.0
    return int(max(minimum, duration * multiplier))


def _resolve_max_workers(job_count: int) -> int:
    """Bound concurrent reel exports. Defaults to min(4, cpu_count) — safe for a laptop —
    but a production box (e.g. 32 cores + NVMe) should raise REEL_MAX_WORKERS to use the
    extra throughput for many-reel batches."""
    override = os.getenv("REEL_MAX_WORKERS")
    if override:
        try:
            val = int(override)
            if val > 0:
                return max(1, min(val, job_count))
        except ValueError:
            pass
    return max(1, min(4, os.cpu_count() or 4, job_count))


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
        seg = {
            "order": idx + 1,
            "role": role,
            "label": label[:60],
            "start_time_seconds": round(max(0.0, start), 3),
            "end_time_seconds": round(max(0.0, end), 3),
            "note": str(row.get("note") or row.get("description") or "")[:500],
        }
        # Multi-camera (optional): which camera this cut pulls footage from.
        # Missing/empty means "use the project's primary source", exactly like
        # every existing single-camera reel — see media.cjs exportReel.
        camera = row.get("camera")
        if camera:
            seg["camera"] = str(camera)
        out.append(seg)
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
            timeout=_export_timeout_seconds(reel),
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
    # "original" is a sentinel meaning "use the source's native resolution" (see
    # media.cjs exportReel) — it isn't a real pixel count, so skip the int() cast.
    is_original_res = str(resolution.get("width")).lower() == "original"
    resolution_payload = (
        {"width": "original", "height": "original"}
        if is_original_res
        else {"width": int(resolution["width"]), "height": int(resolution["height"])}
    )
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
        "cutSilences": bool(options.get("cutSilences")),
        "quality": quality,
        "bitrate": bitrate,
        "fps": fps,
        "resolution": resolution_payload,
        "losslessAudio": bool(options.get("losslessAudio")),
    }

    # Multi-camera (optional): {camera_id: {"path": ..., "offsetSec": ...}}.
    # Segments with a matching `camera` field pull footage from these files
    # instead of `source` — see media.cjs exportReel. Absent for ordinary
    # single-camera reels, which behave exactly as before.
    sources = options.get("sources")
    if sources:
        payload["sources"] = sources

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
            timeout=_export_timeout_seconds(reel),
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

    def _export_one(reel: dict) -> Path:
        reel_id = int(reel.get("id") or 0)
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(reel.get("title") or f"reel_{reel_id}"))[:48].strip("_")
        out = exported / f"reel_{reel_id:02d}_{safe}_916_karaoke.mp4"
        export_reel_mp4(reel, source, out)
        return out

    # Reels are independent exports (separate ffmpeg processes), so run several
    # concurrently instead of one-at-a-time. Bounded by core count by default;
    # override with REEL_MAX_WORKERS on bigger production hardware.
    max_workers = _resolve_max_workers(len(reels))
    outputs: list[Path] = [None] * len(reels)  # type: ignore[list-item]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_index = {pool.submit(_export_one, reel): i for i, reel in enumerate(reels)}
        for future in concurrent.futures.as_completed(future_to_index):
            i = future_to_index[future]
            out = future.result()
            print(f"[{i + 1}/{len(reels)}] OK {out.name} ({out.stat().st_size // 1024} KB)", flush=True)
            outputs[i] = out

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
