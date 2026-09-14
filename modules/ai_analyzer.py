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
    "Kamu adalah Art Director, Editor Video Profesional, dan Short-Form Content Strategist "
    "untuk TikTok, Instagram Reels, dan YouTube Shorts. "
    "Tugasmu adalah menganalisis transkrip audio, membuang semua basa-basi, dan memilih segmen "
    "yang punya daya tarik viral tinggi, narasi utuh, dan retensi maksimal. "
    "Kamu HANYA mengembalikan JSON valid sesuai skema yang diminta, tanpa markdown fences, tanpa pengantar atau penutup."
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
7. 'hook': 1 kalimat hook pembuka paling punchy untuk teks on-screen (maksimal 8 kata, HURUF KAPITAL).
8. 'topic': ringkasan 1 kalimat padat tentang konteks klip.
9. 'alasan': penjelasan 1-2 kalimat mengapa klip ini sangat berpotensi FYP/viral dan trigger emosionalnya.
10. 'bgm_mood': pilih 1 mood musik: "chill", "epic", "sad", "upbeat", atau "suspense".
11. 'caption': teks caption medsos menarik siap posting lengkap dengan 3-5 hashtag relevan.

BAHASA OUTPUT:
- Sesuaikan bahasa 'hook', 'topic', 'alasan', dan 'caption' dengan bahasa transkrip (Indonesia -> Bahasa Indonesia natural & kekinian; Inggris -> Bahasa Inggris catchy).

Kembalikan HANYA JSON object valid dengan format persis berikut:
{{
  "clips": [
    {{
      "rank": 1,
      "viral_score": 95,
      "start": <detik_float>,
      "end": <detik_float>,
      "first_words": "<3-5 kata pertama>",
      "last_words": "<3-5 kata terakhir>",
      "hook": "<KALIMAT HOOK MAKS 8 KATA HURUF KAPITAL>",
      "topic": "<ringkasan 1 kalimat topik>",
      "alasan": "<alasan trigger viralitas & retensi>",
      "bgm_mood": "upbeat",
      "caption": "<caption medsos menarik lengkap hashtag>"
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
    "11. 'caption': teks caption medsos menarik siap posting lengkap dengan 3-5 hashtag relevan.\n"
)
_RISK_FLAGS_META_LINE = (
    "12. 'risk_flags': daftar id risiko yang TERPENUHI untuk klip ini, HANYA dari id yang "
    "disebut di blok ATURAN CAMPAIGN; isi [] bila tidak ada.\n"
)
_SCHEMA_CAPTION_ANCHOR = '"caption": "<caption medsos menarik lengkap hashtag>"'
_SCHEMA_RISK_FLAGS_LINE = (
    ',\n      "risk_flags": ["<id risiko terpenuhi dari ATURAN CAMPAIGN, atau []>"]'
)
_TRANSCRIPT_ANCHOR = "Transkrip:\n{transcript}"


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


def inject_campaign_rules(prompt: str, campaign_rules_text: str | None) -> str:
    """Sematkan blok aturan campaign + syarat field ``risk_flags`` ke prompt base.

    ``campaign_rules_text`` kosong/None -> prompt dikembalikan apa adanya.
    """
    rules = (campaign_rules_text or "").strip()
    if not rules:
        return prompt

    block = (
        "\nATURAN CAMPAIGN DARI PARTNER (WAJIB):\n"
        + rules
        + "\n\nUntuk SETIAP klip, tambahkan field \"risk_flags\": list string berisi id risiko "
        "dari aturan di atas yang benar-benar terpenuhi di klip itu (kosongkan [] bila aman). "
        "Jangan mengarang id di luar daftar tersebut, dan jangan menolak klip hanya karena flag "
        "— flag hanyalah sinyal untuk review manusia.\n"
    )

    if _CAPTION_META_ANCHOR in prompt:
        prompt = prompt.replace(_CAPTION_META_ANCHOR, _CAPTION_META_ANCHOR + _RISK_FLAGS_META_LINE, 1)
    if _SCHEMA_CAPTION_ANCHOR in prompt:
        prompt = prompt.replace(_SCHEMA_CAPTION_ANCHOR, _SCHEMA_CAPTION_ANCHOR + _SCHEMA_RISK_FLAGS_LINE, 1)
    if _TRANSCRIPT_ANCHOR in prompt:
        # Letak: sesudah contoh skema JSON, sebelum heading "Transkrip:".
        prompt = prompt.replace(_TRANSCRIPT_ANCHOR, block + "\n" + _TRANSCRIPT_ANCHOR, 1)
    else:  # fallback: jangan pernah buang aturan campaign
        prompt = prompt + block
    return prompt


def build_user_prompt(
    transcript: str,
    campaign_rules_text: str | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
) -> str:
    """Render user prompt. Tanpa ``campaign_rules_text`` -> persis seperti sebelumnya."""
    prompt = USER_PROMPT_TEMPLATE.format(
        transcript="{transcript}",
        min_duration=int(config.MIN_CLIP_DURATION_S if min_duration is None else min_duration),
        max_duration=int(config.MAX_CLIP_DURATION_S if max_duration is None else max_duration),
    )
    prompt = inject_campaign_rules(prompt, campaign_rules_text)
    # Transcript disisipkan terakhir supaya isinya tidak pernah ikut di-escape/replace.
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
) -> list[ClipMoment]:
    if isinstance(raw, dict) and isinstance(raw.get("clips"), list):
        raw_items = raw["clips"]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raise AIAnalyzerError("AI response must be a JSON array or object with clips[]")

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

        duration = end - start
        if start < 0 or start >= end:
            continue
        if duration < config.MIN_CLIP_DURATION_S or duration > config.MAX_CLIP_DURATION_S:
            continue
        if end > video_duration_s:
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


def snap_moment_to_transcript(moment: ClipMoment, segments: Iterable[Any]) -> ClipMoment:
    """Snaps moment start & end to exact sentence boundaries based on first_words/last_words or nearest cue."""
    natural_sentences = build_natural_sentences(segments)
    if not natural_sentences:
        return moment

    best_start = moment.start
    best_end = moment.end
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
        for s in natural_sentences:
            s_text = s["text"].lower()
            if last_target in s_text and abs(s["end"] - moment.end) <= 12.0:
                matched_end = float(s["end"])
                break

    if matched_end is None:
        # Snap to nearest sentence end within ±4.0 seconds
        candidates = [s for s in natural_sentences if abs(float(s["end"]) - moment.end) <= 4.0]
        if candidates:
            closest = min(candidates, key=lambda s: abs(float(s["end"]) - moment.end))
            matched_end = float(closest["end"])

    if matched_end is not None:
        best_end = matched_end

    # Guard: Ensure valid duration
    if best_end <= best_start:
        best_start = moment.start
        best_end = moment.end

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
    ) -> list[ClipMoment]:
        transcript = transcript_text_from_segments(segments)
        if not transcript:
            raise AIAnalyzerError("Transcript is empty; cannot analyze clips")
        moments = self.analyze_transcript_text(
            transcript,
            video_duration_s,
            campaign_active=campaign_active,
            campaign_rules_text=campaign_rules_text,
        )
        # Snap all moments to exact sentence boundaries using first_words/last_words
        snapped = [snap_moment_to_transcript(m, segments) for m in moments]
        return dedupe_moments(snapped)

    def analyze_transcript_text(
        self,
        transcript: str,
        video_duration_s: float,
        campaign_active: bool = False,
        campaign_rules_text: str | None = None,
    ) -> list[ClipMoment]:
        chunks = split_transcript_text(transcript)
        if len(chunks) == 1:
            return self._analyze_transcript_chunk(
                chunks[0],
                video_duration_s,
                campaign_active=campaign_active,
                campaign_rules_text=campaign_rules_text,
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
    ) -> list[ClipMoment]:
        response_format_enabled = True
        last_error: Exception | None = None

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                content = self._request_chat_completion(
                    transcript, response_format_enabled, campaign_rules_text=campaign_rules_text
                )
                raw = parse_json_content(content)
                moments = validate_moments(raw, video_duration_s, campaign_active=campaign_active)
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
    ) -> str:
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise AIAnalyzerError("requests is required; run: pip install -r requirements.txt") from exc

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        user_prompt = build_user_prompt(
            transcript,
            campaign_rules_text,
            min_duration=int(config.MIN_CLIP_DURATION_S),
            max_duration=int(config.MAX_CLIP_DURATION_S),
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
