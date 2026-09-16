"""FFmpeg video rendering for vertical clips."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import textwrap
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import config
from modules import file_manager
from modules.transcriber import TranscriptSegment, priority_command_prefix, write_srt


class VideoEditError(RuntimeError):
    """Raised when probing or rendering a clip fails."""


DEFAULT_HOOK_TEMPLATE: dict[str, Any] = {
    "text_transform": "upper",
    "max_line_chars": 20,
    "max_lines": 3,
    "display_seconds": 3,
    "fade_seconds": 0.35,
    "video": {
        "overlay_enabled": True,
        "width": 1080,
        "height": -2,
        "x": "(W-w)/2",
        "y": "(H-h)/2",
        "background_blur": 25,
        "background_blur_power": 5,
        "border_w": 0,
        "border_color": "#ffffff@0.70",
    },
    "hook": {
        "enabled": True,
        "font_file": "",
        "font_size": 82,
        "font_color": "white",
        "line_spacing": 12,
        "border_w": 2,
        "border_color": "black@0.95",
        "shadow_color": "black@0.65",
        "shadow_x": 3,
        "shadow_y": 3,
        "box": True,
        "box_color": "#101010@0.72",
        "box_border_w": 26,
        "box_radius": 18,
        "box_outline_w": 2,
        "box_outline_color": "#ffffff@0.22",
        "highlight_color": "#ffe600",
        "highlight_last_word": False,
        "x": "(w-text_w)/2",
        "y": "220",
    },
    "subtitle": {
        "enabled": True,
        "mode": "karaoke",
        "word_animation": "pop",
        "font_file": "",
        "font_name": "Impact",
        "font_size": 74,
        "font_color": "#ffffff",
        "highlight_color": "#ffe600",
        "outline_color": "#000000",
        "outline": 1.8,
        "shadow": 1.2,
        "shadow_color": "#000000",
        "border_style": 1,
        "back_color": "#000000@0.7",
        "alignment": 2,
        "margin_v": 420,
        "words_per_phrase": 4,
        "text_transform": "upper",
    },
}


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float

    @property
    def is_vertical(self) -> bool:
        return self.height >= self.width


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_attr_or_key(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def ffmpeg_filter_escape(value: str, escape_colon: bool = True) -> str:
    value = value.replace("\\", "\\\\")
    value = value.replace("'", "\\'")
    if escape_colon:
        value = value.replace(":", "\\:")
    return value


def drawtext_escape(value: str) -> str:
    value = " ".join(value.split())
    value = value.replace("\\", "\\\\")
    value = value.replace("'", "\\'")
    value = value.replace(":", "\\:")
    value = value.replace("%", "\\%")
    return value


def merge_template(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(default)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_template(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_hook_template() -> dict[str, Any]:
    if not config.HOOK_TEMPLATE_PATH.exists():
        return deepcopy(DEFAULT_HOOK_TEMPLATE)
    try:
        raw = json.loads(config.HOOK_TEMPLATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoEditError(f"Invalid hook template {config.HOOK_TEMPLATE_PATH}: {exc}") from exc
    if not isinstance(raw, dict):
        raise VideoEditError(f"Hook template must be a JSON object: {config.HOOK_TEMPLATE_PATH}")
    return merge_template(DEFAULT_HOOK_TEMPLATE, raw)


def load_named_template(name_or_path: str) -> dict[str, Any]:
    """Load a named template preset from hook_templates/ (e.g. 'capcut', 'hormozi') or path."""
    name = str(name_or_path or "").strip().lower()
    if not name:
        return load_hook_template()

    # Normalize name (strip .json if provided)
    clean_name = name[:-5] if name.endswith(".json") else name
    candidate_paths = [
        config.PROJECT_ROOT / "hook_templates" / f"{clean_name}.json",
        config.PROJECT_ROOT / name,
        Path(name),
    ]
    for p in candidate_paths:
        if p.exists() and p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return merge_template(DEFAULT_HOOK_TEMPLATE, raw)
            except Exception as exc:
                logging.getLogger(__name__).warning("Could not read template %s: %s", p, exc)
    return load_hook_template()


def template_value(template: dict[str, Any], section: str, key: str) -> Any:
    value = template.get(section, {}).get(key)
    if value is None:
        value = DEFAULT_HOOK_TEMPLATE[section][key]
    return value


def drawtext_option(name: str, value: Any, escape_value: bool = True) -> str:
    if isinstance(value, bool):
        value = 1 if value else 0
    value_text = str(value)
    if name in {"fontcolor", "boxcolor", "bordercolor", "shadowcolor"}:
        value_text = drawtext_color(value_text)
    if escape_value:
        value_text = ffmpeg_filter_escape(value_text)
    return f"{name}={value_text}"


def drawtext_filter(input_label: str, output_label: str, options: list[str]) -> str:
    return f"[{input_label}]drawtext=" + ":".join(options) + f"[{output_label}]"


def font_value_for_layer(template: dict[str, Any], section: str, default_font_path: Path) -> str:
    configured = str(template.get(section, {}).get("font_file") or "").strip()
    font_path = Path(configured) if configured else default_font_path
    if configured and not font_path.is_absolute():
        font_path = config.PROJECT_ROOT / font_path
    if not font_path.exists():
        raise VideoEditError(f"Configured {section} font file does not exist: {font_path}")
    return ffmpeg_filter_escape(str(font_path.resolve()))


def drawtext_color(value: str) -> str:
    text = str(value).strip()
    if text.startswith("#"):
        if "@" in text:
            hex_part, alpha = text.split("@", 1)
            return f"0x{hex_part[1:]}@{alpha}"
        return f"0x{text[1:]}"
    return text


def css_color_to_ass(value: Any, default: str) -> str:
    text = str(value or default).strip().lower()
    named = {
        "white": "#ffffff",
        "black": "#000000",
        "yellow": "#ffff00",
        "red": "#ff0000",
        "green": "#00ff00",
        "blue": "#0000ff",
    }
    text = named.get(text, text)
    if "@" in text:
        text = text.split("@", 1)[0]
    if text.startswith("0x"):
        text = text[2:]
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        text = default.lstrip("#")
    rr = text[0:2]
    gg = text[2:4]
    bb = text[4:6]
    return f"&H00{bb}{gg}{rr}&"


def bundled_subtitle_font_selected(template: dict[str, Any]) -> bool:
    """True when the bundled subtitle font should drive ASS rendering.

    The bundled TTF (assets/fonts/) is prioritized on every platform, but an
    explicit user font still wins: a non-empty subtitle.font_file, or a
    subtitle.font_name that differs from the template default, disables it.
    Falls back to DejaVu/Impact resolution when the bundled file is missing.
    """
    try:
        bundled_path = Path(config.BUNDLED_SUBTITLE_FONT_PATH)
    except AttributeError:
        return False
    if not bundled_path.exists():
        return False
    subtitle = template.get("subtitle", {})
    if str(subtitle.get("font_file") or "").strip():
        return False
    name = str(subtitle.get("font_name") or "").strip()
    default_name = str(DEFAULT_HOOK_TEMPLATE["subtitle"].get("font_name") or "").strip()
    return not name or name == default_name


def subtitle_font_name(template: dict[str, Any]) -> str:
    if bundled_subtitle_font_selected(template):
        return str(config.BUNDLED_SUBTITLE_FONT_NAME)
    subtitle = template.get("subtitle", {})
    explicit = str(subtitle.get("font_name") or "").strip()
    if explicit:
        return explicit
    font_file = str(subtitle.get("font_file") or "").strip()
    if font_file:
        return Path(font_file).stem
    if os.name == "nt":
        if Path(r"C:\Windows\Fonts\impact.ttf").exists():
            return "Impact"
        if Path(r"C:\Windows\Fonts\ariblk.ttf").exists():
            return "Arial Black"
        return "Arial"
    return config.SUBTITLE_FONT_NAME


def ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    centiseconds = int(round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, cs = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_escape_text(value: str) -> str:
    return value.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def format_subtitle_line(text: str, max_line_chars: int = 26, text_transform: str = "upper") -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""
    if text_transform == "upper":
        cleaned = cleaned.upper()
    elif text_transform == "lower":
        cleaned = cleaned.lower()
    elif text_transform == "title":
        cleaned = cleaned.title()

    if len(cleaned) <= max_line_chars:
        return ass_escape_text(cleaned)
    lines = textwrap.wrap(cleaned, width=max_line_chars, break_long_words=False, break_on_hyphens=False)
    escaped_lines = [ass_escape_text(l) for l in lines[:2]]
    return r"\N".join(escaped_lines)


def word_animation_tag(animation: str) -> str:
    mode = str(animation or "pop").strip().lower()
    if mode == "none":
        return ""
    if mode == "fade":
        return r"{\fad(70,70)}"
    if mode == "pulse":
        return r"{\fad(40,50)\fscx92\fscy92\t(0,90,\fscx116\fscy116)\t(90,190,\fscx100\fscy100)}"
    return r"{\fad(35,55)\fscx82\fscy82\t(0,85,\fscx122\fscy122)\t(85,180,\fscx100\fscy100)}"


def segment_word_timings(segment: TranscriptSegment) -> list[tuple[float, float, str]]:
    words = [word for word in segment.text.strip().split() if word.strip()]
    if not words or segment.end <= segment.start:
        return []

    duration = segment.end - segment.start
    weights = [max(0.65, min(2.2, len(word.strip(".,!?;:'\"()[]{}")) / 4.5)) for word in words]
    total = sum(weights) or float(len(words))
    cursor = segment.start
    timings: list[tuple[float, float, str]] = []
    for index, (word, weight) in enumerate(zip(words, weights)):
        if index == len(words) - 1:
            end = segment.end
        else:
            end = min(segment.end, cursor + duration * (weight / total))
        if end > cursor:
            timings.append((cursor, end, word))
        cursor = end
    return timings


def write_line_ass(segments: Iterable[TranscriptSegment], ass_path: Path, template: dict[str, Any]) -> Path:
    subtitle = template.get("subtitle", {})
    font_name = subtitle_font_name(template)
    font_size = int(float(subtitle.get("font_size", 64)))
    primary = css_color_to_ass(subtitle.get("font_color"), "#ffffff")
    outline_color = css_color_to_ass(subtitle.get("outline_color"), "#000000")
    back_color = css_color_to_ass(subtitle.get("back_color"), "#000000")
    outline = float(subtitle.get("outline", 5.0))
    shadow = float(subtitle.get("shadow", 2.5))
    alignment = int(float(subtitle.get("alignment", 2)))
    margin_v = int(float(subtitle.get("margin_v", 300)))
    border_style = int(float(subtitle.get("border_style", 1)))
    transform = str(subtitle.get("text_transform", "upper")).lower()
    max_chars = int(float(subtitle.get("max_line_chars", 26)))

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {config.TARGET_WIDTH}",
        f"PlayResY: {config.TARGET_HEIGHT}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Line,{font_name},{font_size},{primary},&H000000FF,{outline_color},{back_color},0,0,0,0,100,100,0,0,{border_style},{outline:g},{shadow:g},{alignment},80,80,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for segment in segments:
        if segment.end <= segment.start:
            continue
        text = format_subtitle_line(segment.text, max_line_chars=max_chars, text_transform=transform)
        if text:
            lines.append(f"Dialogue: 0,{ass_timestamp(segment.start)},{ass_timestamp(segment.end)},Line,,0,0,0,,{text}")

    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ass_path


def write_word_ass(segments: Iterable[TranscriptSegment], ass_path: Path, template: dict[str, Any]) -> Path:
    subtitle = template.get("subtitle", {})
    font_name = subtitle_font_name(template)
    font_size = int(float(subtitle.get("font_size", 64)))
    primary = css_color_to_ass(subtitle.get("font_color"), "#ffffff")
    outline_color = css_color_to_ass(subtitle.get("outline_color"), "#000000")
    back_color = css_color_to_ass(subtitle.get("back_color"), "#000000")
    outline = float(subtitle.get("outline", 5.0))
    shadow = float(subtitle.get("shadow", 2.5))
    alignment = int(float(subtitle.get("alignment", 2)))
    margin_v = int(float(subtitle.get("margin_v", 300)))
    border_style = int(float(subtitle.get("border_style", 1)))
    animation = str(subtitle.get("word_animation", "pop"))
    tag = word_animation_tag(animation)
    transform = str(subtitle.get("text_transform", "upper")).lower()

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {config.TARGET_WIDTH}",
        f"PlayResY: {config.TARGET_HEIGHT}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Word,{font_name},{font_size},{primary},&H000000FF,{outline_color},{back_color},0,0,0,0,100,100,0,0,{border_style},{outline:g},{shadow:g},{alignment},80,80,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for segment in segments:
        for start, end, word in segment_word_timings(segment):
            if transform == "upper":
                word = word.upper()
            elif transform == "lower":
                word = word.lower()
            text = f"{tag}{ass_escape_text(word)}"
            lines.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Word,,0,0,0,,{text}")

    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ass_path


def write_karaoke_ass(
    segments: Iterable[TranscriptSegment],
    ass_path: Path,
    template: dict[str, Any],
) -> Path:
    """Generate kinetic karaoke ASS subtitles (Alex Hormozi / CapCut style).

    Displays 3-5 words per phrase with the active spoken word highlighted and scaled up
    in bright gold/yellow, while inactive words remain solid white with black outline.
    """
    subtitle = template.get("subtitle", {})
    font_name = subtitle_font_name(template)
    font_size = int(float(subtitle.get("font_size", 74)))
    primary_color = css_color_to_ass(subtitle.get("font_color"), "#ffffff")
    highlight_ass = css_color_to_ass(subtitle.get("highlight_color"), "#ffe600")
    outline_color = css_color_to_ass(subtitle.get("outline_color"), "#000000")
    back_color = css_color_to_ass(subtitle.get("back_color"), "#000000")
    outline = float(subtitle.get("outline", 5.0))
    shadow = float(subtitle.get("shadow", 2.5))
    alignment = int(float(subtitle.get("alignment", 2)))
    margin_v = int(float(subtitle.get("margin_v", 420)))
    border_style = int(float(subtitle.get("border_style", 1)))
    transform = str(subtitle.get("text_transform", "upper")).lower()
    words_per_phrase = max(1, int(float(subtitle.get("words_per_phrase", 4))))

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {config.TARGET_WIDTH}",
        f"PlayResY: {config.TARGET_HEIGHT}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Karaoke,{font_name},{font_size},{primary_color},&H000000FF,{outline_color},{back_color},1,0,0,0,100,100,0,0,{border_style},{outline:g},{shadow:g},{alignment},80,80,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    all_words: list[dict[str, Any]] = []
    for segment in segments:
        if segment.end <= segment.start:
            continue
        seg_words = getattr(segment, "words", None)
        if seg_words and isinstance(seg_words, list):
            for w in seg_words:
                w_text = str(_get_attr_or_key(w, "word") or "").strip()
                w_s = _as_float(_get_attr_or_key(w, "start"), segment.start)
                w_e = _as_float(_get_attr_or_key(w, "end"), segment.end)
                if w_text and w_e > w_s:
                    all_words.append({"word": w_text, "start": w_s, "end": w_e})
        else:
            for w_s, w_e, w_text in segment_word_timings(segment):
                if w_text.strip() and w_e > w_s:
                    all_words.append({"word": w_text.strip(), "start": w_s, "end": w_e})

    # Group into punchy phrases
    phrases: list[list[dict[str, Any]]] = []
    current_phrase: list[dict[str, Any]] = []
    for w in all_words:
        current_phrase.append(w)
        has_punctuation = any(w["word"].endswith(p) for p in (".", "!", "?", ":", ";"))
        if len(current_phrase) >= words_per_phrase or has_punctuation:
            phrases.append(current_phrase)
            current_phrase = []
    if current_phrase:
        phrases.append(current_phrase)

    for phrase in phrases:
        phrase_len = len(phrase)
        for i, word_item in enumerate(phrase):
            w_start = word_item["start"]
            if i < phrase_len - 1:
                w_end = max(w_start + 0.05, phrase[i + 1]["start"])
            else:
                w_end = word_item["end"]
            if w_end <= w_start:
                continue

            phrase_parts: list[str] = []
            for j, other_word in enumerate(phrase):
                w_display = other_word["word"]
                if transform == "upper":
                    w_display = w_display.upper()
                elif transform == "lower":
                    w_display = w_display.lower()
                elif transform == "title":
                    w_display = w_display.title()
                w_display = ass_escape_text(w_display)

                if j == i:
                    phrase_parts.append(
                        f"{{\\c{highlight_ass}\\fscx114\\fscy114}}{w_display}{{\\fscx100\\fscy100\\c{primary_color}}}"
                    )
                else:
                    phrase_parts.append(w_display)

            dialogue_text = " ".join(phrase_parts)
            lines.append(
                f"Dialogue: 0,{ass_timestamp(w_start)},{ass_timestamp(w_end)},Karaoke,,0,0,0,,{dialogue_text}"
            )

    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ass_path


def _int_template(template: dict[str, Any], section: str, key: str, default: int) -> int:
    try:
        return int(float(template.get(section, {}).get(key, default)))
    except (TypeError, ValueError):
        return default


def build_base_video_chain(video_info: VideoInfo, template: dict[str, Any], target_w: int, target_h: int) -> str:
    video = template.get("video", {})
    overlay_enabled = bool(video.get("overlay_enabled", True)) and not video_info.is_vertical

    if video_info.is_vertical or not overlay_enabled:
        return (
            f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},setpts=PTS-STARTPTS[base]"
        )

    blur = max(0, _int_template(template, "video", "background_blur", 20))
    blur_power = max(1, _int_template(template, "video", "background_blur_power", 5))
    fg_width = max(120, _int_template(template, "video", "width", target_w))
    fg_height = _int_template(template, "video", "height", -2)
    if fg_height == 0:
        fg_height = -2
    border_w = max(0, _int_template(template, "video", "border_w", 0))
    border_color = drawtext_color(str(video.get("border_color", "black@0.0")))
    overlay_x = str(video.get("x", "(W-w)/2"))
    overlay_y = str(video.get("y", "(H-h)/2"))

    bg_filter = (
        f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h}"
    )
    if blur > 0:
        bg_filter += (
            f",boxblur={blur}:{blur_power}"
            f",colorlevels=romin=0.04:gomin=0.04:bomin=0.04:romax=0.78:gomax=0.78:bomax=0.78"
        )
    bg_filter += "[bg]"

    fg_filter = f"[0:v]scale={fg_width}:{fg_height}:force_original_aspect_ratio=decrease[fgraw]"
    if border_w > 0:
        fg_filter += (
            f";[fgraw]pad=iw+{border_w * 2}:ih+{border_w * 2}:{border_w}:{border_w}:"
            f"color={border_color}[fg]"
        )
    else:
        fg_filter += ";[fgraw]null[fg]"

    return f"{bg_filter};{fg_filter};[bg][fg]overlay={overlay_x}:{overlay_y},setpts=PTS-STARTPTS[base]"


def build_audio_filter_chain() -> str:
    """Final audio filter chain for rendered clips.

    loudnorm is intentionally the LAST stage: any future pre-filters (EQ,
    denoise) must run before normalization so the loudness target holds.
    """
    stages: list[str] = []
    loudnorm = str(getattr(config, "AUDIO_LOUDNORM_FILTER", "") or "").strip()
    if loudnorm:
        stages.append(loudnorm)
    return ",".join(stages)


def video_has_audio(video_path: Path) -> bool:
    """True when the source contains at least one decodable audio stream."""
    command = priority_command_prefix() + [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0 and "audio" in (result.stdout or "")


def probe_video(video_path: Path) -> VideoInfo:
    command = priority_command_prefix() + [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration:format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode != 0:
        raise VideoEditError("ffprobe failed:\n" + (result.stderr or result.stdout or "no output"))

    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration = _as_float(stream.get("duration")) or _as_float(data.get("format", {}).get("duration"))
    except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise VideoEditError(f"Could not parse ffprobe output: {exc}") from exc

    if width <= 0 or height <= 0 or duration <= 0:
        raise VideoEditError(f"Invalid video metadata: width={width}, height={height}, duration={duration}")

    return VideoInfo(width=width, height=height, duration=duration)


def build_clip_segments(segments: Iterable[Any], clip_start: float, clip_end: float) -> list[TranscriptSegment]:
    clip_segments: list[TranscriptSegment] = []
    for segment in segments:
        start = _as_float(_get_attr_or_key(segment, "start"))
        end = _as_float(_get_attr_or_key(segment, "end"))
        text = _get_attr_or_key(segment, "text")
        if not isinstance(text, str) or not text.strip() or end <= clip_start or start >= clip_end:
            continue

        relative_start = max(start, clip_start) - clip_start
        relative_end = min(end, clip_end) - clip_start
        if relative_end <= relative_start:
            continue

        raw_words = _get_attr_or_key(segment, "words")
        clip_words: list[dict[str, Any]] | None = None
        if raw_words and isinstance(raw_words, list):
            clip_words = []
            for w in raw_words:
                w_text = _get_attr_or_key(w, "word") or ""
                w_s = _as_float(_get_attr_or_key(w, "start"), start)
                w_e = _as_float(_get_attr_or_key(w, "end"), end)
                if w_e <= clip_start or w_s >= clip_end:
                    continue
                clip_words.append({
                    "word": str(w_text).strip(),
                    "start": max(w_s, clip_start) - clip_start,
                    "end": min(w_e, clip_end) - clip_start,
                })

        clip_segments.append(
            TranscriptSegment(
                start=relative_start,
                end=relative_end,
                text=text.strip(),
                words=clip_words if clip_words else None,
            )
        )
    return clip_segments


def prepare_hook_text(hook: str, template: dict[str, Any]) -> str:
    words = " ".join(hook.strip().upper().split())
    transform = str(template.get("text_transform", "upper")).lower()
    if transform == "lower":
        words = words.lower()
    elif transform == "title":
        words = words.title()
    elif transform in {"none", "original"}:
        words = " ".join(hook.strip().split())

    lines = textwrap.wrap(
        words,
        width=max(10, int(template.get("max_line_chars", config.HOOK_MAX_LINE_CHARS))),
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not lines:
        return words
    return "\n".join(lines[: max(1, int(template.get("max_lines", config.HOOK_MAX_LINES)))])


def write_hook_textfile(hook: str, path: Path, template: dict[str, Any]) -> Path:
    path.write_text(prepare_hook_text(hook, template), encoding="utf-8")
    return path


def _overlay_filter_section(
    current_label: str,
    overlay_input_label: str | None,
    display_seconds: float,
    fade_seconds: float,
    output_label: str,
) -> tuple[str, str] | None:
    """Build filter strings for a Pillow-based overlay using a separate -i input.

    The overlay PNG is passed as a separate FFmpeg input with -loop 1.
    overlay_input_label is like "[1:v]" or "[2:v]".

    Returns:
        (new_current_label, filter_section_string) or None if no overlay.
    """
    if overlay_input_label is None:
        return None

    enable_expr = f"between(t,0,{display_seconds:g})"
    result_label = output_label

    # Process overlay input: fix timestamps, ensure RGBA, fade in alpha
    filter_section = (
        f"{overlay_input_label}setpts=PTS-STARTPTS,format=rgba,"
        f"fade=in:st=0:d={fade_seconds:g}:alpha=1[{result_label}];"
        f"[{current_label}][{result_label}]overlay=0:0:"
        f"enable='{enable_expr}':format=auto:shortest=1[{result_label}]"
    )

    return (result_label, filter_section)


def build_filter_chain(
    video_info: VideoInfo,
    subtitle_path: Path,
    hook_overlay_path: Path | None,
    template: dict[str, Any],
    badge_overlay_path: Path | None = None,
) -> tuple[str, int]:
    """Build FFmpeg filter_complex string.

    Returns:
        (filter_chain_string, num_overlay_inputs)
        where num_overlay_inputs is the count of additional -i inputs needed
        (0, 1, or 2 for badge and hook overlays).
    """
    use_bundled_font = bundled_subtitle_font_selected(template)
    if use_bundled_font:
        font_path = Path(config.BUNDLED_SUBTITLE_FONT_PATH)
    else:
        font_path = Path(config.SUBTITLE_FONT_PATH)
    if not font_path.exists():
        if os.name == "nt":
            impact_path = Path(r"C:\Windows\Fonts\impact.ttf")
            if impact_path.exists():
                font_path = impact_path
            else:
                win_arial = Path(r"C:\Windows\Fonts\arialbd.ttf")
                if win_arial.exists():
                    font_path = win_arial
        if not font_path.exists():
            raise VideoEditError(
                f"Configured font file does not exist: {font_path}. Install fonts-dejavu-core or set SUBTITLE_FONT_PATH."
            )

    target_w = config.TARGET_WIDTH
    target_h = config.TARGET_HEIGHT
    subtitle_value = ffmpeg_filter_escape(str(subtitle_path.resolve()))
    display_seconds = float(template.get("display_seconds", config.HOOK_DISPLAY_SECONDS))
    fade_seconds = float(template.get("fade_seconds", config.HOOK_FADE_SECONDS))
    subtitle = template.get("subtitle", {})
    force_style = (
        f"FontName={subtitle_font_name(template)},"
        f"FontSize={int(float(subtitle.get('font_size', 64)))},"
        f"PrimaryColour={css_color_to_ass(subtitle.get('font_color'), '#ffffff')},"
        f"OutlineColour={css_color_to_ass(subtitle.get('outline_color'), '#000000')},"
        f"BackColour={css_color_to_ass(subtitle.get('back_color'), '#000000')},"
        f"BorderStyle={int(float(subtitle.get('border_style', 1)))},"
        f"Outline={float(subtitle.get('outline', 5.0)):g},"
        f"Shadow={float(subtitle.get('shadow', 2.5)):g},"
        f"Alignment={int(float(subtitle.get('alignment', 2)))},"
        f"MarginV={int(float(subtitle.get('margin_v', 300)))}"
    )

    base_chain = build_base_video_chain(video_info, template, target_w, target_h)

    # Compute overlay input indices
    # Input 0 = main video
    # Input 1 = badge overlay (if present)
    # Input 2 = hook overlay (if present)
    overlay_count = 0
    badge_input_label: str | None = None
    hook_input_label: str | None = None

    if badge_overlay_path is not None:
        overlay_count += 1
        badge_input_label = f"[{overlay_count}:v]"

    hook_enabled = bool(template.get("hook", {}).get("enabled", True))
    if hook_overlay_path is not None and hook_enabled:
        overlay_count += 1
        hook_input_label = f"[{overlay_count}:v]"

    # Build filter graph
    filter_steps: list[str] = []
    current_label = "base"

    # Subtitle (always uses ASS with PlayResX/Y)
    if bool(template.get("subtitle", {}).get("enabled", True)):
        fontsdir_opt = ""
        if use_bundled_font:
            # libass must scan the bundled dir or it silently falls back to a
            # system font when "Montserrat ExtraBold" is not installed.
            fdir = ffmpeg_filter_escape(str(font_path.parent.resolve()))
            fontsdir_opt = f":fontsdir='{fdir}'"
        elif os.name == "nt" and Path(r"C:\Windows\Fonts").is_dir():
            fdir = ffmpeg_filter_escape(r"C:\Windows\Fonts")
            fontsdir_opt = f":fontsdir='{fdir}'"
        elif font_path.exists() and font_path.parent.is_dir():
            fdir = ffmpeg_filter_escape(str(font_path.parent.resolve()))
            fontsdir_opt = f":fontsdir='{fdir}'"

        if subtitle_path.suffix.lower() == ".ass":
            filter_steps.append(f"[{current_label}]subtitles='{subtitle_value}'{fontsdir_opt}[subbed]")
        else:
            filter_steps.append(f"[{current_label}]subtitles='{subtitle_value}':force_style='{force_style}'{fontsdir_opt}[subbed]")
        current_label = "subbed"

    # Badge overlay
    badge_result = _overlay_filter_section(
        current_label, badge_input_label,
        display_seconds, fade_seconds,
        "badged",
    )
    if badge_result is not None:
        new_label, section = badge_result
        filter_steps.append(section)
        current_label = new_label

    # Hook overlay
    hook_result = _overlay_filter_section(
        current_label, hook_input_label,
        display_seconds, fade_seconds,
        "out",
    )
    if hook_result is not None:
        new_label, section = hook_result
        filter_steps.append(section)
        current_label = new_label

    # If no overlay was added, pass through to [out]
    if current_label != "out":
        filter_steps.append(f"[{current_label}]null[out]")

    subtitle_chain = ";".join(filter_steps)
    return f"{base_chain};{subtitle_chain}", overlay_count


class VideoEditor:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)

    def render_clip(
        self,
        video_path: Path,
        segments: list,
        moment: dict[str, Any] | ClipMoment,
        clip_index: int,
        work_dir: Path,
        video_info: VideoInfo | None = None,
        user_template: dict[str, Any] | None = None,
    ) -> Path:
        start = _as_float(_get_attr_or_key(moment, "start"))
        end = _as_float(_get_attr_or_key(moment, "end"))
        hook = _get_attr_or_key(moment, "hook")
        if not isinstance(hook, str) or not hook.strip() or end <= start:
            raise VideoEditError(f"Invalid clip moment: {moment}")

        video_info = video_info or probe_video(video_path)
        if end > video_info.duration:
            raise VideoEditError(
                f"Clip end {end:.2f}s exceeds video duration {video_info.duration:.2f}s"
            )

        clip_segments = build_clip_segments(segments, start, end)
        if not clip_segments:
            raise VideoEditError(f"No transcript segments overlap clip {clip_index}")

        if user_template is not None:
            hook_template = merge_template(DEFAULT_HOOK_TEMPLATE, user_template)
        else:
            hook_template = load_hook_template()
        subtitle_mode = str(hook_template.get("subtitle", {}).get("mode", "karaoke")).strip().lower()
        if subtitle_mode in ("karaoke", "kinetic", "hormozi"):
            subtitle_path = write_karaoke_ass(
                clip_segments,
                work_dir / f"clip{clip_index}_karaoke.ass",
                hook_template,
            )
        elif subtitle_mode == "word":
            subtitle_path = write_word_ass(
                clip_segments,
                work_dir / f"clip{clip_index}_words.ass",
                hook_template,
            )
        else:
            subtitle_path = write_line_ass(
                clip_segments,
                work_dir / f"clip{clip_index}_line.ass",
                hook_template,
            )
        hook_text_path = write_hook_textfile(
            hook, work_dir / f"clip{clip_index}_hook.txt", hook_template
        )

        # Render overlay PNGs (hook only) using Pillow
        from modules.overlay_renderer import render_hook

        hook_overlay_path = render_hook(
            hook_text_path.read_text(encoding="utf-8"),
            hook_template,
            work_dir,
        )

        duration = end - start
        final_path = file_manager.unique_destination(
            config.OUTPUT_DIR, f"{video_path.stem}_clip{clip_index}.mp4"
        )
        tmp_output = work_dir / f"{final_path.stem}.tmp.mp4"

        filter_chain, overlay_count = build_filter_chain(
            video_info, subtitle_path,
            hook_overlay_path,
            hook_template,
        )

        command = priority_command_prefix() + [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(video_path),
        ]

        # Add overlay inputs (must come after main video, before -filter_complex)
        if hook_overlay_path is not None:
            command.extend(["-loop", "1", "-i", str(hook_overlay_path)])

        command.extend([
            "-filter_complex",
            filter_chain,
            "-map",
            "[out]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            str(config.X264_PRESET),
            "-crf",
            str(config.X264_CRF),
            "-maxrate",
            str(config.X264_MAXRATE),
            "-bufsize",
            str(config.X264_BUFSIZE),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-threads",
            str(config.X264_THREADS),
        ])

        # Audio filters are only safe when the source actually has audio;
        # -af on a stream-less mapping would abort the render.
        audio_chain = build_audio_filter_chain()
        if audio_chain:
            if video_has_audio(video_path):
                command.extend(["-af", audio_chain])
            else:
                self.logger.info(
                    "Clip %s: source has no audio stream; skipping audio filters", clip_index
                )
        command.append(str(tmp_output))

        self.logger.info(
            "Rendering clip %s: %.2f-%.2fs -> %s", clip_index, start, end, final_path
        )
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=600)
        if result.returncode != 0:
            raise VideoEditError(
                f"FFmpeg render failed for clip {clip_index}:\n"
                + (result.stderr or result.stdout or "no output")
            )

        shutil.move(str(tmp_output), str(final_path))
        return final_path

    def render_clips(
        self,
        video_path: Path,
        segments: Iterable[Any],
        moments: Iterable[Any],
        work_dir: Path,
    ) -> list[Path]:
        video_info = probe_video(video_path)
        self.logger.info(
            "Video metadata: %sx%s, %.2fs, vertical=%s",
            video_info.width,
            video_info.height,
            video_info.duration,
            video_info.is_vertical,
        )

        # The same transcript segments are needed for every clip; materialize once.
        segment_list = list(segments)
        output_paths: list[Path] = []
        for index, moment in enumerate(moments, start=1):
            try:
                output_paths.append(
                    self.render_clip(video_path, segment_list, moment, index, work_dir, video_info)
                )
            except VideoEditError:
                self.logger.exception("Clip %s failed and will be skipped", index)
                continue

        if not output_paths:
            raise VideoEditError("No clips rendered successfully")
        return output_paths
