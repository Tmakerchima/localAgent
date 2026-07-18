"""A compact, local-first coding agent backed by Ollama."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
IGNORED_DIRS = {".data", ".runtime", ".local-agent", ".git", "__pycache__", "node_modules"}


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


def run_command(workspace: Path, command: str, timeout: int, allow_risky: bool) -> str:
    if not command.strip():
        raise AgentError("Command cannot be empty")
    if not allow_risky and any(re.search(pattern, command, re.IGNORECASE) for pattern in RISKY_COMMANDS):
        raise AgentError("Command blocked by safe mode. Review it and rerun the agent with --allow-risky if intended.")
    if os.name == "nt":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["/bin/sh", "-lc", command]
    completed = subprocess.run(
        argv,
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(1, min(timeout, 900)),
        check=False,
    )
    combined = (completed.stdout + completed.stderr).strip()
    return f"exit_code={completed.returncode}\n{combined}".rstrip()


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
            "description": "Run a PowerShell command in the workspace and return stdout, stderr, and exit code.",
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
]


class LocalAgent:
    def __init__(self, workspace: Path, config: dict[str, Any], allow_risky: bool = False):
        self.workspace = workspace.resolve()
        self.config = config
        self.allow_risky = allow_risky
        system_prompt = (ROOT / "prompts" / "system.md").read_text(encoding="utf-8")
        context = f"\n\nRuntime context:\n- Workspace: {self.workspace}\n- Platform: {sys.platform}\n"
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt + context}]
        self.mode = "auto"

    MODE_DIRECTIVES = {
        "plan": "PLAN MODE: inspect and explain only. Do not modify files or run commands. Return a concrete implementation plan.",
        "edits": "EDITS MODE: you may read and edit workspace files. Do not run shell commands; describe commands that the user can run.",
        "auto": "AUTO MODE: complete the task using the available workspace tools while respecting safe-mode command restrictions.",
    }

    def api_chat(self) -> dict[str, Any]:
        payload = {
            "model": self.config["model"],
            "messages": self.messages,
            "tools": TOOLS,
            "stream": False,
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
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=1800) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {502, 503, 504} or attempt == 2:
                    raise AgentError(
                        f"Ollama returned HTTP {exc.code}. It may still be loading the model; "
                        "wait a minute and retry."
                    ) from exc
                time.sleep(2 * (attempt + 1))
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 2:
                    raise AgentError(
                        f"Cannot reach Ollama at {self.config['base_url']}. "
                        "Run scripts\\start.ps1 first, then retry."
                    ) from exc
                time.sleep(2 * (attempt + 1))
        raise AgentError(f"Ollama request failed: {last_error}")

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if self.mode == "plan" and name in {"write_file", "replace_in_file", "run_command"}:
            return "ERROR: Plan mode is read-only; no files or commands were changed."
        if self.mode == "edits" and name == "run_command":
            return "ERROR: Edits mode does not run shell commands; switch to Auto mode after reviewing the edits."
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
            result = run_command(
                self.workspace,
                arguments["command"],
                int(arguments.get("timeout_seconds", self.config.get("command_timeout_seconds", 120))),
                self.allow_risky,
            )
        else:
            raise AgentError(f"Unknown tool: {name}")
        limit = int(self.config.get("max_tool_output_chars", 16000))
        if len(result) > limit:
            result = result[:limit] + f"\n... truncated at {limit} characters"
        return result

    def turn(self, user_text: str, mode: str = "auto", on_event: Callable[[dict[str, Any]], None] | None = None) -> str:
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

        self.messages.append({"role": "user", "content": f"[{self.MODE_DIRECTIVES[self.mode]}]\n\n{user_text}"})
        for step in range(1, int(self.config.get("max_steps", 16)) + 1):
            emit({"type": "step", "step": step})
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
            if content:
                emit({"type": "assistant", "content": content, "final": not tool_calls})
            if not tool_calls:
                return content

            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name", "")
                arguments = function.get("arguments") or {}
                emit({"type": "tool_start", "name": name, "arguments": arguments})
                try:
                    output = self.execute_tool(name, arguments)
                except (AgentError, OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
                    output = f"ERROR: {exc}"
                emit({"type": "tool_result", "name": name, "output": output})
                self.messages.append({"role": "tool", "tool_name": name, "content": output})
        raise AgentError("Maximum tool-step limit reached; start a new turn with a narrower request")


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

