"""Local-only web interface for the Ollama coding agent."""

from __future__ import annotations

import argparse
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agent import AgentError, LOCAL_OPENER, LocalAgent, ROOT, load_json


WEB_ROOT = ROOT / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
SETTINGS_PATH = ROOT / ".local-agent" / "settings.json"


class Session:
    def __init__(self, workspace: Path, config: dict[str, Any], allow_risky: bool):
        self.agent = LocalAgent(workspace, config, allow_risky)
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()


class AppState:
    def __init__(
        self,
        workspace: Path,
        config: dict[str, Any],
        allow_risky: bool,
        allowed_origins: set[str] | None = None,
    ):
        self.workspace = workspace
        self.config = dict(config)
        self.allow_risky = allow_risky
        self.allowed_origins = allowed_origins or set()
        self.pairing_token = secrets.token_urlsafe(24)
        self.sessions: dict[str, Session] = {}
        self.sessions_lock = threading.Lock()
        try:
            saved = load_json(SETTINGS_PATH)
            if isinstance(saved.get("model"), str):
                self.config["model"] = saved["model"]
        except (OSError, json.JSONDecodeError):
            pass
        self.model_warm_error = ""
        self.model_warming = True
        threading.Thread(target=self._warm_in_background, daemon=True).start()

    def _warm_in_background(self) -> None:
        try:
            self.warm_model()
        except AgentError as exc:
            self.model_warm_error = str(exc)
        finally:
            self.model_warming = False

    def warm_model(self) -> None:
        """Load the selected model with the same context used by real turns."""
        payload = {
            "model": self.config["model"],
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": False,
            "think": False,
            "keep_alive": self.config.get("keep_alive", "30m"),
            "options": {
                "num_ctx": int(self.config.get("context_length", 8192)),
                "num_predict": 1,
            },
        }
        request = urllib.request.Request(
            self.config["base_url"].rstrip("/") + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with LOCAL_OPENER.open(
                request, timeout=int(self.config.get("model_timeout_seconds", 180))
            ) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise AgentError(f"Unable to warm model (HTTP {exc.code}): {detail}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise AgentError(f"Unable to warm model: {exc}") from exc

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
            session = self.sessions.pop(session_id, None)
            if session is not None:
                session.cancel_event.set()

    def cancel_session(self, session_id: str) -> bool:
        with self.sessions_lock:
            session = self.sessions.get(session_id)
            if session is None:
                return False
            session.cancel_event.set()
            return True

    def installed_models(self) -> list[str]:
        base_url = self.config["base_url"].rstrip("/")
        with LOCAL_OPENER.open(base_url + "/api/tags", timeout=5) as response:
            tags = json.loads(response.read().decode("utf-8"))
        names = {
            item.get("name") or item.get("model")
            for item in tags.get("models", [])
            if item.get("name") or item.get("model")
        }
        return sorted(names, key=str.casefold)

    def switch_model(self, model: str) -> None:
        if model not in self.installed_models():
            raise AgentError("The selected model is not installed in local Ollama")
        with self.sessions_lock:
            for session in self.sessions.values():
                session.cancel_event.set()
            self.config["model"] = model
            self.sessions.clear()
        self.model_warming = True
        try:
            self.warm_model()
            self.model_warm_error = ""
        except AgentError as exc:
            self.model_warm_error = str(exc)
            raise
        finally:
            self.model_warming = False
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps({"model": model}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(SETTINGS_PATH)


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
        origin = self.headers.get("Origin", "")
        if origin and origin in self.app.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

    def cross_origin_authorized(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        if origin == f"http://{self.headers.get('Host', '')}":
            return True
        if origin not in self.app.allowed_origins:
            return False
        supplied = self.headers.get("X-Local-Agent-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.app.pairing_token)

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin", "")
        if not origin or origin not in self.app.allowed_origins:
            self.send_json({"error": "Origin is not allowed"}, HTTPStatus.FORBIDDEN)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Local-Agent-Token")
        self.common_headers()
        self.end_headers()

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
        if path.startswith("/api/") and not self.cross_origin_authorized():
            self.send_json({"error": "Pairing required"}, HTTPStatus.UNAUTHORIZED)
            return
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
        if not self.cross_origin_authorized():
            self.send_json({"error": "Pairing required"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            payload = self.read_json()
            if path == "/api/chat":
                self.handle_chat(payload)
            elif path == "/api/model":
                model = payload.get("model")
                if not isinstance(model, str) or not model.strip():
                    raise AgentError("model is required")
                self.app.switch_model(model.strip())
                self.send_json({"ok": True, "model": self.app.config["model"]})
            elif path == "/api/reset":
                session_id = validate_session_id(payload.get("session_id"))
                self.app.reset_session(session_id)
                self.send_json({"ok": True})
            elif path == "/api/cancel":
                session_id = validate_session_id(payload.get("session_id"))
                self.send_json({"ok": True, "cancelled": self.app.cancel_session(session_id)})
            else:
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except AgentError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_status(self) -> None:
        model = self.app.config["model"]
        installed = False
        loaded = False
        loaded_context = 0
        installed_models: list[str] = []
        try:
            base_url = self.app.config["base_url"].rstrip("/")
            installed_models = self.app.installed_models()
            installed = model in installed_models
            with LOCAL_OPENER.open(base_url + "/api/ps", timeout=3) as response:
                running = json.loads(response.read().decode("utf-8"))
            for item in running.get("models", []):
                if item.get("name") == model or item.get("model") == model:
                    loaded_context = int(item.get("context_length") or 0)
                    loaded = loaded_context >= int(self.app.config.get("context_length", 8192))
                    break
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        disk = shutil.disk_usage(self.app.workspace.anchor)
        self.send_json(
            {
                "ok": True,
                "model": model,
                "model_installed": installed,
                "model_loaded": loaded,
                "model_context_length": loaded_context,
                "model_warm_error": self.app.model_warm_error,
                "model_warming": self.app.model_warming,
                "installed_models": installed_models,
                "project": self.app.workspace.name,
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
            session.cancel_event.clear()
            mode = payload.get("mode", "auto")
            if mode not in {"plan", "edits", "auto"}:
                raise AgentError("mode must be plan, edits, or auto")
            final_text = session.agent.turn(
                message.strip(),
                mode=mode,
                on_event=emit,
                cancel_event=session.cancel_event,
            )
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


def validate_https_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError(
            "allowed origin must be an exact HTTPS origin such as https://agent.example"
        )
    return f"https://{parsed.netloc}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Build Agent web interface")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-risky", action="store_true")
    parser.add_argument(
        "--allowed-origin",
        action="append",
        type=validate_https_origin,
        default=[],
        help="Exact HTTPS frontend origin allowed to pair with this local companion",
    )
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
    allowed_origins = set(args.allowed_origin)
    app = AppState(workspace, load_json(ROOT / "config.json"), args.allow_risky, allowed_origins)
    server = ThreadingHTTPServer((args.host, args.port), AgentWebHandler)
    server.app = app  # type: ignore[attr-defined]
    print(f"Local Build Agent UI: http://{args.host}:{args.port}")
    print(f"Workspace: {workspace}")
    if allowed_origins:
        print(f"Allowed frontend: {', '.join(sorted(allowed_origins))}")
        print(f"Pairing token: {app.pairing_token}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web interface...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
