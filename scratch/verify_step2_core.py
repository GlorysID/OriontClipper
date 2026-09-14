"""Pure verification untuk kontrak LANGKAH-2 campaign compliance.

Menutupi modules/campaign_policy.py (v2: curation_note, flag_requests, human_gates,
campaign_rules_text, llm_flag_verdicts, human_gates_for, record_override,
read_overrides_tail) dan modules/ai_analyzer.py (ClipMoment.risk_flags, parsing
toleran, threading campaign_rules_text).

TANPA network, TANPA Telegram, TANPA tulis ke data/ (audit trail di-redirect ke
dir sementara). Jalankan:
    .venv\\Scripts\\python.exe scratch\\verify_step2_core.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import campaign_policy as cp  # noqa: E402
from modules import ai_analyzer as ai  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def prof(clip_rules=None, strictness="standard", pid="t", name="T"):
    return {
        "schema_version": 2,
        "id": pid,
        "name": name,
        "strictness": strictness,
        "profile_version": 1,
        "clip_rules": clip_rules
        if clip_rules is not None
        else {"duration_s": {"min": 15, "max": 60}, "banned_exact": [], "banned_topics": []},
        "post_rules": {"caption_required": None, "platforms": {}},
        "account_items": [],
        "out_of_control": [],
    }


GOOD_CLIP_RULES = {
    "duration_s": {"min": 15, "max": 60},
    "banned_exact": [],
    "banned_topics": [{"topic": "adult", "regex": ["\\bnsfw\\b"]}],
    "curation_note": "Prioritaskan segmen webinar/podcast.",
    "flag_requests": [
        {"id": "multi_speaker", "ask": "Apakah penutur bukan Graeme dominan?"},
        {"id": "off_topic", "ask": "Apakah klip keluar dari topik partner?"},
    ],
    "human_gates": [
        {
            "id": "graeme_majority",
            "text": "Konfirmasi Graeme penutur dominan.",
            "trigger": "llm_flag:multi_speaker",
            "strict_mode": "ack_always",
        },
        {"id": "style_fit", "text": "Cek kecocokan gaya.", "trigger": "always", "strict_mode": "warn"},
    ],
}


# --------------------------------------------------------------------------- #
section("0. registry v2")
# --------------------------------------------------------------------------- #
check("CHECK_REGISTRY punya llm_flag class warn", cp.CHECK_REGISTRY.get("llm_flag", {}).get("class") == "warn")
check("CHECK_REGISTRY punya human_gate class warn", cp.CHECK_REGISTRY.get("human_gate", {}).get("class") == "warn")
check(
    "CHECK_REGISTRY entri v2 punya desc",
    all(bool(cp.CHECK_REGISTRY[k].get("desc")) for k in ("llm_flag", "human_gate")),
)

# --------------------------------------------------------------------------- #
section("1. validate_profile: field opsional baru")
# --------------------------------------------------------------------------- #
legacy = prof()
check("profil LAMA tanpa field v2 lolos validate", not cp.validate_profile(legacy), str(cp.validate_profile(legacy)))
check(
    "profil v2 lengkap lolos validate",
    not cp.validate_profile(prof(dict(GOOD_CLIP_RULES))),
    str(cp.validate_profile(prof(dict(GOOD_CLIP_RULES)))),
)
check(
    "profil v2 tanpa human_gates lolos",
    not cp.validate_profile(prof({**GOOD_CLIP_RULES, "human_gates": []})),
)
check(
    "clip_rules=null tetap lolos",
    not cp.validate_profile(prof(None)),
)

bad_cases = {
    "flag_requests bukan list": ({"flag_requests": {"id": "x", "ask": "y"}}, "flag_requests"),
    "flag id haram (spasi/kapital)": (
        {"flag_requests": [{"id": "Multi Speaker", "ask": "y"}]},
        "id",
    ),
    "flag id kepanjangan (>32)": ({"flag_requests": [{"id": "a" * 33, "ask": "y"}]}, "id"),
    "flag ask kosong": ({"flag_requests": [{"id": "ok_flag", "ask": "   "}]}, "ask"),
    "flag field tak dikenal": ({"flag_requests": [{"id": "ok_flag", "ask": "y", "when": "z"}]}, "tak dikenal"),
    "flag id duplikat": (
        {"flag_requests": [{"id": "dup", "ask": "a"}, {"id": "dup", "ask": "b"}]},
        "duplikat",
    ),
    "curation_note bukan string": ({"curation_note": ["a"]}, "curation_note"),
    "human_gates bukan list": ({"human_gates": {"id": "g"}}, "human_gates"),
    "gate trigger format salah": (
        {"human_gates": [{"id": "g", "text": "t", "trigger": "llm-flag:multi_speaker"}]},
        "format salah",
    ),
    "gate trigger kosong": ({"human_gates": [{"id": "g", "text": "t", "trigger": ""}]}, "trigger"),
    "gate tanpa text": ({"human_gates": [{"id": "g", "trigger": "always"}]}, "text"),
    "gate strict_mode haram": (
        {"human_gates": [{"id": "g", "text": "t", "trigger": "always", "strict_mode": "block"}]},
        "strict_mode",
    ),
    "gate field tak dikenal": (
        {"human_gates": [{"id": "g", "text": "t", "trigger": "always", "on_fail": "drop"}]},
        "tak dikenal",
    ),
    "gate trigger merujuk flag tak dideklarasikan": (
        {"flag_requests": [{"id": "known", "ask": "a"}], "human_gates": [
            {"id": "g", "text": "t", "trigger": "llm_flag:ghost"}]},
        "tidak dideklarasikan",
    ),
    "gate llm_flag tanpa flag_requests sama sekali": (
        {"human_gates": [{"id": "g", "text": "t", "trigger": "llm_flag:ghost"}]},
        "tidak dideklarasikan",
    ),
    "rule kind v2 salah ketik": ({"flag_request": [{"id": "a", "ask": "b"}]}, "tak dikenal"),
}
for label, (rules, needle) in bad_cases.items():
    base = {
        "duration_s": {"min": 15, "max": 60},
        "banned_exact": [],
        "banned_topics": [{"topic": "adult", "regex": ["\\bnsfw\\b"]}],
    }
    errs = cp.validate_profile(prof({**base, **rules}))
    check(f"BENTUK SALAH ditolak: {label}", any(needle in e for e in errs), "; ".join(errs))

try:
    cp.save_profile(prof({"flag_requests": "rusak"}))
    check("save_profile menolak bentuk salah", False)
except ValueError:
    check("save_profile menolak bentuk salah", True)
except Exception as exc:  # noqa: BLE001
    check("save_profile menolak bentuk salah", False, repr(exc))

# --------------------------------------------------------------------------- #
section("2. campaign_rules_text")
# --------------------------------------------------------------------------- #
graeme = cp.load_profile("graeme-clipinfluence")
bebas = cp.load_profile("bebas")
t1 = cp.campaign_rules_text(graeme)
t2 = cp.campaign_rules_text(graeme)
check("rules_text deterministik 2x identik", t1 == t2 and bool(t1))
check("rules_text bebas kosong", cp.campaign_rules_text(bebas) == "", repr(cp.campaign_rules_text(bebas)))
check("rules_text profil tanpa aturan actionable kosong",
      cp.campaign_rules_text(prof({"duration_s": None, "banned_exact": [], "banned_topics": []})) == "")
check("rules_text profil rusak -> kosong", cp.campaign_rules_text(None) == "" and cp.campaign_rules_text("x") == "")
for needle in ("ATURAN CAMPAIGN", "standard", "15-60", "onlyfans_sexual", "multi_speaker",
               "penutur bukan Graeme", "webinar", "risk_flags"):
    check(f"rules_text graeme memuat {needle!r}", needle.lower() in t1.lower())
check("rules_text tidak memuat id gate (hanya kontrak AI)", "style_fit" not in t1)
no_note = prof({k: v for k, v in GOOD_CLIP_RULES.items() if k != "curation_note"})
txt_no_note = cp.campaign_rules_text(no_note)
check("rules_text tetap ada tanpa curation_note", "multi_speaker" in txt_no_note)
note_only = prof({"duration_s": None, "banned_exact": [], "banned_topics": [], "curation_note": "Fokus studi kasus"})
check("curation_note sendiri = actionable", "studi kasus" in cp.campaign_rules_text(note_only))
ws_txt = cp.campaign_rules_text(prof({**GOOD_CLIP_RULES, "curation_note": "baris1\n   baris2"}))
check("curation_note whitespace dinormalkan", "Arahan kurasi: baris1 baris2" in ws_txt, ws_txt[-60:])

# --------------------------------------------------------------------------- #
section("3. llm_flag_verdicts")
# --------------------------------------------------------------------------- #
std = prof(dict(GOOD_CLIP_RULES))
v_gate = cp.llm_flag_verdicts(["multi_speaker"], std)
check("flag ber-gate -> warn", [v.level for v in v_gate] == ["warn"], str(v_gate))
check("code = llm_flag:<flag>", [v.code for v in v_gate] == ["llm_flag:multi_speaker"])
check("message = teks gate", v_gate[0].message == "Konfirmasi Graeme penutur dominan.")
v_open = cp.llm_flag_verdicts(["unknown_flag"], std)
check("flag tanpa gate (standard) -> warn 'AI menandai'", v_open[0].level == "warn"
      and v_open[0].message == "AI menandai: unknown_flag", str(v_open))
loose_profile = prof(dict(GOOD_CLIP_RULES), strictness="loose")
v_loose = cp.llm_flag_verdicts(["unknown_flag"], loose_profile)
check("flag tanpa gate (loose) -> info", v_loose[0].level == "info", str(v_loose))
check("flag dikenal dgn gate tetap warn walau loose",
      cp.llm_flag_verdicts(["multi_speaker"], loose_profile)[0].level == "warn")
messy = cp.llm_flag_verdicts(["Multi Speaker??", "multi_speaker", "", None, 7, "off topic"], std)
check("input kotor disanitasi + dedupe (kapital/ '?' sama dg slug)",
      [v.code for v in messy] == ["llm_flag:multi_speaker", "llm_flag:off_topic"], str([v.code for v in messy]))
never_fail_inputs = [
    None, [], "multi_speaker", ["multi_speaker"], [object()], ["***"],
    [{"id": "x"}], ["a" * 200],
]
check(
    "llm_flag_verdicts TAK PERNAH fail (berbagai input)",
    all(all(v.level != "fail" for v in cp.llm_flag_verdicts(inp, p))
        for inp in never_fail_inputs for p in (std, loose_profile, prof(), {}, None)),
)
check("profil kosong -> flag jadi warn 'AI menandai'",
      cp.llm_flag_verdicts(["x"], {})[0].message == "AI menandai: x")
two_gates = prof({**GOOD_CLIP_RULES, "human_gates": [
    {"id": "g1", "text": "cek 1", "trigger": "llm_flag:multi_speaker", "strict_mode": "ack_always"},
    {"id": "g2", "text": "cek 2", "trigger": "llm_flag:multi_speaker", "strict_mode": "warn"},
]})
check("satu flag bisa memicu beberapa gate",
      [v.message for v in cp.llm_flag_verdicts(["multi_speaker"], two_gates)] == ["cek 1", "cek 2"]
      and all(v.level == "warn" for v in cp.llm_flag_verdicts(["multi_speaker"], two_gates)))

# --------------------------------------------------------------------------- #
section("4. human_gates_for")
# --------------------------------------------------------------------------- #
g_none = cp.human_gates_for(std, [])
check("tanpa flags -> hanya gate 'always'", [v.code for v in g_none] == ["human_gate:style_fit"], str(g_none))
check("strict_mode warn -> info", g_none[0].level == "info")
g_hit = cp.human_gates_for(std, [[], ["multi_speaker"]])
check("gate terpicu by flag (urut deklarasi profil)",
      [v.code for v in g_hit] == ["human_gate:graeme_majority", "human_gate:style_fit"],
      str([v.code for v in g_hit]))
check("strict_mode ack_always -> warn", [v for v in g_hit if v.code == "human_gate:graeme_majority"][0].level == "warn")
check("gate tak relevan tidak ikut",
      all(v.code != "human_gate:ghost" for v in cp.human_gates_for(std, [["off_topic"]])))
check("toleran moments_flags None/str/bukan-list",
      [v.code for v in cp.human_gates_for(std, None)] == ["human_gate:style_fit"]
      and len(cp.human_gates_for(std, "multi_speaker")) == 2
      and [v.code for v in cp.human_gates_for(std, [None])] == ["human_gate:style_fit"])
check("human_gates_for tidak pernah fail",
      all(v.level != "fail" for p in (std, loose_profile, prof(), {}, None)
          for f in ([None], "x", [[1, 2]], [["multi_speaker"]])
          for v in cp.human_gates_for(p, f)))
check("gate graeme seed: always + berpicu",
      len(cp.human_gates_for(graeme, [["multi_speaker"]])) == 2
      and len(cp.human_gates_for(graeme, [[]])) == 1)

# --------------------------------------------------------------------------- #
section("5. record_override / read_overrides_tail")
# --------------------------------------------------------------------------- #
tmp_root = Path(tempfile.mkdtemp(prefix="orion_step2_"))
real_campaigns_dir = cp.config.CAMPAIGNS_DIR
try:
    cp.config.CAMPAIGNS_DIR = tmp_root

    cp.record_override(123, "456", "graeme-clipinfluence", 2, 0, 10.5, 40.25,
                       ["llm_flag:multi_speaker", "human_gate:style_fit"], "oke   lah")
    cp.record_override(123, 456, "graeme-clipinfluence", 2, 1, 60, 90, [], None)
    path = tmp_root / "overrides" / "graeme-clipinfluence.jsonl"
    check("file JSONL dibuat di <campaigns>/overrides/", path.exists(), str(path))
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    check("setiap baris JSON valid", all(json.loads(ln) for ln in lines), str(lines))
    rec = json.loads(lines[0])
    check("field audit lengkap",
          {"ts", "chat_id", "user_id", "campaign_id", "profile_version", "clip_index",
           "moment_start", "moment_end", "verdict_codes", "note"} <= set(rec), str(sorted(rec)))
    check("note whitespace dinormalkan", rec["note"] == "oke lah", repr(rec["note"]))
    check("user_id string angka dikutif ke int", rec["user_id"] == 456)
    tail = cp.read_overrides_tail("graeme-clipinfluence", 1)
    check("read_overrides_tail n=1 -> paling baru", len(tail) == 1 and tail[0]["clip_index"] == 1, str(tail))
    tail2 = cp.read_overrides_tail("graeme-clipinfluence", 5)
    check("read_overrides_tail balik kronologis", [r["clip_index"] for r in tail2] == [0, 1], str(tail2))
    check("read_overrides_tail default n=5", len(cp.read_overrides_tail("graeme-clipinfluence")) == 2)
    check("read_overrides_tail file hilang -> []", cp.read_overrides_tail("tidakada") == [])
    check("read_overrides_tail n=0/negatif -> []",
          cp.read_overrides_tail("graeme-clipinfluence", 0) == []
          and cp.read_overrides_tail("graeme-clipinfluence", -3) == [])

    # baris rusak di tengah tidak boleh menghilangkan yang sehat
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{baris rusak\n")
    cp.record_override(1, 1, "graeme-clipinfluence", 2, 9, 0, 5, ["c"])
    tail3 = cp.read_overrides_tail("graeme-clipinfluence", 3)
    check("baris rusak dilewati, record sehat tetap terbaca urut",
          [r["clip_index"] for r in tail3] == [0, 1, 9], str([r["clip_index"] for r in tail3]))
    tail1 = cp.read_overrides_tail("graeme-clipinfluence", 1)
    check("n=1 tetap memberi record terakhir meski baris sebelumnya rusak",
          [r["clip_index"] for r in tail1] == [9], str(tail1))

    # id tidak aman / tipe aneh -> tidak raise, tidak nulis file baru
    before = {p.name for p in (tmp_root / "overrides").iterdir()}
    for evil in ("../../evil", "a/b", "", None, 12.5, "x" * 200, "UPPER-id"):
        cp.record_override(1, 2, evil, 1, 0, 0, 1, ["c"], "n")
        check(f"campaign_id {evil!r} tidak ditulis & tidak raise", cp.read_overrides_tail(evil) == [])
    after = {p.name for p in (tmp_root / "overrides").iterdir()}
    check("tidak ada file audit bocor di luar slug", before == after, str(sorted(after)))

    # rusaki direktori -> record_override tetap tidak raise
    shutil.rmtree(tmp_root / "overrides")
    (tmp_root / "overrides").write_text("ini file, bukan folder", encoding="utf-8")
    cp.record_override(1, 2, "graeme-clipinfluence", 2, 0, 0, 5, ["c"], "rusak")
    check("record_override tidak raise saat dir rusaki", True)
    (tmp_root / "overrides").unlink()
    (tmp_root / "broken.jsonl").mkdir()
    check("read_overrides_tail pada path folder -> []", cp.read_overrides_tail("broken") == [])
finally:
    cp.config.CAMPAIGNS_DIR = real_campaigns_dir
    shutil.rmtree(tmp_root, ignore_errors=True)

check("verifier tidak menyentuh data/campaigns/ asli",
      not (real_campaigns_dir / "overrides").exists(), str(real_campaigns_dir / "overrides"))

# --------------------------------------------------------------------------- #
section("6. ai_analyzer: ClipMoment.risk_flags + validate_moments")
# --------------------------------------------------------------------------- #
check("ClipMoment lama (positional) tetap bisa dibuat",
      ai.ClipMoment(1.0, 20.0, "HOOK").risk_flags == [])
m_no = ai.validate_moments([{"start": 1, "end": 30, "hook": "H"}], 200.0)
check("risk_flags hilang -> []", m_no[0].risk_flags == [])
m_dirty = ai.validate_moments(
    [{"start": 1, "end": 30, "hook": "H", "risk_flags": ["Multi Speaker??", "off_topic", "", None, 5,
                                                        "a", "b", "c", "d", "e"]}],
    200.0,
)
check("sanitizer slug OK", m_dirty[0].risk_flags[:2] == ["multi_speaker", "off_topic"], str(m_dirty[0].risk_flags))
check("maks 6 flag per momen", len(m_dirty[0].risk_flags) == 6, str(m_dirty[0].risk_flags))
check("risk_flags bukan list -> [] tanpa gagal",
      ai.validate_moments([{"start": 1, "end": 30, "hook": "H", "risk_flags": "ya"}], 200.0)[0].risk_flags == [])
check("risk_flags dict/None -> []",
      ai.validate_moments([{"start": 1, "end": 30, "hook": "H", "risk_flags": {"a": 1}}], 200.0)[0].risk_flags == [])
check("flag invalid sendiri tidak membatalkan momen",
      len(ai.validate_moments([{"start": 1, "end": 30, "hook": "H", "risk_flags": ["***", "---"]}], 200.0)) == 1)
check("flag duplikat dikompres",
      ai.validate_moments([{"start": 1, "end": 30, "hook": "H",
                            "risk_flags": ["Off Topic", "off  topic"]}], 200.0)[0].risk_flags == ["off_topic"])

cap_default = ai.validate_moments([{"start": 1, "end": 30, "hook": "HOOK SATU"}], 200.0)[0].caption
cap_campaign = ai.validate_moments([{"start": 1, "end": 30, "hook": "HOOK SATU"}], 200.0,
                                   campaign_active=True)[0].caption
check("default tetap suntik hashtag generik", "#trending" in cap_default and "#fyp" in cap_default, cap_default)
check("campaign_active=True tetap suppress hashtag generik",
      cap_campaign == "HOOK SATU" and "#" not in cap_campaign, cap_campaign)

check("slug ai_analyzer == slug campaign_policy (anti-drift)",
      all(ai.sanitize_flag(x) == cp.flag_slug(x) for x in
          ["Multi Speaker??", "  a  b ", "Éxito Uno", "***", "x" * 50, "", None, "ok_1"]))

# --------------------------------------------------------------------------- #
section("7. threading campaign_rules_text (tanpa network)")
# --------------------------------------------------------------------------- #
MOCK = json.dumps({
    "clips": [
        {"rank": 1, "viral_score": 90, "start": 0.0, "end": 30.0, "first_words": "satu dua tiga",
         "last_words": "empat lima enam", "hook": "HOOK A", "topic": "t", "alasan": "a",
         "bgm_mood": "upbeat", "caption": "cap", "risk_flags": ["Multi Speaker"]},
        {"rank": 2, "viral_score": 80, "start": 40.0, "end": 60.0, "first_words": "tujuh delapan",
         "last_words": "sembilan sepuluh", "hook": "HOOK B", "topic": "u", "alasan": "b",
         "bgm_mood": "chill", "caption": "cap2"},
    ]
})


class FakeAnalyzer(ai.AIAnalyzer):
    def __init__(self) -> None:
        super().__init__(api_key="")
        self.captured: list[tuple[str, str | None]] = []

    def _request_chat_completion(self, transcript, response_format_enabled, campaign_rules_text=None):
        self.captured.append((transcript, campaign_rules_text))
        return MOCK


segs = [
    {"start": 0.0, "end": 15.0, "text": "satu dua tiga empat lima enam"},
    {"start": 15.0, "end": 30.0, "text": "tujuh delapan sembilan sepuluh"},
    {"start": 40.0, "end": 60.0, "text": "tujuh delapan sembilan sepuluh"},
]

fa = FakeAnalyzer()
mom_campaign = fa.analyze_segments(segs, 120.0, campaign_active=True, campaign_rules_text=t1)
check("analyze_segments meneruskan rules_text ke layer request",
      bool(fa.captured) and fa.captured[0][1] == t1, str([c[1][:20] if c[1] else c[1] for c in fa.captured]))
check("risk_flags bertahan lewat snap + dedupe",
      mom_campaign[0].risk_flags == ["multi_speaker"], str([m.risk_flags for m in mom_campaign]))
check("momen tanpa risk_flags -> []",
      all(hasattr(m, "risk_flags") for m in mom_campaign) and mom_campaign[-1].risk_flags == [])

fa2 = FakeAnalyzer()
mom_plain = fa2.analyze_transcript_text("[0.00-30.00] satu dua tiga", 120.0)
check("golden path: rules_text None diteruskan apa adanya",
      fa2.captured[0][1] is None)
check("golden path: tanpa campaign tetap normal", len(mom_plain) == 2)

base_prompt = ai.USER_PROMPT_TEMPLATE.format(
    transcript="TRANSCRIP CONTOH", min_duration=int(cp.config.MIN_CLIP_DURATION_S),
    max_duration=int(cp.config.MAX_CLIP_DURATION_S))
check("prompt tanpa rules BYTE-IDENTIK dengan template lama",
      ai.build_user_prompt("TRANSCRIP CONTOH") == base_prompt)
check("prompt rules None/''/whitespace identik",
      ai.build_user_prompt("T", None) == ai.build_user_prompt("T", "") == ai.build_user_prompt("T", "   \n ")
      == ai.USER_PROMPT_TEMPLATE.format(transcript="T", min_duration=int(cp.config.MIN_CLIP_DURATION_S),
                                        max_duration=int(cp.config.MAX_CLIP_DURATION_S)))
camp_prompt = ai.build_user_prompt("TRANSCRIP CONTOH", t1)
check("prompt campaign memuat blok aturan", "ATURAN CAMPAIGN DARI PARTNER (WAJIB):" in camp_prompt)
check("prompt campaign memuat rules_text utuh", t1 in camp_prompt)
check("prompt campaign minta risk_flags di skema JSON", '"risk_flags": [' in camp_prompt)
check("prompt campaign menambah item metadata risk_flags", "'risk_flags'" in camp_prompt)
check("prompt campaign tetap menutup dengan transkrip aslinya",
      camp_prompt.endswith("Transkrip:\nTRANSCRIP CONTOH\n") or camp_prompt.rstrip().endswith("TRANSCRIP CONTOH"))
check("prompt campaign tidak menghapus aturan bawaan", "5 PILAR VIRALITAS" in camp_prompt and "viral_score" in camp_prompt)
check("rules block sebelum heading Transkrip",
      camp_prompt.index("ATURAN CAMPAIGN DARI PARTNER") < camp_prompt.index("Transkrip:"))
check("prompt bebas (rules '') identik golden path", "ATURAN CAMPAIGN" not in ai.build_user_prompt("T", cp.campaign_rules_text(bebas)))

# --------------------------------------------------------------------------- #
section("8. serialisasi & backward compatibility")
# --------------------------------------------------------------------------- #
dumped = json.loads(ai.moments_to_json(mom_campaign))
check("asdict(moment) memuat risk_flags", "risk_flags" in dumped[0], str(sorted(dumped[0])))
check("momen hasil parse ulang punya risk_flags",
      ai.validate_moments(dumped, 120.0)[0].risk_flags == ["multi_speaker"])
legacy_moment = ai.ClipMoment(0.0, 30.0, "H", "topic", "cap", 90, "alasan", "upbeat", 1, "fw", "lw")
check("11 argumen posisional lama tetap sah", legacy_moment.risk_flags == [])
check("asdict legacy tidak error", asdict(legacy_moment)["risk_flags"] == [])
# verdict dari rantai penuh: momen AI -> policy
chain_flags = [m.risk_flags for m in mom_campaign]
check("rantai AI -> llm_flag_verdicts hidup",
      any(v.code == "llm_flag:multi_speaker" for v in cp.llm_flag_verdicts(chain_flags[0], std)))
check("rantai AI -> human_gates_for hidup",
      any(v.code == "human_gate:graeme_majority" for v in cp.human_gates_for(std, chain_flags)))
check("aggregate tetap <= warn untuk sinyal AI saja",
      cp.aggregate_level(cp.llm_flag_verdicts(["multi_speaker", "x"], std) + cp.human_gates_for(std, chain_flags))
      in ("pass", "info", "warn"))

# --------------------------------------------------------------------------- #
section("9. seed profiles tetap valid")
# --------------------------------------------------------------------------- #
check("graeme seed valid setelah field v2", not cp.validate_profile(graeme), str(cp.validate_profile(graeme)))
check("bebas seed valid", not cp.validate_profile(bebas), str(cp.validate_profile(bebas)))
check("graeme punya multi_speaker gate ack_always",
      {g["id"]: g["strict_mode"] for g in cp._human_gates(graeme)} ==
      {"graeme_majority": "ack_always", "style_fit": "warn"})
check("list_profiles tetap jalan",
      {"bebas", "graeme-clipinfluence"} <= {r["id"] for r in cp.list_profiles()})

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL GREEN")
sys.exit(0)
