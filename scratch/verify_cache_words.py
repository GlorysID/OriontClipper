"""Temp validation for word-timing transcript cache (modules/transcriber.py).

Run:  .venv\\Scripts\\python.exe scratch\\verify_cache_words.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import transcriber as T  # noqa: E402
from modules.transcriber import (  # noqa: E402
    TranscriptionError,
    TranscriptSegment,
    segments_from_json,
    segments_to_json,
)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


# --- live-shape synthetic segments (dict words, as faster-whisper builds them)
live = [
    TranscriptSegment(
        start=0.0,
        end=4.137,
        text="Halo semua selamat datang",
        words=[
            {"word": "Halo", "start": 0.0, "end": 0.412},
            {"word": "semua", "start": 0.412, "end": 0.988},
            {"word": "selamat", "start": 1.204, "end": 1.876},
            {"word": "datang", "start": 1.876, "end": 2.553},
        ],
    ),
    TranscriptSegment(
        start=5.001,
        end=9.983,
        text="Kita bahas tools AI hari ini.",
        words=[
            {"word": "Kita", "start": 5.001, "end": 5.32},
            {"word": "bahas", "start": 5.32, "end": 5.771},
            {"word": "tools", "start": 5.999, "end": 6.604},
            {"word": "AI", "start": 6.604, "end": 7.012},
            {"word": "hari", "start": 7.012, "end": 7.44},
            {"word": "ini.", "start": 7.44, "end": 7.988},
        ],
    ),
]

# 1) round-trip through the real file path used by transcribe_video
doc = segments_to_json(live, model="small", language="id")
check("doc is versioned object", isinstance(doc, dict) and doc.get("version") == 2, str(doc.get("version")))
check("doc carries model/language", doc.get("model") == "small" and doc.get("language") == "id")
check("segment words persisted", all(isinstance(s["words"], list) and s["words"] for s in doc["segments"]))

with tempfile.TemporaryDirectory() as td:
    cache = Path(td) / "v2.segments.json"
    cache.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    loaded = segments_from_json(
        json.loads(cache.read_text(encoding="utf-8")), expected_model="small"
    )
    check("round-trip segment count", len(loaded) == len(live), f"{len(loaded)}")
    check(
        "round-trip identical to live",
        loaded == live,
        "" if loaded == live else f"live={live}\ncached={loaded}",
    )
    check(
        "words type identical (list[dict])",
        all(type(s.words) is list and all(type(w) is dict for w in s.words) for s in loaded),
    )
    check(
        "precision preserved >=3 decimals",
        loaded[1].words[0]["end"] == 5.32 and loaded[0].words[2]["start"] == 1.204,
    )

# 2) consumers must not distinguish cache vs live
def consume(segments):
    out = []
    for s in segments:
        for w in (getattr(s, "words", None) or []):
            out.append((str(w.get("word")), float(w.get("start")), float(w.get("end"))))
    return out

check("consumer view identical", consume(loaded) == consume(live), f"{len(consume(loaded))} words")

# 3) legacy v1 cache (bare list, no version, no words) -> must be REJECTED
v1 = [{"start": 0.0, "end": 2.0, "text": "lama sekali"}, {"start": 2.5, "end": 4.0, "text": "tanpa kata"}]
try:
    segments_from_json(v1)
    check("v1 bare list rejected", False, "no exception raised")
except TranscriptionError as exc:
    check("v1 bare list rejected", True, str(exc))

v1_obj = {"segments": [{"start": 0.0, "end": 2.0, "text": "hai", "words": None}]}
try:
    segments_from_json(v1_obj, expected_model="small")
    check("v1 object without version rejected", False)
except TranscriptionError as exc:
    check("v1 object without version rejected", True, str(exc))

v2_no_words = {
    "version": 2,
    "model": "small",
    "language": "id",
    "segments": [{"start": 0.0, "end": 2.0, "text": "hai"}],
}
try:
    segments_from_json(v2_no_words, expected_model="small")
    check("v2 segment missing 'words' key rejected", False)
except TranscriptionError as exc:
    check("v2 segment missing 'words' key rejected", True, str(exc))

try:
    segments_from_json(
        segments_to_json(live, model="large-v3", language="id"), expected_model="small"
    )
    check("model mismatch rejected", False)
except TranscriptionError as exc:
    check("model mismatch rejected", True, str(exc))

# 4) transcribe_video cache-hit path: v1 on disk must trigger re-transcription
class FakeT(T.WhisperTranscriber):
    def __init__(self):
        self.model_name = "small"
        self.device = "cpu"
        self.detected_language = "id"
        import logging

        self.logger = logging.getLogger("verify")
        self._model = None
        self.extract_calls: list = []
        self.transcribe_calls: list = []

    def extract_audio(self, video_path, audio_path):
        self.extract_calls.append(video_path)
        return audio_path

    def transcribe_audio(self, audio_path, language=None):
        self.transcribe_calls.append(audio_path)
        return live

with tempfile.TemporaryDirectory() as td:
    work = Path(td) / "work"
    work.mkdir()
    video = work / "clip.mp4"
    video.write_bytes(b"\x00" * 32)
    orig_cache_dir = T.config.TRANSCRIPT_CACHE_DIR
    try:
        T.config.TRANSCRIPT_CACHE_DIR = Path(td) / "cache"
        T.config.TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        stem = T.safe_cache_stem(video)

        # --- legacy cache present -> re-transcribe, then rewrite as v2
        legacy = T.config.TRANSCRIPT_CACHE_DIR / f"{stem}.segments.json"
        legacy.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")
        t = FakeT()
        t.transcribe_video(video, work, language="id")
        check("v1 cache triggered re-transcribe", len(t.transcribe_calls) == 1)
        rewritten = json.loads(legacy.read_text(encoding="utf-8"))
        check(
            "cache rewritten as v2 with words",
            rewritten.get("version") == 2 and bool(rewritten["segments"][0]["words"]),
        )

        # --- valid v2 cache present -> cache hit, no transcription
        t2 = FakeT()
        res = t2.transcribe_video(video, work, language="id")
        check("valid v2 cache hit skips transcription", t2.transcribe_calls == [] and t2.extract_calls == [])
        check("cache-hit segments carry words", all(s.words for s in res.segments))
        check(
            "cache-hit identical to live",
            res.segments == live,
        )
        srt = (work / "clip.srt").read_text(encoding="utf-8")
        check("SRT regenerated on cache hit", "-->" in srt and "Halo semua" in srt)
    finally:
        T.config.TRANSCRIPT_CACHE_DIR = orig_cache_dir

# 5) untouched helpers still work
compact = T.compact_transcript(live)
check("compact_transcript works", compact.count("\n") == 1 and "Halo semua" in compact)

print()
print("RESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILURES -> {FAILS}")
sys.exit(1 if FAILS else 0)
