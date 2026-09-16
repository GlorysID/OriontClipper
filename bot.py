"""ContentClipper Telegram Bot — clear UX flow with stage tracking."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import os
import re
import shutil
import sys
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any

import yt_dlp
import telegram
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

import config
from modules import bot_ui, youtube_flow, gdrive_flow
from modules.ai_analyzer import (
    AIAnalyzer,
    AIAnalyzerError,
    ClipMoment,
    extract_chat_message_content,
    extract_duration_bounds,
    parse_chat_response_body,
)
from modules.file_manager import (
    ensure_directories,
    make_job_tmp_dir,
    move_to_failed,
    move_to_processed,
)
from modules.transcriber import TranscriptionError, WhisperTranscriber
from modules.video_editor import (
    DEFAULT_HOOK_TEMPLATE,
    VideoEditError,
    VideoEditor,
    load_named_template,
    merge_template,
    probe_video,
)


LOGGER = logging.getLogger("contentclipper.bot")

YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be|m\.youtube\.com)",
    re.IGNORECASE,
)

COOKIE_PATH = config.PROJECT_ROOT / "tmp" / "cookies_shared.txt"

# Campaign Compliance (opsional — modul dibangun terpisah; bot harus tetap hidup
# kalau modul belum ada / error saat import).
cp: Any = None
CAMPAIGN_OK = False
_CP_IMPORT_ERR = "import belum dicoba"
try:
    from modules import campaign_policy as _campaign_policy  # noqa: N813
    cp = _campaign_policy
    CAMPAIGN_OK = True
    _CP_IMPORT_ERR = ""
except Exception as _cp_exc:  # pragma: no cover - jalur pengaman
    _CP_IMPORT_ERR = str(_cp_exc)

CAMPAIGN_FREE_ID = "bebas"
CAMPAIGN_CC_IMPORT = "campaign_compiler"  # lane paralel — lazy import, boleh belum ada

if not CAMPAIGN_OK:
    LOGGER.warning(
        "modules.campaign_policy tidak tersedia (%s) — fitur Campaign Compliance dimatikan.",
        _CP_IMPORT_ERR,
    )

# Global lock — only 1 video at a time (2-core VPS)
_PROCESSING_LOCK = threading.Lock()

def try_claim_lock() -> bool:
    """Atomically check and claim the global processing lock. True = claimed."""
    return _PROCESSING_LOCK.acquire(blocking=False)

def release_lock() -> None:
    _PROCESSING_LOCK.release()

# ---------------------------------------------------------------------------
# Keyboard menus
# ---------------------------------------------------------------------------

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Proses Video"), KeyboardButton("Pilihan Klip")],
        [KeyboardButton("🎨 Style & Hook"), KeyboardButton("Cookies YouTube")],
        [KeyboardButton("Bantuan"), KeyboardButton("Tentang Bot")],
    ],
    resize_keyboard=True,
)

BACK_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton("Kembali ke Menu")]],
    resize_keyboard=True,
)

YOUTUBE_HELP_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Cara Export Cookies")],
        [KeyboardButton("Kembali ke Menu")],
    ],
    resize_keyboard=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_allowed(user_id: int) -> bool:
    if not config.TELEGRAM_ALLOWED_USERS:
        return True
    allowed = [int(x.strip()) for x in config.TELEGRAM_ALLOWED_USERS.split(",") if x.strip()]
    return user_id in allowed


def is_youtube_url(text: str) -> bool:
    if not text:
        return False
    text = text.strip().split()[0]
    return bool(YOUTUBE_RE.search(text))


def is_processing(context: ContextTypes.DEFAULT_TYPE) -> bool:
    job = context.user_data.get("active_job")
    if job and job.get("rendering"):
        return True
    return context.user_data.get("processing", False)


def strip_ansi(text: Any) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text or "")).strip()


def is_bot_check_error(exc: Any) -> bool:
    msg = strip_ansi(exc).lower()
    return any(k in msg for k in ("sign in to confirm", "not a bot", "login_required", "exporting youtube cookies"))


def has_cookies() -> bool:
    return COOKIE_PATH.exists() and COOKIE_PATH.stat().st_size > 50


def download_youtube_video(url: str, output_dir: Path, user_id: int) -> Path | None:
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(output_dir / "%(title).100s.%(ext)s")

    # Ensure Deno JS runtime is in PATH
    deno_path = Path.home() / ".deno" / "bin"
    if deno_path.exists() and str(deno_path) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{deno_path}:{os.environ.get('PATH', '')}"

    ydl_opts: dict = {
        "format": "best[height<=720]/best",
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "remote_components": ["ejs:github"],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        },
    }

    node_exe = shutil.which("node") or (
        r"C:\Program Files\nodejs\node.exe"
        if Path(r"C:\Program Files\nodejs\node.exe").exists()
        else None
    )
    if node_exe:
        ydl_opts["js_runtimes"] = {"node": {"path": str(node_exe)}}

    cookie_tmp = COOKIE_PATH.parent / f"cookies_ytdlp_{os.getpid()}.txt"
    if COOKIE_PATH.exists():
        if cookie_tmp.exists():
            try:
                cookie_tmp.chmod(0o666)
            except Exception:
                pass
        shutil.copy2(COOKIE_PATH, cookie_tmp)
        if os.name != "nt":
            cookie_tmp.chmod(0o600)
        ydl_opts["cookiefile"] = str(cookie_tmp)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
            if not path.exists():
                for f in output_dir.iterdir():
                    if f.is_file() and f.suffix in config.SUPPORTED_EXTENSIONS:
                        return f
                return None
            return path
    except Exception as exc:
        LOGGER.error("YouTube download failed for user %s: %s", user_id, exc)
        raise
    finally:
        cookie_tmp = COOKIE_PATH.parent / f"cookies_ytdlp_{os.getpid()}.txt"
        if cookie_tmp.exists():
            cookie_tmp.unlink(missing_ok=True)


class ProgressState:
    def __init__(self, on_update: Any = None) -> None:
        self.stage: str = ""
        self.detail: str = ""
        self.on_update = on_update

    def set(self, stage: str, detail: str) -> None:
        self.stage = stage
        self.detail = detail
        if self.on_update:
            try:
                self.on_update(stage, detail)
            except Exception:
                pass

    def format(self, elapsed: float) -> str:
        return f"{self.detail} ({int(elapsed)}s)"


def run_transcribe(source_path: Path, work_dir: Path, progress: ProgressState):
    """Transcribe audio — call in executor."""
    progress.set("transcribe", "Transkripsi audio dengan Whisper... (~10-15 mnt)")
    transcriber = WhisperTranscriber(logger=LOGGER)
    return transcriber.transcribe_video(source_path, work_dir)


def campaign_analyze_kwargs(
    analyzer: Any, campaign_active: bool, campaign_rules_text: str | None
) -> dict[str, Any]:
    """Kwargs untuk ``AIAnalyzer.analyze_segments`` (dipanggil di executor).

    ``campaign_rules_text`` HANYA disuntik bila analyzer memang menerima parameter
    itu (atau ``**kwargs``). Lane AI membangun parameter ini paralel dengan lane
    bot, jadi bot tidak boleh crash hanya karena signature-nya belum ada:
    tanpa kwargs tersebut alur lama (campaign_active saja) tetap jalan.
    """
    kwargs: dict[str, Any] = {"campaign_active": bool(campaign_active)}
    if not campaign_rules_text:
        return kwargs
    accepts = False
    try:
        params = inspect.signature(analyzer.analyze_segments).parameters
        accepts = "campaign_rules_text" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - introspection gagal
        LOGGER.warning("introspeksi analyze_segments gagal: %s", exc)
    if accepts:
        kwargs["campaign_rules_text"] = campaign_rules_text
    else:
        LOGGER.warning(
            "AIAnalyzer.analyze_segments belum menerima campaign_rules_text — "
            "injeksi aturan S&K ke prompt dilewati (campaign_active tetap aktif)."
        )
    return kwargs


def run_analyze(
    source_path: Path,
    transcription,
    progress: ProgressState,
    campaign_active: bool = False,
    campaign_rules_text: str | None = None,
):
    """AI analysis — call in executor. Returns (moments, video_info).

    ``campaign_active``: True bila job ini akan terikat campaign nyata.
    AIAnalyzer memakainya untuk TIDAK menyuntik hashtag generik (#fyp dll) ke
    caption fallback yang bisa menabrak S&K campaign.

    ``campaign_rules_text``: ringkasan aturan S&K (dari cp.campaign_rules_text)
    yang diinjeksikan ke prompt agar AI bisa MENANDAI risiko (risk_flags).
    AI hanya menandai, tidak pernah memveto — keputusan tetap di manusia.
    """
    progress.set("analyze", "Menganalisis semua topik & momen menarik dengan AI...")
    video_info = probe_video(source_path)
    analyzer = AIAnalyzer(logger=LOGGER)
    video_lang = getattr(transcription, "language", None)
    moments = analyzer.analyze_segments(
        transcription.segments,
        video_info.duration,
        video_language=video_lang,
        **campaign_analyze_kwargs(analyzer, campaign_active, campaign_rules_text),
    )
    return moments, video_info


def run_youtube_analyze(
    transcript,
    progress: ProgressState,
    campaign_active: bool = False,
    campaign_rules_text: str | None = None,
):
    """AI analysis on a YouTube transcript — call in executor. Returns moments."""
    progress.set("analyze", "Menganalisis semua topik & momen menarik dengan AI...")
    analyzer = AIAnalyzer(logger=LOGGER)
    video_lang = getattr(transcript, "language", None)
    return analyzer.analyze_segments(
        transcript.segments,
        transcript.duration,
        video_language=video_lang,
        **campaign_analyze_kwargs(analyzer, campaign_active, campaign_rules_text),
    )


def refine_clip_boundaries_with_whisper(
    whisper_segs: list[Any],
    approx_start: float,
    approx_end: float,
    first_words: str = "",
    last_words: str = "",
    min_clip_duration: float | None = None,
) -> tuple[float, float]:
    """Refines clip start and end timestamps to exact word boundaries from Whisper.

    Eliminates chopped syllables at the beginning (e.g. trailing syllable of previous sentence)
    and prevents topic bleed at the end (e.g. first word of next question).
    """
    all_words: list[dict[str, Any]] = []
    for s in whisper_segs:
        words = getattr(s, "words", None)
        if isinstance(s, dict):
            words = s.get("words")
        if words:
            for w in words:
                w_start = float(w.get("start") if isinstance(w, dict) else getattr(w, "start", 0.0))
                w_end = float(w.get("end") if isinstance(w, dict) else getattr(w, "end", 0.0))
                w_word = str(w.get("word") if isinstance(w, dict) else getattr(w, "word", "")).strip()
                if w_end > w_start and w_word:
                    all_words.append({"word": w_word, "start": w_start, "end": w_end})

    if not all_words:
        return approx_start, approx_end

    best_start = approx_start
    first_target_tokens = [w.lower().strip(".,?!\"'“‘") for w in first_words.split()[:4] if w.strip(".,?!\"'“‘")]

    # 1. Match start word near approx_start (window: approx_start - 2.0s to approx_start + 4.0s)
    matched_start = None
    candidate_start_words = [
        w for w in all_words if (approx_start - 2.0) <= w["start"] <= (approx_start + 4.0)
    ]
    if first_target_tokens and candidate_start_words:
        for i in range(len(candidate_start_words)):
            cand_token = candidate_start_words[i]["word"].lower().strip(".,?!\"'“‘")
            if cand_token == first_target_tokens[0]:
                matched_start = candidate_start_words[i]["start"]
                break

    if matched_start is not None:
        # 0.08s lead-in for natural audio breath, avoiding clipping the first consonant
        best_start = max(0.0, matched_start - 0.08)
    else:
        # Fallback: first word starting at or after approx_start - 0.1s
        after_words = [w for w in all_words if w["start"] >= approx_start - 0.1]
        if after_words:
            best_start = max(0.0, after_words[0]["start"] - 0.08)

    # 2. Match end word near approx_end (window: approx_end - 4.0s to approx_end + 2.0s)
    best_end = approx_end
    last_target_tokens = [w.lower().strip(".,?!\"'“‘") for w in last_words.split()[-3:] if w.strip(".,?!\"'“‘")]

    matched_end = None
    candidate_end_words = [
        w for w in all_words if (approx_end - 4.0) <= w["end"] <= (approx_end + 2.0)
    ]
    if last_target_tokens and candidate_end_words:
        for i in range(len(candidate_end_words) - 1, -1, -1):
            cand_token = candidate_end_words[i]["word"].lower().strip(".,?!\"'“‘")
            if cand_token in last_target_tokens:
                matched_end = candidate_end_words[i]["end"]
                break

    if matched_end is not None:
        # 0.15s trailing room for natural vocal decay, but before next word
        best_end = matched_end + 0.15
    else:
        # Fallback: last word ending before approx_end + 0.2s
        before_words = [w for w in all_words if w["end"] <= approx_end + 0.2]
        if before_words:
            best_end = before_words[-1]["end"] + 0.15

    # Enforce minimum duration constraint: don't let Whisper refinement shrink clip below min_clip_duration
    if min_clip_duration is not None and min_clip_duration > 0:
        eff_min = float(min_clip_duration)
        if (best_end - best_start) < eff_min and (approx_end - approx_start) >= (eff_min - 2.0):
            extended_words = [w for w in all_words if (w["end"] + 0.15 - best_start) >= eff_min]
            if extended_words:
                best_end = extended_words[0]["end"] + 0.15
            elif (approx_end - best_start) >= eff_min:
                best_end = approx_end

    if best_end <= best_start + 2.0:
        return approx_start, approx_end

    return best_start, best_end


def run_youtube_render_single(
    url: str,
    transcript,
    moment: ClipMoment,
    idx: int,
    work_dir: Path,
    user_id: int,
    progress: ProgressState | None = None,
    user_template: dict[str, Any] | None = None,
    min_clip_duration: float | None = None,
) -> Path:
    """Download only the selected section and render 1 clip."""
    if progress:
        progress.set("download", "Mengunduh potongan video YouTube...")
    cookie_path = COOKIE_PATH if has_cookies() else None
    editor = VideoEditor(logger=LOGGER)

    # Snap moment to exact sentence boundaries before download
    from modules.ai_analyzer import snap_moment_to_transcript
    target_min = min_clip_duration if min_clip_duration is not None else (moment.end - moment.start)
    snapped_moment = snap_moment_to_transcript(moment, transcript.segments, min_clip_duration=target_min)

    seg_path = youtube_flow.download_youtube_segment(
        url,
        float(snapped_moment.start),
        float(snapped_moment.end),
        f"ytseg{idx}_{int(time.time())}",
        work_dir,
        user_id,
        cookie_path=cookie_path,
    )

    offset = float(snapped_moment.start) - youtube_flow.SECTION_MARGIN_S
    if offset < 0:
        offset = 0.0

    video_info = probe_video(seg_path)
    clip_end = float(snapped_moment.end) - offset
    clip_start = float(snapped_moment.start) - offset
    clip_end = min(clip_end, video_info.duration)
    eff_min_check = target_min if (target_min and target_min > config.MIN_CLIP_DURATION_S) else config.MIN_CLIP_DURATION_S
    if clip_end - clip_start < (eff_min_check - 2.0):
        seg_path.unlink(missing_ok=True)
        raise VideoEditError(f"Segmen {idx} terlalu pendek setelah di-clamp ({clip_end - clip_start:.1f}s)")

    rebased_segments = [
        type(seg)(seg.start - offset, seg.end - offset, seg.text)
        for seg in transcript.segments
        if seg.end > offset and seg.start < float(snapped_moment.end)
    ]
    rebased_segments = youtube_flow.sanitize_transcript_segments(rebased_segments)
    # Transcribe downloaded segment with Whisper for 100% verbatim Indonesian audio & timestamps
    clip_segments_to_use = rebased_segments
    try:
        if progress:
            progress.set("transcribe", "Mengekstrak audio & transkripsi kata demi kata...")
        audio_path = work_dir / f"{seg_path.stem}.wav"
        transcriber = WhisperTranscriber(logger=LOGGER)
        transcriber.extract_audio(seg_path, audio_path)
        target_lang = getattr(transcript, "language", None)
        whisper_segs = transcriber.transcribe_audio(audio_path, language=target_lang)
        if whisper_segs:
            LOGGER.info(
                "Whisper generated %d precise segments directly from audio for clip %s",
                len(whisper_segs),
                idx,
            )
            # Refine exact start & end boundaries using Whisper word timestamps
            refined_start, refined_end = refine_clip_boundaries_with_whisper(
                whisper_segs,
                clip_start,
                clip_end,
                snapped_moment.first_words,
                snapped_moment.last_words,
                min_clip_duration=target_min,
            )
            LOGGER.info(
                "Refined clip %s boundaries using Whisper: %.2fs-%.2fs -> %.2fs-%.2fs (duration: %.2fs)",
                idx, clip_start, clip_end, refined_start, refined_end, refined_end - refined_start,
            )
            clip_start = refined_start
            clip_end = refined_end
            clip_segments_to_use = whisper_segs
    except Exception as exc:
        LOGGER.warning(
            "Whisper transcription failed for clip %s: %s; falling back to YouTube captions",
            idx,
            exc,
        )

    rebased_moment = ClipMoment(
        start=clip_start,
        end=clip_end,
        hook=snapped_moment.hook,
        topic=snapped_moment.topic,
        caption=snapped_moment.caption,
        viral_score=getattr(snapped_moment, "viral_score", 85),
        alasan=getattr(snapped_moment, "alasan", ""),
        bgm_mood=getattr(snapped_moment, "bgm_mood", "upbeat"),
        rank=getattr(snapped_moment, "rank", idx),
        first_words=snapped_moment.first_words,
        last_words=snapped_moment.last_words,
        # Sinyal risiko AI ikut di-rebase: tanpa baris ini jejak risk_flags hilang
        # begitu klip dibingkai ulang, dan gerbang manusia jadi buta.
        risk_flags=list(getattr(snapped_moment, "risk_flags", None) or []),
        campaign_notes=list(getattr(snapped_moment, "campaign_notes", None) or []),
    )

    try:
        if progress:
            progress.set("render", "Merender video vertikal 9:16 & animasi subtitel...")
        output_path = editor.render_clip(
            seg_path,
            clip_segments_to_use,
            rebased_moment,
            idx,
            work_dir,
            video_info,
            user_template=user_template,
        )
    finally:
        seg_path.unlink(missing_ok=True)
        if "audio_path" in locals():
            audio_path.unlink(missing_ok=True)

    return output_path


def run_file_render_single(
    source_path: Path,
    transcription,
    moment: ClipMoment,
    idx: int,
    work_dir: Path,
    video_info,
    progress: ProgressState | None = None,
    user_template: dict[str, Any] | None = None,
) -> Path:
    """Render 1 clip from local video file."""
    if progress:
        progress.set("render", "Merender video vertikal 9:16 & animasi subtitel...")
    editor = VideoEditor(logger=LOGGER)
    segment_list = list(transcription.segments)
    return editor.render_clip(
        source_path,
        segment_list,
        moment,
        idx,
        work_dir,
        video_info,
        user_template=user_template,
    )


def format_seconds(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def escape_tg_md(text: str) -> str:
    """Escape Telegram Markdown v1 reserved characters."""
    if not text:
        return ""
    for char in ("_", "*", "`", "["):
        text = text.replace(char, f"\\{char}")
    return text


def build_clip_catalog_messages(
    moments: list[ClipMoment],
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> list[str]:
    """Format candidate clips into Telegram-safe chunks (max 3800 chars per message)."""
    header = (
        f"✨ *KATALOG REKOMENDASI KLIP VIRAL* ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Ditemukan {len(moments)} Pilihan Konteks Klip Menarik!*\n"
        f"_Diurutkan berdasarkan skor potensi viral tertinggi (AI Curated):_\n\n"
        f"Pilih klip mana yang ingin kamu jadikan video vertikal (9:16) lewat tombol di bawah.\n"
        f"💡 _Kustomisasi: Ketik `/edithook <id> <teks>` untuk ubah hook, atau `/style` untuk ubah desain & posisi hook!_\n\n"
    )

    # Badge kepatuhan campaign per klip (NO-OP bila tidak ada campaign / modul off).
    verdicts_by_clip: list[list[Any]] | None = None
    job = context.user_data.get("active_job") if context is not None else None
    if isinstance(job, dict) and campaign_job_active(job):
        profile = campaign_get_profile(str(job.get("campaign_id")))
        segments = get_job_segments(job) if profile else None
        if profile and segments:
            verdicts_by_clip = campaign_compute_verdicts(moments, segments, profile)
            if verdicts_by_clip is not None:
                job["campaign_verdicts"] = verdicts_by_clip
                stale = campaign_binding_note(job, profile)
                cname = escape_tg_md(str(profile.get("name", job.get("campaign_id"))))[:40]
                header += f"🛡 Campaign: {cname}{escape_tg_md(stale)}\n\n"

    # Catatan campaign non-konten (mis. syarat akun, link bio, dsb.)
    notes: list[str] = []
    if moments and getattr(moments[0], "campaign_notes", None):
        notes = list(moments[0].campaign_notes)
    elif isinstance(job, dict) and job.get("campaign_notes"):
        notes = list(job["campaign_notes"])
    if notes:
        header += "👤 *Catatan Syarat Campaign (Tanggung Jawab Akun):*\n"
        for n in notes[:6]:
            header += f"  • _{escape_tg_md(str(n))}_\n"
        header += "\n"

    messages: list[str] = []
    current = header
    for idx, m in enumerate(moments, start=1):
        campaign_line = ""
        if verdicts_by_clip and idx <= len(verdicts_by_clip):
            vs = verdicts_by_clip[idx - 1] or []
            lvl, worst = campaign_worst_verdict(vs)
            emoji = CAMPAIGN_LEVEL_EMOJI.get(lvl, "✅")
            if lvl == "pass":
                campaign_line = "✅ Lolos aturan campaign"
            else:
                reason = campaign_verdict_reason(worst)
                # Bedakan sinyal lunak dari AI dengan pelanggaran aturan eksplisit.
                who = "Sinyal AI: " if campaign_verdict_is_soft(worst) else ""
                campaign_line = (
                    f"{emoji} Campaign: {who}{campaign_short(reason) or 'melanggar aturan'}"
                )
        item = bot_ui.format_clip_card(idx, m, campaign_line=campaign_line)
        if len(current) + len(item) > 3800:
            messages.append(current)
            current = item
        else:
            current += item
    if current:
        messages.append(current)
    return messages


def build_clip_keyboard(total_clips: int, generated_indices: set[int]) -> InlineKeyboardMarkup:
    """Build grid of buttons for clip selection."""
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx in range(1, total_clips + 1):
        if idx in generated_indices:
            label = f"✅ Klip {idx} (Selesai)"
            cb_data = f"clip:info:{idx}"
        else:
            label = f"🎬 Generate Klip {idx}"
            cb_data = f"clip:gen:{idx}"
        row.append(InlineKeyboardButton(label, callback_data=cb_data))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    bottom_row = []
    unrendered_count = total_clips - len(generated_indices)
    if unrendered_count > 1:
        bottom_row.append(InlineKeyboardButton("✨ Generate Semua", callback_data="clip:all"))
    bottom_row.append(InlineKeyboardButton("🗑️ Selesai / Sesi Baru", callback_data="clip:done"))
    buttons.append(bottom_row)

    return InlineKeyboardMarkup(buttons)


def cleanup_active_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.user_data.pop("active_job", None)
    if job:
        work_dir = job.get("work_dir")
        if work_dir and isinstance(work_dir, Path) and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        source_path = job.get("source_path")
        if source_path and isinstance(source_path, Path) and source_path.exists():
            if "bot_" in source_path.name:
                source_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Campaign Compliance (langkah 1) — semua fungsi NO-OP saat CAMPAIGN_OK False
# atau campaign_id == "bebas".
# ---------------------------------------------------------------------------

CAMPAIGN_LEVEL_EMOJI: dict[str, str] = {
    "pass": "✅", "info": "ℹ️", "warn": "⚠️", "fail": "❌",
}


def campaign_enabled() -> bool:
    return bool(CAMPAIGN_OK)


def campaign_job_active(job: dict | None) -> bool:
    """True bila job terikat campaign nyata (bukan "bebas" / tanpa ikatan)."""
    if not campaign_enabled() or not job:
        return False
    cid = job.get("campaign_id")
    return bool(cid) and cid != CAMPAIGN_FREE_ID


def campaign_pending_bound(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Read-only: True bila `pending_campaign` menunjuk campaign NON-bebas atau aturan kustom."""
    if not campaign_enabled():
        return False
    try:
        if context.user_data.get("pending_campaign_rules"):
            return True
        job = context.user_data.get("active_job")
        if isinstance(job, dict) and job.get("campaign_rules_text"):
            return True
        pid = context.user_data.get("pending_campaign")
    except AttributeError:
        return False
    if not pid or str(pid) == CAMPAIGN_FREE_ID:
        return False
    return True


def campaign_pending_rules_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Teks aturan S&K / brief bebas untuk prompt AI — dibaca SEBELUM active_job dibuat."""
    if not campaign_enabled():
        return ""
    try:
        custom = context.user_data.get("pending_campaign_rules")
        if custom and str(custom).strip():
            return str(custom).strip()
        job = context.user_data.get("active_job")
        if isinstance(job, dict) and job.get("campaign_rules_text"):
            return str(job.get("campaign_rules_text")).strip()
        pid = context.user_data.get("pending_campaign")
    except AttributeError:
        return ""
    if not pid or str(pid) == CAMPAIGN_FREE_ID:
        return ""
    if str(pid) == "custom":
        return str(context.user_data.get("pending_campaign_rules") or "").strip()
    profile = campaign_get_profile(str(pid))
    if profile is None:
        return ""
    try:
        return str(cp.campaign_rules_text(profile) or "")
    except Exception as exc:
        LOGGER.warning("campaign_rules_text(%s) gagal: %s", pid, exc)
        return ""


def campaign_get_profile(campaign_id: str) -> dict[str, Any] | None:
    if not campaign_enabled() or not campaign_id:
        return None
    if campaign_id == "custom":
        return {
            "id": "custom",
            "name": "Aturan Kustom",
            "strictness": "standard",
            "profile_version": 1,
            "clip_rules": {},
            "post_rules": {},
        }
    try:
        return cp.load_profile(campaign_id)
    except Exception as exc:
        LOGGER.warning("load_profile(%s) gagal: %s", campaign_id, exc)
        return None


def campaign_profile_by_id(pid: str) -> dict[str, Any] | None:
    """Cari profil lewat list_profiles (hindari load id tak dikenal langsung)."""
    if not campaign_enabled():
        return None
    try:
        for meta in cp.list_profiles():
            if str(meta.get("id", "")) == pid:
                return campaign_get_profile(pid)
    except Exception as exc:
        LOGGER.warning("list_profiles gagal: %s", exc)
    return None


def campaign_list_profiles() -> list[dict[str, Any]]:
    if not campaign_enabled():
        return []
    try:
        return list(cp.list_profiles() or [])
    except Exception as exc:
        LOGGER.warning("list_profiles gagal: %s", exc)
        return []


def campaign_binding_note(job: dict, profile: dict[str, Any]) -> str:
    """True bila job menyimpan snapshot versi lama dibanding profil sekarang."""
    stored = job.get("campaign_version")
    current = profile.get("profile_version")
    if stored is None or current is None:
        return ""
    return "" if stored == current else " (profil lama — update tersedia)"


def campaign_short(msg: str, limit: int = 60) -> str:
    text = " ".join(str(msg or "").split())[:limit].strip()
    return escape_tg_md(text)


def campaign_profile_details_text(pid: str) -> str:
    profile = campaign_profile_by_id(pid)
    if profile is None:
        return f"Profil `{escape_tg_md(pid)}` tidak ditemukan."
    lines = [
        f"🎯 *Profil:* {escape_tg_md(str(profile.get('name', pid)))}",
        f"_id:_ `{pid}` • _strictness:_ `{profile.get('strictness', '?')}` • _versi:_ {profile.get('profile_version', '?')}",
    ]
    src_doc = str(profile.get("source_doc") or "").strip()
    if src_doc:
        lines.append(f"_sumber:_ {escape_tg_md(src_doc[:80])}")

    clip_rules = profile.get("clip_rules") or {}
    dur = clip_rules.get("duration_s")
    if isinstance(dur, (list, tuple)) and len(dur) == 2:
        lines.append(f"⏱ Durasi klip: {int(dur[0])}-{int(dur[1])} detik")
    elif isinstance(dur, dict):
        lines.append(f"⏱ Durasi klip: {dur.get('min', '?')}-{dur.get('max', '?')} detik")
    banned_exact = clip_rules.get("banned_exact") or []
    if banned_exact:
        shown = ", ".join(str(x) for x in list(banned_exact)[:8])
        lines.append(f"🚫 Kata terlarang: {escape_tg_md(shown[:120])}")
    topics = clip_rules.get("banned_topics") or []
    for t in topics[:8]:
        if isinstance(t, dict):
            lines.append(f"🚫 Topik: {escape_tg_md(str(t.get('topic', t)))}")

    post_rules = profile.get("post_rules") or {}
    req = post_rules.get("caption_required") or {}
    all_of = req.get("all_of") or []
    any_of = req.get("any_of") or []
    if all_of:
        lines.append(f"📝 Caption wajib semua: {escape_tg_md(', '.join(str(x) for x in list(all_of)[:8])[:120])}")
    if any_of:
        lines.append(f"📝 Caption minimal satu: {escape_tg_md(', '.join(str(x) for x in list(any_of)[:8])[:120])}")
    platforms = post_rules.get("platforms")
    if platforms:
        lines.append(f"📱 Platform: {escape_tg_md(', '.join(str(p) for p in list(platforms))[:80])}")

    note_line = campaign_curation_note_text(profile)
    if note_line:
        lines.append(note_line)

    account = profile.get("account_items") or []
    if account:
        lines.append("👤 *Masih di tanganmu:*")
        for item in list(account)[:8]:
            lines.append(f"  • {campaign_item_text(item)}")
    ooc = profile.get("out_of_control") or []
    if ooc:
        lines.append("🌀 *Di luar kendali bot:*")
        for item in list(ooc)[:6]:
            lines.append(f"  • {campaign_item_text(item)}")

    try:
        issues = cp.validate_profile(profile) or []
    except Exception as exc:
        LOGGER.warning("validate_profile(%s) gagal: %s", pid, exc)
        issues = []
    if issues:
        lines.append("⚠️ *Masalah konfigurasi profil:*")
        for it in list(issues)[:8]:
            lines.append(f"  • {escape_tg_md(str(it))}")
    else:
        lines.append("✅ Profil valid.")
    return "\n".join(lines)


def campaign_item_text(item: Any) -> str:
    if isinstance(item, dict):
        label = item.get("text") or item.get("item") or item.get("label") or item.get("name") or "-"
        status = item.get("status") or item.get("state")
        if status:
            return f"{escape_tg_md(str(label))} — `{status}`"
        return escape_tg_md(str(label))
    return escape_tg_md(str(item))


def campaign_intro_text(profile: dict[str, Any]) -> str:
    try:
        lines = [str(x) for x in (cp.campaign_intro_lines(profile) or [])]
    except Exception as exc:
        LOGGER.warning("campaign_intro_lines gagal: %s", exc)
        return ""
    return "\n".join(lines).strip()


def campaign_curation_note_text(profile: dict[str, Any]) -> str:
    """Arahan kurasi dari profil (string) — pengingat, bukan aturan keras.

    Kanonik kontrak langkah-2: ``clip_rules.curation_note`` (lihat
    ``campaign_policy.validate_profile`` + seed graeme). Bentuk top-level ikut
    dibaca untuk toleransi profil lawas/manual, tapi tidak pernah menimpa yang
    ada di dalam ``clip_rules``.
    """
    rules = profile.get("clip_rules")
    if not isinstance(rules, dict):
        rules = {}
    note = rules.get("curation_note")
    if note is None:
        note = profile.get("curation_note")
    if isinstance(note, (list, tuple)):
        note = " ".join(str(x).strip() for x in note if str(x).strip())
    note = str(note or "").strip()
    if not note:
        return ""
    return f"🧭 *Arahan kurasi:* {campaign_short(note, 300)}"


def campaign_reminders_text(profile: dict[str, Any]) -> str:
    """curation_note + account_items + out_of_control sebagai pengingat sekali."""
    parts: list[str] = []
    note = campaign_curation_note_text(profile)
    if note:
        parts.append(note)
    account = profile.get("account_items") or []
    if account:
        parts.append("👤 *Yang masih jadi tanggung jawabmu:*")
        parts.extend(f"  • {campaign_item_text(i)}" for i in list(account)[:8])
    ooc = profile.get("out_of_control") or []
    if ooc:
        parts.append("🌀 *Di luar kendali bot:*")
        parts.extend(f"  • {campaign_item_text(i)}" for i in list(ooc)[:6])
    return "\n".join(parts)


def build_campaign_menu_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("✍️ Ketik Aturan Campaign (Bebas)", callback_data="camp:set:custom")],
    ]
    for meta in campaign_list_profiles():
        pid = re.sub(r"[^A-Za-z0-9_\-.]", "", str(meta.get("id", "")))[:40]
        if not pid or pid == CAMPAIGN_FREE_ID:
            continue
        name = str(meta.get("name") or pid)[:22]
        strict = str(meta.get("strictness") or "")[:10]
        rows.append([
            InlineKeyboardButton(f"🎯 {name} ({strict})", callback_data=f"camp:set:{pid}"),
            InlineKeyboardButton("ℹ️", callback_data=f"camp:det:{pid}"),
        ])
    rows.append([
        InlineKeyboardButton("🆓 Bebas (tanpa aturan)", callback_data="camp:set:bebas"),
        InlineKeyboardButton("📋 Tampilkan aturan aktif", callback_data="camp:show"),
    ])
    rows.append([InlineKeyboardButton("📥 Impor S&K (tempel teks)", callback_data="camp:import")])
    return InlineKeyboardMarkup(rows)


def build_campaign_gate_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("✍️ Ketik Aturan Campaign (Bebas)", callback_data="camp:job:custom")],
    ]
    for meta in campaign_list_profiles():
        pid = re.sub(r"[^A-Za-z0-9_\-.]", "", str(meta.get("id", "")))[:40]
        if not pid or pid == CAMPAIGN_FREE_ID:
            continue
        name = str(meta.get("name") or pid)[:26]
        rows.append([InlineKeyboardButton(f"🎯 {name}", callback_data=f"camp:job:{pid}")])
    rows.append([InlineKeyboardButton("🆓 Bebas (tanpa aturan)", callback_data="camp:job:bebas")])
    return InlineKeyboardMarkup(rows)


def consume_campaign_binding(context: ContextTypes.DEFAULT_TYPE) -> tuple[str | None, Any]:
    """Panggil HANYA saat active_job baru dibuat: konsumsi pending_campaign."""
    pid = context.user_data.pop("pending_campaign", None)
    if not pid or not campaign_enabled():
        return (None, None)
    if str(pid) == "custom":
        return ("custom", 1)
    profile = campaign_get_profile(str(pid))
    return (str(pid), profile.get("profile_version") if profile else None)


def campaign_overrides_tail_text(cid: str, n: int = 3) -> str:
    """Baris ringkas override terakhir dari audit trail campaign (bisa kosong).

    Bentuk baris dari ``cp.read_overrides_tail`` tidak dijamin; kunci waktu dan
    kode verdict dibaca dari beberapa nama kandidat lalu jatuh ke render dict
    ringkas, jadi perubahan skema audit di lane lain tidak memecah UI.
    """
    if not campaign_enabled() or not cid or cid == CAMPAIGN_FREE_ID:
        return ""
    try:
        rows = list(cp.read_overrides_tail(cid, n=max(n, 1) + 2) or [])
    except Exception as exc:
        LOGGER.warning("read_overrides_tail(%s) gagal: %s", cid, exc)
        return ""
    rows = [r for r in rows if isinstance(r, dict)][-n:]
    if not rows:
        return ""
    rows.reverse()  # terbaru dulu
    lines = [f"🧾 *{len(rows)} override terakhir* (jejak keputusanmu):"]
    for r in rows:
        when = ""
        for key in ("ts", "time", "created_at", "at", "timestamp", "iso"):
            val = r.get(key)
            if val:
                when = str(val)[:19]
                break
        idx_raw = r.get("clip_index", r.get("clip", r.get("idx", "?")))
        try:
            idx = str(int(idx_raw))
        except (TypeError, ValueError):
            idx = re.sub(r"[^A-Za-z0-9_.\-]", "", str(idx_raw))[:12] or "?"
        codes = r.get("verdict_codes") or r.get("codes") or r.get("gates") or []
        if isinstance(codes, str):
            codes = [codes]
        code_txt = ", ".join(campaign_short(str(c), 40) for c in list(codes)[:4])
        note = str(r.get("note") or "").strip()
        parts = [f"klip `{idx}`"]
        if code_txt:
            parts.append(code_txt)
        if note:
            parts.append(f"_{campaign_short(note, 40)}_")
        if when:
            parts.append(campaign_short(when, 24))
        lines.append("  • " + " • ".join(parts))
    return "\n".join(lines)


async def campaign_show_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    parts: list[str] = []
    pending = context.user_data.get("pending_campaign")
    job = context.user_data.get("active_job") or {}
    cid = job.get("campaign_id")

    if pending:
        if str(pending) == CAMPAIGN_FREE_ID:
            parts.append("⏭ Video berikutnya: *Bebas* (tanpa aturan).")
        elif str(pending) == "custom":
            rules_snippet = campaign_short(str(context.user_data.get("pending_campaign_rules") or ""), 120)
            parts.append(f"⏭ Video berikutnya terikat: *Aturan Kustom*\n📝 _{rules_snippet}_\n(kirim /campaign lagi untuk ganti).")
        else:
            prof = campaign_get_profile(str(pending))
            nm = escape_tg_md(str((prof or {}).get("name", pending))) if prof else escape_tg_md(str(pending))
            parts.append(f"⏭ Video berikutnya terikat: *{nm}* (pesan /campaign lagi untuk ganti).")
    elif not campaign_enabled():
        parts.append("Modul campaign tidak tersedia — fitur OFF.")
    else:
        parts.append("⏭ Belum ada campaign diantrekan — kamu akan ditanya saat kirim video baru.")

    if campaign_job_active(job):
        if str(cid) == "custom":
            parts.append("📌 Job aktif: *Aturan Kustom (Bebas)*")
            c_rules = job.get("campaign_rules_text") or ""
            if c_rules:
                parts.append(f"📝 *Aturan:* _{campaign_short(str(c_rules), 140)}_")
            notes = job.get("campaign_notes") or []
            if notes:
                parts.append("👤 *Catatan Syarat Akun / Luar Kendali:*")
                for n in list(notes)[:6]:
                    parts.append(f"  • {escape_tg_md(str(n))}")
        else:
            prof = campaign_get_profile(str(cid))
            if prof:
                label = escape_tg_md(str(prof.get("name", cid)))
                parts.append(f"📌 Job aktif: *{label}*{campaign_binding_note(job, prof)}")
                moments = job.get("moments") or []
                if moments:
                    verdicts = ensure_campaign_verdicts(job)
                    if verdicts:
                        flat = [v for clip_vs in verdicts for v in (clip_vs or [])]
                        agg = "?"
                        try:
                            agg = str(cp.aggregate_level(flat))
                        except Exception:
                            pass
                        fails = [v for v in flat if getattr(v, "level", "") == "fail"]
                        warns = [v for v in flat if getattr(v, "level", "") == "warn"]
                        soft = [
                            v for v in flat
                            if campaign_verdict_is_soft(v) and str(getattr(v, "level", "")) in ("warn", "info")
                        ]
                        parts.append(
                            f"📊 Verdict agregat: {CAMPAIGN_LEVEL_EMOJI.get(agg, '•')} `{agg}` "
                            f"(klip gagal: {len(fails)} • peringatan: {len(warns)})"
                        )
                        if soft:
                            parts.append(
                                f"🤖 Sinyal AI / gerbang manusia yang perlu kamu putuskan: {len(soft)}"
                            )
                acked = [k for k in campaign_ack_set(job) if isinstance(k, str)]
                if acked:
                    parts.append(f"✅ Sudah kamu akui: {len(acked)} gerbang campaign.")
                # Pengingat sekali: arahan kurasi + tanggung jawab manusia.
                rem = campaign_reminders_text(prof)
                if rem:
                    parts.append(rem)
            tail = campaign_overrides_tail_text(str(cid))
            if tail:
                parts.append(tail)
    elif isinstance(job, dict) and job.get("awaiting_campaign"):
        parts.append("⏳ Menunggu pilihan campaign untuk video yang baru dikirim.")
    elif cid:
        parts.append("📌 Job aktif: Bebas (tanpa aturan campaign).")

    text = "\n\n".join(parts)
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as exc:
        LOGGER.debug("campaign status Markdown gagal (%s); kirim teks polos", exc)
        await context.bot.send_message(chat_id=chat_id, text=re.sub(r"[*_`]", "", text))


async def campaign_select_and_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int, pid: str) -> None:
    context.user_data["pending_campaign"] = pid
    if pid == CAMPAIGN_FREE_ID:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🆓 Campaign: *Bebas (tanpa aturan).*\n\n"
                "Akan dipakai untuk video BERIKUTNYA — setelah itu bot tidak menanyakan lagi.\n"
                "Kirim /campaign untuk memasang aturan.",
            ),
            parse_mode="Markdown",
        )
        return
    profile = campaign_profile_by_id(pid)
    if profile is None:
        await context.bot.send_message(chat_id=chat_id, text=f"Profil `{pid}` tidak ditemukan.")
        return
    name = escape_tg_md(str(profile.get("name", pid)))
    lines = [
        f"🎯 Campaign *{name}* akan dipakai untuk video BERIKUTNYA.",
        "_Aturan lama mati untuk video baru: setelah video ini, bot akan menanyakan campaign lagi._",
        "",
    ]
    intro = campaign_intro_text(profile)
    if intro:
        lines.append(escape_tg_md(intro))
        lines.append("")
    rem = campaign_reminders_text(profile)
    if rem:
        lines.append(rem)
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


async def campaign_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, payload: Any) -> bool:
    """Gerbang awal pekerjaan baru (setelah klaim lock).

    True = TUNDA: keyboard pilihan campaign terkirim, konteks disimpan di
    user_data["active_job"]["awaiting_campaign"]; alur lanjut via camp:job callback.
    """
    if not campaign_enabled():
        return False
    if context.user_data.get("pending_campaign"):
        return False  # akan di-bind saat active_job dibuat
    chat_id = update.effective_chat.id
    awaiting = {
        "kind": kind,
        "payload": payload,
        "chat_id": chat_id,
        "user_id": update.effective_user.id,
    }
    job = context.user_data.get("active_job")
    if isinstance(job, dict) and "awaiting_campaign" in job:
        # konteks baru menggantikan konteks yang menunggu (keyboard lama tetap valid)
        job["awaiting_campaign"] = awaiting
        return True
    context.user_data["active_job"] = {"awaiting_campaign": awaiting}
    try:
        await update.effective_message.reply_text(
            "🎯 *Pilih atau ketik aturan campaign untuk video ini:*\n\n"
            "• Tekan tombol *✍️ Ketik Aturan Campaign* (atau langsung balas/ketik teks aturanmu di chat ini).\n"
            "• Tanpa aturan? Pilih *🆓 Bebas*.\n\n"
            "💡 _AI OriontClipper akan menalar apa yang boleh & dilarang dalam video. Syarat non-video (seperti minimal follower akun) akan dicatat sebagai pengingat._",
            parse_mode="Markdown",
            reply_markup=build_campaign_gate_keyboard(),
        )
    except Exception as exc:
        LOGGER.warning("keyboard campaign gagal dikirim: %s", exc)
        context.user_data.pop("active_job", None)
        return False
    return True


async def resume_job_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, awaiting: dict) -> None:
    """Lanjut alur normal setelah user memilih campaign untuk job menunggu."""
    chat_id = int(awaiting.get("chat_id") or update.effective_chat.id)
    kind = str(awaiting.get("kind") or "")
    payload = awaiting.get("payload")

    sticker_msg = await bot_ui.safe_send_animated_sticker(context.bot, chat_id, "robot")
    status_msg = await context.bot.send_message(
        chat_id=chat_id, text="🚀 *Mempersiapkan proses...*", parse_mode="Markdown",
    )
    if kind == "youtube":
        if not has_cookies():
            await status_msg.edit_text(
                "⚠️ Cookies YouTube belum terpasang. Kirim file cookies.txt lalu kirim ulang link-nya!",
                reply_markup=YOUTUBE_HELP_MENU,
            )
            return
        await status_msg.edit_text("🚀 *Mempersiapkan analisis YouTube...*", parse_mode="Markdown")
        await run_youtube_flow(update, context, str(payload), status_msg, sticker_msg=sticker_msg)
    elif kind == "gdrive":
        await status_msg.edit_text("🚀 *Mempersiapkan pengunduhan Google Drive...*", parse_mode="Markdown")
        await run_gdrive_flow(update, context, str(payload), status_msg, sticker_msg=sticker_msg)
    elif kind == "video":
        ts = int(time.time())
        token = str(hash(str(payload)))[:8].lstrip("-") or "0"
        file_path = config.INPUT_DIR / f"bot_{ts}_{token}.mp4"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tg_file = await context.bot.get_file(str(payload))
            await tg_file.download_to_drive(custom_path=str(file_path))
        except Exception as exc:
            LOGGER.error("Resume unduh video gagal: %s", exc)
            await status_msg.edit_text(f"❌ Gagal download video: {exc}")
            if sticker_msg:
                await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
            return
        await run_processing_flow(update, context, file_path, status_msg, sticker_msg=sticker_msg)
    else:
        await status_msg.edit_text("❌ Konteks pekerjaan tersimpan tidak dikenali. Kirim ulang video/link ya!")


# --- agregasi verdict: aturan profil + sinyal risiko dari AI -----------------
#
# Kode verdict "lunak" adalah sinyal yang TIDAK boleh memveto: AI hanya menandai
# (llm_flag) dan gerbang manusia hanya meminta konfirmasi (human_gate).
CAMPAIGN_SOFT_CODE_PREFIXES = ("llm_flag:", "human_gate:")


def campaign_verdict_code(verdict: Any) -> str:
    return str(getattr(verdict, "code", "") or "")


def campaign_verdict_reason(verdict: Any) -> str:
    return str(getattr(verdict, "message", "") or getattr(verdict, "code", "") or "")


def campaign_verdict_is_soft(verdict: Any) -> bool:
    """True untuk verdict berkode llm_flag:*/human_gate:* (bukan regex/daftar profil)."""
    return campaign_verdict_code(verdict).startswith(CAMPAIGN_SOFT_CODE_PREFIXES)


def campaign_moment_flags(moment: Any) -> list[str]:
    """``ClipMoment.risk_flags`` (field baru lane AI) sebagai list[str] bersih.

    Field belum tentu ada (analisis lama / cache sebelum fitur), jadi selalu
    dibaca lewat getattr + filter; tidak pernah raise.
    """
    raw = getattr(moment, "risk_flags", None)
    if raw is None and isinstance(moment, dict):
        raw = moment.get("risk_flags")
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    try:
        return [str(x).strip() for x in raw if str(x).strip()]
    except TypeError:
        return []


def campaign_demote_soft_fails(verdicts: list[Any]) -> list[Any]:
    """AI tidak punya hak veto: verdict lunak berlevel ``fail`` diturunkan ke warn.

    Lapisan pengaman di sisi bot saja — kebijakan level tetap milik
    campaign_policy. Tidak pernah raise; kalau ``replace`` gagal, verdict
    dikembalikan apa adanya.
    """
    out: list[Any] = []
    for v in verdicts or []:
        code = campaign_verdict_code(v)
        if code.startswith(CAMPAIGN_SOFT_CODE_PREFIXES) and str(getattr(v, "level", "")) == "fail":
            LOGGER.warning("verdict lunak %s level fail — diturunkan ke warn oleh bot", code)
            try:
                v = dataclasses.replace(v, level="warn")
            except Exception as exc:  # pragma: no cover - dataclass tak terduga
                LOGGER.warning("demote verdict %s gagal: %s", code, exc)
        out.append(v)
    return out


def campaign_merge_llm_flags(
    verdicts_by_clip: list[list[Any]], moments: list[Any], profile: dict[str, Any]
) -> list[list[Any]]:
    """Gabungkan ``cp.llm_flag_verdicts(risk_flags, profile)`` ke verdict per klip.

    Peringatan lunak: tidak pernah mengubah hasil evaluate_catalog, hanya
    menambah baris verdict sehingga badge katalog ikut naik ke ⚠️/ℹ️.
    """
    if not campaign_enabled() or profile is None or not verdicts_by_clip:
        return verdicts_by_clip or []
    for i, moment in enumerate(moments or []):
        if i >= len(verdicts_by_clip):
            break
        flags = campaign_moment_flags(moment)
        if not flags:
            continue
        try:
            extra = list(cp.llm_flag_verdicts(flags, profile) or [])
        except Exception as exc:
            LOGGER.warning("llm_flag_verdicts gagal (klip %s): %s", i + 1, exc)
            extra = []
        if extra:
            verdicts_by_clip[i] = list(verdicts_by_clip[i] or []) + extra
    return [campaign_demote_soft_fails(vs) for vs in verdicts_by_clip]


def campaign_compute_verdicts(
    moments: list[Any], segments: list[Any], profile: dict[str, Any]
) -> list[list[Any]] | None:
    """Satu-satunya jalur perhitungan verdict katalog (profil + sinyal AI)."""
    if not campaign_enabled() or profile is None:
        return None
    try:
        verdicts = cp.evaluate_catalog(moments, segments, profile)
        verdicts = [list(v or []) for v in (verdicts or [])]
    except Exception as exc:
        LOGGER.warning("evaluate_catalog gagal: %s", exc)
        return None
    return campaign_merge_llm_flags(verdicts, moments, profile)


def campaign_worst_verdict(verdicts: list[Any]) -> tuple[str, Any]:
    """(level terburuk, verdict penyebab) — ('pass', None) bila kosong/off."""
    lvl = "pass"
    if campaign_enabled():
        try:
            lvl = str(cp.aggregate_level(verdicts or []))
        except Exception as exc:
            LOGGER.warning("aggregate_level gagal: %s", exc)
            lvl = "pass"
    worst: Any = None
    for v in verdicts or []:
        if str(getattr(v, "level", "pass") or "pass") == lvl:
            worst = v
            break
    return lvl, worst


def get_job_segments(job: dict) -> list[Any] | None:
    """Ambil segments transkrip dari memori job; fallback ke cache .segments.json."""
    tr = job.get("transcription")
    segs = getattr(tr, "segments", None) if tr is not None else None
    if segs:
        return list(segs)
    tc = job.get("transcript")
    segs = getattr(tc, "segments", None) if tc is not None else None
    if segs:
        return list(segs)
    src = job.get("source_path")
    if isinstance(src, Path) and src.exists():
        try:
            from modules import transcriber as _tr
            cache = config.TRANSCRIPT_CACHE_DIR / f"{_tr.safe_cache_stem(src)}.segments.json"
            if cache.exists():
                import json as _json
                return _tr.segments_from_json(_json.loads(cache.read_text(encoding="utf-8")))
        except Exception as exc:
            LOGGER.warning("cache segments untuk campaign tidak terbaca: %s", exc)
    return None


def ensure_campaign_verdicts(job: dict) -> list[list[Any]] | None:
    """Verdict per klip katalog; hitung & cache di job bila belum ada."""
    if not campaign_job_active(job):
        return None
    cached = job.get("campaign_verdicts")
    if cached:
        return cached
    moments = job.get("moments") or []
    segments = get_job_segments(job)
    profile = campaign_get_profile(str(job.get("campaign_id")))
    if not moments or segments is None or profile is None:
        return None
    verdicts = campaign_compute_verdicts(moments, segments, profile)
    if verdicts is None:
        return None
    job["campaign_verdicts"] = verdicts
    return verdicts


def campaign_caption_note(job: dict, moment: Any) -> str:
    """Nasehat kelas post (check_caption) — TIDAK memblokir, tanpa auto-fix.

    Daftar usulan tag diambil dari ``Verdict.evidence["missing"]`` (satu-satunya
    sumber kebenaran = campaign_policy) — bot hanya merender. Semua teks profil
    di-escape Markdown + dipotong, karena hasil ini dikirim dengan
    parse_mode="Markdown" (baik saat ditempel ke caption video maupun berdiri
    sendiri).
    """
    if not campaign_job_active(job):
        return ""
    profile = campaign_get_profile(str(job.get("campaign_id")))
    if profile is None:
        return ""
    try:
        verdicts = cp.check_caption(str(getattr(moment, "caption", "") or ""), profile) or []
    except Exception as exc:
        LOGGER.warning("check_caption gagal: %s", exc)
        return ""
    notes = [v for v in verdicts if getattr(v, "level", "") in ("warn", "info")]
    if not notes:
        return ""
    lines = ["⚠️ Catatan kepatuhan caption (campaign):"]
    for v in notes:
        lines.append(f"• {campaign_short(str(getattr(v, 'message', '') or getattr(v, 'code', '') or ''), 140)}")
        ev = getattr(v, "evidence", None)
        if not isinstance(ev, dict):
            continue
        # Prioritas: bukti terstruktur dari campaign_policy (all_of / any_of)
        # supaya tidak sekadar mengulang pesan. Fallback ke flat "missing".
        all_of = [str(x) for x in (ev.get("all_of") or [])]
        any_of = [str(x) for x in (ev.get("any_of") or [])]
        if all_of:
            lines.append("   Usulan: " + ", ".join(campaign_short(x, 60) for x in all_of[:8]))
        if any_of:
            lines.append("   Atau salah satu: " + ", ".join(campaign_short(x, 60) for x in any_of[:8]))
        if not all_of and not any_of:
            missing = ev.get("missing") or ev.get("missing_hashtags") or ev.get("suggested_hashtags")
            if missing:
                try:
                    items = ", ".join(campaign_short(str(x), 60) for x in list(missing)[:8])
                except Exception:
                    items = ""
                if items:
                    lines.append(f"   Usulan: {items}")
    return "\n".join(lines)


def campaign_fail_warning_text(job: dict, idx: int) -> str:
    if not campaign_job_active(job):
        return ""
    verdicts = ensure_campaign_verdicts(job)
    if not verdicts or idx < 1 or idx > len(verdicts):
        return ""
    fails = [v for v in (verdicts[idx - 1] or []) if getattr(v, "level", "") == "fail"]
    if not fails:
        return ""
    lines = []
    for v in fails[:4]:
        lines.append(f"• {campaign_short(str(getattr(v, 'message', '') or getattr(v, 'code', '') or ''), 120)}")
        # Bukti kutipan transkrip (str) untuk fail keras: helps the human decide.
        ev = getattr(v, "evidence", None)
        if isinstance(ev, str) and ev.strip():
            lines.append(f"   _{campaign_short(ev, 90)}_")
    return "\n".join(lines)


# --- Gerbang manusia (langkah 2) — konfirmasi sebelum "Generate Semua" --------
#
# AI hanya MENANDAI risiko (risk_flags); yang memutuskan adalah manusia.
# Gate level ``warn`` dari profil (strict_mode ack_always) harus diakui
# satu-per-satu / sekaligus sebelum bot mau merender seluruh klip.

CAMPAIGN_GATE_ITEMS_KEY = "camp_gate_pending"  # daftar gate menunggu ack (di job)


def campaign_ack_set(job: dict) -> set:
    """Kumpulan ack: index klip ``int`` (jalur konfirmasi ❌) DAN code gate
    ``str`` (jalur generate-semua). Satu set dipakai bersama karena tipenya
    tidak mungkin bertabrakan.
    """
    acks = job.get("camp_ack")
    if not isinstance(acks, set):
        acks = set(acks) if isinstance(acks, (list, tuple, set)) else set()
        job["camp_ack"] = acks
    return acks


def campaign_is_acked(job: dict, key: Any) -> bool:
    try:
        return key in campaign_ack_set(job)
    except TypeError:
        return False


def campaign_mark_ack(job: dict, key: Any) -> None:
    campaign_ack_set(job).add(key)


def campaign_gates_for_flags(profile: dict[str, Any] | None, flags: list[str]) -> list[Any]:
    """Gerbang manusia untuk SATU klip (satu daftar flag).

    Bentuk argumen kontrak lane A sudah final: ``human_gates_for(profile,
    moments_flags)`` dengan ``moments_flags`` = daftar flag PER MOMEN (list of
    lists). Untuk satu klip cukup ``[clean]`` — tidak perlu introspeksi signature
    atau percobaan ganda.
    """
    if not campaign_enabled() or profile is None:
        return []
    clean = [str(f).strip() for f in (flags or []) if str(f).strip()]
    try:
        return list(cp.human_gates_for(profile, [clean]) or [])
    except Exception as exc:
        LOGGER.warning("human_gates_for gagal: %s", exc)
        return []


def campaign_gate_map(job: dict, indices: list[int], profile: dict[str, Any] | None = None) -> list[dict]:
    """Gate berlevel ``warn`` yang terpakai di klip-klip ``indices``.

    Hasil digabung per code: ``{"code", "message", "clips": [idx...]}`` urut
    kemunculan. NO-OP untuk campaign bebas / modul off / job tanpa moments.
    """
    if not campaign_job_active(job):
        return []
    if profile is None:
        profile = campaign_get_profile(str(job.get("campaign_id")))
    if profile is None:
        return []
    moments = job.get("moments") or []
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for idx in indices:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            continue
        if i < 1 or i > len(moments):
            continue
        for gate in campaign_gates_for_flags(profile, campaign_moment_flags(moments[i - 1])):
            code = campaign_verdict_code(gate)
            if not code or str(getattr(gate, "level", "") or "") != "warn":
                continue  # 'info' = cukup catatan, tidak meminta konfirmasi
            entry = merged.get(code)
            if entry is None:
                entry = {
                    "code": code,
                    "message": campaign_verdict_reason(gate) or "perlu review manusia",
                    "clips": [],
                }
                merged[code] = entry
                order.append(code)
            if i not in entry["clips"]:
                entry["clips"].append(i)
    return [merged[c] for c in order]


def campaign_pending_gates(
    job: dict, indices: list[int] | None = None, profile: dict[str, Any] | None = None
) -> list[dict]:
    """Gate 'warn' yang BELUM diakui untuk klip yang belum digenerate."""
    if not campaign_job_active(job):
        return []
    moments = job.get("moments") or []
    if indices is None:
        indices = [
            i for i in range(1, len(moments) + 1) if i not in job.get("generated", set())
        ]
    if not indices:
        return []
    if profile is None:
        profile = campaign_get_profile(str(job.get("campaign_id")))
        if profile is None:
            return []
    acked = campaign_ack_set(job)
    return [g for g in campaign_gate_map(job, indices, profile) if g["code"] not in acked]


def campaign_clip_fail_codes(job: dict, idx: int) -> list[str]:
    """Code verdict ``fail`` satu klip (dipakai jejak override konfirmasi ❌)."""
    verdicts = ensure_campaign_verdicts(job)
    if not verdicts or idx < 1 or idx > len(verdicts):
        return []
    return [
        campaign_verdict_code(v)
        for v in (verdicts[idx - 1] or [])
        if str(getattr(v, "level", "") or "") == "fail"
    ]


def campaign_record_override(job: dict, chat_id: Any, idx: int, codes: list[str], note: str) -> bool:
    """Tulis jejak keputusan manusia ke audit campaign — TIDAK PERNAH raise."""
    if not campaign_job_active(job) or not codes:
        return False
    moments = job.get("moments") or []
    if idx < 1 or idx > len(moments):
        return False
    moment = moments[idx - 1]
    profile = campaign_get_profile(str(job.get("campaign_id")))
    version = job.get("campaign_version")
    if version is None and profile is not None:
        version = profile.get("profile_version")
    try:
        cp.record_override(
            int(chat_id),
            int(job.get("user_id") or 0),
            str(job.get("campaign_id")),
            version,
            int(idx),
            float(getattr(moment, "start", 0.0) or 0.0),
            float(getattr(moment, "end", 0.0) or 0.0),
            [str(c) for c in codes],
            note=str(note or ""),
        )
        return True
    except Exception as exc:  # pragma: no cover - audit tidak boleh menghentikan klip
        LOGGER.warning("record_override klip %s gagal: %s", idx, exc)
        return False


def build_campaign_gate_ack_keyboard(gates: list[dict], per_item: bool) -> InlineKeyboardMarkup:
    """Tombol ack: per pokok untuk strictness strict, satu tombol untuk lainnya."""
    rows: list[list[InlineKeyboardButton]] = []
    if per_item:
        for i, g in enumerate(gates, start=1):
            label = f"✅ Aku mengerti ({i})"
            if len(gates) == 1:
                label = "✅ Aku mengerti"
            rows.append([InlineKeyboardButton(label, callback_data=f"camp:ack:{i - 1}")])
    rows.append([
        InlineKeyboardButton("✅ Aku mengerti semuanya", callback_data="camp:ackall"),
    ])
    rows.append([
        InlineKeyboardButton("❌ Batal, pilih klip sendiri", callback_data="camp:gatecancel"),
    ])
    return InlineKeyboardMarkup(rows)


def campaign_gate_confirm_text(job: dict, gates: list[dict], per_item: bool) -> str:
    profile = campaign_get_profile(str(job.get("campaign_id"))) or {}
    cname = escape_tg_md(str(profile.get("name", job.get("campaign_id") or "campaign")))[:40]
    clips = sorted({i for g in gates for i in (g.get("clips") or [])})
    clip_txt = ", ".join(str(i) for i in clips[:20]) or "-"
    lines = [
        "⚠️ *PERLU KAMU AKUI SEBELUM GENERATE SEMUA*",
        "",
        f"🛡 Campaign: *{cname}*",
        f"📎 Klip terdampak: `{escape_tg_md(clip_txt)}`",
        "",
    ]
    for i, g in enumerate(gates, start=1):
        where = ", ".join(str(x) for x in (g.get("clips") or [])[:12])
        lines.append(f"{i}. {campaign_short(str(g.get('message', '')), 150)}")
        if where:
            lines.append(f"   _klip:_ {escape_tg_md(where)}")
    lines.append("")
    lines.append(
        "Bot tidak menilai ini — aturan campaign minta *konfirmasi manusia.*"
        if per_item else "Tekan sekali untuk melanjutkan semua klip."
    )
    lines.append("Setiap yang kamu akui dicatat sebagai jejak override.")
    return "\n".join(lines)


async def campaign_send_gate_confirm(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, job: dict, gates: list[dict]
) -> None:
    """Tahan render; kirim daftar '⚠️ perlu kamu akui' + tombol per gate."""
    profile = campaign_get_profile(str(job.get("campaign_id"))) or {}
    per_item = str(profile.get("strictness", "")) == "strict"
    job[CAMPAIGN_GATE_ITEMS_KEY] = [
        {"code": str(g.get("code", "")), "message": str(g.get("message", "")),
         "clips": list(g.get("clips") or [])}
        for g in gates
    ]
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=campaign_gate_confirm_text(job, gates, per_item),
            parse_mode="Markdown",
            reply_markup=build_campaign_gate_ack_keyboard(gates, per_item),
        )
    except Exception as exc:
        LOGGER.warning("pesan konfirmasi gate (Markdown) gagal dikirim: %s", exc)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=re.sub(r"[*_`]", "", campaign_gate_confirm_text(job, gates, per_item)),
                reply_markup=build_campaign_gate_ack_keyboard(gates, per_item),
            )
        except Exception as exc2:  # pragma: no cover - jangan gantung, tapi jangan render
            LOGGER.error("pesan konfirmasi gate hilang total: %s", exc2)
            job.pop(CAMPAIGN_GATE_ITEMS_KEY, None)
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Daftar konfirmasi campaign tidak bisa ditampilkan. "
                     "Pilih klip satu per satu dari katalog ya (generate semua ditahan).",
            )


async def campaign_resume_generate_all_after_ack(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> None:
    """Setelah semua gate diakui: lanjut render semua klip yang belum jadi."""
    job = context.user_data.get("active_job")
    if not isinstance(job, dict):
        return
    job.pop(CAMPAIGN_GATE_ITEMS_KEY, None)
    moments = job.get("moments") or []
    unrendered = [
        i for i in range(1, len(moments) + 1) if i not in job.get("generated", set())
    ]
    if not unrendered:
        await context.bot.send_message(chat_id=chat_id, text="✅ Semua klip sudah digenerate.")
        return
    await run_generate_all_flow(context, chat_id, unrendered, None)


async def campaign_handle_gate_ack(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, raw_arg: str
) -> None:
    """Pengguna mengakui (sebagian) gerbang manusia -> lanjut/batal render."""
    query = update.callback_query
    chat_id = query.message.chat_id
    job = context.user_data.get("active_job")
    if not isinstance(job, dict) or not campaign_job_active(job):
        await context.bot.send_message(
            chat_id=chat_id,
            text="ℹ️ Tidak ada gerbang campaign yang menunggu konfirmasi. Kirim video baru ya!",
        )
        return

    if action == "gatecancel":
        job.pop(CAMPAIGN_GATE_ITEMS_KEY, None)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🚫 *Generate semua dibatalkan.*\n\n"
                "Tidak ada klip yang dirender. Kamu tetap bisa memilih klip satu per satu "
                "dari katalog di atas."
            ),
            parse_mode="Markdown",
        )
        return

    gates = job.get(CAMPAIGN_GATE_ITEMS_KEY)
    if not isinstance(gates, list) or not gates:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Daftar konfirmasi itu sudah tidak aktif. Tekan ✨ Generate Semua lagi ya!",
        )
        return

    if action == "ackall":
        targets = list(range(len(gates)))
    else:
        try:
            n = int(str(raw_arg).strip())
        except (TypeError, ValueError):
            n = -1
        if not 0 <= n < len(gates):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏳ Tombol itu sudah tidak aktif. Tekan ✨ Generate Semua lagi ya!",
            )
            return
        targets = [n]

    for n in targets:
        try:
            code = str((gates[n] or {}).get("code", "") or "")
        except (AttributeError, TypeError, IndexError):
            code = ""
        if code:
            campaign_mark_ack(job, code)

    acked = campaign_ack_set(job)
    remaining = [g for g in gates if str((g or {}).get("code", "")) not in acked]
    if remaining:
        job[CAMPAIGN_GATE_ITEMS_KEY] = remaining
        profile = campaign_get_profile(str(job.get("campaign_id"))) or {}
        per_item = str(profile.get("strictness", "")) == "strict"
        try:
            await query.message.edit_text(
                campaign_gate_confirm_text(job, remaining, per_item),
                parse_mode="Markdown",
                reply_markup=build_campaign_gate_ack_keyboard(remaining, per_item),
            )
        except Exception as exc:
            LOGGER.debug("update pesan konfirmasi gate dilewati: %s", exc)
        return

    job.pop(CAMPAIGN_GATE_ITEMS_KEY, None)
    try:
        await query.message.edit_text(
            "✅ Semua gerbang campaign diakui — lanjut render semua klip...",
            reply_markup=None,
        )
    except Exception:
        pass
    await campaign_resume_generate_all_after_ack(context, chat_id)



async def campaign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat_id
    if not campaign_enabled():
        await query.answer("Modul campaign tidak tersedia.", show_alert=True)
        return
    await query.answer()

    parts = data.split(":", 2)  # camp:<aksi>[:<arg>]
    action = parts[1] if len(parts) > 1 else ""
    arg = re.sub(r"[^A-Za-z0-9_\-.]", "", parts[2] if len(parts) > 2 else "")[:40]

    if action == "det":
        text = campaign_profile_details_text(arg) if arg else "Profil tidak dikenal."
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as exc:
            LOGGER.warning("detail campaign gagal dikirim: %s", exc)
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
        return

    if action == "show":
        await campaign_show_status(context, chat_id)
        return

    if action == "import":
        await campaign_import_start(update, context)
        return

    if action == "imp":
        await campaign_import_handle_callback(
            update, context, parts[2] if len(parts) > 2 else "",
        )
        return

    if action in ("ack", "ackall", "gatecancel"):
        await campaign_handle_gate_ack(
            update, context, action, parts[2] if len(parts) > 2 else "",
        )
        return

    if action == "set":
        if arg == "custom":
            context.user_data["awaiting_custom_rules"] = "global"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✍️ *Ketik Aturan Campaign Bebas:*\n\n"
                    "Silakan ketik atau tempel aturan/brief campaign kamu langsung di chat ini.\n\n"
                    "Contoh:\n"
                    "• Wajib bahas topik AI, jangan sebut kompetitor X\n"
                    "• Durasi klip 30-60 detik\n"
                    "• Syarat akun: minimal 1000 follower & wajib sertakan link bio\n\n"
                    "💡 _AI OriontClipper akan menyaring klip sesuai aturan video, dan menampilkan syarat akun (seperti follower) sebagai pengingat._\n"
                    "_Ketik /cancel untuk membatalkan._"
                ),
                parse_mode="Markdown",
            )
            return
        if arg == CAMPAIGN_FREE_ID:
            context.user_data.pop("pending_campaign_rules", None)
            context.user_data.pop("awaiting_custom_rules", None)
            await campaign_select_and_summary(context, chat_id, CAMPAIGN_FREE_ID)
            return
        known = {str(m.get("id", "")) for m in campaign_list_profiles()}
        if arg and arg in known:
            context.user_data.pop("pending_campaign_rules", None)
            context.user_data.pop("awaiting_custom_rules", None)
            await campaign_select_and_summary(context, chat_id, arg)
        else:
            await context.bot.send_message(chat_id=chat_id, text="Profil campaign tidak dikenal.")
        return

    if action == "job":
        job = context.user_data.get("active_job")
        awaiting = None
        if isinstance(job, dict):
            awaiting = job.get("awaiting_campaign")
        if not awaiting:
            await context.bot.send_message(
                chat_id=chat_id,
                text="ℹ️ Tidak ada pekerjaan yang menunggu pilihan campaign. Kirim video/link baru untuk mulai.",
                reply_markup=MAIN_MENU,
            )
            return

        if arg == "custom":
            context.user_data["awaiting_custom_rules"] = "job"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✍️ *Ketik Aturan Campaign untuk Video Ini:*\n\n"
                    "Silakan ketik atau tempel aturan/brief campaign kamu langsung di chat ini.\n\n"
                    "Contoh:\n"
                    "• Video wajib fokus ke topik tertentu\n"
                    "• Dilarang sebut brand kompetitor\n"
                    "• Akun harus 1000 followers\n\n"
                    "💡 _AI OriontClipper akan menalar aturan video (apa yang boleh & dilarang). Syarat akun seperti follower akan otomatis dicatat sebagai pengingat._\n"
                    "_Ketik /cancel untuk membatalkan._"
                ),
                parse_mode="Markdown",
            )
            return

        # Pilihan selain custom (misal Bebas atau profil preset)
        if isinstance(job, dict):
            job.pop("awaiting_campaign", None)
        context.user_data.pop("awaiting_custom_rules", None)
        context.user_data.pop("pending_campaign_rules", None)

        if arg == CAMPAIGN_FREE_ID:
            context.user_data["pending_campaign"] = CAMPAIGN_FREE_ID
        else:
            known = {str(m.get("id", "")) for m in campaign_list_profiles()}
            if not arg or arg not in known:
                try:
                    arg = str(cp.default_profile_id()) or CAMPAIGN_FREE_ID
                except Exception:
                    arg = CAMPAIGN_FREE_ID
            context.user_data["pending_campaign"] = arg
        if not try_claim_lock():
            if isinstance(job, dict):
                job["awaiting_campaign"] = awaiting
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏳ Bot sedang memproses video user lain. Tekan lagi tombol campaign-nya sebentar lagi.",
            )
            return
        try:
            try:
                await query.message.edit_text("🎯 Campaign dipilih — memproses video...")
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await resume_job_from_callback(update, context, awaiting)
        finally:
            release_lock()
        return


async def campaign_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/campaign — pilih aturan compliance; /campaign show — status aktif."""
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if not campaign_enabled():
        await update.message.reply_text(
            "⚠️ Modul campaign tidak tersedia — fitur nonaktif.",
            reply_markup=MAIN_MENU,
        )
        return

    arg = (context.args[0] if context.args else "").strip()
    if arg.lower() == "show":
        await campaign_show_status(context, chat_id)
        return
    if arg.lower() == "import":
        await campaign_import_start(update, context)
        return
    if arg:
        pid = re.sub(r"[^A-Za-z0-9_\-.]", "", arg)[:40]
        known = {str(m.get("id", "")) for m in campaign_list_profiles()}
        if pid == CAMPAIGN_FREE_ID or pid in known:
            await campaign_select_and_summary(context, chat_id, pid)
        else:
            await update.message.reply_text(
                f"Profil `{escape_tg_md(pid)}` tidak dikenal. Lihat pilihan lewat /campaign.",
                parse_mode="Markdown", reply_markup=MAIN_MENU,
            )
        return

    await update.message.reply_text(
        "🎯 *CAMPAIGN COMPLIANCE*\n\n"
        "Pilih profil aturan — akan dipakai untuk video BERIKUTNYA (sekali pakai).\n"
        "Kirim /campaign show kapan pun untuk melihat aturan aktif.",
        parse_mode="Markdown",
        reply_markup=build_campaign_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# /campaign import (langkah 3) — impor bahan S&K -> compiler -> draft profil.
#
# modules/campaign_compiler.py dibangun lane PARALEL: nama modul TIDAK PERNAH
# muncul di klausa import tingkat-modul (hanya string di importlib di dalam
# try/except), jadi bot tetap hidup bila modul belum ada. State murni
# user_data["camp_import"] dan TIDAK pernah mengklaim _PROCESSING_LOCK —
# alur klip tidak boleh terganggu oleh impor.
# ---------------------------------------------------------------------------

CAMP_IMPORT_RAW_LIMIT = 12000   # kurasi mentok bahan S&K
CAMP_IMPORT_MIN_CHARS = 80      # ambang bawah bahan S&K
CAMP_IMPORT_CHUNK = 4000        # batas potongan tampilan ringkasan


def campaign_cc():
    """Lazy import modules.campaign_compiler. -> (modul|None, pesan_err|None).

    Dipanggil HANYA dari dalam handler (try/except sudah di pemanggil);
    tabrakan/kegagalan import apa pun dianggap 'belum tersedia', bukan crash.
    """
    try:
        import importlib

        return importlib.import_module(f"modules.{CAMPAIGN_CC_IMPORT}"), None
    except Exception as exc:
        LOGGER.warning("lazy import %s gagal: %s", CAMPAIGN_CC_IMPORT, exc)
        return None, str(exc)


class _CampaignChatAdapter(AIAnalyzer):
    """chat_fn untuk campaign_compiler: SATU panggilan LLM, balas teks mentah.

    JAHLAN LANGKAH-3 (PENTING, jangan dikembalikan): method warisan
    ``AIAnalyzer._request_chat_completion`` SELALU membungkus teksnya ke
    ``USER_PROMPT_TEMPLATE`` — prompt analisis klip yang meminta
    ``{"clips": [...]}``. Bila bahan S&K dikirim lewat jalur itu, model disuruh
    membuat daftar klip dari instruksi compiler, dan ``compile_profile`` pasti
    mati di ``"jawaban AI tidak punya bagian 'profile'"``. Jadi adapter sengaja
    TIDAK memakai prompt klip: transportnya ``campaign_cc_completion`` berikut,
    yang mengirim ``messages`` APA ADANYA (role system/user dipertahankan, urutan
    sama dengan ``cc.build_messages``).

    ``config``/``base_url``/``api_key``/``model``/``logger`` tetap diwarisi dari
    AIAnalyzer supaya satu sumber kebenaran setting 9Router.
    """

    def _request_chat_completion(self, *args: Any, **kwargs: Any) -> str:
        """Dilarang keras — jalur ini membungkus bahan S&K ke prompt analisis klip."""
        raise AIAnalyzerError(
            "_CampaignChatAdapter tidak memakai prompt analisis klip "
            "(gunakan campaign_cc_completion / _chat_text)"
        )

    def _chat_text(self, messages: list[dict]) -> str:
        return campaign_cc_completion(self, messages)


def campaign_cc_completion(analyzer: Any, messages: list[dict]) -> str:
    """Kirim pesan compiler apa adanya ke 9Router — SATU percobaan, tanpa retry.

    Retry bukan tanggung jawab sini: ``campaign_compiler`` memanggil ``chat_fn``
    tepat sekali dan keputusan mengulang ada di tangan user (tombol 🔁 Paste
    ulang). Mengulang diam-diam di sini membuat biaya/latensi tak terkendali dan
    menutupi dokumen S&K yang memang tidak bisa dipetakan.
    """
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise AIAnalyzerError("requests is required; run: pip install -r requirements.txt") from exc

    payload_messages = [
        {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
        for m in (messages or [])
        if isinstance(m, dict)
    ]
    if not payload_messages:
        raise AIAnalyzerError("campaign_compiler tidak memberi pesan apa pun")

    url = f"{analyzer.base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if analyzer.api_key:
        headers["Authorization"] = f"Bearer {analyzer.api_key}"
    payload = {
        "model": analyzer.model,
        "messages": payload_messages,
        "temperature": 0.0,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=config.REQUEST_TIMEOUT_S)
    except requests.exceptions.Timeout as exc:
        raise AIAnalyzerError(f"9Router timeout setelah {config.REQUEST_TIMEOUT_S}s") from exc
    except requests.exceptions.ConnectionError as exc:
        raise AIAnalyzerError("9Router connection error/unreachable") from exc
    except requests.exceptions.RequestException as exc:
        raise AIAnalyzerError(f"9Router request error: {exc}") from exc

    if response.status_code != 200:
        raise AIAnalyzerError(f"9Router HTTP {response.status_code}: {(response.text or '')[:600]}")

    try:
        return extract_chat_message_content(parse_chat_response_body(response.text))
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        # ValueError sudah mencakup json.JSONDecodeError
        raise AIAnalyzerError(f"Respons 9Router bentuknya tak terduga: {exc}") from exc


def campaign_import_state(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    st = context.user_data.get("camp_import")
    return st if isinstance(st, dict) else None


def campaign_reply_target(update: Update) -> Message:
    """Pesan yang bisa di-reply: update.message (perintah/teks) atau query.message."""
    return update.message if update.message is not None else update.callback_query.message


def campaign_import_clear(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Hapus state impor dari SEMUA jalur terminal. True bila ada state aktif."""
    try:
        return context.user_data.pop("camp_import", None) is not None
    except AttributeError:  # pragma: no cover - konteks tanpa user_data
        return False


def build_campaign_import_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Simpan & jadikan campaign berikutnya", callback_data="camp:imp:save")],
        [InlineKeyboardButton("🔁 Paste ulang", callback_data="camp:imp:again")],
        [InlineKeyboardButton("Batal", callback_data="camp:imp:cancel")],
    ])


def build_campaign_import_error_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Paste ulang", callback_data="camp:imp:again")],
        [InlineKeyboardButton("Batal", callback_data="camp:imp:cancel")],
    ])


async def campaign_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mulai alur impor dari /campaign import atau tombol 📥. 'Belum tersedia' tanpa crash."""
    reply = campaign_reply_target(update)
    if not campaign_enabled():
        await reply.reply_text(
            "⚠️ Impor S&K belum tersedia — fitur campaign tidak aktif.",
            reply_markup=MAIN_MENU,
        )
        return
    cc, err = campaign_cc()
    if cc is None:
        await reply.reply_text(
            f"⚠️ Fitur impor S&K belum tersedia (module compiler: {err}).",
            reply_markup=MAIN_MENU,
        )
        return
    context.user_data["camp_import"] = {"stage": "await_text"}
    await reply.reply_text(
        "Tempel teks S&K lengkap (min 80 char) sebagai PESAN TEKS biasa. "
        "Kirim /cancel untuk batal.",
        reply_markup=MAIN_MENU,
    )


def campaign_import_chunks(escaped: str, limit: int = CAMP_IMPORT_CHUNK) -> list[str]:
    r"""Potong teks yang SUDAH di-escape Markdown-v1 tanpa merusak pasangan ``\x``.

    WAJIB sesudah escaping (bukan sebelumnya): satu karakter profil bisa jadi 2
    karakter di payload, jadi memotong teks mentah di 4000 lalu meng-escape bisa
    melewati limit Telegram 4096. Potongan juga tidak boleh berakhir dengan
    backslash menggantung (parse_mode Markdown akan error).
    """
    text = str(escaped or "")
    if not text:
        return ["(kosong)"]
    cap = max(3, int(limit))
    out: list[str] = []
    rest = text
    while len(rest) > cap:
        cut = cap
        while cut > 1 and rest[cut - 1] == "\\":
            cut -= 1
        out.append(rest[:cut])
        rest = rest[cut:]
    out.append(rest)
    return out


async def campaign_import_send_summary(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
) -> None:
    """Tampilkan summarize_for_user: escape Markdown-v1 DULU, baru chunk <=4000."""
    chunks = campaign_import_chunks(escape_tg_md(text))
    kb = build_campaign_import_keyboard()
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        body = chunk
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=body,
                parse_mode="Markdown",
                reply_markup=kb if is_last else None,
            )
        except Exception as exc:
            LOGGER.debug("ringkasan impor Markdown gagal (%s); teks polos", exc)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=body,
                    reply_markup=kb if is_last else None,
                )
            except Exception:
                pass  # tampilan bukan jalur kritis — state result tetap hidup


def campaign_import_compile_sync(cc: Any, raw: str) -> dict:
    """EKSEKUTOR/BLOCKING: compile_profile dengan chat_fn dari AIAnalyzer."""
    analyzer = _CampaignChatAdapter(logger=LOGGER)

    def chat_fn(messages: list[dict]) -> str:
        return analyzer._chat_text(messages)

    out = cc.compile_profile(raw, chat_fn)
    if not isinstance(out, dict):
        raise RuntimeError("compile_profile tidak mengembalikan dict")
    return out


async def campaign_import_run_compile(
    context: ContextTypes.DEFAULT_TYPE,
    cc: Any,
    chat_id: int,
    st: dict,
    raw: str,
    status_msg: Message,
) -> None:
    """Kompilasi via asyncio.to_thread + render hasil/error. State selalu konsisten."""
    st["stage"] = "compiling"
    st["raw"] = raw
    try:
        result = await asyncio.to_thread(campaign_import_compile_sync, cc, raw)
    except Exception as exc:
        human = ""
        try:
            compiler_err = getattr(cc, "CompilerError", None)
        except Exception:  # pragma: no cover - getattr modul tidak raise
            compiler_err = None
        if compiler_err is not None and isinstance(exc, compiler_err):
            human = str(exc) or "CompilerError tanpa pesan"
        else:
            LOGGER.exception("compile S&K gagal (non-CompilerError)")
            human = f"Terjadi kesalahan tak terduga: {exc}"
        st["stage"] = "await_text"
        st.pop("result", None)
        lines = [f"❌ *Impor S&K gagal:*\n{escape_tg_md(human[:600])}"]
        # Rincian terstruktur dari CompilerError.details (mis. alasan penolakan
        # validator) — TANPA ini user cuma melihat 'ditolak validator' buta.
        detail_txt: list[str] = []
        for item in (list(getattr(exc, "details", None) or [])[:3]):
            one = " ".join(str(item).split())[:220]
            if one:
                detail_txt.append(f"  • {escape_tg_md(one)}")
        if detail_txt:
            lines.append("Rincian:\n" + "\n".join(detail_txt))
        low = human.lower()
        if "klausul" in low and "hilang" in low:
            lines.append(
                "\n💡 Saran: padukan poin bertanda yang terpotong jadi satu paragraf, "
                "atau kirim versi lebih rapi."
            )
        lines.append("Kamu bisa tempel ulang teks yang sudah diperbaiki.")
        try:
            await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")
        except Exception:
            try:
                await status_msg.edit_text(re.sub(r"[*_`]", "", "\n".join(lines)))
            except Exception as exc2:  # pragma: no cover
                LOGGER.warning("pesan error impor tidak terkirim: %s", exc2)
        try:
            await status_msg.reply_text(
                "Pilih: tempel ulang teks S&K, tekan 🔁 Paste ulang, atau /cancel.",
                reply_markup=build_campaign_import_error_keyboard(),
            )
        except Exception:
            pass
        return

    st["stage"] = "result"
    st["result"] = result
    st.pop("raw", None)
    try:
        summary = str(cc.summarize_for_user(result) or "(tidak ada ringkasan)")
    except Exception as exc:
        LOGGER.warning("summarize_for_user gagal: %s", exc)
        summary = "✅ S&K terkompilasi (ringkasan tidak bisa ditampilkan)."
    try:
        await status_msg.edit_text("📋 Ringkasan hasil analisis:")
    except Exception:
        pass
    await campaign_import_send_summary(context, chat_id, summary)


async def campaign_import_handle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
) -> None:
    """Tombol camp:imp:* — save / again / cancel. State dibersihkan di jalur terminal."""
    query = update.callback_query
    chat_id = query.message.chat_id
    st = campaign_import_state(context)
    if st is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="ℹ️ Sesi impor S&K tidak aktif lagi. Mulai lewat /campaign import.",
        )
        return
    stage = str(st.get("stage") or "")

    if action == "again":
        st["stage"] = "await_text"
        st.pop("result", None)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text="Tempel teks S&K lengkap (min 80 char) sebagai PESAN TEKS biasa. "
                 "Kirim /cancel untuk batal.",
        )
        return

    if action == "cancel":
        campaign_import_clear(context)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text="🚫 Impor S&K dibatalkan — tidak ada profil yang disimpan.",
            reply_markup=MAIN_MENU,
        )
        return

    if action == "save":
        if stage != "result" or not isinstance(st.get("result"), dict):
            await query.answer("Tunggu hasil analisis selesai dulu ya!", show_alert=True)
            return
        cc, err = campaign_cc()
        if cc is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Simpan draft belum tersedia (module compiler: {err}).",
            )
            return
        try:
            cid = str(cc.save_draft(st["result"]))
        except Exception as exc:
            LOGGER.exception("save_draft gagal")
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Gagal menyimpan draft profil "
                    f"({escape_tg_md(str(exc)[:200])}) — hasil analisis masih di layar, coba lagi."
                ),
                parse_mode="Markdown",
                reply_markup=build_campaign_import_keyboard(),
            )
            return
        campaign_import_clear(context)
        context.user_data["pending_campaign"] = cid
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "💾 *Draft tersimpan & siap!*\n\n"
                f"🎯 Campaign `{escape_tg_md(cid)}` akan dipakai untuk *video BERIKUTNYA*.\n"
                "Kirim /campaign untuk melihat daftar / menggantinya.",
            ),
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    await context.bot.send_message(chat_id=chat_id, text="Aksi impor tidak dikenal.")


async def campaign_import_handle_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Intersep PESAN TEKS saat state camp_import aktif — SEBELUM deteksi
    url/file/flow lain dan SEBELUM klaim lock. Menu button tetap ditangani
    caller (kandidat material hanya teks bebas)."""
    chat_id = update.effective_chat.id
    st = campaign_import_state(context)
    if st is None:  # pragma: no cover - caller sudah cek
        return
    stage = str(st.get("stage") or "")

    if stage == "compiling":
        await update.message.reply_text(
            "⏳ S&K sedang dianalisis — sebentar ya. Tekan Batal untuk hentikan tampilan "
            "(proses di latar tetap berjalan sampai selesai)."
        )
        return

    if text.lower().startswith("/"):
        # perintah yang lolos ke teks-bebas (mis. '/campaign import' diketik ulang)
        campaign_import_clear(context)
        await update.message.reply_text(
            "🚫 Impor S&K dibatalkan — pesan tampak seperti perintah, bukan bahan S&K.\n"
            "Mulai lagi dengan /campaign import.",
            reply_markup=MAIN_MENU,
        )
        return

    raw = text.strip()
    if len(raw) < CAMP_IMPORT_MIN_CHARS:
        await update.message.reply_text(
            f"⚠️ Bahan S&K terlalu pendek (min {CAMP_IMPORT_MIN_CHARS} karakter). "
            "Tempel ulang teks lengkap, atau kirim /cancel."
        )
        return

    first_token = raw.split()[0] if raw.split() else ""
    if is_youtube_url(first_token) or gdrive_flow.is_gdrive_url(first_token):
        campaign_import_clear(context)
        await update.message.reply_text(
            "ℹ️ Itu link video — impor S&K dibatalkan, link diproses seperti biasa.",
        )
        return

    notice = ""
    if len(raw) > CAMP_IMPORT_RAW_LIMIT:
        notice = (
            f"ℹ️ Bahan panjang — dipotong ke {CAMP_IMPORT_RAW_LIMIT} karakter "
            "agar muat di analisis."
        )
        raw = raw[:CAMP_IMPORT_RAW_LIMIT]

    cc, err = campaign_cc()
    if cc is None:
        campaign_import_clear(context)
        await update.message.reply_text(
            f"⚠️ Fitur impor S&K belum tersedia (module compiler: {err}).",
            reply_markup=MAIN_MENU,
        )
        return

    if notice:
        await update.message.reply_text(notice)
    try:
        status_msg = await update.message.reply_text("⏳ Menganalisis S&K...")
    except Exception:  # pragma: no cover
        status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Menganalisis S&K...")
    await campaign_import_run_compile(context, cc, chat_id, st, raw, status_msg)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel — batal state impor S&K atau input aturan campaign kustom; selain itu no-op ramah."""
    if not is_allowed(update.effective_user.id):
        return
    canceled = False
    if context.user_data.pop("awaiting_custom_rules", None):
        canceled = True
    job = context.user_data.get("active_job")
    if isinstance(job, dict) and job.pop("awaiting_custom_rules", None):
        canceled = True
    if campaign_import_clear(context):
        canceled = True
    if canceled:
        await update.message.reply_text(
            "🚫 Pengisian aturan / brief campaign dibatalkan.",
            reply_markup=MAIN_MENU,
        )
        return
    await update.message.reply_text(
        "Tidak ada proses input aturan yang berjalan. /done untuk membersihkan sesi klip.",
        reply_markup=MAIN_MENU,
    )


# ---------------------------------------------------------------------------
# Style & Hook Customization
# ---------------------------------------------------------------------------

PRESET_NAMES: dict[str, str] = {
    "hormozi": "Alex Hormozi (Bold & Yellow)",
    "capcut": "CapCut Modern (Clean Pill)",
    "cinematic": "Cinematic Gold (Warm Film)",
    "minimal": "Minimalist (Stroke & Shadow)",
    "neon_badge": "Cyber Neon (Cyan Glow)",
}

POSITION_NAMES: dict[str, str] = {
    "top": "Atas (Top)",
    "upper_center": "Tengah-Atas (Upper Center)",
    "center": "Tengah (Center)",
    "lower": "Bawah (Lower)",
}

COLOR_NAMES: dict[str, str] = {
    "#ffe600": "Kuning 🟡",
    "#00e5ff": "Cyan 🔵",
    "#ff3b30": "Merah 🔴",
    "#00ff66": "Hijau 🟢",
    "#ffffff": "Putih ⚪",
}


def get_user_style(context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    default_style = {
        "preset": "hormozi",
        "position": "upper_center",
        "highlight_color": "#ffe600",
    }
    user_style = context.user_data.get("hook_style")
    if not isinstance(user_style, dict):
        user_style = dict(default_style)
        context.user_data["hook_style"] = user_style
    else:
        for k, v in default_style.items():
            user_style.setdefault(k, v)
    return user_style


def get_user_hook_template(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    style = get_user_style(context)
    preset_name = style.get("preset", "hormozi")
    template = load_named_template(preset_name)
    pos = style.get("position", "upper_center")
    color = style.get("highlight_color", "#ffe600")

    if "hook" in template and isinstance(template["hook"], dict):
        template["hook"]["position"] = pos
        template["hook"]["y"] = pos
        template["hook"]["highlight_color"] = color
        template["hook"]["highlight_last_word"] = True  # Ensure hook always displays the chosen highlight color
        if template["hook"].get("box_outline_color"):
            template["hook"]["box_outline_color"] = f"{color}@0.65"

    if "subtitle" in template and isinstance(template["subtitle"], dict):
        template["subtitle"]["highlight_color"] = color
        if pos == "lower":
            template["subtitle"]["margin_v"] = 280
        elif pos in ("top", "upper_center"):
            template["subtitle"]["margin_v"] = 420

    return template


def render_sample_hook_preview(template: dict[str, Any], text: str, out_path: Path) -> Path:
    from PIL import Image
    from modules.overlay_renderer import render_text_layer
    from modules.video_editor import prepare_hook_text

    hook_sec = template.get("hook", {})
    if "\n" not in text:
        text = prepare_hook_text(text, hook_sec if "max_line_chars" in hook_sec else template)

    bg = Image.new("RGB", (1080, 1920), (18, 21, 28))
    layer = render_text_layer(text, hook_sec, 1080, 1920)
    bg.paste(layer, (0, 0), layer)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out_path, "PNG")
    return out_path


def build_style_message_and_keyboard(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    style = get_user_style(context)
    preset = style.get("preset", "hormozi")
    pos = style.get("position", "upper_center")
    color = style.get("highlight_color", "#ffe600").lower()

    msg = (
        "🎨 *PENGATURAN STYLE & HOOK VIDEO*\n\n"
        f"👑 *Preset Style Aktif:* `{PRESET_NAMES.get(preset, preset)}`\n"
        f"📍 *Letak/Posisi Hook:* `{POSITION_NAMES.get(pos, pos)}`\n"
        f"✨ *Warna Highlight Kata Kunci:* `{COLOR_NAMES.get(color, color)}` (`{color}`)\n\n"
        "💡 *Tips Custom:*\n"
        "• Klik tombol preset / posisi / warna di bawah untuk mengubah seketika.\n"
        "• Klik *👁️ Preview Hook* untuk melihat langsung tampilan 9:16 di chat Telegram.\n"
        "• Ingin ubah teks hook klip? Ketik: `/edithook <id> <teks>`\n"
        "  _Contoh: `/edithook 1 RAHASIA *CUAN* MELEDAK` (kata dalam `*bintang*` otomatis di-highlight!)_\n"
    )

    kb = [
        # Presets row 1
        [
            InlineKeyboardButton(f"{'✅ ' if preset=='hormozi' else ''}Hormozi", callback_data="style:preset:hormozi"),
            InlineKeyboardButton(f"{'✅ ' if preset=='capcut' else ''}CapCut", callback_data="style:preset:capcut"),
            InlineKeyboardButton(f"{'✅ ' if preset=='cinematic' else ''}Cinematic", callback_data="style:preset:cinematic"),
        ],
        # Presets row 2
        [
            InlineKeyboardButton(f"{'✅ ' if preset=='minimal' else ''}Minimal", callback_data="style:preset:minimal"),
            InlineKeyboardButton(f"{'✅ ' if preset=='neon_badge' else ''}Cyber Neon", callback_data="style:preset:neon_badge"),
        ],
        # Position row
        [
            InlineKeyboardButton(f"{'📍 ' if pos=='top' else ''}Atas", callback_data="style:pos:top"),
            InlineKeyboardButton(f"{'📍 ' if pos=='upper_center' else ''}Tengah-Atas", callback_data="style:pos:upper_center"),
            InlineKeyboardButton(f"{'📍 ' if pos=='center' else ''}Tengah", callback_data="style:pos:center"),
            InlineKeyboardButton(f"{'📍 ' if pos=='lower' else ''}Bawah", callback_data="style:pos:lower"),
        ],
        # Color row
        [
            InlineKeyboardButton(f"{'✨ ' if color=='#ffe600' else ''}🟡 Kuning", callback_data="style:color:#ffe600"),
            InlineKeyboardButton(f"{'✨ ' if color=='#00e5ff' else ''}🔵 Cyan", callback_data="style:color:#00e5ff"),
            InlineKeyboardButton(f"{'✨ ' if color=='#ff3b30' else ''}🔴 Merah", callback_data="style:color:#ff3b30"),
            InlineKeyboardButton(f"{'✨ ' if color=='#00ff66' else ''}🟢 Hijau", callback_data="style:color:#00ff66"),
            InlineKeyboardButton(f"{'✨ ' if color=='#ffffff' else ''}⚪ Putih", callback_data="style:color:#ffffff"),
        ],
        # Actions row
        [
            InlineKeyboardButton("👁️ Preview Hook", callback_data="style:preview"),
            InlineKeyboardButton("❌ Tutup Menu", callback_data="style:close"),
        ],
    ]
    return msg, InlineKeyboardMarkup(kb)


async def style_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    msg, kb = build_style_message_and_keyboard(context)
    await update.effective_message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)


async def style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("style:"):
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    style = get_user_style(context)

    if action == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "preset" and len(parts) > 2:
        preset_name = parts[2]
        if preset_name in PRESET_NAMES:
            style["preset"] = preset_name
    elif action == "pos" and len(parts) > 2:
        pos_name = parts[2]
        if pos_name in POSITION_NAMES:
            style["position"] = pos_name
    elif action == "color" and len(parts) > 2:
        color_code = parts[2]
        style["highlight_color"] = color_code
    elif action == "preview":
        tpl = get_user_hook_template(context)
        preview_file = config.PROJECT_ROOT / "tmp" / f"hook_preview_{update.effective_user.id}.png"
        sample_text = "CONTOH *HOOK* VIRAL TERBARU!"
        job = context.user_data.get("active_job")
        if job and job.get("moments"):
            sample_text = job["moments"][0].hook

        try:
            render_sample_hook_preview(tpl, sample_text, preview_file)
            with open(preview_file, "rb") as f:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=f,
                    caption=(
                        f"👁️ *Preview Tampilan Hook Saat Ini:*\n"
                        f"• Preset: `{PRESET_NAMES.get(style['preset'], style['preset'])}`\n"
                        f"• Posisi: `{POSITION_NAMES.get(style['position'], style['position'])}`\n"
                        f"• Highlight: `{style['highlight_color']}`\n\n"
                        f"Teks contoh: \"{sample_text}\""
                    ),
                    parse_mode="Markdown",
                )
        except Exception as exc:
            LOGGER.error("Failed to render preview: %s", exc)
            await query.message.reply_text(f"❌ Gagal render preview: {exc}")
        return

    msg, kb = build_style_message_and_keyboard(context)
    try:
        await query.message.edit_text(msg, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        pass


async def setstyle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    args = context.args or []
    if not args:
        options = ", ".join(f"`{k}`" for k in PRESET_NAMES.keys())
        await update.message.reply_text(
            f"ℹ️ Format: `/setstyle <nama_preset>`\n\nPilihan preset: {options}",
            parse_mode="Markdown",
        )
        return
    choice = args[0].lower().strip()
    if choice not in PRESET_NAMES:
        options = ", ".join(f"`{k}`" for k in PRESET_NAMES.keys())
        await update.message.reply_text(
            f"❌ Preset `{choice}` tidak ditemukan.\nPilihan yang tersedia: {options}",
            parse_mode="Markdown",
        )
        return
    style = get_user_style(context)
    style["preset"] = choice
    await update.message.reply_text(
        f"✅ Preset berhasil diubah ke: *{PRESET_NAMES[choice]}*",
        parse_mode="Markdown",
    )


async def setpos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    args = context.args or []
    if not args:
        options = ", ".join(f"`{k}`" for k in POSITION_NAMES.keys())
        await update.message.reply_text(
            f"ℹ️ Format: `/setpos <posisi>`\n\nPilihan: {options} atau angka koordinat Y (misal `300`).",
            parse_mode="Markdown",
        )
        return
    pos = args[0].lower().strip()
    style = get_user_style(context)
    style["position"] = pos
    name = POSITION_NAMES.get(pos, f"Custom Y ({pos})")
    await update.message.reply_text(
        f"✅ Posisi hook berhasil diubah ke: *{name}*",
        parse_mode="Markdown",
    )


async def setcolor_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "ℹ️ Format: `/setcolor <kode_hex>`\n\nContoh: `/setcolor #FFE600` (Kuning) atau `/setcolor #00E5FF` (Cyan).",
            parse_mode="Markdown",
        )
        return
    col = args[0].strip()
    if not col.startswith("#") and len(col) == 6:
        col = f"#{col}"
    style = get_user_style(context)
    style["highlight_color"] = col
    await update.message.reply_text(
        f"✅ Warna highlight kata kunci berhasil diubah ke: `{col}`",
        parse_mode="Markdown",
    )


async def edithook_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    job = context.user_data.get("active_job")
    if not job or not job.get("moments"):
        await update.message.reply_text(
            "⚠️ Belum ada sesi klip aktif. Kirim file video atau link YouTube dulu!",
            reply_markup=MAIN_MENU,
        )
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "📌 *Cara Mengubah Teks Hook Klip:*\n\n"
            "Ketik: `/edithook <nomor_klip> <teks_hook_baru>`\n\n"
            "*Contoh:*\n"
            "`/edithook 1 RAHASIA *CUAN* DARI TIKTOK!`\n\n"
            "_Tips: Kata di dalam tanda bintang `*KATA*` akan otomatis diberi warna highlight menyala!_",
            parse_mode="Markdown",
        )
        return

    try:
        clip_idx = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Nomor klip harus angka (contoh: 1, 2, 3).")
        return

    moments = job["moments"]
    if clip_idx < 1 or clip_idx > len(moments):
        await update.message.reply_text(f"❌ Nomor klip tidak valid. Tersedia klip 1 s/d {len(moments)}.")
        return

    new_hook = " ".join(args[1:]).strip()
    old_hook = moments[clip_idx - 1].hook
    moments[clip_idx - 1] = dataclasses.replace(moments[clip_idx - 1], hook=new_hook)

    await update.message.reply_text(
        f"✅ *Hook Klip #{clip_idx} Berhasil Diubah!*\n\n"
        f"• *Lama:* \"{escape_tg_md(old_hook)}\"\n"
        f"• *Baru:* \"{escape_tg_md(new_hook)}\"\n\n"
        f"Siap dirender dengan tombol *🎬 Generate Klip {clip_idx}*!",
        parse_mode="Markdown",
    )


async def previewhook_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    custom_text = " ".join(context.args).strip() if context.args else ""
    if not custom_text:
        job = context.user_data.get("active_job")
        if job and job.get("moments"):
            custom_text = job["moments"][0].hook
        else:
            custom_text = "CONTOH *HOOK* VIRAL TERBARU!"

    style = get_user_style(context)
    tpl = get_user_hook_template(context)
    preview_file = config.PROJECT_ROOT / "tmp" / f"hook_preview_{update.effective_user.id}.png"

    try:
        render_sample_hook_preview(tpl, custom_text, preview_file)
        with open(preview_file, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=(
                    f"👁️ *Preview Tampilan Hook:*\n"
                    f"• Preset: `{PRESET_NAMES.get(style['preset'], style['preset'])}`\n"
                    f"• Posisi: `{POSITION_NAMES.get(style['position'], style['position'])}`\n"
                    f"• Highlight: `{style['highlight_color']}`\n\n"
                    f"Teks: \"{custom_text}\""
                ),
                parse_mode="Markdown",
            )
    except Exception as exc:
        LOGGER.error("Failed to render preview: %s", exc)
        await update.message.reply_text(f"❌ Gagal render preview: {exc}")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Maaf, Anda tidak memiliki akses ke bot ini.")
        return

    cookies_status = "✅ Aktif" if has_cookies() else "❌ Belum Terpasang"
    await update.message.reply_text(
        "✨ *OriontClip — AI Smart Clipper*\n\n"
        "Saya mengubah video panjang (YouTube / Google Drive / file) menjadi klip vertikal 9:16.\n\n"
        "🌟 *Fitur & Alur Kerja:*\n"
        "1. Kirim file video, link YouTube, atau link Google Drive.\n"
        "2. AI akan menganalisis dan menemukan *banyak pilihan topik menarik*, hook, dan caption medsos.\n"
        "3. Pilih klip mana yang ingin digenerate via tombol.\n"
        "4. 🎨 *Kustomisasi Teks Hook:* Tekan menu *Style & Hook* untuk ganti posisi, warna highlight, atau edit teks hook klip dengan `/edithook`!\n\n"
        f"Cookies YouTube: {cookies_status}\n\n"
        "Pilih menu di keyboard atau kirim video/link langsung!",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*OriontClip — Panduan Penggunaan*\n\n"
        "▸ *Kirim link Google Drive* — link file video (akses 'Siapa saja yang memiliki link')\n"
        "▸ *Kirim link YouTube* — bot akan ambil transkrip & video otomatis\n"
        "▸ *Kirim file video* — format .mp4/.mov/.mkv langsung ke chat\n"
        "▸ */style* — atur preset hook (Hormozi, CapCut, Cinematic, dll), letak posisi (Atas/Tengah/Bawah), dan warna highlight\n"
        "▸ */edithook <id> <teks>* — ganti teks hook klip sesukamu (gunakan `*KATA*` untuk highlight warna menyala)\n"
        "▸ */previewhook* — preview langsung tampilan hook di resolusi vertikal 9:16\n"
        "▸ */cookies* — upload cookies.txt untuk bypass blokade YouTube\n"
        "▸ */klip* — melihat daftar pilihan konteks klip yang sedang aktif\n"
        "▸ */campaign* — pilih profil aturan kepatuhan campaign untuk video berikutnya\n"
        "▸ */campaign import* — tempel teks S&K, bot meracik profil otomatis\n"
        "▸ */done* — selesai dan bersihkan file sementara\n\n"
        "*Alur Kerja:*\n"
        "1. Bot menganalisis transkrip & mencari topik-topik klip terbaik.\n"
        "2. Bot menampilkan katalog topik, hook, dan caption.\n"
        "3. Tekan *Generate Klip X* untuk klip yang kamu inginkan.\n"
        "4. Bot merender klip terpilih tanpa menghapus pilihan klip lainnya.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*OriontClip*\n\n"
        "Versi 2.0 — Interactive AI Video Curator\n"
        "- Transkripsi: OpenAI Whisper (small, CPU) / YouTube Captions\n"
        "- Analisis AI: 9Router (LLM Topic & Hook Discovery)\n"
        "- Subtitle: ASS styling dengan auto-wrap & safe-zone placement\n"
        "- Render: FFmpeg + Pillow Overlay\n"
        "- Output: 1080×1920 (9:16 vertikal)\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        " IG: @jalipryyy",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    has = has_cookies()
    status = "✅ *Cookies terpasang*" if has else "❌ *Belum ada cookies*"

    await update.message.reply_text(
        f"{status}\n\n"
        "YouTube sekarang mewajibkan cookies untuk download.\n"
        "Kirim file *cookies.txt* untuk bypass.\n\n"
        "*Cara export cookies.txt:*\n"
        "1. Buka YouTube di Chrome/Edge/Firefox (PC)\n"
        "2. Login ke akun YouTube (bisa akun cadangan khusus)\n"
        "3. Pasang ekstensi *Get cookies.txt* (Chrome Web Store)\n"
        "4. Klik ikon ekstensi → *Export* → simpan sebagai cookies.txt\n"
        "5. Kirim file *cookies.txt* ke chat ini\n\n"
        "_Cookies dipakai bersama untuk semua user._",
        parse_mode="Markdown",
        reply_markup=YOUTUBE_HELP_MENU,
    )


# ---------------------------------------------------------------------------
# Cookie file handler
# ---------------------------------------------------------------------------

async def handle_cookies_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if COOKIE_PATH.exists():
        try:
            COOKIE_PATH.chmod(0o666)
        except Exception:
            pass

    try:
        file = await update.effective_message.effective_attachment.get_file()
        await file.download_to_drive(custom_path=str(COOKIE_PATH))
    except Exception as exc:
        await update.message.reply_text(f"Gagal menyimpan cookies: {exc}")
        return

    if os.name != "nt":
        COOKIE_PATH.chmod(0o600)

    try:
        content = COOKIE_PATH.read_text(encoding="utf-8")
        if "youtube" not in content.lower() and ".youtube.com" not in content:
            COOKIE_PATH.unlink(missing_ok=True)
            await update.message.reply_text(
                "File tidak valid. Pastikan file *cookies.txt* hasil export "
                "dari browser yang sudah login ke YouTube.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return
    except Exception:
        COOKIE_PATH.unlink(missing_ok=True)
        await update.message.reply_text("File tidak terbaca. Kirim file cookies.txt yang valid.")
        return

    await update.message.reply_text(
        "✅ *Cookies berhasil disimpan!*\n\n"
        "Sekarang kamu bisa download YouTube tanpa hambatan.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


# ---------------------------------------------------------------------------
# Shared processing & analysis logic
# ---------------------------------------------------------------------------

async def run_processing_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source_path: Path,
    status_msg: Message,
    download_dir: Path | None = None,
    sticker_msg: Message | None = None,
) -> None:
    context.user_data["processing"] = True
    chat_id = update.effective_chat.id
    work_dir: Path | None = None

    animator = bot_ui.StatusCardAnimator(
        context.bot,
        chat_id=chat_id,
        status_msg=status_msg,
        flow_type="discovery",
    )
    progress = ProgressState(on_update=animator.set_stage)
    animator.start()

    try:
        loop = asyncio.get_event_loop()
        cleanup_active_job(context)
        work_dir = make_job_tmp_dir(source_path)

        # Stage 1 — Transcribe
        animator.set_stage("transcribe", "Transkripsi audio dengan Whisper...")
        transcription = await loop.run_in_executor(
            None, partial(run_transcribe, source_path, work_dir, progress),
        )
        LOGGER.info("Transcription done for %s", source_path)

        # Stage 2 — AI analysis
        # Kunci campaign SEBELUM analisis: snapshot active_job["campaign_id"] baru
        # ada setelah consume_campaign_binding() di bawah, jadi dipakai read-only.
        camp_active = campaign_pending_bound(context)
        camp_rules = campaign_pending_rules_text(context)
        animator.set_stage("analyze", "Menganalisis semua topik & momen menarik dengan AI...")
        moments, video_info = await loop.run_in_executor(
            None,
            partial(
                run_analyze, source_path, transcription, progress, camp_active, camp_rules,
            ),
        )
        LOGGER.info("AI analysis done for %s: %d clip(s) discovered", source_path, len(moments))

        if not moments:
            animator.stop()
            await status_msg.edit_text("Tidak ada momen menarik yang ditemukan di video ini.")
            return

        animator.set_stage("catalog", "Menyiapkan katalog rekomendasi klip viral...")
        await asyncio.sleep(0.5)
        animator.stop()

        # Save active job state (do not delete source/work_dir yet!)
        camp_id, camp_ver = consume_campaign_binding(context)
        context.user_data["active_job"] = {
            "type": "file",
            "source_path": source_path,
            "work_dir": work_dir,
            "user_id": update.effective_user.id,
            "transcription": transcription,
            "video_info": video_info,
            "moments": moments,
            "generated": set(),
            "rendering": False,
            "campaign_id": camp_id,
            "campaign_version": camp_ver,
            "campaign_rules_text": camp_rules,
            "campaign_notes": getattr(moments[0], "campaign_notes", []) if moments else [],
        }

        # Clean up the loading HUD message & sticker message
        await bot_ui.safe_delete_message(context.bot, chat_id, status_msg.message_id)
        if sticker_msg:
            await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)

        catalog_messages = build_clip_catalog_messages(moments, context)
        reply_markup = build_clip_keyboard(len(moments), set())

        for i, msg_text in enumerate(catalog_messages):
            is_last = (i == len(catalog_messages) - 1)
            sent = await update.effective_message.reply_text(
                msg_text,
                parse_mode="Markdown",
                reply_markup=reply_markup if is_last else None,
            )
            if is_last:
                context.user_data["active_job"]["catalog_msg_id"] = sent.message_id

    except (TranscriptionError, AIAnalyzerError) as exc:
        LOGGER.error("Processing failed: %s", exc)
        animator.stop()
        move_to_failed(source_path, str(exc))
        await status_msg.edit_text(f"❌ *Proses gagal*\n\n{exc}", parse_mode="Markdown")
        if work_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:
        LOGGER.error("Unexpected error: %s", exc)
        animator.stop()
        await status_msg.edit_text(f"❌ *Error tak terduga*\n\n{exc}", parse_mode="Markdown")
        if work_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
    finally:
        context.user_data["processing"] = False
        animator.stop()
        if download_dir and download_dir.exists():
            shutil.rmtree(download_dir, ignore_errors=True)


async def run_youtube_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    status_msg: Message,
    sticker_msg: Message | None = None,
) -> None:
    """Transcript-first YouTube flow: analyze text first, download sections on demand."""
    context.user_data["processing"] = True
    chat_id = update.effective_chat.id
    work_dir: Path | None = None

    animator = bot_ui.StatusCardAnimator(
        context.bot,
        chat_id=chat_id,
        status_msg=status_msg,
        flow_type="discovery",
    )
    progress = ProgressState(on_update=animator.set_stage)
    animator.start()

    try:
        loop = asyncio.get_event_loop()
        user_id = update.effective_user.id
        cleanup_active_job(context)
        work_dir = make_job_tmp_dir(Path(f"yt_{int(time.time())}"))

        # Stage 1 — Fetch captions only (no video download)
        animator.set_stage("transcript", "Mengambil transkrip YouTube (tanpa unduh video)...")
        try:
            transcript = await loop.run_in_executor(
                None,
                partial(
                    youtube_flow.fetch_youtube_transcript,
                    url,
                    work_dir,
                    user_id,
                    COOKIE_PATH if has_cookies() else None,
                ),
            )
        except youtube_flow.YoutubeTranscriptError as exc:
            LOGGER.warning("YouTube transcript unavailable: %s", exc)
            animator.stop()
            context.user_data["processing"] = False

            if is_bot_check_error(exc):
                # Clean invalid cookies if present so they don't break subsequent requests
                if COOKIE_PATH.exists():
                    try:
                        COOKIE_PATH.chmod(0o666)
                        COOKIE_PATH.unlink(missing_ok=True)
                    except Exception:
                        pass
                if sticker_msg:
                    await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
                await status_msg.edit_text(
                    "⚠️ *YouTube Mewajibkan Cookies (Anti-Bot)*\n\n"
                    "YouTube memblokir akses ke video ini karena mewajibkan login akun Google.\n"
                    "Cookies yang tersimpan sebelumnya sudah kedaluwarsa.\n\n"
                    "*Cara Mengatasi Cepat (1 Menit):*\n"
                    "1. Buka YouTube di Chrome/Edge PC (pastikan sudah login akun Google).\n"
                    "2. Buka ekstensi *Get cookies.txt LOCALLY* atau *Cookie-Editor*.\n"
                    "3. Klik *Export* dan simpan sebagai file `cookies.txt`.\n"
                    "4. Kirim file `cookies.txt` tersebut langsung ke chat bot ini.\n"
                    "5. Kirim kembali link YouTube Anda setelah cookies tersimpan!",
                    parse_mode="Markdown",
                    reply_markup=YOUTUBE_HELP_MENU,
                )
                return

            animator.set_stage("download_meta", "Transkrip otomatis tidak tersedia. Mengunduh video penuh untuk transkripsi Whisper...")
            download_dir = config.PROJECT_ROOT / "tmp" / "bot_downloads" / str(user_id)
            try:
                video_path = await loop.run_in_executor(
                    None, partial(download_youtube_video, url, download_dir, user_id),
                )
            except Exception as exc_dl:
                clean_msg = strip_ansi(exc_dl)
                animator.stop()
                if sticker_msg:
                    await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
                if is_bot_check_error(exc_dl):
                    if COOKIE_PATH.exists():
                        try:
                            COOKIE_PATH.chmod(0o666)
                            COOKIE_PATH.unlink(missing_ok=True)
                        except Exception:
                            pass
                    await status_msg.edit_text(
                        "⚠️ *YouTube Mewajibkan Cookies (Anti-Bot)*\n\n"
                        "YouTube menolak pengunduhan video ini tanpa verifikasi login.\n\n"
                        "*Cara Mengatasi:*\n"
                        "1. Buka YouTube di browser PC dalam keadaan login.\n"
                        "2. Export cookies menggunakan ekstensi *Get cookies.txt LOCALLY*.\n"
                        "3. Kirim file *cookies.txt* langsung ke chat bot ini.\n"
                        "4. Kirim kembali link video Anda!",
                        parse_mode="Markdown",
                        reply_markup=YOUTUBE_HELP_MENU,
                    )
                else:
                    await status_msg.edit_text(f"❌ Gagal download YouTube: {clean_msg}")
                return
            if not video_path or not video_path.exists():
                animator.stop()
                if sticker_msg:
                    await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
                await status_msg.edit_text("❌ Tidak bisa mendownload video dari link tersebut.")
                return

            animator.stop()
            source_path = config.INPUT_DIR / f"yt_{int(time.time())}_{video_path.stem[:50]}{video_path.suffix}"
            shutil.copy2(video_path, source_path)
            await run_processing_flow(update, context, source_path, status_msg, download_dir, sticker_msg=sticker_msg)
            return

        LOGGER.info("YouTube transcript ready: %d segments, %.0fs", len(transcript.segments), transcript.duration)

        # Stage 2 — AI analysis on the transcript
        # (sama seperti run_processing_flow: kunci campaign dibaca lebih dulu,
        #  belum dikonsumsi, agar analyzer tidak menyuntik hashtag generik)
        camp_active = campaign_pending_bound(context)
        camp_rules = campaign_pending_rules_text(context)
        animator.set_stage("analyze", "Menganalisis semua topik & konteks menarik dengan AI...")
        moments = await loop.run_in_executor(
            None,
            partial(run_youtube_analyze, transcript, progress, camp_active, camp_rules),
        )
        if not moments:
            animator.stop()
            if sticker_msg:
                await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
            await status_msg.edit_text(
                "Tidak ada momen menarik yang ditemukan di video ini."
            )
            return
        LOGGER.info("AI selected %d distinct moment(s) from YouTube transcript", len(moments))

        animator.set_stage("catalog", "Menyiapkan katalog rekomendasi klip viral...")
        await asyncio.sleep(0.5)
        animator.stop()

        # Save active job state (keep work_dir & transcript alive for on-demand generation)
        camp_id, camp_ver = consume_campaign_binding(context)
        context.user_data["active_job"] = {
            "type": "youtube",
            "url": url,
            "work_dir": work_dir,
            "user_id": user_id,
            "transcript": transcript,
            "moments": moments,
            "generated": set(),
            "rendering": False,
            "campaign_id": camp_id,
            "campaign_version": camp_ver,
            "campaign_rules_text": camp_rules,
            "campaign_notes": getattr(moments[0], "campaign_notes", []) if moments else [],
        }

        # Clean up the loading HUD message & sticker message
        await bot_ui.safe_delete_message(context.bot, chat_id, status_msg.message_id)
        if sticker_msg:
            await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)

        catalog_messages = build_clip_catalog_messages(moments, context)
        reply_markup = build_clip_keyboard(len(moments), set())

        for i, msg_text in enumerate(catalog_messages):
            is_last = (i == len(catalog_messages) - 1)
            sent = await update.effective_message.reply_text(
                msg_text,
                parse_mode="Markdown",
                reply_markup=reply_markup if is_last else None,
            )
            if is_last:
                context.user_data["active_job"]["catalog_msg_id"] = sent.message_id

    except (AIAnalyzerError, youtube_flow.YoutubeDownloadError) as exc:
        LOGGER.error("YouTube flow failed: %s", exc)
        animator.stop()
        if sticker_msg:
            await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
        await status_msg.edit_text(f"❌ *Proses gagal*\n\n{exc}", parse_mode="Markdown")
        if work_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:
        LOGGER.error("Unexpected YouTube flow error: %s", exc)
        animator.stop()
        if sticker_msg:
            await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
        await status_msg.edit_text(f"❌ *Error tak terduga*\n\n{exc}", parse_mode="Markdown")
        if work_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
    finally:
        context.user_data["processing"] = False
        animator.stop()


async def run_gdrive_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    status_msg: Message,
    sticker_msg: Message | None = None,
) -> None:
    """Download video from Google Drive and hand over to run_processing_flow."""
    context.user_data["processing"] = True
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    download_dir = config.PROJECT_ROOT / "tmp" / "bot_downloads" / f"gdrive_{user_id}_{int(time.time())}"

    animator = bot_ui.StatusCardAnimator(
        context.bot,
        chat_id=chat_id,
        status_msg=status_msg,
        flow_type="discovery",
    )
    progress = ProgressState(on_update=animator.set_stage)
    animator.start()

    try:
        loop = asyncio.get_event_loop()
        animator.set_stage("download", "Menghubungkan ke server Google Drive...")

        try:
            downloaded_file = await loop.run_in_executor(
                None,
                partial(
                    gdrive_flow.download_gdrive_video,
                    url,
                    download_dir,
                    user_id,
                    progress,
                ),
            )
        except gdrive_flow.GDrivePermissionError as exc:
            LOGGER.warning("GDrive permission error: %s", exc)
            animator.stop()
            if sticker_msg:
                await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
            await status_msg.edit_text(
                "🔒 *Akses Google Drive Ditolak (Privat)*\n\n"
                "File video tidak dapat diunduh karena pengaturannya masih bersifat privat / dibatasi.\n\n"
                "*Cara Mengatasinya (10 Detik):*\n"
                "1. Buka file di Google Drive Anda.\n"
                "2. Klik tombol *Bagikan* (Share) di pojok kanan atas.\n"
                "3. Pada *Akses Umum*, ubah dari *Dibatasi* ke *Siapa saja yang memiliki link* (*Anyone with the link*).\n"
                "4. Salin link dan kirimkan kembali ke sini!",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return
        except gdrive_flow.GDriveQuotaError as exc:
            LOGGER.warning("GDrive quota error: %s", exc)
            animator.stop()
            if sticker_msg:
                await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
            await status_msg.edit_text(
                "⚠️ *Batas Kuota Google Drive Terlampaui*\n\n"
                "Google Drive membatasi unduhan file ini untuk sementara waktu karena terlalu banyak diakses dalam waktu singkat.\n"
                "Silakan coba lagi nanti atau upload video langsung sebagai file ke bot.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return
        except gdrive_flow.GDriveInvalidFileError as exc:
            LOGGER.warning("GDrive invalid file error: %s", exc)
            animator.stop()
            if sticker_msg:
                await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
            await status_msg.edit_text(
                f"❌ *Bukan File Video yang Valid*\n\n{exc}",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return
        except Exception as exc:
            clean_msg = strip_ansi(exc)
            LOGGER.error("GDrive download error: %s", exc)
            animator.stop()
            if sticker_msg:
                await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
            await status_msg.edit_text(
                f"❌ *Gagal Mengunduh dari Google Drive*\n\n{clean_msg}",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return

        if not downloaded_file or not downloaded_file.exists():
            animator.stop()
            if sticker_msg:
                await bot_ui.safe_delete_message(context.bot, chat_id, sticker_msg.message_id)
            await status_msg.edit_text("❌ File hasil unduhan Google Drive tidak ditemukan.")
            return

        # Move downloaded file to config.INPUT_DIR so run_processing_flow handles it consistently
        ts = int(time.time())
        file_id = gdrive_flow.extract_gdrive_id(url) or "vid"
        source_path = config.INPUT_DIR / f"gdrive_{ts}_{file_id[:16]}{downloaded_file.suffix}"
        shutil.copy2(downloaded_file, source_path)

        animator.stop()
        await run_processing_flow(
            update,
            context,
            source_path,
            status_msg,
            download_dir=download_dir,
            sticker_msg=sticker_msg,
        )
    finally:
        pass


async def generate_single_clip_flow(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    idx: int,
    catalog_message: Message | None = None,
) -> bool:
    """Renders and sends a single chosen clip. Returns True if successful."""
    job = context.user_data.get("active_job")
    if not job:
        return False

    moments = job.get("moments", [])
    if not moments:
        return False
    if idx < 1 or idx > len(moments):
        return False

    moment = moments[idx - 1]
    work_dir = job["work_dir"]
    user_id = job["user_id"]
    loop = asyncio.get_event_loop()
    dur = moment.end - moment.start

    # 1. Send animated rocket sticker
    render_sticker_msg = await bot_ui.safe_send_animated_sticker(context.bot, chat_id, "rocket")

    # 2. Send initial status message & attach StatusCardAnimator
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🎬 *Mempersiapkan render Klip #{idx}...*",
        parse_mode="Markdown",
    )

    animator = bot_ui.StatusCardAnimator(
        context.bot,
        chat_id=chat_id,
        status_msg=status_msg,
        flow_type="render",
        clip_idx=idx,
        hook=moment.hook,
        duration=dur,
    )
    progress = ProgressState(on_update=animator.set_stage)
    animator.set_stage("init", "Mempersiapkan render...")
    animator.start()

    user_tpl = get_user_hook_template(context)
    try:
        if job["type"] == "youtube":
            camp_rules = job.get("campaign_rules_text") or ""
            rule_min, _ = extract_duration_bounds(camp_rules)
            target_min_dur = rule_min if rule_min > config.MIN_CLIP_DURATION_S else (moment.end - moment.start)
            clip_path = await loop.run_in_executor(
                None,
                partial(
                    run_youtube_render_single,
                    job["url"],
                    job["transcript"],
                    moment,
                    idx,
                    work_dir,
                    user_id,
                    progress,
                    user_tpl,
                    min_clip_duration=target_min_dur,
                ),
            )
        else:
            clip_path = await loop.run_in_executor(
                None,
                partial(
                    run_file_render_single,
                    job["source_path"],
                    job["transcription"],
                    moment,
                    idx,
                    work_dir,
                    job["video_info"],
                    progress,
                    user_tpl,
                ),
            )

        animator.set_stage("upload", "Mengunggah video klip ke Telegram...")
        await asyncio.sleep(0.5)

        caption_full = bot_ui.format_delivery_caption(idx, moment)
        # Nasehat kepatuhan caption (kelas "post" — tidak memblokir, tidak auto-fix).
        caption_notes = campaign_caption_note(job, moment)
        if caption_notes and len(caption_full) + len(caption_notes) + 2 <= 1024:
            caption_full = f"{caption_full}\n\n{caption_notes}"
            caption_notes = ""

        with open(clip_path, "rb") as f:
            try:
                if len(caption_full) <= 1024:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=caption_full,
                        parse_mode="Markdown",
                        write_timeout=300,
                        read_timeout=300,
                    )
                else:
                    short_cap = f"🎬 *Klip #{idx}:* {escape_tg_md(moment.hook)}\n📌 {escape_tg_md(moment.topic)} ({int(dur)}s)"
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=short_cap,
                        parse_mode="Markdown",
                        write_timeout=300,
                        read_timeout=300,
                    )
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"📝 *Caption Siap Copy untuk Klip #{idx}:*\n\n{moment.caption}",
                    )
            except Exception as send_err:
                LOGGER.warning("Markdown send failed (%s), falling back to plain text", send_err)
                f.seek(0)
                plain_cap = f"🎬 Klip #{idx}: {moment.hook}\n\n📌 Topik: {moment.topic}\n⏱ Durasi: {int(dur)}s\n\n📝 Caption Medsos:\n{moment.caption}"
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=plain_cap[:1024],
                    write_timeout=300,
                    read_timeout=300,
                )

        if caption_notes:
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=caption_notes, parse_mode="Markdown",
                )
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=caption_notes, parse_mode=None)

        clip_path.unlink(missing_ok=True)
        job["generated"].add(idx)

        # Stop animator and clean up temporary render messages
        animator.stop()
        await bot_ui.safe_delete_message(context.bot, chat_id, status_msg.message_id)
        if render_sticker_msg:
            await bot_ui.safe_delete_message(context.bot, chat_id, render_sticker_msg.message_id)

        # Update keyboard on catalog message
        catalog_msg_id = job.get("catalog_msg_id") or (catalog_message.message_id if catalog_message else None)
        if catalog_msg_id:
            try:
                new_kb = build_clip_keyboard(len(moments), job["generated"])
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=catalog_msg_id,
                    reply_markup=new_kb,
                )
            except Exception:
                pass

        # Send celebration animated sticker & delivery message
        await bot_ui.safe_send_animated_sticker(context.bot, chat_id, "party")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ *Klip #{idx} Berhasil Dibuat & Dikirim!*\n\n"
                f"📐 *Format:* 1080×1920 (9:16 Vertikal)\n"
                f"🎯 *Hook:* \"_{moment.hook.replace('*', '')}_\"\n\n"
                "Pilihan konteks klip lainnya tetap aktif di atas 👆. Kamu bisa langsung memilih klip berikutnya!"
            ),
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return True

    except Exception as exc:
        LOGGER.exception("Failed to render clip %s: %s", idx, exc)
        animator.stop()
        if render_sticker_msg:
            await bot_ui.safe_delete_message(context.bot, chat_id, render_sticker_msg.message_id)
        await status_msg.edit_text(f"❌ *Gagal merender Klip #{idx}:* {exc}", parse_mode="Markdown")
        return False
    finally:
        animator.stop()


# ---------------------------------------------------------------------------
# Menu & Message handlers
# ---------------------------------------------------------------------------

async def show_active_clips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.user_data.get("active_job")
    if not job or not job.get("moments"):
        await update.message.reply_text(
            "ℹ️ Belum ada video aktif yang sedang dikurasi.\n\n"
            "Silakan kirimkan *file video* atau *link YouTube* terlebih dahulu!",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    moments = job["moments"]
    generated = job.get("generated", set())
    catalog_messages = build_clip_catalog_messages(moments, context)
    reply_markup = build_clip_keyboard(len(moments), generated)

    for i, msg_text in enumerate(catalog_messages):
        is_last = (i == len(catalog_messages) - 1)
        sent = await update.message.reply_text(
            msg_text,
            parse_mode="Markdown",
            reply_markup=reply_markup if is_last else None,
        )
        if is_last:
            job["catalog_msg_id"] = sent.message_id


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if text == "Proses Video":
        await update.message.reply_text(
            "🎬 Kirim *file video* (.mp4/.mov/.mkv),\n"
            "*link YouTube*, atau *link Google Drive* untuk diproses.\n\n"
            "AI akan mencari semua konteks menarik & kamu bebas memilih klip mana yang ingin digenerate!",
            parse_mode="Markdown",
            reply_markup=BACK_MENU,
        )
    elif text == "Pilihan Klip":
        await show_active_clips(update, context)
    elif text == "🎨 Style & Hook":
        await style_command(update, context)
    elif text == "Bantuan":
        await help_command(update, context)
    elif text == "Tentang Bot":
        await about(update, context)
    elif text in ("Cookies YouTube", "Cara Export Cookies"):
        await cookies_command(update, context)
    elif text == "Kembali ke Menu":
        await start(update, context)
    else:
        await update.message.reply_text(
            "Pilih menu di keyboard atau kirim video/link.",
            reply_markup=MAIN_MENU,
        )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    if is_processing(context):
        await update.message.reply_text(
            "⏳ Masih ada proses yang sedang berjalan. Tunggu selesai dulu ya!",
            reply_markup=MAIN_MENU,
        )
        return

    if not try_claim_lock():
        await update.message.reply_text(
            "⏳ *Bot sedang memproses video dari user lain.*\n\n"
            "Silakan tunggu sebentar, kamu akan dapat giliran setelah proses sebelumnya selesai.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    try:
        attachment = update.effective_message.effective_attachment
        file_id = getattr(attachment, "file_id", "") or ""
        if await campaign_gate(update, context, "video", file_id):
            return
        await bot_ui.safe_set_reaction(context.bot, update.effective_chat.id, update.effective_message.message_id, "🎬")
        sticker_msg = await bot_ui.safe_send_animated_sticker(context.bot, update.effective_chat.id, "robot")
        status_msg = await update.message.reply_text("📥 *Menerima video...*", parse_mode="Markdown")

        ts = int(time.time())
        file_path = config.INPUT_DIR / f"bot_{ts}_{update.effective_message.message_id}.mp4"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            file = await update.effective_message.effective_attachment.get_file()
            await file.download_to_drive(custom_path=str(file_path))
        except Exception as exc:
            await status_msg.edit_text(f"❌ Gagal download video: {exc}")
            if sticker_msg:
                await bot_ui.safe_delete_message(context.bot, update.effective_chat.id, sticker_msg.message_id)
            return

        await run_processing_flow(update, context, file_path, status_msg, sticker_msg=sticker_msg)
    finally:
        release_lock()


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document uploads — detect cookies.txt files."""
    doc = update.effective_message.document
    if not doc:
        return

    file_name = (doc.file_name or "").lower()
    if "cookies" in file_name and file_name.endswith(".txt"):
        await handle_cookies_file(update, context)
        return

    await update.message.reply_text(
        "Kirim file *cookies.txt* untuk autentikasi YouTube.\n"
        "Atau kirim video sebagai file untuk diproses.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    text = update.message.text.strip()

    # Menu buttons
    MENU_TEXTS = ("Proses Video", "Pilihan Klip", "🎨 Style & Hook", "Bantuan", "Tentang Bot", "Cookies YouTube", "Cara Export Cookies", "Kembali ke Menu")
    if text in MENU_TEXTS:
        await menu_handler(update, context)
        return

    # Intersep /campaign import (langkah 3): SEMUA pesan teks bebas berikutnya
    # dianggap bahan S&K selama state aktif — dieksekusi sebelum deteksi
    # url/file/flow lain dan sebelum klaim lock apa pun (lock global tidak
    # pernah diklaim jalur ini). Link video = batalkan impor lalu proses normal.
    if campaign_import_state(context) is not None:
        await campaign_import_handle_text(update, context, text)
        return

    # 1. Folder Google Drive check
    if gdrive_flow.is_gdrive_folder_url(text):
        await update.message.reply_text(
            "📁 *Link Folder Google Drive Terdeteksi*\n\n"
            "Bot membutuhkan *link langsung ke file video*, bukan link folder.\n\n"
            "*Cara mengambil link video:*\n"
            "1. Buka folder di Google Drive Anda.\n"
            "2. Klik kanan (atau tap titik tiga) pada *file video* yang ingin diproses.\n"
            "3. Pilih *Bagikan (Share)* → ubah ke *Siapa saja yang memiliki link* (*Anyone with the link*).\n"
            "4. Salin link dan kirimkan ke sini!",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    is_gdrive = gdrive_flow.is_gdrive_url(text)
    is_yt = is_youtube_url(text)

    # Intersep input aturan campaign kustom (bebas)
    awaiting_custom = context.user_data.get("awaiting_custom_rules")
    job = context.user_data.get("active_job")
    has_awaiting_job = isinstance(job, dict) and "awaiting_campaign" in job

    if (awaiting_custom or has_awaiting_job) and not is_yt and not is_gdrive:
        context.user_data.pop("awaiting_custom_rules", None)
        rules_text = text.strip()
        context.user_data["pending_campaign"] = "custom"
        context.user_data["pending_campaign_rules"] = rules_text

        if has_awaiting_job:
            awaiting = job.pop("awaiting_campaign")
            if not try_claim_lock():
                job["awaiting_campaign"] = awaiting
                await update.message.reply_text(
                    "⏳ Bot sedang memproses video user lain. Kirim ulang aturanmu sebentar lagi.",
                )
                return
            try:
                await update.message.reply_text(
                    "🎯 *Aturan campaign kustom diterima!*\n\n"
                    "🤖 AI OriontClipper akan menalar apa yang boleh & dilarang dalam video.\n"
                    "📋 Syarat akun (seperti jumlah follower) akan dicatat sebagai pengingat.\n"
                    "🚀 *Memulai pemrosesan video...*",
                    parse_mode="Markdown",
                )
                await resume_job_from_callback(update, context, awaiting)
            finally:
                release_lock()
            return
        else:
            await update.message.reply_text(
                "🎯 *Aturan campaign kustom berhasil disimpan!*\n\n"
                "Aturan ini akan dipakai saat kamu mengirim video berikutnya.\n"
                "Ketik /campaign show untuk melihat status aturan kapan saja.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return

    if not is_yt and not is_gdrive:
        await update.message.reply_text(
            "Kirimkan link YouTube, link Google Drive, atau file video.\n"
            "Tekan /help untuk panduan.",
            reply_markup=MAIN_MENU,
        )
        return

    if is_processing(context):
        await update.message.reply_text(
            "⏳ Masih ada proses yang sedang berjalan. Tunggu selesai dulu ya!",
            reply_markup=MAIN_MENU,
        )
        return

    if not try_claim_lock():
        await update.message.reply_text(
            "⏳ *Bot sedang memproses video dari user lain.*\n\n"
            "Silakan tunggu sebentar, kamu akan dapat giliran setelah proses sebelumnya selesai.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    if is_gdrive:
        try:
            if await campaign_gate(update, context, "gdrive", text):
                return
            await bot_ui.safe_set_reaction(context.bot, update.effective_chat.id, update.effective_message.message_id, "⚡")
            sticker_msg = await bot_ui.safe_send_animated_sticker(context.bot, update.effective_chat.id, "robot")
            status_msg = await update.message.reply_text(
                "🚀 *Mempersiapkan pengunduhan Google Drive...*", parse_mode="Markdown",
            )
            await run_gdrive_flow(update, context, text, status_msg, sticker_msg=sticker_msg)
        finally:
            release_lock()
        return

    try:
        if not has_cookies():
            await update.message.reply_text(
                "⚠️ *YouTube mewajibkan cookies*\n\n"
                "Untuk download video YouTube, silakan upload file cookies.txt dari browser.\n\n"
                "Tekan menu *Cookies YouTube* untuk panduan.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return

        if await campaign_gate(update, context, "youtube", text):
            return

        await bot_ui.safe_set_reaction(context.bot, update.effective_chat.id, update.effective_message.message_id, "⚡")
        sticker_msg = await bot_ui.safe_send_animated_sticker(context.bot, update.effective_chat.id, "brain")
        status_msg = await update.message.reply_text(
            "🚀 *Mempersiapkan analisis YouTube...*", parse_mode="Markdown",
        )

        await run_youtube_flow(update, context, text, status_msg, sticker_msg=sticker_msg)
    finally:
        release_lock()


# ---------------------------------------------------------------------------
# Callback Query Handler
# ---------------------------------------------------------------------------

async def clip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    chat_id = query.message.chat_id

    if data in ("clip:done", "done"):
        cleanup_active_job(context)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🗑️ *Sesi selesai!*\n\n"
                "Semua file sementara telah dibersihkan. Siap untuk video atau link YouTube berikutnya!",
            ),
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    job = context.user_data.get("active_job")
    if not job or not job.get("moments"):
        try:
            await query.answer("Sesi klip tidak aktif.", show_alert=True)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Pilih campaign untuk video yang baru kamu kirim lewat tombol di atas ya!\n"
                "(Kirim /done untuk membatalkan.)"
                if job.get("awaiting_campaign")
                else "ℹ️ Sesi klip tidak ditemukan atau sudah selesai. Kirim video/link baru untuk mulai!"
            ),
            reply_markup=MAIN_MENU,
        )
        return

    if data.startswith("clip:info:"):
        idx = int(data.split(":")[-1])
        await query.answer(f"Klip #{idx} sudah selesai digenerate!", show_alert=True)
        return

    if job.get("rendering"):
        await query.answer("Sedang ada klip yang sedang dirender. Tunggu sebentar ya!", show_alert=True)
        return

    if data.startswith("clip:nope:"):
        idx = int(data.split(":")[-1])
        campaign_ack_set(job).discard(idx)
        return

    if data.startswith("clip:gen:"):
        idx = int(data.split(":")[-1])
        if idx in job.get("generated", set()):
            await query.answer(f"Klip #{idx} sudah pernah digenerate!", show_alert=True)
            return

        # Konfirmasi inline untuk klip yang MELANGGAR aturan campaign (fail).
        fail_note = campaign_fail_warning_text(job, idx)
        if fail_note and not campaign_is_acked(job, idx):
            campaign_mark_ack(job, idx)
            confirm_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Ya, render", callback_data=f"clip:gen:{idx}"),
                InlineKeyboardButton("❌ Batal", callback_data=f"clip:nope:{idx}"),
            ]])
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🚫 *Klip ini melanggar aturan campaign!*\n\n"
                    f"{fail_note}\n\n"
                    "Kamu tetap bisa mem-render — *render satuan atas tanggung jawabmu.*\n"
                    "Lanjutkan?"
                ),
                parse_mode="Markdown",
                reply_markup=confirm_kb,
            )
            return

        if fail_note:
            # User sudah menekan "✅ Ya, render": keputusan manusia dicatat.
            campaign_record_override(
                job, chat_id, idx, campaign_clip_fail_codes(job, idx), "confirm_fail_override",
            )

        if not try_claim_lock():
            await query.answer("Bot sedang memproses video lain, silakan coba lagi sesaat lagi.", show_alert=True)
            return

        job["rendering"] = True
        try:
            await generate_single_clip_flow(context, chat_id, idx, query.message)
        finally:
            job["rendering"] = False
            release_lock()

    elif data == "clip:all":
        unrendered = [
            i for i in range(1, len(job["moments"]) + 1)
            if i not in job.get("generated", set())
        ]
        if not unrendered:
            await query.answer("Semua klip sudah digenerate!", show_alert=True)
            return

        # Gerbang manusia campaign: JANGAN render dulu sebelum semua gate diakui.
        try:
            gates = campaign_pending_gates(job, unrendered)
        except Exception as exc:  # pragma: no cover - fitur tidak boleh menghentikan klip
            LOGGER.warning("cek gerbang generate-semua gagal: %s", exc)
            gates = []
        if gates:
            await campaign_send_gate_confirm(context, chat_id, job, gates)
            return

        await run_generate_all_flow(context, chat_id, unrendered, query.message)


async def run_generate_all_flow(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    unrendered: list[int],
    catalog_message: Message | None = None,
) -> None:
    """Render semua klip terpilih secara berurutan (satu lock, satu batch)."""
    job = context.user_data.get("active_job")
    if not isinstance(job, dict) or not unrendered:
        return

    if not try_claim_lock():
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Bot sedang memproses video lain, silakan coba lagi sesaat lagi.",
        )
        return

    job["rendering"] = True
    batch_fail_note = ""
    gate_codes_by_clip: dict[int, list[str]] = {}
    if campaign_job_active(job):
        verdicts_all = ensure_campaign_verdicts(job)
        if verdicts_all:
            n_fail = sum(
                1 for i in unrendered
                if i <= len(verdicts_all)
                and any(getattr(v, "level", "") == "fail" for v in (verdicts_all[i - 1] or []))
            )
            if n_fail:
                batch_fail_note = (
                    f"\n⚠️ Termasuk {n_fail} klip ❌ yang melanggar aturan campaign — "
                    "render atas tanggung jawabmu."
                )
        # Satu perhitungan gate untuk seluruh batch (hindari baca profil per klip).
        try:
            profile = campaign_get_profile(str(job.get("campaign_id")))
            if profile is not None:
                for entry in campaign_gate_map(job, unrendered, profile):
                    for i in entry.get("clips") or []:
                        gate_codes_by_clip.setdefault(int(i), []).append(str(entry["code"]))
        except Exception as exc:  # pragma: no cover - audit tak boleh menghentikan render
            LOGGER.warning("peta gate batch gagal: %s", exc)
            gate_codes_by_clip = {}
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✨ Memulai render {len(unrendered)} klip sekaligus secara berurutan..."
                f"{batch_fail_note}"
            ),
            parse_mode="Markdown",
        )
        for idx in unrendered:
            # Jejak audit: klip yang tadi tertahan gerbang manusia, kini dirender
            # atas konfirmasi "aku mengerti".
            codes = gate_codes_by_clip.get(idx) or []
            if codes:
                campaign_record_override(job, chat_id, idx, codes, "ack_generate_all")
            success = await generate_single_clip_flow(context, chat_id, idx, catalog_message)
            if not success:
                break
    finally:
        job["rendering"] = False
        release_lock()


async def show_active_clips_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_active_clips(update, context)


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cleanup_active_job(context)
    campaign_import_clear(context)  # /done = sesi benar-benar bersih
    await update.message.reply_text(
        "🗑️ *Sesi selesai!*\n\nSemua file sementara telah dibersihkan. Siap untuk video baru!",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diset di .env")

    ensure_directories()
    handlers: list[logging.Handler] = [
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    (config.PROJECT_ROOT / "tmp" / "bot_downloads").mkdir(parents=True, exist_ok=True)

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).request(request).build()

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.warning("Telegram network or API event: %s", context.error)

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cookies", cookies_command))
    app.add_handler(CommandHandler("klip", show_active_clips_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("style", style_command))
    app.add_handler(CommandHandler("setstyle", setstyle_command))
    app.add_handler(CommandHandler("setpos", setpos_command))
    app.add_handler(CommandHandler("setcolor", setcolor_command))
    app.add_handler(CommandHandler("edithook", edithook_command))
    app.add_handler(CommandHandler("previewhook", previewhook_command))
    app.add_handler(CommandHandler("campaign", campaign_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(style_callback, pattern="^style:"))
    app.add_handler(CallbackQueryHandler(clip_callback, pattern="^(clip:|done$)"))
    app.add_handler(CallbackQueryHandler(campaign_callback, pattern="^camp:"))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    LOGGER.info("ContentClipper Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
