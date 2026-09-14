"""Verifikasi langkah 3: compiler S&K -> draf CampaignProfile (NOL NETWORK).

Semua "LLM" adalah FakeChat closure yang menyusun JSON dari klausul kanonik
hasil ``split_clauses``. Profil ditulis ke direktori sementara
(``config.CAMPAIGNS_DIR`` di-patch) sehingga data asli tidak tersentuh.

Jalankan:  .venv\\Scripts\\python.exe scratch\\verify_step3_compiler.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:  # pragma: no cover - kenyamanan konsol Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

import config  # noqa: E402
from modules import campaign_compiler as cc  # noqa: E402
from modules import campaign_policy as cp  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def expect_error(fn, contains=()) -> tuple[bool, str]:
    """Jalankan fn; (apakah CompilerError dan memuat semua teks, blob pesan)."""
    needles = [contains] if isinstance(contains, str) else list(contains)
    try:
        fn()
    except cc.CompilerError as exc:
        blob = str(exc) + "\n" + "\n".join(exc.details)
        missing = [n for n in needles if n and n not in blob]
        return (not missing, blob)
    except Exception as exc:  # noqa: BLE001 - exception lain = salah jenis
        return (False, f"BUKAN CompilerError: {type(exc).__name__}: {exc}")
    return (False, "tidak raise sama sekali")


# --------------------------------------------------------------------------- #
# Fixture S&K: 10 klausul campuran (durasi, hashtag, 2 topik, kompetitor exact,
# aturan akun, 2 klausul "gaib", CTA, klausul watermark-editor)
# --------------------------------------------------------------------------- #

CLAUSES = [
    "Durasi setiap klip harus antara 15 dan 60 detik.",                     # 0 clip_rules.duration_s
    "Setiap caption wajib menyertakan tagar #OriontClip dan tagar sponsor.",  # 1 post_rules
    "Dilarang menyinggung topik judi online termasuk taruhan olahraga.",      # 2 banned_topics
    "Konten tidak boleh membahas kripto, NFT, atau jual beli aset digital.",  # 3 banned_topics
    "Jangan menyebut merek kompetitor seperti Streamlabs di dalam klip.",     # 4 banned_exact
    "Akun YouTube harus punya minimal 1000 subscriber sebelum klip diposting.",  # 5 account_items
    "Gunakan nada yang positif dan suportif di setiap klip.",                 # 6 gaib -> out_of_control
    "Target audiens utama adalah 30% perempuan usia 25 sampai 34 tahun.",     # 7 gaib -> out_of_control
    "Akhiri setiap klip dengan ajakan subscribe yang natural.",               # 8 curation_note
    "Watermark editor harus tetap terlihat di pojok kanan bawah klip.",       # 9 flag + gate
]

RAW_SK = "\n".join(
    ["S&K Graeme:", "=================="]
    + [f"{i}) {text}" for i, text in enumerate(CLAUSES, start=1)]
    + ["Catatan:", "-"]
)

MAPPING = [
    (0, "clip_rules.duration_s"),
    (1, "post_rules.caption_required"),
    (2, "clip_rules.banned_topics"),
    (3, "clip_rules.banned_topics"),
    (4, "clip_rules.banned_exact"),
    (5, "account_items"),
    (8, "clip_rules.curation_note"),
    (9, "clip_rules.human_gates"),
]
OOC_IDX = [6, 7]


def variant(i: int) -> str:
    """Variasi teks klausul yang masih harus terikat ke klausul kanonik
    (prefix nomor, UPPERCASE, kutipel/spasi, tanpa titik akhir)."""
    text = CLAUSES[i]
    if i == 2:
        return f"3. {text}"
    if i == 3:
        return text.upper()
    if i == 4:
        return f'   "{text}"   '
    if i == 8:
        return text.rstrip(".")
    return text


def build_draft() -> dict:
    return {
        "schema_version": 2,
        "id": "Graeme Fund 2026/!",                 # uji sanitasi id
        "name": "Graeme Creator Fund 2026",
        "strictness": "",                           # uji default 'standard'
        "clip_rules": {
            "duration_s": {"min": 15, "max": 60},
            "banned_exact": ["streamlabs"],
            "banned_topics": [
                {"topic": "gambling", "regex": "judi"},                 # string -> wajib dipaksa list
                {"topic": "crypto_nft", "regex": ["kripto", "nft"]},
            ],
            "curation_note": "Prioritaskan klip yang menutup dengan ajakan subscribe.",
            "flag_requests": [
                {"id": "watermark_visible", "ask": "Apakah watermark editor terlihat?"}
            ],
            "human_gates": [
                {
                    "id": "confirm_watermark",
                    "text": "Konfirmasi watermark editor terlihat sebelum posting.",
                    "trigger": "llm_flag:watermark_visible",
                    "strict_mode": "ack_always",
                }
            ],
        },
        "post_rules": {"caption_required": {"all_of": ["#OriontClip"]}},
        "account_items": [CLAUSES[5]],
        "out_of_control": [CLAUSES[6], CLAUSES[7]],
    }


def payload_ok(drop=(), unaccounted=()) -> dict:
    """Jawaban LLM 'sempurna': semua klausul punya jejak."""
    drop, unacc = set(drop), set(unaccounted)
    mapped = [
        {"clause": variant(i), "dest": dest}
        for i, dest in MAPPING
        if i not in drop and i not in unacc
    ]
    ooc_idx = [i for i in OOC_IDX if i not in drop and i not in unacc]
    draft = build_draft()
    draft["out_of_control"] = [CLAUSES[i] for i in ooc_idx]
    return {
        "profile": draft,
        "mapped": mapped,
        "out_of_control": [CLAUSES[i] for i in ooc_idx],
        "unaccounted": [CLAUSES[i] for i in range(len(CLAUSES)) if i in unacc],
    }


def fake_chat_factory(payload_obj, *, fence: bool = False, raise_exc: Exception | None = None):
    """FakeChat closure — mencatat setiap panggilan."""
    state: dict = {"calls": []}

    def chat_fn(messages):
        state["calls"].append(messages)
        if raise_exc is not None:
            raise raise_exc
        text = (
            payload_obj
            if isinstance(payload_obj, str)
            else json.dumps(payload_obj, ensure_ascii=False)
        )
        return f"```json\n{text}\n```" if fence else text

    chat_fn.state = state  # type: ignore[attr-defined]
    return chat_fn


def run(payload_obj, *, fence: bool = False, raw: str = RAW_SK):
    fn = fake_chat_factory(payload_obj, fence=fence)
    return cc.compile_profile(raw, fn), fn


# --------------------------------------------------------------------------- #
section("0. higiene modul (netral jaringan, tanpa import bot)")
# --------------------------------------------------------------------------- #
src = (ROOT / "modules" / "campaign_compiler.py").read_text(encoding="utf-8")
banned = ["import bot", "import requests", "import httpx", "import urllib", "import socket",
          "import subprocess", "import openai", "http://", "https://"]
found = [b for b in banned if b in src]
check("tidak ada impor bot/jaringan/subprocess di compiler", not found, "; ".join(found))
check("semua akses LLM lewat chat_fn injeksi", "chat_fn(messages)" in src)

# --------------------------------------------------------------------------- #
section("1. split_clauses kanonik")
# --------------------------------------------------------------------------- #
clauses = cc.split_clauses(RAW_SK)
check("fixture -> tepat 10 klausul", len(clauses) == 10, f"{len(clauses)}: {clauses}")
check("penanda nomor/bullet dibuang, teks utuh", clauses == CLAUSES, json.dumps(clauses[0], ensure_ascii=False))
check("baris pendek (<=15 char) bukan klausul",
      all(c not in clauses for c in ("S&K Graeme:", "Catatan:", "=" * 18)))
check("deterministik: dua panggilan identik", cc.split_clauses(RAW_SK) == clauses)
check("idempoten: re-split hasil split", cc.split_clauses("\n".join(clauses)) == clauses)
check("CRLF == LF", cc.split_clauses(RAW_SK.replace("\n", "\r\n")) == clauses)
check("input kosong/None -> []",
      cc.split_clauses("") == [] and cc.split_clauses(None) == [] and cc.split_clauses(" \n\t ") == [])  # type: ignore[arg-type]
two_sentences = "Durasi klip maksimal 60 detik. Semua klip wajib melewati review manusia sebelum tayang."
check("baris panjang dipecah per kalimat", cc.split_clauses(two_sentences) == [
    "Durasi klip maksimal 60 detik.", "Semua klip wajib melewati review manusia sebelum tayang.",
], str(cc.split_clauses(two_sentences)))
check("angka desimal / persen tidak dipecah", cc.split_clauses("Target audiens 30% perempuan usia 25.1 tahun atau lebih.")
      == ["Target audiens 30% perempuan usia 25.1 tahun atau lebih."])
check("numbered_clauses 1-based", cc.numbered_clauses(RAW_SK)[:2] == [(1, CLAUSES[0]), (2, CLAUSES[1])])

# --------------------------------------------------------------------------- #
section("2. happy path compile_profile (1 panggilan, tanpa retry)")
# --------------------------------------------------------------------------- #
result, fake = run(payload_ok())
draft = result["profile"]
cov = result["coverage"]
calls = fake.state["calls"]  # type: ignore[attr-defined]
check("hanya SATU panggilan chat_fn (retry milik pemanggil)", len(calls) == 1, str(len(calls)))
check("build_messages -> [system, user]", [m["role"] for m in calls[0]] == ["system", "user"])
check("user prompt memuat klausul bernomor",
      all(f"{i}. {t}" in calls[0][1]["content"] for i, t in enumerate(clauses, 1)))
check("validate_profile lolos", cp.validate_profile(draft) == [], str(cp.validate_profile(draft)))
check("coverage lengkap = jumlah klausul",
      len(cov["mapped"]) + len(cov["out_of_control"]) + len(cov["unaccounted"]) == len(clauses),
      json.dumps({k: len(v) for k, v in cov.items()}, ensure_ascii=False))
check("8 klausul mapped", len(cov["mapped"]) == 8, str([m["dest"] for m in cov["mapped"]]))
check("2 klausul out_of_control verbatim", cov["out_of_control"] == [clauses[6], clauses[7]])
check("klausul mapped memakai teks KANONIK (bukan varian LLM)",
      [m["clause"] for m in cov["mapped"]] == [CLAUSES[i] for i, _d in MAPPING])
dests = {m["clause"]: m["dest"] for m in cov["mapped"]}
check("dest tercatat per klausul",
      dests[CLAUSES[0]] == "clip_rules.duration_s" and dests[CLAUSES[5]] == "account_items", str(dests))
check("varian fuzzy (prefix nomor / uppercase / kutipel / tanpa titik) tetap terikat",
      all(dests[CLAUSES[i]] == d for i, d in MAPPING if i in (2, 3, 4, 8)))
again, _ = run(payload_ok())
check("idempoten: compile dua kali -> profile & coverage identik",
      again["profile"] == draft and again["coverage"] == cov)
check("id draft disanitasi jadi slug aman", bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", draft["id"])), draft["id"])
check("id bebas separator path", "/" not in draft["id"] and "\\" not in draft["id"] and ".." not in draft["id"])
check("name non-kosong & dipotong maks", bool(draft["name"].strip()) and len(draft["name"]) <= cc.MAX_NAME_LEN, draft["name"])
check("strictness default 'standard' saat LLM tidak isi", draft["strictness"] == "standard", repr(draft.get("strictness")))
check("schema_version dipaksa ke versi policy",
      draft["schema_version"] == cc.PROFILE_SCHEMA_VERSION == 2)
check("regex string banned_topics dipaksa jadi list",
      all(isinstance(t["regex"], list) for t in draft["clip_rules"]["banned_topics"]))
check("out_of_control profil memuat klausul gaib verbatim",
      clauses[6] in draft["out_of_control"] and clauses[7] in draft["out_of_control"], str(draft["out_of_control"]))
check("llm_raw string mentah utuh", result["llm_raw"] == json.dumps(payload_ok(), ensure_ascii=False))
check("gate & flag hasil draf benar-benar berfungsi di policy",
      bool(cp.human_gates_for(draft, [["watermark_visible"]])) and bool(cp.llm_flag_verdicts(["watermark_visible"], draft)))
check("campaign_rules_text terisi untuk lane AI", "Durasi klip" in cp.campaign_rules_text(draft))
_, fenced = run(payload_ok(), fence=True)
check("payung ```json fence tetap terparse", len(fenced.state["calls"]) == 1 and  # type: ignore[attr-defined]
      cp.validate_profile(cc.compile_profile(RAW_SK, fake_chat_factory(payload_ok(), fence=True))["profile"]) == [])
prose = "Tentu, berikut hasilnya:\n" + json.dumps(payload_ok(), ensure_ascii=False) + "\nSemoga membantu!"
check("prose sebelum/sesama {...} terluar ditoleransi",
      cp.validate_profile(run(prose)[0]["profile"]) == [])
bare = json.dumps(build_draft(), ensure_ascii=False)
ok, blob = expect_error(lambda: run(bare), ["klausul hilang tanpa jejak", CLAUSES[0]])
check("jawaban tanpa bungkus 'profile' (profil langsung) tetap dibaca sbg draf", ok, blob[:140])

# --------------------------------------------------------------------------- #
section("3. JSON rusak / AI gagal -> CompilerError jelas")
# --------------------------------------------------------------------------- #
for label, bad in [
    ("tanpa JSON", "Maaf, saya tidak bisa memproses dokumen ini."),
    ("terpotong", '{"profile": {"id": "x", "clip_rules": '),
    ("kosong", ""),
    ("fence tanpa isi", "```json\n```"),
]:
    ok, blob = expect_error(lambda b=bad: cc.compile_profile(RAW_SK, fake_chat_factory(b)), "bukan JSON")
    check(f"JSON rusak ({label}) -> CompilerError jelas", ok, blob[:110])
raising = fake_chat_factory("diabaikan", raise_exc=RuntimeError("timeout 9router"))
ok, blob = expect_error(
    lambda: cc.compile_profile(RAW_SK, raising),
    "AI gagal menjawab",
)
check("chat_fn meledak -> CompilerError (bukan traceback telanjang)", ok, blob[:110])
check("tetap 1 panggilan meski chat_fn meledak (tanpa retry)",
      len(raising.state["calls"]) == 1, str(len(raising.state["calls"])))  # type: ignore[attr-defined]
ok, blob = expect_error(lambda: cc.compile_profile(RAW_SK, fake_chat_factory(payload_ok() | {"profile": "salah"})), "bukan JSON object")
check("bagian 'profile' bukan dict -> CompilerError", ok, blob[:140])
ok, blob = expect_error(lambda: cc.compile_profile("S&K:\n- singkat", fake_chat_factory("{}")), "tidak ada klausul terbaca")
check("S&K tanpa klausul -> CompilerError sebelum panggil LLM", ok, blob[:110])
ok, blob = expect_error(lambda: cc.compile_profile(RAW_SK, "bukan fungsi"), "callable")  # type: ignore[arg-type]
check("chat_fn bukan callable -> CompilerError", ok, blob[:110])
state_probe: dict = {"n": 0}
def counting_chat(messages):
    state_probe["n"] += 1
    return "bukan json"
expect_error(lambda: cc.compile_profile(RAW_SK, counting_chat), "bukan JSON")
check("TIDAK ada retry internal (1 panggilan meski gagal)", state_probe["n"] == 1, str(state_probe["n"]))

# --------------------------------------------------------------------------- #
section("4. rule kind karangan LLM ditolak (anti-halusinasi)")
# --------------------------------------------------------------------------- #
p1 = payload_ok()
p1["profile"]["clip_rules"]["deteksi_watermark"] = [{"topic": "watermark", "regex": ["watermark"]}]
ok, blob = expect_error(lambda: run(p1), ["draf profil ditolak validator", "deteksi_watermark"])
check("kind karangan 'deteksi_watermark' ditolak + namanya disebut", ok, blob[:200])
check("pesan ikut memuat daftar kind/field sah", "banned_exact" in blob and "human_gates" in blob, blob[-160:])
p2 = payload_ok()
p2["profile"]["post_rules"]["auto_reply_komentar"] = True
ok, blob = expect_error(lambda: run(p2), "auto_reply_komentar")
check("field post_rules karangan juga ditolak", ok, blob[:160])
p3 = payload_ok(unaccounted=[9])
p3["profile"]["clip_rules"]["human_gates"] = [{"id": "g1", "text": "cek manual", "trigger": "llm_flag:flag_tiada"}]
ok, blob = expect_error(lambda: run(p3), "flag_tiada")
check("gate merujuk flag tak dideklarasikan -> ditolak validator", ok, blob[:200])
p4 = payload_ok()
p4["profile"]["clip_rules"]["duration_s"] = {"min": 90, "max": 60}
ok, blob = expect_error(lambda: run(p4), "melebihi max")
check("durasi min>max ditolak", ok, blob[:160])

# --------------------------------------------------------------------------- #
section("5. coverage gate (anti silent-drop)")
# --------------------------------------------------------------------------- #
ok, blob = expect_error(lambda: cc.compile_profile(RAW_SK, fake_chat_factory(payload_ok(drop=[9]))),
                        ["klausul hilang tanpa jejak", CLAUSES[9]])
check("klausul hilang tanpa jejak -> pesan menyebut teks klausul", ok, blob[:220])
ok, blob = expect_error(lambda: cc.compile_profile(RAW_SK, fake_chat_factory(payload_ok(drop=[6]))),
                        ["klausul hilang tanpa jejak", CLAUSES[6]])
check("klausul out_of_control yang ikut hilang ketahuan juga", ok, blob[:220])
res_unacc, _ = run(payload_ok(unaccounted=[6, 7]))
check("jejak lewat 'unaccounted' TIDAK error (transparan, bukan drop)",
      res_unacc["coverage"]["unaccounted"] == [clauses[6], clauses[7]], str(res_unacc["coverage"]["unaccounted"]))
check("unaccounted tetap tercatat di profil", clauses[6] in res_unacc["profile"]["out_of_control"])
check("unaccounted tidak dihitung sebagai mapped", len(res_unacc["coverage"]["mapped"]) == 8)
lazy = payload_ok()
lazy["mapped"] = [{"clause": str(i + 1), "dest": d} for i, d in MAPPING]
ok, blob = expect_error(lambda: run(lazy), "klausul hilang tanpa jejak")
check("jawaban hanya nomor (tanpa teks klausul) -> ditolak", ok, blob[:160])
half = payload_ok()
half["mapped"] = half["mapped"][:4]
half["out_of_control"] = []
half["unaccounted"] = []
ok, blob = expect_error(lambda: run(half), ["klausul hilang tanpa jejak", CLAUSES[8]])
check("setengah jawaban -> Klausul sisa dilaporkan sekaligus", ok, blob[:220])

# --------------------------------------------------------------------------- #
section("6. prompt divariasikan RUNTIME dari campaign_policy (anti drift)")
# --------------------------------------------------------------------------- #
sysprompt = cc.build_messages(RAW_SK)[0]["content"]
kinds = set(cp.CHECK_REGISTRY)
absent_kinds = sorted(k for k in kinds if not re.search(rf"\b{re.escape(k)}\b", sysprompt))
check("system prompt memuat SELURUH nama kind CHECK_REGISTRY", bool(kinds) and not absent_kinds, str(absent_kinds))
fields = cc.profile_field_map()
for layer, source in [
    ("clip_rules", cp._KNOWN_CLIP_KEYS),
    ("post_rules", cp._KNOWN_POST_KEYS),
    ("flag_request", cp._KNOWN_FLAG_REQUEST_KEYS),
    ("human_gate", cp._KNOWN_HUMAN_GATE_KEYS),
    ("strictness", cp._VALID_STRICTNESS),
    ("strict_mode", cp._VALID_STRICT_MODES),
]:
    names = set(fields[layer])
    check(f"field '{layer}' sama persis dengan sumber validasi policy", names == set(source), str(sorted(names ^ set(source))))
    missing = sorted(n for n in names if n not in sysprompt)
    check(f"prompt memuat field '{layer}'", bool(names) and not missing, str(missing))
core_top = {"schema_version", "id", "name", "strictness", "clip_rules", "post_rules", "account_items", "out_of_control"}
check("field top-level inti terderive (id/name/strictness/manual lists)", core_top <= set(fields["top"]),
      str(sorted(core_top - set(fields["top"]))))
NEW_KIND, NEW_CLIP_KEY = "uji_kind_baru", "uji_field_baru"
cp.CHECK_REGISTRY[NEW_KIND] = {"class": "warn", "desc": "kemampuan uji yang ditambahkan runtime"}
cp._KNOWN_CLIP_KEYS.add(NEW_CLIP_KEY)
try:
    grown = cc.build_messages(RAW_SK)[0]["content"]
    check("kind baru di registry otomatis masuk prompt (tanpa edit compiler)", NEW_KIND in grown, NEW_KIND)
    check("field clip_rules baru otomatis masuk prompt", NEW_CLIP_KEY in grown, NEW_CLIP_KEY)
    check("rule_kinds()/profile_field_map() live", NEW_KIND in cc.rule_kinds() and NEW_CLIP_KEY in set(cc.profile_field_map()["clip_rules"]))
finally:
    cp.CHECK_REGISTRY.pop(NEW_KIND, None)
    cp._KNOWN_CLIP_KEYS.discard(NEW_CLIP_KEY)
check("prompt memuat larangan mengarang + kewajiban verbatim",
      "DILARANG MENGARANG" in sysprompt and "verbatim" in sysprompt and "HANYA JSON" in sysprompt.replace("HANYA dengan", "HANYA JSON"))
check("prompt memuat instruksi out_of_control untuk klausul ambigu", "out_of_control" in sysprompt and "ambigu" in sysprompt)

# --------------------------------------------------------------------------- #
section("7. save_draft (dir sementara, tabrakan id -> -2/-3)")
# --------------------------------------------------------------------------- #
tmp = Path(tempfile.mkdtemp(prefix="orion_campaigns_"))
orig_dir = config.CAMPAIGNS_DIR
config.CAMPAIGNS_DIR = tmp
try:
    r1, _ = run(payload_ok())
    pid1 = cc.save_draft(r1)
    check("save_draft mengembalikan id final + file tertulis", pid1 == r1["profile"]["id"] and (tmp / f"{pid1}.json").exists(), pid1)
    r2, _ = run(payload_ok())
    pid2 = cc.save_draft(r2)
    check("tabrakan id -> akhiran -2", pid2 == f"{pid1}-2", pid2)
    pid3 = cc.save_draft(r2)
    check("tabrakan lagi -> -3 (rantai suffix deterministik, id draf tak diubah)",
          pid3 == f"{pid1}-3" and r2["profile"]["id"] == pid1, f"{pid3}/{r2['profile']['id']}")
    saved = cp.load_profile(pid2)
    check("profil terbaca kembali & lolos validator", cp.validate_profile(saved) == [] and saved["id"] == pid2)
    check("isi profil utuh (durasi, topics, gate)",
          saved["clip_rules"]["duration_s"] == {"min": 15, "max": 60}
          and len(saved["clip_rules"]["banned_topics"]) == 2
          and saved["clip_rules"]["human_gates"][0]["trigger"] == "llm_flag:watermark_visible")
    listed = {p["id"] for p in cp.list_profiles()}
    check("list_profiles melihat ketiganya", {pid1, pid2, pid3} <= listed, str(sorted(listed)))
    check("unique=False menimpa id yang sama", cc.save_draft(r1, unique=False) == pid1 and len(cp.list_profiles()) == 3)
    pid4 = cc.save_draft({"profile": {"id": "BAD ID/!!/../x", "name": "x", "schema_version": 2, "strictness": "strict"}})
    check("id mentah dari pemanggil disanitasi sebelum menulis", bool(pid4) and "/" not in pid4 and ".." not in pid4, pid4)
    ok, blob = expect_error(lambda: cc.save_draft({"profile": {"id": "x", "name": "y"}}), "draf profil ditolak")
    check("profil tidak valid -> CompilerError (bukan ValueError telanjang)", ok, blob[:150])
    ok, blob = expect_error(lambda: cc.save_draft("bukan dict"), "wajib dict")
    check("result bukan dict -> CompilerError", ok, blob[:110])
    check("save_draft tidak mengubah file sumber S&K / result lainnya",
          r1["coverage"] == cov and "coverage" in r1)
finally:
    config.CAMPAIGNS_DIR = orig_dir
    shutil.rmtree(tmp, ignore_errors=True)
check("direktori sementara dibersihkan", not tmp.exists())

# --------------------------------------------------------------------------- #
section("8. summarize_for_user")
# --------------------------------------------------------------------------- #
summary = cc.summarize_for_user(result)
print("-" * 70)
print(summary)
print("-" * 70)
want = [
    "Graeme Creator Fund 2026", f"id: {draft['id']}", "strictness: standard",
    "Durasi klip: 15-60 detik", "#OriontClip", '"streamlabs"', "gambling", "crypto_nft",
    "ajakn" if False else "Prioritaskan klip", "flag `watermark_visible`",
    "gate `confirm_watermark`", "llm_flag:watermark_visible", "ack_always",
    "1000 subscriber", "nada yang positif", "Coverage: 8/10", "2 out_of_control", "0 tanpa tujuan",
]
missing = [w for w in want if w not in summary]
check("ringkasan memuat field terisi per layer + gate/flag + akun + out_of_control", not missing, str(missing))
check("ringkasan tidak menyebut mutu rendah saat coverage baik", "mutu rendah" not in summary)
lq = payload_ok(unaccounted=[1, 2, 3, 4, 5, 6, 7, 8])
lq["profile"]["out_of_control"] = []
res_lq, _ = run(lq)
lq_summary = cc.summarize_for_user(res_lq)
check(">60% tanpa tujuan -> peringatan 'profil mutu rendah'",
      bool(cc.quality_warnings(res_lq)) and "profil mutu rendah" in lq_summary, str(cc.quality_warnings(res_lq)))
check("peringatan mutu tampil di ringkasan (dengan hitungan)", "⚠" in lq_summary and "8 dari 10" in lq_summary)
check("bagian 'tanpa tujuan' terpisah dari out_of_control",
      "Klausul yang tidak sempat dipetakan AI" in lq_summary and "8 tanpa tujuan" in lq_summary, str(res_lq["coverage"].keys()))
check("quality_warnings kosong saat semua mapped",
      cc.quality_warnings({"coverage": {"mapped": [1] * 10, "out_of_control": [], "unaccounted": []}}) == [])
side = payload_ok()
side["profile"]["catatan_tambahan"] = "hal lain"
res_side, _ = run(side)
check("field top-level asing dibuang + dicatat di warnings",
      "catatan_tambahan" not in res_side["profile"]
      and any("di luar denah dibuang" in w for w in res_side["warnings"]), str(res_side["warnings"]))
check("warnings juga tampil di ringkasan", any("di luar denah dibuang" in line for line in cc.summarize_for_user(res_side).splitlines()))
check("summarize tahan untuk profil mentah (tanpa coverage)", "Draf profil" in cc.summarize_for_user(draft))
ok, blob = expect_error(lambda: cc.summarize_for_user("x"), "wajib dict")  # type: ignore[arg-type]
check("summarize input salah -> CompilerError", ok, blob[:110])

# --------------------------------------------------------------------------- #
section("9. sanity lintas-lane: draf tetap dipakai policy persis seperti profil manual")
# --------------------------------------------------------------------------- #
probe = cp.evaluate_moment(_m := type("M", (), {"start": 0.0, "end": 120.0, "caption": "clip baru"})(),
                           [{"start": 0.0, "end": 120.0, "text": "ayo pasang taruhan judi di streamlabs sekarang"}],
                           draft)
levels = {v.code: v.level for v in probe}
check("duration_range fail + banned_exact fail + banned_topics warn pada draf compiler",
      levels.get("duration_range") == "fail" and levels.get("banned_exact") == "fail" and levels.get("banned_topics") == "warn",
      str(levels))
caps = cp.check_caption("cek aja", draft)
check("caption_required warn dengan bukti terstruktur (kontrak bot)",
      bool(caps) and caps[0].level == "warn" and isinstance(caps[0].evidence, dict) and "#OriontClip" in caps[0].evidence["missing"],
      str(caps))
intro = cp.campaign_intro_lines(draft)
check("campaign_intro_lines menampilkan out_of_control dari draf", any("nada yang positif" in line for line in intro), str(intro))

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL PASS")
sys.exit(0)
