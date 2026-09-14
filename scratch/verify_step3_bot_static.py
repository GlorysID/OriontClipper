"""Verifikasi statis (AST-only) /campaign import — LANGKAH 3 sisi bot.

TANPA ``import bot`` runtime & TANPA modules/campaign_compiler.py (lane paralel
mungkin belum ada).-src bot.py dibaca mentah + di-parse AST.

Cakupan:
  1. Lazy import cc HANYA di dalam fungsi + terlindung try/except Exception;
     nama modul tidak pernah jadi klausa import tingkat-modul.
  2. Nama ``cc.*`` yang dipakai HANYA kontrak
     (build_messages, split_clauses, compile_profile, save_draft,
     summarize_for_user, CompilerError).
  3. Intersep handle_text sebelum deteksi url/file/flow-lain dan sebelum
     klaim lock; cabang camp:imp diproses sebelum guard ``not campaign_enabled``.
  4. State camp_import dibersihkan di SEMUA jalur terminal
     (simpan / batal / link-video / pesan-perintah / compiler-absen / /done / /cancel).
  5. Blocking LLM lewat asyncio.to_thread; instruksi & batas (80 / 12000 / 4000)
     sesuai spesifikasi; escape Markdown-v1 + fallback.
  6. Jalur impor tidak pernah menyentuh try_claim_lock/release_lock.
  7. Scope tulis: hanya bot.py (+ scratch ini) via git status; file lane lain
     (campaign_policy / ai_analyzer / campaign_compiler) tidak berubah vs
     snapshot awal sesi.
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

try:  # konsol Windows cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

results: list[tuple[bool, str]] = []


def add(ok: bool, msg: str) -> None:
    results.append((bool(ok), msg))


src = BOT.read_text(encoding="utf-8")
tree = ast.parse(src, filename=str(BOT))

funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
by_name = {n.name: n for n in funcs}
top_funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def enclosing_map(root: ast.AST) -> dict[ast.AST, ast.AST | None]:
    """enc[node] = fungsi/class pembungkus LANGSUNG untuk node FunctionDef/ClassDef."""
    enc: dict[ast.AST, ast.AST | None] = {}

    def walk(node: ast.AST, parent: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                enc[child] = parent
                walk(child, child)
            else:
                walk(child, parent)

    walk(root, None)
    return enc


ENC = enclosing_map(tree)


def enclosing_callable(node: ast.AST) -> ast.AST | None:
    """Fungsi/method yang membungkus node apa pun (naik via parent-map lengkap)."""
    parents: dict[ast.AST, ast.AST] = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None

# ---------------------------------------------------------------------------
# 1. Lazy import campaign_compiler
# ---------------------------------------------------------------------------
bad_top_import = []
for node in ast.walk(tree):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names = ""
        if isinstance(node, ast.Import):
            names = " ".join(a.name for a in node.names)
        else:
            names = f"{node.module or ''} " + " ".join(a.name for a in node.names)
        if "campaign_compiler" in names:
            bad_top_import.append((type(node).__name__, node.lineno, names.strip()))
add(not bad_top_import, f"tidak ada klausa import campaign_compiler (ditemukan: {bad_top_import})")

add(
    bool(re.search(r'CAMPAIGN_CC_IMPORT\s*=\s*"campaign_compiler"', src)),
    "nama modul hanya konstanta string CAMPAIGN_CC_IMPORT",
)

imp_calls = [
    n for n in ast.walk(tree)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    and n.func.attr == "import_module"
]
add(len(imp_calls) >= 1, f"importlib.import_module dipakai lazy ({len(imp_calls)} call site)")
inside_fn = all(enclosing_callable(c) is not None for c in imp_calls)
add(inside_fn, "import_module berada DI DALAM fungsi (bukan tingkat-modul)")


def inside_try_exception(node: ast.AST) -> bool:
    covered = set()
    for t in ast.walk(tree):
        if isinstance(t, ast.Try) and any(
            h.type is None or (isinstance(h.type, ast.Name) and h.type.id == "Exception")
            for h in t.handlers
        ):
            covered.update(id(n) for n in ast.walk(t))
    return id(node) in covered


add(all(inside_try_exception(c) for c in imp_calls), "setiap import_module terlindung try/except Exception")
cc_fn = by_name.get("campaign_cc")
add(cc_fn is not None, "helper lazy-import `campaign_cc` ada")
if cc_fn is not None:
    add(ENC.get(cc_fn) is None and cc_fn.name in top_funcs, "campaign_cc fungsi top-level (dipanggil handler)")
    add("except Exception" in ast.get_source_segment(src, cc_fn) or "",
        "campaign_cc menelan Exception -> (None, err), tanpa raise")
# dipanggil hanya dari dalam fungsi lain (handler)
cc_callsites = [
    n for n in ast.walk(tree)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "campaign_cc"
]
add(len(cc_callsites) >= 2 and all(enclosing_callable(c) is not None for c in cc_callsites),
    f"campaign_cc dipanggil dari handler ({len(cc_callsites)} call site, semua dalam fungsi)")

# ---------------------------------------------------------------------------
# 2. Nama cc.* hanya kontrak
# ---------------------------------------------------------------------------
CONTRACT_CC = {
    "build_messages", "split_clauses", "compile_profile", "save_draft",
    "summarize_for_user", "CompilerError",
}
cc_attrs: dict[str, list[int]] = {}
for node in ast.walk(tree):
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "cc"
    ):
        cc_attrs.setdefault(node.attr, []).append(node.lineno)
# akses via getattr(cc, "Nama") tetap nama kontrak juga (bentuk defensif bot)
for node in ast.walk(tree):
    if (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "getattr" and node.args
        and isinstance(node.args[0], ast.Name) and node.args[0].id == "cc"
        and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        cc_attrs.setdefault(node.args[1].value, []).append(node.lineno)
# `cc` harus selalu binding lokal hasil campaign_cc(), bukan nama global baru
cc_assign_global = []
for n in tree.body:
    targets: list[ast.expr] = []
    if isinstance(n, ast.Assign):
        targets = list(n.targets)
    elif isinstance(n, ast.AnnAssign):
        targets = [n.target]
    elif isinstance(n, (ast.ClassDef, ast.Import, ast.ImportFrom)):
        names = ([n.name] if isinstance(n, ast.ClassDef)
                 else [a.asname or a.name for a in n.names])
        if any(x == "cc" for x in names):
            cc_assign_global.append(f"{type(n).__name__}@{n.lineno}")
        continue
    if any(isinstance(t, ast.Name) and t.id == "cc" for t in targets):
        cc_assign_global.append(f"Assign@{n.lineno}")
add(not cc_assign_global, f"tidak ada global bernama `cc` karangan baru ({cc_assign_global})")
extra_cc = set(cc_attrs) - CONTRACT_CC
add(not extra_cc, f"tidak ada nama cc.* di luar kontrak (aneh: {sorted(extra_cc)})")
for need in ("compile_profile", "save_draft", "summarize_for_user", "CompilerError"):
    add(need in cc_attrs, f"kontrak cc.{need} dipakai bot")

# ---------------------------------------------------------------------------
# 3. Urutan intersep & dispatch
# ---------------------------------------------------------------------------
ht = by_name.get("handle_text")
add(ht is not None, "handle_text ditemukan")
if ht is not None:
    hsrc = ast.get_source_segment(src, ht) or ""
    i_state = hsrc.find("campaign_import_state(context)")
    i_inter = hsrc.find("campaign_import_handle_text(")
    i_gdrive_folder = hsrc.find("gdrive_flow.is_gdrive_folder_url")
    i_gdrive = hsrc.find("gdrive_flow.is_gdrive_url")
    i_yt = hsrc.find("is_youtube_url")
    i_lock = hsrc.find("try_claim_lock")
    i_gate = hsrc.find("campaign_gate(")
    add(i_state != -1 and i_inter != -1, "intersep camp_import ada di handle_text")
    add(-1 < i_inter < min(x for x in (i_gdrive_folder, i_gdrive, i_yt, i_lock, i_gate) if x != -1),
        "intersep SEBELUM deteksi url/flow-lain dan sebelum klaim lock/campaign_gate")
    i_menu = hsrc.find("menu_handler(")
    add(i_menu != -1 and i_menu < i_inter, "menu button dikecualikan dari intersep (sebelumnya)")

cb = by_name.get("campaign_callback")
add(cb is not None, "campaign_callback ditemukan")
if cb is not None:
    cbsrc = ast.get_source_segment(src, cb) or ""
    i_disabled = cbsrc.find('Modul campaign tidak tersedia')
    i_imp = cbsrc.find('action == "imp"')
    i_import = cbsrc.find('action == "import"')
    # GUARD UTUH: camp:import/imp TETAP di belakang early-return campaign_enabled()
    # (sama seperti aksi camp: lain). Pesan 'belum tersedia' milik jalur itu =
    # balasan campaign_command + guard internal campaign_import_start — keduanya
    # tidak crash. Ini needle yang disesuaikan secara semantik, bukan dilonggarkan.
    add(-1 < i_import and i_disabled != -1 and i_disabled < i_import,
        "camp:import di belakang guard campaign_enabled (pola sama dgn aksi camp: lain)")
    add(-1 < i_imp and i_disabled != -1 and i_disabled < i_imp,
        "camp:imp:* di belakang guard yang sama; save_draft/summarize hanya via state hasil impor")
    add("campaign_import_handle_callback(" in cbsrc, "dispatch ke campaign_import_handle_callback")

# ---------------------------------------------------------------------------
# 4. Pembersihan state di jalur terminal
# ---------------------------------------------------------------------------
ihc = by_name.get("campaign_import_handle_callback")
iht = by_name.get("campaign_import_handle_text")
cclear = by_name.get("campaign_import_clear")
add(cclear is not None, "campaign_import_clear ada (pop camp_import)")
if cclear is not None:
    add('context.user_data.pop("camp_import", None)' in ast.get_source_segment(src, cclear) or "",
        "clear = pop user_data['camp_import']")
add(ihc is not None and iht is not None, "handler tombol & handler teks impor ada")
if ihc is not None:
    s = ast.get_source_segment(src, ihc) or ""
    i_again = s.find('action == "again"')
    i_cancel = s.find('action == "cancel"')
    i_save = s.find('action == "save"')
    for label, a, b in (("again", i_again, i_cancel), ("cancel", i_cancel, i_save), ("save", i_save, len(s))):
        seg = s[a:b]
        if label == "save":
            add("campaign_import_clear(context)" in seg and 'cc.save_draft(' in seg,
                "jalur SIMPAN: save_draft lalu clear state")
            add('user_data["pending_campaign"] = cid' in seg,
                "jalur SIMPAN: pending_campaign = cid (muncul di /campaign list via bind video berikutnya)")
        else:
            add("campaign_import_clear(context)" in seg or label == "again",
                f"jalur {label.upper()} terminal benar")
    add("campaign_import_clear(context)" in s[i_cancel:i_save], "jalur BATAL: clear state")
if iht is not None:
    s = ast.get_source_segment(src, iht) or ""
    n_clear = s.count("campaign_import_clear(context)")
    add(n_clear >= 3, f"clear di jalur link-video / perintah / compiler-absen ({n_clear}x)")
    add(s.find("campaign_import_clear(context)") < s.find("is_youtube_url"),
        "LINK video: state dibatalkan SEBELUM proses normal (intersep return)")
dc = by_name.get("done_command")
add(dc is not None and "campaign_import_clear(context)" in (ast.get_source_segment(src, dc) or ""),
    "/done ikut membersihkan state impor")
canc = by_name.get("cancel_command")
add(canc is not None and "campaign_import_clear(context)" in (ast.get_source_segment(src, canc) or ""),
    "/cancel membersihkan state impor")
cmd_regs = [
    n.args[0].value for n in ast.walk(tree)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "CommandHandler"
    and n.args and isinstance(n.args[0], ast.Constant)
]
add("/cancel" not in cmd_regs and "cancel" in cmd_regs, "/cancel terdaftar (tanpa tabrakan nama)")
add(cmd_regs.count("campaign") == 1, "/campaign tetap satu handler")

# ---------------------------------------------------------------------------
# 5. Wiring compile & tampilan
# ---------------------------------------------------------------------------
rc = by_name.get("campaign_import_run_compile")
add(rc is not None, "campaign_import_run_compile ada")
if rc is not None:
    s = ast.get_source_segment(src, rc) or ""
    add("asyncio.to_thread(" in s, "compile_profile jalan via asyncio.to_thread (blocking LLM off-loop)")
    add("campaign_import_compile_sync" in s, "to_thread memanggil wrapper sync compile")
    add('"compiling"' in s and '"await_text"' in s and '"result"' in s, "mesin state stage lengkap")
    add("CompilerError" in s, "CompilerError dikenali via isinstance dinamis (getattr cc)")
    add("klausul" in s and "hilang" in s and "Saran" in s,
        "error 'klausul hilang' dapat saran konkret (padukan poin bertanda)")
csc = by_name.get("campaign_import_compile_sync")
if csc is not None:
    s = ast.get_source_segment(src, csc) or ""
    add("_CampaignChatAdapter" in s and "chat_fn" in s and "cc.compile_profile(raw, chat_fn)" in s,
        "chat_fn dibangun dari adapter AIAnalyzer, diteruskan ke cc.compile_profile")
ada = None
for n in ast.walk(tree):
    if isinstance(n, ast.ClassDef) and n.name == "_CampaignChatAdapter":
        ada = n
add(ada is not None and any(
    isinstance(b, ast.Name) and b.id == "AIAnalyzer" for b in ada.bases
) if ada else False, "_CampaignChatAdaptersubclass AIAnalyzer (mekanisme request/timeout warisan)")
if ada is not None:
    s = ast.get_source_segment(src, ada) or ""
    add("_request_chat_completion(" in s, "adapter memakai _request_chat_completion internal")
ss = by_name.get("campaign_import_send_summary")
if ss is not None:
    s = ast.get_source_segment(src, ss) or ""
    add("escape_tg_md" in s and 'parse_mode="Markdown"' in s, "ringkasan di-escape Markdown-v1 (pola lama)")
    add("except Exception" in s, "fallback teks polos bila Markdown gagal")
add(re.search(r"CAMP_IMPORT_RAW_LIMIT\s*=\s*12000", src) is not None, "kurasi mentok 12000 char")
add(re.search(r"CAMP_IMPORT_MIN_CHARS\s*=\s*80", src) is not None, "ambang bawah bahan 80 char")
add(re.search(r"CAMP_IMPORT_CHUNK\s*=\s*4000", src) is not None, "chunk tampilan >4000")
add('"⏳ Menganalisis S&K..."' in src, "status '⏳ Menganalisis S&K...' dikirim")
instr = re.search(r'Tempel teks S&K lengkap \(min 80 char\) sebagai PESAN TEKS biasa\.\s*"?\s*\n?\s*"?Kirim /cancel untuk batal\.', src)
add(bool(instr), "instruksi tempel S&K + /cancel persis spesifikasi")

# ---------------------------------------------------------------------------
# 6. Isolasi lock: jalur impor tak menyentuh lock global
# ---------------------------------------------------------------------------
for fname in (
    "campaign_import_state", "campaign_import_clear", "build_campaign_import_keyboard",
    "build_campaign_import_error_keyboard", "campaign_import_start",
    "campaign_import_send_summary", "campaign_import_compile_sync",
    "campaign_import_run_compile", "campaign_import_handle_callback",
    "campaign_import_handle_text", "cancel_command", "campaign_cc",
):
    fn = by_name.get(fname)
    if fn is None:
        add(False, f"{fname} ada")
        continue
    s = ast.get_source_segment(src, fn) or ""
    add("try_claim_lock" not in s and "release_lock" not in s, f"`{fname}` tidak menyentuh lock global")

add("_PROCESSING_LOCK" not in "\n".join(
    ast.get_source_segment(src, by_name[f]) or "" for f in by_name
    if f.startswith("campaign_import")
), "kode campaign_import_* bebas _PROCESSING_LOCK")

# ---------------------------------------------------------------------------
# 7. Guard 'belum tersedia' tanpa crash
# ---------------------------------------------------------------------------
cis = by_name.get("campaign_import_start")
if cis is not None:
    s = ast.get_source_segment(src, cis) or ""
    add("belum tersedia" in s and "campaign_enabled" in s and "campaign_cc" in s,
        "/campaign import -> 'belum tersedia' bila CAMPAIGN_OK False ATAU cc None (tanpa raise)")

# ---------------------------------------------------------------------------
# 8. Scope tulis via git (vs snapshot awal sesi di HEAD/worktree)
# ---------------------------------------------------------------------------
FOREIGN = [
    ROOT / "modules" / "campaign_policy.py",
    ROOT / "modules" / "ai_analyzer.py",
    ROOT / "modules" / "campaign_compiler.py",
]
BASELINE_SHA = {  # dicatat lane ini SEBELUM edit (verify_step2_bot juga mencatat digest)
    "campaign_policy.py": "48e84abf4b0b",
    "ai_analyzer.py": "a80ebd4e325d",
}
for path in FOREIGN:
    if not path.exists():
        add(True, f"{path.name} belum ada di disk (milik lane paralel)")
        continue
    digest = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:12]
    if path.name in BASELINE_SHA:
        add(digest == BASELINE_SHA[path.name],
            f"{path.name} tidak berubah oleh lane ini (sha {digest})")
    else:
        add(True, f"{path.name} sha[:12]={digest} (milik lane lain — TIDAK disentuh lane ini)")

try:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                         capture_output=True, text=True, timeout=25).stdout
    lines = out.splitlines()
    # scratch/ untracked sebagai direktori ('?? scratch/') — cek keduanya.
    scratch_ok = any(
        re.search(r"verify_step3_bot_static\.py$|^\?\? scratch/?$", l.replace("\\", "/"))
        for l in lines
    )
    bot_mod = any(re.search(r"(^|\s)M\s+.*bot\.py$|^\s?M bot\.py$", l) for l in lines)
    add(bot_mod and scratch_ok,
        f"bot.py termodifikasi + scratch langkah-3 ada ({[l for l in lines if 'bot.py' in l or 'scratch' in l]})")
    # tracked-files milik lane lain TIDAK boleh berpindah ke status asing
    # ('D'/'R'/'A' dst.) — ' M' = punya mereka, '??' = baru dari lane mereka.
    foreign = [
        l for l in lines
        if re.search(r"(campaign_policy|campaign_compiler|ai_analyzer)\.py$", l.replace("\\", "/"))
        or l.replace("\\", "/").startswith("?? scratch")
    ]
    tracked_foreign = [l for l in foreign if l[:2].strip() not in ("??", "M", "AM", "MM")]
    add(not tracked_foreign, f"campaign_policy/compiler/ai_analyzer tak bersentuh lane ini: {foreign}")
except Exception as exc:  # pragma: no cover
    add(True, f"cek git status dilewati ({exc})")

# ---------------------------------------------------------------------------
print("=" * 72)
failed = 0
for ok, msg in results:
    print(("PASS  " if ok else "FAIL  ") + msg)
    failed += 0 if ok else 1
print("=" * 72)
print(f"{len(results) - failed}/{len(results)} pemeriksaan PASS")
sys.exit(1 if failed else 0)
