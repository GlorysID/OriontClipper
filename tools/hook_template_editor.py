"""Visual hook template editor for ContentClipper.

This is intentionally dependency-free. It serves a small browser UI and writes
the active HOOK_TEMPLATE_PATH JSON file directly.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from modules.video_editor import DEFAULT_HOOK_TEMPLATE, merge_template  # noqa: E402


FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}
FONT_DIRS = [
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".local/share/fonts",
]


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ContentClipper Hook Studio</title>
  <style>
    :root {
      --bg: #090909;
      --panel: #141414;
      --panel-2: #1d1b18;
      --ink: #f6efe1;
      --muted: #9c9488;
      --line: rgba(246, 239, 225, 0.13);
      --yellow: #f4d000;
      --orange: #ff7a1a;
      --red: #ff3449;
      --green: #44f2a6;
      --shadow: rgba(0, 0, 0, 0.52);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: ui-sans-serif, "Aptos", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 13% 8%, rgba(255, 122, 26, 0.18), transparent 28rem),
        radial-gradient(circle at 83% 12%, rgba(244, 208, 0, 0.12), transparent 34rem),
        linear-gradient(135deg, #070707, #11100e 55%, #070707);
      overflow-x: hidden;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.09;
      background-image:
        linear-gradient(rgba(255,255,255,.4) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.4) 1px, transparent 1px);
      background-size: 42px 42px;
      mask-image: linear-gradient(to bottom, black, transparent 85%);
    }

    .shell {
      width: min(1520px, calc(100vw - 28px));
      margin: 0 auto;
      padding: 24px 0 34px;
    }

    header {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      padding: 8px 4px 22px;
    }

    .eyebrow {
      color: var(--yellow);
      font-weight: 900;
      letter-spacing: 0.18em;
      font-size: 12px;
      text-transform: uppercase;
    }

    h1 {
      margin: 6px 0 0;
      max-width: 780px;
      font-size: clamp(34px, 5vw, 78px);
      line-height: 0.9;
      letter-spacing: -0.065em;
      text-transform: uppercase;
    }

    .status {
      min-width: 280px;
      color: var(--muted);
      text-align: right;
      font-size: 13px;
      line-height: 1.5;
    }

    .status strong { color: var(--ink); }

    .workspace {
      display: grid;
      grid-template-columns: minmax(330px, 410px) minmax(390px, 1fr) minmax(330px, 430px);
      gap: 18px;
      align-items: start;
    }

    .panel {
      position: relative;
      border: 1px solid var(--line);
      border-radius: 26px;
      background: linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.025));
      box-shadow: 0 24px 80px var(--shadow);
      overflow: hidden;
    }

    .panel::after {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      border-radius: inherit;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.08);
    }

    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 18px 18px 12px;
      border-bottom: 1px solid var(--line);
    }

    .panel-title {
      font-weight: 900;
      letter-spacing: -0.03em;
      text-transform: uppercase;
    }

    .panel-body { padding: 16px 18px 18px; }

    .studio {
      display: grid;
      place-items: center;
      min-height: 760px;
      padding: 22px;
      background:
        radial-gradient(circle at 50% 35%, rgba(255,255,255,.08), transparent 18rem),
        linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.01));
    }

    .phone-wrap {
      position: relative;
      width: min(440px, 100%);
      aspect-ratio: 9 / 16;
      border-radius: 42px;
      padding: 12px;
      background: linear-gradient(145deg, #2a2925, #040404 40%, #24211c);
      box-shadow: 0 30px 90px rgba(0,0,0,.74), 0 0 0 1px rgba(255,255,255,.1);
    }

    .phone {
      position: relative;
      width: 100%;
      height: 100%;
      overflow: hidden;
      border-radius: 32px;
      background:
        linear-gradient(rgba(0,0,0,.12), rgba(0,0,0,.12)),
        radial-gradient(ellipse at 50% 40%, rgba(255,255,255,.20), transparent 18rem),
        linear-gradient(100deg, #332114, #0d0d0f 42%, #1f2b26);
      user-select: none;
      touch-action: none;
    }

    .phone::before {
      content: "";
      position: absolute;
      inset: -28px;
      background:
        linear-gradient(90deg, transparent 0 18%, rgba(255,255,255,.08) 18% 19%, transparent 19% 43%, rgba(255,255,255,.06) 43% 44%, transparent 44% 72%, rgba(255,255,255,.06) 72% 73%, transparent 73%),
        radial-gradient(circle at 50% 38%, rgba(255, 122, 26, .28), transparent 16rem);
      filter: blur(18px);
      transform: scale(1.06);
    }

    .subject {
      position: absolute;
      left: 50%;
      top: 24%;
      width: 58%;
      height: 44%;
      transform: translateX(-50%);
      border-radius: 42% 42% 18% 18%;
      background: linear-gradient(180deg, rgba(255,225,178,.82), rgba(63,44,32,.92));
      box-shadow: 0 18px 60px rgba(0,0,0,.55);
      opacity: .84;
    }

    .subtitle-sample {
      position: absolute;
      left: 9%;
      right: 9%;
      bottom: 70px;
      text-align: center;
      color: #fff200;
      font-weight: 900;
      font-size: clamp(15px, 4vw, 26px);
      text-shadow: 2px 2px 0 #000, -2px 2px 0 #000, 2px -2px 0 #000, -2px -2px 0 #000;
      line-height: 1.08;
      letter-spacing: -0.02em;
    }

    .safe-zone {
      position: absolute;
      inset: 58px 24px 82px;
      border: 1px dashed rgba(255,255,255,.18);
      border-radius: 18px;
      pointer-events: none;
    }

    .overlay {
      position: absolute;
      cursor: grab;
      white-space: pre-line;
      z-index: 5;
    }

    .overlay:active { cursor: grabbing; }
    .overlay.selected { outline: 2px solid var(--green); outline-offset: 8px; }

    .badge-preview {
      font-weight: 1000;
      text-transform: uppercase;
      line-height: 1;
      letter-spacing: .02em;
    }

    .hook-preview {
      font-weight: 1000;
      text-align: center;
      line-height: .96;
      letter-spacing: -0.045em;
    }

    .section { margin-bottom: 18px; }
    .section:last-child { margin-bottom: 0; }

    label {
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 900;
      letter-spacing: .12em;
      text-transform: uppercase;
      margin: 0 0 7px;
    }

    input, select, textarea, button {
      font: inherit;
    }

    input[type="text"], input[type="number"], select, textarea {
      width: 100%;
      color: var(--ink);
      background: rgba(0,0,0,.28);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 11px 12px;
      outline: none;
    }

    input[type="color"] {
      width: 100%;
      height: 42px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(0,0,0,.28);
    }

    textarea {
      min-height: 96px;
      resize: vertical;
      font-family: ui-monospace, "Cascadia Code", monospace;
      font-size: 12px;
      line-height: 1.45;
    }

    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }

    .tabs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 16px;
    }

    .tab, .btn {
      border: 1px solid var(--line);
      color: var(--ink);
      background: rgba(255,255,255,.055);
      border-radius: 999px;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .05em;
      font-size: 12px;
      transition: transform .16s ease, background .16s ease, border-color .16s ease;
    }

    .tab:hover, .btn:hover { transform: translateY(-1px); border-color: rgba(244,208,0,.45); }
    .tab.active { background: var(--yellow); color: #070707; border-color: var(--yellow); }

    .btn.primary { background: var(--yellow); color: #070707; border-color: var(--yellow); }
    .btn.danger { background: rgba(255,52,73,.14); border-color: rgba(255,52,73,.35); }
    .btn.full { width: 100%; }

    .switch-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 10px 0;
    }

    .switch-row label { margin: 0; }

    .hint {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }

    .json-output { min-height: 250px; }

    .toast {
      position: fixed;
      right: 22px;
      bottom: 22px;
      z-index: 30;
      background: #f4d000;
      color: #060606;
      border-radius: 18px;
      padding: 14px 16px;
      font-weight: 1000;
      box-shadow: 0 18px 50px rgba(0,0,0,.45);
      opacity: 0;
      transform: translateY(12px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }

    .toast.show { opacity: 1; transform: translateY(0); }

    @media (max-width: 1180px) {
      .workspace { grid-template-columns: 1fr; }
      .studio { min-height: auto; }
      .status { text-align: left; }
      header { align-items: start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <div class="eyebrow">ContentClipper Visual Tool</div>
        <h1>Hook Layout Studio</h1>
      </div>
      <div class="status">
        Active template<br />
        <strong id="templatePath">Loading...</strong><br />
        Drag overlay di preview, lalu tekan Save.
      </div>
    </header>

    <main class="workspace">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">Controls</div>
          <button class="btn" id="reloadBtn">Reload</button>
        </div>
        <div class="panel-body">
          <div class="section">
            <label for="sampleText">Sample Hook Text</label>
            <textarea id="sampleText">AI & SpaceX tarik investasi dari Bitcoin</textarea>
          </div>

          <div class="section grid-3">
            <div>
              <label for="displaySeconds">Display</label>
              <input id="displaySeconds" type="number" min="0.5" step="0.1" />
            </div>
            <div>
              <label for="fadeSeconds">Fade</label>
              <input id="fadeSeconds" type="number" min="0" step="0.05" />
            </div>
            <div>
              <label for="transform">Text</label>
              <select id="transform">
                <option value="upper">UPPER</option>
                <option value="title">Title</option>
                <option value="lower">lower</option>
                <option value="none">Original</option>
              </select>
            </div>
          </div>

          <div class="section grid-2">
            <div>
              <label for="maxLineChars">Wrap Chars</label>
              <input id="maxLineChars" type="number" min="8" max="60" step="1" />
            </div>
            <div>
              <label for="maxLines">Max Lines</label>
              <input id="maxLines" type="number" min="1" max="5" step="1" />
            </div>
          </div>

          <div class="tabs">
            <button class="tab active" data-tab="hook">Hook Text</button>
            <button class="tab" data-tab="badge">Badge</button>
          </div>

          <div id="hookControls">
            <div class="section grid-2">
              <div>
                <label for="hookX">X</label>
                <input id="hookX" type="text" />
              </div>
              <div>
                <label for="hookY">Y</label>
                <input id="hookY" type="text" />
              </div>
            </div>
            <div class="section">
              <label for="hookFontFile">Font Family</label>
              <select id="hookFontFile"></select>
            </div>
            <div class="section grid-2">
              <div>
                <label for="hookFontSize">Font Size</label>
                <input id="hookFontSize" type="number" min="12" max="220" />
              </div>
              <div>
                <label for="hookLineSpacing">Line Spacing</label>
                <input id="hookLineSpacing" type="number" min="-30" max="80" />
              </div>
            </div>
            <div class="section grid-2">
              <div>
                <label for="hookFontColor">Font Color</label>
                <input id="hookFontColor" type="color" />
              </div>
              <div>
                <label for="hookBorderColor">Stroke Color</label>
                <input id="hookBorderColor" type="color" />
              </div>
            </div>
            <div class="section grid-3">
              <div>
                <label for="hookBorderW">Stroke</label>
                <input id="hookBorderW" type="number" min="0" max="30" />
              </div>
              <div>
                <label for="hookShadowX">Shadow X</label>
                <input id="hookShadowX" type="number" min="-40" max="40" />
              </div>
              <div>
                <label for="hookShadowY">Shadow Y</label>
                <input id="hookShadowY" type="number" min="-40" max="40" />
              </div>
            </div>
            <div class="section grid-2">
              <div>
                <label for="hookShadowColor">Shadow</label>
                <input id="hookShadowColor" type="color" />
              </div>
              <div>
                <label for="hookBoxColor">Box Color</label>
                <input id="hookBoxColor" type="color" />
              </div>
            </div>
            <div class="section grid-2">
              <div class="switch-row">
                <label for="hookBox">Box</label>
                <input id="hookBox" type="checkbox" />
              </div>
              <div>
                <label for="hookBoxBorderW">Box Pad</label>
                <input id="hookBoxBorderW" type="number" min="0" max="80" />
              </div>
            </div>
          </div>

          <div id="badgeControls" hidden>
            <div class="section switch-row">
              <label for="badgeEnabled">Show Badge</label>
              <input id="badgeEnabled" type="checkbox" />
            </div>
            <div class="section">
              <label for="badgeText">Badge Text</label>
              <input id="badgeText" type="text" />
            </div>
            <div class="section">
              <label for="badgeFontFile">Font Family</label>
              <select id="badgeFontFile"></select>
            </div>
            <div class="section grid-2">
              <div>
                <label for="badgeX">X</label>
                <input id="badgeX" type="text" />
              </div>
              <div>
                <label for="badgeY">Y</label>
                <input id="badgeY" type="text" />
              </div>
            </div>
            <div class="section grid-2">
              <div>
                <label for="badgeFontSize">Font Size</label>
                <input id="badgeFontSize" type="number" min="10" max="120" />
              </div>
              <div>
                <label for="badgeBoxBorderW">Box Pad</label>
                <input id="badgeBoxBorderW" type="number" min="0" max="80" />
              </div>
            </div>
            <div class="section grid-2">
              <div>
                <label for="badgeFontColor">Text Color</label>
                <input id="badgeFontColor" type="color" />
              </div>
              <div>
                <label for="badgeBoxColor">Box Color</label>
                <input id="badgeBoxColor" type="color" />
              </div>
            </div>
          </div>
        </div>
      </section>

      <section class="panel studio">
        <div class="phone-wrap">
          <div class="phone" id="phone">
            <div class="subject"></div>
            <div class="safe-zone"></div>
            <div id="badgePreview" class="overlay badge-preview" data-kind="badge">HOT TAKE</div>
            <div id="hookPreview" class="overlay hook-preview selected" data-kind="hook">AI & SPACEX TARIK\nINVESTASI DARI\nBITCOIN</div>
            <div class="subtitle-sample">Subtitle kuning muncul di sini<br />dengan border hitam.</div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">Template JSON</div>
          <button class="btn primary" id="saveBtn">Save</button>
        </div>
        <div class="panel-body">
          <div class="section">
            <p class="hint">Preview ini approximation dari FFmpeg drawtext. Drag elemen di phone untuk set posisi absolute pixel target 1080x1920.</p>
          </div>
          <div class="section grid-2">
            <button class="btn full" id="copyBtn">Copy JSON</button>
            <button class="btn full danger" id="resetBtn">Reset Default</button>
          </div>
          <div class="section">
            <label for="jsonOutput">Live JSON</label>
            <textarea id="jsonOutput" class="json-output"></textarea>
          </div>
          <button class="btn primary full" id="applyJsonBtn">Apply JSON Text</button>
        </div>
      </section>
    </main>
  </div>

  <div id="toast" class="toast">Saved</div>

  <script>
    const target = { width: 1080, height: 1920 };
    const defaultTemplate = __DEFAULT_TEMPLATE__;
    let template = structuredClone(defaultTemplate);
    let fonts = [];
    const loadedFontFaces = new Map();
    let activeKind = 'hook';
    let dragState = null;

    const $ = (id) => document.getElementById(id);

    function tokenQuery() {
      const token = new URLSearchParams(location.search).get('token');
      return token ? `?token=${encodeURIComponent(token)}` : '';
    }

    function basePath() {
      let path = location.pathname;
      if (path.endsWith('/')) path = path.slice(0, -1);
      if (!path || path === '/') return '';
      return path;
    }

    function apiUrl(path) {
      return `${basePath()}${path}${tokenQuery()}`;
    }

    function tokenParamJoin() {
      const token = new URLSearchParams(location.search).get('token');
      return token ? `&token=${encodeURIComponent(token)}` : '';
    }

    function fontFamilyFor(path) {
      if (!path) return 'ui-sans-serif, Segoe UI, sans-serif';
      let hash = 0;
      for (const ch of path) hash = ((hash << 5) - hash + ch.charCodeAt(0)) | 0;
      return `cc-font-${Math.abs(hash)}`;
    }

    function ensureFontFace(path) {
      if (!path || loadedFontFaces.has(path)) return fontFamilyFor(path);
      const family = fontFamilyFor(path);
      const style = document.createElement('style');
      const url = `${basePath()}/api/font?path=${encodeURIComponent(path)}${tokenParamJoin()}`;
      style.textContent = `@font-face{font-family:${family};src:url("${url}");font-display:swap;}`;
      document.head.appendChild(style);
      loadedFontFaces.set(path, family);
      return family;
    }

    function addSelectOptionIfMissing(select, value, label) {
      if ([...select.options].some(option => option.value === value)) return;
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }

    function populateFontSelects() {
      for (const id of ['hookFontFile', 'badgeFontFile']) {
        const select = $(id);
        select.innerHTML = '';
        addSelectOptionIfMissing(select, '', 'Default subtitle font');
        for (const font of fonts) addSelectOptionIfMissing(select, font.path, font.label);
      }
    }

    function deepMerge(base, patch) {
      const out = structuredClone(base);
      for (const [key, value] of Object.entries(patch || {})) {
        if (value && typeof value === 'object' && !Array.isArray(value) && out[key] && typeof out[key] === 'object') {
          out[key] = deepMerge(out[key], value);
        } else {
          out[key] = value;
        }
      }
      return out;
    }

    function hexToFfmpeg(hex, alpha = null) {
      if (!hex) return '#ffffff';
      const value = hex.startsWith('#') ? hex : `#${hex}`;
      if (alpha) return `${value}${alpha}`;
      return value;
    }

    function ffmpegColorToHex(value, fallback = '#ffffff') {
      if (!value) return fallback;
      const text = String(value).trim();
      const named = {
        white: '#ffffff', black: '#000000', yellow: '#f4d000', red: '#ff3449',
        blue: '#3399ff', green: '#44f2a6', orange: '#ff7a1a'
      };
      if (named[text.toLowerCase()]) return named[text.toLowerCase()];
      const match = text.match(/#([0-9a-fA-F]{6})/);
      return match ? `#${match[1]}` : fallback;
    }

    function ffmpegAlpha(value, fallback = '@0.95') {
      const match = String(value || '').match(/(@[0-9.]+)/);
      return match ? match[1] : fallback;
    }

    function wrapText(text) {
      let words = text.trim().replace(/\s+/g, ' ');
      const transform = template.text_transform || 'upper';
      if (transform === 'upper') words = words.toUpperCase();
      if (transform === 'lower') words = words.toLowerCase();
      if (transform === 'title') words = words.toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
      const maxChars = Math.max(8, Number(template.max_line_chars || 20));
      const maxLines = Math.max(1, Number(template.max_lines || 3));
      const output = [];
      let line = '';
      for (const word of words.split(' ')) {
        const next = line ? `${line} ${word}` : word;
        if (line && next.length > maxChars) {
          output.push(line);
          line = word;
        } else {
          line = next;
        }
      }
      if (line) output.push(line);
      return output.slice(0, maxLines).join('\n');
    }

    function phoneScale() {
      return $('phone').clientWidth / target.width;
    }

    function parseCoord(value, axis, element) {
      const text = String(value ?? '').trim();
      const numeric = Number(text);
      if (Number.isFinite(numeric)) return numeric;
      const rect = element.getBoundingClientRect();
      const scale = phoneScale();
      const elW = rect.width / scale;
      const elH = rect.height / scale;
      if (text === '(w-text_w)/2') return (target.width - elW) / 2;
      if (text === '(h-text_h)/2') return (target.height - elH) / 2;
      const hMult = text.match(/^h\s*\*\s*([0-9.]+)$/);
      const wMult = text.match(/^w\s*\*\s*([0-9.]+)$/);
      if (axis === 'y' && hMult) return target.height * Number(hMult[1]);
      if (axis === 'x' && wMult) return target.width * Number(wMult[1]);
      return axis === 'x' ? (target.width - elW) / 2 : 230;
    }

    function styleOverlay(element, section) {
      const scale = phoneScale();
      const s = template[section];
      element.style.fontFamily = ensureFontFace(s.font_file || '');
      element.style.fontSize = `${Number(s.font_size || 80) * scale}px`;
      element.style.color = ffmpegColorToHex(s.font_color, '#ffffff');
      element.style.left = `${parseCoord(s.x, 'x', element) * scale}px`;
      element.style.top = `${parseCoord(s.y, 'y', element) * scale}px`;
      element.style.padding = s.box ? `${Number(s.box_border_w || 0) * scale}px` : '0';
      element.style.background = s.box ? ffmpegColorToHex(s.box_color, '#000000') : 'transparent';
      element.style.lineHeight = section === 'hook' ? '.96' : '1';
      element.style.textShadow = '';
      element.style.webkitTextStroke = '0 transparent';

      if (section === 'hook') {
        const border = Number(s.border_w || 0) * scale;
        element.style.webkitTextStroke = `${border}px ${ffmpegColorToHex(s.border_color, '#000000')}`;
        const sx = Number(s.shadow_x || 0) * scale;
        const sy = Number(s.shadow_y || 0) * scale;
        element.style.textShadow = `${sx}px ${sy}px 0 ${ffmpegColorToHex(s.shadow_color, '#000000')}`;
      }
    }

    function render() {
      $('badgePreview').hidden = !template.badge.enabled;
      $('badgePreview').textContent = template.badge.text || 'HOT TAKE';
      $('hookPreview').textContent = wrapText($('sampleText').value);
      styleOverlay($('badgePreview'), 'badge');
      styleOverlay($('hookPreview'), 'hook');
      $('jsonOutput').value = JSON.stringify(template, null, 2);
      syncInputs();
    }

    function syncInputs() {
      $('displaySeconds').value = template.display_seconds;
      $('fadeSeconds').value = template.fade_seconds;
      $('transform').value = template.text_transform;
      $('maxLineChars').value = template.max_line_chars;
      $('maxLines').value = template.max_lines;

      $('hookX').value = template.hook.x;
      $('hookY').value = template.hook.y;
      addSelectOptionIfMissing($('hookFontFile'), template.hook.font_file || '', template.hook.font_file || 'Default subtitle font');
      $('hookFontFile').value = template.hook.font_file || '';
      $('hookFontSize').value = template.hook.font_size;
      $('hookLineSpacing').value = template.hook.line_spacing;
      $('hookFontColor').value = ffmpegColorToHex(template.hook.font_color, '#ffffff');
      $('hookBorderColor').value = ffmpegColorToHex(template.hook.border_color, '#000000');
      $('hookBorderW').value = template.hook.border_w;
      $('hookShadowX').value = template.hook.shadow_x;
      $('hookShadowY').value = template.hook.shadow_y;
      $('hookShadowColor').value = ffmpegColorToHex(template.hook.shadow_color, '#000000');
      $('hookBox').checked = Boolean(template.hook.box);
      $('hookBoxColor').value = ffmpegColorToHex(template.hook.box_color, '#000000');
      $('hookBoxBorderW').value = template.hook.box_border_w;

      $('badgeEnabled').checked = Boolean(template.badge.enabled);
      $('badgeText').value = template.badge.text;
      addSelectOptionIfMissing($('badgeFontFile'), template.badge.font_file || '', template.badge.font_file || 'Default subtitle font');
      $('badgeFontFile').value = template.badge.font_file || '';
      $('badgeX').value = template.badge.x;
      $('badgeY').value = template.badge.y;
      $('badgeFontSize').value = template.badge.font_size;
      $('badgeFontColor').value = ffmpegColorToHex(template.badge.font_color, '#000000');
      $('badgeBoxColor').value = ffmpegColorToHex(template.badge.box_color, '#f4d000');
      $('badgeBoxBorderW').value = template.badge.box_border_w;
    }

    function updateFromInputs() {
      template.display_seconds = Number($('displaySeconds').value);
      template.fade_seconds = Number($('fadeSeconds').value);
      template.text_transform = $('transform').value;
      template.max_line_chars = Number($('maxLineChars').value);
      template.max_lines = Number($('maxLines').value);

      Object.assign(template.hook, {
        x: $('hookX').value,
        y: $('hookY').value,
        font_file: $('hookFontFile').value,
        font_size: Number($('hookFontSize').value),
        line_spacing: Number($('hookLineSpacing').value),
        font_color: hexToFfmpeg($('hookFontColor').value),
        border_color: hexToFfmpeg($('hookBorderColor').value, ffmpegAlpha(template.hook.border_color, '@0.95')),
        border_w: Number($('hookBorderW').value),
        shadow_x: Number($('hookShadowX').value),
        shadow_y: Number($('hookShadowY').value),
        shadow_color: hexToFfmpeg($('hookShadowColor').value, ffmpegAlpha(template.hook.shadow_color, '@0.65')),
        box: $('hookBox').checked,
        box_color: hexToFfmpeg($('hookBoxColor').value, ffmpegAlpha(template.hook.box_color, '@0.0')),
        box_border_w: Number($('hookBoxBorderW').value)
      });

      Object.assign(template.badge, {
        enabled: $('badgeEnabled').checked,
        text: $('badgeText').value,
        font_file: $('badgeFontFile').value,
        x: $('badgeX').value,
        y: $('badgeY').value,
        font_size: Number($('badgeFontSize').value),
        font_color: hexToFfmpeg($('badgeFontColor').value),
        box_color: hexToFfmpeg($('badgeBoxColor').value, ffmpegAlpha(template.badge.box_color, '@0.95')),
        box_border_w: Number($('badgeBoxBorderW').value)
      });
      render();
    }

    function selectKind(kind) {
      activeKind = kind;
      document.querySelectorAll('.tab').forEach(tab => tab.classList.toggle('active', tab.dataset.tab === kind));
      $('hookControls').hidden = kind !== 'hook';
      $('badgeControls').hidden = kind !== 'badge';
      $('hookPreview').classList.toggle('selected', kind === 'hook');
      $('badgePreview').classList.toggle('selected', kind === 'badge');
    }

    async function loadTemplate() {
      const res = await fetch(apiUrl('/api/template'));
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json();
      template = deepMerge(defaultTemplate, payload.template || {});
      $('templatePath').textContent = payload.path;
      render();
      toast('Template loaded');
    }

    async function loadFonts() {
      const res = await fetch(apiUrl('/api/fonts'));
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json();
      fonts = payload.fonts || [];
      populateFontSelects();
    }

    async function saveTemplate() {
      const res = await fetch(apiUrl('/api/template'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(template)
      });
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json();
      $('templatePath').textContent = payload.path;
      toast('Template saved');
    }

    function toast(message) {
      const el = $('toast');
      el.textContent = message;
      el.classList.add('show');
      clearTimeout(el.timer);
      el.timer = setTimeout(() => el.classList.remove('show'), 1800);
    }

    function setDragPosition(kind, leftPx, topPx) {
      const scale = phoneScale();
      template[kind].x = String(Math.round(leftPx / scale));
      template[kind].y = String(Math.round(topPx / scale));
      render();
    }

    for (const id of [
      'sampleText', 'displaySeconds', 'fadeSeconds', 'transform', 'maxLineChars', 'maxLines',
      'hookX', 'hookY', 'hookFontFile', 'hookFontSize', 'hookLineSpacing', 'hookFontColor', 'hookBorderColor',
      'hookBorderW', 'hookShadowX', 'hookShadowY', 'hookShadowColor', 'hookBox', 'hookBoxColor',
      'hookBoxBorderW', 'badgeEnabled', 'badgeText', 'badgeFontFile', 'badgeX', 'badgeY', 'badgeFontSize',
      'badgeFontColor', 'badgeBoxColor', 'badgeBoxBorderW'
    ]) {
      $(id).addEventListener('input', updateFromInputs);
      $(id).addEventListener('change', updateFromInputs);
    }

    document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => selectKind(tab.dataset.tab)));

    for (const el of [$('badgePreview'), $('hookPreview')]) {
      el.addEventListener('pointerdown', (event) => {
        const kind = el.dataset.kind;
        selectKind(kind);
        const rect = el.getBoundingClientRect();
        const phoneRect = $('phone').getBoundingClientRect();
        dragState = {
          kind,
          dx: event.clientX - rect.left,
          dy: event.clientY - rect.top,
          phoneLeft: phoneRect.left,
          phoneTop: phoneRect.top,
          phoneW: phoneRect.width,
          phoneH: phoneRect.height,
          elW: rect.width,
          elH: rect.height
        };
        el.setPointerCapture(event.pointerId);
      });
    }

    window.addEventListener('pointermove', (event) => {
      if (!dragState) return;
      const left = Math.min(Math.max(event.clientX - dragState.phoneLeft - dragState.dx, 0), dragState.phoneW - dragState.elW);
      const top = Math.min(Math.max(event.clientY - dragState.phoneTop - dragState.dy, 0), dragState.phoneH - dragState.elH);
      setDragPosition(dragState.kind, left, top);
    });

    window.addEventListener('pointerup', () => { dragState = null; });
    window.addEventListener('resize', render);

    $('saveBtn').addEventListener('click', () => saveTemplate().catch(err => toast(`Save failed: ${err.message}`)));
    $('reloadBtn').addEventListener('click', () => loadTemplate().catch(err => toast(`Load failed: ${err.message}`)));
    $('resetBtn').addEventListener('click', () => { template = structuredClone(defaultTemplate); render(); toast('Reset in preview'); });
    $('copyBtn').addEventListener('click', async () => {
      await navigator.clipboard.writeText(JSON.stringify(template, null, 2));
      toast('JSON copied');
    });
    $('applyJsonBtn').addEventListener('click', () => {
      try {
        template = deepMerge(defaultTemplate, JSON.parse($('jsonOutput').value));
        render();
        toast('JSON applied');
      } catch (err) {
        toast(`Invalid JSON: ${err.message}`);
      }
    });

    selectKind('hook');
    loadFonts()
      .then(loadTemplate)
      .catch(err => toast(`Load failed: ${err.message}`));
  </script>
</body>
</html>
"""


CLEAN_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ContentClipper Hook Studio</title>
  <style>
    :root {
      --bg: #1a1a1e;
      --panel: #232329;
      --panel-2: #2c2c33;
      --ink: #f0f0f4;
      --ink-2: #b8b8c4;
      --muted: #78788a;
      --line: rgba(255,255,255,.08);
      --line-2: rgba(255,255,255,.14);
      --accent: #6c5ce7;
      --accent-2: #a29bfe;
      --green: #00cec9;
      --danger: #e17055;
      --yellow: #fdcb6e;
      --radius: 12px;
      --bar-h: 48px;
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; color: var(--ink); background: var(--bg); font-family: ui-sans-serif,"Segoe UI",Aptos,sans-serif; font-size: 13px; }
    button,input,select,textarea { font: inherit; }
    button { cursor: pointer; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--line-2); border-radius: 3px; }

    .app { display: flex; flex-direction: column; height: 100vh; }
    .topbar {
      display: flex; align-items: center; gap: 6px; padding: 0 12px; height: var(--bar-h);
      background: var(--panel); border-bottom: 1px solid var(--line);
      flex-shrink: 0; z-index: 20; overflow-x: auto; overflow-y: hidden;
    }
    .brand { display: flex; align-items: center; gap: 8px; margin-right: auto; flex-shrink: 0; }
    .brand svg { width: 22px; height: 22px; }
    .brand strong { font-size: 15px; letter-spacing: -.03em; }
    .brand span { color: var(--muted); font-size: 11px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .tbtn {
      display: inline-flex; align-items: center; gap: 5px; padding: 6px 10px;
      border: 1px solid var(--line); border-radius: 8px;
      background: transparent; color: var(--ink-2); font-size: 12px; font-weight: 600;
      transition: .12s; white-space: nowrap; flex-shrink: 0;
    }
    .tbtn:hover { background: var(--panel-2); color: var(--ink); }
    .tbtn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    .tbtn.primary:hover { background: var(--accent-2); }
    .tbtn.green { background: var(--green); border-color: var(--green); color: #000; }
    .tbtn.danger { color: var(--danger); }
    .tbtn.icon { padding: 6px 7px; }
    .tbtn.active { background: var(--panel-2); color: var(--accent-2); border-color: var(--accent); }

    .workspace {
      display: grid; grid-template-columns: minmax(220px, 260px) minmax(420px, 1fr) minmax(300px, 340px);
      flex: 1; min-height: 0; overflow: hidden;
    }
    .sidebar {
      min-width: 0; background: var(--panel); border-right: 1px solid var(--line);
      display: flex; flex-direction: column;
    }
    .sidebar-tabs { display: flex; border-bottom: 1px solid var(--line); }
    .sidebar-tab {
      flex: 1; padding: 10px 6px; text-align: center; font-size: 11px; font-weight: 700;
      color: var(--muted); background: transparent; border: none; border-bottom: 2px solid transparent;
      transition: .12s;
    }
    .sidebar-tab.active { color: var(--accent-2); border-bottom-color: var(--accent); }
    .sidebar-body { flex: 1; min-height: 0; overflow-y: auto; padding: 10px; }

    .layer-item {
      display: flex; align-items: center; gap: 8px; padding: 8px 10px;
      border-radius: 8px; cursor: pointer; user-select: none; margin-bottom: 2px;
      transition: .1s;
    }
    .layer-item:hover { background: var(--panel-2); }
    .layer-item.active { background: var(--panel-2); outline: 1px solid var(--accent); }
    .layer-item .thumb {
      width: 28px; height: 28px; border-radius: 6px; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
      background: var(--panel-2); font-size: 14px;
    }
    .layer-item .info { flex: 1; min-width: 0; }
    .layer-item .info strong { display: block; font-size: 12px; }
    .layer-item .info span { color: var(--muted); font-size: 10px; }
    .layer-item .lbtn {
      background: none; border: none; color: var(--muted); padding: 4px; border-radius: 4px;
      font-size: 13px; line-height: 1;
    }
    .layer-item .lbtn:hover { color: var(--ink); background: rgba(255,255,255,.06); }

    .media-btn {
      display: flex; align-items: center; justify-content: center; gap: 8px;
      width: 100%; padding: 28px 12px; border: 1px dashed var(--line-2); border-radius: 10px;
      background: transparent; color: var(--muted); font-size: 12px; cursor: pointer; transition: .12s;
    }
    .media-btn:hover { border-color: var(--accent); color: var(--ink); background: rgba(108,92,231,.06); }

    .center-col { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; }

    .preview-area {
      flex: 1; display: flex; align-items: center; justify-content: center;
      background: radial-gradient(ellipse at center, var(--panel) 0%, var(--bg) 100%);
      position: relative; overflow: auto; min-height: 0; padding: 16px;
    }
    .phone-wrap {
      height: clamp(300px, calc(100vh - var(--bar-h) - 330px), 650px);
      aspect-ratio: 9/16; width: auto; max-width: 100%; border-radius: 28px; padding: 8px;
      background: #0a0a0c; box-shadow: 0 20px 60px rgba(0,0,0,.5);
    }
    .phone {
      position: relative; width: 100%; height: 100%; overflow: hidden; border-radius: 21px;
      background: linear-gradient(135deg, #2d1f1a, #111 54%, #1a2520);
      touch-action: none; user-select: none;
    }
    .phone video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: .35; pointer-events: none; }
    .subject {
      position: absolute; left: 50%; top: 22%; width: 56%; height: 45%;
      transform: translateX(-50%); border-radius: 42% 42% 20% 20%;
      background: linear-gradient(#e8c090, #3d2b1e); opacity: .75;
      box-shadow: 0 20px 44px rgba(0,0,0,.36);
    }
    .safe {
      position: absolute; inset: 58px 24px 80px;
      border: 1px dashed rgba(255,255,255,.2); border-radius: 18px; pointer-events: none;
    }
    .video-preview {
      position: absolute; z-index: 2; overflow: hidden; cursor: grab;
      background: linear-gradient(120deg,#c89050,#141415 54%,#3a5a4a);
      box-shadow: 0 12px 36px rgba(0,0,0,.3);
    }
    .video-preview::after {
      content: "VIDEO"; position: absolute; left: 8px; top: 6px;
      color: rgba(255,255,255,.7); font-size: 10px; font-weight: 900;
      letter-spacing: .06em; text-shadow: 0 1px 3px rgba(0,0,0,.5);
    }
    .video-preview.selected { outline: 2px solid var(--green); outline-offset: 6px; }
    .overlay {
      position: absolute; z-index: 5; cursor: grab; white-space: pre-line; font-weight: 900;
    }
    .overlay.selected { outline: 2px solid var(--green); outline-offset: 6px; }
    .badge-preview { line-height: 1; letter-spacing: .02em; }
    .hook-preview { text-align: center; line-height: .96; letter-spacing: -.04em; }
    .subtitle-preview { left: 8%; right: 8%; text-align: center; line-height: 1.08; }

    .styles-bar {
      height: 252px; flex-shrink: 0; background: #18181d;
      border-top: 1px solid var(--line); display: flex; flex-direction: column;
    }
    .styles-header {
      padding: 8px 14px; font-size: 11px; font-weight: 800; letter-spacing: .08em;
      text-transform: uppercase; color: var(--muted); display: flex; align-items: center; justify-content: space-between; gap: 12px;
      border-bottom: 1px solid var(--line);
    }
    .style-tabs { display: inline-flex; gap: 6px; padding: 3px; border-radius: 999px; background: rgba(255,255,255,.06); }
    .style-tab {
      border: 0; border-radius: 999px; padding: 5px 10px; background: transparent;
      color: var(--muted); font-size: 10px; font-weight: 900; text-transform: uppercase; letter-spacing: .06em;
    }
    .style-tab.active { background: var(--accent); color: white; }
    .styles-body {
      flex: 1; display: flex; gap: 16px; padding: 16px 18px 18px; overflow-x: auto;
      align-items: stretch;
    }
    .style-card {
      flex-shrink: 0; width: 198px; border: 1px solid rgba(255,255,255,.12);
      border-radius: 16px; padding: 12px; background: var(--card-bg, #22232a);
      cursor: pointer; display: grid; grid-template-rows: 1fr auto auto; gap: 8px;
      text-align: center; position: relative; overflow: hidden;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.03), 0 12px 30px rgba(0,0,0,.18);
      transition: transform .14s ease, border-color .14s ease, box-shadow .14s ease;
    }
    .style-card::before {
      content: ''; position: absolute; inset: 0; pointer-events: none; opacity: .55;
      border-radius: inherit;
      background: var(--card-texture, radial-gradient(circle at 20% 15%, rgba(255,255,255,.16), transparent 28%));
    }
    .style-card:hover {
      transform: translateY(-2px); border-color: var(--accent-2);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.05), 0 16px 42px rgba(0,0,0,.32);
    }
    .style-card .preview {
      min-height: 126px; display: grid; place-items: center; position: relative; z-index: 1;
      padding: 18px 14px; border-radius: 14px; background: var(--demo-bg, rgba(0,0,0,.16));
      overflow: hidden;
    }
    .style-card .preview span {
      display: inline-block; max-width: 154px; font-size: var(--demo-size, 20px);
      font-family: var(--demo-font, Impact, Haettenschweiler, sans-serif);
      font-weight: 950; line-height: .95; letter-spacing: var(--demo-tracking, -.04em);
      text-transform: uppercase; transform: rotate(var(--demo-rotate, 0deg));
      transform-origin: center;
      padding: var(--demo-pad, 0); border-radius: var(--demo-radius, 0);
      color: var(--demo-color, #fff); background: var(--demo-box, transparent);
      -webkit-text-stroke: var(--demo-stroke, 0 #000);
      text-shadow: var(--demo-shadow, none); box-shadow: var(--demo-box-shadow, none);
      overflow-wrap: normal; word-break: normal; white-space: normal;
    }
    .style-card .name { position: relative; z-index: 1; font-size: 12px; color: #f3f3f7; font-weight: 850; letter-spacing: -.01em; }
    .style-card .badge-tag { position: relative; z-index: 1; justify-self: center; font-size: 9px; color: rgba(255,255,255,.72); background: rgba(0,0,0,.28); padding: 3px 8px; border-radius: 999px; }

    .prop-panel {
      min-width: 0; background: var(--panel); border-left: 1px solid var(--line);
      display: flex; flex-direction: column;
    }
    .prop-header {
      padding: 10px 12px; font-weight: 700; font-size: 12px;
      border-bottom: 1px solid var(--line); color: var(--ink-2);
    }
    .prop-body { flex: 1; min-height: 0; overflow-y: auto; padding: 10px 12px; }
    .field { display: grid; gap: 5px; margin-bottom: 10px; }
    .field label { color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
    .row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .row-3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
    input, select, textarea {
      width: 100%; border: 1px solid var(--line); border-radius: 8px;
      padding: 7px 9px; background: var(--panel-2); color: var(--ink); outline: none; font-size: 12px;
    }
    input:focus, select:focus, textarea:focus { border-color: var(--accent); }
    input[type="color"] { height: 34px; padding: 2px; cursor: pointer; }
    input[type="checkbox"] { width: auto; height: auto; accent-color: var(--accent); }
    textarea { min-height: 60px; resize: vertical; line-height: 1.35; }

    .section-title { color: var(--muted); font-size: 10px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; margin: 14px 0 8px; }

    .json-box { min-height: 120px; font-family: ui-monospace,"Cascadia Code",monospace; font-size: 11px; }
    .toast {
      position: fixed; right: 18px; bottom: 18px;
      background: var(--panel-2); color: var(--ink);
      border: 1px solid var(--line-2); border-radius: 10px;
      padding: 10px 14px; font-weight: 600; font-size: 12px;
      opacity: 0; transform: translateY(8px); transition: .16s ease;
      pointer-events: none; z-index: 40;
    }
    .toast.show { opacity: 1; transform: translateY(0); }

    @media (max-height: 780px) {
      .styles-bar { height: 216px; }
      .styles-body { gap: 12px; padding: 12px 14px 14px; }
      .style-card { width: 178px; padding: 10px; }
      .style-card .preview { min-height: 96px; padding: 14px 12px; }
      .style-card .preview span { max-width: 138px; font-size: var(--demo-size, 18px); }
      .style-card .name { font-size: 11px; }
      .phone-wrap { height: clamp(270px, calc(100vh - var(--bar-h) - 284px), 560px); }
    }

    @media (max-height: 650px) {
      .styles-bar { height: 184px; }
      .style-card { width: 160px; gap: 5px; }
      .style-card .preview { min-height: 78px; padding: 10px; }
      .style-card .preview span { max-width: 124px; font-size: var(--demo-size, 16px); }
      .badge-tag { display: none; }
      .phone-wrap { height: clamp(230px, calc(100vh - var(--bar-h) - 240px), 460px); }
    }

    @media (max-width: 1180px) {
      .workspace {
        grid-template-columns: minmax(210px, 240px) minmax(420px, 1fr) minmax(290px, 320px);
        overflow-x: auto;
      }
    }

    @media (max-width: 860px) {
      .app { height: auto; min-height: 100vh; }
      .workspace { display: block; overflow: visible; }
      .sidebar, .prop-panel { width: auto; min-height: 260px; border-left: 0; border-right: 0; border-bottom: 1px solid var(--line); }
      .center-col { min-height: 720px; }
      .preview-area { min-height: 460px; }
      .phone-wrap { height: min(520px, calc(100vw * 1.55)); }
      .styles-bar { height: 230px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <svg viewBox="0 0 24 24" fill="none" stroke="var(--accent-2)" stroke-width="2"><path d="M6 5h12a2 2 0 012 2v10a2 2 0 01-2 2H6a2 2 0 01-2-2V7a2 2 0 012-2z"/><path d="M10 9l5 3-5 3V9z"/></svg>
        <strong>Hook Studio</strong>
        <span id="templatePath">Loading...</span>
      </div>
      <button class="tbtn icon" id="undoBtn" title="Undo (Ctrl+Z)">&#x21A9;</button>
      <button class="tbtn icon" id="redoBtn" title="Redo (Ctrl+Shift+Z)">&#x21AA;</button>
      <button class="tbtn icon" id="mediaBtn" title="Import Background Video">&#x1F4F9;</button>
      <button class="tbtn" id="copyBtn">Copy JSON</button>
      <button class="tbtn danger" id="resetBtn">Reset</button>
      <button class="tbtn" id="reloadBtn">Reload</button>
      <button class="tbtn primary" id="saveBtn">&#x1F4BE; Save</button>
    </header>

    <div class="workspace">
      <aside class="sidebar">
        <div class="sidebar-tabs">
          <button class="sidebar-tab active" data-tab="layers">Layers</button>
          <button class="sidebar-tab" data-tab="media">Media</button>
        </div>
        <div class="sidebar-body" id="layersTab">
          <div class="layers" id="layerList"></div>
          <div style="margin-top:14px">
            <div class="field"><label>Sample Hook</label><textarea id="sampleText">AI & SpaceX tarik investasi dari Bitcoin</textarea></div>
            <div class="row">
              <div class="field"><label>Display Sec</label><input data-path="display_seconds" type="number" step="0.1" min="0.5"></div>
              <div class="field"><label>Fade Sec</label><input data-path="fade_seconds" type="number" step="0.05" min="0"></div>
            </div>
            <div class="row">
              <div class="field"><label>Text Case</label><select data-path="text_transform"><option value="upper">UPPER</option><option value="title">Title</option><option value="lower">lower</option><option value="none">Original</option></select></div>
              <div class="field"><label>Max Lines</label><input data-path="max_lines" type="number" min="1" max="5"></div>
            </div>
            <div class="field"><label>Wrap Chars</label><input data-path="max_line_chars" type="number" min="8" max="60"></div>
          </div>
        </div>
        <div class="sidebar-body" id="mediaTab" hidden>
          <label class="media-btn" id="importMediaBtn">
            &#x1F4C2; Import Video / Image
            <input type="file" accept="video/*,image/*" id="mediaFileInput" style="display:none">
          </label>
          <div id="mediaList" style="margin-top:10px"></div>
        </div>
      </aside>

      <main class="center-col">
        <div class="preview-area">
          <div class="phone-wrap">
            <div class="phone" id="phone">
              <video id="bgVideo" muted loop playsinline></video>
              <div class="subject"></div>
              <div class="video-preview" id="videoPreview" data-layer="video"></div>
              <div class="safe"></div>
              <div class="overlay badge-preview" id="badgePreview" data-layer="badge">HOT TAKE</div>
              <div class="overlay hook-preview" id="hookPreview" data-layer="hook">AI & SPACEX TARIK\nINVESTASI DARI\nBITCOIN</div>
              <div class="overlay subtitle-preview" id="subtitlePreview" data-layer="subtitle">Subtitle kuning muncul di sini<br>border bisa diatur.</div>
            </div>
          </div>
        </div>

        <div class="styles-bar" id="stylesBar">
          <div class="styles-header"><span id="stylesTitle">Text Styles</span><div class="style-tabs"><button class="style-tab active" id="textStyleTab" type="button">Text</button><button class="style-tab" id="subtitleStyleTab" type="button">Subtitle</button></div></div>
          <div class="styles-body" id="stylesBody"></div>
        </div>
      </main>

      <aside class="prop-panel">
        <div class="prop-header" id="propertiesTitle">Properties</div>
        <div class="prop-body">
          <div id="properties"></div>
          <div class="section-title">JSON</div>
          <div class="field">
            <textarea id="jsonOutput" class="json-box"></textarea>
          </div>
          <button class="tbtn" id="applyJsonBtn" style="width:100%;justify-content:center">Apply JSON</button>
        </div>
      </aside>
    </div>
  </div>
  <div class="toast" id="toast">Saved</div>

  <script>
    const target = { width: 1080, height: 1920 };
    const defaultTemplate = __DEFAULT_TEMPLATE__;
    let template = structuredClone(defaultTemplate);
    let fonts = [];
    let selected = 'hook';
    let styleMode = 'text';
    let drag = null;
    let history = [], historyIdx = -1;
    let mediaFile = null;
    const loadedFonts = new Map();
    const $ = id => document.getElementById(id);


    const layerKeys = ['video','hook','badge','subtitle'];
    const layerLabels = { video:'Video Overlay', hook:'Hook Text', badge:'Badge', subtitle:'Subtitle' };
    const layerIcons = { video:'&#x1F4F9;', hook:'T', badge:'&#x2B50;', subtitle:'&#x1F4AC;' };
    const templateFontChoices = {
      'Sticker Punch':['Poppins-ExtraBold.ttf','ArchivoBlack-Regular.ttf','FreeSansBoldOblique.ttf'],
      'Breaking News':['BebasNeue-Regular.ttf','Anton-Regular.ttf','BarlowCondensed-Bold.ttf'],
      'Meme Caption':['Anton-Regular.ttf','ArchivoBlack-Regular.ttf','Poppins-ExtraBold.ttf'],
      'Neon Alley':['JetBrainsMono-Bold.ttf','DejaVuSansMono-Bold.ttf'],
      'Gold Vault':['Outfit-Bold.ttf','Poppins-Bold.ttf','LiberationSerif-Bold.ttf'],
      'Tape Label':['JetBrainsMono-Bold.ttf','LiberationMono-Bold.ttf'],
      'Cyber Terminal':['JetBrainsMono-Bold.ttf','DejaVuSansMono-BoldOblique.ttf'],
      'Comic Pop':['ArchivoBlack-Regular.ttf','Poppins-ExtraBold.ttf'],
      'Glass Plate':['Manrope-Bold.ttf','Outfit-Bold.ttf','Loma-Bold.otf'],
      'Red Stamp':['BarlowCondensed-Bold.ttf','BebasNeue-Regular.ttf'],
      'Karaoke Bite':['Poppins-ExtraBold.ttf','Anton-Regular.ttf'],
      'Editorial Black':['BebasNeue-Regular.ttf','ArchivoBlack-Regular.ttf'],
      'Candy Block':['Poppins-ExtraBold.ttf','Outfit-Bold.ttf'],
      'Warning Plate':['Anton-Regular.ttf','ArchivoBlack-Regular.ttf'],
      'Purple Stream':['JetBrainsMono-Bold.ttf','Manrope-Bold.ttf'],
      'White Receipt':['JetBrainsMono-Bold.ttf','LiberationMono-Bold.ttf'],
      'Docu Serif':['Outfit-Bold.ttf','Manrope-Bold.ttf','DejaVuSerif-Bold.ttf'],
      'Sports Label':['BarlowCondensed-Bold.ttf','Anton-Regular.ttf'],
      'Finance Ticker':['JetBrainsMono-Bold.ttf','Manrope-Bold.ttf'],
      'Y2K Bubble':['Poppins-ExtraBold.ttf','Outfit-Bold.ttf'],
      'Horror Cut':['BebasNeue-Regular.ttf','Anton-Regular.ttf'],
      'Clean Subtitle':['Manrope-Bold.ttf','Outfit-Bold.ttf'],
      'Creator Pop':['Poppins-ExtraBold.ttf','ArchivoBlack-Regular.ttf'],
      'Sub Clean':['Manrope-Bold.ttf','Outfit-Bold.ttf'],
      'Sub Karaoke':['Poppins-ExtraBold.ttf','Anton-Regular.ttf'],
      'Sub News':['BarlowCondensed-Bold.ttf','BebasNeue-Regular.ttf'],
      'Sub Cinema':['Outfit-Bold.ttf','Manrope-Bold.ttf'],
      'Sub Terminal':['JetBrainsMono-Bold.ttf','DejaVuSansMono-Bold.ttf'],
      'Sub Creator':['Poppins-Bold.ttf','Manrope-Bold.ttf'],
      'Sub Bubble':['Poppins-ExtraBold.ttf','Outfit-Bold.ttf'],
      'Sub Minimal':['Manrope-Bold.ttf','Outfit-Bold.ttf']
    };
    const textTemplates = [
      {name:'Sticker Punch',tag:'sans + bubble',sample:'WOW!',card:'#231f20',texture:'radial-gradient(circle at 15% 15%, rgba(255,242,0,.36), transparent 30%), radial-gradient(circle at 88% 18%, rgba(255,80,80,.35), transparent 26%)',demoBg:'#fff200',demo:{rotate:'-3deg',pad:'7px 11px',radius:'999px',box:'#fff200',boxShadow:'6px 6px 0 #000',size:'22px'},style:{font_size:86,font_color:'#111111',border_w:0,border_color:'#000000',shadow_x:6,shadow_y:6,shadow_color:'#000000',box:true,box_color:'#fff200@0.96',box_border_w:24,box_radius:42,box_shadow:'10px 10px 0 rgba(0,0,0,.75)',rotate:-2,letter_spacing:-2,line_spacing:2}},
      {name:'Breaking News',tag:'tabloid sans',sample:'BREAKING',card:'linear-gradient(135deg,#260000,#111)',texture:'linear-gradient(90deg, rgba(255,0,0,.38), transparent 46%)',demoBg:'#150000',demo:{box:'#d71920',pad:'5px 10px',radius:'2px',shadow:'4px 4px 0 #000',tracking:'.02em'},style:{font_size:72,font_color:'#ffffff',border_w:2,border_color:'#000000',shadow_x:4,shadow_y:4,shadow_color:'#000000',box:true,box_color:'#d71920@0.92',box_border_w:22,box_radius:4,box_shadow:'8px 8px 0 rgba(0,0,0,.8)',letter_spacing:1,line_spacing:0}},
      {name:'Meme Caption',tag:'impact sans',sample:'NO WAY',card:'#f2f2f2',texture:'repeating-linear-gradient(45deg, rgba(0,0,0,.06) 0 8px, transparent 8px 16px)',demoBg:'#ffffff',demo:{shadow:'none',stroke:'2px #000',size:'20px'},style:{font_size:88,font_color:'#ffffff',border_w:6,border_color:'#000000',shadow_x:0,shadow_y:0,shadow_color:'#000000',box:false,letter_spacing:-1,line_spacing:0}},
      {name:'Neon Alley',tag:'mono glow',sample:'NEON',card:'linear-gradient(135deg,#050022,#001d2a)',texture:'radial-gradient(circle at 80% 20%, rgba(0,242,255,.5), transparent 26%), radial-gradient(circle at 20% 80%, rgba(255,0,180,.35), transparent 28%)',demoBg:'#070416',demo:{color:'#00eaff',stroke:'1px #062f38',shadow:'0 0 8px #00eaff, 0 0 18px #008cff',tracking:'.06em'},style:{font_size:82,font_color:'#00d4ff',border_w:2,border_color:'#001d33',shadow_x:0,shadow_y:0,shadow_color:'#00aaff',box:true,box_color:'#050022@0.58',box_border_w:18,box_radius:18,box_shadow:'0 0 24px rgba(0,212,255,.45)',letter_spacing:4,line_spacing:2}},
      {name:'Gold Vault',tag:'serif luxury',sample:'GOLD',card:'linear-gradient(135deg,#2b1b00,#0c0b08)',texture:'radial-gradient(circle at 50% 0%, rgba(255,215,0,.45), transparent 36%)',demoBg:'#130d00',demo:{color:'#ffd56b',stroke:'1px #4d2c00',shadow:'3px 3px 0 #000'},style:{font_size:78,font_color:'#ffd56b',border_w:3,border_color:'#4d2c00',shadow_x:5,shadow_y:5,shadow_color:'#000000',box:false,letter_spacing:-1,line_spacing:-2}},
      {name:'Tape Label',tag:'mono cutout',sample:'HOT TAKE',card:'#191919',texture:'linear-gradient(90deg, rgba(255,255,255,.08), transparent), repeating-linear-gradient(-12deg, transparent 0 10px, rgba(255,255,255,.06) 10px 12px)',demoBg:'#171717',demo:{box:'#f5e6b8',color:'#111',pad:'5px 9px',radius:'1px',rotate:'-2deg',shadow:'4px 4px 0 rgba(0,0,0,.75)'},style:{font_size:70,font_color:'#111111',border_w:0,border_color:'#000000',shadow_x:4,shadow_y:4,shadow_color:'#000000',box:true,box_color:'#f5e6b8@0.94',box_border_w:18,box_radius:0,box_shadow:'8px 8px 0 rgba(0,0,0,.65)',box_clip:'polygon(3% 0, 100% 5%, 97% 100%, 0 94%)',rotate:-2,letter_spacing:2,line_spacing:1}},
      {name:'Cyber Terminal',tag:'mono tech',sample:'ALERT',card:'linear-gradient(135deg,#001b13,#020806)',texture:'linear-gradient(0deg, rgba(0,255,136,.09) 50%, transparent 50%)',demoBg:'#00140d',demo:{color:'#00ff88',stroke:'1px #001f12',shadow:'0 0 10px #00ff88',tracking:'.08em'},style:{font_size:72,font_color:'#00ff88',border_w:2,border_color:'#003322',shadow_x:3,shadow_y:3,shadow_color:'#001a0f',box:true,box_color:'#00170f@0.72',box_border_w:16,box_radius:8,box_shadow:'0 0 20px rgba(0,255,136,.36)',letter_spacing:4,line_spacing:2}},
      {name:'Comic Pop',tag:'cartoon sans',sample:'BOOM',card:'#241120',texture:'radial-gradient(circle at 25% 25%, #ff4fd8 0 7%, transparent 8%), radial-gradient(circle at 75% 65%, #ffdd00 0 9%, transparent 10%)',demoBg:'#ff4fd8',demo:{color:'#fff',stroke:'2px #260015',shadow:'5px 5px 0 #111',rotate:'2deg',size:'22px'},style:{font_size:86,font_color:'#ffffff',border_w:5,border_color:'#260015',shadow_x:6,shadow_y:6,shadow_color:'#111111',box:true,box_color:'#ff4fd8@0.18',box_border_w:10,box_radius:30,rotate:2,letter_spacing:-2,line_spacing:0}},
      {name:'Glass Plate',tag:'sans glass',sample:'INSIGHT',card:'linear-gradient(135deg,#1e2638,#0c1018)',texture:'radial-gradient(circle at 20% 0%, rgba(255,255,255,.18), transparent 30%)',demoBg:'rgba(255,255,255,.08)',demo:{box:'rgba(255,255,255,.18)',pad:'6px 10px',radius:'10px',color:'#ffffff',stroke:'0 #000',shadow:'0 2px 10px rgba(0,0,0,.65)'},style:{font_size:70,font_color:'#ffffff',border_w:1,border_color:'#728197',shadow_x:0,shadow_y:4,shadow_color:'#000000',box:true,box_color:'#1f2733@0.72',box_border_w:18,box_radius:18,box_shadow:'0 12px 30px rgba(0,0,0,.32)',line_spacing:1}},
      {name:'Red Stamp',tag:'serif italic',sample:'VIRAL',card:'#250707',texture:'repeating-linear-gradient(-8deg, rgba(255,255,255,.05) 0 7px, transparent 7px 14px)',demoBg:'#120202',demo:{color:'#ff3b30',stroke:'2px #ffb3ad',shadow:'3px 3px 0 #000',rotate:'-5deg',tracking:'.04em'},style:{font_size:82,font_color:'#ff3b30',border_w:3,border_color:'#ffd0cc',shadow_x:5,shadow_y:5,shadow_color:'#000000',box:true,box_color:'#250707@0.20',box_border_w:12,box_radius:6,rotate:-5,letter_spacing:2,line_spacing:0}},
      {name:'Karaoke Bite',tag:'caption sans',sample:'INI DIA',card:'linear-gradient(135deg,#202000,#090900)',texture:'radial-gradient(circle at 80% 20%, rgba(255,242,0,.34), transparent 30%)',demoBg:'#050500',demo:{color:'#fff200',stroke:'2px #000',shadow:'3px 3px 0 #000'},style:{font_size:78,font_color:'#fff200',border_w:4,border_color:'#000000',shadow_x:3,shadow_y:3,shadow_color:'#000000',box:false,letter_spacing:-1,line_spacing:1}},
      {name:'Editorial Black',tag:'serif headline',sample:'THE TAKE',card:'#e9e5dc',texture:'linear-gradient(90deg, rgba(0,0,0,.08), transparent 48%)',demoBg:'#f4f1ea',demo:{color:'#111',stroke:'0 #000',shadow:'none',tracking:'-.08em'},style:{font_size:84,font_color:'#111111',border_w:0,border_color:'#000000',shadow_x:0,shadow_y:0,shadow_color:'#000000',box:false,letter_spacing:-4,line_spacing:-5}},
      {name:'Candy Block',tag:'rounded sans',sample:'OMG',card:'linear-gradient(135deg,#ff9ad5,#fff2f7)',texture:'radial-gradient(circle at 22% 30%, rgba(255,255,255,.65), transparent 18%)',demoBg:'#ffd6ea',demo:{color:'#ff2d95',stroke:'2px #ffffff',shadow:'4px 4px 0 #6d003a',box:'#fff',pad:'4px 9px',radius:'999px'},style:{font_size:82,font_color:'#ff2d95',border_w:4,border_color:'#ffffff',shadow_x:5,shadow_y:5,shadow_color:'#6d003a',box:true,box_color:'#ffffff@0.9',box_border_w:18,box_radius:999,box_shadow:'6px 6px 0 rgba(109,0,58,.72)',letter_spacing:-2,line_spacing:1}},
      {name:'Warning Plate',tag:'industrial',sample:'STOP!',card:'linear-gradient(135deg,#191200,#3b2600)',texture:'repeating-linear-gradient(45deg, rgba(255,214,10,.22) 0 8px, transparent 8px 16px)',demoBg:'#ffd60a',demo:{color:'#111',stroke:'0 #000',shadow:'3px 3px 0 rgba(0,0,0,.5)',box:'#ffd60a',pad:'6px 12px',radius:'4px'},style:{font_size:80,font_color:'#111111',border_w:0,border_color:'#000000',shadow_x:4,shadow_y:4,shadow_color:'#000000',box:true,box_color:'#ffd60a@0.95',box_border_w:20,box_radius:6,box_shadow:'8px 8px 0 rgba(0,0,0,.65)',letter_spacing:1,line_spacing:0}},
      {name:'Purple Stream',tag:'mono gaming',sample:'LEVEL UP',card:'linear-gradient(135deg,#1b083d,#09050f)',texture:'radial-gradient(circle at 70% 30%, rgba(185,103,255,.5), transparent 34%)',demoBg:'#130828',demo:{color:'#d6b2ff',stroke:'2px #2d0054',shadow:'0 0 12px #9b5cff'},style:{font_size:78,font_color:'#d6b2ff',border_w:3,border_color:'#2d0054',shadow_x:0,shadow_y:0,shadow_color:'#9b5cff',box:true,box_color:'#1b083d@0.52',box_border_w:16,box_radius:22,box_shadow:'0 0 26px rgba(155,92,255,.42)',letter_spacing:1,line_spacing:0}},
      {name:'White Receipt',tag:'mono label',sample:'FACTS',card:'#0e0e0e',texture:'linear-gradient(180deg, rgba(255,255,255,.06), transparent)',demoBg:'#151515',demo:{box:'#f7f7f2',color:'#111',pad:'7px 10px',radius:'0',tracking:'.04em'},style:{font_size:68,font_color:'#111111',border_w:0,border_color:'#000000',shadow_x:3,shadow_y:3,shadow_color:'#000000',box:true,box_color:'#f7f7f2@0.96',box_border_w:20,box_radius:0,box_shadow:'6px 6px 0 rgba(0,0,0,.72)',box_clip:'polygon(0 0, 97% 0, 100% 100%, 3% 95%)',letter_spacing:3,line_spacing:2}},
      {name:'Docu Serif',tag:'documentary',sample:'THE TRUTH',card:'linear-gradient(135deg,#1d1711,#4a3a27)',texture:'linear-gradient(90deg, rgba(245,232,204,.18), transparent 55%)',demoBg:'#efe1c5',demo:{color:'#24180f',stroke:'0 #000',shadow:'2px 2px 0 rgba(0,0,0,.22)',tracking:'-.07em',size:'19px'},style:{font_size:80,font_color:'#24180f',border_w:0,border_color:'#000000',shadow_x:2,shadow_y:2,shadow_color:'#000000@0.25',box:true,box_color:'#efe1c5@0.88',box_border_w:22,box_radius:10,letter_spacing:-3,line_spacing:-4}},
      {name:'Sports Label',tag:'action',sample:'GOAL!',card:'linear-gradient(135deg,#041b43,#ff5a00)',texture:'radial-gradient(circle at 20% 25%, rgba(255,255,255,.25), transparent 24%)',demoBg:'#06245a',demo:{color:'#ffffff',stroke:'2px #ff7a00',shadow:'5px 5px 0 #00112e',rotate:'-4deg',size:'23px'},style:{font_size:90,font_color:'#ffffff',border_w:5,border_color:'#ff7a00',shadow_x:7,shadow_y:7,shadow_color:'#00112e',box:false,rotate:-4,letter_spacing:-2,line_spacing:0}},
      {name:'Finance Ticker',tag:'market mono',sample:'+24.8%',card:'linear-gradient(135deg,#001510,#061f1a)',texture:'linear-gradient(90deg, rgba(0,255,170,.16), transparent 60%)',demoBg:'#020d0a',demo:{color:'#72ffb6',stroke:'1px #003520',shadow:'0 0 12px rgba(114,255,182,.8)',tracking:'.06em',size:'18px'},style:{font_size:72,font_color:'#72ffb6',border_w:2,border_color:'#003520',shadow_x:0,shadow_y:0,shadow_color:'#72ffb6',box:true,box_color:'#020d0a@0.78',box_border_w:18,box_radius:8,letter_spacing:4,line_spacing:2}},
      {name:'Y2K Bubble',tag:'chrome pop',sample:'TREND',card:'linear-gradient(135deg,#9efcff,#ff9df3)',texture:'radial-gradient(circle at 80% 18%, rgba(255,255,255,.8), transparent 18%)',demoBg:'#e8fcff',demo:{box:'#ffffff',color:'#37b7ff',stroke:'2px #ffffff',shadow:'4px 4px 0 #ff5fd2',pad:'6px 10px',radius:'999px'},style:{font_size:82,font_color:'#37b7ff',border_w:4,border_color:'#ffffff',shadow_x:5,shadow_y:5,shadow_color:'#ff5fd2',box:true,box_color:'#ffffff@0.94',box_border_w:20,box_radius:999,letter_spacing:-2,line_spacing:0}},
      {name:'Horror Cut',tag:'serif cut',sample:'RISK',card:'linear-gradient(135deg,#140000,#2b0000)',texture:'repeating-linear-gradient(-18deg, rgba(255,0,0,.14) 0 6px, transparent 6px 13px)',demoBg:'#120000',demo:{color:'#f7eee8',stroke:'1px #8b0000',shadow:'5px 5px 0 #000',rotate:'3deg',tracking:'-.04em'},style:{font_size:88,font_color:'#f7eee8',border_w:2,border_color:'#8b0000',shadow_x:6,shadow_y:6,shadow_color:'#000000',box:true,box_color:'#2b0000@0.32',box_border_w:12,box_radius:2,box_clip:'polygon(6% 0, 100% 10%, 94% 100%, 0 88%)',rotate:3,letter_spacing:-2,line_spacing:-2}},
      {name:'Clean Subtitle',tag:'pill caption',sample:'KEY POINT',card:'linear-gradient(135deg,#111827,#374151)',texture:'radial-gradient(circle at 30% 0%, rgba(255,255,255,.16), transparent 28%)',demoBg:'#0b1220',demo:{box:'rgba(0,0,0,.72)',color:'#ffffff',pad:'7px 12px',radius:'999px',shadow:'0 4px 14px rgba(0,0,0,.6)',tracking:'-.02em',size:'17px'},style:{font_size:66,font_color:'#ffffff',border_w:0,border_color:'#000000',shadow_x:0,shadow_y:3,shadow_color:'#000000',box:true,box_color:'#000000@0.68',box_border_w:20,box_radius:999,letter_spacing:-1,line_spacing:2}},
      {name:'Creator Pop',tag:'social hook',sample:'WATCH THIS',card:'linear-gradient(135deg,#0061ff,#60efff)',texture:'radial-gradient(circle at 15% 80%, rgba(255,255,255,.32), transparent 24%)',demoBg:'#001d4d',demo:{box:'#ffffff',color:'#0061ff',pad:'5px 10px',radius:'12px',shadow:'5px 5px 0 #04111f',tracking:'-.04em',size:'18px'},style:{font_size:76,font_color:'#0061ff',border_w:0,border_color:'#000000',shadow_x:5,shadow_y:5,shadow_color:'#04111f',box:true,box_color:'#ffffff@0.96',box_border_w:20,box_radius:18,letter_spacing:-2,line_spacing:0}}
    ];

    const subtitleTemplates = [
      {name:'Sub Clean',tag:'word pill',sample:'clean readable caption',card:'linear-gradient(135deg,#101827,#263247)',texture:'radial-gradient(circle at 18% 0%, rgba(255,255,255,.18), transparent 30%)',demoBg:'#050b14',demo:{box:'rgba(0,0,0,.72)',color:'#ffffff',pad:'8px 13px',radius:'999px',shadow:'0 5px 16px rgba(0,0,0,.6)',size:'15px',tracking:'-.02em'},style:{enabled:true,mode:'word',word_animation:'pop',font_size:48,font_color:'#ffffff',outline:0,outline_color:'#000000',shadow:1,border_style:3,back_color:'#000000@0.58',margin_v:70,alignment:2,box_radius:999}},
      {name:'Sub Karaoke',tag:'word yellow',sample:'kata penting nyala',card:'linear-gradient(135deg,#171500,#3c3300)',texture:'radial-gradient(circle at 80% 20%, rgba(255,242,0,.38), transparent 34%)',demoBg:'#050500',demo:{color:'#fff200',stroke:'2px #000',shadow:'3px 3px 0 #000',size:'17px'},style:{enabled:true,mode:'word',word_animation:'pulse',font_size:52,font_color:'#fff200',outline:3,outline_color:'#000000',shadow:0,border_style:1,back_color:'#000000@0.0',margin_v:64,alignment:2}},
      {name:'Sub News',tag:'word lower bar',sample:'market update terbaru',card:'linear-gradient(135deg,#2c0000,#080808)',texture:'linear-gradient(90deg, rgba(255,0,0,.35), transparent 52%)',demoBg:'#160000',demo:{box:'#c50016',color:'#fff',pad:'7px 12px',radius:'2px',shadow:'4px 4px 0 #000',tracking:'.02em'},style:{enabled:true,mode:'word',word_animation:'pop',font_size:46,font_color:'#ffffff',outline:1,outline_color:'#000000',shadow:1,border_style:3,back_color:'#c50016@0.78',margin_v:56,alignment:2,box_radius:2}},
      {name:'Sub Cinema',tag:'word fade',sample:'quiet cinematic line',card:'linear-gradient(135deg,#101010,#313131)',texture:'linear-gradient(180deg, rgba(255,255,255,.10), transparent)',demoBg:'#060606',demo:{color:'#f5f0e8',stroke:'1px #111',shadow:'0 4px 10px #000',size:'16px',tracking:'-.01em'},style:{enabled:true,mode:'word',word_animation:'fade',font_size:44,font_color:'#f5f0e8',outline:1.4,outline_color:'#111111',shadow:1.2,border_style:1,back_color:'#000000@0.0',margin_v:74,alignment:2}},
      {name:'Sub Terminal',tag:'word mono',sample:'system status online',card:'linear-gradient(135deg,#00150e,#001f18)',texture:'linear-gradient(0deg, rgba(0,255,136,.10) 50%, transparent 50%)',demoBg:'#020d0a',demo:{box:'rgba(0,20,12,.82)',color:'#72ffb6',pad:'7px 11px',radius:'6px',shadow:'0 0 12px rgba(114,255,182,.7)',tracking:'.04em',size:'14px'},style:{enabled:true,mode:'word',word_animation:'fade',font_size:42,font_color:'#72ffb6',outline:1,outline_color:'#003520',shadow:0,border_style:3,back_color:'#00150e@0.75',margin_v:68,alignment:2,box_radius:6}},
      {name:'Sub Creator',tag:'word social',sample:'ini bagian paling penting',card:'linear-gradient(135deg,#0061ff,#60efff)',texture:'radial-gradient(circle at 20% 78%, rgba(255,255,255,.35), transparent 28%)',demoBg:'#002763',demo:{box:'#ffffff',color:'#0057ff',pad:'7px 12px',radius:'14px',shadow:'4px 4px 0 #00142e',size:'15px'},style:{enabled:true,mode:'word',word_animation:'pop',font_size:48,font_color:'#0057ff',outline:0,outline_color:'#000000',shadow:1,border_style:3,back_color:'#ffffff@0.92',margin_v:62,alignment:2,box_radius:14}},
      {name:'Sub Bubble',tag:'word rounded',sample:'lebih enak dibaca',card:'linear-gradient(135deg,#ff8ed8,#ffeef8)',texture:'radial-gradient(circle at 24% 20%, rgba(255,255,255,.65), transparent 18%)',demoBg:'#ffd9ef',demo:{box:'#ffffff',color:'#f02d93',pad:'7px 12px',radius:'999px',shadow:'4px 4px 0 #7d0044',stroke:'1px #fff',size:'15px'},style:{enabled:true,mode:'word',word_animation:'pulse',font_size:48,font_color:'#f02d93',outline:1.5,outline_color:'#ffffff',shadow:1,border_style:3,back_color:'#ffffff@0.92',margin_v:64,alignment:2,box_radius:999}},
      {name:'Sub Minimal',tag:'word docu',sample:'simple but premium',card:'linear-gradient(135deg,#efe7d8,#b9a98d)',texture:'linear-gradient(90deg, rgba(0,0,0,.08), transparent 55%)',demoBg:'#efe7d8',demo:{color:'#1e1a14',stroke:'0 #000',shadow:'none',tracking:'-.03em',size:'15px'},style:{enabled:true,mode:'word',word_animation:'fade',font_size:46,font_color:'#1e1a14',outline:0,outline_color:'#000000',shadow:0,border_style:3,back_color:'#efe7d8@0.82',margin_v:72,alignment:2,box_radius:8}}
    ];

    function applyTextTemplate(tmpl){
      const targetLayer = ['hook','badge'].includes(selected) ? selected : 'hook';
      selected = targetLayer;
      const layer = template[targetLayer];
      pushHistory();
      const nextStyle = JSON.parse(JSON.stringify(tmpl.style || {}));
      const fontPath = templateFontPath(tmpl);
      if (fontPath) nextStyle.font_file = fontPath;
      Object.assign(layer, nextStyle);
      layer.enabled = true;
      renderAll(); toast('Applied: '+tmpl.name);
    }

    function applySubtitleTemplate(tmpl){
      selected = 'subtitle';
      pushHistory();
      const nextStyle = JSON.parse(JSON.stringify(tmpl.style || {}));
      const fontPath = templateFontPath(tmpl);
      if (fontPath) {
        nextStyle.font_file = fontPath;
        nextStyle.font_name = fontPath.split('/').pop().replace(/\.[^.]+$/, '');
      }
      Object.assign(template.subtitle, nextStyle);
      template.subtitle.enabled = true;
      renderAll(); toast('Subtitle: '+tmpl.name);
    }

    function applyStyleTemplate(tmpl){
      if (tmpl.kind === 'subtitle') applySubtitleTemplate(tmpl);
      else applyTextTemplate(tmpl);
    }

    function fontBasename(path){ return String(path || '').split('/').pop().toLowerCase(); }
    function pickFontPath(candidates){
      for (const candidate of candidates || []) {
        const wanted = String(candidate).toLowerCase();
        const found = fonts.find(f => fontBasename(f.path) === wanted || String(f.path || '').toLowerCase().includes('/'+wanted));
        if (found) return found.path;
      }
      for (const candidate of candidates || []) {
        const wanted = String(candidate).replace(/\.[^.]+$/, '').toLowerCase();
        const found = fonts.find(f => String(f.label || '').toLowerCase().includes(wanted) || String(f.path || '').toLowerCase().includes(wanted));
        if (found) return found.path;
      }
      return '';
    }
    function templateFontPath(tmpl){ return pickFontPath(templateFontChoices[tmpl.name] || []); }

    function setVars(el, vars){
      for (const [key, value] of Object.entries(vars || {})) el.style.setProperty(key, value);
    }

    function renderTextTemplates(){
      const root = $('stylesBody'); if(!root) return;
      root.innerHTML = '';
      const isSubtitleMode = styleMode === 'subtitle';
      $('stylesTitle').textContent = isSubtitleMode ? 'Subtitle Styles' : 'Text Styles';
      $('textStyleTab').classList.toggle('active', !isSubtitleMode);
      $('subtitleStyleTab').classList.toggle('active', isSubtitleMode);
      const templates = isSubtitleMode ? subtitleTemplates.map(t => ({...t, kind:'subtitle'})) : textTemplates;
      templates.forEach(t => {
        const card = document.createElement('div'); card.className = 'style-card';
        setVars(card, {'--card-bg':t.card || '#22232a','--card-texture':t.texture || 'none','--demo-bg':t.demoBg || 'rgba(0,0,0,.18)'});
        const preview = document.createElement('div'); preview.className = 'preview';
        const span = document.createElement('span'); span.textContent = t.sample || 'TEXT';
        const demo = t.demo || {};
        const previewFont = templateFontPath(t);
        setVars(span, {
          '--demo-font':previewFont ? fontFamily(previewFont) : 'Impact, Haettenschweiler, sans-serif',
          '--demo-color':demo.color || t.style?.font_color || '#fff',
          '--demo-stroke':demo.stroke || ((t.style?.border_w || 0)+'px '+(t.style?.border_color || '#000')),
          '--demo-shadow':demo.shadow || ((t.style?.shadow_x || 0)+'px '+(t.style?.shadow_y || 0)+'px 0 '+(t.style?.shadow_color || '#000')),
          '--demo-box':demo.box || (t.style?.box ? cssColor(t.style?.box_color,'#000') : 'transparent'),
          '--demo-pad':demo.pad || (t.style?.box ? '4px 8px' : '0'),
          '--demo-radius':demo.radius || (t.style?.box ? '7px' : '0'),
          '--demo-rotate':demo.rotate || '0deg',
          '--demo-size':demo.size || '18px',
          '--demo-tracking':demo.tracking || '-.04em',
          '--demo-box-shadow':demo.boxShadow || 'none'
        });
        preview.appendChild(span);
        const name = document.createElement('div'); name.className = 'name'; name.textContent = t.name;
        const tag = document.createElement('div'); tag.className = 'badge-tag'; tag.textContent = t.tag || 'style';
        card.appendChild(preview); card.appendChild(name); card.appendChild(tag);
        card.addEventListener('click', () => applyStyleTemplate(t));
        root.appendChild(card);
      });
    }

    const fields = {
      video: [
        ['overlay_enabled','Show Overlay','checkbox'], ['width','Width','number'],
        ['height','Height (-2 auto)','number'], ['x','X','text'], ['y','Y','text'],
        ['background_blur','Blur','number'], ['background_blur_power','Blur Power','number'],
        ['border_w','Border W','number'], ['border_color','Border Color','color']
      ],
      hook: [
        ['enabled','Show Hook','checkbox'], ['font_file','Font','font'], ['font_size','Size','number'],
        ['x','X','text'], ['y','Y','text'],
        ['font_color','Color','color'], ['line_spacing','Line Spacing','number'],
        ['border_w','Stroke','number'], ['border_color','Stroke Color','color'],
        ['shadow_x','Shadow X','number'], ['shadow_y','Shadow Y','number'], ['shadow_color','Shadow Color','color'],
        ['box','Bubble Box','checkbox'], ['box_color','Bubble Color','color'], ['box_border_w','Bubble Pad','number'],
        ['box_radius','Bubble Radius','number'], ['rotate','Rotate','number'], ['letter_spacing','Letter Spacing','number']
      ],
      badge: [
        ['enabled','Show Badge','checkbox'], ['text','Text','text'], ['font_file','Font','font'],
        ['font_size','Size','number'], ['x','X','text'], ['y','Y','text'],
        ['font_color','Color','color'], ['box','Box BG','checkbox'], ['box_color','Box Color','color'], ['box_border_w','Box Pad','number'],
        ['box_radius','Box Radius','number'], ['rotate','Rotate','number'], ['letter_spacing','Letter Spacing','number'],
        ['border_w','Stroke','number'], ['border_color','Stroke Color','color'],
        ['shadow_x','Shadow X','number'], ['shadow_y','Shadow Y','number'], ['shadow_color','Shadow Color','color']
      ],
      subtitle: [
        ['enabled','Show Sub','checkbox'], ['mode','Mode','subtitle_mode'], ['word_animation','Word Animation','word_animation'],
        ['font_file','Font','font'], ['font_name','Font Name (render)','text'],
        ['font_size','Size','number'], ['font_color','Color','color'],
        ['outline','Outline','number'], ['outline_color','Outline Color','color'],
        ['shadow','Shadow','number'], ['back_color','Box Color','color'],
        ['border_style','Box Style (1/3)','number'], ['box_radius','Preview Radius','number'],
        ['margin_v','Margin V','number'],
        ['alignment','Alignment','alignment']
      ]
    };

    function tokenQuery(p='?'){const t=new URLSearchParams(location.search).get('token');return t?`${p}token=${encodeURIComponent(t)}`:''}
    function basePath(){const p=location.pathname;return p.endsWith('/')?p.slice(0,-1):p==='/'?'':p}
    function apiUrl(p){return basePath()+p+tokenQuery('?')}
    function fontUrl(p){return basePath()+'/api/font?path='+encodeURIComponent(p)+tokenQuery('&')}

    function deepMerge(base,patch){const o=structuredClone(base);for(const[k,v]of Object.entries(patch||{})){o[k]=v&&typeof v==='object'&&!Array.isArray(v)&&o[k]?deepMerge(o[k],v):v}return o}
    function get(path){return path.split('.').reduce((o,k)=>o?.[k],template)}
    function assignPath(path,value){const parts=path.split('.');let o=template;for(const p of parts.slice(0,-1))o=o[p];o[parts.at(-1)]=value}
    function set(path,value){pushHistory();assignPath(path,value);renderAll()}
    function sectionPath(key){return['display_seconds','fade_seconds','text_transform','max_line_chars','max_lines'].includes(key)?key:`${selected}.${key}`}
    // unused timeStr removed

    function pushHistory(){history=history.slice(0,historyIdx+1);history.push(JSON.stringify(template));if(history.length>50)history.shift();historyIdx=history.length-1}
    function undo(){if(historyIdx<=0)return;historyIdx--;template=JSON.parse(history[historyIdx]);renderAll();toast('Undo')}
    function redo(){if(historyIdx>=history.length-1)return;historyIdx++;template=JSON.parse(history[historyIdx]);renderAll();toast('Redo')}

    function stripAlpha(c,fb='#ffffff'){if(!c)return fb;const n={white:'#ffffff',black:'#000000',yellow:'#fff200',red:'#ff0000',green:'#00ff00',blue:'#0000ff'};const r=String(c).trim().toLowerCase();if(n[r])return n[r];const m=r.match(/#([0-9a-f]{6})/i);const fm=r.match(/0x([0-9a-f]{6})/i);return m?'#'+m[1]:fm?'#'+fm[1]:fb}
    function keepAlpha(p,fb='@1'){const m=String(p||'').match(/(@[0-9.]+)/);return m?m[1]:fb}
    function withAlpha(h,p,fb='@1'){return h+keepAlpha(p,fb)}
    function cssColor(v,fb='#ffffff'){return stripAlpha(v,fb)}
    function fontFamily(path){if(!path)return'ui-sans-serif,Segoe UI,sans-serif';if(loadedFonts.has(path))return loadedFonts.get(path);const f='cc-font'+(loadedFonts.size+1);const s=document.createElement('style');s.textContent=`@font-face{font-family:${f};src:url("${fontUrl(path)}");font-display:swap;}`;document.head.appendChild(s);loadedFonts.set(path,f);return f}
    function scale(){return $('phone').clientWidth/target.width}

    function wrapHook(text){let v=text.trim().replace(/\s+/g,' ');if(template.text_transform==='upper')v=v.toUpperCase();if(template.text_transform==='lower')v=v.toLowerCase();if(template.text_transform==='title')v=v.toLowerCase().replace(/\b\w/g,c=>c.toUpperCase());const mx=Number(template.max_line_chars||20);const lines=[];let line='';for(const w of v.split(' ')){const n=line?line+' '+w:w;if(line&&n.length>mx){lines.push(line);line=w}else line=n}if(line)lines.push(line);return lines.slice(0,Number(template.max_lines||3)).join('\n')}
    function coord(value,axis,el){const num=Number(value);if(Number.isFinite(num))return num;const w=el.getBoundingClientRect().width/scale(),h=el.getBoundingClientRect().height/scale();if(value==='(w-text_w)/2')return(target.width-w)/2;if(value==='(h-text_h)/2')return(target.height-h)/2;if(value==='(W-w)/2')return(target.width-w)/2;if(value==='(H-h)/2')return(target.height-h)/2;return axis==='x'?(target.width-w)/2:220}

    function styleVideoOverlay(){const el=$('videoPreview');const d=template.video||{};const s=scale();el.hidden=!d.overlay_enabled;const w=Math.max(120,Number(d.width||1080));const h=Number(d.height||-2)>0?Number(d.height):w*9/16;el.style.width=w*s+'px';el.style.height=h*s+'px';el.style.left=coord(d.x||'(W-w)/2','x',el)*s+'px';el.style.top=coord(d.y||'(H-h)/2','y',el)*s+'px';el.style.border=Number(d.border_w||0)*s+'px solid '+cssColor(d.border_color,'#000')}
    function styleDrawtext(el,layer){const d=template[layer];const s=scale();el.style.fontFamily=fontFamily(d.font_file||'');el.style.fontSize=Number(d.font_size||70)*s+'px';el.style.color=cssColor(d.font_color,'#fff');el.style.left=coord(d.x,'x',el)*s+'px';el.style.top=coord(d.y,'y',el)*s+'px';el.style.padding=d.box?Number(d.box_border_w||0)*s+'px':'0';el.style.background=d.box?cssColor(d.box_color,'#000'):'transparent';el.style.borderRadius=d.box?Number(d.box_radius||0)*s+'px':'0';el.style.boxShadow=d.box_shadow||'none';el.style.clipPath=d.box_clip||'none';el.style.transform=d.rotate?`rotate(${Number(d.rotate)||0}deg)`:'none';el.style.letterSpacing=d.letter_spacing?Number(d.letter_spacing)*s+'px':'';el.style.webkitTextStroke=Number(d.border_w||0)*s+'px '+cssColor(d.border_color,'#000');el.style.textShadow=Number(d.shadow_x||0)*s+'px '+Number(d.shadow_y||0)*s+'px 0 '+cssColor(d.shadow_color,'#000');if(layer==='badge')el.hidden=!d.enabled;if(layer==='hook')el.hidden=!d.enabled}
    function styleSubtitle(){const el=$('subtitlePreview');const d=template.subtitle;const s=scale();el.hidden=!d.enabled;el.style.fontFamily=fontFamily(d.font_file||'');el.style.fontSize=Number(d.font_size||46)*s+'px';el.style.color=cssColor(d.font_color,'#fff200');el.style.webkitTextStroke=Number(d.outline||2)*s+'px '+cssColor(d.outline_color,'#000');el.style.textShadow='0 '+Number(d.shadow||0)*s+'px 0 #000';const boxed=Number(d.border_style||1)===3;el.style.background=boxed?cssColor(d.back_color,'#000000'):'transparent';el.style.padding=boxed?`${Math.max(8, Number(d.font_size||46)*.22)*s}px ${Math.max(18, Number(d.font_size||46)*.42)*s}px`:'0';el.style.borderRadius=boxed?Number(d.box_radius||8)*s+'px':'0';el.style.left='8%';el.style.right='8%';el.style.top='auto';el.style.bottom='auto';const a=Number(d.alignment||2);if([7,8,9].includes(a))el.style.top=Number(d.margin_v||60)*s+'px';else if([4,5,6].includes(a))el.style.top='50%';else el.style.bottom=Number(d.margin_v||60)*s+'px'}

    function renderPreview(){$('badgePreview').textContent=template.badge.text||'BADGE';$('hookPreview').textContent=wrapHook($('sampleText').value);$('subtitlePreview').innerHTML=template.subtitle?.mode==='word'?'WORD<br><span style="font-size:.58em;opacity:.72">by word</span>':'Subtitle kuning muncul di sini<br>border bisa diatur.';styleVideoOverlay();styleDrawtext($('badgePreview'),'badge');styleDrawtext($('hookPreview'),'hook');styleSubtitle();document.querySelectorAll('.overlay,.video-preview').forEach(el=>el.classList.toggle('selected',el.dataset.layer===selected));$('jsonOutput').value=JSON.stringify(template,null,2)}

    function renderLayers(){const root=$('layerList');root.innerHTML='';layerKeys.forEach(key=>{const d=template[key];const div=document.createElement('div');div.className='layer-item'+(selected===key?' active':'');div.dataset.layer=key;div.draggable=true;div.innerHTML='<div class="thumb">'+layerIcons[key]+'</div><div class="info"><strong>'+layerLabels[key]+'</strong><span>'+(d.enabled!==false?'visible':'hidden')+'</span></div><button class="lbtn vis-btn">'+(d.enabled!==false?'&#x1F441;':'&#x1F648;')+'</button><button class="lbtn">&#x2630;</button>';div.addEventListener('click',e=>{if(e.target.closest('.lbtn'))return;selected=key;if(key==='subtitle')styleMode='subtitle';else if(['hook','badge'].includes(key))styleMode='text';renderLayers();renderProperties();renderPreview();renderTextTemplates()});div.querySelector('.vis-btn').addEventListener('click',e=>{e.stopPropagation();set(key+'.enabled',!(template[key].enabled!==false))});root.appendChild(div)})}

    function fieldControl(key,label,type){const path=sectionPath(key);const value=get(path);const wrap=document.createElement('div');wrap.className='field';const lab=document.createElement('label');lab.textContent=label;wrap.appendChild(lab);let input;if(type==='font'){input=document.createElement('select');input.innerHTML='<option value="">Default</option>'+fonts.map(f=>'<option value="'+f.path+'">'+f.label+'</option>').join('')}else if(type==='alignment'){input=document.createElement('select');input.innerHTML='<option value="2">Bottom Center</option><option value="5">Middle Center</option><option value="8">Top Center</option>'}else if(type==='subtitle_mode'){input=document.createElement('select');input.innerHTML='<option value="line">Line / Sentence</option><option value="word">Word by Word</option>'}else if(type==='word_animation'){input=document.createElement('select');input.innerHTML='<option value="pop">Pop</option><option value="pulse">Pulse</option><option value="fade">Fade</option><option value="none">None</option>'}else{input=document.createElement('input');input.type=type;if(type==='number')input.step='1'}if(type==='checkbox')input.checked=Boolean(value);else if(type==='color')input.value=stripAlpha(value,key.includes('font')?'#fff':'#000');else input.value=value??'';input.addEventListener('input',()=>{let next=type==='checkbox'?input.checked:input.value;if(type==='number'||type==='alignment')next=Number(next);if(type==='color')next=withAlpha(input.value,get(path),key.includes('box')?'@0.95':'@1');pushHistory();assignPath(path,next);if(type==='font'&&selected==='subtitle'&&next)assignPath('subtitle.font_name',next.split('/').pop().replace(/\.[^.]+$/,''));renderPreview();renderLayers();renderTextTemplates()});wrap.appendChild(input);return wrap}
    function renderProperties(syncCommon=true){$('propertiesTitle').textContent=layerLabels[selected]+' Properties';const root=$('properties');root.innerHTML='';const list=fields[selected];for(let i=0;i<list.length;i+=2){const row=document.createElement('div');row.className='row';row.appendChild(fieldControl(...list[i]));if(list[i+1])row.appendChild(fieldControl(...list[i+1]));root.appendChild(row)}if(syncCommon)syncCommonInputs()}
    function syncCommonInputs(){document.querySelectorAll('[data-path]').forEach(input=>{const v=get(input.dataset.path);if(input.type==='checkbox')input.checked=Boolean(v);else input.value=v??''})}

    function renderAll(){renderPreview();renderLayers();renderProperties();renderTextTemplates()}

    // Media import
    $('mediaFileInput').addEventListener('change',e=>{const file=e.target.files[0];if(!file)return;const url=URL.createObjectURL(file);const video=$('bgVideo');video.src=url;video.play().catch(()=>{});mediaFile=file;$('mediaList').innerHTML='<div style="color:var(--ink-2);font-size:11px">'+file.name+'</div>';toast('Media imported')})

    // Undo/redo
    $('undoBtn').addEventListener('click',undo);$('redoBtn').addEventListener('click',redo)
    document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='z'&&!e.shiftKey){e.preventDefault();undo()}if((e.ctrlKey||e.metaKey)&&e.key==='z'&&e.shiftKey){e.preventDefault();redo()}})

    // Tab switching
    document.querySelectorAll('.sidebar-tab').forEach(tab=>{tab.addEventListener('click',()=>{document.querySelectorAll('.sidebar-tab').forEach(t=>t.classList.toggle('active',t===tab));$('layersTab').hidden=tab.dataset.tab!=='layers';$('mediaTab').hidden=tab.dataset.tab!=='media'})})

    // Layer button events (delegated)
    document.addEventListener('click',e=>{const btn=e.target.closest('.layer-item .lbtn');if(!btn||btn.closest('.vis-btn'))return;const item=btn.closest('.layer-item');if(!item)return;const key=item.dataset.layer;const idx=layerKeys.indexOf(key);if(idx>0){layerKeys[idx]=layerKeys[idx-1];layerKeys[idx-1]=key;selected=key;renderAll();pushHistory()}})

    // Drag layer reorder
    document.addEventListener('dragstart',e=>{const item=e.target.closest('.layer-item');if(!item)return;e.dataTransfer.setData('text/plain',item.dataset.layer)})
    document.addEventListener('dragover',e=>{const item=e.target.closest('.layer-item');if(!item)return;e.preventDefault();const rect=item.getBoundingClientRect();const mid=rect.top+rect.height/2;if(e.clientY<mid)item.style.borderTop='2px solid var(--accent)';else item.style.borderBottom='2px solid var(--accent)'})
    document.addEventListener('dragleave',e=>{const item=e.target.closest('.layer-item');if(item){item.style.borderTop='';item.style.borderBottom=''}})
    document.addEventListener('drop',e=>{const item=e.target.closest('.layer-item');if(!item)return;e.preventDefault();item.style.borderTop='';item.style.borderBottom='';const from=e.dataTransfer.getData('text/plain');const to=item.dataset.layer;if(from===to)return;const fi=layerKeys.indexOf(from);const ti=layerKeys.indexOf(to);layerKeys.splice(fi,1);layerKeys.splice(ti,0,from);selected=from;pushHistory();renderAll()})

    // Pointer drag on phone preview
    for(const el of[$('videoPreview'),$('badgePreview'),$('hookPreview'),$('subtitlePreview')]){el.addEventListener('pointerdown',event=>{selected=el.dataset.layer;renderLayers();renderProperties();renderPreview();const r=el.getBoundingClientRect(),p=$('phone').getBoundingClientRect();drag={layer:selected,dx:event.clientX-r.left,dy:event.clientY-r.top,p,w:r.width,h:r.height};el.setPointerCapture(event.pointerId)})}
    window.addEventListener('pointermove',event=>{if(!drag)return;const left=Math.max(0,Math.min(event.clientX-drag.p.left-drag.dx,drag.p.width-drag.w));const top=Math.max(0,Math.min(event.clientY-drag.p.top-drag.dy,drag.p.height-drag.h));if(drag.layer==='subtitle'){template.subtitle.margin_v=Math.round((drag.p.height-top-drag.h)/scale());template.subtitle.alignment=2}else if(drag.layer==='video'){template.video.x=String(Math.round(left/scale()));template.video.y=String(Math.round(top/scale()))}else{template[drag.layer].x=String(Math.round(left/scale()));template[drag.layer].y=String(Math.round(top/scale()))}renderPreview();$('jsonOutput').value=JSON.stringify(template,null,2)})
    window.addEventListener('pointerup',()=>{if(drag){pushHistory();drag=null;renderAll()}})

    // Common inputs
    document.querySelectorAll('[data-path]').forEach(input=>input.addEventListener('input',()=>{pushHistory();set(input.dataset.path,input.type==='number'?Number(input.value):input.value);renderAll()}))
    $('sampleText').addEventListener('input',renderPreview)

    // Buttons
    $('textStyleTab').addEventListener('click',()=>{styleMode='text';renderTextTemplates()})
    $('subtitleStyleTab').addEventListener('click',()=>{styleMode='subtitle';renderTextTemplates()})
    $('saveBtn').addEventListener('click',()=>saveTemplate().catch(e=>toast('Save failed: '+e.message)))
    $('reloadBtn').addEventListener('click',()=>loadTemplate().catch(e=>toast('Load failed: '+e.message)))
    $('resetBtn').addEventListener('click',()=>{template=structuredClone(defaultTemplate);pushHistory();renderAll();toast('Reset')})
    $('copyBtn').addEventListener('click',async()=>{await navigator.clipboard.writeText(JSON.stringify(template,null,2));toast('Copied')})
    $('applyJsonBtn').addEventListener('click',()=>{try{pushHistory();template=deepMerge(defaultTemplate,JSON.parse($('jsonOutput').value));renderAll();toast('Applied')}catch(e){toast('Invalid JSON: '+e.message)}})
    $('mediaBtn').addEventListener('click',()=>$('mediaFileInput').click())
    window.addEventListener('resize',renderPreview)

    // Load
    async function loadFonts(){const res=await fetch(apiUrl('/api/fonts'));if(!res.ok)throw new Error(await res.text());fonts=(await res.json()).fonts||[]}
    async function loadTemplate(){const res=await fetch(apiUrl('/api/template'));if(!res.ok)throw new Error(await res.text());const p=await res.json();template=deepMerge(defaultTemplate,p.template||{});$('templatePath').textContent=p.path;pushHistory();renderAll();toast('Loaded')}
    async function saveTemplate(){const res=await fetch(apiUrl('/api/template'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(template)});if(!res.ok)throw new Error(await res.text());toast('Saved')}
    function toast(text){const el=$('toast');el.textContent=text;el.classList.add('show');clearTimeout(el.t);el.t=setTimeout(()=>el.classList.remove('show'),1600)}

    loadFonts().then(loadTemplate).catch(e=>toast('Load failed: '+e.message))
  </script>
</body>
</html>
"""

HTML = CLEAN_HTML


def active_template_path() -> Path:
    return config.HOOK_TEMPLATE_PATH


def read_active_template() -> dict[str, Any]:
    path = active_template_path()
    if not path.exists():
        return merge_template(DEFAULT_HOOK_TEMPLATE, {})
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Template file must contain a JSON object")
    return merge_template(DEFAULT_HOOK_TEMPLATE, raw)


def write_active_template(template: dict[str, Any]) -> Path:
    if not isinstance(template, dict):
        raise ValueError("Template payload must be a JSON object")
    path = active_template_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return path


def discover_fonts() -> list[dict[str, str]]:
    seen: set[Path] = set()
    fonts: list[dict[str, str]] = []
    for directory in FONT_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            label = f"{path.stem} - {path.parent.name}"
            fonts.append({"label": label, "path": str(resolved)})

    default_path = Path(config.SUBTITLE_FONT_PATH).resolve()
    if default_path.exists() and default_path not in seen:
        fonts.insert(0, {"label": f"{default_path.stem} - default", "path": str(default_path)})
    return fonts


def is_allowed_font_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved.suffix.lower() not in FONT_SUFFIXES or not resolved.is_file():
        return False
    for directory in FONT_DIRS:
        try:
            resolved.relative_to(directory.resolve())
            return True
        except (ValueError, OSError):
            continue
    try:
        return resolved == Path(config.SUBTITLE_FONT_PATH).resolve()
    except OSError:
        return False


def font_content_type(path: Path) -> str:
    if path.suffix.lower() == ".otf":
        return "font/otf"
    if path.suffix.lower() == ".ttc":
        return "font/collection"
    return "font/ttf"


class HookTemplateHandler(BaseHTTPRequestHandler):
    server_version = "ContentClipperHookStudio/1.0"

    def _token_ok(self) -> bool:
        required = getattr(self.server, "editor_token", "")
        if not required:
            return True
        parsed = urlparse(self.path)
        query_token = parse_qs(parsed.query).get("token", [""])[0]
        header_token = self.headers.get("X-Editor-Token", "")
        return secrets.compare_digest(required, query_token) or secrets.compare_digest(required, header_token)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("Invalid request body length")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = HTML.replace("__DEFAULT_TEMPLATE__", json.dumps(DEFAULT_HOOK_TEMPLATE, ensure_ascii=False))
            self._send_text(html, content_type="text/html")
            return
        if parsed.path == "/api/template":
            if not self._token_ok():
                self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            try:
                self._send_json({"path": str(active_template_path()), "template": read_active_template()})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/fonts":
            if not self._token_ok():
                self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            self._send_json({"fonts": discover_fonts()})
            return
        if parsed.path == "/api/font":
            if not self._token_ok():
                self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            query = parse_qs(parsed.query)
            requested = Path(query.get("path", [""])[0])
            if not is_allowed_font_path(requested):
                self._send_json({"error": "font not allowed"}, HTTPStatus.FORBIDDEN)
                return
            self._send_bytes(requested.read_bytes(), font_content_type(requested))
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/template":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self._token_ok():
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            payload = self._read_json_body()
            merged = merge_template(DEFAULT_HOOK_TEMPLATE, payload)
            path = write_active_template(merged)
            self._send_json({"ok": True, "path": str(path), "template": merged})
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ContentClipper visual hook template editor")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Use 0.0.0.0 only with --token.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--token",
        default=config.HOOK_EDITOR_TOKEN,
        help="Required token for browser/API access. Defaults to HOOK_EDITOR_TOKEN from .env.",
    )
    parser.add_argument("--check", action="store_true", help="Validate active template and exit.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.check:
        template = read_active_template()
        print(json.dumps({"path": str(active_template_path()), "template": template}, ensure_ascii=False, indent=2))
        return

    is_public_bind = args.host not in {"127.0.0.1", "localhost", "::1"}
    if is_public_bind and not args.token:
        raise SystemExit("Refusing public bind without --token")

    server = ThreadingHTTPServer((args.host, args.port), HookTemplateHandler)
    server.editor_token = args.token
    url_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    token_suffix = f"?token={args.token}" if args.token else ""
    print(f"Hook Studio running: http://{url_host}:{args.port}/{token_suffix}")
    print(f"Editing template: {active_template_path()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
