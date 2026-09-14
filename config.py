"""Central configuration for ContentClipper.

Assumptions are intentionally documented here instead of hidden in code:
- 9Router is assumed to run locally at http://127.0.0.1:20128/v1 unless
  overridden by NINEROUTER_BASE_URL.
- Whisper runs on CPU by default for a 4GB RAM VPS. Set WHISPER_DEVICE only
  after confirming the host has a suitable GPU.
- The 9Router model value is an alias/combo configured in the 9Router
  dashboard, not a provider-specific model name.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _path_from_env(name: str, default: str) -> Path:
    value = os.getenv(name, default)
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


INPUT_DIR = _path_from_env("INPUT_DIR", "input")
OUTPUT_DIR = _path_from_env("OUTPUT_DIR", "output")
PROCESSED_DIR = _path_from_env("PROCESSED_DIR", "processed")
FAILED_DIR = _path_from_env("FAILED_DIR", "failed")
TMP_DIR = _path_from_env("TMP_DIR", "tmp")
TRANSCRIPT_CACHE_DIR = _path_from_env("TRANSCRIPT_CACHE_DIR", "tmp/transcripts")
LOG_DIR = _path_from_env("LOG_DIR", "logs")
HOOK_TEMPLATE_PATH = _path_from_env("HOOK_TEMPLATE_PATH", "hook_templates/capcut.json")
HOOK_EDITOR_TOKEN = os.getenv("HOOK_EDITOR_TOKEN", "")
# Campaign compliance profiles (S&K) live here as {id}.json. Tracked in git on
# purpose: these are curated config, not per-run user/runtime data.
CAMPAIGNS_DIR = _path_from_env("CAMPAIGNS_DIR", "data/campaigns")

WATCH_PATTERNS = ["*.mp4", "*.mov", "*.mkv"]
FILE_STABILITY_INTERVAL_S = float(os.getenv("FILE_STABILITY_INTERVAL_S", "3"))
FILE_STABILITY_CHECKS = int(os.getenv("FILE_STABILITY_CHECKS", "2"))

NINEROUTER_BASE_URL = os.getenv("NINEROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
NINEROUTER_API_KEY = os.getenv("NINEROUTER_API_KEY", "")
NINEROUTER_MODEL = os.getenv("NINEROUTER_MODEL", "auto")
REQUEST_TIMEOUT_S = int(os.getenv("REQUEST_TIMEOUT_S", "120"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF_S = float(os.getenv("RETRY_BACKOFF_S", "5"))
AI_TRANSCRIPT_CHUNK_CHARS = int(os.getenv("AI_TRANSCRIPT_CHUNK_CHARS", "12000"))
AI_MAX_TRANSCRIPT_CHUNKS = int(os.getenv("AI_MAX_TRANSCRIPT_CHUNKS", "12"))

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
OMP_NUM_THREADS = os.getenv("OMP_NUM_THREADS", "1")
TORCH_NUM_THREADS = int(os.getenv("TORCH_NUM_THREADS", "1"))
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "auto")
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "3"))
WHISPER_VAD_FILTER = os.getenv("WHISPER_VAD_FILTER", "1") == "1"

FFMPEG_THREADS = int(os.getenv("FFMPEG_THREADS", "2"))
# x264 encode settings for final clip render. Clips are delivered through
# Telegram, so maxrate/bufsize act as a VBV size guard while CRF 21 keeps
# subtitle edges clean. X264_THREADS=0 lets libx264 pick the thread count.
X264_PRESET = os.getenv("X264_PRESET", "medium")
X264_CRF = os.getenv("X264_CRF", "21")
X264_MAXRATE = os.getenv("X264_MAXRATE", "8M")
X264_BUFSIZE = os.getenv("X264_BUFSIZE", "16M")
X264_THREADS = int(os.getenv("X264_THREADS", "0"))
# Single-pass loudness normalization applied as the FINAL stage of the audio
# filter chain (skipped automatically for sources without an audio stream).
AUDIO_LOUDNORM_FILTER = os.getenv("AUDIO_LOUDNORM_FILTER", "loudnorm=I=-16:TP=-1.5:LRA=11")
USE_IONICE = os.getenv("USE_IONICE", "1") == "1"
TARGET_WIDTH = int(os.getenv("TARGET_WIDTH", "1080"))
TARGET_HEIGHT = int(os.getenv("TARGET_HEIGHT", "1920"))
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "3"))
MAX_PROPOSED_CLIPS = int(os.getenv("MAX_PROPOSED_CLIPS", "25"))
MAX_CLIP_DURATION_S = float(os.getenv("MAX_CLIP_DURATION_S", "120"))
MIN_CLIP_DURATION_S = float(os.getenv("MIN_CLIP_DURATION_S", "1"))
HOOK_BADGE_TEXT = os.getenv("HOOK_BADGE_TEXT", "HOT TAKE")
HOOK_DISPLAY_SECONDS = float(os.getenv("HOOK_DISPLAY_SECONDS", "3"))
HOOK_FADE_SECONDS = float(os.getenv("HOOK_FADE_SECONDS", "0.35"))
HOOK_MAX_LINE_CHARS = int(os.getenv("HOOK_MAX_LINE_CHARS", "20"))
HOOK_MAX_LINES = int(os.getenv("HOOK_MAX_LINES", "3"))

SUBTITLE_FONT_NAME = os.getenv("SUBTITLE_FONT_NAME", "DejaVu Sans Bold")
SUBTITLE_FONT_PATH = os.getenv(
    "SUBTITLE_FONT_PATH",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
# Bundled subtitle font: prioritized on ALL platforms by VideoEditor's font
# resolution (assumed internal family name in the ASS style). If the file is
# missing, resolution falls back to SUBTITLE_FONT_PATH / Windows fonts.
BUNDLED_SUBTITLE_FONT_PATH = _path_from_env(
    "BUNDLED_SUBTITLE_FONT_PATH", "assets/fonts/Montserrat-ExtraBold.ttf"
)
BUNDLED_SUBTITLE_FONT_NAME = os.getenv("BUNDLED_SUBTITLE_FONT_NAME", "Montserrat ExtraBold")

LOG_FILE = LOG_DIR / "contentclipper.log"
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USERS = os.getenv("TELEGRAM_ALLOWED_USERS", "")


def get_allowed_users() -> list[int]:
    """Return list of allowed user IDs, or empty list if unrestricted."""
    if not TELEGRAM_ALLOWED_USERS:
        return []
    return [int(x.strip()) for x in TELEGRAM_ALLOWED_USERS.split(",") if x.strip()]

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".mkv"}
