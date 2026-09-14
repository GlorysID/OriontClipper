"""Google Drive video downloader module for OriontClipper.

Handles downloading video files from Google Drive public/shared links:
- Supports all standard Google Drive link formats (file/d/, open?id=, uc?id=).
- Primary download via yt-dlp GoogleDrive extractor with format selection.
- Secondary fallback via direct requests.Session streaming with virus scan confirmation handling.
- Real-time progress updates to StatusCardAnimator.
- Comprehensive permission, quota, and video probe validation.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import requests
import yt_dlp

import config
from modules.video_editor import probe_video, VideoEditError

LOGGER = logging.getLogger("contentclipper.gdrive")

GDRIVE_FILE_RE = re.compile(
    r"(?:https?://)?(?:drive\.google\.com/(?:file/d/|open\?(?:.*&)?id=|uc\?(?:.*&)?id=)|docs\.google\.com/file/d/)([a-zA-Z0-9_-]{20,})",
    re.IGNORECASE,
)

GDRIVE_FOLDER_RE = re.compile(
    r"(?:https?://)?(?:drive\.google\.com/drive/(?:u/\d+/)?folders/)([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)


class GDriveError(Exception):
    """Base exception for Google Drive operations."""


class GDrivePermissionError(GDriveError):
    """Raised when the file is private or access is denied."""


class GDriveQuotaError(GDriveError):
    """Raised when Google Drive download quota is exceeded."""


class GDriveInvalidFileError(GDriveError):
    """Raised when downloaded file is corrupted or not a supported video."""


class GDriveDownloadError(GDriveError):
    """Raised when download fails."""


def is_gdrive_url(text: str) -> bool:
    """Check if the text contains a Google Drive file link."""
    if not text:
        return False
    return bool(GDRIVE_FILE_RE.search(text.strip().split()[0]))


def is_gdrive_folder_url(text: str) -> bool:
    """Check if the text contains a Google Drive folder link."""
    if not text:
        return False
    return bool(GDRIVE_FOLDER_RE.search(text.strip().split()[0]))


def extract_gdrive_id(text: str) -> str | None:
    """Extract Google Drive file ID from a URL."""
    if not text:
        return None
    match = GDRIVE_FILE_RE.search(text.strip().split()[0])
    return match.group(1) if match else None


def _download_via_ytdlp(
    url: str,
    output_dir: Path,
    file_id: str,
    progress: Any = None,
) -> Path | None:
    """Attempt download using yt-dlp's GoogleDrive extractor."""
    outtmpl = str(output_dir / f"gdrive_{file_id}_%(title).50s.%(ext)s")

    def progress_hook(d: dict[str, Any]) -> None:
        if progress and d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total > 0:
                pct = (downloaded / total) * 100.0
                mb_down = downloaded / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                progress.set("download", f"Mengunduh Google Drive: {pct:.0f}% ({mb_down:.1f}/{mb_tot:.1f} MB)")
            elif downloaded > 0:
                mb_down = downloaded / (1024 * 1024)
                progress.set("download", f"Mengunduh Google Drive ({mb_down:.1f} MB)...")

    ydl_opts = {
        "format": "best[height<=1080]/bestvideo+bestaudio/best",
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            p = Path(filename)
            if p.exists() and p.stat().st_size > 10000:
                return p
            # Check directory for matching prefix if extension changed during merge
            for f in output_dir.iterdir():
                if f.is_file() and f.name.startswith(f"gdrive_{file_id}") and f.suffix in config.SUPPORTED_EXTENSIONS:
                    if f.stat().st_size > 10000:
                        return f
    except Exception as exc:
        err_msg = str(exc).lower()
        if "permission" in err_msg or "access denied" in err_msg or "private" in err_msg or "login" in err_msg:
            raise GDrivePermissionError("Akses Google Drive ditolak. Pastikan izin akses diset ke 'Siapa saja yang memiliki link'.") from exc
        if "quota" in err_msg:
            raise GDriveQuotaError("Batas kuota unduh Google Drive terlampaui untuk file ini.") from exc
        LOGGER.debug("yt-dlp Google Drive extraction fallback: %s", exc)

    return None


def _download_via_direct_stream(
    file_id: str,
    output_dir: Path,
    progress: Any = None,
) -> Path:
    """Direct stream download from Google Drive with virus warning confirmation handling."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
    })

    base_url = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download"}

    if progress:
        progress.set("download", "Menghubungkan ke server Google Drive...")

    resp = session.get(base_url, params=params, stream=True, timeout=30)

    # Check if Google returned an HTML confirmation page (common for files > 100MB)
    content_type = resp.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        html = resp.text
        if "permission" in html.lower() or "access denied" in html.lower() or "need permission" in html.lower() or "sign in" in html.lower():
            raise GDrivePermissionError("File Google Drive bersifat privat. Ubah akses menjadi 'Siapa saja yang memiliki link'.")
        if "quota exceeded" in html.lower():
            raise GDriveQuotaError("Kuota unduh Google Drive terlampaui untuk file ini.")

        # Parse confirm token or action URL from HTML
        confirm_url = base_url
        confirm_params = dict(params)

        # Check for download form
        form_match = re.search(r'<form[^>]*id=["\']download-form["\'][^>]*action=["\']([^"\']+)["\']', html)
        if form_match:
            confirm_url = form_match.group(1).replace("&amp;", "&")
            for input_match in re.finditer(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', html):
                confirm_params[input_match.group(1)] = input_match.group(2)
        else:
            link_match = re.search(r'id=["\']uc-download-link["\'][^>]*href=["\']([^"\']+)["\']', html)
            if link_match:
                confirm_url = link_match.group(1).replace("&amp;", "&")
                confirm_params = {}
            else:
                confirm_token = re.search(r'confirm=([0-9a-zA-Z_-]+)', html)
                if confirm_token:
                    confirm_params["confirm"] = confirm_token.group(1)

        resp = session.get(confirm_url, params=confirm_params, stream=True, timeout=30)

    if resp.status_code == 403:
        raise GDrivePermissionError("Akses ditolak (HTTP 403). Pastikan link Google Drive diset ke 'Siapa saja yang memiliki link'.")
    if resp.status_code == 404:
        raise GDriveError("File Google Drive tidak ditemukan (HTTP 404). Periksa kembali link Anda.")
    if resp.status_code != 200:
        raise GDriveDownloadError(f"Gagal mengunduh dari Google Drive (Status HTTP {resp.status_code}).")

    # Extract filename from Content-Disposition header if available
    cd = resp.headers.get("Content-Disposition", "")
    filename = ""
    fn_match = re.search(r'filename\*?=["\']?(?:UTF-8[\'\"])?([^"\';\r\n]+)', cd)
    if fn_match:
        filename = fn_match.group(1).strip()

    if not filename:
        filename = f"gdrive_{file_id}.mp4"

    # Sanitize filename
    safe_name = re.sub(r'[\\/*?:"<>|]', "", filename)
    if not any(safe_name.lower().endswith(ext) for ext in config.SUPPORTED_EXTENSIONS):
        safe_name += ".mp4"

    out_path = output_dir / safe_name
    total_size = int(resp.headers.get("Content-Length", 0))

    downloaded = 0
    t_last_update = time.time()
    chunk_size = 1024 * 1024  # 1MB chunks

    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if progress and (now - t_last_update > 0.8):
                    t_last_update = now
                    mb_down = downloaded / (1024 * 1024)
                    if total_size > 0:
                        pct = (downloaded / total_size) * 100.0
                        mb_tot = total_size / (1024 * 1024)
                        progress.set("download", f"Mengunduh Google Drive: {pct:.0f}% ({mb_down:.1f}/{mb_tot:.1f} MB)")
                    else:
                        progress.set("download", f"Mengunduh Google Drive ({mb_down:.1f} MB)...")

    if not out_path.exists() or out_path.stat().st_size < 10000:
        out_path.unlink(missing_ok=True)
        raise GDriveDownloadError("File yang diunduh dari Google Drive kosong atau tidak lengkap.")

    return out_path


def download_gdrive_video(
    url: str,
    output_dir: Path,
    user_id: int,
    progress: Any = None,
) -> Path:
    """Download video from Google Drive with two-tier strategy and probe validation."""
    output_dir.mkdir(parents=True, exist_ok=True)
    file_id = extract_gdrive_id(url)
    if not file_id:
        raise GDriveError("Link Google Drive tidak valid atau ID file tidak ditemukan.")

    if progress:
        progress.set("download", "Mempersiapkan download dari Google Drive...")

    # Strategy 1: yt-dlp
    downloaded_path = None
    try:
        downloaded_path = _download_via_ytdlp(url, output_dir, file_id, progress)
    except (GDrivePermissionError, GDriveQuotaError):
        raise
    except Exception as exc:
        LOGGER.debug("yt-dlp failed on Google Drive: %s; trying direct stream", exc)

    # Strategy 2: Direct stream
    if not downloaded_path or not downloaded_path.exists():
        LOGGER.info("Falling back to direct stream download for Google Drive ID %s", file_id)
        downloaded_path = _download_via_direct_stream(file_id, output_dir, progress)

    # Validate file format with FFmpeg probe
    try:
        video_info = probe_video(downloaded_path)
        if video_info.duration <= 0:
            downloaded_path.unlink(missing_ok=True)
            raise GDriveInvalidFileError("File dari Google Drive bukan video yang valid (durasi 0).")
    except VideoEditError as exc:
        downloaded_path.unlink(missing_ok=True)
        raise GDriveInvalidFileError(f"File dari Google Drive bukan format video yang didukung: {exc}") from exc

    LOGGER.info(
        "Successfully downloaded Google Drive video for user %s: %s (%.1fs)",
        user_id,
        downloaded_path.name,
        video_info.duration,
    )
    return downloaded_path
