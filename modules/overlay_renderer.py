"""Pillow-based overlay renderer for hook and badge text.

Renders styled text as RGBA PNG overlays with full CSS-alike support:
- box_radius (rounded corners on background box)
- rotate (text rotation)
- box_shadow (CSS-style drop shadow with blur)
- letter_spacing (negative/positive tracking)
- box_clip (CSS clip-path polygon masking)
- shadow_x/shadow_y/shadow_color (text drop shadow)
- border_w/border_color (text stroke)
- font_size/font_color/font_file
- x/y positioning with expression support
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

LOGGER = logging.getLogger(__name__)

CANVAS_W = 1080
CANVAS_H = 1920

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

_NAMED_COLORS: dict[str, str] = {
    "white": "#ffffff",
    "black": "#000000",
    "yellow": "#ffff00",
    "red": "#ff0000",
    "green": "#00ff00",
    "blue": "#0000ff",
    "orange": "#ffa500",
    "transparent": "#00000000",
}


def parse_css_color(value: Any) -> tuple[int, int, int, int]:
    """Parse a CSS-like color string to an RGBA tuple.

    Accepts:
        "#rrggbb", "#rrggbb@a", "0xrrggbb@a", "white@0.5", "black"
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return (255, 255, 255, 255)

    # Named color lookup
    lower = text.lower()
    if lower in _NAMED_COLORS:
        text = _NAMED_COLORS[lower]

    # Strip 0x prefix
    if text.startswith("0x") or text.startswith("0X"):
        text = "#" + text[2:]

    alpha: float = 1.0
    if "@" in text:
        text, alpha_str = text.rsplit("@", 1)
        try:
            alpha = max(0.0, min(1.0, float(alpha_str)))
        except (ValueError, TypeError):
            alpha = 1.0

    # Strip # and parse hex
    hex_str = text.lstrip("#")
    if not hex_str or len(hex_str) < 6:
        return (255, 255, 255, int(round(alpha * 255)))

    try:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        # 8-digit hex: RRGGBBAA
        if len(hex_str) >= 8:
            alpha = int(hex_str[6:8], 16) / 255.0
    except ValueError:
        return (255, 255, 255, int(round(alpha * 255)))

    return (r, g, b, int(round(alpha * 255)))


# ---------------------------------------------------------------------------
# Expression evaluator for x / y
# ---------------------------------------------------------------------------

def evaluate_expr(
    expr: Any,
    text_w: int,
    text_h: int,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
) -> int:
    """Evaluate a position expression.

    Supports variables: w (canvas_w), h (canvas_h), text_w, text_h,
    plus simple arithmetic via Python eval on trusted template input.
    """
    if expr is None:
        return 0
    raw = str(expr).strip()
    if not raw:
        return 0

    # Plain numeric
    try:
        return int(round(float(raw)))
    except (ValueError, TypeError):
        pass

    # Replace variables
    ns = {
        "w": canvas_w,
        "h": canvas_h,
        "W": canvas_w,
        "H": canvas_h,
        "text_w": text_w,
        "text_h": text_h,
    }
    # Simple token replacement (avoid issues with partial matches)
    tokens = re.findall(r"\b(w|h|W|H|text_w|text_h)\b", raw)
    safe = raw
    for token in sorted(set(tokens), key=len, reverse=True):
        safe = safe.replace(token, str(ns[token]))

    # Evaluate — these expressions come from our own template, not user input
    try:
        result = eval(safe, {"__builtins__": {}}, {"min": min, "max": max, "abs": abs})
        return int(round(float(result)))
    except (SyntaxError, NameError, TypeError, ValueError, ZeroDivisionError):
        LOGGER.warning("Could not evaluate position expression: %r", raw)
        return 0


# ---------------------------------------------------------------------------
# CSS box-shadow parser
# ---------------------------------------------------------------------------

_BOX_SHADOW_RE = re.compile(
    r"(?P<ox>[+-]?\d+)px\s+"
    r"(?P<oy>[+-]?\d+)px\s+"
    r"(?P<br>0|[+-]?\d+)px\s*"
    r"(?P<sp>0|[+-]?\d+)px\s+"
    r"(?P<color>.+)",
    re.IGNORECASE,
)

_BOX_SHADOW_NO_SPREAD_RE = re.compile(
    r"(?P<ox>[+-]?\d+)px\s+"
    r"(?P<oy>[+-]?\d+)px\s+"
    r"(?P<br>0|[+-]?\d+)px\s+"
    r"(?P<color>.+)",
    re.IGNORECASE,
)

_BOX_SHADOW_NO_BLUR_RE = re.compile(
    r"(?P<ox>[+-]?\d+)px\s+"
    r"(?P<oy>[+-]?\d+)px\s+"
    r"(?P<color>.+)",
    re.IGNORECASE,
)


def parse_css_box_shadow(shadow_str: str | None) -> dict | None:
    """Parse a CSS box-shadow string into a dict of parameters.

    Supports: ``offset-x offset-y blur-radius spread-radius color``
    and variants with fewer values.
    """
    if not shadow_str or not str(shadow_str).strip():
        return None
    text = str(shadow_str).strip()

    for pattern in (_BOX_SHADOW_RE, _BOX_SHADOW_NO_SPREAD_RE, _BOX_SHADOW_NO_BLUR_RE):
        m = pattern.match(text)
        if m:
            parts = m.groupdict()
            try:
                ox = int(parts.get("ox", "0"))
                oy = int(parts.get("oy", "0"))
                br = int(parts.get("br", "0"))
                sp = int(parts.get("sp", "0"))
            except ValueError:
                return None
            color = parse_css_color(parts.get("color", "#000000"))
            return {"offset_x": ox, "offset_y": oy, "blur": br, "spread": sp, "color": color}
    return None


# ---------------------------------------------------------------------------
# CSS clip-path polygon parser
# ---------------------------------------------------------------------------

_POLYGON_RE = re.compile(
    r"polygon\s*\(\s*([^)]+)\s*\)", re.IGNORECASE
)


def parse_css_polygon(
    clip_str: str | None, canvas_w: int, canvas_h: int
) -> list[tuple[int, int]] | None:
    """Parse a CSS polygon() clip-path into a list of (x, y) pixel coords."""
    if not clip_str or not str(clip_str).strip():
        return None
    text = str(clip_str).strip()
    m = _POLYGON_RE.match(text)
    if not m:
        return None

    points: list[tuple[int, int]] = []
    for pair in m.group(1).split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split()
        if len(parts) < 2:
            continue
        x_str, y_str = parts[0], parts[1]
        try:
            if "%" in x_str:
                x = int(round(float(x_str.replace("%", "")) / 100.0 * canvas_w))
            else:
                x = int(x_str.replace("px", ""))
            if "%" in y_str:
                y = int(round(float(y_str.replace("%", "")) / 100.0 * canvas_h))
            else:
                y = int(y_str.replace("px", ""))
        except (ValueError, TypeError):
            continue
        points.append((x, y))

    return points if len(points) >= 3 else None


# ---------------------------------------------------------------------------
# Pillow-compatible rounded rectangle (Pillow 12.x may lack the built-in)
# ---------------------------------------------------------------------------

def draw_rounded_rect(
    draw: ImageDraw.Draw,
    xy: tuple[float, float, float, float],
    radius: int,
    fill: tuple[int, int, int, int] | tuple[int, int, int] | None = None,
) -> None:
    """Draw a filled rounded rectangle using arcs and rectangles.

    Works on Pillow 8+ (including 12.x which is installed).
    """
    x1, y1, x2, y2 = map(int, xy)
    # Clamp radius
    r = max(0, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    if r == 0:
        draw.rectangle((x1, y1, x2, y2), fill=fill)
        return

    # Centre rectangle
    draw.rectangle((x1 + r, y1, x2 - r, y2), fill=fill)
    # Left / right rectangles
    draw.rectangle((x1, y1 + r, x2, y2 - r), fill=fill)
    # Four corners
    draw.pieslice((x1, y1, x1 + r * 2, y1 + r * 2), 180, 270, fill=fill)
    draw.pieslice((x2 - r * 2, y1, x2, y1 + r * 2), 270, 360, fill=fill)
    draw.pieslice((x1, y2 - r * 2, x1 + r * 2, y2), 90, 180, fill=fill)
    draw.pieslice((x2 - r * 2, y2 - r * 2, x2, y2), 0, 90, fill=fill)


# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

def load_font(font_path: str | None, font_size: int) -> ImageFont.FreeTypeFont | None:
    """Load a TrueType/OpenType font, returning None on failure."""
    if not font_path:
        LOGGER.warning("No font path provided for overlay render")
        return None
    path = Path(font_path)
    if not path.exists():
        LOGGER.warning("Font file does not exist: %s", font_path)
        return None
    try:
        return ImageFont.truetype(str(path), size=font_size)
    except (OSError, IOError) as exc:
        LOGGER.warning("Could not load font %s: %s", font_path, exc)
        return None


# ---------------------------------------------------------------------------
# Text measurement and drawing helpers
# ---------------------------------------------------------------------------

def _char_width(font: ImageFont.FreeTypeFont, char: str) -> int:
    """Return the advance width of a single character."""
    bbox = font.getbbox(char)
    return bbox[2] - bbox[0]


def _text_line_width(font: ImageFont.FreeTypeFont, text: str, tracking: int) -> int:
    """Measure the total width of a line of text with letter-spacing."""
    if not text:
        return 0
    total = sum(_char_width(font, ch) for ch in text)
    if len(text) > 1:
        total += tracking * (len(text) - 1)
    return total


def _text_block_size(
    font: ImageFont.FreeTypeFont,
    lines: list[str],
    tracking: int,
    line_spacing: int,
) -> tuple[int, int]:
    """Measure the total (width, height) of a multi-line text block."""
    if not lines or not lines[0]:
        return (0, 0)
    # Use ascent + descent for line height
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + line_spacing
    widths = [_text_line_width(font, ln, tracking) for ln in lines]
    w = max(widths)
    h = line_h * len(lines) - line_spacing  # no trailing gap
    return (w, h)


def _draw_line_with_tracking(
    draw: ImageDraw.Draw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    tracking: int = 0,
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int, int] | None = None,
) -> None:
    """Draw a single line of text character-by-character with letter-spacing."""
    if not text:
        return
    if tracking == 0 and stroke_width == 0:
        # Fast path
        draw.text((x, y), text, fill=fill, font=font)
        return

    cx = x
    for char in text:
        cw = _char_width(font, char)
        if stroke_width > 0 and stroke_fill is not None:
            draw.text(
                (cx, y),
                char,
                fill=fill,
                font=font,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
        else:
            draw.text((cx, y), char, fill=fill, font=font)
        cx += cw + tracking


# ---------------------------------------------------------------------------
# Main rendering logic
# ---------------------------------------------------------------------------

def _int_val(section: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(round(float(section.get(key, default))))
    except (TypeError, ValueError, OverflowError):
        return default


def _float_val(section: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(section.get(key, default))
    except (TypeError, ValueError):
        return default


def _bool_val(section: dict[str, Any], key: str, default: bool = False) -> bool:
    val = section.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return bool(val)


def _str_val(section: dict[str, Any], key: str, default: str = "") -> str:
    val = section.get(key)
    return str(val).strip() if val is not None else default


def render_text_layer(
    text: str,
    section: dict[str, Any],
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
    font_path: str | None = None,
) -> Image.Image:
    """Render a styled text layer onto a transparent RGBA canvas.

    Args:
        text: Multi-line text (\\n-separated lines), already wrapped & transformed.
        section: Template section dict (hook or badge config).
        canvas_w, canvas_h: Output canvas dimensions.
        font_path: Resolved path to the font file.

    Returns:
        RGBA PIL Image sized (canvas_w, canvas_h).
    """
    # -- Parse section config --------------------------------------------------
    font_size = _int_val(section, "font_size", 70)
    font_color = parse_css_color(_str_val(section, "font_color", "#ffffff"))
    border_w = _int_val(section, "border_w", 0)
    border_color = parse_css_color(_str_val(section, "border_color", "#000000"))
    shadow_x = _int_val(section, "shadow_x", 0)
    shadow_y = _int_val(section, "shadow_y", 0)
    shadow_color = parse_css_color(_str_val(section, "shadow_color", "#000000"))
    box_enabled = _bool_val(section, "box", False)
    box_color = parse_css_color(_str_val(section, "box_color", "#000000"))
    box_border_w = _int_val(section, "box_border_w", 0)
    box_radius = _int_val(section, "box_radius", 0)
    tracking = _int_val(section, "letter_spacing", 0)
    line_spacing = _int_val(section, "line_spacing", 0)
    rotate = _float_val(section, "rotate", 0.0)
    box_shadow = parse_css_box_shadow(section.get("box_shadow"))
    box_clip = parse_css_polygon(section.get("box_clip"), canvas_w, canvas_h)

    # -- Load font -------------------------------------------------------------
    font = load_font(font_path, font_size)
    if font is None:
        # Fallback: draw error text
        img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((20, canvas_h // 2), f"[font error: {font_path}]", fill=(255, 0, 0, 255))
        return img

    # -- Prepare lines ---------------------------------------------------------
    lines = text.split("\n") if text else [""]
    lines = [ln for ln in lines if ln is not None]

    if not lines or not any(ln.strip() for ln in lines):
        # No text: return empty transparent canvas
        return Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # -- Measure text block ----------------------------------------------------
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + line_spacing
    text_w, text_h = _text_block_size(font, lines, tracking, line_spacing)

    if text_w <= 0 or text_h <= 0:
        return Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # -- Auto-fit: reduce font size if text exceeds available width ------------
    x_val_pre = evaluate_expr(section.get("x"), 0, 0, canvas_w, canvas_h)
    available_w = canvas_w - x_val_pre - 40
    min_font = 24
    while text_w > available_w and font_size > min_font:
        font_size -= 2
        font = load_font(font_path, font_size)
        if font is None:
            break
        ascent, descent = font.getmetrics()
        line_h = ascent + descent + line_spacing
        text_w, text_h = _text_block_size(font, lines, tracking, line_spacing)

    # -- Calculate layer dimensions --------------------------------------------
    pad = box_border_w if box_enabled else 0
    layer_w = text_w + pad * 2
    layer_h = text_h + pad * 2

    # Account for shadow offset extending beyond the box
    shadow_ox = 0
    shadow_oy = 0
    shadow_blur = 0
    shadow_spread = 0
    if box_shadow:
        shadow_ox = box_shadow["offset_x"]
        shadow_oy = box_shadow["offset_y"]
        shadow_blur = box_shadow["blur"]
        shadow_spread = box_shadow["spread"]

    # Allow room for shadow + stroke + rotation
    extra = max(
        abs(shadow_ox) + shadow_blur + shadow_spread + abs(shadow_x) + border_w,
        abs(shadow_oy) + shadow_blur + shadow_spread + abs(shadow_y) + border_w,
        0,
    )
    buffer = extra + 20  # safety margin
    content_w = layer_w + buffer * 2
    content_h = layer_h + buffer * 2

    # -- Build content layer (the styled text + box before rotation) -----------
    content = Image.new("RGBA", (content_w, content_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(content)

    # Content origin (where (0,0) of text block lands in content coords)
    ox = buffer + pad
    oy = buffer + pad

    # -- 1. Box shadow ---------------------------------------------------------
    if box_enabled and box_shadow:
        bx1 = ox - pad
        by1 = oy - pad
        bx2 = ox + text_w + pad
        by2 = oy + text_h + pad
        # Expand by spread
        sp_adjust = shadow_spread
        sx1 = bx1 - sp_adjust + shadow_ox
        sy1 = by1 - sp_adjust + shadow_oy
        sx2 = bx2 + sp_adjust + shadow_ox
        sy2 = by2 + sp_adjust + shadow_oy
        # Create shadow mask on separate image
        shadow_layer = Image.new("RGBA", (content_w, content_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow_layer)
        shadow_radius = max(0, box_radius + sp_adjust)
        draw_rounded_rect(sd, (sx1, sy1, sx2, sy2), shadow_radius, fill=box_shadow["color"])
        if shadow_blur > 0:
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
        content = Image.alpha_composite(content, shadow_layer)
        draw = ImageDraw.Draw(content)

    # -- 2. Box background -----------------------------------------------------
    if box_enabled:
        bx1 = ox - pad
        by1 = oy - pad
        bx2 = ox + text_w + pad
        by2 = oy + text_h + pad

        if box_clip:
            # Clip-path: create a mask and apply
            box_img = Image.new("RGBA", (content_w, content_h), (0, 0, 0, 0))
            bd = ImageDraw.Draw(box_img)
            draw_rounded_rect(bd, (bx1, by1, bx2, by2), box_radius, fill=box_color)
            # Create polygon clip mask
            mask_img = Image.new("L", (content_w, content_h), 0)
            md = ImageDraw.Draw(mask_img)
            md.polygon(box_clip, fill=255)
            box_img.putalpha(
                Image.composite(
                    box_img.split()[3] if box_img.mode == "RGBA" else Image.new("L", (content_w, content_h), 255),
                    Image.new("L", (content_w, content_h), 0),
                    mask_img,
                )
            )
            content = Image.alpha_composite(content, box_img)
        else:
            draw_rounded_rect(draw, (bx1, by1, bx2, by2), box_radius, fill=box_color)

    # -- 3. Text shadow (drawtext-style) ---------------------------------------
    if shadow_x != 0 or shadow_y != 0:
        # Text shadow offset
        for i, ln in enumerate(lines):
            line_y = oy + i * line_h
            line_w = _text_line_width(font, ln, tracking)
            # Center the line within the text block width
            line_x = ox + (text_w - line_w) // 2
            _draw_line_with_tracking(
                draw,
                line_x + shadow_x,
                line_y + shadow_y,
                ln,
                font,
                fill=shadow_color,
                tracking=tracking,
                stroke_width=0,
            )

    # -- 4. Text stroke + fill -------------------------------------------------
    for i, ln in enumerate(lines):
        line_y = oy + i * line_h
        line_w = _text_line_width(font, ln, tracking)
        line_x = ox + (text_w - line_w) // 2

        # Draw with stroke (Pillow stroke is an outline around the glyph)
        if border_w > 0:
            _draw_line_with_tracking(
                draw,
                line_x,
                line_y,
                ln,
                font,
                fill=font_color,
                tracking=tracking,
                stroke_width=border_w,
                stroke_fill=border_color,
            )
        else:
            _draw_line_with_tracking(
                draw,
                line_x,
                line_y,
                ln,
                font,
                fill=font_color,
                tracking=tracking,
            )

    # -- 5. Rotation -----------------------------------------------------------
    if rotate != 0.0:
        content = content.rotate(
            rotate,
            center=(content_w // 2, content_h // 2),
            expand=True,
            resample=Image.BICUBIC,
            fillcolor=(0, 0, 0, 0),
        )

    # -- 6. Position on final canvas -------------------------------------------
    final = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # Evaluate X / Y using expressions
    x_val = evaluate_expr(section.get("x"), content.width, content.height, canvas_w, canvas_h)
    y_val = evaluate_expr(section.get("y"), content.width, content.height, canvas_w, canvas_h)

    # For expressions like "(w-text_w)/2", the content width already includes
    # the buffer. But the user's template has expressions designed for drawtext
    # where text_w is the actual text width, not the padded content.
    # So the x/y are the position of the text element (not the content).
    # We adjust so the text aligns at the evaluated position.

    # Compute where the text is within the content
    # The text starts at ox in content coords
    # The content is then positioned so that ox lands at (x_val, y_val) on the canvas
    paste_x = x_val - ox
    paste_y = y_val - oy

    # Clamp to prevent content from overflowing canvas edges
    if paste_x + content.width > canvas_w:
        paste_x = max(0, canvas_w - content.width)
    if paste_y + content.height > canvas_h:
        paste_y = max(0, canvas_h - content.height)

    final.paste(content, (paste_x, paste_y), content)
    return final


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_hook(
    hook_text: str,
    template: dict[str, Any],
    work_dir: Path,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
) -> Path:
    """Render the hook text overlay as a PNG image.

    Args:
        hook_text: Pre-processed hook text (multi-line, already wrapped).
        template: Full hook template dict.
        work_dir: Temporary directory for the output PNG.
        canvas_w, canvas_h: Output dimensions.

    Returns:
        Path to the generated RGBA PNG file.
    """
    hook_cfg = template.get("hook", {})
    section = hook_cfg
    enabled = _bool_val(section, "enabled", True)
    if not enabled or not hook_text or not hook_text.strip():
        # Return empty canvas (no hook shown)
        img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        out_path = work_dir / "hook_overlay.png"
        img.save(out_path, "PNG")
        return out_path

    # Resolve font path
    font_path = _resolve_font_path(section, "hook")
    img = render_text_layer(hook_text, section, canvas_w, canvas_h, font_path)
    out_path = work_dir / "hook_overlay.png"
    img.save(out_path, "PNG")
    LOGGER.info("Rendered hook overlay: %s (%dx%d)", out_path, img.width, img.height)
    return out_path


def render_badge(
    template: dict[str, Any],
    work_dir: Path,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
) -> Path | None:
    """Render the badge overlay as a PNG image.

    Args:
        template: Full hook template dict.
        work_dir: Temporary directory for the output PNG.
        canvas_w, canvas_h: Output dimensions.

    Returns:
        Path to the generated RGBA PNG, or None if badge is disabled.
    """
    badge_cfg = template.get("badge", {})
    enabled = _bool_val(badge_cfg, "enabled", True)
    if not enabled:
        return None

    text = _str_val(badge_cfg, "text", "HOT TAKE")
    if not text.strip():
        return None

    font_path = _resolve_font_path(badge_cfg, "badge")
    img = render_text_layer(text, badge_cfg, canvas_w, canvas_h, font_path)
    out_path = work_dir / "badge_overlay.png"
    img.save(out_path, "PNG")
    LOGGER.info("Rendered badge overlay: %s (%dx%d)", out_path, img.width, img.height)
    return out_path


def _resolve_font_path(
    section: dict[str, Any],
    section_name: str = "hook",
) -> str | None:
    """Resolve the font file path for a layer section.

    Tries in order:
    1. The explicit ``font_file`` in the section.
    2. The project-level ``SUBTITLE_FONT_PATH`` config.
    """
    font_file = _str_val(section, "font_file")
    if font_file:
        path = Path(font_file)
        if not path.is_absolute():
            import config as cfg  # noqa: F811

            path = cfg.PROJECT_ROOT / path
        if path.exists():
            return str(path.resolve())
        LOGGER.warning("Configured font file not found: %s", path)

    # Fall back to subtitle font from config
    try:
        import config as cfg  # noqa: F811

        fallback = str(Path(cfg.SUBTITLE_FONT_PATH).resolve())
        if Path(fallback).exists():
            return fallback
    except Exception:
        pass

    LOGGER.warning("No valid font found for %s, text may fall back", section_name)
    return None
