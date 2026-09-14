"""Campaign compliance policy engine (inti sistem kepatuhan campaign - langkah 1).

Menguji setiap klip kandidat terhadap "profile" campaign (S&K partner) dan
menghasilkan daftar ``Verdict`` yang bisa diagregasi oleh lane UI/bot.

Desain anti-halusinasi & deterministik:
- Semua pencocokan teks lewat ``normalize_text`` (NFKC + lowercase + whitespace
  collapse). Satu sumber kebenaran untuk matching.
- Regex profil di-compile & divalidasi saat ``validate_profile``; profil dengan
  regex rusak atau rule kind tak dikenal DITOLAK (tidak pernah lolos diam-diam).
- TOPIK sensitif (banned_topics) tidak pernah menghasilkan ``fail``: negasi &
  elusi bahasa Indonesia ("gue gak pernah judi online") tetap harus dibaca
  manusia, jadi maksimal ``warn``/``info``.

Level v1 (lihat MATRIKS LEVEL):
- duration_range : strict/standard => fail, loose => info (bila di luar rentang)
- banned_exact   : selalu fail (scan TRANSKRIP asli slice moment, bukan LLM text)
- banned_topics  : loose => info, standard/strict => warn (+ evidence kutipan)
- caption_required : class "post". evaluate_moment hanya memberi ``warn`` bila
  moment SUDAH punya caption preview yang jelas melanggar; tanpa caption => skip.

Level v2 (kurasi berbasis AI + gerbang manusia — TIDAK pernah fail):
- llm_flag       : sinyal risiko dari AI (``ClipMoment.risk_flags``). AI hanya
  MENANDAI, tidak memveto; level maksimum ``warn``.
- human_gate     : checklist review manusia (trigger 'always' atau 'llm_flag:<id>').
  Level ``warn`` (strict_mode ack_always) atau ``info`` (strict_mode warn).
- Profil lama tanpa field baru di atas harus tetap lolos ``validate_profile``.

Bentuk ``Verdict.evidence`` (kontrak ke lane bot/UI): kutipan transkrip berupa
string; untuk ``caption_required`` berupa ``{"missing": [...]}`` sehingga bot bisa
menampilkan usulan tag tanpa menyalin logika pencocokan ke sisi UI.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config


LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Registry & level matrix
# --------------------------------------------------------------------------- #

CHECK_REGISTRY: dict[str, dict] = {
    "duration_range": {
        "class": "hard",
        "desc": "Durasi klip harus berada dalam rentang detik yang diizinkan profile.",
    },
    "banned_exact": {
        "class": "hard",
        "desc": "Istilah terlarang persis; muncul di transkrip slice = gagal keras.",
    },
    "banned_topics": {
        "class": "warn",
        "desc": "Topik sensitif; butuh penilaian manusia (elusi/negasi), tak pernah fail.",
    },
    "caption_required": {
        "class": "post",
        "desc": "Caption wajib memuat tag/frasa tertentu (divalidasi saat posting).",
    },
    "llm_flag": {
        "class": "warn",
        "desc": "Sinyal risiko dari AI (risk_flags); hanya menandai, tak pernah gagal keras.",
    },
    "human_gate": {
        "class": "warn",
        "desc": "Gerbang review manusia (always / terpicu llm_flag); butuh konfirmasi manual.",
    },
}

# Structural schema: field names accepted inside clip_rules / post_rules.
# NOTE: `duration_s` adalah FIELD pada profile, sedangkan KIND/CHECK_REGISTRY key
# tetap "duration_range" (dipakai sebagai Verdict.code). Nama berbeda ini sengaja
# mengikuti skema JSON user.
# v2: `curation_note`, `flag_requests`, `human_gates` OPSIONAL — profil lama tanpa
# ketiganya tetap valid.
_KNOWN_CLIP_KEYS = {
    "duration_s",
    "banned_exact",
    "banned_topics",
    "curation_note",
    "flag_requests",
    "human_gates",
}
_KNOWN_POST_KEYS = {"caption_required", "platforms"}
_KNOWN_FLAG_REQUEST_KEYS = {"id", "ask"}
_KNOWN_HUMAN_GATE_KEYS = {"id", "text", "trigger", "strict_mode"}
_VALID_STRICTNESS = {"loose", "standard", "strict"}
_VALID_STRICT_MODES = {"warn", "ack_always"}

# id flag/gate: slug pendek yang aman dipakai di prompt AI, Verdict.code, dan trigger.
_FLAG_ID_RE = re.compile(r"[a-z0-9_]{1,32}")
# trigger gate: 'always' atau 'llm_flag:<id flag>'
_GATE_TRIGGER_RE = re.compile(r"^llm_flag:([a-z0-9_]{1,32})$")
_GATE_TRIGGER_ALWAYS = "always"
# campaign_id dipakai sebagai nama file audit -> harus slug bebas separator path.
_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FLAG_SLUG_STRIP_RE = re.compile(r"[^a-z0-9_]+")
_FLAG_SLUG_COLLAPSE_RE = re.compile(r"_+")
_MAX_FLAGS_PER_MOMENT = 6

# Level severity ordering for aggregation (higher rank = worse).
_LEVEL_RANK = {"pass": 0, "info": 1, "warn": 2, "fail": 3}


@dataclass(frozen=True)
class Verdict:
    level: str  # pass | info | warn | fail
    code: str
    message: str
    # Bukti pendukung. Kontrak lintas-lane:
    #   - verdict berbasis teks (kutipan transkrip) -> ``str``
    #   - verdict caption (caption_required)         -> ``dict`` dengan kunci
    #     ``"missing": list[str]`` (daftar tag/frasa wajib yang belum ada),
    #     supaya lane bot bisa menampilkan usulan tanpa menduplikasi logika
    #     pencocokan.
    #   - tanpa bukti -> ``None``
    evidence: Any = None


# --------------------------------------------------------------------------- #
# Text normalization (SATU fungsi untuk semua pencocokan)
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")


def normalize_text(s: str) -> str:
    """Lowercase, NFKC-normalize, and collapse whitespace for consistent matching.

    Idempotent: ``normalize_text(normalize_text(x)) == normalize_text(x)``.
    """
    if s is None:
        return ""
    text = unicodedata.normalize("NFKC", str(s))
    text = text.lower()
    text = _WS_RE.sub(" ", text)
    return text.strip()


def flag_slug(value: Any) -> str:
    """Sanitasi satu id flag risiko jadi slug ``[a-z0-9_]`` maks 32 karakter.

    Dipakai SEBELUM pencocokan gate supaya flag dari AI ("Multi Speaker??")
    bertemu dengan id deklaratif di profil ("multi_speaker"). Hasil ``""``
    berarti flag tidak bisa dipakai. Fungsi ini SATU-SATUNYA aturan slug flag
    agar sisi AI (``ai_analyzer.validate_moments``) dan sisi policy tidak
    drift.
    """
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value).lower()
    text = _WS_RE.sub("_", text.strip())
    text = _FLAG_SLUG_STRIP_RE.sub("_", text)
    text = _FLAG_SLUG_COLLAPSE_RE.sub("_", text).strip("_")
    return text[:32]


# --------------------------------------------------------------------------- #
# Profile validation (anti-halusinasi)
# --------------------------------------------------------------------------- #

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_string_list(value: Any, where: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{where}: harus list of string, dapat {type(value).__name__}")
        return
    for i, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{where}[{i}]: item harus string, dapat {type(item).__name__}")


def _validate_regex_list(value: Any, where: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{where}: harus list of regex string, dapat {type(value).__name__}")
        return
    for i, pat in enumerate(value):
        if not isinstance(pat, str):
            errors.append(f"{where}[{i}]: regex harus string, dapat {type(pat).__name__}")
            continue
        try:
            re.compile(normalize_text(pat))
        except re.error as exc:
            errors.append(f"{where}[{i}]: regex tidak valid ({exc!r}): {pat!r}")


def _validate_curation_note(value: Any, where: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{where}: harus string arahan kurasi, dapat {type(value).__name__}")


def _validate_flag_requests(value: Any, where: str, errors: list[str]) -> set[str]:
    """Validate clip_rules.flag_requests; returns the set of declared flag ids."""
    declared: set[str] = set()
    if not isinstance(value, list):
        errors.append(f"{where}: harus list of {{id, ask}}, dapat {type(value).__name__}")
        return declared
    for i, entry in enumerate(value):
        w = f"{where}[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{w}: harus dict {{id, ask}}, dapat {type(entry).__name__}")
            continue
        for key in entry:
            if key not in _KNOWN_FLAG_REQUEST_KEYS:
                errors.append(f"{w}: field tak dikenal '{key}' (boleh: id, ask)")
        fid = entry.get("id")
        if not isinstance(fid, str) or not _FLAG_ID_RE.fullmatch(fid):
            errors.append(f"{w}.id harus slug [a-z0-9_] maks 32 karakter, dapat {fid!r}")
        elif fid in declared:
            errors.append(f"{w}.id duplikat '{fid}'")
        else:
            declared.add(fid)
        ask = entry.get("ask")
        if not isinstance(ask, str) or not ask.strip():
            errors.append(f"{w}.ask wajib string non-kosong (pertanyaan pendek untuk AI)")
    return declared


def _validate_human_gates(
    value: Any,
    where: str,
    errors: list[str],
    declared_flags: set[str],
) -> None:
    if not isinstance(value, list):
        errors.append(
            f"{where}: harus list of {{id, text, trigger, strict_mode}}, dapat {type(value).__name__}"
        )
        return
    seen: set[str] = set()
    for i, entry in enumerate(value):
        w = f"{where}[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{w}: harus dict {{id, text, trigger, strict_mode}}, dapat {type(entry).__name__}")
            continue
        for key in entry:
            if key not in _KNOWN_HUMAN_GATE_KEYS:
                errors.append(f"{w}: field tak dikenal '{key}' (boleh: id, text, trigger, strict_mode)")

        gid = entry.get("id")
        if not isinstance(gid, str) or not _FLAG_ID_RE.fullmatch(gid):
            errors.append(f"{w}.id harus slug [a-z0-9_] maks 32 karakter, dapat {gid!r}")
        elif gid in seen:
            errors.append(f"{w}.id duplikat '{gid}'")
        else:
            seen.add(gid)

        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{w}.text wajib string non-kosong (instruksi review untuk manusia)")

        trigger = entry.get("trigger")
        if not isinstance(trigger, str) or not trigger.strip():
            errors.append(
                f"{w}.trigger wajib '{_GATE_TRIGGER_ALWAYS}' atau 'llm_flag:<id>', dapat {trigger!r}"
            )
        else:
            trigger = trigger.strip()
            if trigger != _GATE_TRIGGER_ALWAYS:
                m = _GATE_TRIGGER_RE.match(trigger)
                if not m:
                    errors.append(
                        f"{w}.trigger format salah: {trigger!r} "
                        f"(boleh '{_GATE_TRIGGER_ALWAYS}' atau 'llm_flag:<id>')"
                    )
                elif m.group(1) not in declared_flags:
                    # Gate yang merujuk flag tak terdaftar tidak akan pernah bisa picu
                    # -> silent no-op, jadi ditolak eksplisit.
                    errors.append(
                        f"{w}.trigger merujuk flag '{m.group(1)}' yang tidak dideklarasikan "
                        f"di clip_rules.flag_requests (gate tidak akan pernah terpicu)"
                    )

        strict_mode = entry.get("strict_mode")
        if strict_mode is None:
            pass  # opsional, default 'warn'
        elif strict_mode not in _VALID_STRICT_MODES:
            errors.append(
                f"{w}.strict_mode harus salah satu {sorted(_VALID_STRICT_MODES)}, dapat {strict_mode!r}"
            )


def validate_profile(p: Any) -> list[str]:
    """Return a list of human-readable errors; empty list means the profile is valid.

    Unknown rule kinds (e.g. 'deteksi_watermark'), malformed regex, wrong types,
    and inconsistent types are all rejected so a hallucinated profile never
    silently becomes a no-op.
    """
    errors: list[str] = []
    if not isinstance(p, dict):
        return [f"profile harus dict, dapat {type(p).__name__}"]

    # --- top-level metadata ---
    if "schema_version" not in p:
        errors.append("schema_version wajib ada")
    elif not _is_number(p["schema_version"]):
        errors.append(f"schema_version harus angka, dapat {type(p['schema_version']).__name__}")

    pid = p.get("id")
    if not isinstance(pid, str) or not pid.strip():
        errors.append("id wajib berupa string non-kosong")
    elif not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", pid):
        errors.append(f"id '{pid}' bukan slug aman (boleh a-z 0-9 . _ -)")

    name = p.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name wajib berupa string non-kosong")

    strictness = p.get("strictness")
    if strictness not in _VALID_STRICTNESS:
        errors.append(f"strictness harus salah satu {sorted(_VALID_STRICTNESS)}, dapat {strictness!r}")

    pv = p.get("profile_version")
    if pv is not None and not _is_number(pv):
        errors.append(f"profile_version harus angka, dapat {type(pv).__name__}")

    # source_doc optional (nullable); tidak memvalidasi isinya di v1.

    # --- clip_rules ---
    clip_rules = p.get("clip_rules")
    if clip_rules is not None and not isinstance(clip_rules, dict):
        errors.append(f"clip_rules harus dict atau null, dapat {type(clip_rules).__name__}")
        clip_rules = None
    if isinstance(clip_rules, dict):
        for key in clip_rules:
            if key not in _KNOWN_CLIP_KEYS:
                errors.append(f"clip_rules: rule kind tak dikenal '{key}'")

        dur = clip_rules.get("duration_s")
        if dur is not None:
            if not isinstance(dur, dict):
                errors.append("clip_rules.duration_s harus dict atau null")
            else:
                mn, mx = dur.get("min"), dur.get("max")
                mn_ok = isinstance(mn, (int, float)) and not isinstance(mn, bool)
                mx_ok = isinstance(mx, (int, float)) and not isinstance(mx, bool)
                if not mn_ok:
                    errors.append("duration_s.min harus angka")
                if not mx_ok:
                    errors.append("duration_s.max harus angka")
                if mn_ok and mx_ok and mn is not None and mx is not None and mn > mx:
                    errors.append(f"duration_s.min ({mn}) melebihi max ({mx})")

        if "banned_exact" in clip_rules:
            _validate_string_list(clip_rules["banned_exact"], "clip_rules.banned_exact", errors)

        topics = clip_rules.get("banned_topics")
        if topics is not None:
            if not isinstance(topics, list):
                errors.append(f"clip_rules.banned_topics harus list, dapat {type(topics).__name__}")
            else:
                for i, t in enumerate(topics):
                    where = f"clip_rules.banned_topics[{i}]"
                    if not isinstance(t, dict):
                        errors.append(f"{where}: harus dict {{topic, regex}}")
                        continue
                    topic = t.get("topic")
                    if not isinstance(topic, str) or not topic.strip():
                        errors.append(f"{where}.topic wajib string non-kosong")
                    _validate_regex_list(t.get("regex"), f"{where}.regex", errors)

        # --- v2 optional curation fields (profil lama tanpa ini tetap valid) ---
        if "curation_note" in clip_rules:
            _validate_curation_note(clip_rules["curation_note"], "clip_rules.curation_note", errors)

        declared_flags: set[str] = set()
        if "flag_requests" in clip_rules:
            declared_flags = _validate_flag_requests(
                clip_rules["flag_requests"], "clip_rules.flag_requests", errors
            )

        if "human_gates" in clip_rules:
            _validate_human_gates(
                clip_rules["human_gates"], "clip_rules.human_gates", errors, declared_flags
            )

    # --- post_rules ---
    post_rules = p.get("post_rules")
    if post_rules is not None and not isinstance(post_rules, dict):
        errors.append(f"post_rules harus dict atau null, dapat {type(post_rules).__name__}")
        post_rules = None
    if isinstance(post_rules, dict):
        for key in post_rules:
            if key not in _KNOWN_POST_KEYS:
                errors.append(f"post_rules: rule kind tak dikenal '{key}'")

        cap = post_rules.get("caption_required")
        if cap is not None:
            if not isinstance(cap, dict):
                errors.append(f"post_rules.caption_required harus dict atau null, dapat {type(cap).__name__}")
            else:
                for subkey in ("all_of", "any_of"):
                    if subkey in cap:
                        _validate_string_list(cap[subkey], f"post_rules.caption_required.{subkey}", errors)

        platforms = post_rules.get("platforms")
        if platforms is not None:
            if not isinstance(platforms, dict):
                errors.append(f"post_rules.platforms harus dict atau null, dapat {type(platforms).__name__}")
            else:
                for pname, pdata in platforms.items():
                    where = f"post_rules.platforms.{pname}"
                    if not isinstance(pdata, dict):
                        errors.append(f"{where}: harus dict {{append:[...]}}")
                        continue
                    if "append" in pdata:
                        _validate_string_list(pdata["append"], f"{where}.append", errors)

    # --- manual / advisory lists ---
    for key in ("account_items", "out_of_control"):
        if key in p:
            _validate_string_list(p[key], key, errors)

    return errors


# --------------------------------------------------------------------------- #
# Persistence (atomic tmp + rename)
# --------------------------------------------------------------------------- #

def _campaigns_dir() -> Path:
    return Path(config.CAMPAIGNS_DIR)


def _profile_path(profile_id: str) -> Path:
    return _campaigns_dir() / f"{profile_id}.json"


def default_profile_id() -> str:
    return "bebas"


def load_profile(profile_id: str) -> dict:
    """Load a campaign profile by id. Raises KeyError if it does not exist."""
    path = _profile_path(profile_id)
    if not path.exists():
        raise KeyError(f"Campaign profile '{profile_id}' tidak ditemukan di {path.parent}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_profile(p: dict) -> None:
    """Validate then atomically write a profile to data/campaigns/{id}.json."""
    errors = validate_profile(p)
    if errors:
        raise ValueError("Profil campaign tidak valid:\n- " + "\n- ".join(errors))
    profile_id = p["id"]
    dirpath = _campaigns_dir()
    dirpath.mkdir(parents=True, exist_ok=True)
    final_path = _profile_path(profile_id)
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    payload = json.dumps(p, ensure_ascii=False, indent=2) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(final_path)


def list_profiles() -> list[dict]:
    """Summaries [{id,name,strictness,profile_version}] for every stored profile."""
    dirpath = _campaigns_dir()
    out: list[dict] = []
    if not dirpath.exists():
        return out
    for path in sorted(dirpath.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "id": data.get("id", path.stem),
                "name": data.get("name", ""),
                "strictness": data.get("strictness", ""),
                "profile_version": data.get("profile_version"),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Transcript slice extraction (scan transkrip ASLI, bukan output LLM)
# --------------------------------------------------------------------------- #

def _seg_field(seg: Any, name: str, default: Any) -> Any:
    if isinstance(seg, dict):
        return seg.get(name, default)
    return getattr(seg, name, default)


def _moment_time(moment: Any, name: str) -> float:
    """Timestamp moment (objek ClipMoment atau dict) sebagai float, 0.0 bila rusak."""
    try:
        return float(_seg_field(moment, name, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _transcript_slice(moment: Any, segments: Iterable[Any]) -> str:
    """Join text of every segment overlapping [moment.start, moment.end].

    Toleran terhadap bentuk segment apa pun yang dipakai alur bot:
    - ``TranscriptSegment`` hasil Whisper (punya ``words``),
    - ``TranscriptSegment`` hasil parse VTT YouTube (``words`` = None / tidak ada),
    - segment dict dari cache ``.segments.json``.
    Hanya ``start``/``end``/``text`` yang dibaca, jadi tidak ada jalur yang
    bergantung pada word timing.
    """
    start = _moment_time(moment, "start")
    end = _moment_time(moment, "end")
    parts: list[str] = []
    for seg in segments or []:
        try:
            s = float(_seg_field(seg, "start", 0.0))
            e = float(_seg_field(seg, "end", 0.0))
        except (TypeError, ValueError):
            continue
        if e <= s:
            continue
        if s < end and e > start:  # overlap
            txt = str(_seg_field(seg, "text", "") or "").strip()
            if txt:
                parts.append(txt)
    return " ".join(parts)


def _evidence_context(haystack: str, span: tuple[int, int], radius: int = 200) -> str:
    """±``radius`` characters of context around a match, for human review."""
    lo = max(0, span[0] - radius)
    hi = min(len(haystack), span[1] + radius)
    snippet = haystack[lo:hi].strip()
    if lo > 0:
        snippet = "..." + snippet
    if hi < len(haystack):
        snippet = snippet + "..."
    return snippet


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def _check_duration(moment: Any, profile: dict) -> Verdict | None:
    dur_rule = (profile.get("clip_rules") or {}).get("duration_s")
    if not isinstance(dur_rule, dict):
        return None
    mn_raw = dur_rule.get("min")
    mx_raw = dur_rule.get("max")
    if not isinstance(mn_raw, (int, float)) or isinstance(mn_raw, bool):
        return None
    if not isinstance(mx_raw, (int, float)) or isinstance(mx_raw, bool):
        return None
    mn = float(mn_raw)
    mx = float(mx_raw)
    duration = _moment_time(moment, "end") - _moment_time(moment, "start")
    if mn <= duration <= mx:
        return Verdict("pass", "duration_range", f"Durasi {duration:.1f}s dalam rentang {mn:g}-{mx:g}s.")
    strictness = profile.get("strictness", "standard")
    level = "info" if strictness == "loose" else "fail"
    if duration < mn:
        detail = f"Durasi {duration:.1f}s terlalu pendek (min {mn:g}s)."
    else:
        detail = f"Durasi {duration:.1f}s terlalu panjang (maks {mx:g}s)."
    return Verdict(level, "duration_range", detail)


def _check_banned_exact(moment: Any, profile: dict, haystack: str) -> list[Verdict]:
    terms = (profile.get("clip_rules") or {}).get("banned_exact") or []
    if not terms:
        return []
    verdicts: list[Verdict] = []
    for term in terms:
        norm_term = normalize_text(term)
        if not norm_term:
            continue
        pattern = r"(?<!\w)" + re.escape(norm_term) + r"(?!\w)"
        match = re.search(pattern, haystack)
        if match:
            verdicts.append(
                Verdict(
                    "fail",
                    "banned_exact",
                    f"Istilah terlarang '{term}' muncul di transkrip klip.",
                    evidence=_evidence_context(haystack, match.span()),
                )
            )
    if not verdicts:
        verdicts.append(Verdict("pass", "banned_exact", "Tidak ada istilah terlarang persis di klip."))
    return verdicts


def _check_banned_topics(moment: Any, profile: dict, haystack: str) -> list[Verdict]:
    topics = (profile.get("clip_rules") or {}).get("banned_topics") or []
    if not topics:
        return []
    strictness = profile.get("strictness", "standard")
    hit_level = "info" if strictness == "loose" else "warn"
    verdicts: list[Verdict] = []
    hit_any = False
    for entry in topics:
        if not isinstance(entry, dict):
            continue
        topic = entry.get("topic", "")
        for pat in entry.get("regex") or []:
            norm_pat = normalize_text(pat)
            if not norm_pat:
                continue
            try:
                compiled = re.compile(norm_pat)
            except re.error:
                # validate_profile should have blocked this; be defensive, don't crash.
                continue
            match = compiled.search(haystack)
            if match:
                hit_any = True
                verdicts.append(
                    Verdict(
                        hit_level,
                        "banned_topics",
                        f"Topik sensitif '{topic}' terdeteksi — perlu review manusia.",
                        evidence=_evidence_context(haystack, match.span()),
                    )
                )
                break  # one hit per topic is enough
    if not hit_any:
        verdicts.append(Verdict("pass", "banned_topics", "Tidak ada topik sensitif terdeteksi di klip."))
    return verdicts


def _caption_missing(caption_text: str, cap_rule: dict) -> dict[str, Any]:
    """Structured caption gap.

    Returns ``{"all_of": [...], "any_of": [...]}`` where:
    - ``all_of``  : item wajib yang benar-benar hilang (string asli dari profil)
    - ``any_of``  : seluruh grup any_of (kosong bila syarat sudah terpenuhi)

    ``any_of`` terisi hanya bila grup itu BELUM ada satu pun yang cocok, sehingga
    lane bot bisa mengusulkan salah satu tag tanpa meniru logika pencocokan.
    """
    norm = normalize_text(caption_text)
    missing_all = [
        str(tag)
        for tag in (cap_rule.get("all_of") or [])
        if normalize_text(tag) and normalize_text(tag) not in norm
    ]
    any_of = [str(x) for x in (cap_rule.get("any_of") or []) if normalize_text(x)]
    any_of_satisfied = any(normalize_text(x) in norm for x in any_of)
    return {"all_of": missing_all, "any_of": [] if any_of_satisfied else any_of}


def _caption_missing_display(detail: dict[str, Any]) -> list[str]:
    """Human-readable flat list of gaps (dipakai pesan verdict + usulan bot)."""
    missing: list[str] = list(detail.get("all_of") or [])
    any_of = detail.get("any_of") or []
    if any_of:
        missing.append("salah satu dari: " + ", ".join(any_of))
    return missing


def _caption_clearly_violates(caption_text: str, cap_rule: dict) -> list[str]:
    """Return list of missing required items (all_of) / any_of failure."""
    return _caption_missing_display(_caption_missing(caption_text, cap_rule))


def _caption_evidence(detail: dict[str, Any]) -> dict[str, Any]:
    """Bentuk ``Verdict.evidence`` untuk caption_required (kontrak lintas-lane)."""
    return {
        "missing": _caption_missing_display(detail),
        "all_of": list(detail.get("all_of") or []),
        "any_of": list(detail.get("any_of") or []),
    }



def _check_caption_preview(moment: Any, profile: dict) -> Verdict | None:
    """Post-class caption check at clip level. Only warns when a caption preview
    already exists AND clearly violates. No caption => skip (None)."""
    cap_rule = (profile.get("post_rules") or {}).get("caption_required")
    if not isinstance(cap_rule, dict) or not cap_rule:
        return None
    caption = str(getattr(moment, "caption", "") or "").strip()
    if not caption:
        return None  # caption belum ada -> skip, jangan warn
    detail = _caption_missing(caption, cap_rule)
    missing = _caption_missing_display(detail)
    if missing:
        return Verdict(
            "warn",
            "caption_required",
            "Caption preview belum memenuhi syarat campaign: kurang "
            + "; ".join(missing),
            evidence=_caption_evidence(detail),
        )
    return Verdict("pass", "caption_required", "Caption preview memenuhi syarat campaign.")


def evaluate_moment(moment: Any, segments: Iterable[Any], profile: dict) -> list[Verdict]:
    """Evaluate one candidate clip against a campaign profile.

    Runs hard (duration_range, banned_exact) and warn (banned_topics) checks on
    the ORIGINAL transcript slice, plus a soft caption-preview warn when the
    moment already carries a caption. Returns one Verdict per applicable rule.
    """
    haystack = normalize_text(_transcript_slice(moment, segments))
    verdicts: list[Verdict] = []

    dur = _check_duration(moment, profile)
    if dur is not None:
        verdicts.append(dur)

    verdicts.extend(_check_banned_exact(moment, profile, haystack))
    verdicts.extend(_check_banned_topics(moment, profile, haystack))

    cap = _check_caption_preview(moment, profile)
    if cap is not None:
        verdicts.append(cap)

    return verdicts


def evaluate_catalog(
    moments: Iterable[Any], segments: Iterable[Any], profile: dict
) -> list[list[Verdict]]:
    """Evaluate a whole clip catalog; index-aligned list of per-moment verdicts."""
    segments = list(segments or [])
    return [evaluate_moment(m, segments, profile) for m in moments]


def check_caption(caption_text: str, profile: dict) -> list[Verdict]:
    """Post-time caption validation (class 'post').

    Violations are ``warn`` (never hard fail): caption compliance is a human
    action at posting time, mirroring banned_topics philosophy.

    ``Verdict.evidence`` pada hasil ``warn`` adalah dict dengan kunci ``missing``
    (flat list usulan) + ``all_of``/``any_of`` terstruktur, sehingga lane bot bisa
    menampilkan usulan hashtag tanpa menduplikasi logika pencocokan.
    """
    cap_rule = (profile.get("post_rules") or {}).get("caption_required")
    if not isinstance(cap_rule, dict) or not cap_rule:
        return []
    detail = _caption_missing(caption_text, cap_rule)
    missing = _caption_missing_display(detail)
    if missing:
        return [
            Verdict(
                "warn",
                "caption_required",
                "Caption kurang: " + "; ".join(missing),
                evidence=_caption_evidence(detail),
            )
        ]
    return [Verdict("pass", "caption_required", "Caption memenuhi syarat campaign.")]


def aggregate_level(verdicts: Iterable[Verdict]) -> str:
    """Worst level across verdicts: fail > warn > info > pass. Empty => pass."""
    worst = "pass"
    for v in verdicts:
        level = getattr(v, "level", "pass")
        if _LEVEL_RANK.get(level, 0) > _LEVEL_RANK.get(worst, 0):
            worst = level
    return worst


# --------------------------------------------------------------------------- #
# AI curation contract (v2)
# --------------------------------------------------------------------------- #

def _clip_rules(profile: Any) -> dict:
    if isinstance(profile, dict):
        rules = profile.get("clip_rules")
        if isinstance(rules, dict):
            return rules
    return {}


def _flag_requests(profile: Any) -> list[dict[str, str]]:
    """Normalized [{id, ask}]; malformed entries are skipped, never crash."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in _clip_rules(profile).get("flag_requests") or []:
        if not isinstance(entry, dict):
            continue
        fid = entry.get("id")
        ask = entry.get("ask")
        if not isinstance(fid, str) or not _FLAG_ID_RE.fullmatch(fid) or fid in seen:
            continue
        seen.add(fid)
        out.append({"id": fid, "ask": _WS_RE.sub(" ", str(ask or "")).strip()})
    return out


def _human_gates(profile: Any) -> list[dict[str, str]]:
    """Normalized [{id, text, trigger, strict_mode}]; malformed entries skipped."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in _clip_rules(profile).get("human_gates") or []:
        if not isinstance(entry, dict):
            continue
        gid = entry.get("id")
        trigger = entry.get("trigger")
        if not isinstance(gid, str) or not _FLAG_ID_RE.fullmatch(gid) or gid in seen:
            continue
        if not isinstance(trigger, str) or not trigger.strip():
            continue
        trigger = trigger.strip()
        if trigger != _GATE_TRIGGER_ALWAYS and not _GATE_TRIGGER_RE.match(trigger):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        strict_mode = entry.get("strict_mode")
        if strict_mode not in _VALID_STRICT_MODES:
            strict_mode = "warn"
        seen.add(gid)
        out.append({"id": gid, "text": _WS_RE.sub(" ", text).strip(), "trigger": trigger, "strict_mode": strict_mode})
    return out


def _gate_flag_id(gate: dict[str, str]) -> str:
    match = _GATE_TRIGGER_RE.match(gate.get("trigger", ""))
    return match.group(1) if match else ""


_STRICTNESS_GUIDE = {
    "loose": "longgar — tandai penyimpangan, jangan buang klip karena ini",
    "standard": "standar — penyimpangan wajib review manusia sebelum posting",
    "strict": "ketat — abaikan klip yang menyimpang kecuali sudah dikonfirmasi manual",
}


def campaign_rules_text(profile: Any) -> str:
    """Blok teks DETERMINISTIK siap-tempel ke prompt AI.

    Memuat ringkas strictness, rentang durasi, topik yang harus dihindari, frasa
    terlarang persis, flag_requests (AI diminta mengisi ``risk_flags`` per id),
    dan curation_note. Profil 'bebas'/kosong atau tanpa aturan yang actionable
    menghasilkan string kosong, sehingga prompt golden path tidak berubah sedikit
    pun.
    """
    if not isinstance(profile, dict):
        return ""
    rules = _clip_rules(profile)

    dur_line = ""
    dur = rules.get("duration_s")
    if isinstance(dur, dict):
        mn, mx = dur.get("min"), dur.get("max")
        if _is_number(mn) and _is_number(mx) and mn is not None and mx is not None:
            dur_line = f"- Durasi klip: {float(mn):g}-{float(mx):g} detik. Di luar rentang ini klip ditolak."

    topic_names: list[str] = []
    for entry in rules.get("banned_topics") or []:
        if isinstance(entry, dict):
            topic = str(entry.get("topic") or "").strip()
            if topic and topic not in topic_names:
                topic_names.append(topic)
    topics_line = (
        ("- Hindari topik: " + ", ".join(topic_names) + ". Jangan memancing/mengulang topik ini.")
        if topic_names
        else ""
    )

    exact_terms = [str(t).strip() for t in (rules.get("banned_exact") or []) if str(t or "").strip()]
    exact_line = (
        "- Frasa terlarang persis (dilarang muncul di ucapan klip): "
        + ", ".join(f'"{t}"' for t in exact_terms)
        + "."
        if exact_terms
        else ""
    )

    flags = _flag_requests(profile)
    flag_lines: list[str] = []
    if flags:
        flag_lines.append(
            '- Tandai risiko per klip lewat field "risk_flags": list id di bawah ini '
            "(tanpa penjelasan, tanpa spasi tambahan), kosongkan [] bila klip bersih:"
        )
        for flag in flags:
            ask = f": {flag['ask']}" if flag["ask"] else ""
            flag_lines.append(f"  - {flag['id']}{ask}")
        flag_lines.append("  Jangan mengarang id flag di luar daftar di atas.")

    note = _WS_RE.sub(" ", str(rules.get("curation_note") or "")).strip()
    note_line = f"- Arahan kurasi: {note}" if note else ""

    body = [line for line in (dur_line, topics_line, exact_line) if line]
    body.extend(flag_lines)
    if note_line:
        body.append(note_line)
    if not body:
        return ""

    strictness = str(profile.get("strictness") or "standard")
    guide = _STRICTNESS_GUIDE.get(strictness, _STRICTNESS_GUIDE["standard"])
    name = str(profile.get("name") or profile.get("id") or "Campaign")
    header = (
        f"ATURAN CAMPAIGN — {name} (kepatuhan: {strictness}; {guide}).\n"
        "Aturan ini mengalahkan instruksi lain yang bertentangan."
    )
    return header + "\n" + "\n".join(body)


def llm_flag_verdicts(risk_flags: Any, profile: Any) -> list[Verdict]:
    """Verdict dari sinyal risiko AI (``ClipMoment.risk_flags``).

    Setiap flag dicocokkan dengan human_gate ber-trigger ``llm_flag:<flag>``:
    - ada gate  -> ``warn`` dengan message teks gate (konfirmasi manusia wajib),
    - tanpa gate -> ``warn`` ("AI menandai: <flag>") pada strictness
      standard/strict, ``info`` pada loose.

    Level TIDAK PERNAH ``fail``: AI hanya menandai, keputusan tetap manusia.
    """
    if not isinstance(profile, dict):
        profile = {}
    strictness = profile.get("strictness", "standard")
    open_level = "info" if strictness == "loose" else "warn"
    gates_by_flag: dict[str, list[dict[str, str]]] = {}
    for gate in _human_gates(profile):
        fid = _gate_flag_id(gate)
        if fid:
            gates_by_flag.setdefault(fid, []).append(gate)

    verdicts: list[Verdict] = []
    seen: set[str] = set()
    flags_in = [risk_flags] if isinstance(risk_flags, str) else (risk_flags or [])
    for raw in flags_in:
        flag = flag_slug(raw)
        if not flag or flag in seen:
            continue
        seen.add(flag)
        matched = gates_by_flag.get(flag)
        if matched:
            for gate in matched:
                verdicts.append(Verdict("warn", f"llm_flag:{flag}", gate["text"]))
        else:
            verdicts.append(Verdict(open_level, f"llm_flag:{flag}", f"AI menandai: {flag}"))
    return [v if v.level != "fail" else Verdict("warn", v.code, v.message, v.evidence) for v in verdicts]


def human_gates_for(profile: Any, moments_flags: Any = None) -> list[Verdict]:
    """Gerbang review manusia yang aktif untuk satu momen/katalog.

    - gate ``always`` selalu ikut,
    - gate ``llm_flag:<id>`` ikut bila id ada di ``moments_flags``
      (list of list of flag, biasanya hasil ``[m.risk_flags for m in moments]``).

    Level per strict_mode: ``ack_always`` -> ``warn`` (wajib konfirmasi eksplisit),
    ``warn`` -> ``info`` (pengingat). Tidak pernah ``fail``.
    """
    if not isinstance(profile, dict):
        profile = {}
    fired: set[str] = set()
    groups_in = [moments_flags] if isinstance(moments_flags, str) else (moments_flags or [])
    for group in groups_in:
        if isinstance(group, str):
            group = [group]
        for raw in group or []:
            flag = flag_slug(raw)
            if flag:
                fired.add(flag)

    verdicts: list[Verdict] = []
    for gate in _human_gates(profile):
        trigger = gate["trigger"]
        if trigger != _GATE_TRIGGER_ALWAYS:
            fid = _gate_flag_id(gate)
            if not fid or fid not in fired:
                continue
        level = "warn" if gate["strict_mode"] == "ack_always" else "info"
        verdicts.append(Verdict(level, f"human_gate:{gate['id']}", gate["text"]))
    return verdicts


# --------------------------------------------------------------------------- #
# Audit trail: manusia meng-override verdict (append-only JSON-lines)
# --------------------------------------------------------------------------- #

_OVERRIDES_SUBDIR = "overrides"
_OVERRIDE_SCAN_CAP = 2000  # guard: jangan baca file audit raksasa seluruhnya


def _overrides_dir() -> Path:
    return _campaigns_dir() / _OVERRIDES_SUBDIR


def _override_path(campaign_id: str) -> Path:
    return _overrides_dir() / f"{campaign_id}.jsonl"


def _safe_audit_id(campaign_id: Any) -> str:
    """campaign_id dipakai sebagai nama file -> wajib slug tanpa separator path."""
    if isinstance(campaign_id, str) and _SAFE_ID_RE.fullmatch(campaign_id):
        return campaign_id
    return ""


def _coerce_scalar(value: Any) -> Any:
    """Simpan id/angka sewajarnya: int/float kalau bisa, sisanya string ramping."""
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def record_override(
    chat_id: Any,
    user_id: Any,
    campaign_id: Any,
    profile_version: Any,
    clip_index: Any,
    moment_start: Any,
    moment_end: Any,
    verdict_codes: Any = None,
    note: Any = "",
) -> None:
    """Catat override manusia ke ``data/campaigns/overrides/<campaign_id>.jsonl``.

    Append-only JSON-lines (audit trail). Fungsi ini TIDAK PERNAH raise: kegagalan
    audit tidak boleh memblokir user sudah menandai override; cukup ``LOGGER.warning``.
    """
    try:
        safe_id = _safe_audit_id(campaign_id)
        if not safe_id:
            LOGGER.warning(
                "record_override: campaign_id %r bukan slug aman, override tidak dicatat",
                campaign_id,
            )
            return
        codes_in = [verdict_codes] if isinstance(verdict_codes, str) else (verdict_codes or [])
        codes = [str(c).strip() for c in codes_in if str(c or "").strip()]
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chat_id": _coerce_scalar(chat_id),
            "user_id": _coerce_scalar(user_id),
            "campaign_id": safe_id,
            "profile_version": _coerce_scalar(profile_version),
            "clip_index": _coerce_scalar(clip_index),
            "moment_start": _coerce_scalar(moment_start),
            "moment_end": _coerce_scalar(moment_end),
            "verdict_codes": codes,
            "note": _WS_RE.sub(" ", str(note or "")).strip(),
        }
        path = _override_path(safe_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
    except Exception as exc:  # noqa: BLE001 - audit never blocks the user
        LOGGER.warning("record_override gagal menulis audit trail: %s", exc)


def read_overrides_tail(campaign_id: Any, n: Any = 5) -> list[dict]:
    """``n`` override terakhir (urut kronologis) untuk satu campaign. [] bila hilang/rusak."""
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = 5
    if count <= 0:
        return []
    safe_id = _safe_audit_id(campaign_id)
    if not safe_id:
        return []
    path = _override_path(safe_id)
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-_OVERRIDE_SCAN_CAP:]
    except (OSError, UnicodeDecodeError) as exc:
        LOGGER.warning("read_overrides_tail gagal membaca %s: %s", path, exc)
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # baris rusak dilewati, bukan crash
        if isinstance(item, dict):
            out.append(item)
        if len(out) >= count:
            break
    out.reverse()
    return out


# --------------------------------------------------------------------------- #
# Bot-facing summary
# --------------------------------------------------------------------------- #

def campaign_intro_lines(profile: dict) -> list[str]:
    """Human-readable account / out-of-control reminders for the bot to display."""
    name = profile.get("name") or profile.get("id") or "Campaign"
    strictness = profile.get("strictness", "standard")
    lines: list[str] = [f"📌 Campaign: {name} (strictness: {strictness})"]

    account_items = profile.get("account_items") or []
    if account_items:
        lines.append("Yang harus kamu cek manual di akun:")
        lines.extend(f"  - {item}" for item in account_items)

    out_of_control = profile.get("out_of_control") or []
    if out_of_control:
        lines.append("Di luar kendali bot (tidak bisa dijamin otomatis):")
        lines.extend(f"  - {item}" for item in out_of_control)

    return lines
