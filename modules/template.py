"""Per-user template management — JSON-based, fully customizable."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import config

LOGGER = logging.getLogger(__name__)

TEMPLATES_DIR = config.PROJECT_ROOT / "templates"
HOOK_TEMPLATES_DIR = config.PROJECT_ROOT / "hook_templates"

TEMPLATE_NAMES = {
    "capcut": "CapCut (default)",
    "minimal": "Minimal",
    "bold": "Bold",
    "classic": "Classic",
    "custom": "Custom",
}


def available_templates() -> list[str]:
    return ["capcut", "minimal", "bold", "classic"]


def all_templates() -> list[str]:
    return ["capcut", "minimal", "bold", "classic", "custom"]


def template_display_name(name: str) -> str:
    return TEMPLATE_NAMES.get(name, name)


def preset_path(name: str) -> Path:
    return HOOK_TEMPLATES_DIR / f"{name}.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── Preset definitions ──────────────────────────────────────────────

PRESETS: dict[str, dict] = {
    "hormozi": {
        "name": "Alex Hormozi",
        "hook": {
            "font_name": "Impact", "font_size": 84, "font_color": "#ffffff",
            "highlight_color": "#ffe600", "highlight_last_word": True,
            "border_w": 2, "border_color": "#000000@0.95",
            "shadow_x": 3, "shadow_y": 3, "shadow_color": "#000000@0.75",
            "box": True, "box_color": "#0a0a0a@0.92", "box_border_w": 26,
            "box_radius": 16, "box_outline_w": 2.5, "box_outline_color": "#ffe600@0.80",
            "x": "(w-text_w)/2", "y": "upper_center", "letter_spacing": 0, "rotate": 0,
        },
        "subtitle": {
            "font_name": "Impact", "mode": "karaoke", "text_transform": "upper",
            "font_size": 76, "font_color": "#ffffff", "highlight_color": "#ffe600",
            "outline_color": "#000000", "outline": 1.8, "shadow": 1.2,
            "shadow_color": "#000000", "back_color": "#000000@0.75",
            "alignment": 2, "margin_v": 420, "words_per_phrase": 3, "max_line_chars": 24,
        },
        "video": {
            "background_blur": 25, "background_blur_power": 5, "overlay_enabled": True,
            "width": 1080, "x": "(W-w)/2", "y": "(H-h)/2", "border_w": 0, "border_color": "#ffffff@0.70",
        },
        "shapes": [],
    },
    "capcut": {
        "name": "CapCut Modern",
        "hook": {
            "font_name": "Impact", "font_size": 82, "font_color": "#ffffff",
            "highlight_color": "#00ff66", "highlight_last_word": True,
            "border_w": 2, "border_color": "#000000@0.90",
            "shadow_x": 3, "shadow_y": 3, "shadow_color": "#000000@0.60",
            "box": True, "box_color": "#141824@0.80", "box_border_w": 24,
            "box_radius": 24, "box_outline_w": 2, "box_outline_color": "#00ff66@0.70",
            "x": "(w-text_w)/2", "y": "upper_center", "letter_spacing": 0, "rotate": 0,
        },
        "subtitle": {
            "font_name": "Impact", "mode": "karaoke", "text_transform": "upper",
            "font_size": 74, "font_color": "#ffffff", "highlight_color": "#00ff66",
            "outline_color": "#000000", "outline": 1.8, "shadow": 1.2,
            "shadow_color": "#000000", "back_color": "#000000@0.7",
            "alignment": 2, "margin_v": 420, "words_per_phrase": 4, "max_line_chars": 26,
        },
        "video": {
            "background_blur": 25, "background_blur_power": 5, "overlay_enabled": True,
            "width": 1080, "x": "(W-w)/2", "y": "(H-h)/2", "border_w": 0, "border_color": "#ffffff@0.70",
        },
        "shapes": [],
    },
    "cinematic": {
        "name": "Cinematic",
        "hook": {
            "font_name": "Impact", "font_size": 76, "font_color": "#ffffff",
            "highlight_color": "#f5c518", "highlight_last_word": True,
            "border_w": 2, "border_color": "#000000@0.85",
            "shadow_x": 3, "shadow_y": 3, "shadow_color": "#000000@0.65",
            "box": True, "box_color": "#080c14@0.88", "box_border_w": 20,
            "box_radius": 6, "box_outline_w": 1.5, "box_outline_color": "#f5c518@0.60",
            "x": "(w-text_w)/2", "y": "top", "letter_spacing": 2, "rotate": 0,
        },
        "subtitle": {
            "font_name": "Impact", "mode": "karaoke", "text_transform": "upper",
            "font_size": 72, "font_color": "#ffffff", "highlight_color": "#f5c518",
            "outline_color": "#000000", "outline": 1.5, "shadow": 1.0,
            "shadow_color": "#000000", "back_color": "#000000@0.7",
            "alignment": 2, "margin_v": 380, "words_per_phrase": 4, "max_line_chars": 26,
        },
        "video": {
            "background_blur": 28, "background_blur_power": 5, "overlay_enabled": True,
            "width": 1080, "x": "(W-w)/2", "y": "(H-h)/2", "border_w": 0, "border_color": "#ffffff@0.70",
        },
        "shapes": [],
    },
    "minimal": {
        "name": "Minimal",
        "hook": {
            "font_name": "Impact", "font_size": 84, "font_color": "#ffffff",
            "highlight_color": "#ffffff", "highlight_last_word": True,
            "border_w": 2, "border_color": "#000000@0.95",
            "shadow_x": 2, "shadow_y": 2, "shadow_color": "#000000@0.70",
            "box": False, "box_color": "#000000@0.0", "box_border_w": 0, "box_radius": 0,
            "x": "(w-text_w)/2", "y": "top", "letter_spacing": 0, "rotate": 0,
        },
        "subtitle": {
            "font_name": "Impact", "mode": "karaoke", "text_transform": "upper",
            "font_size": 74, "font_color": "#ffffff", "highlight_color": "#ffffff",
            "outline_color": "#000000", "outline": 1.8, "shadow": 1.2,
            "shadow_color": "#000000", "back_color": "#000000@0.7",
            "alignment": 2, "margin_v": 400, "words_per_phrase": 4, "max_line_chars": 26,
        },
        "video": {
            "background_blur": 25, "background_blur_power": 5, "overlay_enabled": True,
            "width": 1080, "x": "(W-w)/2", "y": "(H-h)/2", "border_w": 0, "border_color": "#ffffff@0.70",
        },
        "shapes": [],
    },
    "neon_badge": {
        "name": "Cyber Neon",
        "hook": {
            "font_name": "Impact", "font_size": 82, "font_color": "#ffffff",
            "highlight_color": "#00e5ff", "highlight_last_word": True,
            "border_w": 2, "border_color": "#000000@0.95",
            "shadow_x": 3, "shadow_y": 3, "shadow_color": "#000000@0.70",
            "box": True, "box_color": "#050814@0.92", "box_border_w": 24,
            "box_radius": 20, "box_outline_w": 3, "box_outline_color": "#00e5ff@0.90",
            "x": "(w-text_w)/2", "y": "upper_center", "letter_spacing": 0, "rotate": 0,
        },
        "subtitle": {
            "font_name": "Impact", "mode": "karaoke", "text_transform": "upper",
            "font_size": 74, "font_color": "#ffffff", "highlight_color": "#00e5ff",
            "outline_color": "#000000", "outline": 1.8, "shadow": 1.2,
            "shadow_color": "#000000", "back_color": "#000000@0.7",
            "alignment": 2, "margin_v": 420, "words_per_phrase": 4, "max_line_chars": 26,
        },
        "video": {
            "background_blur": 25, "background_blur_power": 5, "overlay_enabled": True,
            "width": 1080, "x": "(W-w)/2", "y": "(H-h)/2", "border_w": 0, "border_color": "#ffffff@0.70",
        },
        "shapes": [],
    },
}


def ensure_presets() -> None:
    """Write preset template files so the file-based loader can also use them."""
    for name, data in PRESETS.items():
        path = preset_path(name)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            LOGGER.info("Created preset: %s", path)


def _load_preset(name: str) -> dict[str, Any]:
    if name == "custom":
        return {}
    path = preset_path(name)
    if not path.exists():
        LOGGER.warning("Preset %s not found, falling back to capcut", name)
        path = preset_path("capcut")
    return json.loads(path.read_text(encoding="utf-8"))


def _deep_merge(base: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _pref_path(user_id: int) -> Path:
    return TEMPLATES_DIR / str(user_id) / "preference.json"


def _read_pref(user_id: int) -> dict:
    p = _pref_path(user_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"preset": "capcut", "overrides": {}}


def _write_pref(user_id: int, pref: dict) -> None:
    p = _pref_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pref, indent=2), encoding="utf-8")


def get_user_template(user_id: int) -> dict[str, Any]:
    """Full merged template for rendering."""
    pref = _read_pref(user_id)
    base = _load_preset(pref.get("preset", "capcut"))
    overrides = pref.get("overrides", {})
    custom = pref.get("custom", {})
    merged = {**base}
    if custom:
        _deep_merge(merged, custom)
    if overrides:
        _deep_merge(merged, overrides)
    return merged


def get_pref(user_id: int) -> dict:
    """Get raw preference."""
    return _read_pref(user_id)


def set_preset(user_id: int, preset_name: str) -> None:
    pref = {"preset": preset_name, "overrides": {}}
    if preset_name == "custom" and _pref_path(user_id).exists():
        prev = _read_pref(user_id)
        pref["custom"] = prev.get("custom", {})
    _write_pref(user_id, pref)


def set_custom_json(user_id: int, data: dict) -> None:
    pref = _read_pref(user_id)
    pref["preset"] = "custom"
    pref["custom"] = data
    pref["overrides"] = {}
    _write_pref(user_id, pref)


def override(user_id: int, section: str, key: str, value: Any) -> None:
    pref = _read_pref(user_id)
    pref.setdefault("overrides", {})
    _deep_merge(pref["overrides"], {section: {key: value}})
    _write_pref(user_id, pref)


def export_json(user_id: int) -> str:
    return json.dumps(get_user_template(user_id), indent=2)


# ── Parameter catalogue (for UI) ────────────────────────────────────

ParamDef = tuple[str, str, str, list[Any] | None]

SECTIONS: dict[str, list[ParamDef]] = {
    "hook": [
        ("font_size",     "Font Size (px)",       "int",    [56, 64, 72, 82, 86, 96, 110, 130]),
        ("font_color",    "Text Color",            "color",  ["#ffffff", "#fff200", "#ff4500", "#00ff88", "#ff69b4", "#00bfff"]),
        ("box",           "Background Box",         "bool",   None),
        ("box_color",     "Box Color",             "color",  ["#1c1717@0.96", "#000000@0.85", "#000000@0.6", "#ffffff@0.3"]),
        ("box_border_w",  "Box Padding (px)",      "int",    [10, 16, 20, 24, 30, 40]),
        ("box_radius",    "Box Rounded Corner",    "int",    [0, 8, 12, 20, 30, 42, 60]),
        ("border_w",      "Text Border (px)",      "int",    [0, 2, 4, 6, 8]),
        ("border_color",  "Border Color",          "color",  ["#000000", "#ffffff", "#ff4500", "#ff0000"]),
        ("shadow_x",      "Shadow X (px)",         "int",    [0, 2, 4, 6, 8, 10]),
        ("shadow_y",      "Shadow Y (px)",         "int",    [0, 2, 4, 6, 8, 10]),
        ("shadow_color",  "Shadow Color",          "color",  ["#000000@0.65", "#000000@0.8", "#000000@0.4"]),
        ("letter_spacing","Letter Spacing (px)",   "int",    [-2, 0, 1, 2, 3, 5]),
        ("rotate",        "Rotation (°)",          "int",    [-4, -2, 0, 2, 4]),
        ("y",             "Vertical Position",     "expr",   ["230", "100", "500", "900", "1200", "(h-text_h)/2"]),
        ("x",             "Horizontal Position",   "expr",   ["118", "(w-text_w)/2", "50", "200", "(w-text_w)/3*2"]),
    ],
    "subtitle": [
        ("mode",          "Mode",                  "choice", ["line", "word"]),
        ("word_animation","Word Animation",        "choice", ["pop", "fade", "pulse", "none"]),
        ("font_size",     "Font Size (px)",        "int",    [32, 36, 40, 44, 46, 52, 60]),
        ("font_color",    "Text Color",            "color",  ["#fff200", "#ffffff", "#f5f0e8", "#00ff88"]),
        ("outline_color", "Outline Color",         "color",  ["#000000", "#111111", "#1a73e8"]),
        ("outline",       "Outline Width",         "int",    [0, 1, 2, 3, 4]),
        ("shadow",        "Shadow Depth",          "int",    [0, 1, 2, 3]),
        ("alignment",     "Alignment",             "choice", ["1 (bottom-left)", "2 (bottom-center)", "3 (bottom-right)", "8 (top-center)"]),
        ("margin_v",      "Vertical Margin (px)",  "int",    [40, 60, 80, 100, 150, 200, 400, 787]),
    ],
    "video": [
        ("background_blur",  "Blur Strength",      "int",    [5, 10, 15, 20, 25, 35, 50]),
        ("background_blur_power", "Blur Power",    "int",    [2, 3, 5, 7]),
        ("overlay_enabled",  "Overlay Video",       "bool",   None),
        ("width",            "Video Width (px)",    "int",    [540, 720, 854, 960, 1080, 1200]),
        ("x",                "Overlay X Position",  "choice", ["(W-w)/2", "0", "50", "100", "200", "(W-w)"]),
        ("y",                "Overlay Y Position",  "choice", ["(H-h)/2", "0", "100", "200", "400", "(H-h)"]),
        ("border_w",      "Border Width (px)",     "int",    [0, 2, 4, 6, 8]),
        ("border_color",  "Border Color",          "color",  ["#ffffff@0.70", "#ff4500@0.6", "#000000@0.5"]),
    ],
    "shapes": [
        ("count", "Shapes Count", "info", None),
    ],
}

SORT_ORDER = {"hook": 0, "subtitle": 1, "video": 2, "shapes": 3}

# Mapping from UI choice values to actual template values
ALIGNMENT_MAP = {"1 (bottom-left)": 1, "2 (bottom-center)": 2, "3 (bottom-right)": 3,
                 "8 (top-center)": 8}
ALIGNMENT_REVERSE = {1: "1 (bottom-left)", 2: "2 (bottom-center)", 3: "3 (bottom-right)", 8: "8 (top-center)"}


def format_current_value(tmpl: dict, section: str, key: str, ptype: str) -> str:
    if section == "shapes" and key == "count":
        shapes = tmpl.get("shapes", [])
        if isinstance(shapes, dict):
            return "1 shape (legacy)" if shapes.get("enabled", False) else "Disabled (legacy)"
        n = len(shapes)
        return f"{n} shape{'s' if n != 1 else ''} (use Web Editor)"
    val = tmpl.get(section, {}).get(key, "?")
    if key == "alignment" and isinstance(val, int):
        return ALIGNMENT_REVERSE.get(val, str(val))
    if ptype == "bool":
        return "✅" if val else "❌"
    return str(val)


def format_section_summary(tmpl: dict, section: str) -> str:
    lines = []
    for key, label, ptype, _ in SECTIONS.get(section, []):
        val = format_current_value(tmpl, section, key, ptype)
        lines.append(f"  {label}: `{val}`")
    return "\n".join(lines)
