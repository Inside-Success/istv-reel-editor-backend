#!/usr/bin/env python3
"""Generate short-form reels from a source video (v2_test2 profile).

Drop a video into the input/ folder and run:

    python generate_reels.py

Or pass a video path directly:

    python generate_reels.py --video "path/to/interview.mp4" --name "Speaker Name"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from export_pipeline import export_all_reels
from paths import INPUT_DIR, OUTPUT_ROOT
from src.analyzer import analyze_with_claude
from src.cutter import REEL_END_TOLERANCE_SECONDS, reel_max_seconds
from src.audio_processor import check_ffmpeg, compress_audio, extract_audio
from src.source_video_paths import read_recorded_path, record_source_path
from src.transcript_cleanup import correct_transcript_words
from src.transcription import fmt_time, transcribe_audio
from src.validate import validate_summary

# Default profile: updated v2_test2 (same reel quality as caylene_updated_v2_test2)
DEFAULT_PROFILE = {
    "REEL_PROFILE": "v2",
    "REEL_CONTEXT_AWARE": "1",
    "REEL_MIN_SECONDS": "30",
    "REEL_MAX_SECONDS": "90",
    "REEL_CAPTION_CHUNK": "4",
    "REEL_TEXT_OVERLAYS": "0",
}

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def find_input_video() -> Path:
    videos = sorted(
        [p for p in INPUT_DIR.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not videos:
        raise RuntimeError(
            f"No video found in {INPUT_DIR}\n"
            f"Drop a .mp4/.mov/.mkv file into input/ or pass --video <path>"
        )
    return videos[0]


def _clean(token: str) -> str:
    return re.sub(r"[^a-zA-Z']", "", str(token or ""))


def detect_name_aliases(words: list[dict], speaker_name: str) -> dict[str, str]:
    parts = [p for p in re.split(r"\s+", speaker_name.strip()) if len(p) >= 3]
    if not parts:
        return {}
    first = parts[0]
    surname = parts[-1] if len(parts) > 1 else ""
    toks = [_clean(w.get("word")) for w in words]

    aliases: dict[str, str] = {}
    follow: Counter[str] = Counter()
    for i, t in enumerate(toks[:-1]):
        if not t:
            continue
        ratio_first = SequenceMatcher(None, t.lower(), first.lower()).ratio()
        if ratio_first >= 0.7:
            nxt = toks[i + 1]
            if nxt and nxt[0:1].isupper():
                follow[nxt.lower()] += 1
    if surname:
        for tok, _count in follow.most_common(4):
            aliases[tok] = surname
        for t in toks:
            if t and len(t) >= 4 and t.lower() != surname.lower():
                if SequenceMatcher(None, t.lower(), surname.lower()).ratio() >= 0.7:
                    aliases[t.lower()] = surname
    return aliases


def apply_profile(profile: dict[str, str]) -> None:
    for key, value in profile.items():
        os.environ[key] = value


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Generate 9:16 karaoke reels from a source video (v2_test2 profile)"
    )
    parser.add_argument(
        "--video",
        default="",
        help="Path to source video (default: newest file in input/)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Correct speaker name for subtitle spelling (e.g. 'Caylene Salii')",
    )
    parser.add_argument(
        "--title",
        default="",
        help="Optional speaker role/title (only used if text overlays are enabled)",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Job folder prefix (default: video filename stem)",
    )
    parser.add_argument(
        "--num-reels",
        type=int,
        default=15,
        help="Number of reels to generate (default: 15)",
    )
    parser.add_argument(
        "--model",
        default="claude-opus-4-8",
        help="Claude model for analysis (default: claude-opus-4-8)",
    )
    parser.add_argument(
        "--transcript",
        default="",
        help="Reuse an existing transcript.json (skips Rev.ai transcription)",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Export MP4s from an existing job bundle (skip transcribe/cleanup/analyze)",
    )
    parser.add_argument(
        "--job-dir",
        default="",
        help="Job folder with analysis.json (for --export-only; default: generated_data/<prefix>_updated_v2_test2)",
    )
    args = parser.parse_args()

    video: Path | None = None
    if args.export_only:
        if args.video:
            video = Path(args.video).expanduser()
            if not video.is_file():
                raise RuntimeError(f"Video not found: {video}")
    elif args.video:
        video = Path(args.video).expanduser()
        if not video.is_file():
            raise RuntimeError(f"Video not found: {video}")
    else:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        video = find_input_video()

    check_ffmpeg()
    apply_profile(DEFAULT_PROFILE)

    if args.name:
        os.environ["REEL_SPEAKER_NAME"] = args.name
    if args.title:
        os.environ["REEL_SPEAKER_TITLE"] = args.title

    prefix = args.prefix.strip() or date.today().isoformat()
    job_id = f"{prefix}_updated_v2_test2"
    bundle = Path(args.job_dir).expanduser() if args.job_dir else (OUTPUT_ROOT / job_id)

    if args.export_only:
        if not bundle.is_dir():
            raise RuntimeError(f"Job folder not found: {bundle}")
        analysis_path = bundle / "analysis.json"
        if not analysis_path.is_file():
            raise RuntimeError(f"Missing analysis.json in {bundle}")
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        source = read_recorded_path(bundle) or video
        if not source or not source.is_file():
            raise RuntimeError(f"No source video found for job {bundle}")
        print(f"Export-only: {bundle.name}", flush=True)
        print(f"Profile: updated_v2_test2 (karaoke, no text overlays)", flush=True)
        print(f"Source: {source.name}", flush=True)
        print(f"Output: {bundle / 'exported'}/", flush=True)
        print("\nExporting reels...", flush=True)
        outputs = export_all_reels(
            bundle.name,
            analysis,
            source,
            source.stem,
            bundle_dir=bundle,
        )
        print(f"\nDone — {len(outputs)} reels saved to:\n  {bundle / 'exported'}")
        return

    rev_key = (os.getenv("REVAI_API_KEY") or "").strip()
    claude_key = (os.getenv("CLAUDE_API_KEY") or "").strip()
    if not rev_key or not claude_key:
        raise RuntimeError(
            "REVAI_API_KEY and CLAUDE_API_KEY are required.\n"
            "Copy .env.example to .env and add your API keys."
        )

    print(f"Source: {video.name} ({video.stat().st_size // (1024 * 1024)} MB)", flush=True)
    print(f"Profile: updated_v2_test2 (same as caylene_updated_v2_test2)", flush=True)
    print(f"Output: generated_data/{job_id}/exported/", flush=True)

    if args.transcript:
        tpath = Path(args.transcript).expanduser()
        if not tpath.is_file():
            raise RuntimeError(f"Transcript not found: {tpath}")
        transcript = json.loads(tpath.read_text(encoding="utf-8"))
        print(
            f"Reusing transcript {tpath} ({transcript.get('word_count', 0):,} words, "
            f"{fmt_time(transcript.get('duration', 0))})",
            flush=True,
        )
    else:
        tmpdir = tempfile.mkdtemp(prefix="reels-")
        print("Extracting audio...", flush=True)
        audio = extract_audio(str(video), tmpdir)
        print("Compressing audio...", flush=True)
        compressed = compress_audio(audio, tmpdir)
        print("Transcribing with Rev.ai...", flush=True)
        transcript = transcribe_audio(
            compressed,
            rev_key,
            progress_cb=lambda m: print(f"  {m}", flush=True),
        )
        print(
            f"Transcription complete: {transcript['word_count']:,} words, "
            f"{fmt_time(transcript['duration'])}",
            flush=True,
        )

    if args.name:
        aliases = detect_name_aliases(transcript.get("words") or [], args.name)
        if aliases:
            os.environ["REEL_NAME_ALIASES"] = ",".join(f"{k}={v}" for k, v in aliases.items())
            print(f"Name aliases (STT -> correct): {os.environ['REEL_NAME_ALIASES']}", flush=True)

    print("Cleaning transcript (STT spelling fixes, timing preserved)...", flush=True)
    fixed_words, _n = correct_transcript_words(
        transcript.get("words") or [],
        model=args.model,
        api_key=claude_key,
        progress_cb=lambda m: print(f"  {m}", flush=True),
        speaker_name=args.name,
    )
    active = {**transcript, "words": fixed_words}

    print("Analyzing with Claude (selecting reel moments)...", flush=True)
    analysis = analyze_with_claude(
        dict(active),
        args.model,
        claude_key,
        progress_cb=lambda m: print(f"  {m}", flush=True),
        num_reels=args.num_reels,
        story_mode=False,
    )

    bundle = OUTPUT_ROOT / job_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "transcript.json").write_text(json.dumps(active, indent=2, ensure_ascii=False), encoding="utf-8")
    (bundle / "analysis.json").write_text(
        json.dumps(analysis, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    record_source_path(bundle, video, archive=False)

    # Allow the soft tolerance (story-driven overruns) before flagging on length;
    # dangling-ending / context checks stay strict regardless.
    print(
        validate_summary(analysis, max_len=reel_max_seconds() + REEL_END_TOLERANCE_SECONDS),
        flush=True,
    )

    print("\nExporting reels...", flush=True)
    outputs = export_all_reels(job_id, analysis, video, video.stem)

    print(f"\nDone — {len(outputs)} reels saved to:\n  {bundle / 'exported'}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
