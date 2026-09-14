"""Static (AST-only) verification for Campaign Compliance LANGKAH-2 di bot.py.

TANPA import bot.py / campaign_policy (modul lain masih dibangun paralel).

Cakupan pemeriksaan:
  1. Nama fungsi ``cp.*`` yang dipakai bot HANYA yang diizinkan kontrak
     (langkah 1 + kontrak langkah 2) — tidak ada karangan sepihak.
  2. Semua jalur baru terlindung guard CAMPAIGN_OK (``campaign_enabled`` /
     ``campaign_job_active`` muncul SEBELUM akses ``cp.`` pertama di fungsi yang sama).
  3. Callback baru ``camp:`` (ack / ackall / gatecancel) tidak menabrak pola
     handler lain dan tidak menambah handler baru.
  4. Titik integrasi langkah-2 ada & berurutan benar (gerbang sebelum render).
  5. ``risk_flags`` selalu dibaca defensif (getattr), tidak pernah asumsi langsung.
  6. Helper fitur tidak boleh ``raise`` (fitur tak boleh menghentikan pekerjaan klip).
  7. Scope tulis: modul milik lane lain (campaign_policy.py / ai_analyzer.py)
     tidak mengandung simbol milik lane bot (cek konten + ``git status``).
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot.py"
UI = ROOT / "modules" / "bot_ui.py"
FOREIGN = [ROOT / "modules" / "campaign_policy.py", ROOT / "modules" / "ai_analyzer.py"]

try:  # konsol Windows cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

results: list[tuple[bool, str]] = []


def add(ok: bool, msg: str) -> None:
    results.append((bool(ok), msg))


src = BOT.read_text(encoding="utf-8")
tree = ast.parse(src, filename=str(BOT))

# ---------------------------------------------------------------------------
# 1. Kontrak nama cp.*
# ---------------------------------------------------------------------------
STEP1_CP = {
    "load_profile", "list_profiles", "validate_profile", "evaluate_moment",
    "evaluate_catalog", "check_caption", "aggregate_level", "campaign_intro_lines",
    "default_profile_id", "Verdict",
}
STEP2_CP = {
    "campaign_rules_text", "llm_flag_verdicts", "human_gates_for",
    "record_override", "read_overrides_tail",
}
ALLOWED_CP = STEP1_CP | STEP2_CP

cp_calls: dict[str, list[int]] = {}
for node in ast.walk(tree):
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "cp"
    ):
        cp_calls.setdefault(node.attr, []).append(node.lineno)

used = set(cp_calls)
extra = used - ALLOWED_CP
missing_step2 = STEP2_CP - used
add(not extra, f"tidak ada nama cp.* di luar kontrak (aneh: {sorted(extra)})")
add(not missing_step2, f"semua kontrak langkah-2 dipakai (kurang: {sorted(missing_step2)})")

# record_override WAJIB dipanggil posisi (8 arg wajib) + note keyword -> tahan
# terhadap pergantian nama parameter di lane A, dan tidak pernah dipanggil telanjang.
rec = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
       and isinstance(n.func, ast.Attribute) and n.func.attr == "record_override"]
add(bool(rec), f"cp.record_override dipanggil ({len(rec)} call site)")
add(all(len(c.args) >= 8 for c in rec), "record_override pakai arg posisional (bukan keyword baru)")
add(all(any(k.arg == "note" for k in c.keywords) for c in rec), "setiap record_override mengirim note audit")
oot = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
       and isinstance(n.func, ast.Attribute) and n.func.attr == "read_overrides_tail"]
add(all(any(k.arg == "n" for k in c.keywords) for c in oot), "read_overrides_tail dipanggil dengan n=")

# ---------------------------------------------------------------------------
# 2. Guard CAMPAIGN_OK di setiap fungsi yang menyentuh cp.*
# ---------------------------------------------------------------------------
GUARDS = ("campaign_enabled", "campaign_job_active", "campaign_pending_bound")


def guard_token_lines(body: ast.AST) -> list[int]:
    return [
        n.lineno for n in ast.walk(body)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in GUARDS
    ]


def cp_nodes(body: ast.AST) -> list[ast.Attribute]:
    return [
        n for n in ast.walk(body)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "cp"
    ]


def cp_lines(body: ast.AST) -> list[int]:
    return [n.lineno for n in cp_nodes(body)]


def try_wrapped_lines(body: ast.AST) -> set[int]:
    """Baris akses cp.* yang terlindung try/except Exception di fungsi yang sama."""
    covered: set[int] = set()
    for node in ast.walk(body):
        if isinstance(node, ast.Try) and any(
            h.type is None or (isinstance(h.type, ast.Name) and h.type.id in ("Exception", "AttributeError"))
            for h in node.handlers
        ):
            covered.update(n.lineno for n in ast.walk(node)
                           if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                           and n.value.id == "cp")
    return covered


def calls_guarded_helper_before(body: ast.AST, first_cp: int) -> bool:
    """Akses cp.* baru terjadi setelah helper campaign yang sendiri terjaga."""
    for node in ast.walk(body):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.startswith("campaign_")
            and node.func.id in GUARDED_TRANSITIVE
            and node.lineno < first_cp
        ):
            return True
    return False


funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
by_name = {n.name: n for n in funcs}
GUARDED_TRANSITIVE = {
    "campaign_get_profile", "campaign_profile_by_id", "campaign_list_profiles",
    "campaign_compute_verdicts", "campaign_gates_for_flags", "campaign_gate_map",
    "campaign_pending_gates", "campaign_worst_verdict",
}

unprotected: list[str] = []
protected: list[str] = []
for fn in funcs:
    cps = cp_lines(fn)
    if not cps:
        continue
    first = min(cps)
    direct = [g for g in guard_token_lines(fn) if g < first]
    wrapped = set(cp_lines(fn)) <= try_wrapped_lines(fn)
    transitive = calls_guarded_helper_before(fn, first)
    (protected if (direct or wrapped or transitive) else unprotected).append(fn.name)
add(not unprotected, f"semua pengguna langsung cp.* ter-guard (belum: {unprotected})")
add(len(protected) >= 14, f"{len(protected)} fungsi campaign terjaga: contoh {sorted(protected)[:4]}")

# fungsi langkah-2 baru harus lewat gerbang guard (langsung atau via helper terjaga)
STEP2_FUNCS = [
    "campaign_pending_rules_text", "campaign_merge_llm_flags",
    "campaign_compute_verdicts", "campaign_gates_for_flags", "campaign_gate_map",
    "campaign_pending_gates", "campaign_record_override",
    "campaign_overrides_tail_text", "campaign_moment_flags",
]
absent = [f for f in STEP2_FUNCS if f not in by_name]
add(not absent, f"fungsi baru langkah-2 ada semua{'' if not absent else ' HILANG: ' + str(absent)}")
for f in ("campaign_merge_llm_flags", "campaign_gates_for_flags", "campaign_record_override",
          "campaign_pending_rules_text", "campaign_overrides_tail_text",
          "campaign_compute_verdicts", "campaign_pending_gates", "campaign_gate_map"):
    if f in by_name:
        cps = cp_lines(by_name[f])
        gs = [g for g in guard_token_lines(by_name[f]) if g < (min(cps) if cps else 10 ** 9)]
        add(bool(gs) or not cps, f"`{f}` NO-OP tanpa campaign aktif (guard {'ada' if gs else 'TIDAK ADA'})")

# tidak boleh ada helper campaign yang jadi dead code setelah langkah 2
dead = []
for fn in funcs:
    if not fn.name.startswith(("campaign_", "build_campaign_", "ensure_campaign_", "run_generate_all")):
        continue
    refs = len(re.findall(rf"\b{re.escape(fn.name)}\b", src)) - 1
    if refs <= 0:
        dead.append(fn.name)
add(not dead, f"tidak ada helper campaign menganggur (dead: {dead})")

# setiap call site fitur baru juga harus di balik guard
add("if not campaign_job_active(job):" in src or "if campaign_job_active(job)" in src,
    "jalur verdict/gate di clip flow dicek campaign_job_active dulu")

# ---------------------------------------------------------------------------
# 3. Handler camp: tidak bertambah & tidak tabrakan
# ---------------------------------------------------------------------------
cb: list[str] = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CallbackQueryHandler":
        for kw in node.keywords:
            if kw.arg == "pattern" and isinstance(kw.value, ast.Constant):
                cb.append(str(kw.value.value))
add(len(cb) == 3 and "^camp:" in cb, f"3 handler callback, camp: terdaftar: {cb}")
clip_re, style_re = re.compile("^(clip:|done$)"), re.compile("^style:")
for probe in ("camp:ack:0", "camp:ack:11", "camp:ackall", "camp:gatecancel"):
    add(not clip_re.match(probe) and not style_re.match(probe) and probe.startswith("camp:"),
        f"callback `{probe}` hanya cocok ke handler camp:")

cb_handler = by_name.get("campaign_callback")
found: set[str] = set()
if cb_handler is not None:
    for node in ast.walk(cb_handler):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "action" or not node.comparators:
            continue
        comp = node.comparators[0]
        if isinstance(node.ops[0], ast.Eq) and isinstance(comp, ast.Constant):
            found.add(str(comp.value))
        elif isinstance(node.ops[0], ast.In) and isinstance(comp, (ast.Tuple, ast.List)):
            found.update(str(e.value) for e in comp.elts if isinstance(e, ast.Constant))
else:
    add(False, "campaign_callback ditemukan untuk pemetaan aksi")
for need in ("ack", "ackall", "gatecancel", "det", "show", "set", "job"):
    add(need in found, f"aksi camp:{need} ditangani")
# Langit-langit langkah-3: aksi 'import' (tombol 📥 menu) & 'imp' (camp:imp:*)
# adalah perluasan kontrak — kontrak langkah-2 lamanya sendiri tidak berubah.
add(found <= {"det", "show", "set", "job", "ack", "ackall", "gatecancel", "import", "imp"},
    f"aksi camp: hanya kontrak (+import/imp langkah-3): {sorted(found)}")

# tombol baru dibuat dengan data callback yang benar
add('callback_data=f"camp:ack:' in src, "tombol ack per-gate memakai camp:ack:<idx>")
add('callback_data="camp:ackall"' in src, "tombol 'aku mengerti semuanya' = camp:ackall")
add('callback_data="camp:gatecancel"' in src, "tombol batal = camp:gatecancel")
add(len("camp:ack:0") < 64, "callback_data ack di bawah limit 64 byte Telegram")

# ---------------------------------------------------------------------------
# 4. Titik integrasi + urutan
# ---------------------------------------------------------------------------
needles = {
    "injeksi rules (file)": "run_analyze, source_path, transcription, progress, camp_active, camp_rules",
    "injeksi rules (youtube)": "run_youtube_analyze, transcript, progress, camp_active, camp_rules",
    "rules text helper": "cp.campaign_rules_text(profile)",
    "kwargs adapter": 'kwargs["campaign_rules_text"] = campaign_rules_text',
    "signature-aware": "campaign_rules_text\" in params",
    "llm flags merge": "cp.llm_flag_verdicts(flags, profile)",
    # Lane C (jahit): bentuk argumen kontrak lane A final -> list of lists [clean];
    # introspeksi signature + percobaan ganda dihapus dari bot. Awalan "!" = harus ABSEN.
    "human gates": "cp.human_gates_for(profile, [clean])",
    "bentuk arg gate": "!campaign_gates_arg_nested",
    "audit tail": "cp.read_overrides_tail(cid, n=max(n, 1) + 2)",
    "ack note generate-all": '"ack_generate_all"',
    "ack note fail confirm": '"confirm_fail_override"',
    "curation note": "campaign_curation_note_text(profile)",
    "ack state camp_ack": 'CAMPAIGN_GATE_ITEMS_KEY = "camp_gate_pending"',
}
for label, needle in needles.items():
    if needle.startswith("!"):
        add(needle[1:] not in src, f"integrasi `{label}` ABS (bekas lama dihapus)")
    else:
        add(needle in src, f"integrasi `{label}` hadir")

# gerbang sebelum render: di CABANG `clip:all`, gate didahului sebelum lock/render
fn = by_name.get("clip_callback")
if fn is not None:
    body_src = ast.get_source_segment(src, fn) or ""
    mark = body_src.find('elif data == "clip:all"')
    add(mark != -1, "cabang `clip:all` ada di clip_callback")
    branch = body_src[mark:]
    i_gate = branch.find("campaign_pending_gates(")
    i_render = branch.find("run_generate_all_flow(")
    i_lock = branch.find("try_claim_lock(")
    add(i_gate != -1 and i_render != -1 and i_gate < i_render,
        "gerbang manusia dinilai SEBELUM render semua (lock/render)")
    add(i_gate != -1 and (i_lock == -1 or i_gate < i_lock),
        "tidak ada klaim lock sebelum gerbang generate-semua")
    add("campaign_send_gate_confirm(" in branch and "return" in branch[i_gate:i_render],
        "bila ada gate: kirim daftar konfirmasi lalu TAHAN (return sebelum render)")
    add("campaign_record_override(" in body_src, "jalur ❌ mencatat override saat user konfirmasi ya")
    add('if fail_note and not campaign_is_acked(job, idx):' in body_src,
        "konfirmasi ❌ tetap pakai state camp_ack lama (index klip)")
else:
    add(False, "clip_callback ditemukan")

gf = by_name.get("run_generate_all_flow")
add(gf is not None, "run_generate_all_flow diekstrak (dipakai clip:all & resume setelah ack)")
if gf is not None:
    gsrc = ast.get_source_segment(src, gf) or ""
    add("campaign_record_override" in gsrc and "ack_generate_all" in gsrc,
        "setiap klip ber-gate dicatat override-nya saat render batch")
    add("try_claim_lock()" in gsrc, "run_generate_all_flow tetap mengklaim lock global")
    add("job[\"rendering\"] = False" in gsrc and "finally:" in gsrc,
        "lock & flag rendering dilepas di finally")

rr = by_name.get("campaign_resume_generate_all_after_ack")
if rr is not None:
    rsrc = ast.get_source_segment(src, rr) or ""
    add("campaign_pending_gates" not in rsrc and "run_generate_all_flow" in rsrc,
        "resume setelah ack penuh langsung render (tidak mutar di gerbang)")

# badge katalog memakai verdict gabungan + pembeda teks sinyal AI
add("campaign_compute_verdicts(moments, segments, profile)" in src,
    "katalog & ensure_campaign_verdicts pakai satu sumber verdict gabungan")
add("campaign_verdict_is_soft(worst)" in src, "badge ⚠️ diberi penanda 'Sinyal AI' untuk flag LLM")
add("CAMPAIGN_SOFT_CODE_PREFIXES = (\"llm_flag:\", \"human_gate:\")" in src,
    "prefix kode lunak (llm_flag/human_gate) terdaftar")
add("campaign_demote_soft_fails" in src, "verdict lunak difail-kan -> diturunkan ke warn (AI tak pernah veto)")

# ---------------------------------------------------------------------------
# 5. risk_flags selalu defensif (AST: tidak ada attribute access langsung)
# ---------------------------------------------------------------------------
direct_rf = [
    n for n in ast.walk(tree)
    if isinstance(n, ast.Attribute) and n.attr == "risk_flags"
    and not (isinstance(n.value, ast.Name) and n.value.id in ("ClipMoment",))
]
getattr_rf = [
    n for n in ast.walk(tree)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr"
    and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value == "risk_flags"
]
add(bool(getattr_rf), "risk_flags dibaca via getattr")
add(not direct_rf, f"tidak ada akses langsung `<objek>.risk_flags` (ditemukan: {len(direct_rf)})")
add("inspect.signature(analyzer.analyze_segments)" in src,
    "campaign_rules_text hanya dikirim bila analyzer menerima parameternya")

# ---------------------------------------------------------------------------
# 6. Fitur baru tidak boleh raise
# ---------------------------------------------------------------------------
STEP2_ALL = STEP2_FUNCS + [
    "campaign_worst_verdict", "campaign_verdict_code", "campaign_verdict_reason",
    "campaign_verdict_is_soft", "campaign_demote_soft_fails", "campaign_clip_fail_codes",
    "campaign_ack_set", "campaign_is_acked", "campaign_mark_ack", "campaign_gate_confirm_text",
    "build_campaign_gate_ack_keyboard", "campaign_send_gate_confirm",
    "campaign_handle_gate_ack", "campaign_curation_note_text", "campaign_analyze_kwargs",
]
raisers: list[str] = []
for name in STEP2_ALL:
    fn = by_name.get(name)
    if fn is None:
        raisers.append(f"{name}(HILANG)")
        continue
    if any(isinstance(n, ast.Raise) for n in ast.walk(fn)):
        raisers.append(name)
add(not raisers, f"langkah-2 bebas raise (pelanggar: {raisers})")

no_handler = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.type is None]
add(True, f"kecuali total except-bare: {len(no_handler)} (informasi)")

# ---------------------------------------------------------------------------
# 7. Scope tulis: tidak ada konten lane bot di modul milik lane lain
# ---------------------------------------------------------------------------
BOT_MARKERS = (
    "InlineKeyboardButton", "callback_data", "query.answer", "context.bot",
    "campaign_gate(", "run_generate_all_flow", "build_clip_catalog_messages",
    "clip_callback", "active_job",
)
for path in FOREIGN:
    if not path.exists():
        add(True, f"{path.name} belum ada (lane lain)")
        continue
    txt = path.read_text(encoding="utf-8")
    leaked = [m for m in BOT_MARKERS if m in txt]
    add(not leaked, f"{path.name} bersih dari konten lane bot (bocor: {leaked})")
    digest = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:12]
    add(True, f"{path.name} sha256[:12]={digest} size={len(txt)} (milik lane lain, TIDAK ditulis lane ini)")

try:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                         capture_output=True, text=True, timeout=25).stdout
    lines = out.splitlines()
    touched = [l for l in lines if re.search(r"(bot\.py|modules/bot_ui\.py)$", l.replace("\\", "/"))]
    foreign = [l for l in lines if re.search(r"(campaign_policy|ai_analyzer)\.py$", l.replace("\\", "/"))]
    add(bool(touched), f"bot.py/bot_ui.py termodifikasi (workspace lane ini): {touched}")
    add(
        all(l[:2].strip() in ("??", "M", "AM", "MM") for l in foreign),
        f"campaign_policy/ai_analyzer hanya berstatus milik lane lain: {foreign}",
    )
except Exception as exc:  # pragma: no cover
    add(True, f"cek git status dilewati ({exc})")

# ---------------------------------------------------------------------------
# 8. bot_ui: kontrak campaign_line utuh (tidak diubah lane ini)
# ---------------------------------------------------------------------------
ui = UI.read_text(encoding="utf-8")
add("def format_clip_card(idx: int, moment: Any, campaign_line: str = \"\") -> str:" in ui,
    "bot_ui.format_clip_card tetap menerima campaign_line (badge ⚠️ cukup teks dari bot)")

# ---------------------------------------------------------------------------
print("=" * 72)
failed = 0
for ok, msg in results:
    print(("PASS  " if ok else "FAIL  ") + msg)
    failed += 0 if ok else 1
print("=" * 72)
print(f"{len(results) - failed}/{len(results)} pemeriksaan PASS")
sys.exit(1 if failed else 0)
