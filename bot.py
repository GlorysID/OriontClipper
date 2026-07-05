"""ContentClipper Telegram Bot — clear UX flow with stage tracking."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
import time
from functools import partial
from pathlib import Path

import yt_dlp
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

import config
from modules.ai_analyzer import AIAnalyzer, AIAnalyzerError
from modules.file_manager import (
    ensure_directories,
    make_job_tmp_dir,
    move_to_failed,
    move_to_processed,
)
from modules.transcriber import TranscriptionError, WhisperTranscriber
from modules.video_editor import VideoEditError, VideoEditor, probe_video
from modules import template as tpl
from modules import web_editor


LOGGER = logging.getLogger("contentclipper.bot")

YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be|m\.youtube\.com)",
    re.IGNORECASE,
)

COOKIE_PATH = config.PROJECT_ROOT / "tmp" / "cookies_shared.txt"

# Global lock — only 1 video at a time (2-core VPS)
_PROCESSING_LOCK = threading.Lock()

def try_claim_lock() -> bool:
    """Atomically check and claim the global processing lock. True = claimed."""
    return _PROCESSING_LOCK.acquire(blocking=False)

def release_lock() -> None:
    _PROCESSING_LOCK.release()

_EDITOR_SERVER: web_editor.EditorServer | None = None
_PUBLIC_URL_FILE = config.PROJECT_ROOT / ".public_url"


def _read_public_url() -> str:
    try:
        return _PUBLIC_URL_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


def _web_editor_url(user_id: int) -> str:
    public = _read_public_url()
    if _EDITOR_SERVER:
        _EDITOR_SERVER.set_last_user(user_id)
    if public:
        return f"{public}/editor?user_id={user_id}"
    return f"http://localhost:{web_editor.PORT}/editor?user_id={user_id}"

# ---------------------------------------------------------------------------
# Keyboard menus
# ---------------------------------------------------------------------------

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Proses Video"), KeyboardButton("Bantuan")],
        [KeyboardButton("Template"), KeyboardButton("Cookies YouTube")],
        [KeyboardButton("Tentang Bot")],
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
    return context.user_data.get("processing", False)


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

    cookie_tmp = COOKIE_PATH.parent / f"cookies_ytdlp_{os.getpid()}.txt"
    if COOKIE_PATH.exists():
        shutil.copy2(COOKIE_PATH, cookie_tmp)
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
    __slots__ = ("stage", "detail")
    STAGE_EMOJI = {
        "transcribe": "",
        "analyze": "",
        "render": "",
        "upload": "",
    }

    def __init__(self) -> None:
        self.stage: str = ""
        self.detail: str = ""

    def set(self, stage: str, detail: str) -> None:
        self.stage = stage
        self.detail = detail

    def format(self, elapsed: float) -> str:
        emoji = self.STAGE_EMOJI.get(self.stage, "")
        return f"{emoji} {self.detail}\n {int(elapsed)}s"


def run_transcribe(source_path: Path, work_dir: Path, progress: ProgressState):
    """Transcribe audio — call in executor."""
    progress.set("transcribe", "Transkripsi audio dengan Whisper... (~10-15 mnt)")
    transcriber = WhisperTranscriber(logger=LOGGER)
    return transcriber.transcribe_video(source_path, work_dir)


def run_analyze(source_path: Path, transcription, progress: ProgressState):
    """AI analysis — call in executor. Returns (moments, video_info)."""
    progress.set("analyze", "Menganalisis momen menarik dengan AI...")
    video_info = probe_video(source_path)
    analyzer = AIAnalyzer(logger=LOGGER)
    moments = analyzer.analyze_segments(transcription.segments, video_info.duration)
    return moments, video_info


def run_render(source_path: Path, transcription, moments, work_dir, video_info, progress: ProgressState, user_template=None):
    """Render clips — call in executor."""
    segment_list = list(transcription.segments)
    editor = VideoEditor(logger=LOGGER)
    total = len(moments)
    output_paths: list[Path] = []
    for idx, moment in enumerate(moments, start=1):
        progress.set("render", f"Merender klip {idx}/{total}...")
        try:
            output_paths.append(
                editor.render_clip(source_path, segment_list, moment, idx, work_dir, video_info,
                                   user_template=user_template)
            )
        except VideoEditError:
            LOGGER.exception("Clip %s failed and will be skipped", idx)
            continue
    if not output_paths:
        raise VideoEditError("No clips rendered successfully")
    move_to_processed(source_path)
    LOGGER.info("Completed %s -> %s clip(s)", source_path, len(output_paths))
    return output_paths


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Maaf, Anda tidak memiliki akses ke bot ini.")
        return

    cookies_status = "" if has_cookies() else ""
    await update.message.reply_text(
        " *MensuraAIClip*\n\n"
        "Saya mengubah video *panjang* (YouTube / file) menjadi\n"
        "3 klip *vertikal 9:16* dengan subtitle & hook AI.\n\n"
        f"Cookies YouTube: {cookies_status}\n\n"
        "Pilih menu di bawah atau kirim video/link langsung!\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        " IG: @jalipryyy",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
       "*MensuraAIClip — Bantuan*\n\n"
        "▸ *Kirim video* — file .mp4/.mov/.mkv langsung ke chat\n"
        "▸ *Kirim link YouTube* — bot akan download otomatis\n"
        "▸ */cookies* — upload cookies.txt untuk bypass blokade YouTube\n\n"
        "Proses:\n"
        " 1. Transkripsi audio (Whisper, ~10-15 mnt)\n"
        " 2. Analisis momen menarik (AI)\n"
        " 3. Render 3 klip vertikal 9:16\n\n"
        "Klik *Done* setelah selesai untuk hapus semua klip.\n\n"
        " Total: ~20-30 menit | Kirim 1 video dalam satu waktu",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*MensuraAIClip*\n\n"
        "Versi 1.0\n"
        "- Transkripsi: OpenAI Whisper (small, CPU)\n"
        "- Analisis AI: 9Router (LLM)\n"
        "- Render: FFmpeg + Pillow\n"
        "- Output: 1080×1920, word-level subtitle, hook overlay\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        " IG: @jalipryyy",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    has = has_cookies()
    status = "*Cookies terpasang*" if has else "*Belum ada cookies*"

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
        "_Cookies dipakai bersama untuk semua user (akun cadangan)._",
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

    try:
        file = await update.effective_message.effective_attachment.get_file()
        await file.download_to_drive(custom_path=str(COOKIE_PATH))
    except Exception as exc:
        await update.message.reply_text(f"Gagal menyimpan cookies: {exc}")
        return

    # yt-dlp uses a temp copy of this file, so the original stays pristine
    COOKIE_PATH.chmod(0o600)

    # Validate — must contain at least one youtube.com cookie line
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
        "*Cookies berhasil disimpan (shared)!*\n\n"
        "Semua user sekarang bisa download YouTube tanpa perlu upload cookies sendiri.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


# ---------------------------------------------------------------------------
# Shared processing logic
# ---------------------------------------------------------------------------

async def run_processing_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source_path: Path,
    status_msg: Message,
    download_dir: Path | None = None,
) -> None:
    context.user_data["processing"] = True
    t0 = time.time()
    progress = ProgressState()
    updater: asyncio.Task | None = None

    async def progress_updater():
        while context.user_data.get("processing"):
            try:
                await status_msg.edit_text(progress.format(time.time() - t0))
            except Exception:
                pass
            await asyncio.sleep(5)

    try:
        loop = asyncio.get_event_loop()
        work_dir = make_job_tmp_dir(source_path)
        user_template = tpl.get_user_template(update.effective_user.id)

        updater = asyncio.create_task(progress_updater())

        # Stage 1 — Transcribe
        transcription = await loop.run_in_executor(
            None, partial(run_transcribe, source_path, work_dir, progress),
        )
        LOGGER.info("Transcription done for %s", source_path)

        # Stage 2 — AI analysis
        moments, video_info = await loop.run_in_executor(
            None, partial(run_analyze, source_path, transcription, progress),
        )
        LOGGER.info("AI analysis done for %s", source_path)

        # Stage 3 — Render
        output_paths = await loop.run_in_executor(
            None, partial(run_render, source_path, transcription, moments, work_dir, video_info, progress,
                          user_template=user_template),
        )
        LOGGER.info("Render done for %s", source_path)

        updater.cancel()

        progress.set("upload", f"Mengirim {len(output_paths)} klip...")
        await status_msg.edit_text(progress.format(time.time() - t0))

        clip_ids: list[int] = []
        for clip_path in output_paths:
            try:
                with open(clip_path, "rb") as f:
                    sent = await update.message.reply_video(
                        video=f,
                        caption=f" *{clip_path.stem}*",
                        parse_mode="Markdown",
                        write_timeout=300,
                        read_timeout=300,
                    )
                    clip_ids.append(sent.message_id)
            except Exception as exc:
                await status_msg.edit_text(f"Gagal kirim {clip_path.name}: {exc}")
                return
            finally:
                clip_path.unlink(missing_ok=True)

        context.user_data["clip_ids"] = clip_ids
        total = time.time() - t0

        keyboard = [[InlineKeyboardButton("Done ", callback_data="done")]]
        await status_msg.edit_text(
            f" *Selesai dalam {int(total // 60)}m{int(total % 60)}s*\n\n"
            f"3 klip vertikal siap. Klik *Done* untuk hapus.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except (TranscriptionError, AIAnalyzerError, VideoEditError) as exc:
        LOGGER.error("Processing failed: %s", exc)
        move_to_failed(source_path, str(exc))
        await status_msg.edit_text(f"*Proses gagal*\n\n{exc}", parse_mode="Markdown")
    except Exception as exc:
        LOGGER.error("Unexpected error: %s", exc)
        await status_msg.edit_text(f" *Error tak terduga*\n\n{exc}", parse_mode="Markdown")
    finally:
        context.user_data["processing"] = False
        if updater is not None:
            updater.cancel()
        if download_dir and download_dir.exists():
            shutil.rmtree(download_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if text == "Proses Video":
        await update.message.reply_text(
            " Kirim *file video* (.mp4/.mov/.mkv)\n"
            "atau *link YouTube* untuk diproses.\n\n"
            "_Hanya 1 video dalam satu waktu._",
            parse_mode="Markdown",
            reply_markup=BACK_MENU,
        )
    elif text == "Bantuan":
        await help_command(update, context)
    elif text == "Tentang Bot":
        await about(update, context)
    elif text in ("Cookies YouTube", "Cara Export Cookies"):
        await cookies_command(update, context)
    elif text == "Template":
        await template_menu(update, context)
    elif text == "Kembali ke Menu":
        await start(update, context)
    else:
        await update.message.reply_text(
            "Pilih menu di keyboard atau kirim video/link.",
            reply_markup=MAIN_MENU,
        )


# ---------------------------------------------------------------------------
# Template handlers
# ---------------------------------------------------------------------------

async def template_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    pref = tpl.get_pref(user_id)
    preset_name = pref.get("preset", "capcut")
    current = tpl.get_user_template(user_id)

    lines = [
        f"*Template Editor*{' (Custom)' if preset_name == 'custom' else ''}\n",
        f"Preset: *{tpl.template_display_name(preset_name)}*",
        "",
    ]
    for section in ["hook", "subtitle", "video", "shapes"]:
        sec_summary = tpl.format_section_summary(current, section)
        if sec_summary:
            labels = {"hook": "Hook", "subtitle": "Subtitle", "video": "Video", "shapes": "Shapes"}
            lines.append(f"*{labels[section]}*")
            lines.append(sec_summary)
            lines.append("")

    keyboard = [
        [InlineKeyboardButton("Ganti Preset", callback_data="tmpl_presets")],
        [InlineKeyboardButton("Hook", callback_data="tmpl_edit_hook"),
         InlineKeyboardButton("Subtitle", callback_data="tmpl_edit_subtitle")],
        [InlineKeyboardButton("Video", callback_data="tmpl_edit_video"),
         InlineKeyboardButton("Shapes", callback_data="tmpl_edit_shapes")],
        [InlineKeyboardButton("Export Template", callback_data="tmpl_export"),
         InlineKeyboardButton("Upload Custom", callback_data="tmpl_upload")],
        [InlineKeyboardButton("Buka Web Editor", web_app=WebAppInfo(url=_web_editor_url(user_id)))],
        [InlineKeyboardButton("Kembali", callback_data="tmpl_back")],
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def template_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("*Template* — Kembali ke menu utama.", parse_mode="Markdown")
    await query.message.reply_text("Pilih menu di bawah.", reply_markup=MAIN_MENU)


async def template_presets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lines = ["*Pilih Preset Template*\n"]
    for name in tpl.all_templates():
        lines.append(f"▸ *{tpl.template_display_name(name)}*")
    lines.append("▸ *Custom* — pakai file JSON kustom")
    lines.extend(["", "Klik untuk memilih:"])
    keyboard = [
        [InlineKeyboardButton(tpl.template_display_name(n), callback_data=f"tmpl_set_{n}")]
        for n in tpl.available_templates()
    ]
    keyboard.append([InlineKeyboardButton(" Upload Custom JSON", callback_data="tmpl_upload")])
    keyboard.append([InlineKeyboardButton("Kembali", callback_data="tmpl_back")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def template_set_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    preset = query.data.replace("tmpl_set_", "")
    tpl.set_preset(user_id, preset)
    current = tpl.get_user_template(user_id)
    lines = [f" Template diubah ke *{tpl.template_display_name(preset)}*\n"]
    for section in ["hook", "subtitle", "video"]:
        sec_summary = tpl.format_section_summary(current, section)
        if sec_summary:
            labels = {"hook": "Hook", "subtitle": "Subtitle", "video": "Video"}
            lines.append(f"*{labels[section]}*")
            lines.append(sec_summary)
            lines.append("")
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
    )


async def template_edit_section(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    section = query.data.replace("tmpl_edit_", "")
    user_id = update.effective_user.id
    current = tpl.get_user_template(user_id)
    labels = {"hook": "Hook Teks", "subtitle": "Subtitle", "video": "Video Layout", "shapes": "Shapes"}
    label = labels.get(section, section)

    lines = [f"*{label}*\n", tpl.format_section_summary(current, section), ""]
    params = tpl.SECTIONS.get(section, [])
    keyboard = []
    for key, display, ptype, options in params:
        if key == "enabled":
            continue
        val = tpl.format_current_value(current, section, key, ptype)
        if ptype == "bool":
            keyboard.append([InlineKeyboardButton(f"{display}: {'' if val else ''}", callback_data=f"tmpl_param_{section}_{key}")])
        elif ptype == "int":
            keyboard.append([InlineKeyboardButton(f"{display}: {val}", callback_data=f"tmpl_param_{section}_{key}")])
        elif ptype == "color":
            keyboard.append([InlineKeyboardButton(f" {display}", callback_data=f"tmpl_param_{section}_{key}")])
        elif ptype == "choice":
            keyboard.append([InlineKeyboardButton(f"{display}: {val}", callback_data=f"tmpl_param_{section}_{key}")])
        elif ptype == "expr":
            keyboard.append([InlineKeyboardButton(f" {display}: {val}", callback_data=f"tmpl_param_{section}_{key}")])
        elif ptype == "text":
            keyboard.append([InlineKeyboardButton(f" {display}: {val}", callback_data=f"tmpl_param_{section}_{key}")])
    keyboard.append([InlineKeyboardButton("Kembali ke Template", callback_data="tmpl_back")])

    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def template_param_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data.replace("tmpl_param_", "")  # e.g. "hook_font_size" or "subtitle_font_size"
    # Safely split: section is first segment, rest is the key
    known_sections = ("hook", "subtitle", "video", "shapes")
    section = data
    key = data
    for s in known_sections:
        if data.startswith(s + "_"):
            section = s
            key = data[len(s) + 1:]
            break

    # Find param definition
    params = tpl.SECTIONS.get(section, [])
    ptype, options = None, None
    label = key
    for k, lbl, pt, opts in params:
        if k == key:
            ptype, options, label = pt, opts, lbl
            break
    if ptype is None:
        await query.edit_message_text(" Parameter tidak dikenal.")
        return

    current = tpl.get_user_template(user_id)
    current_val = current.get(section, {}).get(key)

    if ptype == "bool":
        new_val = not current_val if isinstance(current_val, bool) else True
        tpl.override(user_id, section, key, new_val)
        await query.edit_message_text(
            f" *{label}* → `{'Aktif' if new_val else 'Nonaktif'}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Kembali", callback_data=f"tmpl_edit_{section}")]]
            ),
        )
        return

    if not options:
        await query.edit_message_text(f" {label} tidak punya opsi.")
        return

    # For alignment, convert to/from display value
    if key == "alignment":
        if current_val in tpl.ALIGNMENT_REVERSE:
            current_val = tpl.ALIGNMENT_REVERSE[current_val]

    idx = -1
    try:
        if current_val in options:
            idx = options.index(current_val)
    except (ValueError, TypeError):
        pass
    next_val = options[(idx + 1) % len(options)]

    # Convert back for alignment
    if key == "alignment":
        next_val = tpl.ALIGNMENT_MAP.get(next_val, next_val)

    tpl.override(user_id, section, key, next_val)

    # Display value
    display = next_val
    if ptype == "int":
        display = f"{next_val}px" if key != "rotate" else f"{next_val}°"

    await query.edit_message_text(
        f" *{label}* diubah ke `{display}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Kembali", callback_data=f"tmpl_edit_{section}")]]
        ),
    )


async def template_export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    json_str = tpl.export_json(user_id)
    await query.message.reply_document(
        document=json_str.encode("utf-8"),
        filename=f"template_{user_id}.json",
        caption=" *Template kamu* — simpan file ini atau upload kembali kapan saja.",
        parse_mode="Markdown",
    )


async def template_upload_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show prompt for uploading a custom JSON template."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        " *Upload Custom Template*\n\n"
        "Kirim file *JSON* (.json) dengan struktur template kamu.\n\n"
        "Kamu bisa gunakan file hasil export sebagai referensi, "
        "atau buat dari awal. Parameter yang tidak disertakan akan "
        "menggunakan nilai default.\n\n"
        "_Kirim file .json ke chat ini._",
        parse_mode="Markdown",
    )


async def handle_template_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle uploaded JSON template file."""
    if not is_allowed(update.effective_user.id):
        return
    doc = update.effective_message.document
    if not doc or not (doc.file_name or "").lower().endswith(".json"):
        return

    user_id = update.effective_user.id
    try:
        file = await doc.get_file()
        raw = (await file.download_as_bytearray()).decode("utf-8")
        data = json.loads(raw)
    except Exception as exc:
        await update.message.reply_text(f" Gagal membaca file JSON: {exc}")
        return

    if not isinstance(data, dict):
        await update.message.reply_text(" File JSON harus berupa object (dict).")
        return

    tpl.set_custom_json(user_id, data)
    current = tpl.get_user_template(user_id)
    lines = [" *Custom template berhasil diupload!*\n"]
    for section in ["hook", "subtitle", "video"]:
        sec_summary = tpl.format_section_summary(current, section)
        if sec_summary:
            labels = {"hook": "Hook", "subtitle": "Subtitle", "video": "Video"}
            lines.append(f"*{labels[section]}*")
            lines.append(sec_summary)
            lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    if is_processing(context):
        await update.message.reply_text(
            " Masih ada video yang diproses. Tunggu selesai dulu ya!",
            reply_markup=MAIN_MENU,
        )
        return

    if not try_claim_lock():
        await update.message.reply_text(
            " *Bot sedang memproses video dari user lain.*\n\n"
            "Silakan tunggu beberapa menit, kamu akan dapat giliran "
            "setelah video sebelumnya selesai.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    try:
        status_msg = await update.message.reply_text(" *Menerima video...*", parse_mode="Markdown")

        ts = int(time.time())
        file_path = config.INPUT_DIR / f"bot_{ts}_{update.effective_message.message_id}.mp4"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            file = await update.effective_message.effective_attachment.get_file()
            await file.download_to_drive(custom_path=str(file_path))
        except Exception as exc:
            await status_msg.edit_text(f" Gagal download video: {exc}")
            return

        await run_processing_flow(update, context, file_path, status_msg)
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

    # Any other document
    await update.message.reply_text(
        "Kirim file *cookies.txt* untuk autentikasi YouTube.\n"
        "Atau kirim video sebagai file untuk diproses.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle data sent from the Mini App (template editor)."""
    if not is_allowed(update.effective_user.id):
        return
    data = update.effective_message.web_app_data
    if not data:
        return

    try:
        payload = json.loads(data.data) if isinstance(data.data, str) else {}
    except json.JSONDecodeError:
        await update.message.reply_text(" Data dari Mini App tidak valid.")
        return

    action = payload.get("action")
    if action == "save_template" and "template" in payload:
        tmpl_data = payload["template"]
        user_id = update.effective_user.id
        for section in ["hook", "subtitle", "video"]:
            if section in tmpl_data and isinstance(tmpl_data[section], dict):
                for key, value in tmpl_data[section].items():
                    tpl.override(user_id, section, key, value)
        current = tpl.get_user_template(user_id)
        preset_name = tpl.get_pref(user_id).get("preset", "capcut")
        lines = [
            f" *Template dari Mini App tersimpan!*\n",
            f"Preset: *{tpl.template_display_name(preset_name)}*",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        await template_menu(update, context)
    else:
        await update.message.reply_text(" Aksi tidak dikenal dari Mini App.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    text = update.message.text.strip()

    # Menu buttons
    if text in ("Proses Video", "Bantuan", "Tentang Bot", "Cookies YouTube", "Cara Export Cookies", "Template", "Kembali ke Menu"):
        await menu_handler(update, context)
        return

    if not is_youtube_url(text):
        await update.message.reply_text(
            "Kirimkan link YouTube atau file video.\n"
            "Tekan /help untuk panduan.",
            reply_markup=MAIN_MENU,
        )
        return

    if is_processing(context):
        await update.message.reply_text(
            " Masih ada video yang diproses. Tunggu selesai dulu ya!",
            reply_markup=MAIN_MENU,
        )
        return

    if not try_claim_lock():
        await update.message.reply_text(
            " *Bot sedang memproses video dari user lain.*\n\n"
            "Silakan tunggu beberapa menit, kamu akan dapat giliran "
            "setelah video sebelumnya selesai.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    try:
        # Check cookies
        if not has_cookies():
            await update.message.reply_text(
                " *YouTube mewajibkan cookies*\n\n"
                "Untuk download video YouTube, admin perlu mengirim "
                "file cookies.txt dari browser.\n\n"
                "Tekan menu *Cookies YouTube* untuk panduan.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return

        status_msg = await update.message.reply_text(
            " *Mendownload dari YouTube...*", parse_mode="Markdown",
        )

        user_id = update.effective_user.id
        download_dir = config.PROJECT_ROOT / "tmp" / "bot_downloads" / str(user_id)

        try:
            loop = asyncio.get_event_loop()
            video_path = await loop.run_in_executor(
                None, partial(download_youtube_video, text, download_dir, user_id),
            )
        except Exception as exc:
            err = str(exc)
            if "Sign in" in err or "cookies" in err.lower():
                await status_msg.edit_text(
                    " *YouTube memblokir request.*\n\n"
                    "Cookies mungkin expired. Export ulang cookies.txt "
                    "dari browser yang login ke YouTube.\n"
                    "Menu *Cookies YouTube* → Kirim file baru.",
                    parse_mode="Markdown",
                )
                await update.message.reply_text(
                    "Silakan pilih menu di bawah.", reply_markup=MAIN_MENU,
                )
            else:
                await status_msg.edit_text(f" Gagal download YouTube: {err}")
            return

        if not video_path or not video_path.exists():
            await status_msg.edit_text(" Tidak bisa mendownload video dari link tersebut.")
            return

        ts = int(time.time())
        source_path = config.INPUT_DIR / f"yt_{ts}_{video_path.stem[:50]}{video_path.suffix}"
        shutil.copy2(video_path, source_path)

        await run_processing_flow(update, context, source_path, status_msg, download_dir)
    finally:
        release_lock()


# ---------------------------------------------------------------------------
# Callback: Done
# ---------------------------------------------------------------------------

async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    clip_ids = context.user_data.pop("clip_ids", [])
    chat_id = query.message.chat_id
    deleted = 0

    for msg_id in clip_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
        except Exception:
            pass

    try:
        await query.message.delete()
    except Exception:
        pass  # message may already be deleted

    await context.bot.send_message(
        chat_id=chat_id,
        text=f" {deleted} klip dihapus. Siap untuk video berikut!",
        reply_markup=MAIN_MENU,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diset di .env")

    ensure_directories()
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    (config.PROJECT_ROOT / "tmp" / "bot_downloads").mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cookies", cookies_command))
    app.add_handler(CallbackQueryHandler(done_callback, pattern="^done$"))
    app.add_handler(CallbackQueryHandler(template_presets_menu, pattern="^tmpl_presets$"))
    app.add_handler(CallbackQueryHandler(template_set_preset, pattern="^tmpl_set_"))
    app.add_handler(CallbackQueryHandler(template_edit_section, pattern="^tmpl_edit_"))
    app.add_handler(CallbackQueryHandler(template_param_cycle, pattern="^tmpl_param_"))
    app.add_handler(CallbackQueryHandler(template_export_handler, pattern="^tmpl_export$"))
    app.add_handler(CallbackQueryHandler(template_upload_prompt, pattern="^tmpl_upload$"))
    app.add_handler(CallbackQueryHandler(template_back, pattern="^tmpl_back$"))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), handle_template_upload))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    tpl.ensure_presets()

    global _EDITOR_SERVER
    _EDITOR_SERVER = web_editor.EditorServer()
    _EDITOR_SERVER.start_thread()

    LOGGER.info("ContentClipper Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
