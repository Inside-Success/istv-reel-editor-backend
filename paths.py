"""Shared paths for the standalone reels tool."""
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
INPUT_DIR = TOOL_ROOT / "input"
OUTPUT_ROOT = TOOL_ROOT / "generated_data"
