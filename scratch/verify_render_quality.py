"""Smoke test for the render-quality package.

Goes through the PRODUCTION path (VideoEditor.render_clip -> build_filter_chain
-> ffmpeg) on a 16s slice of the real test video, then verifies:
  1. output exists, moov before mdat (faststart)
  2. ebur128 integrated loudness ~ -16 LUFS
  3. no libass "font not found"/fallback warnings in render stderr
  4. bundled Montserrat ExtraBold is actually used (A/B frame hash vs empty
     fontsdir + libass debug fontselect trace)
  5. size/time vs old encode (ultrafast/crf24/threads2, no VBV, no loudnorm)
"""
from __future__ import annotations

import hashlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import modules.video_editor as ve  # noqa: E402
from modules.transcriber import TranscriptSegment  # noqa: E402

VIDEO = ROOT / "output/job_20260908_223824_6975234347/video_asli.mp4"
START, END = 60.0, 76.0
WORK = ROOT / "scratch/render_smoke"
real_run = subprocess.run

# --- capture production ffmpeg stderr -------------------------------------
captured: list[tuple[list[str], subprocess.CompletedProcess]] = []

def spy_run(command, *a, **k):
    r = real_run(command, *a, **k)
    cs = [str(c) for c in command]
    if "ffmpeg" in cs[0] or any(c.endswith("ffmpeg") or c.endswith("ffmpeg.exe") for c in cs[:3]):
        captured.append((cs, r))
    return r

ve.subprocess = types.SimpleNamespace(
    run=spy_run,
    SubprocessError=subprocess.SubprocessError,
    TimeoutExpired=subprocess.TimeoutExpired,
    CompletedProcess=subprocess.CompletedProcess,
)

# --- production render ------------------------------------------------------
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)

WORDS = [
    "the market shifted again today and nobody expected it",
    "everyone is talking about the new clipper tool",
    "this is exactly why vertical video wins attention",
    "cut the silence and keep only the punchy parts",
    "quality audio matters more than you think",
    "one render pass is all it takes to prove it",
    "subtitles should pop without stealing the frame",
    "fast start moov keeps streaming smooth everywhere",
]
segments = []
for i, text in enumerate(WORDS):
    s = START + 0.2 + i * 2.0
    segments.append(TranscriptSegment(start=s, end=s + 1.85, text=text))

editor = ve.VideoEditor()
final_path = editor.render_clip(
    video_path=VIDEO,
    segments=segments,
    moment={"start": START, "end": END, "hook": "SMOKE TEST HOOK"},
    clip_index=99,
    work_dir=WORK,
    user_template=None,
)
print(f"[1] production render -> {final_path} ({final_path.stat().st_size/1e6:.2f} MB)")

render_cmds = [(cs, r) for cs, r in captured if "-filter_complex" in cs]
assert render_cmds, "no ffmpeg render command captured"
cs, r = render_cmds[-1]
stderr = (r.stderr or "") + (r.stdout or "")
for flag, want in [("-preset", config.X264_PRESET), ("-crf", str(config.X264_CRF)),
                   ("-maxrate", config.X264_MAXRATE), ("-bufsize", config.X264_BUFSIZE),
                   ("-threads", str(config.X264_THREADS)), ("-movflags", "+faststart")]:
    assert flag in cs and cs[cs.index(flag) + 1] == want, f"{flag} {want} missing from command"
assert "-af" in cs and "loudnorm" in cs[cs.index("-af") + 1], "loudnorm -af missing"
print("[1] encode flags present:", " ".join(f"{f}={cs[cs.index(f)+1]}" for f in
      ("-preset", "-crf", "-maxrate", "-bufsize", "-threads", "-movflags")))

# --- ASS style font check ---------------------------------------------------
ass = next(WORK.glob("clip99_karaoke.ass"))
style = [l for l in ass.read_text(encoding="utf-8").splitlines() if l.startswith("Style:")]
print("[font] ASS style:", style[0][:80])
assert "Montserrat ExtraBold" in style[0]

# --- faststart: moov before mdat --------------------------------------------
def top_level_boxes(path: Path) -> list[str]:
    boxes, size_total = [], path.stat().st_size
    with path.open("rb") as f:
        pos = 0
        while pos < size_total - 8:
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

boxes = top_level_boxes(final_path)
assert "moov" in boxes and "mdat" in boxes, f"missing moov/mdat: {boxes}"
assert boxes.index("moov") < boxes.index("mdat"), f"NOT faststart: {boxes}"
print(f"[2] faststart OK, box order: {boxes}")

# --- loudness (use FINAL integrated value from the Summary section) ---------
lr = real_run(["ffmpeg", "-hide_banner", "-i", str(final_path), "-af", "ebur128",
               "-f", "null", "-"], capture_output=True, text=True)
lufs_all = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", lr.stderr)
assert lufs_all, "no integrated loudness found:\n" + lr.stderr[-1500:]
lufs = float(lufs_all[-1])
print(f"[3] integrated loudness: {lufs:.1f} LUFS (target -16 +/-2) -> "
      f"{'PASS' if abs(lufs + 16) <= 2 else 'FAIL'}")

# --- font fallback detection in production stderr -----------------------------
# libass logs a SUCCESSFUL match as: fontselect: (Montserrat ExtraBold, ...) -> Montserrat-ExtraBold
fs_lines = [l.strip() for l in stderr.splitlines() if "fontselect" in l.lower()]
print(f"[4] production stderr fontselect lines: {fs_lines or 'NONE'}")
bad = [l for l in fs_lines if "Montserrat" not in l]
bad += re.findall(r"(?im)^.*(?:font[^\n]{0,40}(?:not found|missing)|failed to (?:find|load) font|cannot select).*$", stderr)
print(f"[4] fallback/warning indicators: {bad or 'NONE'} -> {'PASS' if not bad else 'FAIL'}")

# --- A/B proof that the bundled font is actually used -------------------------
empty = Path(tempfile.mkdtemp(prefix="fonts_empty_"))
def filt(path: Path, fontsdir: str) -> str:
    a = path.as_posix().replace(":", "\\:")
    d = Path(fontsdir).as_posix().replace(":", "\\:")
    return f"subtitles='{a}':fontsdir='{d}'"

def frame_hash(fontsdir: str) -> str:
    p = real_run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(START),
                  "-i", str(VIDEO), "-ss", "3.0", "-frames:v", "1",
                  "-vf", filt(ass, fontsdir),
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                 capture_output=True)
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    return hashlib.sha256(p.stdout).hexdigest()[:16]

ha = frame_hash(str(config.BUNDLED_SUBTITLE_FONT_PATH.parent))
hb = frame_hash(str(empty))
print(f"[5] frame hash with bundled fontsdir: {ha}")
print(f"[5] frame hash with empty  fontsdir: {hb}")
print(f"[5] bundled font demonstrably used: {'YES (frames differ)' if ha != hb else 'NO (identical -> silent fallback!)'}")

dbg = real_run(["ffmpeg", "-hide_banner", "-loglevel", "verbose", "-ss", str(START),
                "-i", str(VIDEO), "-ss", "3.0", "-frames:v", "1",
                "-vf", filt(ass, str(config.BUNDLED_SUBTITLE_FONT_PATH.parent)),
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, text=True)
fs = [l.strip() for l in dbg.stderr.splitlines()
      if "fontselect" in l.lower() or "Loading font" in l]
print("[5] libass trace:", fs[:4] if fs else "(no explicit trace emitted)")

# --- duration + old-encode comparison -----------------------------------------
pr = real_run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(final_path)], capture_output=True, text=True)
print(f"[6] output duration: {float(pr.stdout.strip()):.2f}s (expected {END-START:.2f}s)")

old_out = WORK / "old_encode.mp4"
oc = list(cs)
def swap(lst, flag, val):
    lst[lst.index(flag) + 1] = val
swap(oc, "-preset", "ultrafast"); swap(oc, "-crf", "24"); swap(oc, "-threads", "2")
i = oc.index("-af"); del oc[i:i + 2]  # drop loudnorm to mirror the old pipeline
for f in ("-maxrate", "-bufsize"):
    j = oc.index(f); del oc[j:j + 2]
oc[-1] = str(old_out)
import time
t0 = time.time()
o = real_run(oc, capture_output=True, text=True)
assert o.returncode == 0, o.stderr[-2000:]
print(f"[7] old encode (ultrafast/crf24/threads2): {old_out.stat().st_size/1e6:.2f} MB in {time.time()-t0:.1f}s")
print(f"[7] new encode: {final_path.stat().st_size/1e6:.2f} MB")
print("DONE")
