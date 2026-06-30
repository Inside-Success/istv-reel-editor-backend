import subprocess
import shutil
import os
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}
MAX_MB = 200


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError(
            "FFmpeg not found.\n"
            "Please install FFmpeg and add it to your system PATH.\n"
            "Download: https://ffmpeg.org/download.html"
        )


def file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def extract_audio(video_path: str, out_dir: str) -> str:
    """Extract mono 128kbps MP3 from any video file."""
    out_path = os.path.join(out_dir, "extracted.mp3")
    result = subprocess.run(
        [
            "ffmpeg", "-i", video_path,
            "-vn",                     # drop video stream
            "-acodec", "libmp3lame",
            "-ar", "44100",
            "-ac", "1",                # mono — sufficient for speech
            "-b:a", "128k",
            out_path, "-y",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Audio extraction failed:\n{result.stderr[-600:]}")
    return out_path


def compress_audio(audio_path: str, out_dir: str) -> str:
    """Re-encode to 64 kbps mono — ~28 MB/hr, well under 200 MB for 35-min docs."""
    out_path = os.path.join(out_dir, "compressed.mp3")
    result = subprocess.run(
        [
            "ffmpeg", "-i", audio_path,
            "-acodec", "libmp3lame",
            "-ar", "16000",
            "-ac", "1",
            "-b:a", "64k",
            out_path, "-y",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Audio compression failed:\n{result.stderr[-600:]}")
    return out_path


def prepare_audio(input_path: str, out_dir: str) -> tuple:
    """
    Prepare audio for Rev.ai:
      - Video input  → extract audio first
      - Audio > 200 MB → compress to speech-quality MP3
    Returns (final_audio_path, list_of_human_readable_steps).
    """
    check_ffmpeg()
    steps: list = []
    ext = Path(input_path).suffix.lower()
    audio_path = input_path

    if ext in VIDEO_EXTS:
        steps.append(f"Extracting audio from {Path(input_path).name}...")
        audio_path = extract_audio(input_path, out_dir)
        steps.append(f"Audio extracted — {file_size_mb(audio_path):.1f} MB")
    elif ext in AUDIO_EXTS:
        dest = os.path.join(out_dir, "input" + ext)
        shutil.copy2(input_path, dest)
        audio_path = dest
        steps.append(f"Audio loaded — {file_size_mb(audio_path):.1f} MB")
    else:
        raise ValueError(
            f"Unsupported format: {ext}\n"
            f"Video: {VIDEO_EXTS}\nAudio: {AUDIO_EXTS}"
        )

    size = file_size_mb(audio_path)
    if size > MAX_MB:
        steps.append(f"File is {size:.1f} MB > 200 MB — compressing for API upload...")
        audio_path = compress_audio(audio_path, out_dir)
        new_size = file_size_mb(audio_path)
        steps.append(f"Compressed to {new_size:.1f} MB (speech-quality 64 kbps mono)")
    else:
        steps.append(f"Size OK ({size:.1f} MB) — no compression needed")

    return audio_path, steps
