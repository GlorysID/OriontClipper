"""File safety helpers for ContentClipper."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import config


def ensure_directories() -> None:
    """Create all runtime directories if they do not exist."""
    for directory in (
        config.INPUT_DIR,
        config.OUTPUT_DIR,
        config.PROCESSED_DIR,
        config.FAILED_DIR,
        config.TMP_DIR,
        config.TRANSCRIPT_CACHE_DIR,
        config.LOG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def is_supported_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in config.SUPPORTED_EXTENSIONS


def wait_for_file_stable(
    path: Path,
    interval_s: float = config.FILE_STABILITY_INTERVAL_S,
    checks: int = config.FILE_STABILITY_CHECKS,
    logger: logging.Logger | None = None,
) -> bool:
    """Return True when file size is unchanged across repeated checks."""
    if checks < 2:
        checks = 2

    previous_size: int | None = None
    stable_count = 0

    while stable_count < checks - 1:
        if not path.exists() or not path.is_file():
            if logger:
                logger.warning("File disappeared before it became stable: %s", path)
            return False

        current_size = path.stat().st_size
        if previous_size is not None and current_size == previous_size:
            stable_count += 1
        else:
            stable_count = 0
            previous_size = current_size

        time.sleep(interval_s)

    return True


def unique_destination(destination_dir: Path, filename: str) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    candidate = destination_dir / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    for index in range(1, 1000):
        candidate = destination_dir / f"{stem}_{timestamp}_{index}{suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not find unique destination for {filename}")


def move_to_directory(source_path: Path, destination_dir: Path) -> Path:
    """Move source into destination_dir without overwriting existing files."""
    destination = unique_destination(destination_dir, source_path.name)
    shutil.move(str(source_path), str(destination))
    return destination


def move_to_processed(source_path: Path) -> Path:
    return move_to_directory(source_path, config.PROCESSED_DIR)


def move_to_failed(source_path: Path, reason: str) -> tuple[Path | None, Path]:
    """Move a failed source and write a sibling .log reason file."""
    config.FAILED_DIR.mkdir(parents=True, exist_ok=True)

    moved_path: Path | None = None
    log_base_name = f"{source_path.stem}.log"

    if source_path.exists():
        moved_path = move_to_directory(source_path, config.FAILED_DIR)
        log_base_name = f"{moved_path.stem}.log"

    log_path = unique_destination(config.FAILED_DIR, log_base_name)
    log_path.write_text(reason.strip() + "\n", encoding="utf-8")
    return moved_path, log_path


def make_job_tmp_dir(source_path: Path) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    safe_stem = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in source_path.stem)
    job_tmp_dir = config.TMP_DIR / f"{safe_stem}_{timestamp}"
    job_tmp_dir.mkdir(parents=True, exist_ok=False)
    return job_tmp_dir


def cleanup_path(path: Path, logger: logging.Logger | None = None) -> None:
    if not path.exists():
        return
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        if logger:
            logger.warning("Could not clean up %s: %s", path, exc)
