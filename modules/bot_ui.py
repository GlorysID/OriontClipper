"""ContentClipper Bot UI & Visual Polish Module.

Provides real-time animated HUD status cards, official Telegram animated stickers (.TGS),
dynamic progress bars, live ticking timers, and aesthetic card formatting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from telegram import Bot, Message, ReactionTypeEmoji
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter

import config

LOGGER = logging.getLogger("contentclipper.bot_ui")

# ---------------------------------------------------------------------------
# Telegram Animated Stickers (.TGS 60 FPS)
# Verified Telegram file_ids with fallback to local assets/stickers/*.tgs
# ---------------------------------------------------------------------------
STICKER_DIR = config.PROJECT_ROOT / "assets" / "stickers"

ANIMATED_STICKERS: dict[str, dict[str, Any]] = {
    "robot": {
        "file_id": "CAACAgIAAxUAAWqg2SpwXPWWlZVVgPMKgkCcdYetAAJOAgACVp29CjD-a22BMgNvPQQ",
        "file_path": STICKER_DIR / "robot.tgs",
    },
    "brain": {
        "file_id": "CAACAgIAAxUAAWqg2S0-BVa_Bc_fHYefuX4_pSaEAAL7DQAC9UMgSMVtjjzmO7uRPQQ",
        "file_path": STICKER_DIR / "brain.tgs",
    },
    "rocket": {
        "file_id": "CAACAgIAAxUAAWqg2NXuu8Y7HhsrryxWgx9vDwWKAALhAANWnb0KW8GUi0D406A9BA",
        "file_path": STICKER_DIR / "rocket.tgs",
    },
    "party": {
        "file_id": "CAACAgIAAxUAAWqg2SrV9z_piRvMa_-UcAvA315GAAJKAgACVp29CslqxmhgGHrwPQQ",
        "file_path": STICKER_DIR / "party.tgs",
    },
    "fire": {
        "file_id": "CAACAgIAAxUAAWqg2NXye8pKaLhGVygUC6a4h-kOAALcAANWnb0KnL0ablODX3Y9BA",
        "file_path": STICKER_DIR / "fire.tgs",
    },
    "victory": {
        "file_id": "CAACAgIAAxUAAWqg2NWo95Yf7dDzcJMB6VDyQTuyAALfAANWnb0KEEh8kSOlJ_09BA",
    },
}

# ---------------------------------------------------------------------------
# Visual Constants
# ---------------------------------------------------------------------------
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
PULSE_ICONS = ["⚡", "✨", "💫", "🔥", "🚀"]

PRO_TIPS = [
    "OriontClip menyeleksi kalimat lengkap tanpa memotong konteks pembicara!",
    "Gunakan menu Style & Hook untuk mencoba preset CapCut atau Alex Hormozi.",
    "Ketik /edithook <nomor> <teks> untuk mengubah hook klip sesukamu.",
    "Kata di dalam tanda *bintang* otomatis diberi warna highlight menyala!",
    "Hook dengan viral score tinggi dioptimalkan untuk menarik penonton di 3 detik awal.",
    "Whisper AI menyelaraskan audio kata-per-kata untuk subtitel ultra presisi.",
    "Video otomatis di-framing ke rasio 9:16 vertikal siap unggah ke Reels & TikTok.",
]


# ---------------------------------------------------------------------------
# Helpers: Stickers & Reactions
# ---------------------------------------------------------------------------

async def safe_set_reaction(bot: Bot, chat_id: int, message_id: int, emoji: str = "⚡") -> None:
    """Set an emoji reaction on a message safely."""
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as exc:
        LOGGER.debug("Could not set reaction %s: %s", emoji, exc)


async def safe_send_animated_sticker(bot: Bot, chat_id: int, sticker_key: str = "robot") -> Message | None:
    """Send an animated .TGS sticker using Telegram file_id with local file fallback."""
    cfg = ANIMATED_STICKERS.get(sticker_key)
    if not cfg:
        return None

    file_id = cfg.get("file_id")
    local_path = cfg.get("file_path")

    # 1. Try sending via Telegram file_id (instant server-side delivery)
    if file_id:
        try:
            return await bot.send_sticker(chat_id=chat_id, sticker=file_id)
        except Exception as exc:
            LOGGER.debug("Could not send sticker via file_id %s: %s", sticker_key, exc)

    # 2. Fallback: upload local .tgs file
    if local_path and isinstance(local_path, Path) and local_path.exists():
        try:
            with open(local_path, "rb") as f:
                return await bot.send_sticker(chat_id=chat_id, sticker=f)
        except Exception as exc:
            LOGGER.debug("Could not send sticker via local file %s: %s", local_path, exc)

    return None


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Delete a message safely without raising exceptions if already gone."""
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Formatting Utilities
# ---------------------------------------------------------------------------

def render_progress_bar(pct: float, width: int = 10) -> str:
    """Generate a sleek unicode progress bar."""
    filled = int(round(width * (pct / 100.0)))
    filled = max(0, min(width, filled))
    empty = width - filled
    return f"{'▰' * filled}{'▱' * empty} {int(pct)}%"


def format_elapsed(seconds: float) -> str:
    """Format seconds into MM:SS."""
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


# ---------------------------------------------------------------------------
# Real-Time HUD Status Card Animator
# ---------------------------------------------------------------------------

class StatusCardAnimator:
    """Controls dynamic, real-time animated status card in Telegram (1.5s refresh).

    Features:
    - Rotating braille cyber spinners (⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    - Live ticking seconds timer (⏱ 00:14)
    - Auto-interpolating progress bar
    - Pipeline checklist with active stage indicator
    - Contextual rotating pro tips
    - Native Telegram ChatAction support (typing, record_video, upload_video)
    - Robust rate-limiting & exception immunity
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        status_msg: Message,
        flow_type: str = "discovery",  # 'discovery' or 'render'
        clip_idx: int = 1,
        hook: str = "",
        duration: float = 0.0,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.status_msg = status_msg
        self.flow_type = flow_type
        self.clip_idx = clip_idx
        self.hook = hook
        self.duration = duration

        self.stage = "init"
        self.detail = "Mempersiapkan analisis AI..."
        self.is_running = False
        self.t0 = time.time()
        self.frame_idx = 0
        self._task: asyncio.Task | None = None
        self._last_rendered_text = ""

    def set_stage(self, stage: str, detail: str) -> None:
        """Update current stage and description text."""
        self.stage = stage
        self.detail = detail

    def start(self) -> asyncio.Task:
        """Launch the background animation loop."""
        self.is_running = True
        self.t0 = time.time()
        self._task = asyncio.create_task(self._animate_loop())
        return self._task

    def stop(self) -> None:
        """Gracefully stop the animation loop."""
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _animate_loop(self) -> None:
        while self.is_running:
            try:
                elapsed = time.time() - self.t0
                self.frame_idx += 1

                # 1. Trigger native Telegram ChatAction every 4-5 seconds
                if self.frame_idx % 3 == 1:
                    await self._send_chat_action()

                # 2. Build current frame text
                if self.flow_type == "discovery":
                    card_text = self._build_discovery_card(elapsed)
                else:
                    card_text = self._build_render_card(elapsed)

                # 3. Edit status message if changed
                if card_text != self._last_rendered_text:
                    try:
                        await self.status_msg.edit_text(card_text, parse_mode="Markdown")
                        self._last_rendered_text = card_text
                    except RetryAfter as err:
                        await asyncio.sleep(float(err.retry_after) + 0.2)
                    except BadRequest as err:
                        # Silently ignore message is not modified or deleted
                        if "message is not modified" not in str(err).lower():
                            LOGGER.debug("Edit status message ignored: %s", err)
                    except Exception as err:
                        LOGGER.debug("Non-fatal edit exception: %s", err)

            except asyncio.CancelledError:
                break
            except Exception as loop_err:
                LOGGER.debug("StatusCardAnimator loop error: %s", loop_err)

            # Smooth 1.5s refresh interval (fluid animation, 100% safe from rate limits)
            await asyncio.sleep(1.5)

    async def _send_chat_action(self) -> None:
        try:
            if self.stage in ("render", "ffmpeg"):
                await self.bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.RECORD_VIDEO)
            elif self.stage in ("upload", "send"):
                await self.bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.UPLOAD_VIDEO)
            else:
                await self.bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.TYPING)
        except Exception:
            pass

    def _build_discovery_card(self, elapsed: float) -> str:
        spinner = SPINNER_FRAMES[self.frame_idx % len(SPINNER_FRAMES)]
        pulse = PULSE_ICONS[(self.frame_idx // 2) % len(PULSE_ICONS)]
        tip = PRO_TIPS[(self.frame_idx // 4) % len(PRO_TIPS)]

        # Dynamic progress estimation
        if self.stage in ("init", "download", "download_meta", "transcript"):
            s1, s2, s3 = f"[{spinner}]", "[⏳]", "[⏳]"
            pct = min(38.0, 10.0 + elapsed * 2.0)
        elif self.stage in ("transcribe", "whisper"):
            s1, s2, s3 = f"[{spinner}]", "[⏳]", "[⏳]"
            pct = min(50.0, 15.0 + elapsed * 1.5)
        elif self.stage in ("analyze", "ai"):
            s1, s2, s3 = "[✅]", f"[{spinner}]", "[⏳]"
            pct = min(92.0, 50.0 + elapsed * 1.8)
        elif self.stage in ("catalog", "finalize"):
            s1, s2, s3 = "[✅]", "[✅]", f"[{spinner}]"
            pct = 98.0
        else:
            s1, s2, s3 = f"[{spinner}]", "[⏳]", "[⏳]"
            pct = min(92.0, 10.0 + elapsed * 1.5)

        bar = render_progress_bar(pct)
        time_str = format_elapsed(elapsed)
        clean_detail = escape_tg_md(self.detail)

        return (
            f"{pulse} *ORIONTCLIPPER AI STUDIO* {pulse}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄 *Status:* _{clean_detail}_ {spinner}\n"
            f"⏱ *Waktu:* `{time_str}` • *Engine:* `Deep-Reasoning AI`\n\n"
            f"📊 *Tahapan Analisis:*\n"
            f"  {s1} *1.* Ekstraksi Transkrip & Audio\n"
            f"  {s2} *2.* AI Context Reasoning (9Router Combo)\n"
            f"  {s3} *3.* Kurasi Topik & Pembuatan Katalog Klip\n\n"
            f"🚀 *Progress:* `{bar}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _{tip}_"
        )

    def _build_render_card(self, elapsed: float) -> str:
        spinner = SPINNER_FRAMES[self.frame_idx % len(SPINNER_FRAMES)]
        pulse = PULSE_ICONS[(self.frame_idx // 2) % len(PULSE_ICONS)]
        tip = PRO_TIPS[(self.frame_idx // 4) % len(PRO_TIPS)]

        if self.stage in ("init", "download"):
            s1, s2, s3, s4 = f"[{spinner}]", "[⏳]", "[⏳]", "[⏳]"
            pct = min(25.0, 5.0 + elapsed * 2.5)
        elif self.stage in ("transcribe", "whisper"):
            s1, s2, s3, s4 = "[✅]", f"[{spinner}]", "[⏳]", "[⏳]"
            pct = min(50.0, 25.0 + elapsed * 1.8)
        elif self.stage in ("render", "ffmpeg"):
            s1, s2, s3, s4 = "[✅]", "[✅]", f"[{spinner}]", "[⏳]"
            pct = min(88.0, 50.0 + elapsed * 1.4)
        elif self.stage in ("upload", "send"):
            s1, s2, s3, s4 = "[✅]", "[✅]", "[✅]", f"[{spinner}]"
            pct = 95.0
        else:
            s1, s2, s3, s4 = "[✅]", "[✅]", f"[{spinner}]", "[⏳]"
            pct = min(90.0, 50.0 + elapsed * 1.2)

        bar = render_progress_bar(pct)
        time_str = format_elapsed(elapsed)
        clean_hook = escape_tg_md(self.hook.replace("*", ""))
        clean_detail = escape_tg_md(self.detail)

        return (
            f"{pulse} *ORIONTCLIP RENDER STUDIO* {pulse}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎬 *Render Klip #{self.clip_idx}* • ⏱ `{int(self.duration)}s` (9:16)\n"
            f"🎯 *Hook:* \"_{clean_hook}_\"\n\n"
            f"🔄 *Status:* _{clean_detail}_ {spinner}\n"
            f"⏱ *Waktu:* `{time_str}` • *Engine:* `FFmpeg + Whisper`\n\n"
            f"📊 *Tahapan Render:*\n"
            f"  {s1} *1.* Unduh Segmen Video HD\n"
            f"  {s2} *2.* Whisper AI Word Alignment\n"
            f"  {s3} *3.* Burn Subtitle & Smart 9:16 Framing\n"
            f"  {s4} *4.* Upload Video ke Telegram\n\n"
            f"🚀 *Progress:* `{bar}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _{tip}_"
        )


# ---------------------------------------------------------------------------
# Card Formatters: Catalog & Video Delivery
# ---------------------------------------------------------------------------

def format_clip_card(idx: int, moment: Any, campaign_line: str = "") -> str:
    """Format an individual clip item in the catalog.

    campaign_line: opsional — baris badge kepatuhan campaign (emoji + alasan
    singkat) yang Already escaped-Markdown-safe dari pemanggil.
    """
    dur = moment.end - moment.start
    topic_clean = escape_tg_md(moment.topic)
    hook_clean = escape_tg_md(moment.hook)
    caption_clean = escape_tg_md(moment.caption)
    alasan_clean = escape_tg_md(getattr(moment, "alasan", ""))
    bgm_mood = getattr(moment, "bgm_mood", "") or "upbeat"
    viral_score = getattr(moment, "viral_score", 85)

    start_m, start_s = int(moment.start // 60), int(moment.start % 60)
    end_m, end_s = int(moment.end // 60), int(moment.end % 60)
    time_range = f"{start_m:02d}:{start_s:02d} - {end_m:02d}:{end_s:02d}"

    card = (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 *KLIP #{idx}* (⏱ `{time_range}` • {int(dur)}s) | 🔥 *Skor: {viral_score}/100*\n"
    )
    if campaign_line:
        card += f"{campaign_line}\n"
    card += (
        f"📌 *Topik:* {topic_clean}\n"
        f"🎯 *Hook Utama:*\n\"{hook_clean}\"\n"
    )
    if alasan_clean:
        card += f"💡 *Kenapa Menarik:* _{alasan_clean}_\n"
    card += (
        f"🎵 *Mood BGM:* `{bgm_mood}`\n"
        f"📝 *Caption Siap Pakai:*\n_{caption_clean}_\n\n"
    )
    return card


def format_delivery_caption(idx: int, moment: Any) -> str:
    """Format the delivery caption accompanying the final 9:16 vertical video."""
    dur = moment.end - moment.start
    clean_hook = moment.hook.replace("*", "")
    caption_full = (
        f"🏆 *KLIP #{idx} SELESAI DIGENERATE!* 🎉\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Hook:* \"_{clean_hook}_\"\n\n"
        f"📌 *Topik:* {moment.topic}\n"
        f"⏱ *Durasi:* {int(dur)} detik • 📐 *Format:* 1080×1920 (9:16)\n"
        f"🔥 *Skor Viral:* {getattr(moment, 'viral_score', 85)}/100\n\n"
        f"📝 *Caption Medsos (Tinggal Salin):*\n"
        f"{moment.caption}"
    )
    notes = [str(n).strip() for n in (getattr(moment, "campaign_notes", None) or []) if str(n).strip()]
    if notes:
        caption_full += "\n\n👤 *Catatan Syarat Campaign (Tanggung Jawab Akun):*\n"
        for n in notes[:4]:
            caption_full += f"• {escape_tg_md(n)}\n"
    return caption_full
