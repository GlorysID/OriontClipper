"""9Router/OpenAI-compatible transcript analysis."""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import config


SYSTEM_PROMPT = (
    "Kamu adalah editor video viral. Kamu HANYA mengembalikan JSON valid, "
    "tanpa markdown, tanpa penjelasan."
)

USER_PROMPT_TEMPLATE = """Analisis transkrip video ini (format [start-end] text per baris).
Pilih maksimal 3 momen paling kontroversial/menarik untuk dijadikan klip pendek (durasi ideal per klip 20-90 detik).
Timestamp start/end harus tetap memakai detik global sesuai transkrip.
Kembalikan HANYA JSON object valid dengan format:
{{"clips": [{{"start": <detik_float>, "end": <detik_float>, "hook": "<kalimat hook singkat, max 8 kata>"}}]}}

Transkrip:
{transcript}
"""


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


def validate_moments(raw: Any, video_duration_s: float) -> list[ClipMoment]:
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
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue

        hook = item.get("hook")
        if not isinstance(hook, str):
            continue
        hook = " ".join(hook.strip().split())

        duration = end - start
        if start < 0 or start >= end:
            continue
        if duration < config.MIN_CLIP_DURATION_S or duration > config.MAX_CLIP_DURATION_S:
            continue
        if end > video_duration_s:
            continue
        if not hook:
            continue

        moments.append(ClipMoment(start=start, end=end, hook=hook))
        if len(moments) >= config.MAX_CLIPS:
            break

    return moments


def transcript_text_from_segments(segments: Iterable[Any]) -> str:
    lines: list[str] = []
    for segment in segments:
        if isinstance(segment, dict):
            start = segment.get("start")
            end = segment.get("end")
            text = segment.get("text")
        else:
            start = getattr(segment, "start", None)
            end = getattr(segment, "end", None)
            text = getattr(segment, "text", None)

        try:
            start_f = float(start)
            end_f = float(end)
        except (TypeError, ValueError):
            continue
        if not isinstance(text, str) or not text.strip() or end_f <= start_f:
            continue
        lines.append(f"[{start_f:.2f}-{end_f:.2f}] {text.strip()}")
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


def dedupe_moments(moments: list[ClipMoment]) -> list[ClipMoment]:
    deduped: list[ClipMoment] = []
    for moment in sorted(moments, key=lambda item: (item.start, item.end)):
        overlaps_existing = any(
            max(moment.start, existing.start) < min(moment.end, existing.end)
            for existing in deduped
        )
        if not overlaps_existing:
            deduped.append(moment)
        if len(deduped) >= config.MAX_CLIPS:
            break
    return deduped


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

    def analyze_segments(self, segments: Iterable[Any], video_duration_s: float) -> list[ClipMoment]:
        transcript = transcript_text_from_segments(segments)
        if not transcript:
            raise AIAnalyzerError("Transcript is empty; cannot analyze clips")
        return self.analyze_transcript_text(transcript, video_duration_s)

    def analyze_transcript_text(self, transcript: str, video_duration_s: float) -> list[ClipMoment]:
        chunks = split_transcript_text(transcript)
        if len(chunks) == 1:
            return self._analyze_transcript_chunk(chunks[0], video_duration_s)

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
                collected.extend(self._analyze_transcript_chunk(chunk, video_duration_s))
            except AIAnalyzerError as exc:
                last_error = exc
                self.logger.warning("AI chunk %s/%s failed: %s", index, len(chunks), exc)
                continue

        moments = dedupe_moments(collected)
        if moments:
            self.logger.info("AI selected %s valid clip moment(s) after chunking", len(moments))
            return moments
        raise AIAnalyzerError(f"All AI transcript chunks failed or returned no valid clips: {last_error}")

    def _analyze_transcript_chunk(self, transcript: str, video_duration_s: float) -> list[ClipMoment]:
        response_format_enabled = True
        last_error: Exception | None = None

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                content = self._request_chat_completion(transcript, response_format_enabled)
                raw = parse_json_content(content)
                moments = validate_moments(raw, video_duration_s)
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

    def _request_chat_completion(self, transcript: str, response_format_enabled: bool) -> str:
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise AIAnalyzerError("requests is required; run: pip install -r requirements.txt") from exc

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(transcript=transcript)},
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
