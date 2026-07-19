"""A compact, local-first coding agent backed by Ollama."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
IGNORED_DIRS = {".data", ".runtime", ".local-agent", ".git", "__pycache__", "node_modules"}
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AgentError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_workspace_path(workspace: Path, relative_path: str) -> Path:
    if not relative_path or relative_path.strip() in {".", "./"}:
        return workspace
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise AgentError("Only workspace-relative paths are allowed")
    resolved = (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise AgentError("Path escapes the workspace") from exc
    return resolved


def inspect_workspace(workspace: Path, max_depth: int = 3) -> str:
    max_depth = max(0, min(int(max_depth), 6))
    rows: list[str] = [f"workspace: {workspace}"]
    for current, dirs, files in os.walk(workspace):
        current_path = Path(current)
        depth = len(current_path.relative_to(workspace).parts)
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and depth < max_depth)
        if depth > max_depth:
            continue
        indent = "  " * depth
        if depth:
            rows.append(f"{indent}{current_path.name}/")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = current_path / name
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            rows.append(f"{indent}  {name} ({size} bytes)")
    return "\n".join(rows[:2000])


def read_file(workspace: Path, path: str, start_line: int = 1, end_line: int = 400) -> str:
    target = resolve_workspace_path(workspace, path)
    if not target.is_file():
        raise AgentError(f"File not found: {path}")
    start = max(1, int(start_line))
    end = max(start, min(int(end_line), start + 999))
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[start - 1 : end]
    return "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start=start))


def write_file(workspace: Path, path: str, content: str, overwrite: bool = False) -> str:
    target = resolve_workspace_path(workspace, path)
    if target == workspace:
        raise AgentError("A file path is required")
    if target.exists() and not overwrite:
        raise AgentError("File already exists; set overwrite=true only after reading it")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return f"Wrote {len(content.encode('utf-8'))} bytes to {path}"


def replace_in_file(
    workspace: Path, path: str, old_text: str, new_text: str, replace_all: bool = False
) -> str:
    target = resolve_workspace_path(workspace, path)
    if not target.is_file():
        raise AgentError(f"File not found: {path}")
    original = target.read_text(encoding="utf-8")
    count = original.count(old_text)
    if count == 0:
        raise AgentError("Exact old_text was not found")
    if count > 1 and not replace_all:
        raise AgentError(f"old_text occurs {count} times; make it unique or set replace_all=true")
    updated = original.replace(old_text, new_text, -1 if replace_all else 1)
    write_file(workspace, path, updated, overwrite=True)
    return f"Replaced {count if replace_all else 1} occurrence(s) in {path}"


RISKY_COMMANDS = [
    r"\bremove-item\b",
    r"\bdel(?:ete)?\b",
    r"\brmdir\b",
    r"\brm\b",
    r"\bformat\b",
    r"\bdiskpart\b",
    r"\bshutdown\b",
    r"\brestart-computer\b",
    r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|push\s+.*--force)\b",
    r"\binvoke-webrequest\b",
    r"\binvoke-restmethod\b",
    r"\bcurl(?:\.exe)?\b",
    r"\bwget(?:\.exe)?\b",
]


def stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def run_command(
    workspace: Path,
    command: str,
    timeout: int,
    allow_risky: bool,
    on_progress: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    if not command.strip():
        raise AgentError("Command cannot be empty")
    if not allow_risky and any(re.search(pattern, command, re.IGNORECASE) for pattern in RISKY_COMMANDS):
        raise AgentError("Command blocked by safe mode. Review it and rerun the agent with --allow-risky if intended.")
    if os.name == "nt":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["/bin/sh", "-lc", command]
    process = subprocess.Popen(
        argv,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    hard_timeout = max(1, min(int(timeout), 900))
    started = time.monotonic()
    last_report = -1
    while True:
        try:
            stdout, stderr = process.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            elapsed = int(time.monotonic() - started)
            if cancel_event is not None and cancel_event.is_set():
                stop_process_tree(process)
                raise AgentError(f"Command cancelled by user after {elapsed}s")
            if elapsed >= hard_timeout:
                stop_process_tree(process)
                raise AgentError(
                    f"Command timed out after {hard_timeout}s. Try a narrower command or a different strategy."
                )
            if on_progress is not None and elapsed // 5 != last_report:
                last_report = elapsed // 5
                on_progress(elapsed)
    combined = (stdout + stderr).strip()
    return f"exit_code={process.returncode}\n{combined}".rstrip()


def validate_web_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentError("Only complete http:// or https:// URLs can be opened")
    if parsed.username or parsed.password:
        raise AgentError("URLs containing credentials are not allowed")
    return urllib.parse.urlunsplit(parsed)


def open_url(url: str, browser: str = "edge") -> str:
    target = validate_web_url(url)
    if os.name != "nt":
        raise AgentError("The Edge launcher is currently supported on Windows only")
    if browser != "edge":
        raise AgentError("Only Microsoft Edge is supported")
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise AgentError("Microsoft Edge was not found")
    subprocess.Popen(
        [str(executable), target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return f"Opened {target} in Microsoft Edge. The user must complete sign-in and confirmations."


def asks_for_active_model(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in ("当前模型", "什么模型", "哪个模型", "what model", "which model")
    )


def requested_website(text: str) -> str | None:
    lowered = text.lower()
    if not any(term in lowered for term in ("打开", "open", "浏览器", "edge")):
        return None
    match = re.search(r"https?://[^\s<>\"']+", text, re.IGNORECASE)
    if match:
        return validate_web_url(match.group(0).rstrip(".,;!?，。；！？"))
    if "bilibili" in lowered or "b站" in lowered or "哔哩哔哩" in lowered:
        return "https://www.bilibili.com/"
    return None


ACTION_TERMS = (
    "test", "run", "check", "inspect", "edit", "fix", "open", "browser", "website",
    "create", "write", "copy", "move", "send", "ocr", "automation", "powershell", "script",
    "测试", "运行", "检查", "诊断", "修复", "修改", "读取", "打开", "浏览器", "网站",
    "创建", "新建", "写入", "复制", "移动", "发送", "联系人", "自动化", "脚本",
    "接入", "安装", "下载", "配置", "启用", "添加", "增加", "集成",
)


def requested_missing_capability(text: str) -> str | None:
    """Return a deterministic explanation for actions the runtime cannot perform."""
    lowered = text.lower()
    provision_terms = ("接入", "安装", "下载", "配置", "启用", "添加", "增加", "集成", "install", "setup")
    if any(term in lowered for term in provision_terms):
        return None
    if re.search(r"(?:编写|写一个|创建|新建|开发).{0,12}(?:脚本|工具|代码|项目|能力)", lowered):
        return None
    qq_terms = ("qq", "腾讯qq")
    qq_actions = ("发送", "消息", "点击", "输入", "自动化", "send", "type", "click")
    if any(term in lowered for term in qq_terms) and any(term in lowered for term in qq_actions):
        vision_status = (
            "当前运行时可以截图并用 OCR 查找 QQ 界面文字"
            if desktop_vision_available()
            else "当前运行时也尚未安装截图/OCR 能力"
        )
        return (
            f"{vision_status}，但尚未安装经过验证的鼠标/键盘发送能力，"
            "因此不能点击 QQ 控件或发送消息。需要先实现“准备草稿→用户确认→发送”的两阶段工具。"
        )
    return None


def find_desktop_tool(name: str) -> str | None:
    located = shutil.which(name)
    if located:
        return located
    candidates: list[Path] = []
    if name == "winapp":
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WindowsApps/winapp.exe")
    elif name == "tesseract":
        candidates.extend(
            [
                ROOT / ".runtime/tools/tesseract/tesseract.exe",
                Path(os.environ.get("ProgramFiles", "")) / "Tesseract-OCR/tesseract.exe",
            ]
        )
    return str(next((path for path in candidates if path.is_file()), "")) or None


def desktop_vision_available() -> bool:
    return bool(find_desktop_tool("winapp") and find_desktop_tool("tesseract"))


def inspect_desktop_app(app: str, query: str) -> str:
    if os.name != "nt":
        raise AgentError("Desktop OCR is currently supported on Windows only")
    if not re.fullmatch(r"[\w .-]{1,80}", app, re.UNICODE):
        raise AgentError("app must be a short process name or window title")
    if not query.strip() or len(query) > 200:
        raise AgentError("query must contain between 1 and 200 characters")
    winapp = find_desktop_tool("winapp")
    tesseract = find_desktop_tool("tesseract")
    if not winapp or not tesseract:
        raise AgentError("Desktop OCR dependencies are not installed (requires winapp and tesseract)")

    output_dir = ROOT / ".local-agent" / "desktop"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "-", app).strip("-") or "app"
    screenshot = output_dir / f"{safe_name}-{int(time.time())}.png"
    capture = subprocess.run(
        [winapp, "ui", "screenshot", "-a", app, "--capture-screen", "--output", str(screenshot), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if capture.returncode != 0 or not screenshot.is_file():
        raise AgentError(f"Unable to capture {app}: {(capture.stderr or capture.stdout).strip()}")
    ocr = subprocess.run(
        [tesseract, str(screenshot), "stdout", "-l", "eng", "tsv"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if ocr.returncode != 0:
        raise AgentError(f"OCR failed: {ocr.stderr.strip()}")
    needle = query.casefold().strip()
    matches: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(ocr.stdout), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if needle not in text.casefold():
            continue
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            confidence = -1
        matches.append(
            {
                "text": text,
                "confidence": round(confidence, 1),
                "bounds": [int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"])],
            }
        )
    return json.dumps(
        {"app": app, "query": query, "matches": matches[:20], "screenshot": str(screenshot)},
        ensure_ascii=False,
    )


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "inspect_workspace",
            "description": "List the workspace tree while excluding runtime/model/cache directories.",
            "parameters": {
                "type": "object",
                "properties": {"max_depth": {"type": "integer", "minimum": 0, "maximum": 6}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file with line numbers. Paths must be workspace-relative.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Atomically create or overwrite a UTF-8 file inside the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace exact text in a workspace file after reading it.",
            "parameters": {
                "type": "object",
                "required": ["path", "old_text", "new_text"],
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a PowerShell command and return stdout, stderr, and exit code. In Auto mode the command may use absolute paths, start applications, and reach the local machine outside the coding workspace.",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 900},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a complete HTTP(S) URL in Microsoft Edge. This only navigates; the user completes login, credentials, CAPTCHA, and confirmations.",
            "parameters": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string"},
                    "browser": {"type": "string", "enum": ["edge"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_desktop_app",
            "description": "Capture a running Windows application and use OCR to find visible text. This is read-only and cannot click, type, or send messages.",
            "parameters": {
                "type": "object",
                "required": ["app", "query"],
                "properties": {
                    "app": {"type": "string"},
                    "query": {"type": "string"},
                },
            },
        },
    },
]

BASE_CAPABILITIES = (
    "inspect workspace files; read, create, and replace UTF-8 files inside the workspace; "
    "run PowerShell commands in Auto mode; open HTTP(S) pages in Microsoft Edge"
)

class LocalAgent:
    def __init__(self, workspace: Path, config: dict[str, Any], allow_risky: bool = False):
        self.workspace = workspace.resolve()
        self.config = config
        self.allow_risky = allow_risky
        system_prompt = (ROOT / "prompts" / "system.md").read_text(encoding="utf-8")
        guidance_parts: list[str] = []
        guidance_paths = [ROOT / "AGENT.md", self.workspace / "AGENTS.md", self.workspace / "AGENT.md"]
        seen_guidance: set[Path] = set()
        for guidance_path in guidance_paths:
            guidance_path = guidance_path.resolve()
            if guidance_path in seen_guidance:
                continue
            seen_guidance.add(guidance_path)
            if guidance_path.is_file():
                guidance_parts.append(
                    f"## {guidance_path.name} ({guidance_path.parent})\n"
                    f"{guidance_path.read_text(encoding='utf-8', errors='replace')[:12000]}"
                )
        guidance = "\n\n".join(guidance_parts)
        available_capabilities = BASE_CAPABILITIES
        unavailable_capabilities = "mouse clicks, keyboard injection, and QQ message sending"
        if desktop_vision_available():
            available_capabilities += "; capture running Windows applications and locate visible text with OCR"
        else:
            unavailable_capabilities = "screen capture, OCR, " + unavailable_capabilities
        context = (
            f"\n\nRuntime context:\n- Workspace: {self.workspace}\n- Platform: {sys.platform}"
            f"\n- Active model: {self.config['model']}"
            f"\n- Available capabilities: {available_capabilities}"
            f"\n- Unavailable capabilities: {unavailable_capabilities}\n"
        )
        if guidance:
            context += f"\nWorkspace guidance files:\n{guidance}\n"
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt + context}]
        self.mode = "auto"

    MODE_DIRECTIVES = {
        "plan": "PLAN MODE: inspect and explain only. Do not modify files or run commands. Return a concrete implementation plan.",
        "edits": "EDITS MODE: you may read and edit workspace files. Do not run shell commands; describe commands that the user can run.",
        "auto": "AUTO MODE: execute the task with full local command, application, browser, and filesystem reach. Destructive or credential actions still require an explicit user request.",
    }

    def api_chat(
        self,
        include_tools: bool = True,
        on_token: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": self.config["model"],
            "messages": self.messages,
            "tools": TOOLS if include_tools else [],
            "stream": bool(on_token),
            "think": self.config.get("think", False),
            "keep_alive": self.config.get("keep_alive", "5m"),
            "options": {
                "num_ctx": int(self.config.get("context_length", 8192)),
                "num_predict": int(self.config.get("max_output_tokens", 2048)),
            },
        }
        request = urllib.request.Request(
            self.config["base_url"].rstrip("/") + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        model_timeout = int(self.config.get("model_timeout_seconds", 180))
        for attempt in range(3):
            try:
                with LOCAL_OPENER.open(request, timeout=model_timeout) as response:
                    if on_token is None:
                        return json.loads(response.read().decode("utf-8"))
                    merged_message: dict[str, Any] = {"role": "assistant", "content": ""}
                    done_reason = "stop"
                    for raw_line in response:
                        if not raw_line.strip():
                            continue
                        chunk = json.loads(raw_line.decode("utf-8"))
                        message = chunk.get("message") or {}
                        delta = message.get("content") or ""
                        if delta:
                            merged_message["content"] += delta
                            on_token(delta)
                        if message.get("tool_calls"):
                            merged_message["tool_calls"] = message["tool_calls"]
                        if chunk.get("done"):
                            done_reason = chunk.get("done_reason") or done_reason
                    return {"message": merged_message, "done_reason": done_reason}
            except (TimeoutError, socket.timeout) as exc:
                raise AgentError(
                    f"Model response timed out after {model_timeout}s. Stop, simplify the request, or try another model."
                ) from exc
            except urllib.error.HTTPError as exc:
                last_error = exc
                try:
                    error_body = exc.read().decode("utf-8", errors="replace").strip()
                except OSError:
                    error_body = ""
                if exc.code not in {502, 503, 504} or attempt == 2:
                    detail = error_body
                    try:
                        detail = json.loads(error_body).get("error", error_body)
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    raise AgentError(
                        f"Ollama returned HTTP {exc.code}"
                        + (f": {detail}" if detail else ".")
                    ) from exc
                time.sleep(2 * (attempt + 1))
            except urllib.error.URLError as exc:
                last_error = exc
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    raise AgentError(
                        f"Model response timed out after {model_timeout}s. Stop, simplify the request, or try another model."
                    ) from exc
                if attempt == 2:
                    raise AgentError(
                        f"Cannot reach Ollama at {self.config['base_url']}. "
                        "Run scripts\\start.ps1 first, then retry."
                    ) from exc
                time.sleep(2 * (attempt + 1))
        raise AgentError(f"Ollama request failed: {last_error}")

    def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        on_progress: Callable[[int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        if self.mode == "plan" and name in {"write_file", "replace_in_file", "run_command", "open_url"}:
            return "ERROR: Plan mode is read-only; no files or commands were changed."
        if self.mode == "edits" and name in {"run_command", "open_url"}:
            return "ERROR: Edits mode only changes workspace files; switch to Auto mode for commands or browser navigation."
        if name == "inspect_workspace":
            result = inspect_workspace(self.workspace, arguments.get("max_depth", 3))
        elif name == "read_file":
            result = read_file(
                self.workspace,
                arguments["path"],
                arguments.get("start_line", 1),
                arguments.get("end_line", 400),
            )
        elif name == "write_file":
            result = write_file(
                self.workspace,
                arguments["path"],
                arguments["content"],
                arguments.get("overwrite", False),
            )
        elif name == "replace_in_file":
            result = replace_in_file(
                self.workspace,
                arguments["path"],
                arguments["old_text"],
                arguments["new_text"],
                arguments.get("replace_all", False),
            )
        elif name == "run_command":
            configured_timeout = int(self.config.get("command_timeout_seconds", 120))
            requested_timeout = int(arguments.get("timeout_seconds", configured_timeout))
            result = run_command(
                self.workspace,
                arguments["command"],
                min(requested_timeout, configured_timeout),
                self.allow_risky or self.mode == "auto",
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
        elif name == "open_url":
            result = open_url(arguments["url"], arguments.get("browser", "edge"))
        elif name == "inspect_desktop_app":
            result = inspect_desktop_app(arguments["app"], arguments["query"])
        else:
            raise AgentError(f"Unknown tool: {name}")
        limit = int(self.config.get("max_tool_output_chars", 16000))
        if len(result) > limit:
            result = result[:limit] + f"\n... truncated at {limit} characters"
        return result

    def turn(
        self,
        user_text: str,
        mode: str = "auto",
        on_event: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        if callable(mode) and on_event is None:
            on_event = mode
            mode = "auto"
        self.mode = mode if mode in self.MODE_DIRECTIVES else "auto"
        def emit(event: dict[str, Any]) -> None:
            if on_event is not None:
                on_event(event)
                return
            event_type = event.get("type")
            if event_type == "step":
                print(f"\n[thinking {event['step']}]", flush=True)
            elif event_type == "assistant":
                print(f"\n{event['content']}")
            elif event_type == "tool_start":
                print(f"\n[tool] {event['name']}", flush=True)
            elif event_type == "tool_result":
                output = event["output"]
                print(output[:1000] + ("\n..." if len(output) > 1000 else ""), flush=True)

        if asks_for_active_model(user_text):
            content = f"当前实际运行模型是 `{self.config['model']}`。"
            self.messages.extend(
                [{"role": "user", "content": user_text}, {"role": "assistant", "content": content}]
            )
            emit({"type": "assistant", "content": content, "final": True})
            return content

        missing_capability = requested_missing_capability(user_text)
        if missing_capability:
            self.messages.extend(
                [{"role": "user", "content": user_text}, {"role": "assistant", "content": missing_capability}]
            )
            emit({"type": "capability_error", "content": missing_capability})
            emit({"type": "assistant", "content": missing_capability, "final": True})
            return missing_capability

        website = requested_website(user_text)
        if website and self.mode == "auto":
            emit({"type": "step", "step": 1})
            arguments = {"url": website, "browser": "edge"}
            emit({"type": "tool_start", "name": "open_url", "arguments": arguments})
            try:
                output = self.execute_tool("open_url", arguments)
            except (AgentError, OSError, subprocess.SubprocessError) as exc:
                output = f"ERROR: {exc}"
            emit({"type": "tool_result", "name": "open_url", "output": output})
            content = (
                f"已在 Microsoft Edge 中打开 {website}。请你在浏览器中完成登录、验证码和确认。"
                if not output.startswith("ERROR:")
                else f"无法打开网页：{output[7:]}"
            )
            self.messages.extend(
                [{"role": "user", "content": user_text}, {"role": "assistant", "content": content}]
            )
            emit({"type": "assistant", "content": content, "final": True})
            return content

        execution_hint = ""
        lowered_request = user_text.lower()
        if self.mode == "auto" and any(term in lowered_request for term in ("test", "测试")):
            execution_hint = (
                "\n\nMandatory first action: call run_command with the project's documented test command. "
                "Do not inspect unrelated files before running the tests."
            )
        self.messages.append(
            {
                "role": "user",
                "content": f"[{self.MODE_DIRECTIVES[self.mode]}]\n\nCurrent objective: {user_text}{execution_hint}",
            }
        )
        needs_tool = any(term in user_text.lower() for term in ACTION_TERMS)
        used_tool = False
        tool_nudges = 0
        output_continuations = 0
        max_output_continuations = max(0, int(self.config.get("max_output_continuations", 2)))
        continuation_parts: list[str] = []
        seen_tool_calls: set[str] = set()
        tool_failures = 0
        for step in range(1, int(self.config.get("max_steps", 16)) + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise AgentError("Task cancelled by user")
            emit({"type": "step", "step": step})
            token_callback = None
            if self.config.get("stream", False):
                token_callback = lambda token: emit({"type": "assistant_delta", "content": token})
                response = self.api_chat(on_token=token_callback)
            else:
                response = self.api_chat()
            message = response.get("message", {})
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content", ""),
            }
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            self.messages.append(assistant_message)

            content = assistant_message["content"].strip()
            if not tool_calls:
                if response.get("done_reason") == "length" and output_continuations < max_output_continuations:
                    output_continuations += 1
                    if content:
                        continuation_parts.append(content)
                        emit({"type": "assistant", "content": content, "final": False})
                    self.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was cut off by the output limit. Continue exactly "
                                "where it stopped, finish the answer concisely, and do not repeat earlier text."
                            ),
                        }
                    )
                    continue
                if needs_tool and not used_tool and tool_nudges < 2:
                    tool_nudges += 1
                    if content:
                        emit({"type": "assistant", "content": content, "final": False})
                    self.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You have not executed any tool, so you may not claim the task is verified. "
                                "Call a declared native tool now using the runtime's required tool-call format, "
                                "with a valid tool name and only valid schema arguments."
                            ),
                        }
                    )
                    continue
                if needs_tool and not used_tool:
                    content = (
                        "我无法验证或执行这个操作：模型没有调用任何已注册工具。"
                        "请检查运行记录，或先为该能力安装并注册对应工具。"
                    )
                final_content = "\n\n".join(part for part in [*continuation_parts, content] if part)
                emit({"type": "assistant", "content": final_content, "final": True})
                return final_content

            if content:
                emit({"type": "assistant", "content": content, "final": False})

            for call in tool_calls:
                used_tool = True
                function = call.get("function", {})
                name = function.get("name", "")
                arguments = function.get("arguments") or {}
                emit({"type": "tool_start", "name": name, "arguments": arguments})
                signature = json.dumps([name, arguments], ensure_ascii=False, sort_keys=True)
                if signature in seen_tool_calls:
                    output = "ERROR: Identical repeated tool call blocked. Choose a narrower or different strategy."
                else:
                    seen_tool_calls.add(signature)
                    try:
                        output = self.execute_tool(
                            name,
                            arguments,
                            on_progress=lambda elapsed, tool_name=name: emit(
                                {"type": "tool_progress", "name": tool_name, "elapsed_seconds": elapsed}
                            ),
                            cancel_event=cancel_event,
                        )
                    except (AgentError, OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
                        output = f"ERROR: {exc}"
                emit({"type": "tool_result", "name": name, "output": output})
                if output.startswith("ERROR:"):
                    tool_failures += 1
                else:
                    tool_failures = 0
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": f"{output}\n\n[Current objective: {user_text}. Continue this objective; do not ask for a new task.]",
                    }
                )
            # Some GGUF chat templates (including Qwythos on recent Ollama)
            # require an explicit user turn after tool results. Without this,
            # Ollama fails while rendering the template with HTTP 400:
            # "No user query found in messages."
            self.messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Continue the current objective: {user_text}. Use the tool results above. "
                        "Do not repeat an identical failed call; either take the next concrete step "
                        "or give the final evidence-based answer."
                    ),
                }
            )
            if tool_failures >= 2:
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Multiple tool attempts have failed. Change strategy or narrow the request now; "
                            "do not repeat failed arguments. If no declared tool can complete the objective, "
                            "stop and explain the exact limitation using the observed errors."
                        ),
                    }
                )
            elif step % 4 == 0:
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Progress checkpoint: briefly track the objective, confirmed evidence, and the next "
                            "concrete step internally before continuing. Keep the response concise."
                        ),
                    }
                )
        self.messages.append(
            {
                "role": "user",
                "content": (
                    "The tool-step budget has been reached. Do not call any more tools. "
                    "Using only the evidence already collected in this conversation, provide the final answer now. "
                    "State clearly what was inspected and what remains uncertain."
                ),
            }
        )
        try:
            token_callback = None
            if self.config.get("stream", False):
                token_callback = lambda token: emit({"type": "assistant_delta", "content": token})
                final_response = self.api_chat(include_tools=False, on_token=token_callback)
            else:
                final_response = self.api_chat(include_tools=False)
            final_message = final_response.get("message", {})
            final_content = (final_message.get("content") or "").strip()
        except AgentError as exc:
            raise AgentError(
                f"Tool-step limit reached and final summary failed: {exc}"
            ) from exc
        if not final_content:
            raise AgentError("Tool-step limit reached; the model returned no final summary")
        self.messages.append({"role": "assistant", "content": final_content})
        emit({"type": "assistant", "content": final_content, "final": True})
        return final_content


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Build Agent using Ollama + a local 9B GGUF model")
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace the agent may edit")
    parser.add_argument("--allow-risky", action="store_true", help="Allow commands blocked by safe mode")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"Workspace does not exist: {workspace}", file=sys.stderr)
        return 2
    agent = LocalAgent(workspace, load_json(ROOT / "config.json"), args.allow_risky)
    one_shot = " ".join(args.prompt).strip()
    try:
        if one_shot:
            agent.turn(one_shot)
            return 0
        print(f"Local Build Agent | {agent.config['model']} | workspace: {workspace}")
        print("Commands: /clear, /status, /exit")
        while True:
            try:
                user_text = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nbye")
                return 0
            if not user_text:
                continue
            if user_text in {"/exit", "/quit"}:
                return 0
            if user_text == "/clear":
                agent.messages = agent.messages[:1]
                print("Conversation cleared.")
                continue
            if user_text == "/status":
                print(f"model={agent.config['model']} messages={len(agent.messages)} workspace={workspace}")
                continue
            agent.turn(user_text)
    except AgentError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
