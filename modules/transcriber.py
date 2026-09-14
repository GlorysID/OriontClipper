"""Whisper transcription wrapper."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
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
    words: list[dict] | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    audio_path: Path
    srt_path: Path
    segments: list[TranscriptSegment]


def priority_command_prefix() -> list[str]:
    command: list[str] = []
    if os.name != "nt":
        if config.USE_IONICE and shutil.which("ionice"):
            command.extend(["ionice", "-c3"])
        if shutil.which("nice"):
            command.extend(["nice", "-n", "19"])
    return command


def clean_transcription_text(text: str) -> str:
    if not text:
        return ""
    return " ".join(text.strip().split())


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


# --- Transcript cache schema -------------------------------------------------
# Version 2 persists word-level timings (needed for karaoke ASS subtitles and
# for whisper-accurate clip boundary snapping) plus the model/language that
# produced them. Version 1 stored a bare JSON list of {start, end, text} and is
# now considered invalid: it is re-transcribed instead of silently degrading.
TRANSCRIPT_CACHE_VERSION = 2


def safe_cache_stem(video_path: Path) -> str:
    stat = video_path.stat()
    safe_stem = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in video_path.stem)
    return f"{safe_stem}_{stat.st_size}_{int(stat.st_mtime)}"


def _word_to_dict(word: Any) -> dict | None:
    """Normalize one word entry (faster-whisper object or dict) to a plain dict."""
    if isinstance(word, dict):
        text = word.get("word")
        start = word.get("start")
        end = word.get("end")
    else:
        text = getattr(word, "word", None)
        start = getattr(word, "start", None)
        end = getattr(word, "end", None)

    if not isinstance(text, str):
        return None
    text = clean_transcription_text(text)
    if not text:
        return None
    try:
        start_f = float(start)
        end_f = float(end)
    except (TypeError, ValueError):
        return None
    if start_f != start_f or end_f != end_f:  # NaN guard
        return None
    return {"word": text, "start": start_f, "end": end_f}


def _segment_words_to_json(segment: Any) -> list[dict] | None:
    raw = getattr(segment, "words", None)
    if raw is None and isinstance(segment, dict):
        raw = segment.get("words")
    if not raw or not isinstance(raw, list):
        return None
    words = [w for w in (_word_to_dict(item) for item in raw) if w]
    return words or None


def segments_to_json(
    segments: list[TranscriptSegment],
    model: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Serialize segments into the versioned transcript cache document."""
    payload: list[dict[str, Any]] = []
    for segment in segments:
        start = float(segment.start)
        end = float(segment.end)
        text = clean_transcription_text(str(segment.text))
        entry: dict[str, Any] = {
            "start": start,
            "end": end,
            "text": text,
            # Key must always be present: its absence marks a legacy (v1) cache.
            "words": _segment_words_to_json(segment),
        }
        payload.append(entry)

    return {
        "version": TRANSCRIPT_CACHE_VERSION,
        "model": model or config.WHISPER_MODEL,
        "language": language or "",
        "segments": payload,
    }


def _word_from_cache(item: Any) -> dict:
    word = _word_to_dict(item)
    if word is None:
        raise TranscriptionError("Transcript cache contains a malformed word entry")
    return word


def segments_from_json(
    raw: Any,
    expected_model: str | None = None,
    expected_language: str | None = None,
) -> list[TranscriptSegment]:
    """Parse a v2 cache document into TranscriptSegments.

    Raises TranscriptionError for any cache that is not fully valid (legacy v1,
    missing word data, wrong model) so the caller re-transcribes instead of
    silently degrading subtitle quality.
    """
    if not isinstance(raw, dict):
        raise TranscriptionError("Transcript cache is not a versioned object (legacy format)")

    version = raw.get("version")
    if not isinstance(version, int) or version < TRANSCRIPT_CACHE_VERSION:
        raise TranscriptionError(
            f"Transcript cache version {version!r} is older than {TRANSCRIPT_CACHE_VERSION}"
        )

    model = raw.get("model")
    if expected_model and isinstance(model, str) and model and model != expected_model:
        raise TranscriptionError(
            f"Transcript cache model {model!r} does not match active model {expected_model!r}"
        )

    cached_language = raw.get("language")
    cached_language = cached_language if isinstance(cached_language, str) else ""
    wanted = (expected_language or "").strip().lower()
    if wanted and wanted not in ("auto", "none") and cached_language and cached_language != wanted:
        raise TranscriptionError(
            f"Transcript cache language {cached_language!r} does not match requested {wanted!r}"
        )

    payload = raw.get("segments")
    if not isinstance(payload, list):
        raise TranscriptionError("Transcript cache is missing its segments list")

    segments: list[TranscriptSegment] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = clean_transcription_text(text)

        if "words" not in item:
            raise TranscriptionError("Transcript cache segment has no word timings (legacy format)")
        raw_words = item.get("words")
        words: list[dict] | None = None
        if raw_words:
            if not isinstance(raw_words, list):
                raise TranscriptionError("Transcript cache segment words is not a list")
            words = [_word_from_cache(w) for w in raw_words] or None

        if text and end > start:
            segments.append(TranscriptSegment(start=start, end=end, text=text, words=words))

    if not segments:
        raise TranscriptionError("Transcript cache contains no usable segments")
    if not any(segment.words for segment in segments):
        raise TranscriptionError("Transcript cache contains no word timings")
    return segments


class WhisperTranscriber:
    _cached_models: dict[tuple[str, str], Any] = {}

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
        # Language actually detected/used by the last transcribe_audio() call;
        # persisted alongside the cache so a stale-language cache is traceable.
        self.detected_language: str = ""
        self._model: Any | None = WhisperTranscriber._cached_models.get((model_name, device))
        if self._model is None and load_model:
            self.load_model()

    def load_model(self) -> None:
        if self._model is not None:
            return
        cached = WhisperTranscriber._cached_models.get((self.model_name, self.device))
        if cached is not None:
            self._model = cached
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
        WhisperTranscriber._cached_models[(self.model_name, self.device)] = self._model

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

    def transcribe_audio(
        self,
        audio_path: Path,
        language: str | None = None,
    ) -> list[TranscriptSegment]:
        self.load_model()
        assert self._model is not None

        transcribe_kwargs: dict = {
            "beam_size": config.WHISPER_BEAM_SIZE,
            "vad_filter": config.WHISPER_VAD_FILTER,
            "vad_parameters": dict(
                min_silence_duration_ms=500,
                speech_pad_ms=400,
            ),
            "word_timestamps": True,
            "task": "transcribe",
        }

        # Language selection / auto-detection:
        target_lang = language if language and language.lower() not in ("auto", "none") else (
            config.WHISPER_LANGUAGE if config.WHISPER_LANGUAGE and config.WHISPER_LANGUAGE.lower() not in ("auto", "none") else None
        )
        if target_lang:
            transcribe_kwargs["language"] = target_lang

        # Prompt selection based on target language:
        if target_lang == "id" or (not target_lang and config.WHISPER_LANGUAGE == "id"):
            transcribe_kwargs["initial_prompt"] = (
                "Percakapan santai bahasa Indonesia campur istilah teknologi, internet, dan bisnis: "
                "tools, Canva, software, framework, website, mindset, content creator, marketing, gadget, "
                "YouTube, online, AI, template, video, script, design, workflow, aplikasi, platform, dll."
            )
        elif target_lang == "en":
            transcribe_kwargs["initial_prompt"] = (
                "A crisp, clear transcript with proper capitalization, technical terms, and brand names: "
                "YouTube, Canva, tools, AI, software, framework, website, workflow, mindset, marketing."
            )
        else:
            # Universal multilingual prompt with brand names
            transcribe_kwargs["initial_prompt"] = (
                "Clean transcript with accurate terminology: YouTube, Canva, tools, AI, software, website, online."
            )

        self.logger.info(
            "Transcribing with faster-whisper: %s (beam=%s, vad=%s)%s",
            audio_path,
            config.WHISPER_BEAM_SIZE,
            config.WHISPER_VAD_FILTER,
            f", lang={target_lang}" if target_lang else " (auto-detect language)",
        )
        try:
            segments_gen, info = self._model.transcribe(
                str(audio_path), **transcribe_kwargs,
            )
            segments = list(segments_gen)
            self.detected_language = str(getattr(info, "language", "") or "")
            self.logger.info(
                "Whisper detected language: %s (prob=%.2f), segments=%d",
                getattr(info, "language", "unknown"),
                getattr(info, "language_probability", 0.0),
                len(segments),
            )
        except Exception as exc:
            raise TranscriptionError(f"faster-whisper transcription failed: {exc}") from exc

        if not segments:
            raise TranscriptionError("faster-whisper returned no transcript segments")

        # Confidence filtering: only skip extreme noise/silence
        filtered: list[TranscriptSegment] = []
        for s in segments:
            text = s.text.strip()
            if not text or s.end <= s.start:
                continue
            avg_logprob = getattr(s, "avg_logprob", 0)
            no_speech_prob = getattr(s, "no_speech_prob", 0)
            if avg_logprob < -2.0 and no_speech_prob > 0.90:
                self.logger.debug(
                    "Skipping low-confidence segment: logprob=%.2f no_speech=%.2f text=%r",
                    avg_logprob, no_speech_prob, text[:60],
                )
                continue
            seg_words = None
            if getattr(s, "words", None):
                seg_words = [
                    {"word": clean_transcription_text(w.word.strip()), "start": float(w.start), "end": float(w.end)}
                    for w in s.words
                    if getattr(w, "word", "").strip() and float(w.end) > float(w.start)
                ]
            filtered.append(
                TranscriptSegment(
                    start=s.start,
                    end=s.end,
                    text=clean_transcription_text(text),
                    words=seg_words,
                )
            )

        if not filtered:
            self.logger.warning(
                "All %d segments filtered by confidence; falling back to unfiltered",
                len(segments),
            )
            filtered = [
                TranscriptSegment(
                    start=s.start,
                    end=s.end,
                    text=clean_transcription_text(s.text.strip()),
                    words=[
                        {"word": clean_transcription_text(w.word.strip()), "start": float(w.start), "end": float(w.end)}
                        for w in getattr(s, "words", [])
                        if getattr(w, "word", "").strip() and float(w.end) > float(w.start)
                    ] if getattr(s, "words", None) else None,
                )
                for s in segments if s.text.strip() and s.end > s.start
            ]

        return filtered

    def transcribe_video(
        self,
        video_path: Path,
        work_dir: Path,
        language: str | None = None,
    ) -> TranscriptionResult:
        audio_path = work_dir / f"{video_path.stem}.wav"
        srt_path = work_dir / f"{video_path.stem}.srt"
        cache_path = config.TRANSCRIPT_CACHE_DIR / f"{safe_cache_stem(video_path)}.segments.json"

        if cache_path.exists():
            try:
                segments = segments_from_json(
                    json.loads(cache_path.read_text(encoding="utf-8")),
                    expected_model=self.model_name,
                    expected_language=language,
                )
                write_srt(segments, srt_path)
                self.logger.info(
                    "Loaded transcript cache: %s (segments=%d, word_timings=%s)",
                    cache_path,
                    len(segments),
                    sum(1 for s in segments if s.words),
                )
                return TranscriptionResult(audio_path=audio_path, srt_path=srt_path, segments=segments)
            except (OSError, json.JSONDecodeError, TranscriptionError) as exc:
                self.logger.warning(
                    "Ignoring invalid/stale transcript cache %s: %s (re-transcribing)",
                    cache_path,
                    exc,
                )

        try:
            self.extract_audio(video_path, audio_path)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Unexpected audio extraction error: {exc}") from exc

        try:
            segments = self.transcribe_audio(audio_path, language=language)
            write_srt(segments, srt_path)
            config.TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    segments_to_json(
                        segments,
                        model=self.model_name,
                        language=self.detected_language,
                    ),
                    ensure_ascii=False,
                ),
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
