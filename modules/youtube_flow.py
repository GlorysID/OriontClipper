"""YouTube transcript-first flow.

Fetches captions without downloading the video, so moment selection happens
on text only. Only the selected time ranges are downloaded afterwards.

Public API:
- fetch_youtube_transcript(url, work_dir, user_id, cookie_path) -> YoutubeTranscript
- download_youtube_segment(url, start_s, end_s, out_base, work_dir, user_id, cookie_path) -> Path
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

import config
from modules.transcriber import TranscriptSegment


LOGGER = logging.getLogger("contentclipper.youtube")

# Extra seconds downloaded before/after each section so keyframe snapping and
# transcript alignment never clip actual content. The renderer re-encodes with
# an exact seek, so the margin is trimmed again during render.
SECTION_MARGIN_S = 5.0

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_CUE_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]+>")
_BRACKET_ONLY_RE = re.compile(r"^[\[(].*[\])]$")


class YoutubeTranscriptError(Exception):
    """Raised when YouTube captions cannot be fetched or parsed."""


class YoutubeDownloadError(Exception):
    """Raised when a section download fails."""


@dataclass(frozen=True)
class YoutubeTranscript:
    segments: list[TranscriptSegment]
    duration: float
    title: str
    video_id: str
    language: str = "auto"


def _deno_path_prepend() -> None:
    deno_path = Path.home() / ".deno" / "bin"
    if deno_path.exists() and str(deno_path) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{deno_path}{os.pathsep}{os.environ.get('PATH', '')}"


def _prepare_cookie_tmp(cookie_path: Path | None) -> Path | None:
    if cookie_path is None or not cookie_path.exists() or cookie_path.stat().st_size <= 50:
        return None
    cookie_tmp = cookie_path.parent / f"cookies_ytdlp_{os.getpid()}.txt"
    if cookie_tmp.exists():
        try:
            cookie_tmp.chmod(0o666)
        except Exception:
            pass
    shutil.copy2(cookie_path, cookie_tmp)
    if os.name != "nt":
        cookie_tmp.chmod(0o600)
    return cookie_tmp


def _base_ydl_opts() -> dict:
    _deno_path_prepend()
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "fetch_pot": ["always"],
            }
        },
    }
    node_exe = shutil.which("node") or (
        r"C:\Program Files\nodejs\node.exe"
        if Path(r"C:\Program Files\nodejs\node.exe").exists()
        else None
    )
    if node_exe:
        opts["js_runtimes"] = {"node": {"path": str(node_exe)}}
    return opts


def _cleanup_cookie_tmp(cookie_tmp: Path | None) -> None:
    if cookie_tmp is not None:
        cookie_tmp.unlink(missing_ok=True)


def _single_video(info: dict) -> dict:
    """yt-dlp may return a playlist wrapper; unwrap to the first entry."""
    if isinstance(info, dict) and isinstance(info.get("entries"), list):
        for entry in info["entries"]:
            if entry:
                return entry
        raise YoutubeTranscriptError("Playlist has no downloadable video")
    return info


def _find_subtitle_file(
    work_dir: Path,
    native_lang: str | None = None,
    preferred_lang: str = "auto",
) -> Path | None:
    """Find the best subtitle file, prioritizing native audio language over auto-translations."""
    candidates = list(work_dir.glob("*.vtt")) + list(work_dir.glob("*.srt"))
    if not candidates:
        return None

    def priority(path: Path) -> tuple[int, int, str]:
        stem = path.name.lower()
        # Rank 10: Original spoken audio marked with -orig for native language (e.g. *.en-orig.vtt or *.id-orig.vtt)
        if native_lang and (f".{native_lang}-orig" in stem or f"-{native_lang}-orig" in stem):
            return (10, 0, stem)
        # Rank 9: Any -orig caption
        if "-orig" in stem:
            return (9, 0, stem)
        # Rank 8: Native language caption (e.g. *.en.vtt for English, *.id.vtt for Indonesian)
        if native_lang and (f".{native_lang}." in stem or stem.endswith(f".{native_lang}.vtt")):
            return (8, 0, stem)
        # Rank 7: Preferred language caption if set
        if preferred_lang and preferred_lang != "auto" and (f".{preferred_lang}." in stem or stem.endswith(f".{preferred_lang}.vtt")):
            return (7, 0, stem)
        # Rank 6: Indonesian caption
        if ".id." in stem or stem.endswith(".id.vtt") or stem.endswith(".id.srt"):
            return (6, 0, stem)
        # Rank 5: English caption
        if ".en." in stem or stem.endswith(".en.vtt") or stem.endswith(".en.srt"):
            return (5, 0, stem)
        # Rank 1: Any other caption
        return (1, 0, stem)

    best_candidate = max(candidates, key=priority)
    LOGGER.info(
        "Selected best subtitle file: %s (priority rank %d, native_lang=%s, from %d candidate(s): %s)",
        best_candidate.name,
        priority(best_candidate)[0],
        native_lang,
        len(candidates),
        [c.name for c in candidates],
    )
    return best_candidate


def _parse_timestamp(hours: str, minutes: str, seconds: str, fraction: str) -> float:
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(fraction.ljust(3, "0")) / 1000.0
    )


def _clean_cue_text(line: str) -> str:
    cleaned = _TAG_RE.sub("", line).strip()
    return " ".join(cleaned.split())


def sanitize_transcript_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Ensure segments are strictly non-overlapping and clean for subtitle rendering."""
    if not segments:
        return []
    cleaned: list[TranscriptSegment] = []
    for seg in segments:
        text = seg.text.strip()
        if not text or seg.end <= seg.start:
            continue
        # Clamp previous segment end if it extends into current segment start
        if cleaned:
            prev = cleaned[-1]
            if prev.end > seg.start:
                new_prev_end = max(prev.start + 0.3, seg.start)
                cleaned[-1] = TranscriptSegment(prev.start, new_prev_end, prev.text)
        # Deduplicate consecutive identical text
        if cleaned and cleaned[-1].text == text:
            cleaned[-1] = TranscriptSegment(cleaned[-1].start, max(cleaned[-1].end, seg.end), cleaned[-1].text)
            continue
        cleaned.append(TranscriptSegment(seg.start, seg.end, text))
    return cleaned


def parse_vtt(text: str) -> list[TranscriptSegment]:
    """Parse a VTT caption file, properly handling multi-line rolling cues and skipping micro-flash cues."""
    lines = text.splitlines()
    raw_cues: list[tuple[float, float, list[str]]] = []
    i = 0

    while i < len(lines):
        match = _CUE_RE.search(lines[i])
        if match is None:
            i += 1
            continue

        start = _parse_timestamp(*match.group(1, 2, 3, 4))
        end = _parse_timestamp(*match.group(5, 6, 7, 8))
        i += 1

        cue_lines: list[str] = []
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped == "" or _CUE_RE.search(lines[i]):
                break
            if not stripped.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:")):
                cleaned = _clean_cue_text(lines[i])
                if cleaned and not _BRACKET_ONLY_RE.match(cleaned):
                    cue_lines.append(cleaned)
            i += 1

        # Ignore micro-flash cues (<100ms) used by YouTube for styling anchors
        if not cue_lines or (end - start) < 0.10:
            continue

        raw_cues.append((start, end, cue_lines))

    # Deduplicate rolling captions (YouTube 2-line rolling window where line 0 repeats previous cue)
    segments: list[TranscriptSegment] = []
    prev_lines_set: set[str] = set()

    for start, end, cue_lines in raw_cues:
        new_lines = [cl for cl in cue_lines if cl not in prev_lines_set]
        if not new_lines:
            text = " ".join(cue_lines)
            if not segments or segments[-1].text != text:
                new_lines = cue_lines
            else:
                continue

        text = " ".join(new_lines).strip()
        if not text:
            continue

        prev_lines_set = set(cue_lines)

        # Extend if identical
        if segments and segments[-1].text == text:
            segments[-1] = TranscriptSegment(segments[-1].start, max(segments[-1].end, end), text)
            continue

        # Clamp if overlaps
        if segments and segments[-1].end > start:
            prev = segments[-1]
            segments[-1] = TranscriptSegment(prev.start, max(prev.start + 0.3, start), prev.text)

        segments.append(TranscriptSegment(start, end, text))

    return sanitize_transcript_segments(segments)


def parse_vtt_file(path: Path) -> list[TranscriptSegment]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise YoutubeTranscriptError(f"Cannot read subtitle file {path}: {exc}") from exc
    return parse_vtt(text)


def fetch_youtube_transcript(
    url: str,
    work_dir: Path,
    user_id: int,
    cookie_path: Path | None = None,
) -> YoutubeTranscript:
    work_dir.mkdir(parents=True, exist_ok=True)
    preferred_lang = config.WHISPER_LANGUAGE or "auto"
    opts = _base_ydl_opts()
    opts.update(
        {
            "skip_download": True,
            "writeautomaticsub": True,
            "writesubtitles": True,
            "subtitleslangs": ["en", "id", "en-orig", "id-orig"],
            "subtitlesformat": "vtt/srt/best",
            "ignoreerrors": True,
            "sleep_interval_subtitles": 1,
            "max_sleep_interval_subtitles": 3,
            "writeinfojson": True,
            "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
        }
    )

    cookie_tmp = _prepare_cookie_tmp(cookie_path)
    if cookie_tmp is not None:
        opts["cookiefile"] = str(cookie_tmp)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = _single_video(ydl.extract_info(url, download=True))

        duration = float(info.get("duration") or 0)
        title = str(info.get("title") or info.get("id") or "youtube")
        video_id = str(info.get("id") or "video")
        raw_lang = str(info.get("language") or info.get("audio_language") or "").lower()
        native_lang = raw_lang[:2] if raw_lang and len(raw_lang) >= 2 else None

        subtitle_file = _find_subtitle_file(work_dir, native_lang=native_lang, preferred_lang=preferred_lang)
        if subtitle_file is None:
            raise YoutubeTranscriptError(
                "Tidak ada subtitle yang tersedia untuk video ini"
            )

        segments = parse_vtt_file(subtitle_file)
        if not segments:
            raise YoutubeTranscriptError("Subtitle berhasil diunduh tapi transkrip kosong")

        LOGGER.info(
            "Fetched YouTube transcript for user %s: %s (lang=%s, %d segments, %.1fs)",
            user_id,
            video_id,
            native_lang or "auto",
            len(segments),
            duration,
        )
        return YoutubeTranscript(
            segments=segments,
            duration=duration,
            title=title,
            video_id=video_id,
            language=native_lang or "auto",
        )
    except YoutubeTranscriptError:
        raise
    except Exception as exc:
        LOGGER.error("YouTube transcript fetch failed for user %s: %s", user_id, exc)
        raise YoutubeTranscriptError(f"Gagal mengambil transkrip YouTube: {exc}") from exc
    finally:
        _cleanup_cookie_tmp(cookie_tmp)


def _format_section_ts(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def download_youtube_segment(
    url: str,
    start_s: float,
    end_s: float,
    out_base: str,
    work_dir: Path,
    user_id: int,
    cookie_path: Path | None = None,
) -> Path:
    """Download only one time range of the video, re-encoded at exact cuts."""
    dl_start = max(0.0, start_s - SECTION_MARGIN_S)
    dl_end = end_s + SECTION_MARGIN_S

    opts = _base_ydl_opts()
    opts.update(
        {
            "format": (
                "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
                "b[height<=1080][ext=mp4]/bv*+ba/b"
            ),
            "download_ranges": yt_dlp.utils.download_range_func(None, [(dl_start, dl_end)]),
            "force_keyframes_at_cuts": True,
            "merge_output_format": "mp4",
            "outtmpl": str(work_dir / f"{out_base}.%(ext)s"),
        }
    )

    cookie_tmp = _prepare_cookie_tmp(cookie_path)
    if cookie_tmp is not None:
        opts["cookiefile"] = str(cookie_tmp)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        segment_path = _resolve_downloaded_file(work_dir, out_base)
        LOGGER.info(
            "Downloaded segment %s (%.2f-%.2f) for user %s: %s",
            out_base,
            dl_start,
            dl_end,
            user_id,
            segment_path,
        )
        return segment_path
    except (YoutubeDownloadError, YoutubeTranscriptError):
        raise
    except Exception as exc:
        LOGGER.error(
            "YouTube segment download failed for user %s (%s %.2f-%.2f): %s",
            user_id,
            out_base,
            dl_start,
            dl_end,
            exc,
        )
        raise YoutubeDownloadError(f"Gagal mengunduh segmen video: {exc}") from exc
    finally:
        _cleanup_cookie_tmp(cookie_tmp)


def _resolve_downloaded_file(work_dir: Path, out_base: str) -> Path:
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = work_dir / f"{out_base}{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            if candidate.suffix == ".mp4":
                return candidate
            # Renderer expects an FFmpeg-readable file; remux to mp4 losslessly.
            mp4_path = candidate.with_suffix(".mp4")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(candidate), "-c", "copy", str(mp4_path)],
                    check=True,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                raise YoutubeDownloadError(
                    f"Remux segmen {candidate.name} ke mp4 gagal: {exc}"
                ) from exc
            candidate.unlink(missing_ok=True)
            return mp4_path
    raise YoutubeDownloadError(f"Segmen {out_base} tidak ditemukan setelah download")
