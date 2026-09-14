"""Compiler S&K -> draf ``CampaignProfile`` (sistem campaign, langkah 3).

NETRAL JARINGAN. Modul ini tidak punya satu pun panggilan HTTP/LLM: kemampuan
bahasa masuk lewat ``chat_fn(messages) -> str`` yang diinjeksi pemanggil (lane
bot/UI). Karena itu compiler bisa diuji tanpa jaringan dan dipakai backend AI
apa pun (9Router, Ollama, dsb).

Alur:
    raw S&K -> split_clauses() -> build_messages() -> chat_fn -> parse toleran
              -> sanitasi draf -> validate_profile()  [anti halusinasi]
              -> coverage gate  [anti silent-drop]    -> result dict

Kontrak lintas-lane (bot.py menulis terhadap bentuk ini — JANGAN berubah tanpa
sinkron):
    build_messages(raw_sk_text)            -> [{"role","content"}, ...]
    split_clauses(raw)                     -> list[str]  (kanonik, deterministik)
    compile_profile(raw, chat_fn)          -> {'profile','coverage','llm_raw','warnings'}
    save_draft(result, unique=True)        -> profile_id final (str)
    summarize_for_user(result)             -> str ( Markdown escaping ditangani bot)

Kegagalan selalu lewat ``CompilerError`` (punya ``.details`` list) supaya lane
bot bisa menampilkan pesan retry yang jelas. TIDAK ada retry internal: satu
panggilan ``chat_fn``, keputusan retry milik pemanggil.

Anti hardcode-drift: daftar kemampuan (rule kind) dan daftar field profil yang
boleh diisi DIAMBIL SAAT RUNTIME dari ``campaign_policy`` (CHECK_REGISTRY +
struktur validasi internalnya). Kalau registry/field bertambah, prompt ikut
tanpa perlu mengedit modul ini.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

from modules import campaign_policy as cp

LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Konstanta & exception
# --------------------------------------------------------------------------- #

PROFILE_SCHEMA_VERSION = 2
DEFAULT_STRICTNESS = "standard"

# Klausul lebih pendek dari ini dianggap bukan aturan (header, "Catatan:", dsb).
MIN_CLAUSE_LEN = 15
# Batas panjang field yang dipakai nama file / judul.
MAX_ID_LEN = 48
MAX_NAME_LEN = 80
# Ambang "profil mutu rendah": >60% klausul berakhir di out_of_control/unaccounted.
LOW_QUALITY_RATIO = 0.6
# Panjang minimal potongan yang boleh dianggap sebagai "containment match".
MIN_CONTAIN_LEN = 8
# Ambang kesamaan fuzzy untuk mengikat jawaban LLM ke klausul kanonik.
FUZZY_CUTOFF = 0.72

ChatFn = Callable[[list[dict]], str]


class CompilerError(Exception):
    """Gagal kompilasi S&K (JSON rusak, draf ditolak validator, klausul hilang).

    ``details`` berisi daftar baris error terstruktur supaya lane bot bisa
    menampilkan rincian tanpa mem-parsing string pesan.
    """

    def __init__(self, message: str, details: Iterable[Any] | None = None):
        super().__init__(message)
        self.details: list[str] = [str(d) for d in (details or [])]


# --------------------------------------------------------------------------- #
# Derivasi kemampuan/field dari campaign_policy (anti hardcode-drift)
# --------------------------------------------------------------------------- #

# Peta petunjuk kind -> field profil tempat aturan disimpan. Kind di registry
# yang TIDAK ada di peta ini tetap masuk prompt dengan instruksi generik, jadi
# penambahan CHECK_REGISTRY tidak pernah membuat prompt "buta".
_KIND_FIELD_HINT = {
    "duration_range": ("clip_rules", "duration_s"),
    "banned_exact": ("clip_rules", "banned_exact"),
    "banned_topics": ("clip_rules", "banned_topics"),
    "caption_required": ("post_rules", "caption_required"),
    "llm_flag": ("clip_rules", "flag_requests"),
    "human_gate": ("clip_rules", "human_gates"),
}

_CORE_TOP_FIELDS = (
    "schema_version",
    "id",
    "name",
    "strictness",
    "profile_version",
    "source_doc",
    "clip_rules",
    "post_rules",
    "account_items",
    "out_of_control",
)

_TOP_FIELD_FALLBACK = (
    "account_items",
    "clip_rules",
    "id",
    "name",
    "out_of_control",
    "post_rules",
    "profile_version",
    "schema_version",
    "source_doc",
    "strictness",
)


def _get(module_const: str, fallback: Any) -> Any:
    """Baca konstanta (mungkin privat) dari campaign_policy saat runtime."""
    return getattr(cp, module_const, fallback)


def _top_level_fields() -> tuple[str, ...]:
    """Field top-level profil yang dibaca ``validate_profile``.

    Diambil dari sumber ``validate_profile`` (pola ``p.get("x")`` / ``p["x"]`` /
    loop daftar string), digabung dengan daftar inti, sehingga field baru di
    validator ikut masuk prompt tanpa edit modul ini. Kalau inspeksi sumber
    gagal (mis. frozen/compiled), jatuh ke daftar inti.
    """
    found: set[str] = set(_CORE_TOP_FIELDS)
    try:
        src = inspect.getsource(cp.validate_profile)
    except (OSError, TypeError):  # pragma: no cover - sumber tidak tersedia
        src = ""
    if src:
        for pattern in (
            r"""\bp\.get\(\s*["']([a-z_][a-z0-9_]*)["']""",
            r"""\bp\[\s*["']([a-z_][a-z0-9_]*)["']\s*\]""",
        ):
            for match in re.finditer(pattern, src):
                found.add(match.group(1))
        for group in re.findall(r"""for key in \(([^)]*)\)""", src):
            for name in re.findall(r"""["']([a-z_][a-z0-9_]*)["']""", group):
                found.add(name)
    else:  # pragma: no cover
        found |= set(_TOP_FIELD_FALLBACK)
    return tuple(sorted(found))


def profile_field_map() -> dict[str, tuple[str, ...]]:
    """Denah field profil yang DIIZINKAN, divariasikan dari campaign_policy."""
    clip_keys = sorted(_get("_KNOWN_CLIP_KEYS", set()) or set())
    post_keys = sorted(_get("_KNOWN_POST_KEYS", set()) or set())
    flag_keys = sorted(_get("_KNOWN_FLAG_REQUEST_KEYS", {"id", "ask"}) or set())
    gate_keys = sorted(_get("_KNOWN_HUMAN_GATE_KEYS", {"id", "text", "trigger", "strict_mode"}) or set())
    strictness = sorted(_get("_VALID_STRICTNESS", {"loose", "standard", "strict"}) or set())
    modes = sorted(_get("_VALID_STRICT_MODES", {"warn", "ack_always"}) or set())
    return {
        "top": _top_level_fields(),
        "clip_rules": tuple(clip_keys),
        "post_rules": tuple(post_keys),
        "flag_request": tuple(flag_keys),
        "human_gate": tuple(gate_keys),
        "strictness": tuple(strictness),
        "strict_mode": tuple(modes),
    }


def rule_kinds() -> tuple[str, ...]:
    """Semua rule kind dari CHECK_REGISTRY (dibaca saat runtime)."""
    registry = _get("CHECK_REGISTRY", {}) or {}
    return tuple(sorted(str(k) for k in registry))


def _kind_lines() -> list[str]:
    """Render daftar kemampuan mesin sebagai baris prompt."""
    registry = _get("CHECK_REGISTRY", {}) or {}
    lines: list[str] = []
    for kind in sorted(str(k) for k in registry):
        meta = registry.get(kind)
        meta = meta if isinstance(meta, dict) else {}
        desc = str(meta.get("desc") or "").strip() or "(tanpa deskripsi)"
        cls = str(meta.get("class") or "?")
        hint = _KIND_FIELD_HINT.get(kind)
        if hint:
            target = f"{hint[0]}.{hint[1]}"
        else:
            target = (
                "clip_rules.<nama kind> atau post_rules.<nama kind> "
                "(kind baru: pakai nama field yang sama dengan nama kind)"
            )
        lines.append(f"- {kind} (class {cls}) -> simpan di: {target}\n  arti: {desc}")
    return lines


# --------------------------------------------------------------------------- #
# Split klausul (kanonik, deterministik)
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")
_LINE_PREFIX = re.compile(
    r"""^\s*(?:[•·\-\u2013\u2014*>]+\s*|\(?\d{1,3}[.)\:]\s*|[a-z][.)]\s+|\d+\s*\)\s*)""",
    re.IGNORECASE,
)
_SENT_SPLIT = re.compile(r"""(?<=[.!?])\s+(?=[\"'\(\[]*[A-Za-zÀ-ÿ0-9])""")
_QUOTE_STRIP = re.compile(r"""^[\"'“”‘’\s]+|[\"'“”‘’\s]+$""")
_HAS_ALNUM = re.compile(r"[0-9A-Za-zÀ-ÿ]")


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _strip_prefixes(text: str) -> str:
    prev = None
    out = text
    while out != prev:
        prev = out
        out = _LINE_PREFIX.sub("", out).strip()
        out = _QUOTE_STRIP.sub("", out).strip()
    return out


def _sentenceize(line: str) -> list[str]:
    """Pecah baris jadi kalimat pada batas [.!?] + spasi.

    Aman untuk desimal/persen ("25.1", "30%") karena butuh spasi sesudah titik,
    dan tidak memecah titik tunggal di akhir klausul.
    """
    parts = [p.strip() for p in _SENT_SPLIT.split(line) if p and p.strip()]
    return parts or [line]


def split_clauses(raw: str) -> list[str]:
    """Pecah teks S&K jadi daftar klausul kanonik (deterministik & idempoten).

    Aturan: per baris / per bullet / per kalimat pada baris panjang; whitespace
    dinormalisasi; penanda nomor/bullet dibuang; hanya klausul dengan panjang
    > ``MIN_CLAUSE_LEN`` karakter yang dipertahankan. Urutan (dan karena itu
    nomor) stabil: ``split_clauses`` dua kali pada input sama menghasilkan list
    sama, dan men-split ulang hasil split menghasilkan list yang sama.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    text = unicodedata.normalize("NFKC", raw).replace("\r\n", "\n").replace("\r", "\n")
    clauses: list[str] = []
    for line in text.split("\n"):
        line = _clean(line)
        if not line:
            continue
        line = _strip_prefixes(line)
        if not line:
            continue
        for sentence in _sentenceize(line):
            sentence = _strip_prefixes(_clean(sentence))
            if len(sentence) > MIN_CLAUSE_LEN and _HAS_ALNUM.search(sentence):
                clauses.append(sentence)
    return clauses


def numbered_clauses(raw: str) -> list[tuple[int, str]]:
    """Klausul kanonik ber-nomor 1-based (nomor yang sama dipakai di prompt)."""
    return [(i + 1, clause) for i, clause in enumerate(split_clauses(raw))]


def clause_key(text: Any) -> str:
    """Kunci pencocokan klausul: lowercase, prefix dibuang, whitespace rapi."""
    if text is None:
        return ""
    cleaned = _strip_prefixes(_clean(unicodedata.normalize("NFKC", str(text))))
    cleaned = cleaned.rstrip(".;:!?").strip()
    return cp.normalize_text(cleaned)


# --------------------------------------------------------------------------- #
# Pesan prompt
# --------------------------------------------------------------------------- #

_JSON_RULES = (
    "Jawab HANYA dengan satu JSON object valid. Tanpa markdown fence, tanpa "
    "kalimat pembuka/penutup, tanpa komentar."
)


def build_system_prompt() -> str:
    """System prompt: kemampuan mesin + denah field + aturan pemetaan + skema."""
    fields = profile_field_map()
    kinds = rule_kinds()
    lines: list[str] = [
        "Kamu adalah penyusun (compiler) dokumen Syarat & Ketentuan campaign "
        "menjadi profil kepatuhan terstruktur untuk sistem clipping video.",
        "Kamu bekerja Deterministik dan PATUH TEKS: setiap isi profil harus bisa "
        "ditelusuri ke klausul S&K yang diberikan.",
        "",
        "## Kemampuan mesin penilai (satu-satunya hal yang bisa dicek otomatis)",
    ]
    if kinds:
        lines.extend(_kind_lines())
    else:  # pragma: no cover - registry tidak pernah kosong di produksi
        lines.append("- (belum ada rule kind terdaftar)")

    lines += [
        "",
        "## Denah profil yang diterima (field lain akan DITOLAK validator)",
        f"- top-level: {', '.join(fields['top'])}",
        f"- clip_rules: {', '.join(fields['clip_rules']) or '(kosong)'}",
        f"- post_rules: {', '.join(fields['post_rules']) or '(kosong)'}",
        f"- item flag_requests: {', '.join(fields['flag_request'])}",
        f"- item human_gates: {', '.join(fields['human_gate'])}",
        f"- nilai strictness: {', '.join(fields['strictness'])}",
        f"- nilai strict_mode: {', '.join(fields['strict_mode'])}",
        "",
        "## Aturan pemetaan (WAJIB dipatuhi)",
        "1. Petakan TIAP klausul S&K bernomor ke SALAH SATU tujuan berikut:",
        "   a. field clip_rules / post_rules — hanya kalau rule kind-nya ada di "
        "daftar kemampuan di atas;",
        "   b. account_items (list string): sesuatu yang harus dikerjakan/cek "
        "manusia di akun (bukan aturan klip);",
        "   c. out_of_control (list string): di luar kendali sistem;",
        "   d. catatan kurasi (clip_rules.curation_note) atau flag_requests/"
        "human_gates untuk hal yang butuh penilaian manusia/AI.",
        "2. Klausul ambigu, subjektif, atau tidak bisa dicek mesin -> masukkan ke "
        "out_of_control dengan TEKS KLAUSUL ASLI apa adanya (verbatim, jangan "
        "diringkas/diterjemahkan).",
        "3. DILARANG MENGARANG aturan, angka, hashtag, topik, atau nama campaign "
        "yang tidak ada di teks S&K. Kalau teks tidak menyebut sesuatu, jangan "
        "isi field-nya.",
        "4. DILARANG memakai nama field/rule kind di luar denah di atas.",
        "5. Untuk setiap Klausul yang kamu petakan ke field/daftar, buat satu "
        "entri di 'mapped' berisi teks klausul verbatim dan 'dest' tujuan "
        "(contoh 'clip_rules.duration_s', 'account_items', 'post_rules."
        "caption_required').",
        "6. Klausul yang benar-benar tidak bisa kamu petakan TETAP harus "
        "disebutkan di 'unaccounted' (teks verbatim). Menghilangkan klausul tanpa "
        "jejak = kegagalan.",
        "7. Format field penting:",
        "   - clip_rules.duration_s = {\"min\": angka, \"max\": angka} (detik)",
        "   - clip_rules.banned_exact = [\"frasa persis\", ...]",
        "   - clip_rules.banned_topics = [{\"topic\": \"slug\", \"regex\": "
        "\"pattern\", ...}] — pattern ditulis dengan huruf kecil karena teks "
        "transkrip dinormalisasi lowercase",
        "   - post_rules.caption_required = {\"all_of\": [...], \"any_of\": [...]}",
        "   - clip_rules.flag_requests = [{\"id\": \"slug_kecil\", \"ask\": "
        "\"pertanyaan pendek untuk AI\"}]",
        "   - clip_rules.human_gates = [{\"id\": \"slug\", \"text\": \"instruksi "
        "review\", \"trigger\": \"always\" atau \"llm_flag:<id flag>\", "
        "\"strict_mode\": \"warn\" atau \"ack_always\"}]",
        "   - trigger 'llm_flag:<id>' WAJIB merujuk id yang ada di flag_requests.",
        "8. isi 'id' profil sebagai slug pendek [a-z0-9._-], 'name' sebagai nama "
        "campaign/partner dari teks (atau ringkasan netral bila tidak disebut), "
        "'strictness' hanya bila teks S&K benar-benar menyiratkan longgar/ketat; "
        "kalau tidak, isi \"standard\".",
        "9. schema_version = " + str(PROFILE_SCHEMA_VERSION) + ".",
        "",
        "## Skema jawaban",
        "{",
        '  "profile": {',
        f'    "schema_version": {PROFILE_SCHEMA_VERSION},',
        '    "id": "slug",',
        '    "name": "Nama Campaign",',
        '    "strictness": "standard",',
        '    "clip_rules": {},',
        '    "post_rules": {},',
        '    "account_items": [],',
        '    "out_of_control": []',
        "  },",
        '  "mapped": [{"clause": "teks klausul verbatim", "dest": "clip_rules.duration_s"}],',
        '  "out_of_control": ["teks klausul verbatim"],',
        '  "unaccounted": ["teks klausul verbatim"]',
        "}",
        "",
        _JSON_RULES,
    ]
    return "\n".join(lines)


def build_user_prompt(raw_sk_text: str) -> str:
    """User prompt: S&K bernomor per klausul hasil ``split_clauses``."""
    clauses = split_clauses(raw_sk_text)
    body = "\n".join(f"{i}. {clause}" for i, clause in enumerate(clauses, start=1))
    if not body:
        body = "(dokumen kosong / tidak ada klausul terbaca)"
    return (
        "Petakan dokumen Syarat & Ketentuan campaign berikut menjadi profil "
        "kepatuhan sesuai instruksi sistem.\n\n"
        "DAFTAR KLAUSUL (nomor stabil, gunakan teks persis untuk 'clause'/"
        "'out_of_control'/'unaccounted'):\n" + body + "\n\n"
        f"Jumlah klausul = {len(clauses)}. Jawaban harus mencakup semua nomor."
    )


def build_messages(raw_sk_text: str) -> list[dict]:
    """Pesan chat (system + user) untuk satu dokumen S&K."""
    return [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_prompt(raw_sk_text)},
    ]


# --------------------------------------------------------------------------- #
# Parsing JSON toleran (pola mirip ai_analyzer, implementasi internal)
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return _FENCE_RE.sub("", text).strip()


def _extract_json(text: str) -> Any:
    """Ambil JSON terluar: whole-parse, blok ```json```, atau {...} terluar."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("respons kosong")
    stripped = _strip_fences(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    starts = [pos for pos in (stripped.find("{"), stripped.find("[")) if pos >= 0]
    if not starts:
        raise json.JSONDecodeError("tidak ada objek JSON di dalam respons", stripped, 0)
    decoder = json.JSONDecoder()
    value, _end = decoder.raw_decode(stripped[min(starts):])
    return value


# --------------------------------------------------------------------------- #
# Sanitasi draf
# --------------------------------------------------------------------------- #

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_SLUG_DASH_RE = re.compile(r"[-.]{2,}")
_NAME_BAD = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\ufeff]+")


def sanitize_id(value: Any, fallback: str = "campaign") -> str:
    """Paksa 'id' jadi slug aman (valid untuk validate_profile & nama file)."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    text = _WS.sub("-", text.strip().lower())
    text = _SLUG_RE.sub("-", text)
    text = _SLUG_DASH_RE.sub("-", text).strip("-._")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", text or ""):
        text = re.sub(r"^[^a-z0-9]+", "", text)
    text = text[:MAX_ID_LEN].rstrip("-._")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", text or ""):
        text = fallback
    return text[:MAX_ID_LEN].rstrip("-._") or fallback


def sanitize_name(value: Any, fallback: str = "Campaign tanpa nama") -> str:
    text = _NAME_BAD.sub(" ", str(value or ""))
    text = _WS.sub(" ", text).strip(" \t-–—:;,.")
    if not text:
        text = fallback
    if len(text) > MAX_NAME_LEN:
        text = text[: MAX_NAME_LEN - 1].rstrip() + "…"
    return text


def derive_name(raw_sk_text: str) -> str:
    """Nama fallback dari baris pertama S&K yang berarti."""
    for line in (raw_sk_text or "").splitlines():
        line = _clean(line)
        line = _strip_prefixes(line)
        if len(line) >= 4:
            return sanitize_name(line)
    return sanitize_name(None)


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("text") or item.get("item") or item.get("value") or ""
        text = _clean("" if item is None else str(item))
        if not text:
            continue
        key = cp.normalize_text(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _drop_empty(container: dict, key: str) -> None:
    if key in container and container[key] in (None, {}, [], ""):
        del container[key]


def _sanitize_rules(rules: Any, allowed: Iterable[str], where: str, warnings: list[str]) -> dict:
    """Normalisasi layer rules TANPA membuang field tak dikenal.

    Field/rule kind karangan LLM (mis. ``deteksi_watermark``) DIBIARKAN agar
    ``validate_profile`` menolaknya dengan pesan yang menyebut nama kind itu
    (anti-halusinasi + jejak jelas). Kita hanya mencatat warning internal.
    """
    if rules is None:
        return {}
    if not isinstance(rules, dict):
        warnings.append(f"{where}: bukan dict ({type(rules).__name__}), dibiarkan untuk validator")
        return {}
    allowed_set = set(allowed or ())
    for key in rules:
        if key not in allowed_set:
            warnings.append(f"{where}: rule kind/field tak dikenal '{key}' (akan ditolak validator)")
    return dict(rules)


def _sanitize_draft(draft: Any, raw_sk_text: str) -> tuple[dict, list[str]]:
    fields = profile_field_map()
    warnings: list[str] = []
    if not isinstance(draft, dict):
        raise CompilerError("bagian 'profile' dalam jawaban LLM bukan JSON object", [f"jenis data: {type(draft).__name__}"])

    clip_keys = set(fields["clip_rules"] or ())
    post_keys = set(fields["post_rules"] or ())
    allowed_top = set(fields["top"])
    p: dict[str, Any] = {}
    misplaced: list[str] = []
    dropped: list[str] = []
    for key, value in draft.items():
        if key in allowed_top:
            p[key] = value
        elif key in clip_keys or key in post_keys:
            # LLM menaruh aturan di top-level -> dipindah ke layer yang benar
            # supaya tidak hilang diam-diam (coverage gate tetap mengeceknya).
            layer = "clip_rules" if key in clip_keys else "post_rules"
            target = p.setdefault(layer, {})
            if not isinstance(target, dict):  # pragma: no cover - sudah dinormalisasi di bawah
                target = {}
                p[layer] = target
            target.setdefault(key, value)
            misplaced.append(f"{key} -> {layer}")
        else:
            dropped.append(str(key))
    if misplaced:
        warnings.append("field dipindah ke layer profil: " + ", ".join(sorted(misplaced)))
    if dropped:
        warnings.append("field top-level di luar denah dibuang: " + ", ".join(sorted(dropped)))

    p["schema_version"] = PROFILE_SCHEMA_VERSION
    pv = p.get("profile_version")
    p["profile_version"] = pv if (isinstance(pv, (int, float)) and not isinstance(pv, bool)) else 1
    p["name"] = sanitize_name(p.get("name") or derive_name(raw_sk_text))
    p["id"] = sanitize_id(p.get("id") or sanitize_id(p["name"], fallback="campaign").replace(".", "-"))

    strictness = cp.normalize_text(p.get("strictness") or "")
    if strictness not in set(fields["strictness"] or {DEFAULT_STRICTNESS}):
        if p.get("strictness") not in (None, ""):
            warnings.append(f"strictness '{p.get('strictness')}' tidak dikenal -> '{DEFAULT_STRICTNESS}'")
        strictness = DEFAULT_STRICTNESS
    p["strictness"] = strictness

    p["clip_rules"] = _sanitize_rules(p.get("clip_rules"), fields["clip_rules"], "clip_rules", warnings)
    p["post_rules"] = _sanitize_rules(p.get("post_rules"), fields["post_rules"], "post_rules", warnings)
    p["account_items"] = _as_str_list(p.get("account_items"))
    p["out_of_control"] = _as_str_list(p.get("out_of_control"))

    # regex banned_topics harus list (validator menolak string tunggal)
    topics = p["clip_rules"].get("banned_topics")
    if isinstance(topics, list):
        fixed: list[Any] = []
        for entry in topics:
            if isinstance(entry, dict):
                entry = dict(entry)
                regex = entry.get("regex")
                if isinstance(regex, str):
                    entry["regex"] = [regex]
                elif regex is None:
                    entry["regex"] = []
                fixed.append(entry)
            elif isinstance(entry, str):
                fixed.append({"topic": sanitize_id(entry, fallback="topik"), "regex": [cp.normalize_text(entry)]})
        p["clip_rules"]["banned_topics"] = fixed
        if not p["clip_rules"]["banned_topics"]:
            del p["clip_rules"]["banned_topics"]

    for key in ("duration_s", "caption_required", "curation_note", "flag_requests", "human_gates", "platforms"):
        _drop_empty(p["clip_rules"], key)
        _drop_empty(p["post_rules"], key)

    return p, warnings


# --------------------------------------------------------------------------- #
# Coverage gate (anti silent-drop)
# --------------------------------------------------------------------------- #

def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= MIN_CONTAIN_LEN and len(b) >= MIN_CONTAIN_LEN:
        if a in b or b in a:
            return 0.95
    return SequenceMatcher(None, a, b).ratio()


def _best_match(clause_key: str, pool: list[tuple[int, str]]) -> int:
    """Indeks entri pool paling mirip dengan klausul, -1 bila di bawah ambang."""
    best_idx, best_score = -1, 0.0
    for idx, key in pool:
        score = _similarity(clause_key, key)
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx if best_score >= FUZZY_CUTOFF else -1


def _pool_from_mapped(mapped: Any) -> list[tuple[str, str]]:
    """[(teks_klausul, dest)] dari jawaban LLM (toleran bentuk blob)."""
    out: list[tuple[str, str]] = []
    if not isinstance(mapped, list):
        return out
    for entry in mapped:
        if isinstance(entry, dict):
            text = entry.get("clause") or entry.get("klausul") or entry.get("text") or ""
            dest = entry.get("dest") or entry.get("destination") or entry.get("ke") or ""
            if not str(text).strip():
                continue
            out.append((str(text), str(dest) or "catatan"))
        elif isinstance(entry, str) and entry.strip():
            out.append((entry, "catatan"))
    return out


def build_coverage(clauses: list[str], payload: dict, draft: dict) -> dict:
    """Hitung coverage: tiap klausul harus punya jejak (mapped/out_of_control/unaccounted)."""
    mapped_pool = _pool_from_mapped(payload.get("mapped"))
    ooc_texts = _as_str_list(payload.get("out_of_control")) + _as_str_list(draft.get("out_of_control"))
    unacc_texts = _as_str_list(payload.get("unaccounted"))

    mapped_keys = [(i, clause_key(t)) for i, (t, _d) in enumerate(mapped_pool) if clause_key(t)]
    ooc_keys = [(i, clause_key(t)) for i, t in enumerate(ooc_texts) if clause_key(t)]
    unacc_keys = [(i, clause_key(t)) for i, t in enumerate(unacc_texts) if clause_key(t)]

    coverage: dict[str, Any] = {"mapped": [], "out_of_control": [], "unaccounted": []}
    missing: list[str] = []
    used_mapped: set[int] = set()

    for clause in clauses:
        key = clause_key(clause)
        if not key:
            continue
        hit = _best_match(key, [(i, k) for i, k in mapped_keys if i not in used_mapped])
        if hit >= 0:
            used_mapped.add(hit)
            coverage["mapped"].append({"clause": clause, "dest": mapped_pool[hit][1].strip() or "catatan"})
            continue
        if _best_match(key, ooc_keys) >= 0:
            coverage["out_of_control"].append(clause)
            continue
        if _best_match(key, unacc_keys) >= 0:
            coverage["unaccounted"].append(clause)
            continue
        missing.append(clause)

    if missing:
        raise CompilerError(
            "klausul hilang tanpa jejak: " + json.dumps(missing, ensure_ascii=False),
            missing,
        )
    return coverage


def _sync_out_of_control(draft: dict, coverage: dict) -> None:
    """out_of_control profil = teks klausul verbatim (+ catatan tambahan LLM)."""
    merged = _as_str_list(coverage.get("out_of_control")) + [
        t for t in _as_str_list(draft.get("out_of_control"))
        if clause_key(t) not in {clause_key(c) for c in coverage.get("out_of_control") or []}
    ] + _as_str_list(coverage.get("unaccounted"))
    draft["out_of_control"] = merged


# --------------------------------------------------------------------------- #
# API utama
# --------------------------------------------------------------------------- #

def compile_profile(raw_sk_text: Any, chat_fn: ChatFn) -> dict:
    """Kompilasi teks S&K jadi draf profil tervalidasi + laporan coverage.

    ``chat_fn(messages) -> str`` dipanggil TEPAT SATU KALI (tanpa retry
    internal). Mengembalikan::

        {'profile': <draft lolos validate_profile>,
         'coverage': {'mapped': [{'clause','dest'}], 'out_of_control': [...],
                      'unaccounted': [...]},
         'llm_raw': <string mentah dari chat_fn>,
         'warnings': [str, ...]}

    Raise ``CompilerError`` untuk: S&K tanpa klausul, chat_fn gagal, JSON rusak,
    draf ditolak validator, dan klausul hilang tanpa jejak.
    """
    if not callable(chat_fn):
        raise CompilerError("chat_fn wajib callable (messages) -> str")
    text = raw_sk_text if isinstance(raw_sk_text, str) else ("" if raw_sk_text is None else str(raw_sk_text))

    clauses = split_clauses(text)
    if not clauses:
        raise CompilerError(
            "tidak ada klausul terbaca dari dokumen S&K",
            ["pastikan teks punya minimal satu baris/klausul > %d karakter" % MIN_CLAUSE_LEN],
        )

    messages = build_messages(text)
    try:
        raw_out = chat_fn(messages)
    except CompilerError:
        raise
    except Exception as exc:  # noqa: BLE001 - backend apa pun -> satu exception lane
        raise CompilerError(f"AI gagal menjawab: {exc}", [f"jenis: {type(exc).__name__}"]) from exc

    try:
        payload = _extract_json(raw_out if isinstance(raw_out, str) else json.dumps(raw_out))
    except Exception as exc:  # json.JSONDecodeError, ValueError, TypeError
        raise CompilerError(
            "jawaban AI bukan JSON yang bisa dibaca, coba kirim ulang dokumen S&K",
            [f"penyebab: {exc}", (str(raw_out) or "")[:400]],
        ) from exc

    if isinstance(payload, list):
        payload = {"profile": payload[0] if payload and isinstance(payload[0], dict) else {}}
    if not isinstance(payload, dict):
        raise CompilerError("jawaban AI bukan JSON object", [f"jenis data: {type(payload).__name__}"])

    draft_raw = payload.get("profile")
    if draft_raw is None and ("clip_rules" in payload or "schema_version" in payload or "strictness" in payload):
        draft_raw = payload  # toleransi: LLM menjawab profil langsung
    if draft_raw is None:
        raise CompilerError("jawaban AI tidak punya bagian 'profile'", [f"kunci ada: {sorted(map(str, payload))[:12]}"])

    draft, warnings = _sanitize_draft(draft_raw, text)

    errors = cp.validate_profile(draft)
    if errors:
        raise CompilerError(
            "draf profil ditolak validator campaign_policy",
            errors + [f"rule kind dikenal: {', '.join(rule_kinds())}",
                      f"field clip_rules: {', '.join(profile_field_map()['clip_rules'])}",
                      f"field post_rules: {', '.join(profile_field_map()['post_rules'])}"],
        )

    coverage = build_coverage(clauses, payload, draft)
    _sync_out_of_control(draft, coverage)

    residual = cp.validate_profile(draft)
    if residual:  # pragma: no cover - sync tidak boleh merusak profil
        raise CompilerError("draf profil rusak setelah penyelarasan out_of_control", residual)

    LOGGER.info(
        "compile_profile ok: %d klausul -> %d mapped, %d out_of_control, %d unaccounted (%d warning sanitasi)",
        len(clauses), len(coverage["mapped"]), len(coverage["out_of_control"]),
        len(coverage["unaccounted"]), len(warnings),
    )
    return {"profile": draft, "coverage": coverage, "llm_raw": raw_out if isinstance(raw_out, str) else str(raw_out), "warnings": warnings}


# --------------------------------------------------------------------------- #
# Persistensi draf
# --------------------------------------------------------------------------- #

def campaigns_dir() -> Path:
    getter = getattr(cp, "_campaigns_dir", None)
    if callable(getter):
        return Path(str(getter()))
    import config  # lokal: hanya dipakai bila policy tak menyediakan helper

    return Path(str(config.CAMPAIGNS_DIR))


def profile_path(profile_id: str) -> Path:
    maker = getattr(cp, "_profile_path", None)
    if callable(maker):
        return Path(str(maker(profile_id)))
    return campaigns_dir() / f"{profile_id}.json"


def _draft_of(result: Any) -> dict:
    """Profil dari hasil ``compile_profile`` (terima juga dict profil mentah)."""
    if not isinstance(result, dict):
        raise CompilerError("result wajib dict hasil compile_profile")
    inner = result.get("profile")
    return inner if isinstance(inner, dict) else result


def _coverage_of(result: Any) -> dict:
    coverage = result.get("coverage") if isinstance(result, dict) else None
    return coverage if isinstance(coverage, dict) else {}


def _str_list_of(value: Any) -> list:
    return value if isinstance(value, list) else []


def _id_with_suffix(base_id: str, n: int) -> str:
    if n <= 1:
        return base_id[:MAX_ID_LEN]
    suffix = f"-{n}"
    return base_id[: MAX_ID_LEN - len(suffix)].rstrip("-._") + suffix


def save_draft(result: Any, unique: bool = True) -> str:
    """Simpan draf via ``campaign_policy.save_profile``; KEMBALIKAN id final.

    ``result`` boleh dict hasil ``compile_profile`` atau profil itu sendiri.
    ``unique=True`` -> bila ``<id>.json`` sudah ada, id diberi akhiran -2, -3,
    dst sampai bebas. ID FINAL adalah nilai RETURN — draf di ``result`` TIDAK
    diubah (logical id-nya dipertahankan) supaya memanggil ulang ``save_draft``
    pada hasil yang sama menghasilkan rantai suffix yang deterministik
    (``x`` -> ``x-2`` -> ``x-3``), bukan ``x-2-2``. Pakai return value untuk
    mereferensikan file di disk. File sumber tidak disentuh setelah simpan.
    """
    if not isinstance(result, dict):
        raise CompilerError("result wajib dict hasil compile_profile")
    draft = _draft_of(result)
    base = sanitize_id(draft.get("id"))
    n = 1
    while True:
        candidate = _id_with_suffix(base, n)
        if not unique or not profile_path(candidate).exists():
            break
        n += 1
    payload = dict(draft)
    payload["id"] = candidate
    if candidate != base:
        LOGGER.warning("profile id '%s' sudah dipakai -> disimpan sebagai '%s'", base, candidate)
    try:
        cp.save_profile(payload)
    except ValueError as exc:  # validate_profile gagal
        raise CompilerError("draf profil ditolak saat disimpan", [str(exc)]) from exc
    except OSError as exc:
        raise CompilerError(f"gagal menulis profil '{candidate}': {exc}") from exc
    return candidate


# --------------------------------------------------------------------------- #
# Ringkasan untuk user (Telegram)
# --------------------------------------------------------------------------- #

def _bullets(items: Iterable[Any], prefix: str = "  • ") -> list[str]:
    return [f"{prefix}{item}" for item in items if str(item or "").strip()]


def quality_warnings(result: Any) -> list[str]:
    """Peringatan mutu-rendah (>60% klausul tidak bisa dipetakan otomatis)."""
    coverage = _coverage_of(result)
    mapped = _str_list_of(coverage.get("mapped"))
    out = _str_list_of(coverage.get("out_of_control"))
    unacc = _str_list_of(coverage.get("unaccounted"))
    total = len(mapped) + len(out) + len(unacc)
    if total <= 0:
        return []
    unmapped = len(out) + len(unacc)
    if unmapped / total > LOW_QUALITY_RATIO:
        return [
            f"PERINGATAN: profil mutu rendah — {unmapped} dari {total} klausul "
            f"({unmapped / total:.0%}) tidak bisa dipetakan otomatis "
            "(out_of_control/unaccounted). Sebaiknya perbaiki teks S&K atau "
            "susun profil manual."
        ]
    return []


def summarize_for_user(result: Any) -> str:
    """Ringkasan draf siap-kirim Telegram (escaping Markdown ditangani bot)."""
    draft = _draft_of(result)
    coverage = _coverage_of(result)
    clip_rules = draft.get("clip_rules")
    post_rules = draft.get("post_rules")
    clip = clip_rules if isinstance(clip_rules, dict) else {}
    post = post_rules if isinstance(post_rules, dict) else {}
    lines: list[str] = []

    lines.append(
        f"🧾 Draf profil: {draft.get('name') or '(tanpa nama)'} "
        f"(id: {draft.get('id')}, strictness: {draft.get('strictness')})"
    )

    sec: list[str] = []
    dur = clip.get("duration_s")
    if isinstance(dur, dict):
        sec.append(f"Durasi klip: {dur.get('min')}-{dur.get('max')} detik")
    if clip.get("banned_exact"):
        sec.append("Frasa terlarang: " + ", ".join(f'"{t}"' for t in clip["banned_exact"]))
    if clip.get("banned_topics"):
        names = [str(t.get("topic")) for t in clip["banned_topics"] if isinstance(t, dict)]
        sec.append("Topik sensitif: " + (", ".join(names) if names else "(lihat profil)"))
    if clip.get("curation_note"):
        note = _clean(str(clip["curation_note"]))
        sec.append("Arahan kurasi: " + (note[:180] + "…" if len(note) > 180 else note))
    cap = post.get("caption_required")
    if isinstance(cap, dict):
        if cap.get("all_of"):
            sec.append("Caption wajib: " + ", ".join(str(x) for x in cap["all_of"]))
        if cap.get("any_of"):
            sec.append("Caption salah satu: " + ", ".join(str(x) for x in cap["any_of"]))
    plats = post.get("platforms")
    if isinstance(plats, dict) and plats:
        for pname, pdata in plats.items():
            append = pdata.get("append") if isinstance(pdata, dict) else None
            if append:
                sec.append(f"Tambahan caption {pname}: " + ", ".join(str(x) for x in append))
    if sec:
        lines.append("")
        lines.append("📋 Aturan yang tertangkap:")
        lines.extend(_bullets(sec))

    gates = clip.get("human_gates") or []
    flags = clip.get("flag_requests") or []
    if flags or gates:
        lines.append("")
        lines.append("🚦 Gerbang & tanda yang dibuat:")
        for f in flags:
            if isinstance(f, dict):
                lines.extend(_bullets([f"flag `{f.get('id')}`: {f.get('ask', '')}"]))
        for g in gates:
            if isinstance(g, dict):
                mode = g.get("strict_mode") or "warn"
                lines.extend(_bullets([f"gate `{g.get('id')}` ({g.get('trigger')}, {mode}): {g.get('text', '')}"]))

    account_items = draft.get("account_items") or []
    if account_items:
        lines.append("")
        lines.append("🧍 Harus kamu kerjakan manual di akun:")
        lines.extend(_bullets(account_items))

    out = coverage.get("out_of_control") or []
    if out:
        lines.append("")
        lines.append("🚫 Di luar kendali bot (butuh manusia/kebijakan):")
        lines.extend(_bullets(_shorten(out)))

    unacc = coverage.get("unaccounted") or []
    if unacc:
        lines.append("")
        lines.append("❓ Klausul yang tidak sempat dipetakan AI:")
        lines.extend(_bullets(_shorten(unacc)))

    mapped_n = len(coverage.get("mapped") or [])
    total = mapped_n + len(out) + len(unacc)
    lines.append("")
    lines.append(
        f"🧮 Coverage: {mapped_n}/{total} klausul terpetakan otomatis, "
        f"{len(out)} out_of_control, {len(unacc)} tanpa tujuan."
    )

    warns = quality_warnings({"coverage": coverage}) + list(result.get("warnings") or [])
    if warns:
        lines.append("")
        lines.extend(_bullets(warns, prefix="⚠️ "))

    return "\n".join(lines)


def _shorten(items: list[str], limit: int = 6, width: int = 120) -> list[str]:
    out: list[str] = []
    for item in items[:limit]:
        text = _clean(str(item))
        out.append(text[:width] + "…" if len(text) > width else text)
    if len(items) > limit:
        out.append(f"(+{len(items) - limit} klausul lain)")
    return out
