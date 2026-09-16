"""9Router/OpenAI-compatible transcript analysis."""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import config


SYSTEM_PROMPT = (
    "You are an elite Art Director, Professional Video Editor, and Viral Short-Form Content Strategist "
    "for TikTok, Instagram Reels, and YouTube Shorts. "
    "Your job is to analyze video transcripts, remove fluff, and discover viral standalone clip segments. "
    "MANDATORY REQUIREMENT: You MUST ALWAYS output all textual fields ('hook', 'topic', 'alasan', 'caption') "
    "in the EXACT SAME LANGUAGE as the video audio/transcript (e.g., English audio -> English output; Indonesian audio -> Indonesian output). "
    "You ONLY return a valid JSON object matching the requested schema, without markdown fences or pleasantries."
)

USER_PROMPT_TEMPLATE = """Analisis transkrip video berikut (format [start-end] kalimat per baris).

TUGAS UTAMA:
Temukan dan pilih SEMUA momen atau topik pembahasan paling menarik, bernilai, menghibur, kontroversial, atau berpotensi viral tinggi untuk dijadikan video vertikal pendek (TikTok, IG Reels, YouTube Shorts).
Jangan batasi hanya 3 klip! Temukan semua pilihan klip yang kuat (bisa 4, 6, 8, 10, atau lebih jika materi video kaya).

ATURAN KURASI & 5 PILAR VIRALITAS:
Evaluasi setiap momen berdasarkan 5 pilar virality untuk menentukan "viral_score" (skala 1-100):
1. HOOK STRENGTH (1-20): 3 detik pertama WAJIB menghentikan scroll (stop the scroll). Ada statement tajam, kontras, konflik, atau rasa penasaran.
2. EMOTIONAL INTENSITY (1-20): Mengandung emosi nyata (haru, marah, kagum, keresahan/rant, atau sangat relatable).
3. SHAREABILITY & COMMENT POTENTIAL (1-20): Memicu orang ingin berdebat, berkomentar, tag teman, share, atau save.
4. STANDALONE CLARITY (1-20): Momen UTUH yang bisa dipahami 100% tanpa perlu menonton video panjang aslinya.
5. PAYOFF & RETENTION (1-20): Ada reward di akhir (punchline, kesimpulan tajam, twist, atau pelajaran praktis).

ATURAN KETAT KONTEKS MANDIRI & TIMING (ANTI-POTONG & ANTI-BOCOR):
1. KONTEKS MANDIRI (STANDALONE START):
   - Klip WAJIB diawali dengan kalimat pembuka yang mandiri dan jelas bagi penonton baru.
   - DILARANG KERAS memulai klip dari tengah kalimat atau dari kata sambung lanjutan ("jadi harga rata-rata...", "karena itu...", "nah terus...") atau kata ganti menggantung ("dia bilang...", "hal itu...") tanpa subjek yang jelas.
   - Jika ada intro salam/basa-basi kosong ("halo guys...", perkenalan narasumber), lewati langsung ke kalimat pembuka topik/hook utamanya.
2. LOOP NARASI SELESAI (COMPLETE PAYOFF):
   - Klip WAJIB berakhir tepat saat kesimpulan, punchline, atau argumen penutup selesai diucapkan.
   - DILARANG KERAS memotong kalimat di tengah jalan yang membuat ucapan menggantung.
3. ANTI-BOCOR TOPIK BERIKUTNYA (ZERO TOPIC BLEED):
   - DILARANG KERAS menyisakan awal kalimat dari topik berikutnya (misal: "Sekarang kalian mungkin tanya...", "Nah yang kedua...", "Lalu selanjutnya...").
   - Akhiri klip tepat pada titik di mana gagasan topik klip ini selesai.
4. AKURASI TIMESTAMP & VERIFIKASI KATA:
   - 'start': Gunakan persis nilai [start] dari kalimat pembuka yang dipilih. JANGAN menambah/mengurangi angka sendiri.
   - 'end': Gunakan persis nilai [end] dari kalimat penutup yang dipilih. JANGAN menambah/mengurangi angka sendiri.
   - 'first_words': 3 sampai 5 kata pertama dari kalimat pembuka klip (huruf kecil).
   - 'last_words': 3 sampai 5 kata terakhir dari kalimat penutup klip (huruf kecil).
- Durasi per klip: {min_duration} hingga {max_duration} detik.
- VARIASI TOPIK: Setiap klip HARUS membahas topik/sudut pandang berbeda dari berbagai bagian video (awal, tengah, akhir).

METADATA YANG HARUS DIHASILKAN PER KLIP:
1. 'start': detik mulai klip (angka float sesuai transkrip).
2. 'end': detik selesai klip (angka float sesuai transkrip).
3. 'first_words': 3-5 kata pertama kalimat pembuka.
4. 'last_words': 3-5 kata terakhir kalimat penutup.
5. 'viral_score': nilai potensi viralitas 1-100.
6. 'rank': nomor urut peringkat (1 = paling berpotensi viral).
7. 'hook': 1 kalimat hook pembuka paling punchy untuk teks on-screen (maksimal 8 kata, HURUF KAPITAL, DALAM BAHASA VIDEO).
8. 'topic': ringkasan 1 kalimat padat tentang konteks klip (DALAM BAHASA VIDEO).
9. 'alasan': penjelasan 1-2 kalimat mengapa klip ini sangat berpotensi FYP/viral dan penjelasan kesesuaian aturan campaign jika ada (DALAM BAHASA VIDEO).
10. 'bgm_mood': pilih 1 mood musik: "chill", "epic", "sad", "upbeat", atau "suspense".
11. 'caption': teks caption medsos menarik siap posting lengkap dengan 3-5 hashtag relevan (DALAM BAHASA VIDEO).

ATURAN MUTLAK BAHASA OUTPUT (CRITICAL LANGUAGE REQUIREMENT):
- Bahasa target video ini adalah: {target_language_name} ({target_language_code}).
- SEMUA teks hasil clipping ('hook', 'topic', 'alasan', 'caption') WAJIB 100% menggunakan bahasa yang SAMA PERSIS dengan bahasa transkrip video ({target_language_name})!
- Jika transkrip video berbahasa Inggris, maka 'hook', 'topic', 'alasan', dan 'caption' HARUS 100% ditulis dalam Bahasa Inggris! DILARANG KERAS menggunakan Bahasa Indonesia jika transkrip video berbahasa Inggris.
- Jika transkrip video berbahasa Indonesia, gunakan Bahasa Indonesia yang natural, kekinian, dan menarik.
- Jika transkrip video berbahasa lain (Spanyol, Jepang, Mandarin, dll.), gunakan bahasa tersebut secara konsisten.
- DILARANG mencampur bahasa atau menggunakan bahasa yang berbeda dari bahasa penutur di video!

Kembalikan HANYA JSON object valid dengan format persis berikut:
{{
  "campaign_notes": ["<catatan syarat akun / hal di luar kendali AI yang perlu diingat user jika ada, atau []>"],
  "clips": [
    {{
      "rank": 1,
      "viral_score": 95,
      "start": <detik_float>,
      "end": <detik_float>,
      "first_words": "<3-5 kata pertama>",
      "last_words": "<3-5 kata terakhir>",
      "hook": "<KALIMAT HOOK MAKS 8 KATA HURUF KAPITAL DALAM BAHASA VIDEO>",
      "topic": "<ringkasan 1 kalimat topik dalam bahasa video>",
      "alasan": "<alasan trigger viralitas & kesesuaian campaign dalam bahasa video>",
      "bgm_mood": "upbeat",
      "caption": "<caption medsos menarik lengkap hashtag dalam bahasa video>"
    }}
  ]
}}

Transkrip:
{transcript}
"""



# --- Campaign add-ons (hanya dipakai bila campaign_rules_text terisi) -------- #
# JANGAN mengubah USER_PROMPT_TEMPLATE di atas: jalur tanpa campaign harus
# menghasilkan prompt BYTE-IDENTIK. Aturan campaign disuntik lewat replace anchor
# di bawah ini, dengan fallback append-safe bila anchor tak ditemukan.
_CAPTION_META_ANCHOR = (
    "11. 'caption': teks caption medsos menarik siap posting lengkap dengan 3-5 hashtag relevan (DALAM BAHASA VIDEO).\n"
)
_RISK_FLAGS_META_LINE = (
    "12. 'risk_flags': daftar id risiko yang TERPENUHI untuk klip ini, HANYA dari id yang "
    "disebut di blok ATURAN CAMPAIGN; isi [] bila tidak ada.\n"
)
_SCHEMA_CAPTION_ANCHOR = '"caption": "<caption medsos menarik lengkap hashtag dalam bahasa video>"'
_SCHEMA_RISK_FLAGS_LINE = (
    ',\n      "risk_flags": ["<id risiko terpenuhi dari ATURAN CAMPAIGN, atau []>"]'
)
_TRANSCRIPT_ANCHOR = "Transkrip:\n{transcript}"


def detect_transcript_language(transcript: str, hint_language: str | None = None) -> tuple[str, str]:
    """Detect language code and human name from transcript or hint."""
    hint = (hint_language or "").strip().lower()
    if hint and hint not in ("auto", "none", "unknown"):
        if hint.startswith("en"):
            return "en", "English"
        if hint.startswith("id"):
            return "id", "Indonesian (Bahasa Indonesia)"
        if hint.startswith("es"):
            return "es", "Spanish"
        if hint.startswith("ja"):
            return "ja", "Japanese"
        if hint.startswith("zh"):
            return "zh", "Chinese"
        if hint.startswith("de"):
            return "de", "German"
        if hint.startswith("fr"):
            return "fr", "French"
        if hint.startswith("ar"):
            return "ar", "Arabic"
        if hint.startswith("pt"):
            return "pt", "Portuguese"
        if hint.startswith("ru"):
            return "ru", "Russian"
        return hint, hint.upper()

    sample = transcript[:3000].lower()
    id_words = {"yang", "dan", "di", "ini", "itu", "dengan", "untuk", "dari", "tidak", "ada", "bisa", "mereka", "kita", "kamu", "saya", "jadi", "karena"}
    en_words = {"the", "and", "in", "this", "that", "with", "for", "from", "not", "have", "can", "they", "we", "you", "my", "so", "because", "what", "about"}
    tokens = set(re.findall(r"\b[a-z]{2,}\b", sample))
    id_matches = len(tokens.intersection(id_words))
    en_matches = len(tokens.intersection(en_words))

    if en_matches > id_matches and en_matches >= 3:
        return "en", "English"
    if id_matches > en_matches and id_matches >= 3:
        return "id", "Indonesian (Bahasa Indonesia)"

    return ("en", "English") if en_matches >= id_matches else ("id", "Indonesian (Bahasa Indonesia)")


def sanitize_flag(value: Any) -> str:
    """Sanitasi satu id risiko dari AI jadi slug ``[a-z0-9_]`` maks 32 karakter.

    Mencerminkan ``campaign_policy.flag_slug`` (sumber kebenaran canon) tetapi
    sengaja diduplikasi agar modul AI tidak bergantung pada modul policy.
    ``scratch/verify_step2_core.py`` menguji keduanya tetap identik.
    """
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value).lower()
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:32]


def parse_risk_flags(raw: Any, limit: int = 6) -> list[str]:
    """Ekstraksi toleran ``risk_flags`` dari satu momen JSON.

    Bukan list / hilang -> ``[]``; item non-string dilewati; duplikat dibuang;
    maks ``limit`` per momen. TIDAK PERNAH membuat validasi momen gagal.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        flag = sanitize_flag(entry)
        if flag and flag not in out:
            out.append(flag)
        if len(out) >= limit:
            break
    return out


def parse_campaign_notes(raw: Any, limit: int = 10) -> list[str]:
    """Ekstraksi catatan syarat campaign non-konten (mis. akun minimal 1000 followers)."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        s = " ".join(str(entry or "").strip().split())
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def extract_duration_bounds(
    campaign_rules_text: str | None,
    default_min: float = 15.0,
    default_max: float = 120.0,
) -> tuple[float, float]:
    """Ekstraksi batas durasi (min, max detik) dari teks aturan bebas / campaign brief."""
    if not campaign_rules_text or not str(campaign_rules_text).strip():
        return default_min, default_max

    text = str(campaign_rules_text).lower()
    min_dur: float | None = None
    max_dur: float | None = None

    # 1. Rentang durasi: "30-60 detik", "durasi 30 - 60s", "antara 30 sampai 60 detik", "30 to 60 seconds"
    range_match = re.search(
        r"(?:durasi|panjang)?\s*(?:antara\s+)?(\d{1,3})\s*(?:-|sampai|hingga|to)\s*(\d{1,3})\s*(?:detik|second|s\b)",
        text,
    )
    if range_match:
        d1 = float(range_match.group(1))
        d2 = float(range_match.group(2))
        if d1 < d2:
            min_dur, max_dur = d1, d2
        else:
            min_dur, max_dur = d2, d1

    # 2. Minimal: "minimal berdurasi 40 detik", "berdurasi minimal 40 detik", "durasi minimal 40 detik",
    # "minimal 40 detik", "minimal 40s", "min 40 detik", "paling sedikit 40 detik", "at least 40s", "di atas 40 detik"
    if min_dur is None:
        min_match = re.search(
            r"(?:minimal\s+(?:ber)?durasi|berdurasi\s+minimal|durasi\s+minimal|minimal|min\.?|paling\s+sedikit|setidaknya|at\s+least|minimum|di\s+atas|lebih\s+dari)\s*[:=]?\s*(\d{1,3})\s*(?:detik|second|s\b)?",
            text,
        )
        if min_match:
            val = float(min_match.group(1))
            if val > 0:
                min_dur = val

    # 3. Maksimal: "maksimal berdurasi 60 detik", "berdurasi maksimal 60 detik", "durasi maksimal 60 detik",
    # "maksimal 60s", "max 60 detik", "paling banyak 60 detik", "at most 60s", "di bawah 60 detik", "kurang dari 60 detik"
    if max_dur is None:
        max_match = re.search(
            r"(?:maksimal\s+(?:ber)?durasi|berdurasi\s+maksimal|durasi\s+maksimal|maksimal|maks\.?|max\.?|paling\s+banyak|at\s+most|maximum|di\s+bawah|kurang\s+dari)\s*[:=]?\s*(\d{1,3})\s*(?:detik|second|s\b)?",
            text,
        )
        if max_match:
            val = float(max_match.group(1))
            if val > 0:
                max_dur = val

    final_min = min_dur if min_dur is not None else default_min
    final_max = max_dur if max_dur is not None else default_max
    if final_min > final_max:
        final_max = max(final_min + 15.0, default_max)

    return final_min, final_max


def inject_campaign_rules(prompt: str, campaign_rules_text: str | None) -> str:
    """Sematkan blok aturan campaign + instruksi penalaran dan pemisahan syarat akun ke prompt base."""
    rules = (campaign_rules_text or "").strip()
    if not rules:
        return prompt

    dur_min, dur_max = extract_duration_bounds(rules)
    duration_instruction = ""
    if dur_min > config.MIN_CLIP_DURATION_S or dur_max < config.MAX_CLIP_DURATION_S:
        duration_instruction = (
            f"   c) ATURAN KETAT DURASI KLIP DARI USER (WAJIB DIPATUHI):\n"
            f"      - Setiap klip WAJIB memiliki durasi MINIMAL {int(dur_min)} detik dan MAKSIMAL {int(dur_max)} detik!\n"
            f"      - DILARANG KERAS mengusulkan klip dengan durasi di bawah {int(dur_min)} detik atau di atas {int(dur_max)} detik!\n"
            f"      - Jika pembahasan sebuah topik pendek tapi menarik, PERLUAS cakupan konteksnya (sertakan kalimat sebelum/sesudahnya) agar durasi mencapai minimal {int(dur_min)} detik secara utuh tanpa memotong konteks!\n"
        )

    block = (
        "\n=======================================================\n"
        "🎯 ATURAN & BRIEF CAMPAIGN DARI USER (WAJIB DIIKUTI & DINALAR OLEH AI):\n"
        f"{rules}\n\n"
        "INSTRUKSI PENALARAN CAMPAIGN UNTUK AI:\n"
        "1. PILAH ATURAN KONTEN VIDEO vs ATURAN DI LUAR TANGGUNG JAWAB AI:\n"
        "   a) ATURAN KONTEN VIDEO (Apa yang boleh & tidak boleh di dalam klip):\n"
        "      - Pahami topik yang dicari, kriteria kurasi, gaya/tone bicara, serta hal-hal yang dilarang diucapkan/dibahas.\n"
        "      - HANYA pilih dan prioritaskan momen yang selaras dengan kriteria ini. Buang dan eliminasi segmen yang melanggar!\n"
        "      - Pada SETIAP klip yang dipilih, di field 'alasan', sertakan penjelasan singkat mengapa klip ini cocok dan memenuhi aturan campaign tersebut.\n"
        f"{duration_instruction}"
        "   b) ATURAN DI LUAR KENDALI VIDEO / SYARAT AKUN (Out-of-scope / Account Rules):\n"
        "      - Jika ada aturan yang berkaitan dengan akun, profil, posting, atau metrik (contoh: 'akun harus minimal 1000 followers', 'wajib pasang link di bio', 'upload jam 19:00', 'username tanpa kata clips', dll.), "
        "AI TIDAK PERLU menolak klip karena syarat ini (karena syarat ini di luar kendali editing video).\n"
        "      - Ekstrak syarat-syarat non-video tersebut ke dalam field 'campaign_notes': list string berisi ringkasan catatan/pengingat untuk user "
        "(contoh: [\"Akun harus minimal 1000 followers\", \"Wajib pasang link di bio\"]). Isi [] jika tidak ada syarat non-video.\n"
        "2. Sesuaikan 'hook' dan 'caption' agar mendukung pesan dan tujuan campaign di atas.\n"
        "3. Untuk field 'risk_flags': jika ada sinyal risiko atau flag yang terpicu dari aturan di atas, cantumkan id-nya, atau kosongkan [] bila aman.\n"
        "=======================================================\n"
    )

    if _CAPTION_META_ANCHOR in prompt:
        prompt = prompt.replace(_CAPTION_META_ANCHOR, _CAPTION_META_ANCHOR + _RISK_FLAGS_META_LINE, 1)
    if _SCHEMA_CAPTION_ANCHOR in prompt:
        prompt = prompt.replace(_SCHEMA_CAPTION_ANCHOR, _SCHEMA_CAPTION_ANCHOR + _SCHEMA_RISK_FLAGS_LINE, 1)
    if _TRANSCRIPT_ANCHOR in prompt:
        prompt = prompt.replace(_TRANSCRIPT_ANCHOR, block + "\n" + _TRANSCRIPT_ANCHOR, 1)
    else:
        prompt = prompt + block
    return prompt


def build_user_prompt(
    transcript: str,
    campaign_rules_text: str | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    video_language: str | None = None,
) -> str:
    """Render user prompt dengan bahasa video dinamis dan injeksi aturan campaign."""
    lang_code, lang_name = detect_transcript_language(transcript, video_language)
    if min_duration is None or max_duration is None:
        dur_min, dur_max = extract_duration_bounds(campaign_rules_text)
        if min_duration is None:
            min_duration = dur_min
        if max_duration is None:
            max_duration = dur_max

    prompt = USER_PROMPT_TEMPLATE.format(
        transcript="{transcript}",
        min_duration=int(min_duration),
        max_duration=int(max_duration),
        target_language_code=lang_code,
        target_language_name=lang_name,
    )
    prompt = inject_campaign_rules(prompt, campaign_rules_text)
    return prompt.replace("{transcript}", transcript, 1)


class AIAnalyzerError(RuntimeError):
    """Raised when 9Router analysis fails."""


class NonRetryableAIAnalyzerError(AIAnalyzerError):
    """Raised for errors that should not be retried, such as 401/403."""


class ResponseFormatUnsupported(AIAnalyzerError):
    """Raised when the active 9Router target rejects response_format."""


@dataclass(frozen=True)
class ClipMoment:
    start: float
    end: float
    hook: str
    topic: str = ""
    caption: str = ""
    viral_score: int = 85
    alasan: str = ""
    bgm_mood: str = "upbeat"
    rank: int = 1
    first_words: str = ""
    last_words: str = ""
    # Id sinyal risiko dari AI (lihat campaign_policy.llm_flag_verdicts). Kosong
    # bila AI tidak menandai apa pun, jadi profil/campaign lama tetap kompatibel.
    risk_flags: list[str] = field(default_factory=list)
    # Catatan syarat akun / hal di luar kendali AI yang perlu diingat user (mis. minimal 1000 followers)
    campaign_notes: list[str] = field(default_factory=list)


def strip_json_fences(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_content(content: str) -> Any:
    stripped = strip_json_fences(content)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    first_positions = [pos for pos in (stripped.find("{"), stripped.find("[")) if pos >= 0]
    if not first_positions:
        raise json.JSONDecodeError("No JSON object or array found", stripped, 0)

    decoder = json.JSONDecoder()
    value, _end = decoder.raw_decode(stripped[min(first_positions) :])
    return value


def parse_chat_response_body(content: str) -> Any:
    """Parse normal JSON, first-object JSON, or OpenAI stream-style bodies."""
    stripped = content.strip()
    if not stripped:
        raise AIAnalyzerError("Empty 9Router response body")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    streamed_content: list[str] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            item = parse_json_content(data)
        except json.JSONDecodeError:
            continue
        try:
            choice = item["choices"][0]
            if "message" in choice and isinstance(choice["message"].get("content"), str):
                return item
            delta = choice.get("delta", {})
            if isinstance(delta.get("content"), str):
                streamed_content.append(delta["content"])
        except (KeyError, IndexError, TypeError):
            continue

    if streamed_content:
        return {"choices": [{"message": {"content": "".join(streamed_content)}}]}

    decoder = json.JSONDecoder()
    value, _end = decoder.raw_decode(stripped)
    return value


def extract_chat_message_content(body: Any) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIAnalyzerError(f"Unexpected 9Router response shape: {exc}") from exc
    if not isinstance(content, str) or not content.strip():
        raise AIAnalyzerError("9Router response message content is empty")
    return content


def validate_moments(
    raw: Any,
    video_duration_s: float,
    max_clips: int = config.MAX_PROPOSED_CLIPS,
    campaign_active: bool = False,
    min_clip_duration: float | None = None,
    max_clip_duration: float | None = None,
) -> list[ClipMoment]:
    if isinstance(raw, dict) and isinstance(raw.get("clips"), list):
        raw_items = raw["clips"]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raise AIAnalyzerError("AI response must be a JSON array or object with clips[]")

    top_campaign_notes = parse_campaign_notes(raw.get("campaign_notes")) if isinstance(raw, dict) else []

    eff_min = float(min_clip_duration) if min_clip_duration is not None else float(config.MIN_CLIP_DURATION_S)
    eff_max = float(max_clip_duration) if max_clip_duration is not None else float(config.MAX_CLIP_DURATION_S)

    moments: list[ClipMoment] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            raw_start = item.get("start") if item.get("start") is not None else item.get("start_time")
            raw_end = item.get("end") if item.get("end") is not None else item.get("end_time")
            start = float(raw_start)
            end = float(raw_end)
        except (TypeError, ValueError):
            continue

        hook = item.get("hook") or item.get("description_hook") or item.get("title_indonesia") or ""
        if not isinstance(hook, str) or not hook.strip():
            continue
        hook = " ".join(hook.strip().split())

        topic = item.get("topic") or item.get("description_context") or item.get("title_indonesia") or hook
        if isinstance(topic, str) and topic.strip():
            topic = " ".join(topic.strip().split())
        else:
            topic = hook

        caption = item.get("caption") or item.get("tiktok_caption_id") or item.get("tiktok_caption") or ""
        if isinstance(caption, str) and caption.strip():
            caption = caption.strip()
        elif campaign_active:
            # Campaign terikat: JANGAN suntik hashtag generik (#fyp dll) yang bisa
            # menabrak S&K. Cukup hook; lane campaign (caption_required) akan
            # menandai kekurangan tag wajib lewat Verdict warn, bukan silently viral.
            caption = hook
        else:
            caption = f"{hook}\n\n#trending #shorts #viral #fyp"

        # Extract viral_score (1-100)
        try:
            viral_score = int(float(item.get("viral_score", 85)))
            viral_score = max(1, min(100, viral_score))
        except (TypeError, ValueError):
            viral_score = 85

        # Extract alasan
        alasan = item.get("alasan") or item.get("reason") or item.get("why_it_works") or ""
        if isinstance(alasan, str):
            alasan = " ".join(alasan.strip().split())
        else:
            alasan = ""

        # Extract bgm_mood
        bgm_mood = item.get("bgm_mood") or "upbeat"
        if isinstance(bgm_mood, str):
            bgm_mood = bgm_mood.strip().lower()
            if bgm_mood not in {"chill", "epic", "sad", "upbeat", "suspense"}:
                bgm_mood = "upbeat"
        else:
            bgm_mood = "upbeat"

        # Extract first_words and last_words
        first_words = item.get("first_words") or ""
        if isinstance(first_words, str):
            first_words = " ".join(first_words.strip().split())
        else:
            first_words = ""

        last_words = item.get("last_words") or ""
        if isinstance(last_words, str):
            last_words = " ".join(last_words.strip().split())
        else:
            last_words = ""

        # Extract rank
        try:
            rank = int(item.get("rank", len(moments) + 1))
        except (TypeError, ValueError):
            rank = len(moments) + 1

        # Extract risk flags (sinyal risiko campaign). Toleran total: bentuk apa pun
        # yang bukan list of string dianggap "tidak ada flag", bukan momen invalid.
        risk_flags = parse_risk_flags(item.get("risk_flags"))

        # Extract campaign notes (syarat akun / di luar kendali AI yang perlu diingat user)
        item_notes = parse_campaign_notes(item.get("campaign_notes"))
        notes = item_notes or top_campaign_notes

        duration = end - start
        if start < 0 or start >= end:
            continue
        # Toleransi 1.5 detik untuk natural boundaries
        if duration < (eff_min - 1.5) or duration > (eff_max + 5.0):
            continue
        if end > video_duration_s + 2.0:
            continue

        moments.append(
            ClipMoment(
                start=start,
                end=end,
                hook=hook,
                topic=topic,
                caption=caption,
                viral_score=viral_score,
                alasan=alasan,
                bgm_mood=bgm_mood,
                rank=rank,
                first_words=first_words,
                last_words=last_words,
                risk_flags=risk_flags,
                campaign_notes=notes,
            )
        )
        if len(moments) >= max_clips:
            break

    return moments


def split_cue_on_sentences(cue: Any) -> list[dict[str, Any]]:
    """Splits a single cue if it contains a sentence boundary (.?! followed by new clause)."""
    if isinstance(cue, dict):
        text = str(cue.get("text", "")).strip()
        start = float(cue.get("start", 0.0))
        end = float(cue.get("end", 0.0))
    else:
        text = str(getattr(cue, "text", "")).strip()
        start = float(getattr(cue, "start", 0.0))
        end = float(getattr(cue, "end", 0.0))

    match = re.search(
        r"([.?!]+)\s+([A-Z0-9\"\'“‘\(\[]|[Dd]an\b|[Tt]api\b|[Jj]adi\b|[Nn]ah\b|[Kk]arena\b|[Ss]ekarang\b)",
        text,
    )
    if not match:
        return [{"start": start, "end": end, "text": text}]

    split_pos = match.end(1)
    part1_text = text[:split_pos].strip()
    part2_text = text[split_pos:].strip()

    total_len = len(text)
    if total_len == 0:
        return [{"start": start, "end": end, "text": text}]

    duration = end - start
    split_time = start + (len(part1_text) / total_len) * duration

    return [
        {"start": start, "end": split_time, "text": part1_text},
        {"start": split_time, "end": end, "text": part2_text},
    ]


def build_natural_sentences(segments: Iterable[Any]) -> list[dict[str, Any]]:
    """Reconstructs raw rolling caption fragments into grammatically clean sentences with exact start & end times."""
    expanded_cues: list[dict[str, Any]] = []
    for s in segments:
        expanded_cues.extend(split_cue_on_sentences(s))

    sentences: list[dict[str, Any]] = []
    curr_start: float | None = None
    curr_end: float | None = None
    curr_text_parts: list[str] = []

    START_SIGNALS = {
        "dan", "tapi", "namun", "tetapi", "jadi", "karena", "sehingga", "padahal",
        "sekarang", "nah", "terus", "kemudian", "makanya", "coba",
        "pertama", "kedua", "ketiga", "terakhir", "selain", "bahkan",
        "and", "but", "so", "because", "now", "well", "however", "then",
    }

    for idx, s in enumerate(expanded_cues):
        text = s["text"].strip()
        if not text:
            continue

        if curr_start is None:
            curr_start = s["start"]
        curr_end = s["end"]
        curr_text_parts.append(text)

        full_text = " ".join(curr_text_parts).strip()
        words = full_text.split()

        ends_with_terminal_punct = bool(re.search(r"[\.\?\!]\s*$", full_text))
        next_start = expanded_cues[idx + 1]["start"] if idx + 1 < len(expanded_cues) else None
        pause = (next_start - s["end"]) if next_start is not None else 0.0

        next_starts_with_signal = False
        if next_start is not None and idx + 1 < len(expanded_cues):
            next_words = expanded_cues[idx + 1]["text"].strip().split()
            if next_words and next_words[0].lower().strip(".,?!") in START_SIGNALS:
                next_starts_with_signal = True

        should_split = False
        if ends_with_terminal_punct:
            should_split = True
        elif pause >= 0.5:
            should_split = True
        elif len(words) >= 12 and (next_starts_with_signal or pause >= 0.25):
            should_split = True
        elif len(words) >= 22:
            should_split = True

        if should_split and curr_start is not None and curr_end is not None:
            sentences.append({
                "start": curr_start,
                "end": curr_end,
                "text": full_text,
            })
            curr_start = None
            curr_end = None
            curr_text_parts = []

    if curr_text_parts and curr_start is not None and curr_end is not None:
        sentences.append({
            "start": curr_start,
            "end": curr_end,
            "text": " ".join(curr_text_parts).strip(),
        })

    return sentences


def snap_moment_to_transcript(
    moment: ClipMoment,
    segments: Iterable[Any],
    min_clip_duration: float | None = None,
) -> ClipMoment:
    """Snaps moment start & end to exact sentence boundaries based on first_words/last_words or nearest cue."""
    natural_sentences = build_natural_sentences(segments)
    if not natural_sentences:
        return moment

    best_start = moment.start
    best_end = moment.end
    end_idx: int | None = None
    first_target = " ".join((moment.first_words or "").lower().split()[:4])
    last_target = " ".join((moment.last_words or "").lower().split()[-3:])

    # 1. Match start boundary
    matched_start = None
    if first_target:
        for s in natural_sentences:
            s_text = s["text"].lower()
            if first_target in s_text and abs(s["start"] - moment.start) <= 12.0:
                matched_start = float(s["start"])
                break

    if matched_start is None:
        # Snap to nearest sentence start within ±4.0 seconds
        candidates = [s for s in natural_sentences if abs(float(s["start"]) - moment.start) <= 4.0]
        if candidates:
            closest = min(candidates, key=lambda s: abs(float(s["start"]) - moment.start))
            matched_start = float(closest["start"])

    if matched_start is not None:
        best_start = matched_start

    # 2. Match end boundary
    matched_end = None
    if last_target:
        for idx_s, s in enumerate(natural_sentences):
            s_text = s["text"].lower()
            if last_target in s_text and abs(s["end"] - moment.end) <= 12.0:
                matched_end = float(s["end"])
                end_idx = idx_s
                break

    if matched_end is None:
        # Snap to nearest sentence end within ±4.0 seconds
        candidates = [(idx_s, s) for idx_s, s in enumerate(natural_sentences) if abs(float(s["end"]) - moment.end) <= 4.0]
        if candidates:
            closest_idx, closest = min(candidates, key=lambda pair: abs(float(pair[1]["end"]) - moment.end))
            matched_end = float(closest["end"])
            end_idx = closest_idx

    if matched_end is not None:
        best_end = matched_end

    # Guard: Ensure valid duration
    if best_end <= best_start:
        best_start = moment.start
        best_end = moment.end

    # Enforce minimum duration: expand forward to complete natural sentence if snapping shrank duration
    if min_clip_duration is not None and min_clip_duration > 0:
        eff_min = float(min_clip_duration)
        if (best_end - best_start) < eff_min:
            search_start = (end_idx + 1) if end_idx is not None else 0
            for extra_s in natural_sentences[search_start:]:
                if float(extra_s["end"]) - best_start >= eff_min:
                    best_end = float(extra_s["end"])
                    break
            else:
                if natural_sentences and float(natural_sentences[-1]["end"]) > best_end:
                    best_end = float(natural_sentences[-1]["end"])

    return ClipMoment(
        start=best_start,
        end=best_end,
        hook=moment.hook,
        topic=moment.topic,
        caption=moment.caption,
        viral_score=moment.viral_score,
        alasan=moment.alasan,
        bgm_mood=moment.bgm_mood,
        rank=moment.rank,
        first_words=moment.first_words,
        last_words=moment.last_words,
        risk_flags=list(moment.risk_flags),
        campaign_notes=list(getattr(moment, "campaign_notes", []) or []),
    )


def transcript_text_from_segments(segments: Iterable[Any]) -> str:
    sentences = build_natural_sentences(segments)
    lines: list[str] = []
    for s in sentences:
        start_f = float(s["start"])
        end_f = float(s["end"])
        text = str(s["text"]).strip()
        if not text or end_f <= start_f:
            continue
        lines.append(f"[{start_f:.2f}-{end_f:.2f}] {text}")
    return "\n".join(lines)


def split_transcript_text(transcript: str) -> list[str]:
    max_chunks = max(1, config.AI_MAX_TRANSCRIPT_CHUNKS)
    max_chars = max(1000, config.AI_TRANSCRIPT_CHUNK_CHARS)
    if len(transcript) > max_chars * max_chunks:
        max_chars = len(transcript) // max_chunks + 1

    chunks: list[str] = []
    current_lines: list[str] = []
    current_size = 0
    for line in transcript.splitlines():
        line_size = len(line) + 1
        if current_lines and current_size + line_size > max_chars:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_size = 0
        current_lines.append(line)
        current_size += line_size

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks or [transcript]


def dedupe_moments(
    moments: list[ClipMoment],
    max_clips: int = config.MAX_PROPOSED_CLIPS,
    min_gap_s: float = 5.0,
) -> list[ClipMoment]:
    """Deduplicate moments so no two clips significantly overlap, keeping highest viral_score."""
    # First sort by viral_score descending so the best moments claim their time slot first
    sorted_by_score = sorted(moments, key=lambda m: (-m.viral_score, m.start))
    deduped: list[ClipMoment] = []

    for moment in sorted_by_score:
        overlaps_existing = False
        for existing in deduped:
            intersection = max(0.0, min(moment.end, existing.end) - max(moment.start, existing.start))
            if intersection > 10.0:
                overlaps_existing = True
                break
            if abs(moment.start - existing.start) < min_gap_s:
                overlaps_existing = True
                break
        if not overlaps_existing:
            deduped.append(moment)
        if len(deduped) >= max_clips:
            break

    # Return sorted by viral_score descending with sequential rank
    ranked_moments: list[ClipMoment] = []
    for idx, m in enumerate(deduped, start=1):
        ranked_moments.append(
            ClipMoment(
                start=m.start,
                end=m.end,
                hook=m.hook,
                topic=m.topic,
                caption=m.caption,
                viral_score=m.viral_score,
                alasan=m.alasan,
                bgm_mood=m.bgm_mood,
                rank=idx,
                first_words=m.first_words,
                last_words=m.last_words,
                risk_flags=list(m.risk_flags),
                campaign_notes=list(getattr(m, "campaign_notes", []) or []),
            )
        )
    return ranked_moments


class AIAnalyzer:
    def __init__(
        self,
        base_url: str = config.NINEROUTER_BASE_URL,
        api_key: str = config.NINEROUTER_API_KEY,
        model: str = config.NINEROUTER_MODEL,
        logger: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.logger = logger or logging.getLogger(__name__)

    def analyze_segments(
        self,
        segments: Iterable[Any],
        video_duration_s: float,
        campaign_active: bool = False,
        campaign_rules_text: str | None = None,
        video_language: str | None = None,
        **kwargs: Any,
    ) -> list[ClipMoment]:
        transcript = transcript_text_from_segments(segments)
        if not transcript:
            raise AIAnalyzerError("Transcript is empty; cannot analyze clips")
        dur_min, dur_max = extract_duration_bounds(campaign_rules_text)
        moments = self.analyze_transcript_text(
            transcript,
            video_duration_s,
            campaign_active=campaign_active,
            campaign_rules_text=campaign_rules_text,
            video_language=video_language,
            **kwargs,
        )
        # Snap all moments to exact sentence boundaries using first_words/last_words
        snapped = [snap_moment_to_transcript(m, segments, min_clip_duration=dur_min) for m in moments]
        return dedupe_moments(snapped)

    def analyze_transcript_text(
        self,
        transcript: str,
        video_duration_s: float,
        campaign_active: bool = False,
        campaign_rules_text: str | None = None,
        video_language: str | None = None,
        **kwargs: Any,
    ) -> list[ClipMoment]:
        chunks = split_transcript_text(transcript)
        if len(chunks) == 1:
            return self._analyze_transcript_chunk(
                chunks[0],
                video_duration_s,
                campaign_active=campaign_active,
                campaign_rules_text=campaign_rules_text,
                video_language=video_language,
                **kwargs,
            )

        self.logger.info(
            "Transcript is %s chars; splitting into %s AI chunk(s)",
            len(transcript),
            len(chunks),
        )
        collected: list[ClipMoment] = []
        last_error: Exception | None = None
        for index, chunk in enumerate(chunks, start=1):
            try:
                self.logger.info("Analyzing transcript chunk %s/%s", index, len(chunks))
                collected.extend(
                    self._analyze_transcript_chunk(
                        chunk,
                        video_duration_s,
                        campaign_active=campaign_active,
                        campaign_rules_text=campaign_rules_text,
                        video_language=video_language,
                        **kwargs,
                    )
                )
            except AIAnalyzerError as exc:
                last_error = exc
                self.logger.warning("AI chunk %s/%s failed: %s", index, len(chunks), exc)
                continue

        moments = dedupe_moments(collected)
        if moments:
            self.logger.info("AI selected %s valid clip moment(s) after chunking", len(moments))
            return moments
        raise AIAnalyzerError(f"All AI transcript chunks failed or returned no valid clips: {last_error}")

    def _analyze_transcript_chunk(
        self,
        transcript: str,
        video_duration_s: float,
        campaign_active: bool = False,
        campaign_rules_text: str | None = None,
        video_language: str | None = None,
        **kwargs: Any,
    ) -> list[ClipMoment]:
        response_format_enabled = True
        last_error: Exception | None = None
        dur_min, dur_max = extract_duration_bounds(campaign_rules_text)

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                content = self._request_chat_completion(
                    transcript,
                    response_format_enabled,
                    campaign_rules_text=campaign_rules_text,
                    video_language=video_language,
                )
                raw = parse_json_content(content)
                moments = validate_moments(
                    raw,
                    video_duration_s,
                    campaign_active=campaign_active,
                    min_clip_duration=dur_min,
                    max_clip_duration=dur_max,
                )
                if moments:
                    self.logger.info("AI selected %s valid clip moment(s)", len(moments))
                    return moments
                raise AIAnalyzerError("AI response contained no valid clip moments")
            except ResponseFormatUnsupported as exc:
                response_format_enabled = False
                last_error = exc
                self.logger.warning(
                    "9Router target rejected response_format; retrying without it"
                )
                continue
            except NonRetryableAIAnalyzerError:
                raise
            except (AIAnalyzerError, json.JSONDecodeError) as exc:
                last_error = exc
                self.logger.warning(
                    "AI analysis attempt %s/%s failed: %s",
                    attempt,
                    config.MAX_RETRIES,
                    exc,
                )
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_BACKOFF_S * (2 ** (attempt - 1)))

        raise AIAnalyzerError(
            f"9Router analysis failed after {config.MAX_RETRIES} attempts: {last_error}"
        )

    def _request_chat_completion(
        self,
        transcript: str,
        response_format_enabled: bool,
        campaign_rules_text: str | None = None,
        video_language: str | None = None,
    ) -> str:
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise AIAnalyzerError("requests is required; run: pip install -r requirements.txt") from exc

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        dur_min, dur_max = extract_duration_bounds(campaign_rules_text)
        user_prompt = build_user_prompt(
            transcript,
            campaign_rules_text,
            min_duration=dur_min,
            max_duration=dur_max,
            video_language=video_language,
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        if response_format_enabled:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=config.REQUEST_TIMEOUT_S,
            )
        except requests.exceptions.Timeout as exc:
            raise AIAnalyzerError(
                f"9Router timeout after {config.REQUEST_TIMEOUT_S}s"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise AIAnalyzerError("9Router connection error/unreachable") from exc
        except requests.exceptions.RequestException as exc:
            raise AIAnalyzerError(f"9Router request error: {exc}") from exc

        if response.status_code != 200:
            response_text = response.text[:1000]
            if (
                response.status_code == 400
                and response_format_enabled
                and "response_format" in response_text.lower()
            ):
                raise ResponseFormatUnsupported(response_text)
            if response.status_code in {401, 403, 404}:
                raise NonRetryableAIAnalyzerError(
                    f"9Router HTTP {response.status_code}: {response_text}"
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise AIAnalyzerError(f"9Router HTTP {response.status_code}: {response_text}")
            raise NonRetryableAIAnalyzerError(
                f"9Router HTTP {response.status_code}: {response_text}"
            )

        try:
            body = parse_chat_response_body(response.text)
            return extract_chat_message_content(body)
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AIAnalyzerError(f"Unexpected 9Router response shape: {exc}") from exc


def moments_to_json(moments: list[ClipMoment]) -> str:
    return json.dumps([asdict(moment) for moment in moments], ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run 9Router clip analysis")
    parser.add_argument("transcript_file", type=Path, help="Text file in [start-end] text format")
    parser.add_argument("--video-duration", type=float, required=True)
    parser.add_argument(
        "--parse-response",
        type=Path,
        help="Parse a saved/mock AI response instead of calling 9Router",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print parsed clip moments; no rendering is performed.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()

    if args.parse_response:
        raw = parse_json_content(args.parse_response.read_text(encoding="utf-8"))
        moments = validate_moments(raw, args.video_duration)
    else:
        transcript = args.transcript_file.read_text(encoding="utf-8")
        moments = AIAnalyzer().analyze_transcript_text(transcript, args.video_duration)

    print(moments_to_json(moments))


if __name__ == "__main__":
    main()
