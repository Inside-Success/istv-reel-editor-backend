# The Reels Tool Logic

Standalone reel generation pipeline — no website, no desktop app, no API server.

Drop a source video into `input/`, run one command, get 9:16 karaoke reels in `generated_data/`.

Uses the **updated_v2_test2** profile (same as `caylene_updated_v2_test2`):
- 30–90 second reels with context-aware cut points (long narrative arcs up to ~93s)
- v2 transcript cleanup (STT spelling fixes)
- 4-word karaoke captions, fillers hidden in subtitles
- No burned-in text hook / speaker lower-third overlays
- Claude `claude-opus-4-8` for analysis

## Requirements

- **Python 3.11+**
- **Node.js** (for FFmpeg export only — no npm install needed)
- **FFmpeg + ffprobe** on your PATH
- **Rev.ai** API key (transcription)
- **Anthropic** API key (analysis + cleanup)

## Setup

```bash
cd "the reels tool logic - main file"
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and add your `REVAI_API_KEY` and `CLAUDE_API_KEY`.

## Usage

1. Put your source video (`.mp4`, `.mov`, `.mkv`, etc.) in the `input/` folder.
2. Run:

```bash
python generate_reels.py --name "Speaker Name"
```

Reels are saved to `generated_data/<date>_updated_v2_test2/exported/`.

### Options

```bash
python generate_reels.py --video "C:\path\to\interview.mp4"
python generate_reels.py --name "Caylene Salii"
python generate_reels.py --num-reels 10
python generate_reels.py --transcript path\to\transcript.json   # skip Rev.ai
```

### Re-export from saved analysis (exact same reels)

If you already have a job bundle with `analysis.json` (e.g. the reference `caylene_updated_v2_test2` batch), re-export MP4s without re-running Claude:

```bash
python generate_reels.py --export-only --job-dir generated_data/caylene_updated_v2_test2 --name "Caylene Salii"
```

No API keys needed for export-only.

## Output

Each run creates a job folder under `generated_data/`:

```
generated_data/2026-06-30_updated_v2_test2/
├── transcript.json
├── analysis.json
├── source_video.json
└── exported/
    ├── reel_01_..._916_karaoke.mp4
    ├── reel_02_Her_Marriage_and_Job_Collapsed_the_Same_Day_916_karaoke.mp4
    ├── ...
    ├── Caylene_marketing_package.docx
    ├── Caylene_marketing_package.html
    └── Caylene_all_reels_916_karaoke.zip
```

## Pipeline

```
Video → FFmpeg audio extract → Rev.ai transcription
     → Claude transcript cleanup (v2)
     → Claude reel selection + cut sheets (v2 hardening)
     → Word-accurate cutter (context-aware, 30–90s)
     → Node/FFmpeg 9:16 karaoke export (no text overlays)
     → Marketing doc + zip
```

## Reference batch

The canonical reference output is `generated_data/caylene_updated_v2_test2/` — long-form narrative reels like reel 2 (93s, single continuous span). Fresh Claude runs may pick different moments; use `--export-only` to reproduce an exact saved `analysis.json`.

## Folder layout

```
the reels tool logic - main file/
├── generate_reels.py      ← main entry point
├── export_pipeline.py     ← MP4 export orchestration
├── src/                   ← core Python logic
├── export/                ← FFmpeg render engine (Node)
├── input/                 ← drop videos here
├── generated_data/        ← output
├── requirements.txt
└── .env.example
```
