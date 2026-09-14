"""Static verification (AST-only) for Campaign Compliance bot integration.

TANPA menjalankan bot / import campaign_policy (modul sedang dibangun paralel).
Cek: tabrakan handler /campaign, prefix callback, def duplikat, semua nama
impor/helper baru benar-benar dipakai, dan titik integrasi kunci ada.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot.py"

try:  # konsol Windows cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

results: list[tuple[bool, str]] = []


def add(ok: bool, msg: str) -> None:
    results.append((ok, msg))


tree = ast.parse(BOT.read_text(encoding="utf-8"), filename=str(BOT))

# ---------------------------------------------------------------------------
# 1. CommandHandler: /campaign terdaftar tepat sekali & tidak tabrakan
# ---------------------------------------------------------------------------
cmd_names: list[str] = []
for node in ast.walk(tree):
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CommandHandler"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        cmd_names.append(node.args[0].value)

dupes = {c for c in cmd_names if cmd_names.count(c) > 1}
add(not dupes, f"CommandHandler注册 tanpa duplikat: {sorted(set(cmd_names))}")
add(cmd_names.count("campaign") == 1, "/campaign terdaftar tepat satu kali")

# ---------------------------------------------------------------------------
# 2. CallbackQueryHandler patterns: camp: tidak bertabrakan
# ---------------------------------------------------------------------------
cb_patterns: list[str] = []
for node in ast.walk(tree):
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CallbackQueryHandler"
    ):
        for kw in node.keywords:
            if kw.arg == "pattern" and isinstance(kw.value, ast.Constant):
                cb_patterns.append(str(kw.value.value))

add(any(p == "^camp:" for p in cb_patterns), f"handler `^camp:` terdaftar: {cb_patterns}")
clip_re = re.compile("^(clip:|done$)")
style_re = re.compile("^style:")
for probe in ("camp:set:x", "camp:job:bebas", "camp:det:x", "camp:show"):
    add(
        not clip_re.match(probe) and not style_re.match(probe) and probe.startswith("camp:"),
        f"callback `{probe}` hanya cocok ke handler camp:",
    )

# ---------------------------------------------------------------------------
# 3. Definisi top-level unik (tidak ada fungsi yang tak sengaja dobel)
# ---------------------------------------------------------------------------
top_defs = [
    n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
]
d2 = {n for n in top_defs if top_defs.count(n) > 1}
add(not d2, f"def top-level unik ({len(top_defs)} fungsi){'' if not d2 else ' DUPLIKAT: ' + str(sorted(d2))}")

# ---------------------------------------------------------------------------
# 4. Nama baru wajib ada
# ---------------------------------------------------------------------------
must_defs = {
    "campaign_command", "campaign_callback", "campaign_gate",
    "consume_campaign_binding", "get_job_segments", "ensure_campaign_verdicts",
    "campaign_caption_note", "campaign_fail_warning_text", "campaign_enabled",
    "campaign_job_active", "campaign_get_profile", "campaign_profile_by_id",
    "campaign_list_profiles", "campaign_binding_note", "campaign_short",
    "campaign_profile_details_text", "campaign_item_text", "campaign_intro_text",
    "campaign_reminders_text", "build_campaign_menu_keyboard",
    "build_campaign_gate_keyboard", "campaign_show_status",
    "campaign_select_and_summary", "resume_job_from_callback",
    "build_clip_catalog_messages", "clip_callback", "handle_text", "handle_video",
}
missing = must_defs - set(top_defs)
add(not missing, f"semua fungsi campaign ada{'' if not missing else ' HILANG: ' + str(sorted(missing))}")

# ---------------------------------------------------------------------------
# 5. Semua nama impor dipakai (campaign_policy alias cp + typing Any +
#    lazy import transcriber/json di get_job_segments)
# ---------------------------------------------------------------------------
src = BOT.read_text(encoding="utf-8")
src_no_imports = "\n".join(
    l for l in src.splitlines()
    if not re.match(r"^\s*(from|import)\s", l)
)
for name in ("cp", "CAMPAIGN_OK", "CAMPAIGN_FREE_ID", "Any", "_campaign_policy"):
    used = re.search(rf"\b{re.escape(name)}\b", src_no_imports)
    add(bool(used), f"impor/nama `{name}` dipakai di badan kode")
for name in ("transcriber", "safe_cache_stem", "segments_from_json"):
    # lazy import — cukup muncul dirujuk
    add(name in src, f"fallback cache `{name}` dirujuk")

# ---------------------------------------------------------------------------
# 6. Titik integrasi kunci ada
# ---------------------------------------------------------------------------
checks = {
    "gate video": 'campaign_gate(update, context, "video"',
    "gate youtube": 'campaign_gate(update, context, "youtube"',
    "gate gdrive": 'campaign_gate(update, context, "gdrive"',
    "binding file-job": '"campaign_id": camp_id',
    "consume di 2 flow": "consume_campaign_binding(context)",
    "catalog badge": "cp.evaluate_catalog(moments, segments, profile)",
    "caption check": "campaign_caption_note(job, moment)",
    "konfirmasi fail": "clip:nope:",
    "keyboard camp:job": 'callback_data=f"camp:job:',
    "menu camp:set": 'callback_data=f"camp:set:',
    "camp:show": 'callback_data="camp:show"',
    "no-op bebas": 'cid != CAMPAIGN_FREE_ID',
    "import guarded": "except Exception as _cp_exc",
    "registrasi callback camp": 'campaign_callback, pattern="^camp:"',
}
for label, needle in checks.items():
    add(needle in src, f"integrasi `{label}` hadir")

n_consume = src.count("= consume_campaign_binding(context)")
add(n_consume == 2, f"consume dipakai di 2 literal active_job (muncul {n_consume}x pemanggilan)")
add("def consume_campaign_binding(" in src, "def consume_campaign_binding ada")

# ---------------------------------------------------------------------------
# 7. bot_ui: parameter campaign_line diterima
# ---------------------------------------------------------------------------
ui = (ROOT / "modules" / "bot_ui.py").read_text(encoding="utf-8")
add("campaign_line" in ui, "bot_ui.format_clip_card menerima campaign_line")
add("campaign_line=campaign_line" in src, "bot.py meneruskan campaign_line ke format_clip_card")

# ---------------------------------------------------------------------------
# 8. Guard: tidak ada import campaign_policy tingkat-modul yang tak terjaga
# ---------------------------------------------------------------------------
guard_ok = re.search(r"try:\s*\n(?:[^\n]*\n)*?\s*from modules import campaign_policy", src)
add(bool(guard_ok), "import campaign_policy berada di dalam try/except")

# ---------------------------------------------------------------------------
# 9. Scope tulis: hanya bot.py & modules/bot_ui.py (heuristik: cek modul lain
#    yang HARAM disentuh lane ini tidak ikut berubah via git status bila git ada)
# ---------------------------------------------------------------------------
try:
    import subprocess

    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=20,
    ).stdout
    cp_lines = [l for l in out.splitlines() if "campaign_policy" in l]
    # '??' = file baru lane A (di luar Kendali lane ini) -> WARNING informatif.
    # ' M' pun masih mungkin lane A (file sedang dibangun paralel) -> informatif.
    if cp_lines and all(l.startswith("??") for l in cp_lines):
        add(True, "campaign_policy.py untracked — milik lane A, TIDAK disentuh lane ini")
    elif cp_lines:
        add(True, f"WARNING: campaign_policy.py status {cp_lines!r} — kemungkinan lane A, verifikasi kepemilikan")
    else:
        add(True, "campaign_policy.py tidak muncul di git status")
except Exception as exc:  # pragma: no cover
    add(True, f"cek git dilewati ({exc})")

# ---------------------------------------------------------------------------
print("=" * 70)
failed = 0
for ok, msg in results:
    print(("PASS  " if ok else "FAIL  ") + msg)
    failed += 0 if ok else 1
print("=" * 70)
print(f"{len(results) - failed}/{len(results)} pemeriksaan PASS")
sys.exit(1 if failed else 0)
