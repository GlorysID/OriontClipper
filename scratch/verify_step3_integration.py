"""JAHITAN lintas-lane LANGKAH-3: /campaign import (bot.py) x campaign_compiler x campaign_policy.

ATURAN BESI: tidak pernah ``import bot``. Kode bot diambil lewat SILICE SUMBER
(def diekstrak dari AST bot.py, lalu di-exec di namespace terkendali) sehingga
perilaku bot ASLI ikut diuji tanpa memuat telegram loop/main(). ``requests``
ditukar tiruan lewat sys.modules -> payload LLM terbaca utuh, dan tripwire
socket tetap terpasang: nol jaringan.

Jalan:  .venv\\Scripts\\python.exe scratch\\verify_step3_integration.py

Cakupan:
  0 kebersihan harness (import bot / subprocess timeout) + tripwire
  1 fixture S&K 10 klausul: split_clauses/build_messages kanonik
  2 chat_fn adapter: system+user terkirim APA ADANYA (TIDAK dibungkus prompt
    analisis klip), satu percobaan, temperature 0, tanpa response_format
  3 draf compiler -> ditegakkan campaign_policy (validate/save/load/evaluate/caption)
  4 save_draft: id dari RETURN VALUE, tabrakan -> -2/-3, jalur tombol SIMPAN bot
  5 summarize_for_user -> escape+chunk bot (lossless, <=4096, tanpa '\\' menggantung)
  6 jalur CompilerError (validator reject / klausul hilang / bug tak terduga)
  7 konfirmasi keputusan: TIDAK ada auto-retry bot-side (retry = user tekan ulang)
"""

from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import json
import logging
import re
import shutil
import socket
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
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
# Tripwire jaringan
# ---------------------------------------------------------------------------
NET_ATTEMPTS: list[str] = []


def _blocked(*args: Any, **kwargs: Any) -> Any:
    NET_ATTEMPTS.append(f"connect: {args[:2]}")
    raise AssertionError("AKSES JARINGAN DIBLOKIR oleh verify_step3_integration")


_real_create_conn = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo
socket.create_connection = _blocked  # type: ignore[assignment]
socket.getaddrinfo = _blocked  # type: ignore[assignment]


def _no_net(label: str) -> None:
    check(label, not NET_ATTEMPTS, str(NET_ATTEMPTS[:3]))


try:
    socket.create_connection(("api.telegram.org", 443), timeout=1)
    _trip = False
except AssertionError:
    _trip = True
except Exception:
    _trip = False
check("tripwire jaringan efektif", _trip)
NET_ATTEMPTS.clear()


# ---------------------------------------------------------------------------
# 0. Kebersihan harness
# ---------------------------------------------------------------------------
section("0. kebersihan harness")
for path in sorted((ROOT / "scratch").glob("verify_*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    subs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
    ]
    check(f"{path.name}: tanpa import bot", "bot" not in mods, str(sorted(mods & {"bot"})))
    check(f"{path.name}: subprocess selalu pakai timeout",
          all(any(k.arg == "timeout" for k in c.keywords) for c in subs), str([c.lineno for c in subs]))

src_bot = (ROOT / "bot.py").read_text(encoding="utf-8")
bot_tree = ast.parse(src_bot, filename=str(ROOT / "bot.py"))


# ---------------------------------------------------------------------------
# Silice sumber bot (AST-extraksi; TANPA import bot)
# ---------------------------------------------------------------------------
class _Anything:
    def __init__(self, name: str = "?") -> None:
        self._name = name

    def __repr__(self) -> str:
        return f"<dummy {self._name}>"

    def __getattr__(self, item: str) -> "_Anything":
        return _Anything(f"{self._name}.{item}")

    def __call__(self, *a: Any, **k: Any) -> "_Anything":
        return _Anything(f"{self._name}()")


import config  # noqa: E402
from modules import campaign_policy as cp  # noqa: E402
from modules import campaign_compiler as cc  # noqa: E402
from modules.ai_analyzer import (  # noqa: E402
    AIAnalyzer,
    AIAnalyzerError,
    extract_chat_message_content,
    parse_chat_response_body,
)
import telegram  # noqa: E402
from telegram import (  # noqa: E402
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BOT_DEFS: dict[str, ast.stmt] = {
    n.name: n for n in bot_tree.body  # type: ignore[attr-defined]
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
}
# Konstanta modul yang nilainya dikendalikan dari modul asli, bukan dari bot.py
CONST_DENY = {
    "cp", "cc", "CAMPAIGN_OK", "CAMPAIGN_FREE_ID", "_CP_IMPORT_ERR", "LOGGER",
    "config", "CAMPAIGN_CC_IMPORT", "MAIN_MENU",
}
BOT_CONSTS: dict[str, ast.stmt] = {
    t.id: n
    for n in bot_tree.body
    if isinstance(n, ast.Assign)
    for t in n.targets
    if isinstance(t, ast.Name) and t.id not in CONST_DENY
}
DUMMIES: set[str] = set()


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def load_bot_slice(roots: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ns: dict[str, Any] = {
        "__name__": "bot_step3_slice",
        "re": re, "json": json, "inspect": inspect, "asyncio": asyncio, "types": types,
        "logging": logging, "config": config, "cp": cp, "cc": cc, "Path": Path, "Any": Any,
        "AIAnalyzer": AIAnalyzer, "AIAnalyzerError": AIAnalyzerError,
        "extract_chat_message_content": extract_chat_message_content,
        "parse_chat_response_body": parse_chat_response_body,
        "CAMPAIGN_OK": True, "CAMPAIGN_FREE_ID": cp.default_profile_id(),
        "CAMPAIGN_CC_IMPORT": "campaign_compiler", "telegram": telegram,
        "InlineKeyboardButton": InlineKeyboardButton, "InlineKeyboardMarkup": InlineKeyboardMarkup,
        "KeyboardButton": KeyboardButton, "ReplyKeyboardMarkup": ReplyKeyboardMarkup,
        "MAIN_MENU": ReplyKeyboardMarkup([[KeyboardButton("Menu")]], resize_keyboard=True),
        "LOGGER": logging.getLogger("step3.slice"),
    }
    ns["LOGGER"].setLevel(logging.CRITICAL)
    ns.update(extra or {})
    queue = list(roots)
    picked: dict[str, ast.stmt] = {}
    while queue:
        name = queue.pop(0)
        node = BOT_DEFS.get(name) or BOT_CONSTS.get(name)
        if node is None or name in picked:
            continue
        picked[name] = node
        queue.extend(sorted(_names(node) - set(picked)))
    for name in roots:
        if name not in picked:
            raise AssertionError(f"simbol bot '{name}' tidak ada di tingkat modul")
    # URUTAN PENTING: konstanta modul dulu (default arg dievaluasi saat def di-exec),
    # lalu class, baru fungsi. Kalau tidak, default `limit=CAMP_IMPORT_CHUNK` akan
    # melihat dummy daripada nilai aslinya.
    consts = [n for n in picked.values() if isinstance(n, ast.Assign)]
    classes = [n for n in picked.values() if isinstance(n, ast.ClassDef)]
    funcs = [n for n in picked.values() if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    module = ast.Module(body=consts + classes + funcs, type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, "bot_step3_slice.py", "exec")
    for _ in range(150):
        try:
            exec(code, ns)  # noqa: S102 - sumber: bot.py, via AST-nya sendiri
            break
        except NameError as exc:
            missing = getattr(exc, "name", None)
            if not missing or missing in ns:
                raise
            ns[missing] = _Anything(missing)
            DUMMIES.add(missing)
    else:
        raise AssertionError("silice tidak konvergen")
    return ns


# ---------------------------------------------------------------------------
# Sandbox profil
# ---------------------------------------------------------------------------
TMP = Path(tempfile.mkdtemp(prefix="orion_step3_verify_"))
shutil.copytree(config.CAMPAIGNS_DIR, TMP / "campaigns")
CAMPS = TMP / "campaigns"
config.CAMPAIGNS_DIR = CAMPS

# ---------------------------------------------------------------------------
# 1. Fixture S&K 10 klausul
# ---------------------------------------------------------------------------
section("1. fixture S&K 10 klausul (kanonik)")
SK = """S&K MerekX
Klip video harus berdurasi antara 20 sampai 45 detik saja.
Dilarang menyebut kata promogilas di dalam klip maupun caption.
Konten yang membahas seksualitas atau kekerasan grafis tidak diperbolehkan.
Setiap caption wajib menyertakan tag #MerekX di awal baris.
Caption boleh menambahkan tulisan link di bio atau kode promo bulanan.
Akun wajib men-tag @merekx resmi pada bio dan foto profil.
Jumlah followers minimal 25 ribu dan tidak boleh hasil manipulasi.
Engagement rate rata-rata akun harus terjaga di atas 0,2 persen.
Utamakan klip dari segmen webinar saat founder menjelaskan strategi konten.
Jika penutur selain founder lebih dari separuh klip tandai untuk review manual.
"""
clauses = cc.split_clauses(SK)
check("split_clauses -> 10 klausul (judul pendek dibuang)", len(clauses) == 10, str(len(clauses)))
check("split_clauses idempoten", cc.split_clauses("\n".join(clauses)) == clauses)
check("numbered_clauses 1-based & lengkap", cc.numbered_clauses(SK)[0][0] == 1 and cc.numbered_clauses(SK)[-1][0] == 10)
msgs = cc.build_messages(SK)
check("build_messages -> [system, user]",
      len(msgs) == 2 and [m["role"] for m in msgs] == ["system", "user"], str([m.get("role") for m in msgs]))
check("system prompt memuat kemampuan mesin + denah field",
      "Kemampuan mesin" in msgs[0]["content"] and "caption_required" in msgs[0]["content"])
check("user prompt memuat klausul bernomor + jumlah klausul",
      "DAFTAR KLAUSUL" in msgs[1]["content"] and "Jumlah klausul = 10" in msgs[1]["content"])
check("rule kind langkah-2 ikut masuk prompt (registry runtime)",
      "human_gate" in msgs[0]["content"] and "llm_flag" in msgs[0]["content"])


def clauses_from(text: str) -> list[str]:
    return [m.group(2) for m in re.finditer(r"^(\d+)\. (.+)$", text, re.MULTILINE)]


check("klausul dalam prompt == split_clauses", clauses_from(msgs[1]["content"]) == clauses)

ANSWER: dict[str, Any] = {
    "profile": {
        "id": "merekx-id",
        "name": "MerekX Indonesia",
        "strictness": "standard",
        "clip_rules": {
            "duration_s": {"min": 20, "max": 45},
            "banned_exact": ["promogilas"],
            "banned_topics": [{"topic": "seks_kekerasan", "regex": ["seksualitas", "kekerasan grafis"]}],
            "curation_note": clauses[8],
            "flag_requests": [{"id": "non_founder_speaker", "ask": "Apakah penutur selain founder dominan?"}],
            "human_gates": [{
                "id": "founder_majority", "text": "Pastikan founder tetap penutur dominan sebelum posting.",
                "trigger": "llm_flag:non_founder_speaker", "strict_mode": "ack_always",
            }],
        },
        "post_rules": {"caption_required": {"all_of": ["#MerekX"], "any_of": ["link di bio", "kode promo"]}},
        "account_items": [clauses[5]],
        "out_of_control": [clauses[6], clauses[7]],
    },
    "mapped": [
        {"clause": clauses[0], "dest": "clip_rules.duration_s"},
        {"clause": clauses[1], "dest": "clip_rules.banned_exact"},
        {"clause": clauses[2], "dest": "clip_rules.banned_topics"},
        {"clause": clauses[3], "dest": "post_rules.caption_required"},
        {"clause": clauses[4], "dest": "post_rules.caption_required"},
        {"clause": clauses[5], "dest": "account_items"},
        {"clause": clauses[8], "dest": "clip_rules.curation_note"},
        {"clause": clauses[9], "dest": "clip_rules.flag_requests"},
    ],
    "out_of_control": [clauses[6], clauses[7]],
    "unaccounted": [],
}
ANSWER_JSON = json.dumps(ANSWER, ensure_ascii=False)


def good_chat(messages: list[dict]) -> str:
    return ANSWER_JSON


# ---------------------------------------------------------------------------
# 2. Adapter -> payload nyata ke "9Router" (requests ditirukan)
# ---------------------------------------------------------------------------
section("2. chat_fn adapter: payload yang benar-benar dikirim")


class _ReqErrors:
    class Timeout(Exception):
        pass

    class ConnectionError(Exception):  # noqa: N818 - meniru bentuk modul requests
        pass

    class RequestException(Exception):
        pass


POSTS: list[dict[str, Any]] = []
ATTEMPTS: list[int] = []
INJECT: dict[str, Any] = {}


def _fake_post(url: str = "", headers: dict | None = None, json: dict | None = None, timeout: Any = None) -> Any:
    ATTEMPTS.append(1)
    if INJECT.get("exc") is not None:
        raise INJECT["exc"]
    payload = json or {}
    POSTS.append({"url": url, "headers": dict(headers or {}), "payload": payload, "timeout": timeout})
    answer = INJECT.get("answer")
    if answer is None:
        answer = obedient_answer(payload)
    body = {"choices": [{"message": {"content": answer}}]}
    return types.SimpleNamespace(status_code=200, text=json_module_dumps(body))


def json_module_dumps(obj: Any) -> str:
    return _JSON.dumps(obj, ensure_ascii=False)


_JSON = json  # alias: _fake_post punya parameter bernama `json`


def reset_http() -> None:
    POSTS.clear()
    ATTEMPTS.clear()
    INJECT.clear()


def obedient_answer(payload: dict) -> str:
    """Model patuh: jawab sesuai instruksi yang benar-benar ia terima.

    Klausul dibaca HANYA dari pesan role 'user' (system prompt juga punya baris
    bernomor '1. Petakan TIAP klausul...' yang bukan daftar klausul).
    """
    msgs = payload.get("messages") or []
    user_txt = "\n\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "user")
    sys_txt = "\n\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "system")
    listed = clauses_from(user_txt)
    ok = (
        "DAFTAR KLAUSUL" in user_txt
        and len(listed) == len(clauses)
        and all(c in clauses for c in listed)
        and "Denah profil" in sys_txt
    )
    if ok:
        return ANSWER_JSON
    return _JSON.dumps({"clips": [{"start": 1, "end": 20, "hook": "X"}]})


fake_requests = types.ModuleType("requests")
fake_requests.exceptions = _ReqErrors  # type: ignore[attr-defined]
fake_requests.post = _fake_post  # type: ignore[attr-defined]
_real_requests = sys.modules.get("requests")
sys.modules["requests"] = fake_requests

try:
    ns2 = load_bot_slice([
        "_CampaignChatAdapter", "campaign_cc_completion", "campaign_import_compile_sync",
        "campaign_cc", "escape_tg_md",
    ])
    check("silice adapter termuat", callable(ns2.get("campaign_import_compile_sync")))

    seen: list[list[dict]] = []

    def spy_chat(messages: list[dict]) -> str:
        seen.append(copy.deepcopy(messages))
        return good_chat(messages)

    cc.compile_profile(SK, spy_chat)
    check("chat_fn menerima pesan persis cc.build_messages (system dulu, urutan utuh)",
          seen == [cc.build_messages(SK)], str([len(m) for m in seen]))

    reset_http()
    adapter = ns2["_CampaignChatAdapter"](logger=logging.getLogger("t"))
    text_out = adapter._chat_text(cc.build_messages(SK))
    check("adapter mengirim TEPAT SATU request (tanpa retry)",
          len(ATTEMPTS) == 1 and len(POSTS) == 1, f"{len(ATTEMPTS)}/{len(POSTS)}")
    p = POSTS[0]["payload"] if POSTS else {}
    got = p.get("messages") or []
    check("payload mempertahankan role [system, user]",
          [m.get("role") for m in got] == ["system", "user"], str([m.get("role") for m in got]))
    check("isi system utuh == build_messages()[0]",
          bool(got) and got[0].get("content") == msgs[0]["content"])
    check("isi user utuh == build_messages()[1]",
          len(got) > 1 and got[1].get("content") == msgs[1]["content"])
    allj = "\n".join(str(m.get("content", "")) for m in got)
    banned = ["5 PILAR VIRALITAS", "viral_score", "ATURAN KETAT KONTEKS MANDIRI", "bgm_mood", "Transkrip:"]
    check("TIDAK ada template analisis klip di payload",
          not [b for b in banned if b in allj], str([b for b in banned if b in allj]))
    check("bahan S&K benar-benar sampai ke model", "Dilarang menyebut kata promogilas" in allj)
    check("response_format tidak dikirim", "response_format" not in p, str(sorted(p)))
    check("temperature 0 (deterministik untuk compiler)", p.get("temperature") == 0.0, str(p.get("temperature")))
    check("timeout dari config", bool(POSTS) and POSTS[0]["timeout"] == config.REQUEST_TIMEOUT_S,
          str([POSTS[0]["timeout"]] if POSTS else []))
    check("url /chat/completions + model diset", bool(POSTS) and POSTS[0]["url"].endswith("/chat/completions") and "model" in p)
    check("isi jawaban dikembalikan mentah ke compiler", text_out == ANSWER_JSON, repr(text_out[:60]))

    try:
        adapter._request_chat_completion("x", False)
        check("jalur prompt klip di adapter DITOLAK (AIAnalyzerError)", False)
    except AIAnalyzerError:
        check("jalur prompt klip di adapter DITOLAK (AIAnalyzerError)", True)

    reset_http()
    out2 = ns2["campaign_import_compile_sync"](cc, SK)
    check("campaign_import_compile_sync (jalur handler) -> hasil compile", isinstance(out2, dict))
    check("jalur handler: 1 request, tanpa template klip",
          len(POSTS) == 1 and "viral_score" not in str(POSTS[0]["payload"].get("messages")), str(len(POSTS)))
    check("hasil jalur handler == hasil spy chat_fn", out2["profile"]["id"] == "merekx-id")

    reset_http()
    INJECT["exc"] = _ReqErrors.ConnectionError("boom")
    try:
        ns2["campaign_import_compile_sync"](cc, SK)
        check("kegagalan koneksi -> CompilerError", False)
    except cc.CompilerError as exc:
        check("kegagalan koneksi -> CompilerError 'AI gagal menjawab'", "AI gagal menjawab" in str(exc), str(exc)[:120])
        check("details menyebut jenis exception asal",
              bool(exc.details) and "AIAnalyzerError" in " ".join(exc.details), str(exc.details))
    check("TETAP satu percobaan saat koneksi gagal (tanpa retry)", len(ATTEMPTS) == 1, str(len(ATTEMPTS)))

    reset_http()
    INJECT["answer"] = "Maaf, saya tidak bisa membantu."
    try:
        ns2["campaign_import_compile_sync"](cc, SK)
        check("jawaban non-JSON -> CompilerError", False)
    except cc.CompilerError as exc:
        check("jawaban non-JSON -> CompilerError 'bukan JSON'", "JSON" in str(exc), str(exc)[:120])
    check("jawaban non-JSON: tetap 1 percobaan", len(ATTEMPTS) == 1, str(len(ATTEMPTS)))

    reset_http()
    INJECT["answer"] = '{"clips": []}'
    try:
        ns2["campaign_import_compile_sync"](cc, SK)
        check("model menyahut format klip -> CompilerError 'profile'", False)
    except cc.CompilerError as exc:
        check("model menyahut format klip -> CompilerError 'profile'", "profile" in str(exc), str(exc)[:140])
    check("silice adapter tanpa dummy", not DUMMIES, str(sorted(DUMMIES)))
except Exception as exc:  # noqa: BLE001
    check("bagian 2 berjalan", False, repr(exc))
finally:
    if _real_requests is not None:
        sys.modules["requests"] = _real_requests
    else:
        sys.modules.pop("requests", None)

_no_net("bagian 2: nol koneksi nyata (requests ditirukan)")


# ---------------------------------------------------------------------------
# 3. Draf compiler ditegakkan policy
# ---------------------------------------------------------------------------
section("3. draf compiler -> ditegakkan campaign_policy")
result = cc.compile_profile(SK, good_chat)
draft = result["profile"]
check("draf lolos validate_profile", not cp.validate_profile(draft), str(cp.validate_profile(draft)))
check("coverage menelusuri 10 klausul",
      len(result["coverage"]["mapped"]) + len(result["coverage"]["out_of_control"])
      + len(result["coverage"]["unaccounted"]) == 10, str({k: len(v) for k, v in result["coverage"].items()}))
check("out_of_control profil memuat klausul verbatim",
      all(c in draft["out_of_control"] for c in result["coverage"]["out_of_control"]), str(draft["out_of_control"]))
check("warnings sanitasi kosong untuk jawaban patuh", result["warnings"] == [], str(result["warnings"]))

cp.save_profile(dict(draft))
back = cp.load_profile(draft["id"])
check("save_profile/load_profile roundtrip identik", back == draft)

segs = [
    {"start": 0, "end": 30, "text": "halo semuanya ini penjelasan strategi webinar kemarin"},
    {"start": 30, "end": 70, "text": "dan ini bagian yang jauh lebih panjang dari empat puluh lima detik"},
    {"start": 70, "end": 95, "text": "pokoknya promosi promogilas resmi dari sponsor"},
]
m_ok = types.SimpleNamespace(start=0.0, end=30.0, caption="#MerekX link di bio")
m_long = types.SimpleNamespace(start=0.0, end=70.0, caption="")
m_bad = types.SimpleNamespace(start=70.0, end=95.0, caption="caption tanpa tag")
v_ok = cp.evaluate_moment(m_ok, segs, back)
v_long = cp.evaluate_moment(m_long, segs, back)
v_bad = cp.evaluate_moment(m_bad, segs, back)
check("draf menjatuhkan klip 70s (duration_range fail)",
      cp.aggregate_level(v_long) == "fail" and any(v.code == "duration_range" for v in v_long),
      str([(v.level, v.code) for v in v_long]))
check("draf meloloskan klip 30s caption lengkap",
      cp.aggregate_level(v_ok) != "fail"
      and not any(v.code == "caption_required" and v.level != "pass" for v in v_ok),
      str([(v.level, v.code) for v in v_ok]))
check("banned_exact draf menandai klip 'promogilas'",
      any(v.code == "banned_exact" and v.level == "fail" for v in v_bad), str([(v.level, v.code) for v in v_bad]))
cap_warn = cp.check_caption("caption tanpa tag", back)
check("check_caption draf menuntut #MerekX + usulan missing",
      cap_warn[0].level == "warn" and "#MerekX" in (cap_warn[0].evidence or {}).get("missing", []),
      str(cap_warn[:1]))
rules_txt = cp.campaign_rules_text(back)
check("campaign_rules_text draf siap-tempel (durasi + id flag)",
      "20-45" in rules_txt and "non_founder_speaker" in rules_txt, repr(rules_txt[:200]))
flags = cp.llm_flag_verdicts(["non_founder_speaker"], back)
gates = cp.human_gates_for(back, [["non_founder_speaker"]])
check("flag+gate keluaran compiler aktif di mesin kebijakan",
      [v.level for v in flags] == ["warn"] and "human_gate:founder_majority" in [v.code for v in gates],
      str([(v.level, v.code) for v in gates]))


# ---------------------------------------------------------------------------
# 4. save_draft + tombol SIMPAN
# ---------------------------------------------------------------------------
section("4. save_draft (return value) & tombol SIMPAN")
store = copy.deepcopy(result)
store["profile"]["id"] = "merekx-store"
id1 = cc.save_draft(store)
check("save_draft mengembalikan id str", isinstance(id1, str) and id1 == "merekx-store", repr(id1))
check("file id hasil return ada", (CAMPS / f"{id1}.json").exists(), str(CAMPS / f"{id1}.json"))
id2 = cc.save_draft(store)
id3 = cc.save_draft(store)
check("tabrakan -> -2 lalu -3", (id1, id2, id3) == ("merekx-store", "merekx-store-2", "merekx-store-3"),
      f"{id1} {id2} {id3}")
check("result tidak dimutasi suffix (id logis stabil)", store["profile"]["id"] == "merekx-store", store["profile"]["id"])
check("semua varian id tersimpan valid", all(cp.validate_profile(cp.load_profile(x)) == [] for x in (id1, id2, id3)))
check("save_draft(unique=False) menimpa id dasar", cc.save_draft(store, unique=False) == "merekx-store")
check("id hasil compile asli tetap tersimpan terpisah (tidak tertimpa)",
      cp.load_profile("merekx-id")["name"] == "MerekX Indonesia")

check("bot TIDAK membaca result['profile']['id'] di mana pun",
      "['profile']['id']" not in src_bot and '["profile"]["id"]' not in src_bot)
check("bot memakai NILAI RETURN save_draft", 'cid = str(cc.save_draft(st["result"]))' in src_bot)

DUMMIES.clear()
try:
    ns4 = load_bot_slice([
        "campaign_import_handle_callback", "campaign_import_state", "campaign_import_clear",
        "campaign_cc", "build_campaign_import_keyboard", "build_campaign_import_error_keyboard",
        "escape_tg_md", "campaign_short", "campaign_reply_target",
    ])

    class RecBot:
        def __init__(self, owner: list[dict[str, Any]]) -> None:
            self.owner = owner

        async def send_message(self, **kwargs: Any) -> None:
            self.owner.append(kwargs)

    class MsgStub:
        def __init__(self) -> None:
            self.chat_id = 555
            self.edits: list[Any] = []

        async def edit_reply_markup(self, reply_markup: Any = None) -> None:
            self.edits.append(reply_markup)

    class QueryStub:
        def __init__(self) -> None:
            self.message = MsgStub()
            self.answers: list[tuple[str, bool]] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    class CtxStub:
        def __init__(self, user_data: dict[str, Any]) -> None:
            self.user_data = user_data
            self.sent: list[dict[str, Any]] = []
            self.bot = RecBot(self.sent)

    class UpdateStub:
        def __init__(self, query: QueryStub) -> None:
            self.callback_query = query
            self.message = None

    fresh = cc.compile_profile(SK, good_chat)
    ctx4 = CtxStub({"camp_import": {"stage": "result", "result": fresh}})
    upd4 = UpdateStub(QueryStub())
    asyncio.run(ns4["campaign_import_handle_callback"](upd4, ctx4, "save"))
    pending = str(ctx4.user_data.get("pending_campaign") or "")
    check("tombol SIMPAN: pending_campaign = id dari save_draft",
          pending.startswith("merekx-id") and (CAMPS / f"{pending}.json").exists(), pending)
    check("tombol SIMPAN: state camp_import dibersihkan", "camp_import" not in ctx4.user_data)
    check("tombol SIMPAN: markup tombol konfirmasi dihapus", bool(upd4.callback_query.message.edits))
    check("tombol SIMPAN: pesan konfirmasi memuat id (escaping aman)",
          any("Draft tersimpan" in str(s.get("text", "")) for s in ctx4.sent), str(ctx4.sent)[:180])
    check("tombol SIMPAN memakai campaign_cc() lazy (bukan import modul)",
          "importlib" in ast.get_source_segment(src_bot, BOT_DEFS["campaign_cc"]) or "")

    ctx5 = CtxStub({})
    asyncio.run(ns4["campaign_import_handle_callback"](UpdateStub(QueryStub()), ctx5, "save"))
    check("save tanpa state -> 'sesi tidak aktif' tanpa raise",
          any("tidak aktif" in str(s.get("text", "")) for s in ctx5.sent), str(ctx5.sent)[:160])
    kb = ns4["build_campaign_import_keyboard"]()
    labels = [b.text for row in kb.inline_keyboard for b in row]
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("keyboard impor: Simpan/ulang/batal + callback_data aman",
          any("Simpan" in t for t in labels) and "camp:imp:save" in data and all(len(d) <= 64 for d in data),
          str(labels))
    check("silice save tanpa dummy", not DUMMIES, str(sorted(DUMMIES)))
except Exception as exc:  # noqa: BLE001
    check("bagian 4 (jalur tombol) berjalan", False, repr(exc))


# ---------------------------------------------------------------------------
# 5. summarize -> escape/chunk
# ---------------------------------------------------------------------------
section("5. summarize_for_user -> render bot (escape lalu chunk)")
DUMMIES.clear()
try:
    ns5 = load_bot_slice([
        "campaign_import_send_summary", "campaign_import_chunks", "escape_tg_md",
        "build_campaign_import_keyboard",
    ])
    extra = [
        f"Tambahan klausul nomor {i} tentang *rahasia_brand* dan _istilah_bawah_ #tag_x."
        for i in range(60)
    ]
    long_sk = SK + "\n" + "\n".join(extra)
    big = copy.deepcopy(ANSWER)
    # 60 klausul ekstra dipetakan ke account_items (bagian ringkasan yang TIDAK
    # dipotong _shorten) -> ringkasan benar-benar melewati 4000 karakter.
    big["profile"]["account_items"] = [clauses[5]] + extra
    big["mapped"] = list(big["mapped"]) + [{"clause": e, "dest": "account_items"} for e in extra]
    big_result = cc.compile_profile(long_sk, lambda m: _JSON.dumps(big, ensure_ascii=False))
    check("compile bahan panjang -> 70 klausul terpetakan",
          len(big_result["coverage"]["mapped"]) == 68, str({k: len(v) for k, v in big_result["coverage"].items()}))
    summary = cc.summarize_for_user(big_result)
    check("summarize_for_user: ringkasan panjang terisi", len(summary) > 4000, str(len(summary)))

    check("summarize menyebut coverage & out_of_control",
          "Coverage" in summary and "Di luar kendali" in summary, repr(summary[-200:]))
    escaped = ns5["escape_tg_md"](summary)
    check("escaping memperpanjang teks (maka chunk HARUS sesudah escape)",
          len(escaped) > len(summary), f"{len(summary)} -> {len(escaped)}")
    chunks = ns5["campaign_import_chunks"](escaped)
    check("chunk lossless (gabung == escaped)", "".join(chunks) == escaped)
    check("tiap chunk <= CAMP_IMPORT_CHUNK (<=4096)",
          all(len(c) <= 4000 for c in chunks), str([len(c) for c in chunks]))
    check("tidak ada chunk berakhiran backslash menggantung", not any(c.endswith("\\") for c in chunks))
    check("bahan panjang -> multi-chunk", len(chunks) > 1, str(len(chunks)))
    cek_ujung = ns5["campaign_import_chunks"]("a" * 3999 + "\\_" + "b" * 10, 4000)
    check("tepat di batas: pasangan escape tidak dipecah",
          "".join(cek_ujung) == "a" * 3999 + "\\_" + "b" * 10 and not cek_ujung[0].endswith("\\"),
          str([c[-3:] for c in cek_ujung]))

    class SendCtx:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            outer = self

            class B:
                async def send_message(self, **kwargs: Any) -> None:
                    outer.sent.append(kwargs)

            self.bot = B()

    sctx = SendCtx()
    asyncio.run(ns5["campaign_import_send_summary"](sctx, 123, summary))
    check("semua pesan <=4096", all(len(s["text"]) <= 4096 for s in sctx.sent), str([len(s["text"]) for s in sctx.sent]))
    check("parse_mode Markdown + teks sudah terescape",
          all(s.get("parse_mode") == "Markdown" for s in sctx.sent) and any("\\_" in s["text"] for s in sctx.sent))
    check("keyboard hanya pada pesan terakhir",
          [bool(s.get("reply_markup")) for s in sctx.sent] == [False] * (len(sctx.sent) - 1) + [True],
          str([bool(s.get("reply_markup")) for s in sctx.sent]))
    check("reassembly pesan == ringkasan terescape", "".join(s["text"] for s in sctx.sent) == escaped)
    sctx2 = SendCtx()
    asyncio.run(ns5["campaign_import_send_summary"](sctx2, 123, ""))
    check("ringkasan kosong -> tetap kirim '(kosong)'",
          bool(sctx2.sent) and sctx2.sent[0]["text"] == "(kosong)", str(sctx2.sent)[:80])
    check("silice chunk tanpa dummy", not DUMMIES, str(sorted(DUMMIES)))
except Exception as exc:  # noqa: BLE001
    check("bagian 5 berjalan", False, repr(exc))


# ---------------------------------------------------------------------------
# 6. Jalur error CompilerError
# ---------------------------------------------------------------------------
section("6. jalur error CompilerError -> render aman")


class StubCC:
    CompilerError = cc.CompilerError

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def compile_profile(self, raw: str, chat_fn: Any) -> dict:
        self.calls += 1
        raise self.exc

    def summarize_for_user(self, result: Any) -> str:  # pragma: no cover
        return "tidak terpakai"


DUMMIES.clear()
try:
    ns6 = load_bot_slice([
        "campaign_import_run_compile", "campaign_import_compile_sync", "campaign_import_chunks",
        "escape_tg_md", "build_campaign_import_error_keyboard", "build_campaign_import_keyboard",
        "_CampaignChatAdapter", "campaign_cc_completion", "campaign_import_clear",
    ])

    class StatusStub:
        def __init__(self) -> None:
            self.edits: list[str] = []
            self.replies: list[dict[str, Any]] = []
            self.chat_id = 999

        async def edit_text(self, text: str, **kw: Any) -> None:
            self.edits.append(text)

        async def reply_text(self, text: str, **kw: Any) -> None:
            self.replies.append({"text": text, **kw})

    class RunCtx:
        def __init__(self) -> None:
            self.user_data: dict[str, Any] = {}
            self.sent: list[dict[str, Any]] = []
            outer = self

            class B:
                async def send_message(self, **kwargs: Any) -> None:
                    outer.sent.append(kwargs)

            self.bot = B()

    def run_error(exc: Exception) -> tuple[dict[str, Any], StatusStub, StubCC]:
        st = {"stage": "await_text", "raw": "x"}
        ctx = RunCtx()
        msg = StatusStub()
        stub = StubCC(exc)
        asyncio.run(ns6["campaign_import_run_compile"](ctx, stub, 77, st, "BAHAN S&K", msg))
        return st, msg, stub

    st1, msg1, stub1 = run_error(cc.CompilerError(
        "draf profil ditolak validator campaign_policy",
        ["clip_rules: rule kind tak dikenal 'deteksi_watermark'", "duration_s.min harus angka"],
    ))
    t1 = "\n".join(msg1.edits)
    flat1 = t1.replace("\\", "")  # Markdown-v1 escape dari bot
    check("validator-reject: rincian details ikut ditampilkan (terescape)",
          "deteksi\\_watermark" in t1 and "duration_s.min" in flat1 and "Rincian" in flat1, repr(t1[:280]))
    check("validator-reject: state await_text + result dibuang",
          st1["stage"] == "await_text" and "result" not in st1, str(st1))
    err_labels = [b.text for row in msg1.replies[0]["reply_markup"].inline_keyboard for b in row]
    check("validator-reject: tombol Paste ulang & Batal ditawarkan",
          any("Paste" in t for t in err_labels) and any("Batal" in t for t in err_labels), str(err_labels))
    check("hanya SATU percobaan compile (tanpa auto-retry)", stub1.calls == 1, str(stub1.calls))

    st2, msg2, stub2 = run_error(cc.CompilerError(
        'klausul hilang tanpa jejak: ["Klip wajib memakai musik berlisensi"]',
        ["Klip wajib memakai musik berlisensi"],
    ))
    t2 = "\n".join(msg2.edits)
    check("klausul-hilang: saran konkret muncul",
          "Saran" in t2 and "musik berlisensi" in t2.replace("\\", ""), repr(t2[:300]))
    check("klausul-hilang: await_text + 1 percobaan",
          st2["stage"] == "await_text" and stub2.calls == 1, str(st2))

    st3, msg3, _s3 = run_error(RuntimeError("bug tak terduga"))
    check("non-CompilerError tetap aman + await_text",
          st3["stage"] == "await_text" and "tak terduga" in msg3.edits[0].replace("\\", ""), repr(msg3.edits[:1]))

    st4, msg4, _s4 = run_error(cc.CompilerError("", []))
    check("CompilerError tanpa pesan tetap terrender", "Impor S&K gagal" in msg4.edits[0], repr(msg4.edits[:1]))

    for i, m in enumerate((msg1, msg2, msg3, msg4)):
        safe = all(len(line) <= 4096 and not line.endswith("\\") for line in m.edits) and bool(m.edits)
        check(f"pesan error #{i + 1}: <=4096 & tanpa '\\' menggantung", safe, str([len(x) for x in m.edits]))
    check("silice error tanpa dummy", not DUMMIES, str(sorted(DUMMIES)))
except Exception as exc:  # noqa: BLE001
    check("bagian 6 berjalan", False, repr(exc))


# ---------------------------------------------------------------------------
# 7. Keputusan desain: retry milik user
# ---------------------------------------------------------------------------
section("7. konfirmasi: tidak ada auto-retry bot-side")
fn_by_name = {n.name: n for n in ast.walk(bot_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
import_fns = sorted(n for n in fn_by_name if n.startswith("campaign_import") or n == "campaign_cc")
blob = "\n".join(ast.get_source_segment(src_bot, fn_by_name[n]) or "" for n in import_fns)
blob += ast.get_source_segment(src_bot, BOT_DEFS.get("campaign_cc_completion")) or ""
blob += ast.get_source_segment(src_bot, BOT_DEFS.get("_CampaignChatAdapter")) or ""
check("MAX_RETRIES/RETRY_BACKOFF tidak dipakai jalur impor",
      "MAX_RETRIES" not in blob and "RETRY_BACKOFF" not in blob)
check("tidak ada loop percobaan di jalur impor",
      "for attempt" not in blob and "while attempt" not in blob)
check("tepat satu asyncio.to_thread (compile) di jalur impor",
      blob.count("asyncio.to_thread(") == 1, str(blob.count("asyncio.to_thread(")))
check("ulang = aksi user (camp:imp:again), bukan loop internal",
      'callback_data="camp:imp:again"' in src_bot and "again" in blob)
check("compiler internal: satu panggilan chat_fn, tanpa retry",
      "TIDAK ada retry internal" in (ROOT / "modules" / "campaign_compiler.py").read_text(encoding="utf-8")
      and "for attempt" not in (ROOT / "modules" / "campaign_compiler.py").read_text(encoding="utf-8"))

_no_net("nol koneksi jaringan selama seluruh uji")

config.CAMPAIGNS_DIR = ROOT / "data" / "campaigns"
socket.create_connection = _real_create_conn  # type: ignore[assignment]
socket.getaddrinfo = _real_getaddrinfo  # type: ignore[assignment]
shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL GREEN")
sys.exit(0)
