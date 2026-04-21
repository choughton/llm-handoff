from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

from llm_handoff import config
from llm_handoff.agent_process import (
    _first_nonempty_line,
    _first_nonempty_value,
    _run_command_streaming,
    _run_logged_agent_command,
)
from llm_handoff.agent_types import LogFn, SubagentResult, _ProcessResult


@dataclass
class _ClaudeStreamJsonMonitor:
    agent_name: str
    log: LogFn | None
    tool_labels_by_id: dict[str, str] = field(default_factory=dict)
    assistant_chunks: list[str] = field(default_factory=list)
    _assistant_line_buffer: str = ""
    final_result_text: str | None = None

    def consume_stdout(self, chunk: str) -> None:
        if not chunk:
            return

        for raw_line in chunk.splitlines():
            line = raw_line.rstrip("\r")
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if self.log is not None:
                    preview = line[:120]
                    self.log(
                        "WARN",
                        f"{self.agent_name} stream-json parse failed: {preview}",
                    )
                continue
            self._handle_event(event)

    def consume_stderr(self, chunk: str) -> None:
        if self.log is None or not chunk:
            return

        for raw_line in chunk.splitlines():
            line = raw_line.strip()
            if line:
                self.log("INFO", f"{self.agent_name} stderr: {line}")

    def finalize(self, result: _ProcessResult) -> _ProcessResult:
        self._flush_assistant_lines(flush_partial=True)
        final_stdout = self.final_result_text
        if final_stdout is None:
            final_stdout = "".join(self.assistant_chunks)
        return _ProcessResult(
            stdout=final_stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
        )

    def _handle_event(self, event: dict[str, object]) -> None:
        event_type = event.get("type")
        if event_type == "system":
            self._handle_system_event(event)
            return
        if event_type == "assistant":
            self._handle_assistant_event(event)
            return
        if event_type == "user":
            self._handle_user_event(event)
            return
        if event_type == "result":
            self._handle_result_event(event)

    def _handle_system_event(self, event: dict[str, object]) -> None:
        if self.log is None or event.get("subtype") != "init":
            return

        session_id = str(event.get("session_id", "unknown"))
        model = str(event.get("model", "unknown"))
        tools = event.get("tools")
        tool_count = len(tools) if isinstance(tools, list) else 0
        self.log(
            "AGENT",
            f"{self.agent_name}: Claude session {session_id} started (model: {model}, tools: {tool_count})",
        )

    def _handle_assistant_event(self, event: dict[str, object]) -> None:
        message = event.get("message")
        if not isinstance(message, dict):
            return

        content = message.get("content")
        if not isinstance(content, list):
            return

        for item in content:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type == "thinking":
                continue
            if item_type == "tool_use":
                self._handle_tool_use(item)
                continue

            text = _extract_claude_text_fragment(item)
            if text:
                self.assistant_chunks.append(text)
                self._assistant_line_buffer += text
                self._flush_assistant_lines()

    def _handle_user_event(self, event: dict[str, object]) -> None:
        message = event.get("message")
        if not isinstance(message, dict):
            return

        content = message.get("content")
        if not isinstance(content, list):
            return

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result":
                self._handle_tool_result(item)

    def _handle_result_event(self, event: dict[str, object]) -> None:
        result_text = event.get("result")
        if isinstance(result_text, str):
            self.final_result_text = result_text

        if self.log is None:
            return

        stop_reason = str(event.get("stop_reason", "unknown"))
        usage = event.get("usage")
        input_tokens = _coerce_claude_usage_value(usage, "input_tokens")
        output_tokens = _coerce_claude_usage_value(usage, "output_tokens")
        cost = event.get("total_cost_usd")

        summary_parts = [f"stop={stop_reason}"]
        if input_tokens is not None and output_tokens is not None:
            summary_parts.append(f"tokens={input_tokens}/{output_tokens}")
        if cost is not None:
            summary_parts.append(f"cost_usd={cost}")
        self.log(
            "AGENT",
            f"{self.agent_name}: Claude result ({', '.join(summary_parts)})",
        )

    def _handle_tool_use(self, item: dict[str, object]) -> None:
        tool_id = item.get("id")
        name = item.get("name")
        input_payload = item.get("input")
        label = _format_claude_tool_use_label(name, input_payload)
        if isinstance(tool_id, str):
            self.tool_labels_by_id[tool_id] = label
        if self.log is not None:
            self.log("AGENT", f"{self.agent_name}: {label}")

    def _handle_tool_result(self, item: dict[str, object]) -> None:
        tool_use_id = item.get("tool_use_id")
        if isinstance(tool_use_id, str):
            label = self.tool_labels_by_id.pop(tool_use_id, None)
        else:
            label = None
        if label is None:
            label = "Tool"

        content = _extract_claude_tool_result_text(item.get("content"))
        first_line = _first_nonempty_line(content)
        is_error = bool(item.get("is_error"))

        if self.log is None:
            return
        if is_error:
            detail = first_line or "tool reported failure"
            self.log("ERROR", f"{self.agent_name}: {label} failed: {detail}")
        return

    def _flush_assistant_lines(self, *, flush_partial: bool = False) -> None:
        if self.log is None:
            if flush_partial:
                self._assistant_line_buffer = ""
            return

        while "\n" in self._assistant_line_buffer:
            line, self._assistant_line_buffer = self._assistant_line_buffer.split(
                "\n",
                1,
            )
            stripped_line = line.rstrip()
            if stripped_line:
                self.log("AGENT", f"{self.agent_name}: {stripped_line}")

        if flush_partial:
            stripped_line = self._assistant_line_buffer.rstrip()
            if stripped_line:
                self.log("AGENT", f"{self.agent_name}: {stripped_line}")
            self._assistant_line_buffer = ""


def invoke_claude_subagent(
    subagent_name: str,
    prompt: str,
    *,
    binary: str | None = None,
    model: str | None = None,
    permissions_flag: str | None = None,
    timeout_ms: int | None = None,
    agent_name: str | None = None,
    log: LogFn | None = None,
) -> SubagentResult:
    repo_root = Path.cwd()
    resolved_binary = binary or config.CLAUDE_BINARY
    resolved_permissions_flag = permissions_flag or config.CLAUDE_PERMISSIONS_FLAG
    resolved_model = model or config.CLAUDE_MODEL
    resolved_timeout_ms = timeout_ms or config.SUBAGENT_TIMEOUT_MS
    resolved_agent_name = agent_name or f"Claude {subagent_name}"
    start_time = time.monotonic()
    stream_json_command = [
        resolved_binary,
        resolved_permissions_flag,
        "--model",
        resolved_model,
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
        prompt,
    ]
    result = _run_claude_stream_json_command(
        resolved_agent_name,
        stream_json_command,
        cwd=repo_root,
        timeout_ms=resolved_timeout_ms,
        env=_build_claude_env(),
        log=log,
    )
    if _claude_stream_json_unsupported(result):
        if log is not None:
            log(
                "WARN",
                "Claude stream-json output is not supported by the installed Claude CLI. Falling back to buffered text mode; upgrade Claude Code to restore live subagent streaming.",
            )
        result = _run_logged_agent_command(
            resolved_agent_name,
            [
                resolved_binary,
                resolved_permissions_flag,
                "--model",
                resolved_model,
                "-p",
                prompt,
            ],
            cwd=repo_root,
            timeout_ms=resolved_timeout_ms,
            env=_build_claude_env(),
            log=log,
            stream_all_stdout=True,
        )
    return SubagentResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=time.monotonic() - start_time,
    )


def _run_claude_stream_json_command(
    agent_name: str,
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None = None,
    log: LogFn | None = None,
) -> _ProcessResult:
    monitor = _ClaudeStreamJsonMonitor(agent_name=agent_name, log=log)
    result = _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        env=env,
        on_stdout=monitor.consume_stdout,
        on_stderr=monitor.consume_stderr,
    )
    return monitor.finalize(result)


def _claude_stream_json_unsupported(result: _ProcessResult) -> bool:
    if result.exit_code == 0 or result.stdout.strip():
        return False
    normalized_stderr = result.stderr.lower()
    markers = (
        "output-format",
        "unknown flag",
        "unknown option",
        "unrecognized",
    )
    return any(marker in normalized_stderr for marker in markers)


def _extract_claude_text_fragment(item: dict[str, object]) -> str:
    item_type = item.get("type")
    if item_type == "text":
        text = item.get("text")
        return text if isinstance(text, str) else ""

    delta = item.get("delta")
    if isinstance(delta, dict):
        text = delta.get("text")
        if isinstance(text, str):
            return text

    text = item.get("text")
    if isinstance(text, str):
        return text
    return ""


def _extract_claude_tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _format_claude_tool_use_label(name: object, input_payload: object) -> str:
    tool_name = str(name or "Tool")
    payload = input_payload if isinstance(input_payload, dict) else {}
    normalized_name = tool_name.lower()

    if normalized_name == "read":
        path = _first_nonempty_value(payload, "file_path", "path")
        if path:
            return f"Reading {path}"
        return "Reading file"

    if normalized_name in {"edit", "write"}:
        path = _first_nonempty_value(payload, "file_path", "path")
        verb = "Editing" if normalized_name == "edit" else "Writing"
        if path:
            return f"{verb} {path}"
        return f"{verb} file"

    if normalized_name in {"bash", "powershell"}:
        command = _first_nonempty_value(payload, "command", "description")
        if command:
            return f"Running: {command}"
        return "Running command"

    if normalized_name == "grep":
        pattern = _first_nonempty_value(payload, "pattern", "query")
        if pattern:
            return f"Searching: {pattern}"
        return "Searching"

    if normalized_name == "glob":
        pattern = _first_nonempty_value(payload, "pattern", "path")
        if pattern:
            return f"Finding files: {pattern}"
        return "Finding files"

    if normalized_name == "agent":
        subagent = _first_nonempty_value(payload, "subagent_type", "description")
        if subagent:
            return f"Delegating to subagent: {subagent}"
        return "Delegating to subagent"

    return f"Using tool: {tool_name}"


def _coerce_claude_usage_value(usage: object, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if isinstance(value, int):
        return value
    return None


def _build_claude_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env
