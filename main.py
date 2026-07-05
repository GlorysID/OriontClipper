"""ContentClipper entry point."""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import config
from modules import file_manager
from modules.ai_analyzer import AIAnalyzer, AIAnalyzerError, moments_to_json
from modules.transcriber import TranscriptionError, WhisperTranscriber
from modules.video_editor import VideoEditError, VideoEditor, probe_video


LOGGER = logging.getLogger("contentclipper")


@dataclass(frozen=True)
class ProcessingContext:
    watch_only: bool
    dry_run: bool
    transcriber: WhisperTranscriber | None = None
    analyzer: AIAnalyzer | None = None
    editor: VideoEditor | None = None


def setup_logging() -> None:
    file_manager.ensure_directories()
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
    )

    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(stream_handler)


def build_processing_context(args: argparse.Namespace) -> ProcessingContext:
    if args.watch_only:
        LOGGER.info("Starting in watch-only mode; no transcription or rendering will run")
        return ProcessingContext(watch_only=True, dry_run=args.dry_run)

    # Whisper is loaded once during startup so every queued file reuses the same model.
    transcriber = WhisperTranscriber(logger=LOGGER)
    analyzer = AIAnalyzer(logger=LOGGER)
    editor = None if args.dry_run else VideoEditor(logger=LOGGER)
    if args.dry_run:
        LOGGER.info("Starting in dry-run mode; sources will not be moved or rendered")
    return ProcessingContext(
        watch_only=False,
        dry_run=args.dry_run,
        transcriber=transcriber,
        analyzer=analyzer,
        editor=editor,
    )


def mark_failed(source_path: Path, stage: str, exc: Exception, dry_run: bool) -> None:
    reason = f"Stage '{stage}' failed for {source_path}: {exc}"
    LOGGER.error(reason)
    if dry_run:
        LOGGER.info("Dry-run mode: leaving failed source in place: %s", source_path)
        return

    try:
        moved_path, log_path = file_manager.move_to_failed(source_path, reason)
        LOGGER.info("Moved failed source to %s; wrote reason log %s", moved_path, log_path)
    except OSError:
        LOGGER.exception("Could not move failed source to failed/: %s", source_path)


def process_video_file(source_path: Path, context: ProcessingContext) -> None:
    if context.watch_only:
        LOGGER.info("Stable video detected: %s", source_path)
        return

    if context.transcriber is None or context.analyzer is None:
        raise RuntimeError("Processing context is missing transcriber/analyzer")

    try:
        work_dir = file_manager.make_job_tmp_dir(source_path)
    except OSError as exc:
        mark_failed(source_path, "tmp_setup", exc, context.dry_run)
        return

    try:
        try:
            transcription = context.transcriber.transcribe_video(source_path, work_dir)
        except TranscriptionError as exc:
            mark_failed(source_path, "transcribe", exc, context.dry_run)
            return

        try:
            video_info = probe_video(source_path)
        except VideoEditError as exc:
            mark_failed(source_path, "probe_video", exc, context.dry_run)
            return

        try:
            moments = context.analyzer.analyze_segments(
                transcription.segments, video_info.duration
            )
        except AIAnalyzerError as exc:
            mark_failed(source_path, "ai_analyze", exc, context.dry_run)
            return

        if context.dry_run:
            LOGGER.info("Dry-run clip moments for %s:\n%s", source_path, moments_to_json(moments))
            print(moments_to_json(moments), flush=True)
            return

        if context.editor is None:
            raise RuntimeError("Processing context is missing video editor")

        try:
            output_paths = context.editor.render_clips(
                source_path, transcription.segments, moments, work_dir
            )
        except VideoEditError as exc:
            mark_failed(source_path, "render", exc, context.dry_run)
            return

        try:
            processed_path = file_manager.move_to_processed(source_path)
        except OSError:
            LOGGER.exception("Rendered clips but could not move source to processed/: %s", source_path)
            return

        LOGGER.info(
            "Completed %s -> %s clip(s); source moved to %s",
            source_path,
            len(output_paths),
            processed_path,
        )
    finally:
        file_manager.cleanup_path(work_dir, LOGGER)


def build_video_event_handler(
    pattern_handler_class: type[Any],
    work_queue: queue.Queue[Path],
    queued_paths: set[Path],
    lock: threading.Lock,
) -> Any:
    class VideoEventHandler(pattern_handler_class):
        def __init__(self) -> None:
            super().__init__(patterns=config.WATCH_PATTERNS, ignore_directories=True)

        def on_created(self, event: Any) -> None:
            self._enqueue(Path(event.src_path))

        def on_moved(self, event: Any) -> None:
            self._enqueue(Path(event.dest_path))

        def _enqueue(self, path: Path) -> None:
            path = path.resolve()
            with lock:
                if path in queued_paths:
                    LOGGER.info("Already queued, skipping duplicate event: %s", path)
                    return
                queued_paths.add(path)

            LOGGER.info("Queued candidate video: %s", path)
            work_queue.put(path)

    return VideoEventHandler()


def worker_loop(
    work_queue: queue.Queue[Path],
    stop_event: threading.Event,
    queued_paths: set[Path],
    lock: threading.Lock,
    context: ProcessingContext,
) -> None:
    while not stop_event.is_set():
        try:
            path = work_queue.get(timeout=1)
        except queue.Empty:
            continue

        try:
            if not file_manager.is_supported_video(path):
                LOGGER.info("Skipping unsupported or missing file: %s", path)
                continue

            LOGGER.info("Waiting for file to become stable: %s", path)
            stable = file_manager.wait_for_file_stable(path, logger=LOGGER)
            if stable:
                process_video_file(path, context)
            else:
                LOGGER.warning("File did not become stable: %s", path)
        except Exception:
            LOGGER.exception("Unhandled error while handling queued file: %s", path)
        finally:
            with lock:
                queued_paths.discard(path)
            work_queue.task_done()


def enqueue_existing_inputs(
    work_queue: queue.Queue[Path], queued_paths: set[Path], lock: threading.Lock
) -> None:
    for path in sorted(config.INPUT_DIR.iterdir()):
        if not file_manager.is_supported_video(path):
            continue
        resolved = path.resolve()
        with lock:
            if resolved in queued_paths:
                continue
            queued_paths.add(resolved)
        LOGGER.info("Queued existing input video: %s", resolved)
        work_queue.put(resolved)


def run_watch(context: ProcessingContext) -> None:
    try:
        from watchdog.events import PatternMatchingEventHandler
        from watchdog.observers import Observer
    except ModuleNotFoundError as exc:
        raise RuntimeError("watchdog is required; run: pip install -r requirements.txt") from exc

    LOGGER.info("Starting ContentClipper watcher in %s", config.INPUT_DIR)

    work_queue: queue.Queue[Path] = queue.Queue()
    queued_paths: set[Path] = set()
    lock = threading.Lock()
    stop_event = threading.Event()

    worker = threading.Thread(
        target=worker_loop,
        name="content-worker",
        args=(work_queue, stop_event, queued_paths, lock, context),
        daemon=True,
    )
    worker.start()

    enqueue_existing_inputs(work_queue, queued_paths, lock)

    handler = build_video_event_handler(PatternMatchingEventHandler, work_queue, queued_paths, lock)
    observer = Observer()
    observer.schedule(handler, str(config.INPUT_DIR), recursive=False)
    observer.start()

    def handle_shutdown(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s, shutting down", signum)
        stop_event.set()
        observer.stop()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        while observer.is_alive() and not stop_event.is_set():
            time.sleep(1)
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=10)
        worker.join(timeout=10)
        LOGGER.info("ContentClipper watcher stopped")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ContentClipper folder watcher")
    parser.add_argument(
        "--watch-only",
        action="store_true",
        help="Watch input and log stable files without transcription/rendering.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run transcription and AI analysis, print clip moments, and skip render/move.",
    )
    parser.add_argument(
        "--once",
        type=Path,
        help="Process one video path and exit instead of starting watchdog.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()
    context = build_processing_context(args)

    if args.once:
        source_path = args.once.resolve()
        if not file_manager.is_supported_video(source_path):
            raise SystemExit(f"Unsupported or missing video file: {source_path}")
        LOGGER.info("Waiting for one-shot file to become stable: %s", source_path)
        if not file_manager.wait_for_file_stable(source_path, logger=LOGGER):
            raise SystemExit(f"File did not become stable: {source_path}")
        process_video_file(source_path, context)
        return

    run_watch(context)


if __name__ == "__main__":
    main()
