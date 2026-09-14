"""Pure verification for the campaign policy engine (modules/campaign_policy.py).

NO LLM, NO Telegram, NO network, NO file writes outside data/campaigns reads.
Run:  .venv\\Scripts\\python.exe scratch\\verify_campaign_policy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import campaign_policy as cp  # noqa: E402
from modules.ai_analyzer import ClipMoment  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def vtuple(vs: list[cp.Verdict]) -> list[tuple]:
    return [(v.level, v.code, v.message, v.evidence) for v in vs]


def seg(text: str, start: float, end: float) -> dict:
    return {"start": start, "end": end, "text": text}


def prof(clip_rules=None, post_rules=None, strictness="standard") -> dict:
    return {
        "schema_version": 2,
        "id": "test",
        "name": "Test",
        "strictness": strictness,
        "profile_version": 1,
        "clip_rules": clip_rules
        if clip_rules is not None
        else {"duration_s": {"min": 15, "max": 60}, "banned_exact": [], "banned_topics": []},
        "post_rules": post_rules
        if post_rules is not None
        else {"caption_required": {}, "platforms": {}},
        "account_items": [],
        "out_of_control": [],
    }


def save_rejects(p: dict) -> bool:
    """True if save_profile raises ValueError (rejects an invalid profile)."""
    try:
        cp.save_profile(p)
        return False
    except ValueError:
        return True


# --- Seed profiles load + validate ---------------------------------------- #
print("== seed profiles ==")
for pid in ("bebas", "graeme-clipinfluence"):
    try:
        p = cp.load_profile(pid)
        errs = cp.validate_profile(p)
        check(f"seed '{pid}' valid", not errs, "; ".join(errs))
    except Exception as exc:  # noqa: BLE001
        check(f"seed '{pid}' valid", False, repr(exc))

check("default_profile_id == 'bebas'", cp.default_profile_id() == "bebas")
ids = {row["id"] for row in cp.list_profiles()}
check("list_profiles memuat kedua profil", {"bebas", "graeme-clipinfluence"} <= ids, str(sorted(ids)))

# --- (a) determinism: same profile => identical verdicts 2x run ------------ #
print("== (a) determinism ==")
det_profile = cp.load_profile("graeme-clipinfluence")
det_moments = [
    ClipMoment(start=0, end=30, hook="HOOK A", caption="full episode", topic="x"),
    ClipMoment(start=40, end=61, hook="HOOK B", caption="", topic="y"),
]
det_segs = [
    seg(" Halo semua tentang only fans dan brand y ", 0, 30),
    seg(" durasi panjang sekali sampai enam puluh satu detik penuh ", 40, 61),
]
run1 = vtuple(sum(cp.evaluate_catalog(det_moments, det_segs, det_profile), []))
run2 = vtuple(sum(cp.evaluate_catalog(det_moments, det_segs, det_profile), []))
check("evaluate_catalog identik 2x run", run1 == run2)
check("evaluate_catalog index-aligned", len(cp.evaluate_catalog(det_moments, det_segs, det_profile)) == len(det_moments))

# --- (b) fixtures ---------------------------------------------------------- #
print("== (b) fixtures ==")

# banned_topics negasi -> warn/info, BUKAN fail
judi_profile = prof(
    clip_rules={
        "duration_s": None,
        "banned_exact": [],
        "banned_topics": [{"topic": "judi", "regex": ["\\bjudi\\b", "\\bgambling\\b"]}],
    },
    strictness="standard",
)
neg_m = ClipMoment(start=0, end=10, hook="H")
neg_segs = [seg("gue gak pernah judi online", 0, 10)]
neg_v = cp.evaluate_moment(neg_m, neg_segs, judi_profile)
neg_topic = [v for v in neg_v if v.code == "banned_topics"]
check(
    "negasi 'gue gak pernah judi online' -> warn (bukan fail)",
    bool(neg_topic) and neg_topic[0].level == "warn",
    str(vtuple(neg_topic)),
)
check("banned_topics tidak pernah fail", all(v.level != "fail" for v in neg_v))

# banned_exact muncul di transkrip slice -> fail
ex_profile = prof(clip_rules={"duration_s": None, "banned_exact": ["BrandY"], "banned_topics": []})
ex_m = ClipMoment(start=0, end=20, hook="H")
ex_segs = [seg("kita review BrandY musim ini", 0, 20)]
ex_v = cp.evaluate_moment(ex_m, ex_segs, ex_profile)
ex_hit = [v for v in ex_v if v.code == "banned_exact"]
check("banned_exact di slice -> fail", bool(ex_hit) and ex_hit[0].level == "fail", str(vtuple(ex_hit)))

# banned_exact HANYA di luar slice -> tidak boleh fail
ex_out = cp.evaluate_moment(ClipMoment(start=100, end=110, hook="H"),
                            [seg("kita review BrandY", 0, 20), seg("bagian bersih", 100, 110)],
                            ex_profile)
check("banned_exact di luar slice -> pass (bukan fail)",
      all(v.level != "fail" for v in ex_out if v.code == "banned_exact"))

# durasi 61s strict -> fail ; loose -> info
m61 = ClipMoment(start=0, end=61, hook="H")
strict_v = cp.evaluate_moment(m61, [seg("x", 0, 61)], prof(strictness="strict"))
loose_v = cp.evaluate_moment(m61, [seg("x", 0, 61)], prof(strictness="loose"))
sd = [v for v in strict_v if v.code == "duration_range"]
ld = [v for v in loose_v if v.code == "duration_range"]
check("durasi 61s strict -> fail", bool(sd) and sd[0].level == "fail", str(vtuple(sd)))
check("durasi 61s loose -> info", bool(ld) and ld[0].level == "info", str(vtuple(ld)))

# caption 'full episode...' tanpa #Graemeholm -> warn (via graeme)
graeme = cp.load_profile("graeme-clipinfluence")
cap_bad = cp.check_caption("Tonton full episode lengkapnya di kanal ya", graeme)
check("caption tanpa #Graemeholm -> warn",
      bool(cap_bad) and cap_bad[0].level == "warn", str(vtuple(cap_bad)))
cap_ok = cp.check_caption("Tonton full episode, link in bio #Graemeholm", graeme)
check("caption lengkap -> pass", bool(cap_ok) and cap_ok[0].level == "pass", str(vtuple(cap_ok)))

# evaluate_moment: caption preview ada + melanggar -> warn; caption kosong -> skip
prev_bad = cp.evaluate_moment(ClipMoment(start=20, end=40, hook="H", caption="full episode"),
                              [seg("bersih", 20, 40)], graeme)
check("caption preview melanggar -> warn",
      any(v.code == "caption_required" and v.level == "warn" for v in prev_bad))
prev_skip = cp.evaluate_moment(ClipMoment(start=20, end=40, hook="H", caption=""),
                               [seg("bersih", 20, 40)], graeme)
check("caption kosong -> caption_required di-skip",
      not any(v.code == "caption_required" for v in prev_skip))

# aggregate_level ordering
check("aggregate_level fail>warn>info>pass",
      cp.aggregate_level([cp.Verdict("warn", "a", ""), cp.Verdict("fail", "b", ""),
                          cp.Verdict("info", "c", ""), cp.Verdict("pass", "d", "")]) == "fail")
check("aggregate_level empty -> pass", cp.aggregate_level([]) == "pass")
check("aggregate_level warn>info",
      cp.aggregate_level([cp.Verdict("info", "c", ""), cp.Verdict("warn", "a", "")]) == "warn")

# --- (c) unknown rule kind ------------------------------------------------- #
print("== (c) unknown rule kind ==")
bad_kind = prof()
bad_kind["clip_rules"]["deteksi_watermark"] = True
errs = cp.validate_profile(bad_kind)
check("unknown clip kind -> validate error", any("deteksi_watermark" in e for e in errs), "; ".join(errs))
check("save_profile menolak profil invalid", save_rejects(bad_kind))

# --- (d) regex jahat ------------------------------------------------------- #
print("== (d) regex jahat ==")
bad_re = prof(
    clip_rules={
        "duration_s": None,
        "banned_exact": [],
        "banned_topics": [{"topic": "x", "regex": ["[unclosed"]}],
    }
)
errs = cp.validate_profile(bad_re)
check("regex jahat '[unclosed' -> validate error", any("regex" in e for e in errs), "; ".join(errs))

# --- (e) normalize consistency -------------------------------------------- #
print("== (e) normalize ==")
n1 = cp.normalize_text("  GR@AEME   HOLM ")
check("normalize whitespace+case", n1 == "gr@aeme holm", repr(n1))
check("normalize idempoten", cp.normalize_text(n1) == n1)
check(
    "normalize konsisten untuk pencocokan",
    cp.normalize_text("  FULL   EPISODE ") == cp.normalize_text("full episode")
    and "full episode" in cp.normalize_text("Tonton  FULL\nEpisode sekarang"),
)

# campaign_intro_lines shape
intro = cp.campaign_intro_lines(graeme)
check("campaign_intro_lines -> list[str] non-kosong",
      bool(intro) and all(isinstance(x, str) for x in intro))


print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL GREEN")
sys.exit(0)
