"""Post-Phase-B revalidation: hook font consistency + E2E clip re-render.

Uses the v2 transcript cache created by verify_e2e_package.py (cache-hit
required; live whisper is NOT re-run).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import modules.overlay_renderer as orr  # noqa: E402
import modules.transcriber as tr  # noqa: E402
import modules.video_editor as ve  # noqa: E402
from modules.video_editor import load_hook_template  # noqa: E402

E2E = ROOT / "scratch/e2e"
TRIM = E2E / "src50.mp4"
PRE_BASELINE_SHA = "973dfe01d398ed1d"  # hook PNG rendered by arialbd pre-edit
FAILS: list[str] = []

def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" :: {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return bool(ok)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
real_run = subprocess.run
CALLS: list[list[str]] = []
RENDER_STDERrs: list[str] = []

class Spy:
    @staticmethod
    def run(command, *a, **k):
        cs = [str(c) for c in command] if isinstance(command, (list, tuple)) else [str(command)]
        CALLS.append(cs)
        if k.get("text"):
            k.update(encoding="utf-8", errors="replace")
        proc = real_run(command, *a, **k)
        if "-filter_complex" in cs:
            RENDER_STDERrs.append((proc.stderr or "") + (proc.stdout or ""))
        return proc

    SubprocessError = subprocess.SubprocessError
    TimeoutExpired = subprocess.TimeoutExpired
    PIPE = subprocess.PIPE
    STDOUT = subprocess.STDOUT
    DEVNULL = subprocess.DEVNULL
    CalledProcessError = subprocess.CalledProcessError
    CompletedProcess = subprocess.CompletedProcess

# 1. font resolution matrix -----------------------------------------------------
capcut = load_hook_template()
r_default = orr._resolve_font_path(capcut.get("hook", {}), "hook") or ""
check("B3.1a default/capcut hook -> bundled TTF", Path(r_default).name == "Montserrat-ExtraBold.ttf", r_default)
explicit = {"font_file": "assets/fonts/Montserrat-ExtraBold.ttf"}
check("B3.1b explicit font_file honored", Path(orr._resolve_font_path(explicit) or "").exists())
custom = {"font_name": "Bebas Neue"}
r_custom = orr._resolve_font_path(custom, "hook") or ""
check("B3.1c custom font_name bypasses bundled (old chain intact)",
      bool(r_custom) and Path(r_custom).name != "Montserrat-ExtraBold.ttf", r_custom)

# 2. hook PNG hash differs from pre-edit (arialbd) baseline ----------------------
p = orr.render_hook("E2E PACKAGE CHECK", capcut, E2E / "run2")
sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
check("B3.2 hook PNG re-rendered with Montserrat (hash != arialbd baseline)",
      sha != PRE_BASELINE_SHA, f"post={sha} pre={PRE_BASELINE_SHA}")

# 3. production clip re-render from cache ----------------------------------------
tr.subprocess = ve.subprocess = Spy()
cache = config.TRANSCRIPT_CACHE_DIR / f"{tr.safe_cache_stem(TRIM)}.segments.json"
check("B3.3a v2 cache present", cache.exists()
      and json.loads(cache.read_text(encoding="utf-8")).get("version") == 2)
w = E2E / "postb"
w.mkdir(exist_ok=True)
res = tr.WhisperTranscriber().transcribe_video(TRIM, w)
check("B3.3b transcribe cache-hit (0 subprocess calls)", not CALLS, f"{len(CALLS)} calls")
segs = res.segments
cs, ce = segs[0].start, min(segs[0].start + 12.0, segs[-1].end)
final = ve.VideoEditor().render_clip(
    video_path=TRIM, segments=segs,
    moment={"start": cs, "end": ce, "hook": "E2E PACKAGE CHECK"},
    clip_index=8, work_dir=w,
    user_template={"hook": {"x": "(W-w)/2", "y": "(h-160)/5"}},
)
render_cmd = next((c for c in CALLS if "-filter_complex" in c), [])
ok_flags = bool(render_cmd) and render_cmd[render_cmd.index("-preset") + 1] == config.X264_PRESET \
    and any("Montserrat ExtraBold" in s for s in RENDER_STDERrs) \
    and not [l for s in RENDER_STDERrs for l in s.splitlines()
             if "fontselect" in l.lower() and "Montserrat" not in l]
check("B3.3c render: medium preset + ASS Montserrat fontselect, no fallback", ok_flags, final.name)
lr = real_run(["ffmpeg", "-hide_banner", "-i", str(final), "-af", "ebur128", "-f", "null", "-"],
              capture_output=True, text=True, encoding="utf-8", errors="replace")
vals = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", lr.stderr)
lufs = float(vals[-1]) if vals else None
check("B3.3d loudness still ~ -16", lufs is not None and abs(lufs + 16) <= 2, f"{lufs} LUFS")
print("\noutput:", final, f"{final.stat().st_size/1e6:.2f} MB")
print("RESULT:", "ALL PASS" if not FAILS else f"FAILURES: {FAILS}")
sys.exit(1 if FAILS else 0)
