"""Integrasi Campaign Compliance (bot.py <-> campaign_policy.py) — TANPA `import bot`.

ATURAN BESI (sejak insiden sesi fixer menggantung):
  1. Script scratch TIDAK PERNAH `import bot` / `from bot import ...`.
  2. Setiap subprocess wajib memberi `timeout=`.
Ganti posisi: kode bot.py dibaca lewat AST, dan helper murni bot dijalankan dari
"SILICE SUMBER" (def bot diekstrak dari AST-nya sendiri lalu di-exec di namespace
terkendali) — jadi perilaku tetap diuji tanpa pernah memuat telegram/main().

Yang tetap runtime-murni: modules.campaign_policy, modules.ai_analyzer, config.
Tidak ada network: socket + httpx/requests dipasangi tripwire.

Jalan:  .venv\\Scripts\\python.exe scratch\\verify_campaign_integration.py

Cakupan:
  0  kebersihan harness (larangan import bot, timeout subprocess) + tripwire
  1  matriks level 6 momen sintetis (profil graeme) — cp murni
  2  toleransi VTT-flow: segments TANPA `words` (objek & dict) -> verdict identik
  3  determinism 2x
  4  bentuk argumen kontrak langkah-2 (cp murni)
  5  campaign_active -> validate_moments TIDAK menyuntik hashtag generik
  6  call site analisis bot (AST statis) + adapter kwargs diuji vs AIAnalyzer asli
  7  helper render bot (silice sumber): caption note, gerbang, audit override
  8  perutean langkah-2 bot (AST): rules -> AI, risk_flags -> rebase, gerbang -> render
  9  kontrak rekonsiliasi lintas lane (Verdict.code, evidence, KeyError/ValueError)
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import logging
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # konsol Windows cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
# Tripwire jaringan (dipasang SEBELUL apapun dijalankan, dan tidak pernah
# butuh bot.py: seluruh jalur Telegram memang tidak dimuat di file ini).
# ---------------------------------------------------------------------------
NET_ATTEMPTS: list[str] = []


def _blocked(*args: Any, **kwargs: Any) -> Any:
    NET_ATTEMPTS.append(f"connect: {args[:2]}")
    raise AssertionError("AKSES JARINGAN DIBLOKIR oleh verify_campaign_integration")


_real_create_conn = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo
socket.create_connection = _blocked  # type: ignore[assignment]
socket.getaddrinfo = _blocked  # type: ignore[assignment]
_real_httpx: list[tuple[Any, str, Any]] = []
try:
    import httpx

    for _cls_name in ("Client", "AsyncClient"):
        _cls = getattr(httpx, _cls_name, None)
        if _cls is not None and hasattr(_cls, "send"):
            _real_httpx.append((_cls, "send", _cls.send))
            setattr(_cls, "send", lambda self, *a, **k: _blocked("httpx.send", a[:1]))
except Exception:
    pass
_real_req = None
try:
    import requests

    _real_req = requests.Session.request
    requests.Session.request = lambda self, *a, **k: _blocked("requests", a[:2])  # type: ignore[assignment]
except Exception:
    pass


def _no_net() -> None:
    check("nol usaha koneksi jaringan selama uji", not NET_ATTEMPTS, str(NET_ATTEMPTS[:3]))


try:
    socket.create_connection(("api.telegram.org", 443), timeout=1)
    _trip = False
except AssertionError:
    _trip = True
except Exception:
    _trip = False
check("tripwire jaringan efektif (percobaan connect memang diblokir)", _trip)
NET_ATTEMPTS.clear()


# ---------------------------------------------------------------------------
# 0. Kebersihan harness — aturan besi "jangan pernah import bot"
# ---------------------------------------------------------------------------
section("0. kebersihan harness (tanpa import bot)")

BOT_PATH = ROOT / "bot.py"
src_bot = BOT_PATH.read_text(encoding="utf-8")
bot_tree = ast.parse(src_bot, filename=str(BOT_PATH))


def imported_module_names(path: Path) -> tuple[set[str], set[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            if n.level == 0 and n.module:
                mods.add(n.module.split(".")[0])
    # Aturan besi: setiap pemanggilan SUBPROCESS wajib timeout. `asyncio.run`
    # bukan subprocess -> jangan ikut kena (detector dulu terlalu lebar).
    sub_names = {
        a.name.split(".")[0]
        for n in ast.walk(tree) if isinstance(n, ast.Import)
        for a in n.names if a.name.split(".")[0] == "subprocess"
    }
    sub_alias = {
        (a.asname or a.name)
        for n in ast.walk(tree) if isinstance(n, ast.Import)
        for a in n.names if a.name == "subprocess"
    }
    sub_attrs = {
        (a.asname or a.name)
        for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "subprocess"
        for a in n.names
    }
    subs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
             and n.func.value.id in (sub_names | sub_alias | {"subprocess"}))
            or (isinstance(n.func, ast.Name) and n.func.id in sub_attrs)
        )
    ]
    return mods, {n.lineno for n in subs if not any(k.arg == "timeout" for k in n.keywords)}


verifiers = sorted((ROOT / "scratch").glob("verify_*.py"))
for path in verifiers:
    mods, notimeout = imported_module_names(path)
    check(f"{path.name} tidak meng-import bot", "bot" not in mods, str(sorted(mods & {"bot"})))
    check(f"{path.name} subprocess selalu pakai timeout", not notimeout, str(sorted(notimeout)))
check("file ini termasuk yang diperiksa", (ROOT / "scratch" / "verify_campaign_integration.py") in verifiers)

module_level_net = [
    ln for ln in src_bot.splitlines()
    if ln and not ln[0].isspace()
    and ("HTTPXRequest(" in ln or "Application.builder" in ln or "run_polling" in ln)
]
check("bot.py tetap tanpa koneksi Telegram di tingkat modul", not module_level_net, str(module_level_net))


# ---------------------------------------------------------------------------
# Sandbox profil: salin seed ke tmp. data/campaigns asli TIDAK PERNAH ditulis.
# ---------------------------------------------------------------------------
import config  # noqa: E402
from modules import campaign_policy as cp  # noqa: E402
from modules.ai_analyzer import AIAnalyzer, ClipMoment, validate_moments  # noqa: E402
from modules.transcriber import TranscriptSegment  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="orion_campaign_verify_"))
shutil.copytree(config.CAMPAIGNS_DIR, TMP / "campaigns")
CAMPS = TMP / "campaigns"
config.CAMPAIGNS_DIR = CAMPS  # cp._campaigns_dir() membacanya saat panggilan

EVIL = {
    "schema_version": 2,
    "id": "renyah-test",
    "name": "Renyah [Uji] _Escape_",
    "strictness": "standard",
    "profile_version": 3,
    "source_doc": "SK_v2.pdf",
    "clip_rules": {
        "duration_s": {"min": 10, "max": 45},
        "banned_exact": ["rokok_vape_promo"],
        "banned_topics": [
            {"topic": "rokok/vape", "regex": ["\\brokok\\b", "\\bvape\\b"]},
        ],
        "curation_note": "Utamakan segmen *webinar* yang menjelaskan angka.",
        "flag_requests": [{"id": "multi_speaker", "ask": "Apakah penutur bukan Graeme dominan?"}],
        "human_gates": [
            {"id": "graeme_majority", "text": "Konfirmasi penutur dominan.",
             "trigger": "llm_flag:multi_speaker", "strict_mode": "ack_always"},
            {"id": "style_fit", "text": "Cek kecocokan gaya.", "trigger": "always", "strict_mode": "warn"},
        ],
    },
    "post_rules": {
        "caption_required": {"all_of": ["#tag_wajib_x"], "any_of": ["link_di_bio", "promo*kode"]},
        "platforms": {"tiktok": {"append": ["@akun_resmi"]}},
    },
    "account_items": ["Bio harus mencantumkan *nama brand*"],
    "out_of_control": ["Views minimal 10k"],
}
cp.save_profile(EVIL)

GRAEME_ID = "graeme-clipinfluence"
BEBAS_ID = "bebas"
graeme = cp.load_profile(GRAEME_ID)
bebas = cp.load_profile(BEBAS_ID)
renyah = cp.load_profile("renyah-test")

check("seed graeme & bebas valid di sandbox", not cp.validate_profile(graeme) and not cp.validate_profile(bebas))
check("profil uji langkah-2 (gates+flags) valid", not cp.validate_profile(renyah), str(cp.validate_profile(renyah)))
check("CAMPAIGN_FREE_ID bot == default_profile_id cp",
      'CAMPAIGN_FREE_ID = "bebas"' in src_bot and cp.default_profile_id() == "bebas")


# ---------------------------------------------------------------------------
# Fixture transkrip (W = Whisper words, V = VTT object, D = dict cache)
# ---------------------------------------------------------------------------
SEG_TEXTS = [
    (0.0, 61.0, "halo semua selamat datang kembali di kanal ini ya teman teman"),
    (61.0, 120.0, "kali ini kita bahas soal durasi klip dan cara memotong yang benar"),
    (120.0, 160.0, "banyak yang tanya soal onlyfans dan konten dewasa jawabannya tidak"),
    (160.0, 200.0, "untung aku nggak rokok dan nggak vape sama sekali hari ini"),
    (200.0, 240.0, "ini promo rokok_vape_promo resmi dari sponsor acara kita"),
]


def segs_words() -> list[dict[str, Any]]:
    return [
        {
            "start": s,
            "end": e,
            "text": t,
            "words": [{"word": w, "start": s + i * 0.4, "end": s + i * 0.4 + 0.3}
                      for i, w in enumerate(t.split())],
        }
        for s, e, t in SEG_TEXTS
    ]


def segs_vtt() -> list[TranscriptSegment]:
    return [TranscriptSegment(s, e, t) for s, e, t in SEG_TEXTS]


def segs_dict_nowords() -> list[dict[str, Any]]:
    return [{"start": s, "end": e, "text": t} for s, e, t in SEG_TEXTS]


def moments() -> list[ClipMoment]:
    return [
        ClipMoment(start=0, end=40, hook="KLIP BERSIH", topic="perkenalan", caption=""),
        ClipMoment(start=0, end=61, hook="KEPANJANGAN", topic="durasi", caption=""),
        ClipMoment(start=160, end=190, hook="ROKOK NEGANI", topic="gaya hidup", caption=""),
        ClipMoment(start=120, end=150, hook="OF", topic="tanya jawab", caption=""),
        ClipMoment(start=0, end=45, hook="CAP KURANG", topic="podcast",
                   caption="Tonton full episode di kanal ya"),
        ClipMoment(start=0, end=45, hook="CAP LENGKAP", topic="podcast",
                   caption="Percakapan lama #Graemeholm link in bio"),
    ]


EXPECTED = {1: "pass", 2: "fail", 3: "pass", 4: "warn", 5: "warn", 6: "pass"}


def agg(pairs: list[list[cp.Verdict]], i: int) -> str:
    return cp.aggregate_level(pairs[i - 1])


def sig(pairs: list[list[cp.Verdict]]) -> list[list[tuple]]:
    return [[(v.level, v.code, v.message, repr(v.evidence)) for v in vs] for vs in pairs]


section("1. matriks level 6 momen (profil graeme, whisper-style segments)")
ms = moments()
res_a = cp.evaluate_catalog(ms, segs_words(), graeme)
for i, want in EXPECTED.items():
    got = agg(res_a, i)
    check(f"momen #{i} agregat = {want}", got == want, f"dapat {got} :: {sig(res_a)[i - 1]}")
check("rule kosong (banned_exact graeme) tidak menghasilkan verdict",
      not any(v.code == "banned_exact" for vs in res_a for v in vs))
check("topik sensitif tidak pernah fail (grahaeme standard)",
      all(v.level != "fail" for vs in res_a for v in vs if v.code == "banned_topics"))
check("caption_required hanya hadir untuk momen ber-caption preview (5 & 6)",
      [i for i, vs in enumerate(res_a, 1) if any(v.code == "caption_required" for v in vs)] == [5, 6])

section("2. toleransi VTT-flow (segments TANPA words)")
res_v = cp.evaluate_catalog(ms, segs_vtt(), graeme)
res_d = cp.evaluate_catalog(ms, segs_dict_nowords(), graeme)
check("VTT objects (words=None) -> verdict identik dengan Whisper", sig(res_v) == sig(res_a))
check("dict segments tanpa kunci words -> identik", sig(res_d) == sig(res_a))
check("semua agregat tetap sesuai matriks pada jalur VTT",
      all(agg(res_v, i) == EXPECTED[i] for i in EXPECTED))
check("slice per [start,end] benar: momen #4 hanya membaca cue onlyfans",
      any("onlyfans" in (v.evidence or "") for v in res_v[3] if v.code == "banned_topics"),
      str(sig(res_v)[3]))
check("cue yang menyentuh batas akhir tidak ikut tersliced (momen #2, 0-61)",
      all("durasi klip" not in str(v.evidence or "") for v in res_v[1]), str(sig(res_v)[1]))

loose = copy.deepcopy(graeme)
loose["strictness"] = "loose"
strict = copy.deepcopy(graeme)
strict["strictness"] = "strict"
res_l = cp.evaluate_catalog(ms, segs_vtt(), loose)
res_s = cp.evaluate_catalog(ms, segs_vtt(), strict)
check("durasi 61s loose -> info (bukan fail)",
      [v.level for v in res_l[1] if v.code == "duration_range"] == ["info"])
check("durasi 61s strict -> fail",
      [v.level for v in res_s[1] if v.code == "duration_range"] == ["fail"])
check("banned_topics tidak pernah fail di ketiga strictness",
      all(all(v.level != "fail" for vs in r for v in vs if v.code == "banned_topics")
          for r in (res_a, res_l, res_s)))

section("3. determinism")
res_b = cp.evaluate_catalog(ms, segs_vtt(), graeme)
check("evaluate_catalog 2 run identik (VTT)", sig(res_b) == sig(res_v))
check("check_caption deterministik", sig([cp.check_caption("Tonton full episode", graeme)])
      == sig([cp.check_caption("Tonton full episode", graeme)]))
check("campaign_rules_text deterministik",
      cp.campaign_rules_text(renyah) == cp.campaign_rules_text(renyah)
      and cp.campaign_rules_text(bebas) == "")

section("4. negasi & banned_exact pada profil uji (semantik terdokumentasi)")
neg_ms = [
    ClipMoment(start=160, end=190, hook="NEGASI", topic="x", caption=""),
    ClipMoment(start=200, end=235, hook="EXACT", topic="x", caption=""),
]
res_r = cp.evaluate_catalog(neg_ms, segs_vtt(), renyah)
check("negasi 'nggak rokok' -> topics warn, BUKAN fail",
      agg(res_r, 1) == "warn" and all(v.level != "fail" for v in res_r[0]), str(sig(res_r)[0]))
check("banned_exact keras tetap fail meski terkurung negasi sekitar",
      [v.level for v in res_r[1] if v.code == "banned_exact"] == ["fail"], str(sig(res_r)[1]))

section("4b. bentuk argumen kontrak langkah-2 (cp murni)")
gated = cp.human_gates_for(renyah, [["multi_speaker"]])
check("human_gates_for(profile, [[flag]]) -> list of lists diterima",
      [v.code for v in gated] == ["human_gate:graeme_majority", "human_gate:style_fit"], str([v.code for v in gated]))
check("bentuk flag datar juga ditoleransi (tidak double-fire)",
      len(cp.human_gates_for(renyah, ["multi_speaker"])) == len(gated))
check("llm_flag_verdicts flag tak dikenal tidak pernah fail",
      all(v.level != "fail" for v in cp.llm_flag_verdicts(["hantu"], renyah)))

section("5. campaign_active -> tidak ada hashtag generik (lane AI)")
raw = [{"start": 5, "end": 35, "hook": "HOOK SATU", "topic": "topik", "viral_score": 90}]
GENERIC = ("#trending", "#shorts", "#viral", "#fyp")
cap_default = validate_moments(raw, 200.0)[0].caption
cap_campaign = validate_moments(raw, 200.0, campaign_active=True)[0].caption
check("default (campaign_active False) tetap suntik #trending #shorts #viral #fyp",
      all(g in cap_default for g in GENERIC), repr(cap_default))
check("campaign_active=True -> caption fallback TANPA hashtag generik",
      not any(g in cap_campaign for g in GENERIC), repr(cap_campaign))
check("campaign_active=True -> fallback caption = hook", cap_campaign == "HOOK SATU")
check("campaign_active tetap tak mengubah caption yang disediakan AI",
      validate_moments([{"start": 5, "end": 35, "hook": "H", "caption": "-hook- #tag_wajib_x"}],
                       200.0, campaign_active=True)[0].caption == "-hook- #tag_wajib_x")
check("risk_flags utuh dari JSON AI -> ClipMoment",
      validate_moments([{"start": 5, "end": 35, "hook": "H", "risk_flags": ["Multi Speaker"]}], 200.0)[0].risk_flags
      == ["multi_speaker"])


# ---------------------------------------------------------------------------
# Silice sumber bot: ekstrak def dari AST bot.py, exec tanpa menyentuh telegram
# ---------------------------------------------------------------------------
class _Anything:
    """Dummy pengaman; kalau muncul di jalur yang kita jalankan -> uji gagal."""

    def __init__(self, name: str = "?") -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover
        return f"<dummy {self._name}>"

    def __getattr__(self, item: str) -> "_Anything":
        return _Anything(f"{self._name}.{item}")

    def __call__(self, *a: Any, **k: Any) -> "_Anything":
        return _Anything(f"{self._name}()")


BOT_DEFS: dict[str, ast.stmt] = {
    n.name: n for n in bot_tree.body  # type: ignore[attr-defined]
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
}
# Konstanta modul boleh ikut, KECUALI yang nilainya kita kendalikan dari modul asli
# (kalau tidak, CAMPAIGN_OK = False dari baris 57 akan menimpa injeksi True).
CONST_DENY = {"cp", "CAMPAIGN_OK", "CAMPAIGN_FREE_ID", "_CP_IMPORT_ERR", "LOGGER", "config"}
BOT_CONSTS: dict[str, ast.stmt] = {
    t.id: n
    for n in bot_tree.body
    if isinstance(n, ast.Assign)
    for t in n.targets
    if isinstance(t, ast.Name) and t.id not in CONST_DENY
}


def names_used(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
    return out


DUMMIES: set[str] = set()


def load_bot_slice(roots: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compile defs top-level bot.py + dependensinya ke namespace terkendali."""
    ns: dict[str, Any] = {
        "__name__": "bot_campaign_slice",
        "re": re, "json": json, "inspect": inspect, "dataclasses": __import__("dataclasses"),
        "logging": logging, "config": config, "cp": cp, "Path": Path, "Any": Any,
        "functools": __import__("functools"), "partial": __import__("functools").partial,
        "CAMPAIGN_OK": True, "CAMPAIGN_FREE_ID": cp.default_profile_id(),
        "ClipMoment": ClipMoment,
        "LOGGER": logging.getLogger("bot.slice"),
    }
    ns["LOGGER"].setLevel(logging.CRITICAL)
    ns.update(extra or {})
    queue = list(roots)
    picked: dict[str, ast.stmt] = {}
    while queue:
        name = queue.pop()
        node = BOT_DEFS.get(name) or BOT_CONSTS.get(name)
        if node is None or name in picked:
            continue
        picked[name] = node
        queue.extend(sorted(names_used(node) - set(picked)))
    for name in roots:
        if name not in picked:
            raise AssertionError(f"def bot '{name}' tidak ditemukan di tingkat modul")
    mod = ast.Module(body=list(picked.values()), type_ignores=[])
    ast.fix_missing_locations(mod)
    code = compile(mod, "bot_campaign_slice.py", "exec")
    for _ in range(80):
        try:
            exec(code, ns)  # noqa: S102 - sumbernya bot.py, dibaca dari AST-nya sendiri
            break
        except NameError as exc:
            missing = getattr(exc, "name", None)
            if not missing or missing in ns:
                raise
            ns[missing] = _Anything(missing)
            DUMMIES.add(missing)
    else:
        raise AssertionError("silice bot tidak konvergen (NameError berulang)")
    return ns


section("6. call site analisis bot.py (AST statis)")

CALLS = [n for n in ast.walk(bot_tree) if isinstance(n, ast.Call)]
FUNC_BY_NAME = {n.name: n for n in ast.walk(bot_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def pos_params(fn: ast.AST) -> list[str]:
    args = getattr(fn, "args", None)
    if not isinstance(args, ast.arguments):
        return []
    return [a.arg for a in args.args]


def partial_call(target: str) -> ast.Call | None:
    for c in CALLS:
        if isinstance(c.func, ast.Name) and c.func.id == "partial" and c.args:
            first = c.args[0]
            if isinstance(first, ast.Name) and first.id == target:
                return c
    return None


for target in ("run_analyze", "run_youtube_analyze"):
    fn = FUNC_BY_NAME.get(target)
    pc = partial_call(target)
    params = pos_params(fn) if fn else []
    passed = [a.id for a in pc.args[1:] if isinstance(a, ast.Name)] if pc else []
    check(f"{target}: parameter kampanye ada & berurutan benar",
          "campaign_active" in params and "campaign_rules_text" in params
          and params.index("campaign_active") < params.index("campaign_rules_text"), str(params))
    check(f"{target}: partial posisional penuh (jumlah arg == jumlah param, tanpa kwarg)",
          bool(pc) and len(passed) == len(params) and not pc.keywords, f"{passed} vs {params}")
    for pname, argname in {"campaign_active": "camp_active", "campaign_rules_text": "camp_rules"}.items():
        ok = bool(pc) and pname in params and len(passed) == len(params) and passed[params.index(pname)] == argname
        check(f"{target}: arg posisi utk `{pname}` == `{argname}`", ok, f"{passed} vs {params}")
    check(f"{target}: argumen non-kampanye diteruskan dengan nama yang sama",
          bool(pc) and len(passed) == len(params)
          and all(p == a for p, a in zip(params, passed) if not p.startswith("campaign_")),
          f"{params} vs {passed}")

check("needle call site (file) sesuai bot.py hari ini",
      src_bot.count("partial(\n                run_analyze, source_path, transcription, progress, camp_active, camp_rules,") == 1
      or src_bot.count("run_analyze, source_path, transcription, progress, camp_active, camp_rules") == 1)
check("needle call site (youtube) sesuai bot.py hari ini",
      src_bot.count("partial(run_youtube_analyze, transcript, progress, camp_active, camp_rules)") == 1)
check("camp_active & camp_rules dihitung sebelum partial di kedua flow",
      src_bot.count("camp_active = campaign_pending_bound(context)") == 2
      and src_bot.count("camp_rules = campaign_pending_rules_text(context)") == 2)
check("camp_rules dibaca SEBELUM partial di tiap flow (camp_active -> camp_rules -> partial)",
      all(src_bot.find("camp_rules = campaign_pending_rules_text(context)") < src_bot.find(marker)
          for marker in ("run_analyze, source_path, transcription, progress, camp_active, camp_rules",
                         "partial(run_youtube_analyze, transcript, progress, camp_active, camp_rules)")))

for target in ("run_analyze", "run_youtube_analyze"):
    fn = FUNC_BY_NAME.get(target)
    body = (ast.get_source_segment(src_bot, fn) or "") if fn else ""
    # `**campaign_analyze_kwargs(...)` => ast.keyword(arg=None, value=Call(...))
    has_unpack = bool(fn) and any(
        isinstance(n, ast.keyword) and n.arg is None and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name) and n.value.func.id == "campaign_analyze_kwargs"
        for n in ast.walk(fn)
    )
    passes_args = bool(fn) and any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "campaign_analyze_kwargs"
        and [getattr(a, "id", "") for a in n.args] == ["analyzer", "campaign_active", "campaign_rules_text"]
        for n in ast.walk(fn)
    )
    check(f"{target}: meneruskan campaign_active & rules lewat campaign_analyze_kwargs",
          has_unpack and "analyzer.analyze_segments(" in body and passes_args,
          f"unpack={has_unpack} call={'analyze_segments' in body} args={passes_args}")


# needle teks lama yang kini salah (tanpa camp_rules) tidak boleh dipakai lagi
check("needle lama (tanpa camp_rules) sudah tidak ada di bot.py",
      "partial(run_analyze, source_path, transcription, progress, camp_active)" not in src_bot
      and "partial(run_youtube_analyze, transcript, progress, camp_active)" not in src_bot)

section("6b. adapter kwargs bot diuji terhadap AIAnalyzer ASLI (silice sumber)")
try:
    slice_ns = load_bot_slice([
        "campaign_analyze_kwargs", "campaign_pending_rules_text", "campaign_pending_bound",
        "campaign_get_profile", "campaign_job_active", "campaign_enabled", "consume_campaign_binding",
    ])
    check("silice analisis termuat tanpa dummy", not DUMMIES, str(sorted(DUMMIES)))
    camak = slice_ns["campaign_analyze_kwargs"]
    sig_real = inspect.signature(AIAnalyzer.analyze_segments).parameters
    check("AIAnalyzer asli menerima campaign_active & campaign_rules_text",
          {"campaign_active", "campaign_rules_text"} <= set(sig_real), str(list(sig_real)))
    kw = camak(AIAnalyzer(api_key=""), True, "ATURAN CAMPAIGN — uji")
    check("adapter: analyzer nyata -> kedua kwargs terkirim",
          kw.get("campaign_active") is True and kw.get("campaign_rules_text") == "ATURAN CAMPAIGN — uji", str(kw))
    kw_off = camak(AIAnalyzer(api_key=""), False, "")
    check("adapter: rules kosong -> hanya campaign_active (golden path utuh)",
          set(kw_off) == {"campaign_active"} and kw_off["campaign_active"] is False, str(kw_off))

    class LegacyStub:
        def analyze_segments(self, segments, video_duration_s, campaign_active=False):
            return []

    kw_legacy = camak(LegacyStub(), True, "ATURAN")
    check("adapter: analyzer tanpa param baru -> tidak crash, aturan dilewati",
          set(kw_legacy) == {"campaign_active"}, str(kw_legacy))

    class Ctx:
        def __init__(self, user_data): self.user_data = user_data

    ctx = Ctx({"pending_campaign": GRAEME_ID})
    cid, cver = slice_ns["consume_campaign_binding"](ctx)
    check("consume_campaign_binding snapshot id + profile_version",
          cid == GRAEME_ID and cver == graeme.get("profile_version") and "pending_campaign" not in ctx.user_data,
          f"{cid} {cver}")
    check("campaign_pending_bound read-only (tidak mengkonsumsi pending)",
          slice_ns["campaign_pending_bound"](Ctx({"pending_campaign": GRAEME_ID})) is True)
    check("campaign_pending_bound False untuk bebas",
          slice_ns["campaign_pending_bound"](Ctx({"pending_campaign": BEBAS_ID})) is False)
    check("campaign_pending_bound False tanpa pending",
          slice_ns["campaign_pending_bound"](Ctx({})) is False)
    check("campaign_pending_bound True konservatif untuk id hantu",
          slice_ns["campaign_pending_bound"](Ctx({"pending_campaign": "profil-hantu-x"})) is True)
    rules_txt = slice_ns["campaign_pending_rules_text"](Ctx({"pending_campaign": GRAEME_ID}))
    check("campaign_pending_rules_text == cp.campaign_rules_text(profil)",
          rules_txt == cp.campaign_rules_text(graeme) and "ATURAN CAMPAIGN" in rules_txt)
    check("campaign_pending_rules_text kosong untuk bebas",
          slice_ns["campaign_pending_rules_text"](Ctx({"pending_campaign": BEBAS_ID})) == "")
    check("campaign_job_active True graeme / False bebas / False None",
          slice_ns["campaign_job_active"]({"campaign_id": GRAEME_ID}) is True
          and slice_ns["campaign_job_active"]({"campaign_id": BEBAS_ID}) is False
          and slice_ns["campaign_job_active"](None) is False)
    check("silice 6b tidak menyentuh dummy", not DUMMIES, str(sorted(DUMMIES)))
except Exception as exc:  # noqa: BLE001
    check("silice analisis termuat", False, repr(exc))


# ---------------------------------------------------------------------------
# 7. Helper render bot lewat silice sumber
# ---------------------------------------------------------------------------
DUMMIES.clear()
section("7. helper render & gerbang bot (silice sumber)")
try:
    ns = load_bot_slice([
        "campaign_caption_note", "campaign_fail_warning_text", "campaign_short", "campaign_item_text",
        "campaign_binding_note", "campaign_intro_text", "campaign_reminders_text",
        "campaign_curation_note_text", "campaign_profile_details_text", "campaign_profile_by_id",
        "campaign_list_profiles", "campaign_compute_verdicts", "ensure_campaign_verdicts",
        "campaign_worst_verdict", "campaign_verdict_code", "campaign_verdict_reason",
        "campaign_verdict_is_soft", "campaign_demote_soft_fails", "campaign_moment_flags",
        "campaign_merge_llm_flags", "campaign_gates_for_flags", "campaign_gate_map",
        "campaign_pending_gates", "campaign_record_override", "campaign_overrides_tail_text",
        "campaign_ack_set", "campaign_is_acked", "campaign_mark_ack", "escape_tg_md",
    ])
    check("silice render termuat tanpa dummy", not DUMMIES, str(sorted(DUMMIES)))

    job_g = {
        "type": "file", "campaign_id": GRAEME_ID, "campaign_version": graeme.get("profile_version"),
        "moments": ms, "transcription": None, "generated": set(),
    }
    job_g["transcription"] = type("T", (), {"segments": segs_vtt()})()
    job_bebas = dict(job_g, campaign_id=BEBAS_ID, campaign_version=bebas.get("profile_version"))
    job_stale = dict(job_g, campaign_version=99)

    note = ns["campaign_caption_note"](job_g, ms[4])
    check("campaign_caption_note muncul untuk caption melanggar", bool(note), repr(note))
    check("caption_note memuat USULAN #Graemeholm", "Usulan" in note and "#Graemeholm" in note, repr(note))
    check("caption_note kosong untuk caption lengkap", ns["campaign_caption_note"](job_g, ms[5]) == "")
    check("caption_note no-op saat campaign bebas", ns["campaign_caption_note"](job_bebas, ms[4]) == "")
    check("caption_note no-op saat campaign_id hilang",
          ns["campaign_caption_note"]({"moments": ms}, ms[4]) == "")
    note_r = ns["campaign_caption_note"](
        {"campaign_id": "renyah-test", "campaign_version": 3, "moments": []},
        ClipMoment(start=0, end=20, hook="H", topic="t", caption="caption polos"),
    )
    check("caption_note terescape Markdown (tag underscore/bintang tidak merusak parse)",
          "#tag\\_wajib\\_x" in note_r and "#tag_wajib_x" not in note_r, repr(note_r))
    check("caption_note memisahkan usulan all_of vs any_of",
          "Usulan: #tag\\_wajib\\_x" in note_r and "Atau salah satu:" in note_r, repr(note_r))

    stale_note = ns["campaign_binding_note"](job_stale, graeme)
    check("binding_note menandai versi lama", "profil lama" in stale_note, repr(stale_note))
    check("binding_note kosong bila versi sama", ns["campaign_binding_note"](job_g, graeme) == "")

    details = ns["campaign_profile_details_text"](GRAEME_ID)
    check("profile_details_text terisi", bool(details) and "Graeme" in details and "Profil valid" in details)
    check("profile_details_text profil tak dikenal tidak crash",
          "tidak ditemukan" in ns["campaign_profile_details_text"]("id-hantu"))
    det_r = ns["campaign_profile_details_text"]("renyah-test")
    check("profile_details_text mengescape karakter Markdown dari profil",
          "Renyah \\[Uji] \\_Escape\\_" in det_r and "rokok\\_vape\\_promo" in det_r, repr(det_r[:200]))
    check("profile_details_text menampilkan rentang duration_s {min,max}", "10-45" in det_r, repr(det_r[:200]))
    check("profile_details_text menampilkan arahan kurasi (curation_note)",
          "Arahan kurasi" in det_r and "webinar" in det_r, repr(det_r[:260]))
    check("curation_note dibaca dari clip_rules (bukan hanya top-level)",
          "Arahan kurasi" in ns["campaign_reminders_text"](renyah), repr(ns["campaign_reminders_text"](renyah)[:120]))
    check("note profil di-escape aman di teks detail",
          "\\*webinar\\*" in det_r, repr(det_r[det_r.find("Arahan kurasi"):det_r.find("Arahan kurasi") + 90]))

    intro = ns["campaign_intro_text"](graeme)
    rem = ns["campaign_reminders_text"](graeme)
    check("campaign_intro_text berisi baris policy", "Graeme" in intro and "strictness" in intro)
    check("campaign_reminders_text berisi account_items + out_of_control",
          "tanggung jawabmu" in rem and "Di luar kendali" in rem)
    check("campaign_short memotong + mengescape",
          ns["campaign_short"]("a" * 200 + "_x", 10).startswith("aaaaaaaaaa"))
    check("campaign_item_text menangani dict",
          "satu" in ns["campaign_item_text"]({"text": "satu", "status": "todo"}))

    fw = ns["campaign_fail_warning_text"](job_g, 2)
    check("fail_warning_text untuk klip #2 (durasi 61s)", "Durasi" in fw and "61" in fw, repr(fw))
    check("fail_warning_text kosong untuk klip lolos", ns["campaign_fail_warning_text"](job_g, 1) == "")
    check("fail_warning_text no-op campaign bebas", ns["campaign_fail_warning_text"](job_bebas, 2) == "")
    check("fail_warning_text index aman",
          ns["campaign_fail_warning_text"](job_g, 99) == "" and ns["campaign_fail_warning_text"](job_g, 0) == "")
    fw_r = ns["campaign_fail_warning_text"](
        {"campaign_id": "renyah-test", "campaign_version": 3,
         "moments": neg_ms, "transcription": job_g["transcription"], "generated": set()}, 2)
    check("fail_warning_text menyertakan kutipan bukti terescape", "rokok\\_vape\\_promo" in fw_r, repr(fw_r))

    # --- langkah-2: verdict gabungan (evaluate + llm_flag) ---
    flagged = [
        ClipMoment(start=0, end=20, hook="H1", topic="t", risk_flags=["multi_speaker"]),
        ClipMoment(start=160, end=190, hook="H2", topic="t"),
    ]
    job_f = {"campaign_id": GRAEME_ID, "campaign_version": graeme.get("profile_version"),
             "moments": flagged, "transcription": job_g["transcription"], "generated": set()}
    merged = ns["ensure_campaign_verdicts"](job_f)
    codes0 = [v.code for v in merged[0]]
    check("risk_flags klip 1 masuk ke verdict gabungan sebagai llm_flag:*",
          any(c.startswith("llm_flag:") for c in codes0), str(codes0))
    check("klip tanpa risk_flags tidak mendapat llm_flag",
          not any(c.startswith("llm_flag:") for c in [v.code for v in merged[1]]), str(merged[1]))
    lvl0, _cause = ns["campaign_worst_verdict"](merged[0])
    check("sinyal AI tidak pernah memveto (aggregate klip ber-flag tetap < fail)",
          lvl0 in ("pass", "info", "warn"), f"{lvl0} :: {[(v.level, v.code) for v in merged[0]]}")
    check("campaign_verdict_is_soft hanya untuk kode lunak",
          ns["campaign_verdict_is_soft"](cp.Verdict("fail", "llm_flag:x", "m")) is True
          and ns["campaign_verdict_is_soft"](cp.Verdict("fail", "banned_exact", "m")) is False)
    check("verdict lunak berlevel fail diturunkan bot ke warn",
          [v.level for v in ns["campaign_demote_soft_fails"]([cp.Verdict("fail", "human_gate:g", "m")])] == ["warn"])

    # --- gerbang manusia: bentuk argumen final (list of lists) ---
    gates = ns["campaign_gates_for_flags"](renyah, ["multi_speaker"])
    check("campaign_gates_for_flags memakai bentuk [flags] dan mengembalikan 2 gate",
          [getattr(g, "code", "") for g in gates] == ["human_gate:graeme_majority", "human_gate:style_fit"],
          str([getattr(g, "code", "") for g in gates]))
    check("hanya gate 'warn' yang meminta konfirmasi manusia",
          [getattr(g, "code", "") for g in gates if getattr(g, "level", "") == "warn"]
          == ["human_gate:graeme_majority"])
    check("campaign_gates_for_flags NO-OP tanpa profil",
          ns["campaign_gates_for_flags"](None, ["multi_speaker"]) == [])
    gmap = ns["campaign_gate_map"](
        {"campaign_id": "renyah-test", "campaign_version": 3, "moments": flagged,
         "transcription": job_g["transcription"], "generated": set()}, [1, 2])
    check("campaign_gate_map memetakan gate ke klip yang benar",
          any(g["code"] == "human_gate:graeme_majority" and g["clips"] == [1] for g in gmap), str(gmap))

    # --- kontrak rekonsiliasi #4: nama field JSONL record <-> pembaca UI ---
    cand_keys: list[str] = []
    for n in ast.walk(FUNC_BY_NAME["campaign_overrides_tail_text"]):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            cand_keys.append(n.value)
    check("pembaca bot mencakup kunci kanonik lane A (ts/clip_index/verdict_codes)",
          {"ts", "clip_index", "verdict_codes"} <= set(cand_keys), str(sorted(set(cand_keys))[:14]))

    job_o = {"campaign_id": GRAEME_ID, "campaign_version": graeme.get("profile_version"),
             "moments": flagged, "transcription": job_g["transcription"], "generated": set()}
    wrote = ns["campaign_record_override"](job_o, 777, 1, ["human_gate:graeme_majority"], "sengaja lewat")
    check("campaign_record_override melapor sukses", wrote is True, repr(wrote))
    rows = cp.read_overrides_tail(GRAEME_ID, 5)
    check("audit trail lane A tertulis & terbaca lagi", bool(rows), str(rows[:1]))
    last = rows[-1] if rows else {}
    check("isi record sesuai pemanggilan bot",
          last.get("clip_index") == 1 and last.get("verdict_codes") == ["human_gate:graeme_majority"]
          and last.get("note") == "sengaja lewat" and last.get("chat_id") == 777
          and last.get("campaign_id") == GRAEME_ID
          and last.get("profile_version") == graeme.get("profile_version")
          and str(last.get("ts", "")).startswith("20"), str(last))
    tail_txt = ns["campaign_overrides_tail_text"](GRAEME_ID, 3)
    flat = tail_txt.replace("\\", "")  # Markdown v1 escape dari campaign_short
    check("/campaign show merender baris override dengan field benar",
          "klip `1`" in flat and "human_gate:graeme_majority" in flat
          and "sengaja lewat" in flat and "override terakhir" in flat
          and str(last.get("ts", ""))[:10] in flat, repr(tail_txt))
    check("render override memakai kode verdict penuh (bukan nama field kosong)",
          "human_gate:graeme_majority" in flat and "llm_flag" not in flat, repr(tail_txt))
    check("render override tidak memunculkan placeholder '?'/'None'",
          "`?`" not in flat and "None" not in tail_txt, repr(tail_txt))
    check("render override no-op untuk campaign bebas",
          ns["campaign_overrides_tail_text"](BEBAS_ID, 3) == "")
    check("render override aman untuk campaign tanpa audit",
          ns["campaign_overrides_tail_text"]("renyah-test", 3) == "")
    check("silice 7 tidak menyentuh dummy", not DUMMIES, str(sorted(DUMMIES)))
except Exception as exc:  # noqa: BLE001
    check("silice render termuat", False, repr(exc))

section("7b. introspeksi lama benar-benar dihapus (kontrak #3)")
check("campaign_gates_arg_nested dihapus dari bot.py", "campaign_gates_arg_nested" not in src_bot)
check("tidak ada introspeksi signature human_gates_for", "inspect.signature(cp.human_gates_for)" not in src_bot)
check("satu panggilan eksplisit human_gates_for", src_bot.count("cp.human_gates_for(") == 1)
gates_fn = FUNC_BY_NAME.get("campaign_gates_for_flags")
gsrc = ast.get_source_segment(src_bot, gates_fn) or "" if gates_fn else ""
check("call site mengirim bentuk list-of-lists [clean]", "cp.human_gates_for(profile, [clean])" in gsrc, repr(gsrc[:260]))
check("tidak ada percobaan ganda TypeError di helper gerbang",
      "except TypeError" not in gsrc and "secondary" not in gsrc)


# ---------------------------------------------------------------------------
# 8. Perutean langkah-2 (AST statis)
# ---------------------------------------------------------------------------
section("8. perutean langkah-2 di bot.py (AST statis)")
check("risk_flags ikut di-rebase (bot.py:482 sil)",
      "risk_flags=list(getattr(snapped_moment, \"risk_flags\", None) or [])," in src_bot)
render_fn = FUNC_BY_NAME.get("run_youtube_render_single")
clip_ctor = [
    n for n in ast.walk(render_fn)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ClipMoment"
] if render_fn else []
check("satu-satunya ClipMoment(...) di jalur render eksplisit", len(clip_ctor) == 1, str(len(clip_ctor)))
rf_kwargs = [k for c in clip_ctor for k in c.keywords if k.arg == "risk_flags"]
check("ClipMoment rebase mengirim risk_flags", bool(rf_kwargs))
check("akses .risk_flags selalu defensif (getattr), tidak pernah atribut langsung",
      not [n for n in ast.walk(bot_tree)
           if isinstance(n, ast.Attribute) and n.attr == "risk_flags"])
check("curation_note dirender bot", "campaign_curation_note_text(profile)" in src_bot)
check("rules text dari cp dipakai (bukan rakitan ulang di bot)",
      "cp.campaign_rules_text(profile)" in src_bot)
check("audit & tail override terhubung ke cp", "cp.record_override(" in src_bot
      and "cp.read_overrides_tail(cid, n=max(n, 1) + 2)" in src_bot)
cb = [
    str(kw.value.value) for n in ast.walk(bot_tree)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "CallbackQueryHandler"
    for kw in n.keywords if kw.arg == "pattern" and isinstance(kw.value, ast.Constant)
]
check("handler camp: terdaftar tunggal (tidak menabrak clip:/style:)", cb.count("^camp:") == 1, str(cb))
show = FUNC_BY_NAME.get("campaign_show_status")
check("campaign_show_status async + memakai aggregate_level & overrides tail",
      isinstance(show, ast.AsyncFunctionDef)
      and "campaign_overrides_tail_text" in (ast.get_source_segment(src_bot, show) or ""))
# Dua fungsi ini BOLEH raise: keduanya adalah PEKERJA (thread LLM) yang hasilnya
# selalu ditangani `campaign_import_run_compile` (except Exception -> render
# pesan error + state await_text). Transport compiler wajib raise supaya
# `compile_profile` membungkusnya jadi CompilerError.
RAISE_OK = {"campaign_cc_completion", "campaign_import_compile_sync"}
raisers = sorted(
    name for name, node in FUNC_BY_NAME.items()
    if name.startswith("campaign_") and name not in RAISE_OK
    and any(isinstance(n, ast.Raise) for n in ast.walk(node))
)
check("pemanggil pekerja (run_compile) menangkap SEMUA exception -> tidak ada raise lolos",
      any(isinstance(n, ast.Try) and any(h.type is None or
          (isinstance(h.type, ast.Name) and h.type.id == "Exception") for h in n.handlers)
          for n in ast.walk(FUNC_BY_NAME["campaign_import_run_compile"])))
check("helper render/state campaign tidak ada yang raise (fitur tak boleh hentikan klip)",
      not raisers, str(raisers))
check("transport compiler satu-satunya yang raise dan hanya AIAnalyzerError",
      "campaign_cc_completion" in FUNC_BY_NAME
      and all(
          isinstance(n.exc, ast.Call) and getattr(getattr(n.exc, "func", None), "id", "") == "AIAnalyzerError"
          for n in ast.walk(FUNC_BY_NAME["campaign_cc_completion"]) if isinstance(n, ast.Raise)
      ) and any(isinstance(n, ast.Raise) for n in ast.walk(FUNC_BY_NAME["campaign_cc_completion"])))
check("helper bot dipanggil dari silice, bukan dari import runtime",
      "load_bot_slice(" in (ROOT / "scratch" / "verify_campaign_integration.py").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 9. Kontrak rekonsiliasi lintas lane (cp murni)
# ---------------------------------------------------------------------------
section("9. kontrak rekonsiliasi lintas lane")
codes = {v.code for vs in res_a for v in vs}
check("Verdict.code == nama kind registry", codes <= set(cp.CHECK_REGISTRY), str(sorted(codes)))
check("namespace kode langkah-2 terdaftar di registry",
      {"llm_flag", "human_gate"} <= set(cp.CHECK_REGISTRY), str(sorted(cp.CHECK_REGISTRY)))
check("evaluate_moment mengembalikan verdict level pass", any(v.level == "pass" for v in res_a[0]))
ev = [v for v in cp.check_caption("Tonton full episode", graeme) if v.level == "warn"]
check("check_caption evidence berupa dict dengan kunci missing",
      bool(ev) and isinstance(ev[0].evidence, dict) and "#Graemeholm" in (ev[0].evidence.get("missing") or []),
      str(getattr(ev[0], "evidence", None)) if ev else "tanpa warn")
check("check_caption pass tidak memuat missing",
      cp.check_caption("#Graemeholm webinar", graeme)[0].evidence is None)
check("bebas -> check_caption tanpa verdict", cp.check_caption("apa pun", bebas) == [])
check("bebas -> evaluate_moment hanya kumpul verdict rule null (kosong)",
      cp.evaluate_moment(ms[1], segs_vtt(), bebas) == [])
try:
    cp.load_profile("id-hantu")
    check("load_profile profil hilang -> KeyError", False)
except KeyError:
    check("load_profile profil hilang -> KeyError", True)
except Exception as exc:  # noqa: BLE001
    check("load_profile profil hilang -> KeyError", False, repr(exc))
bad = copy.deepcopy(EVIL)
bad["clip_rules"]["deteksi_watermark"] = True
try:
    cp.save_profile(bad)
    check("save_profile profil invalid -> ValueError", False)
except ValueError as exc:
    check("save_profile profil invalid -> ValueError multi-baris", "\n- " in str(exc), repr(str(exc)[:120]))
except Exception as exc:  # noqa: BLE001
    check("save_profile profil invalid -> ValueError", False, repr(exc))
check("save_profile tidak menulis file untuk profil invalid",
      not (CAMPS / "renyah-test.json.tmp").exists())
check("campaign_intro_lines -> list[str] (kontrak bot)",
      all(isinstance(x, str) for x in cp.campaign_intro_lines(renyah)))
check("Verdict.evidence str untuk berbasis kutipan, dict untuk caption",
      isinstance([v for v in res_r[1] if v.code == "banned_exact"][0].evidence, str)
      and isinstance(ev[0].evidence, dict))
check("profil tanpa field langkah-2 (seed bebas) tetap lolos", not cp.validate_profile(bebas))

_no_net()

# cleanup: pulihkan socket/HTTP klien + folder profil, hapus sandbox
config.CAMPAIGNS_DIR = ROOT / "data" / "campaigns"
socket.create_connection = _real_create_conn  # type: ignore[assignment]
socket.getaddrinfo = _real_getaddrinfo  # type: ignore[assignment]
for _cls, _attr, _orig in _real_httpx:
    try:
        setattr(_cls, _attr, _orig)
    except Exception:
        pass
if _real_req is not None:
    try:
        import requests as _rq

        _rq.Session.request = _real_req  # type: ignore[attr-defined]
    except Exception:
        pass
shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL GREEN")
sys.exit(0)
