"""Temp validation on REAL audio: 60s of output/job_.../video_asli.mp4.

Run:  .venv\\Scripts\\python.exe scratch\\verify_cache_real.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.transcriber import (  # noqa: E402
    TranscriptSegment,
    WhisperTranscriber,
    segments_from_json,
    segments_to_json,
)

logging.basicConfig(level=logging.INFO)
audio = ROOT / "scratch" / "real60.wav"
t = WhisperTranscriber()
live = t.transcribe_audio(audio, language=None)
print(f"live segments={len(live)} detected_lang={t.detected_language!r}")
print("words on", sum(1 for s in live if s.words), "/", len(live), "segments")

doc = segments_to_json(live, model=t.model_name, language=t.detected_language)
loaded = segments_from_json(json.loads(json.dumps(doc, ensure_ascii=False)), expected_model=t.model_name)

print("round-trip identical:", loaded == live)
print("segment type identical:", all(type(a) is TranscriptSegment for a in loaded))
print("words type identical:", all(type(s.words) is list and all(type(w) is dict for w in s.words) for s in loaded if s.words))
tot_live = sum(len(s.words or []) for s in live)
tot_cached = sum(len(s.words or []) for s in loaded)
print(f"word count live={tot_live} cached={tot_cached} equal={tot_live == tot_cached}")
sample = next((s for s in loaded if s.words), None)
print("sample:", sample.text[:50] if sample else None)
print("sample words:", sample.words[:4] if sample else None)
sys.exit(0 if loaded == live and tot_live == tot_cached and tot_live > 0 else 1)
