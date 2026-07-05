"""Whisper transcription wrapper."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config


class TranscriptionError(RuntimeError):
    """Raised when audio extraction or Whisper transcription fails."""


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    audio_path: Path
    srt_path: Path
    segments: list[TranscriptSegment]


def priority_command_prefix() -> list[str]:
    command: list[str] = []
    if config.USE_IONICE:
        command.extend(["ionice", "-c3"])
    command.extend(["nice", "-n", "19"])
    return command


def format_srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: list[TranscriptSegment], srt_path: Path) -> None:
    lines: list[str] = []
    index = 1
    for segment in segments:
        text = segment.text.strip()
        if not text or segment.end <= segment.start:
            continue
        lines.extend(
            [
                str(index),
                f"{format_srt_timestamp(segment.start)} --> {format_srt_timestamp(segment.end)}",
                text,
                "",
            ]
        )
        index += 1

    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text("\n".join(lines), encoding="utf-8")


def compact_transcript(segments: list[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{segment.start:.2f}-{segment.end:.2f}] {segment.text.strip()}"
        for segment in segments
        if segment.text.strip() and segment.end > segment.start
    )


def safe_cache_stem(video_path: Path) -> str:
    stat = video_path.stat()
    safe_stem = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in video_path.stem)
    return f"{safe_stem}_{stat.st_size}_{int(stat.st_mtime)}"


def segments_to_json(segments: list[TranscriptSegment]) -> list[dict[str, float | str]]:
    return [
        {"start": segment.start, "end": segment.end, "text": segment.text}
        for segment in segments
    ]


def segments_from_json(raw: Any) -> list[TranscriptSegment]:
    if not isinstance(raw, list):
        raise TranscriptionError("Transcript cache is not a list")
    segments: list[TranscriptSegment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip() and end > start:
            segments.append(TranscriptSegment(start=start, end=end, text=text.strip()))
    if not segments:
        raise TranscriptionError("Transcript cache contains no usable segments")
    return segments


class WhisperTranscriber:
    def __init__(
        self,
        model_name: str = config.WHISPER_MODEL,
        device: str = config.WHISPER_DEVICE,
        logger: logging.Logger | None = None,
        load_model: bool = True,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.logger = logger or logging.getLogger(__name__)
        self._model: Any | None = None
        if load_model:
            self.load_model()

    def load_model(self) -> None:
        if self._model is not None:
            return

        os.environ["OMP_NUM_THREADS"] = config.OMP_NUM_THREADS
        os.environ.setdefault("MKL_NUM_THREADS", "1")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

        try:
            from faster_whisper import WhisperModel
        except ModuleNotFoundError as exc:
            raise TranscriptionError(
                "faster-whisper is required; run: pip install faster-whisper"
            ) from exc

        self.logger.info(
            "Loading faster-whisper model '%s' on %s (int8 quantization)",
            self.model_name,
            self.device,
        )
        self._model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=config.WHISPER_COMPUTE_TYPE,
            cpu_threads=max(1, config.FFMPEG_THREADS),
            num_workers=1,
        )

    def extract_audio(self, video_path: Path, audio_path: Path) -> Path:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        command = priority_command_prefix() + [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-threads",
            str(config.FFMPEG_THREADS),
            str(audio_path),
        ]
        self.logger.info("Extracting audio: %s -> %s", video_path, audio_path)
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)
        if result.returncode != 0:
            raise TranscriptionError(
                "FFmpeg audio extraction failed:\n" + (result.stderr or result.stdout or "no output")
            )
        return audio_path

    def transcribe_audio(self, audio_path: Path) -> list[TranscriptSegment]:
        self.load_model()
        assert self._model is not None

        transcribe_kwargs: dict = {
            "beam_size": config.WHISPER_BEAM_SIZE,
            "vad_filter": config.WHISPER_VAD_FILTER,
        }

        if config.WHISPER_LANGUAGE:
            transcribe_kwargs["language"] = config.WHISPER_LANGUAGE

        self.logger.info(
            "Transcribing with faster-whisper: %s (beam=%s, vad=%s)%s",
            audio_path,
            config.WHISPER_BEAM_SIZE,
            config.WHISPER_VAD_FILTER,
            f", lang={config.WHISPER_LANGUAGE}" if config.WHISPER_LANGUAGE else "",
        )
        try:
            segments_gen, info = self._model.transcribe(
                str(audio_path), **transcribe_kwargs,
            )
            segments = list(segments_gen)
        except Exception as exc:
            raise TranscriptionError(f"faster-whisper transcription failed: {exc}") from exc

        if not segments:
            raise TranscriptionError("faster-whisper returned no transcript segments")

        # Confidence filtering: skip low-confidence / hallucinated segments
        filtered: list[TranscriptSegment] = []
        for s in segments:
            text = s.text.strip()
            if not text or s.end <= s.start:
                continue
            avg_logprob = getattr(s, "avg_logprob", 0)
            no_speech_prob = getattr(s, "no_speech_prob", 0)
            if avg_logprob < -1.0 or no_speech_prob > 0.6:
                self.logger.debug(
                    "Skipping low-confidence segment: logprob=%.2f no_speech=%.2f text=%r",
                    avg_logprob, no_speech_prob, text[:60],
                )
                continue
            filtered.append(TranscriptSegment(start=s.start, end=s.end, text=text))

        if not filtered:
            self.logger.warning(
                "All %d segments filtered by confidence; falling back to unfiltered",
                len(segments),
            )
            filtered = [
                TranscriptSegment(start=s.start, end=s.end, text=s.text.strip())
                for s in segments if s.text.strip() and s.end > s.start
            ]

        return filtered

    def transcribe_video(self, video_path: Path, work_dir: Path) -> TranscriptionResult:
        audio_path = work_dir / f"{video_path.stem}.wav"
        srt_path = work_dir / f"{video_path.stem}.srt"
        cache_path = config.TRANSCRIPT_CACHE_DIR / f"{safe_cache_stem(video_path)}.segments.json"

        if cache_path.exists():
            try:
                segments = segments_from_json(json.loads(cache_path.read_text(encoding="utf-8")))
                write_srt(segments, srt_path)
                self.logger.info("Loaded transcript cache: %s", cache_path)
                return TranscriptionResult(audio_path=audio_path, srt_path=srt_path, segments=segments)
            except (OSError, json.JSONDecodeError, TranscriptionError) as exc:
                self.logger.warning("Ignoring invalid transcript cache %s: %s", cache_path, exc)

        try:
            self.extract_audio(video_path, audio_path)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Unexpected audio extraction error: {exc}") from exc

        try:
            segments = self.transcribe_audio(audio_path)
            write_srt(segments, srt_path)
            config.TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(segments_to_json(segments), ensure_ascii=False),
                encoding="utf-8",
            )
            self.logger.info("Wrote transcript cache: %s", cache_path)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Unexpected transcription/SRT error: {exc}") from exc

        self.logger.info("Wrote transcript SRT: %s", srt_path)
        return TranscriptionResult(audio_path=audio_path, srt_path=srt_path, segments=segments)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcribe a video with Whisper")
    parser.add_argument("video", type=Path, help="Video file to transcribe")
    parser.add_argument("--work-dir", type=Path, default=config.TMP_DIR / "transcriber-test")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    transcriber = WhisperTranscriber()
    result = transcriber.transcribe_video(args.video, args.work_dir)
    print(f"SRT: {result.srt_path}")
    print(compact_transcript(result.segments))


if __name__ == "__main__":
    main()
