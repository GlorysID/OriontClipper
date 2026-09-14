"""Cross-lane E2E package verification (TAHAP A.3).

Production paths only:
  transcribe_video (live whisper -> cache v2) -> cache-hit reload run ->
  VideoEditor.render_clip (karaoke ASS from cached words + overlay_renderer
  hook PNG with expression positions) -> mp4 gate checks.

No production files are modified; subprocess/log spies live here only.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
import shutil
import struct
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import modules.transcriber as tr  # noqa: E402
import modules.video_editor as ve  # noqa: E402

SOURCE = ROOT / "output/job_20260908_223824_6975234347/video_asli.mp4"
E2E = ROOT / "scratch/e2e"
real_run = subprocess.run

FAILS: list[str] = []

def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" :: {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok

# --- spies: capture logs + subprocess calls (utf-8 tolerant stderr) ----------
LOGS: list[str] = []

class LogCapture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOGS.append(f"{record.name}:{record.getMessage()}")
        except Exception:
            pass

logging.getLogger().addHandler(LogCapture())
logging.getLogger().setLevel(logging.INFO)

SUBPROCESS_CALLS: list[tuple[list[str], subprocess.CompletedProcess]] = []

def spy_run(command, *a, **k):
    cs = [str(c) for c in command] if isinstance(command, (list, tuple)) else [str(command)]
    if k.get("text") or k.get("universal_newlines"):
        k.pop("text", None)
        k.pop("universal_newlines", None)
        k["text"] = True
        k["encoding"] = "utf-8"
        k["errors"] = "replace"
    proc = real_run(command, *a, **k)
    SUBPROCESS_CALLS.append((cs, proc))
    return proc

def make_spy_namespace():
    return types.SimpleNamespace(
        run=spy_run,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
        DEVNULL=subprocess.DEVNULL,
        SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        CompletedProcess=subprocess.CompletedProcess,
    )

def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return real_run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

# --- 0. prepare: fresh dirs + 50s source trim --------------------------------
if E2E.exists():
    shutil.rmtree(E2E)
(E2E / "run1").mkdir(parents=True)
(E2E / "run2").mkdir(parents=True)
TRIM = E2E / "src50.mp4"

r = sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0", "-t", "50",
        "-i", str(SOURCE), "-c", "copy", "-avoid_negative_ts", "make_zero", str(TRIM)])
if r.returncode != 0:  # stream-copy refused -> quick re-encode fallback
    r = sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0", "-t", "50",
            "-i", str(SOURCE), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", str(TRIM)])
check("A3.0 trim 50s source exists", r.returncode == 0 and TRIM.exists(), r.stderr[-200:])

cache_path = config.TRANSCRIPT_CACHE_DIR / f"{tr.safe_cache_stem(TRIM)}.segments.json"
cache_path.unlink(missing_ok=True)  # force RUN 1 to be a LIVE transcription

# --- 1. RUN 1: live whisper, writes cache v2 ---------------------------------
tr.subprocess = make_spy_namespace()
t1 = tr.WhisperTranscriber()
res1 = t1.transcribe_video(TRIM, E2E / "run1")
words1 = sum(len(s.words or []) for s in res1.segments)
check("A3.1a run1 produced segments", len(res1.segments) > 0, f"{len(res1.segments)} segs")
check("A3.1b run1 words>0 (whisper word_timestamps)", words1 > 0, f"{words1} words")
check("A3.1c cache file written", cache_path.exists(), str(cache_path.name))
doc = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
check("A3.1d cache is version 2", doc.get("version") == 2, f"version={doc.get('version')}")

# --- 2. RUN 2: reload module, must be cache-hit (no audio-extract ffmpeg) ----
del t1
tr = importlib.reload(tr)
tr.subprocess = make_spy_namespace()
mark = len(SUBPROCESS_CALLS)
log_mark = len(LOGS)
t0 = time.time()
t2 = tr.WhisperTranscriber()
res2 = t2.transcribe_video(TRIM, E2E / "run2")
elapsed = time.time() - t0
new_calls = SUBPROCESS_CALLS[mark:]
check("A3.2a run2 cache-hit log", any("Loaded transcript cache" in l for l in LOGS[log_mark:]))
check("A3.2b run2 made NO subprocess calls (no whisper/audio work)", not new_calls,
      f"{len(new_calls)} calls in {elapsed:.1f}s")
words2 = sum(len(s.words or []) for s in res2.segments)
check("A3.2c run2 words>0", words2 > 0, f"{words2} words")

def tuples(segs):
    out = []
    for s in segs:
        out.append((round(s.start, 5), round(s.end, 5), s.text,
                    tuple((round(w["start"], 5), round(w["end"], 5), w["word"]) for w in (s.words or []))))
    return out

same = bool(res1.segments) and tuples(res1.segments) == tuples(res2.segments)
check("A3.2d run1 vs run2 word-by-word identical", same, f"{len(res1.segments)} vs {len(res2.segments)} segs")

# --- 3. RENDER: 12s clip from cached segments, expression-positioned hook -----
tr.subprocess = make_spy_namespace()
ve.subprocess = make_spy_namespace()
segs = res2.segments
clip_start = segs[0].start
clip_end = min(clip_start + 12.0, segs[-1].end)
editor = ve.VideoEditor()
mark = len(SUBPROCESS_CALLS)
final = editor.render_clip(
    video_path=TRIM,
    segments=segs,
    moment={"start": clip_start, "end": clip_end, "hook": "E2E PACKAGE CHECK"},
    clip_index=7,
    work_dir=E2E,
    user_template={"hook": {"x": "(W-w)/2", "y": "(h-160)/5"}},
)
check("A3.3 render_clip returned", final.exists(), final.name)
hook_png = E2E / "hook_overlay.png"
check("A3.3f hook PNG generated", hook_png.exists() and hook_png.stat().st_size > 1000)
check("A3.3g hook overlay logged by overlay_renderer",
      any("Rendered hook overlay" in l for l in LOGS))
bad_expr = [l for l in LOGS if "Could not evaluate position expression" in l]
check("A3.3h no position-expr fallback warnings", not bad_expr, "; ".join(bad_expr[:1]))

render_procs = [(cs, p) for cs, p in SUBPROCESS_CALLS[mark:] if "-filter_complex" in cs]
assert render_procs, "production render command not captured by spy"
render_stderr = (render_procs[-1][1].stderr or "") + (render_procs[-1][1].stdout or "")

# --- 4. gate checks on output --------------------------------------------------
probe = sh(["ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,width,height:format=duration", "-of", "json", str(final)])
pj = json.loads(probe.stdout)
v = next(s for s in pj["streams"] if s.get("codec_type") == "video")
dur = float(pj["format"]["duration"])
check("A3.4i valid mp4 1080x1920", (v["width"], v["height"]) == (config.TARGET_WIDTH, config.TARGET_HEIGHT),
      f"{v['width']}x{v['height']}")
check("A3.4i2 duration ~ clip", abs(dur - (clip_end - clip_start)) < 0.6, f"{dur:.2f}s")

def top_level_boxes(path: Path) -> list[str]:
    boxes, pos, total = [], 0, path.stat().st_size
    with path.open("rb") as f:
        while pos < total - 8:
            f.seek(pos)
            hdr = f.read(16)
            size = struct.unpack(">I", hdr[:4])[0]
            typ = hdr[4:8].decode("latin1")
            if size == 1:
                size = struct.unpack(">Q", hdr[8:16])[0]
            if size < 8:
                break
            boxes.append(typ)
            pos += size
    return boxes

boxes = top_level_boxes(final)
check("A3.4ii faststart (moov before mdat)", "moov" in boxes and "mdat" in boxes
      and boxes.index("moov") < boxes.index("mdat"), str(boxes))

lr = sh(["ffmpeg", "-hide_banner", "-i", str(final), "-af", "ebur128", "-f", "null", "-"])
vals = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", lr.stderr)
lufs = float(vals[-1]) if vals else None
check("A3.4iii integrated loudness ~ -16 +/-2", lufs is not None and abs(lufs + 16) <= 2,
      f"{lufs:.1f} LUFS")

# --- 4b. font evidence from the PRODUCTION render's own stderr ----------------
prod_fs = [l.strip() for l in render_stderr.splitlines() if "fontselect" in l.lower()]
prod_good = [l for l in prod_fs if "Montserrat" in l]
prod_bad = [l for l in prod_fs if "Montserrat" not in l]
check("A3.4iv prod stderr selects Montserrat ExtraBold", bool(prod_good) and not prod_bad,
      (prod_good or prod_fs or [""])[0][:110])

# --- 5. ASS timing proof: cached whisper words, not proportional guesses ------
ass = next(E2E.glob("clip7_karaoke.ass"))
ass_text = ass.read_text(encoding="utf-8")
dstarts = [m for m in re.findall(r"Dialogue: 0,(\d+):(\d+):(\d+)\.(\d+),", ass_text)]
def to_s(m):
    h, mi, s, cs = map(int, m)
    return h * 3600 + mi * 60 + s + cs / 100.0
d_start = sorted({to_s(m) for m in dstarts})
clip_segs = ve.build_clip_segments(segs, clip_start, clip_end)
cache_word_starts = sorted({round(w["start"], 3) for s in clip_segs for w in (s.words or [])})
def near(a, pool, tol=0.06):
    return any(abs(a - b) <= tol for b in pool)
matched = sum(1 for d in d_start if near(d, cache_word_starts))
rate = matched / len(d_start) if d_start else 0.0
check("A3.5a ASS dialogue starts match cached whisper word times", rate >= 0.9,
      f"{matched}/{len(d_start)} within 60ms")

# proportional-estimate comparison (coarse fallback the words should NOT equal)
from modules.transcriber import TranscriptSegment  # noqa: E402
bare = [TranscriptSegment(start=s.start, end=s.end, text=s.text) for s in clip_segs]
prop = sorted({t for seg in bare for t, _, _ in ve.segment_word_timings(seg)})
diverged = any(not near(p, cache_word_starts, 0.15) for p in prop)
check("A3.5b cached timings differ from proportional estimate", diverged,
      f"{len(prop)} proportional starts vs {len(cache_word_starts)} whisper starts")

# --- 6. fontselect evidence from a verbose standalone of the SAME ASS ---------
vf = f"subtitles='{ass.as_posix().replace(':', chr(92)+':')}':fontsdir='{(ROOT / 'assets/fonts').as_posix().replace(':', chr(92)+':')}'"
dbg = sh(["ffmpeg", "-hide_banner", "-loglevel", "verbose", "-ss", "2", "-t", "5",
          "-i", str(TRIM), "-vf", vf, "-f", "null", "-"])
fs = [l.strip() for l in dbg.stderr.splitlines() if "fontselect" in l.lower()]
good = [l for l in fs if "Montserrat" in l]
fallback = [l for l in fs if "Montserrat" not in l]
check("A3.4iv2 verbose standalone agrees", bool(good) and not fallback, (good or [""])[0][:110])

# --- 7. summary ----------------------------------------------------------------
print(f"\noutput: {final} ({final.stat().st_size/1e6:.2f} MB), LUFS={lufs:.1f}, font=Montserrat ExtraBold")
print("RESULT:", "ALL PASS" if not FAILS else f"FAILURES: {FAILS}")
sys.exit(1 if FAILS else 0)
