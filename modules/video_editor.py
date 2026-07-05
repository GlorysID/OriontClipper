"""FFmpeg video rendering for vertical clips."""

from __future__ import annotations

import json
import logging
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
        "background_blur": 20,
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
        "border_w": 7,
        "border_color": "black@0.95",
        "shadow_color": "black@0.65",
        "shadow_x": 4,
        "shadow_y": 4,
        "box": True,
        "box_color": "#101010@0.72",
        "box_border_w": 26,
        "x": "(w-text_w)/2",
        "y": "230",
    },
    "subtitle": {
        "enabled": True,
        "mode": "line",
        "word_animation": "pop",
        "font_file": "",
        "font_name": "DejaVu Sans Bold",
        "font_size": 46,
        "font_color": "#fff200",
        "outline_color": "#000000",
        "outline": 2,
        "shadow": 0,
        "border_style": 1,
        "back_color": "#000000@0.0",
        "alignment": 2,
        "margin_v": 60,
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


def subtitle_font_name(template: dict[str, Any]) -> str:
    subtitle = template.get("subtitle", {})
    explicit = str(subtitle.get("font_name") or "").strip()
    if explicit:
        return explicit
    font_file = str(subtitle.get("font_file") or "").strip()
    if font_file:
        return Path(font_file).stem
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


def write_word_ass(segments: Iterable[TranscriptSegment], ass_path: Path, template: dict[str, Any]) -> Path:
    subtitle = template.get("subtitle", {})
    font_name = subtitle_font_name(template)
    font_size = int(float(subtitle.get("font_size", 52)))
    primary = css_color_to_ass(subtitle.get("font_color"), "#fff200")
    outline_color = css_color_to_ass(subtitle.get("outline_color"), "#000000")
    back_color = css_color_to_ass(subtitle.get("back_color"), "#000000")
    outline = float(subtitle.get("outline", 3))
    shadow = float(subtitle.get("shadow", 0))
    alignment = int(float(subtitle.get("alignment", 2)))
    margin_v = int(float(subtitle.get("margin_v", 64)))
    border_style = int(float(subtitle.get("border_style", 1)))
    animation = str(subtitle.get("word_animation", "pop"))
    tag = word_animation_tag(animation)

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
        f"Style: Word,{font_name},{font_size},{primary},&H000000FF,{outline_color},{back_color},1,0,0,0,100,100,0,0,{border_style},{outline:g},{shadow:g},{alignment},80,80,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for segment in segments:
        for start, end, word in segment_word_timings(segment):
            text = f"{tag}{ass_escape_text(word)}"
            lines.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Word,,0,0,0,,{text}")

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
        bg_filter += f",boxblur={blur}:{blur_power}"
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
        if relative_end > relative_start:
            clip_segments.append(
                TranscriptSegment(start=relative_start, end=relative_end, text=text.strip())
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
    badge_overlay_path: Path | None,
    template: dict[str, Any],
) -> tuple[str, int]:
    """Build FFmpeg filter_complex string.

    Returns:
        (filter_chain_string, num_overlay_inputs)
        where num_overlay_inputs is the count of additional -i inputs needed
        (0, 1, or 2 for badge and hook overlays).
    """
    font_path = Path(config.SUBTITLE_FONT_PATH)
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
        f"FontSize={int(float(subtitle.get('font_size', 46)))},"
        f"PrimaryColour={css_color_to_ass(subtitle.get('font_color'), '#fff200')},"
        f"OutlineColour={css_color_to_ass(subtitle.get('outline_color'), '#000000')},"
        f"BackColour={css_color_to_ass(subtitle.get('back_color'), '#000000')},"
        f"BorderStyle={int(float(subtitle.get('border_style', 1)))},"
        f"Outline={float(subtitle.get('outline', 2)):g},"
        f"Shadow={float(subtitle.get('shadow', 0)):g},"
        f"Alignment={int(float(subtitle.get('alignment', 2)))},"
        f"MarginV={int(float(subtitle.get('margin_v', 60)))}"
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

    # Subtitle (unchanged — uses ASS / SRT subtitles filter)
    if bool(template.get("subtitle", {}).get("enabled", True)):
        if subtitle_path.suffix.lower() == ".ass":
            filter_steps.append(f"[{current_label}]subtitles='{subtitle_value}'[subbed]")
        else:
            filter_steps.append(f"[{current_label}]subtitles='{subtitle_value}':force_style='{force_style}'[subbed]")
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
        subtitle_mode = str(hook_template.get("subtitle", {}).get("mode", "line")).strip().lower()
        if subtitle_mode == "word":
            subtitle_path = write_word_ass(
                clip_segments,
                work_dir / f"clip{clip_index}_words.ass",
                hook_template,
            )
        else:
            subtitle_path = work_dir / f"clip{clip_index}.srt"
            write_srt(clip_segments, subtitle_path)
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
            "ultrafast",
            "-crf",
            "24",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-threads",
            str(config.FFMPEG_THREADS),
            str(tmp_output),
        ])

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
