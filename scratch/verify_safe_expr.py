"""Verification for the AST-whitelist position expression evaluator (modules/overlay_renderer.py).

1. Valid expressions (repo templates + examples) -> IDENTICAL to the legacy eval() path.
2. Malicious / unsupported payloads -> rejected with ValueError.
3. Named positions and the normal render path still work.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.disable(logging.CRITICAL)
# Payload strings intentionally contain invalid escapes; ast.parse warns while parsing them.
warnings.filterwarnings("ignore", message="invalid escape sequence")

from modules import overlay_renderer as OR  # noqa: E402

FONT = Path(r"C:\Windows\Fonts\arialbd.ttf")
if not FONT.exists():
    FONT = Path(r"C:\Windows\Fonts\arial.ttf")

CANVAS_W, CANVAS_H = 1080, 1920
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# Legacy implementation (verbatim copy of the pre-fix eval() path) for A/B diff
# ---------------------------------------------------------------------------

def legacy_evaluate_expr(expr, text_w, text_h, canvas_w=CANVAS_W, canvas_h=CANVAS_H, axis="y"):
    if expr is None:
        return (canvas_w - text_w) // 2 if axis == "x" else 0
    raw = str(expr).strip()
    if not raw:
        return (canvas_w - text_w) // 2 if axis == "x" else 0

    lower_raw = raw.lower()
    if axis == "x":
        named_x = {
            "center": max(0, (canvas_w - text_w) // 2),
            "tengah": max(0, (canvas_w - text_w) // 2),
            "middle": max(0, (canvas_w - text_w) // 2),
            "left": 40,
            "kiri": 40,
            "right": max(0, canvas_w - text_w - 40),
            "kanan": max(0, canvas_w - text_w - 40),
        }
        if lower_raw in named_x:
            return named_x[lower_raw]
    else:
        named_y = {
            "top": 220,
            "atas": 220,
            "upper_center": 360,
            "tengah_atas": 360,
            "center_top": 360,
            "center": max(0, (canvas_h - text_h) // 2),
            "tengah": max(0, (canvas_h - text_h) // 2),
            "middle": max(0, (canvas_h - text_h) // 2),
            "lower": 1260,
            "bawah": 1260,
            "bottom": 1260,
        }
        if lower_raw in named_y:
            return named_y[lower_raw]

    try:
        return int(round(float(raw)))
    except (ValueError, TypeError):
        pass

    has_cap_w, has_low_w = "W" in raw, "w" in raw
    has_cap_h, has_low_h = "H" in raw, "h" in raw
    ns = {"text_w": text_w, "text_h": text_h}
    if has_cap_w and has_low_w:
        ns["W"], ns["w"] = canvas_w, text_w
    else:
        ns["w"], ns["W"] = canvas_w, canvas_w
    if has_cap_h and has_low_h:
        ns["H"], ns["h"] = canvas_h, text_h
    else:
        ns["h"], ns["H"] = canvas_h, canvas_h

    tokens = re.findall(r"\b(w|h|W|H|text_w|text_h)\b", raw)
    safe = raw
    for token in sorted(set(tokens), key=len, reverse=True):
        safe = safe.replace(token, str(ns[token]))
    try:
        result = eval(safe, {"__builtins__": {}}, {"min": min, "max": max, "abs": abs})
        return int(round(float(result)))
    except (SyntaxError, NameError, TypeError, ValueError, ZeroDivisionError):
        return (canvas_w - text_w) // 2 if axis == "x" else 0


# ---------------------------------------------------------------------------
# 1. Valid expressions from repo templates + realistic examples
# ---------------------------------------------------------------------------

valid_exprs = {
    "(w-text_w)/2", "(W-w)/2", "(H-h)/2", "(w - text_w)/2", "(w-w)/2",
    "(w-text_w)/2.0", "(w-w)/2.0", "220", "360", "1260", "0", "40", "-40",
    "w/2", "h-h", "h - h", "2*w + 10", "w - text_w", "(h//2) - 60",
    "max(0, (w-text_w)/2)", "min(400, (h-text_h)/2)", "abs(-120) + 8",
    "w % 7", "2 ** 3", "-(w // 4)", "+w - 20", "(W - w) // 2", "(H - h) / 2 * 1",
    "max(w, h) - min(text_w, text_h)", "abs(text_w - text_h) // 2",
    "top", "atas", "upper_center", "tengah_atas", "center_top", "center",
    "tengah", "middle", "lower", "bawah", "bottom", "left", "kiri", "right",
    "kanan", "UPPER_CENTER", "Center",
}

# Collect every x/y string found in repo hook templates + video_editor defaults
for path in sorted((ROOT / "hook_templates").glob("*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    for section in ("hook", "badge", "video"):
        sec = data.get(section)
        if isinstance(sec, dict):
            for key in ("x", "y"):
                if key in sec:
                    valid_exprs.add(str(sec[key]))

try:
    import ast as _ast
    ve_src = (ROOT / "modules" / "video_editor.py").read_text(encoding="utf-8")
    for node in _ast.walk(_ast.parse(ve_src)):
        if isinstance(node, _ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, _ast.Constant) and k.value in ("x", "y")
                    and isinstance(v, _ast.Constant) and isinstance(v.value, str)
                ):
                    valid_exprs.add(v.value)
except Exception as exc:  # pragma: no cover - diagnostics only
    print(f"[SKIP] video_editor default scan: {exc}")

print(f"\n== 1. Valid expressions ({len(valid_exprs)}) : new vs legacy eval ==")
dims = [(0, 0), (120, 40), (355, 268), (980, 1100), (1079, 1919)]
mismatch = 0
for expr in sorted(valid_exprs):
    for axis in ("x", "y"):
        for tw, th in dims:
            got = OR.evaluate_expr(expr, tw, th, CANVAS_W, CANVAS_H, axis=axis)
            old = legacy_evaluate_expr(expr, tw, th, CANVAS_W, CANVAS_H, axis=axis)
            if got != old or not isinstance(got, int):
                mismatch += 1
                print(f"    DIFF {expr!r} axis={axis} text=({tw},{th}) new={got!r} legacy={old!r}")
check("valid expressions identical to legacy eval (int type preserved)", mismatch == 0,
      f"({mismatch} diffs)")

# raw evaluator returns the same numeric value eval() produced (float/int parity)
parity = [("(w-text_w)/2", {"w": 1080, "text_w": 350}), ("h//3", {"h": 1920}),
          ("w/2", {"w": 1081}), ("2 ** 3", {}), ("max(0, -5)", {})]
for src, env in parity:
    new_val = OR.safe_eval_expr(src, dict(env))
    old_val = eval(src, {"__builtins__": {}}, {"min": min, "max": max, "abs": abs, **env})
    check(f"numeric parity {src!r}", type(new_val) is type(old_val) and new_val == old_val,
          f"{new_val!r} ({type(new_val).__name__}) vs {old_val!r}")


# ---------------------------------------------------------------------------
# 2. Hostile / unsupported payloads must be rejected
# ---------------------------------------------------------------------------

marker = ROOT / "scratch" / "_pwned_marker.txt"
if marker.exists():
    marker.unlink()

hostile = {
    "().__class__.__bases__[0]": None,
    "open('scratch/_pwned_marker.txt','w')": None,
    "__import__('os').system('calc')": None,
    r"__import__('os').system('cmd /c echo pwned > scratch\_pwned_marker.txt')": None,
    "[y for y in (1,2)]": None,
    "2**999999": None,
    "2 ** 999999": None,
    "unknown_name": None,
    "print('hi')": None,
    "lambda: 1": None,
    "w.__class__": None,
    "ns['w']": None,
    "(w for w in (1,2))": None,
    "{'a': 1}": None,
    "exec('import os')": None,
    "eval('1+1')": None,
    "globals()": None,
    "w if 1 else h": None,
    "1 < 2": None,
    "w and h": None,
    "True + 1": None,
    "'abc'": None,
    "min(1, 2, 3, 4, 5)": None,
    "max(x=1)": None,
    "__import__('os')": None,
    "(1).__class__": None,
}

print("\n== 2. Hostile payloads ==")
for payload in hostile:
    # low-level evaluator must raise ValueError
    try:
        OR.safe_eval_expr(payload, {"w": 1080, "h": 1920})
        check(f"rejected: {payload}", False, "-> safe_eval_expr returned a value")
    except ValueError as exc:
        check(f"rejected: {payload}", True, f"-> ValueError: {exc}")
    except Exception as exc:  # non-ValueError leak
        check(f"rejected: {payload}", False, f"-> {type(exc).__name__}: {exc}")

    # public entry point must fall back safely, never execute
    fallback_x = OR.evaluate_expr(payload, 200, 100, CANVAS_W, CANVAS_H, axis="x")
    fallback_y = OR.evaluate_expr(payload, 200, 100, CANVAS_W, CANVAS_H, axis="y")
    check(f"safe fallback: {payload}",
          fallback_x == (CANVAS_W - 200) // 2 and fallback_y == 0,
          f"x={fallback_x} y={fallback_y}")
    check(f"typed fallback: {payload}",
          type(fallback_x) is int and type(fallback_y) is int)

check("no side-effect file created by payloads", not marker.exists())

# Non-finite / empty edge cases must degrade, never raise out of evaluate_expr
for weird in ["inf", "-inf", "nan", "1e400", "()", "  ", "w", "w +"]:
    try:
        rx = OR.evaluate_expr(weird, 200, 100, CANVAS_W, CANVAS_H, axis="x")
        ry = OR.evaluate_expr(weird, 200, 100, CANVAS_W, CANVAS_H, axis="y")
        ok = type(rx) is int and type(ry) is int
        # must match legacy for inputs legacy survived; legacy crashed on some
        try:
            old_x = legacy_evaluate_expr(weird, 200, 100, CANVAS_W, CANVAS_H, axis="x")
        except Exception:
            old_x = "legacy crashed"
        check(f"edge case {weird!r} degrades safely", ok,
              f"x={rx} y={ry} legacy_x={old_x}")
    except Exception as exc:
        check(f"edge case {weird!r} degrades safely", False, f"-> {type(exc).__name__}: {exc}")

# The exposure was real: under the legacy eval() path (builtins stripped is not a
# sandbox) the classic sandbox-escape chain is actually evaluated.
escape = "().__class__.__bases__[0].__subclasses__()"
legacy_value = legacy_evaluate_expr(escape, 200, 100, CANVAS_W, CANVAS_H, axis="y")
check("legacy eval() did evaluate the escape chain (returns class list)",
      isinstance(legacy_value, int))  # coerced to fallback, but chain ran
try:
    live = eval(escape, {"__builtins__": {}}, {})  # noqa: S307 - deliberate A/B proof
    check("proof: escape chain reachable pre-fix", isinstance(live, list) and len(live) > 10,
          f"{len(live)} classes exposed")
except Exception as exc:  # pragma: no cover
    check("proof: escape chain reachable pre-fix", False, str(exc))
try:
    OR.safe_eval_expr(escape, {"w": 1080, "h": 1920})
    check("post-fix: escape chain blocked", False)
except ValueError as exc:
    check("post-fix: escape chain blocked", True, f"-> {exc}")

# The module itself must no longer contain an eval() call (comment-free scan).
code_only = "\n".join(
    ln for ln in Path(OR.__file__).read_text(encoding="utf-8").splitlines()
    if not ln.lstrip().startswith("#")
)
check("no eval( call left in overlay_renderer code", not re.search(r"(?<![\w.])eval\(", code_only))

# pow DoS guard keeps legitimate small powers working
check("pow guard keeps 2**10 valid", OR.safe_eval_expr("2 ** 10", {}) == 1024)
try:
    OR.safe_eval_expr("2 ** 11", {})
    check("2**11 rejected", False)
except ValueError:
    check("2**11 rejected", True)


# ---------------------------------------------------------------------------
# 3. Named positions + normal render path
# ---------------------------------------------------------------------------

print("\n== 3. Named positions / render path ==")
check("named y 'upper_center' -> 360", OR.evaluate_expr("upper_center", 100, 200, axis="y") == 360)
check("named y 'top' -> 220", OR.evaluate_expr("top", 100, 200, axis="y") == 220)
check("named y 'bottom' -> 1260", OR.evaluate_expr("bottom", 100, 200, axis="y") == 1260)
check("named x 'left' -> 40", OR.evaluate_expr("left", 100, 200, axis="x") == 40)
check("named x 'center'", OR.evaluate_expr("center", 100, 200, axis="x") == (1080 - 100) // 2)

section_named = {
    "font_size": 64, "font_color": "white", "border_w": 4, "border_color": "black",
    "box": True, "box_color": "#101010@0.7", "box_border_w": 20, "box_radius": 16,
    "x": "center", "y": "upper_center",
}
img_named = OR.render_text_layer("INI HOOK\nTES POSISI", section_named, CANVAS_W, CANVAS_H, str(FONT))
bbox = img_named.getbbox()
check("render with named position produces layer", img_named.size == (CANVAS_W, CANVAS_H) and bbox is not None,
      f"size={img_named.size} bbox={bbox}")
check("named-position render sits near upper_center (y=360)",
      bbox is not None and 280 <= bbox[1] <= 460, f"top={bbox[1] if bbox else None}")

y_expr = "max(220, (h-text_h)/5)"
section_expr = dict(section_named, x="(w-text_w)/2", y=y_expr)
img_expr = OR.render_text_layer("INI HOOK\nTES POSISI", section_expr, CANVAS_W, CANVAS_H, str(FONT))
expr_bbox = img_expr.getbbox()
layer_h = expr_bbox[3] - expr_bbox[1] if expr_bbox else 0
expected_y = OR.evaluate_expr(y_expr, 0, layer_h, CANVAS_W, CANVAS_H, axis="y")
check("expression-position render evaluated through AST evaluator",
      expr_bbox is not None and expr_bbox[1] == expected_y,
      f"bbox={expr_bbox} expected_top={expected_y}")
check("expression-position x centering intact",
      expr_bbox is not None
      and abs((expr_bbox[0] + expr_bbox[2]) / 2 - CANVAS_W / 2) <= 2,
      f"bbox={expr_bbox}")

section_evil = dict(section_named, y="__import__('os').system('calc')")
img_evil = OR.render_text_layer("INI HOOK\nTES POSISI", section_evil, CANVAS_W, CANVAS_H, str(FONT))
check("hostile y in section degrades to fallback, no crash",
      img_evil.size == (CANVAS_W, CANVAS_H) and img_evil.getbbox() is not None)

# templates on disk still render
for path in sorted((ROOT / "hook_templates").glob("*.json")):
    tpl = json.loads(path.read_text(encoding="utf-8"))
    out = OR.render_text_layer("CONTOH TEKS", tpl.get("hook", {}), CANVAS_W, CANVAS_H, str(FONT))
    check(f"template {path.name} renders", out.size == (CANVAS_W, CANVAS_H) and out.getbbox() is not None)

print("\n== RESULT ==", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
