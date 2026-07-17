"""Local-only web interface for the Ollama coding agent."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agent import AgentError, LocalAgent, ROOT, load_json


WEB_ROOT = ROOT / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class Session:
    def __init__(self, workspace: Path, config: dict[str, Any], allow_risky: bool):
        self.agent = LocalAgent(workspace, config, allow_risky)
        self.lock = threading.Lock()


class AppState:
    def __init__(self, workspace: Path, config: dict[str, Any], allow_risky: bool):
        self.workspace = workspace
        self.config = config
        self.allow_risky = allow_risky
        self.sessions: dict[str, Session] = {}
        self.sessions_lock = threading.Lock()

    def get_session(self, session_id: str) -> Session:
        with self.sessions_lock:
            session = self.sessions.get(session_id)
            if session is None:
                if len(self.sessions) >= 32:
                    self.sessions.pop(next(iter(self.sessions)))
                session = Session(self.workspace, self.config, self.allow_risky)
                self.sessions[session_id] = session
            return session

    def reset_session(self, session_id: str) -> None:
        with self.sessions_lock:
            self.sessions.pop(session_id, None)


class AgentWebHandler(BaseHTTPRequestHandler):
    server_version = "LocalBuildAgent/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> AppState:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} {format_string % args}")

    def common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; frame-ancestors 'none'",
        )

    def send_bytes(self, data: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.common_headers()
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AgentError("Invalid Content-Length") from exc
        if length < 1 or length > 65536:
            raise AgentError("Request body must be between 1 byte and 64 KB")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError("Invalid JSON request") from exc

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/status":
            self.handle_status()
            return
        static = STATIC_FILES.get(path)
        if static is None:
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        filename, content_type = static
        target = WEB_ROOT / filename
        if not target.is_file():
            self.send_json({"error": "Web assets are missing"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_bytes(target.read_bytes(), content_type)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/chat":
                self.handle_chat(payload)
            elif path == "/api/reset":
                session_id = validate_session_id(payload.get("session_id"))
                self.app.reset_session(session_id)
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except AgentError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_status(self) -> None:
        model = self.app.config["model"]
        installed = False
        loaded = False
        try:
            base_url = self.app.config["base_url"].rstrip("/")
            with urllib.request.urlopen(base_url + "/api/tags", timeout=3) as response:
                tags = json.loads(response.read().decode("utf-8"))
            installed = any(item.get("name") == model for item in tags.get("models", []))
            with urllib.request.urlopen(base_url + "/api/ps", timeout=3) as response:
                running = json.loads(response.read().decode("utf-8"))
            loaded = any(item.get("name") == model for item in running.get("models", []))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        disk = shutil.disk_usage(self.app.workspace.anchor)
        self.send_json(
            {
                "ok": True,
                "model": model,
                "model_installed": installed,
                "model_loaded": loaded,
                "workspace": str(self.app.workspace),
                "context_length": self.app.config.get("context_length"),
                "disk_free_gb": round(disk.free / (1024**3), 1),
                "safe_mode": not self.app.allow_risky,
            }
        )

    def handle_chat(self, payload: dict[str, Any]) -> None:
        session_id = validate_session_id(payload.get("session_id"))
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise AgentError("message is required")
        if len(message) > 32000:
            raise AgentError("message is too long")

        session = self.app.get_session(session_id)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.common_headers()
        self.end_headers()
        self.close_connection = True
        disconnected = False

        def emit(event: dict[str, Any]) -> None:
            nonlocal disconnected
            if disconnected:
                return
            try:
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                disconnected = True

        if not session.lock.acquire(blocking=False):
            emit({"type": "error", "message": "这个任务仍在运行，请等待当前回复完成。"})
            emit({"type": "done"})
            return
        try:
            mode = payload.get("mode", "auto")
            if mode not in {"plan", "edits", "auto"}:
                raise AgentError("mode must be plan, edits, or auto")
            final_text = session.agent.turn(message.strip(), mode=mode, on_event=emit)
            emit({"type": "done", "content": final_text})
        except Exception as exc:  # Keep an agent failure contained to its stream.
            emit({"type": "error", "message": str(exc)})
            emit({"type": "done"})
        finally:
            session.lock.release()


def validate_session_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise AgentError("A valid session_id is required")
    if not all(character.isalnum() or character in "-_" for character in value):
        raise AgentError("session_id contains invalid characters")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Build Agent web interface")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-risky", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"Workspace does not exist: {workspace}")
        return 2
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("For safety, the web interface may only bind to a local loopback address.")
        return 2
    app = AppState(workspace, load_json(ROOT / "config.json"), args.allow_risky)
    server = ThreadingHTTPServer((args.host, args.port), AgentWebHandler)
    server.app = app  # type: ignore[attr-defined]
    print(f"Local Build Agent UI: http://{args.host}:{args.port}")
    print(f"Workspace: {workspace}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web interface...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

