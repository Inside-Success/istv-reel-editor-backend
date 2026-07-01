import time
from rev_ai import apiclient

from src.cutter import normalize_word_timings


def transcribe_audio(
    audio_path: str,
    api_key: str,
    progress_cb=None,
    *,
    remove_disfluencies: bool = False,
    on_submitted=None,
) -> dict:
    """
    Submit audio to Rev.ai, poll until done, return structured verbatim transcript.
    Disfluencies (uh/um) are kept by default for timing fidelity; client may filter later.
    Raises RuntimeError on failure or timeout.

    `on_submitted(revai_job_id)`, if given, fires right after Rev.ai accepts the job —
    callers can persist that id so a crashed/restarted process can reattach to the
    still-running Rev.ai job via `poll_transcription_job` instead of re-uploading audio.
    """
    client = apiclient.RevAiAPIClient(api_key)

    _log(progress_cb, "Uploading audio to Rev.ai (verbatim word-level)...")
    job = client.submit_job_local_file(
        filename=audio_path,
        language="en",
        skip_diarization=False,
        skip_punctuation=False,
        remove_disfluencies=remove_disfluencies,
        metadata="ISTV Reel Editor",
    )
    job_id = job.id
    if on_submitted:
        on_submitted(job_id)
    _log(progress_cb, f"Job submitted (ID: {job_id}). Waiting for transcription...")

    return poll_transcription_job(job_id, api_key, progress_cb)


def poll_transcription_job(job_id: str, api_key: str, progress_cb=None) -> dict:
    """Poll an already-submitted Rev.ai job to completion and return the parsed transcript.

    Split out from `transcribe_audio` so a resumed process can reattach to a job
    submitted before a crash/restart, without re-uploading the source audio.
    """
    client = apiclient.RevAiAPIClient(api_key)

    elapsed = 0
    wait = 10  # start at 10 s, grow to 30 s
    while True:
        details = client.get_job_details(job_id)
        status = str(details.status).lower()

        if "transcribed" in status:
            break
        if "failed" in status:
            failure = getattr(details, "failure", "unknown error")
            raise RuntimeError(f"Rev.ai transcription failed: {failure}")

        _log(progress_cb, f"Transcribing... ({elapsed}s elapsed, checking again in {wait}s)")
        time.sleep(wait)
        elapsed += wait
        wait = min(wait + 5, 30)

        if elapsed > 1800:
            raise RuntimeError("Transcription timed out after 30 minutes.")

    _log(progress_cb, "Transcription complete — fetching word-level data...")
    raw = client.get_transcript_json(job_id)
    return _parse(raw)


def _parse(raw: dict) -> dict:
    """Convert Rev.ai monologue JSON to a flat word-by-word list + metadata."""
    words = []
    text_parts = []
    word_index = 0

    for mono in raw.get("monologues", []):
        speaker = mono.get("speaker", 0)
        for el in mono.get("elements", []):
            if el.get("type") == "text":
                words.append(
                    {
                        "index": word_index,
                        "word": el["value"],
                        "start": float(el.get("ts", 0.0) or 0.0),
                        "end": float(el.get("end_ts", 0.0) or 0.0),
                        "confidence": el.get("confidence", 1.0),
                        "speaker": speaker,
                    }
                )
                word_index += 1
                text_parts.append(el["value"])
            elif el.get("type") == "punct":
                text_parts.append(el.get("value", ""))

    words = normalize_word_timings(words)
    duration = words[-1]["end"] if words else 0.0
    return {
        "words": words,
        "full_text": "".join(text_parts).strip(),
        "duration": duration,
        "word_count": len(words),
    }


def build_timestamped_text(
    transcript: dict, chunk: int = 25, *, include_speakers: bool = True
) -> str:
    """Group words into N-word chunks with [MM:SS] and optional Rev speaker id per chunk."""
    words = transcript["words"]
    lines = []
    for i in range(0, len(words), chunk):
        group = words[i : i + chunk]
        ts = fmt_time(group[0]["start"])
        text = " ".join(w["word"] for w in group)
        sp = int(group[0].get("speaker", 0) or 0)
        tag = f" [Speaker {sp}]" if include_speakers else ""
        lines.append(f"[{ts}]{tag} {text}")
    return "\n".join(lines)


def speaker_prompt_summary(transcript: dict) -> str:
    """Short diarization stats for LLM prompts (who talks how much)."""
    words = transcript.get("words") or []
    if not words:
        return "(No word-level diarization.)"
    counts: dict[int, int] = {}
    for w in words:
        sp = int(w.get("speaker", 0) or 0)
        counts[sp] = counts.get(sp, 0) + 1
    total = len(words)
    ranked = sorted(counts.items(), key=lambda x: -x[1])
    lines = [
        f"- Speaker {sp}: {100 * n / total:.1f}% of words ({n} words)"
        for sp, n in ranked[:8]
    ]
    top = ", ".join(str(sp) for sp, _ in ranked[:2])
    lines.append(
        f"\nThe interview guest/expert is usually among the top speakers by share (often Speakers {top}). "
        "Hooks should start on their substantive speech, not on a rare low-talk-time speaker line unless that line is clearly the expert."
    )
    return "\n".join(lines)


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _log(cb, msg: str):
    if cb:
        cb(msg)
