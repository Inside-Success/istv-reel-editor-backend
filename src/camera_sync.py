"""Multi-camera audio-based sync.

Computes a fixed time offset between each camera's on-board audio and a
dedicated reference audio recording (e.g. a lav mic / field recorder), via
FFT cross-correlation. This is a constant-offset model only — it assumes a
standardized studio setup with negligible clock drift over an interview-length
shoot; it does not correct for drift across multi-hour recordings.

The reference audio is the only thing ever transcribed (see src/transcription.py) —
reel selection and word timing are built entirely on that one timeline. Camera
offsets computed here are only used at export time, to seek into the correct
point of each camera's own file for a given reference-timeline segment.
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

CORRELATION_SAMPLE_RATE = 4000  # Hz — plenty for timing correlation, not quality
DEFAULT_WINDOW_SEC = 300  # 5 min — generous for a studio setup where devices
# are started within a few minutes of each other; increase if cameras are
# started further apart than this.


def check_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError(
            "FFmpeg not found.\n"
            "Please install FFmpeg and add it to your system PATH.\n"
            "Download: https://ffmpeg.org/download.html"
        )


def extract_correlation_track(path: str, out_wav_path: str, window_sec: int = DEFAULT_WINDOW_SEC) -> str:
    """Extract a short, low-samplerate mono WAV snippet for fast correlation.

    Only the first `window_sec` seconds are extracted — cross-correlation cost
    scales with this window, and a studio setup only needs enough overlap to
    find the alignment, not the whole (possibly multi-hour) recording.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-t", str(window_sec),
            "-i", path,
            "-vn",
            "-ac", "1",
            "-ar", str(CORRELATION_SAMPLE_RATE),
            "-acodec", "pcm_s16le",
            out_wav_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Correlation audio extraction failed for {path}:\n{result.stderr[-600:]}")
    return out_wav_path


def _read_wav_mono(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    # Remove DC offset so silence/room-tone doesn't dominate the correlation.
    samples -= samples.mean()
    return samples


def compute_offset(reference_wav: str, camera_wav: str) -> dict:
    """Return {"offset_sec": float, "confidence": float}.

    `offset_sec` is added to a reference-timeline time to get the
    corresponding time in the camera's own file:
    `camera_time = reference_time + offset_sec`.

    `confidence` is the correlation peak normalized against the array's
    typical (std) magnitude — a rough signal-to-noise measure. Low confidence
    (e.g. < 3) usually means one side had no usable ambient audio to match
    against and the result should be treated as unreliable / reviewed manually.
    """
    ref = _read_wav_mono(reference_wav)
    cam = _read_wav_mono(camera_wav)
    if ref.size == 0 or cam.size == 0:
        return {"offset_sec": 0.0, "confidence": 0.0}

    # Cross-correlate via FFT (zero-padded to avoid circular wraparound):
    # correlation[k] peaks where shifting `cam` by k samples best matches `ref`.
    n = ref.size + cam.size - 1
    fft_size = 1 << (n - 1).bit_length()
    ref_f = np.fft.rfft(ref, fft_size)
    cam_f = np.fft.rfft(cam, fft_size)
    corr = np.fft.irfft(ref_f * np.conj(cam_f), fft_size)
    # Lag 0 is at index 0; negative lags wrap to the end of the array.
    corr = np.concatenate((corr[-(cam.size - 1):], corr[: ref.size]))
    lags = np.arange(-(cam.size - 1), ref.size)

    peak_idx = int(np.argmax(np.abs(corr)))
    lag_samples = int(lags[peak_idx])
    # `lags[peak_idx]` is the shift that best aligns cam onto ref under this
    # correlation's convention, which comes out as -(camera_time - reference_time)
    # for matching content — negate to get offset_sec such that
    # camera_time = reference_time + offset_sec (verified against a synthetic
    # signal with a known, deliberately asymmetric shift).
    offset_sec = -lag_samples / CORRELATION_SAMPLE_RATE

    peak = abs(corr[peak_idx])
    baseline = float(np.std(corr)) or 1.0
    confidence = float(peak / baseline)

    return {"offset_sec": offset_sec, "confidence": confidence}


def sync_cameras(
    reference_audio_path: str,
    camera_paths: dict[str, str],
    *,
    window_sec: int = DEFAULT_WINDOW_SEC,
    work_dir: str,
    progress_cb=None,
) -> dict[str, dict]:
    """Compute a time offset for each camera relative to the reference audio.

    Returns {camera_id: {"offset_sec": float, "confidence": float}}.
    `work_dir` holds the small extracted correlation WAVs (caller's temp dir).
    """
    check_ffmpeg()
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    def _log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    _log("Extracting reference audio window...")
    ref_wav = extract_correlation_track(
        reference_audio_path, str(work / "reference.wav"), window_sec
    )

    results: dict[str, dict] = {}
    for cam_id, cam_path in camera_paths.items():
        _log(f"Syncing camera {cam_id}...")
        cam_wav = extract_correlation_track(
            cam_path, str(work / f"camera_{cam_id}.wav"), window_sec
        )
        result = compute_offset(ref_wav, cam_wav)
        results[cam_id] = result
        _log(
            f"Camera {cam_id}: offset {result['offset_sec']:+.3f}s "
            f"(confidence {result['confidence']:.1f})"
        )
    return results
