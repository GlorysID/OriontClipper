"""Lightweight web server for template editor UI + REST API."""

from __future__ import annotations

import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import config
from modules import template as tpl

LOGGER = logging.getLogger(__name__)

WEB_DIR = config.PROJECT_ROOT / "web"
EDITOR_HTML = WEB_DIR / "editor.html"

PORT = 8765
_HOST = "0.0.0.0"


class _Handler(BaseHTTPRequestHandler):
    """Minimal HTTP handler — serves editor UI and template JSON API."""

    # Class-level ref so the thread can update it
    _server: "EditorServer | None" = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/" or path == "/editor":
            self._serve_editor()
        elif path == "/api/template":
            self._serve_template()
        elif path == "/api/export":
            self._serve_export()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/template":
            self._save_template()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── Routes ──

    def _serve_editor(self) -> None:
        if not EDITOR_HTML.exists():
            self._send_text(404, "Editor file not found")
            return
        html = EDITOR_HTML.read_text(encoding="utf-8")
        self._send_text(200, html, content_type="text/html; charset=utf-8")

    def _serve_template(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        user_id = self._resolve_user_id(qs)
        if user_id is None:
            self._send_json(400, {"error": "user_id required (query param or default)"})
            return
        tmpl = tpl.get_user_template(user_id)
        pref = tpl.get_pref(user_id)
        self._send_json(200, {
            "user_id": user_id,
            "preset": pref.get("preset", "capcut"),
            "template": tmpl,
            "defaults": _get_defaults(),
        })

    def _serve_export(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        user_id = self._resolve_user_id(qs)
        if user_id is None:
            self._send_json(400, {"error": "user_id required"})
            return
        json_str = tpl.export_json(user_id)
        self._respond(200, json_str.encode("utf-8"), "application/json; charset=utf-8",
                      headers={"Content-Disposition": f'attachment; filename="template_{user_id}.json"'})

    def _save_template(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        user_id = self._resolve_user_id(qs)
        if user_id is None:
            self._send_json(400, {"error": "user_id required"})
            return

        length = int(self.headers.get("Content-Length", 0))
        if not length:
            self._send_json(400, {"error": "Empty body"})
            return

        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        # Save each section's overrides
        for section in ["hook", "subtitle", "video"]:
            if section in data and isinstance(data[section], dict):
                for key, value in data[section].items():
                    tpl.override(user_id, section, key, value)

        self._send_json(200, {"status": "ok", "user_id": user_id})

    # ── Helpers ──

    def _resolve_user_id(self, qs: dict) -> int | None:
        if "user_id" in qs:
            try:
                return int(qs["user_id"][0])
            except (ValueError, IndexError):
                pass
        if self._server and self._server._last_user_id is not None:
            return self._server._last_user_id
        # First allowed user as fallback
        allowed = config.get_allowed_users()
        if allowed:
            return allowed[0]
        return None

    def _send_json(self, status: int, data: dict) -> None:
        self._respond(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        self._respond(status, text.encode("utf-8"), content_type)

    def _respond(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        pass  # unused, CORS headers set in _respond

    def log_message(self, fmt, *args):
        LOGGER.debug("WEB: " + fmt, *args)


def _get_defaults() -> dict:
    """Return the full default template structure for the editor to reference."""
    from modules.video_editor import DEFAULT_HOOK_TEMPLATE
    return DEFAULT_HOOK_TEMPLATE


class EditorServer:
    """Manages the lightweight HTTP server in a background thread."""

    def __init__(self, host: str = _HOST, port: int = PORT):
        self.host = host
        self.port = port
        self._server: HTTPServer | None = None
        self._last_user_id: int | None = None

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/editor"

    def set_last_user(self, user_id: int) -> None:
        self._last_user_id = user_id

    def run(self) -> None:
        _Handler._server = self
        self._server = HTTPServer((self.host, self.port), _Handler)
        self._server.serve_forever()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    def start_thread(self) -> None:
        import threading
        t = threading.Thread(target=self.run, daemon=True, name="web-editor")
        t.start()
        LOGGER.info("Web editor started at %s", self.url)
