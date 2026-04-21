from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Literal, TextIO

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from llm_handoff import config


GeminiRole = Literal["PE", "Frontend"]
LogFn = Callable[[str, str], None]
StderrMode = Literal["warn", "codex"]
RateLimitLastBlockPolicy = Literal["emit_when_metadata_complete", "exclude_last"]
_LIVE_AGENT_TOOL_LABELS = frozenset(
    {
        "apply_patch",
        "bash",
        "cat",
        "claude",
        "codex",
        "edit",
        "exec",
        "find",
        "git",
        "grep",
        "ls",
        "open",
        "pwsh",
        "python",
        "read",
        "rg",
        "search",
        "view",
        "write",
    }
)
_DIFF_BLOCK_START_RE = re.compile(r"^diff --git\b")
_DIFF_BLOCK_LINE_RE = re.compile(
    r"^(?:diff --git\b|index [0-9a-f]+\.\.[0-9a-f]+|@@ |--- |\+\+\+ |"
    r"new file mode |deleted file mode |similarity index |rename from |"
    r"rename to |old mode |new mode |Binary files )"
)
_COMMAND_RESULT_RE = re.compile(r"\b(?:succeeded|failed|timed out|completed) in \d")
_COMMAND_LINE_RE = re.compile(r'^(?:"[^"]+"|[A-Za-z]:\\|/).+\bin\s+\S')
_STRUCTURED_STATUS_RE = re.compile(
    r"^(?:VALID|ROUTE|CONFIDENCE|SOURCE|REASONING|WARNINGS|SUMMARY|CHECKS|"
    r"AUDIT SHA|COMMIT SHA|PUSH|VERDICT|STATUS|ERROR|FAILED|PASS)\b"
)
_MARKDOWN_STATUS_RE = re.compile(r"^(?:- (?:\*\*|[A-Z0-9])|\d+\.\s)")
_PROSE_LINE_RE = re.compile(r"^(?:\*\*|[A-Z0-9][^\n]{2,})$")
_CODEX_STDERR_METADATA_RE = re.compile(
    r"^(?:--------$|workdir:|model:|provider:|approval:|sandbox:|reasoning effort:|"
    r"reasoning summaries:|session id:|deprecated:)"
)
_CODEX_STDERR_ERROR_RE = re.compile(
    r"^(?:error:|fatal:|panic:|traceback\b|exception\b)",
    re.IGNORECASE,
)
_REAL_TIME_MONOTONIC = time.monotonic


@dataclass(frozen=True)
class DispatchResult:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float
    session_id: str | None = None
    session_invalidated: bool = False


@dataclass(frozen=True)
class SubagentResult:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float


@dataclass(frozen=True)
class _ProcessResult:
    stdout: str
    stderr: str
    exit_code: int
    session_id: str | None = None


@dataclass(frozen=True)
class _CodexCommandResult:
    result: _ProcessResult
    thread_id: str | None
    final_message: str


@dataclass(frozen=True)
class _CodexArtifactPaths:
    output_directory: Path
    output_last_message_path: Path
    output_schema_path: Path
    session_state_path: Path


@dataclass(frozen=True)
class _GeminiRateLimitEvent:
    cli_attempt: str | None
    metadata: dict[str, str]


@dataclass
class _LiveAgentStreamState:
    in_diff_block: bool = False
    awaiting_tool_descriptor: bool = False
    suppress_tool_output: bool = False


@dataclass(frozen=True)
class _GeminiRateLimitBlock:
    start: int
    text: str
    event: _GeminiRateLimitEvent


@dataclass
class _GeminiRateLimitStreamState:
    text: str = ""
    emitted_block_starts: set[int] = field(default_factory=set)
    logged_block_starts: set[int] = field(default_factory=set)


@dataclass
class _GeminiLiveRateLimitMonitor:
    attempt_number: int
    max_attempts: int
    log: LogFn | None
    live_event_count: int = 0
    _agent_output_monitor: _LiveAgentOutputMonitor | None = field(init=False)
    _stream_states: dict[str, _GeminiRateLimitStreamState] = field(
        default_factory=lambda: {
            "stdout": _GeminiRateLimitStreamState(),
            "stderr": _GeminiRateLimitStreamState(),
        }
    )

    def __post_init__(self) -> None:
        if self.log is None:
            self._agent_output_monitor = None
            return
        self._agent_output_monitor = _LiveAgentOutputMonitor(
            agent_name="Gemini CLI",
            log=self.log,
        )

    def consume(self, stream_name: str, chunk: str) -> None:
        if self.log is None or not chunk:
            return

        if stream_name == "stdout":
            if self._agent_output_monitor is not None:
                self._agent_output_monitor.consume(stream_name, chunk)

        state = self._stream_states[stream_name]
        state.text += chunk
        for block in _extract_ready_gemini_rate_limit_blocks(state.text):
            if block.start in state.emitted_block_starts:
                continue
            state.emitted_block_starts.add(block.start)
            self.live_event_count += 1
            _log_live_gemini_rate_limit_event(
                event=block.event,
                attempt_number=self.attempt_number,
                max_attempts=self.max_attempts,
                log=self.log,
            )
        self._log_completed_blocks(stream_name, include_last_block=False)

    def finalize(
        self,
        result: _ProcessResult,
        *,
        command_succeeded: bool,
    ) -> list[_GeminiRateLimitEvent]:
        for stream_name, stream_text in (
            ("stdout", result.stdout),
            ("stderr", result.stderr),
        ):
            if stream_text:
                # Streaming callbacks may receive partial chunks; final captured
                # stdout/stderr are authoritative for deduplicated rate-limit
                # block extraction at process exit.
                self._stream_states[stream_name].text = stream_text

        events = _extract_gemini_rate_limit_events(result)
        if self.log is None:
            return events

        live_detail_logged = any(
            state.logged_block_starts for state in self._stream_states.values()
        )
        for stream_name in self._stream_states:
            live_detail_logged = (
                self._log_completed_blocks(stream_name, include_last_block=True)
                or live_detail_logged
            )
        if not events:
            return events

        _log_gemini_rate_limit_events(
            result=result,
            events=events,
            attempt_number=self.attempt_number,
            max_attempts=self.max_attempts,
            command_succeeded=command_succeeded,
            log=self.log,
            already_emitted_count=self.live_event_count,
            detail_logged=live_detail_logged,
        )
        return events

    def _log_completed_blocks(
        self,
        stream_name: str,
        *,
        include_last_block: bool,
    ) -> bool:
        state = self._stream_states[stream_name]
        logged_any = False
        for block in _extract_completed_gemini_rate_limit_blocks(
            state.text,
            include_last_block=include_last_block,
        ):
            if block.start in state.logged_block_starts:
                continue
            state.logged_block_starts.add(block.start)
            logged_any = True
            _log_gemini_rate_limit_block_detail(
                block=block,
                stream_name=stream_name,
                attempt_number=self.attempt_number,
                max_attempts=self.max_attempts,
                log=self.log,
            )
        return logged_any


@dataclass
class _LiveAgentOutputMonitor:
    agent_name: str
    log: LogFn | None
    stream_all_stdout: bool = False
    stderr_mode: StderrMode = "warn"
    _stdout_state: _LiveAgentStreamState = field(default_factory=_LiveAgentStreamState)
    _stderr_state: _LiveAgentStreamState = field(default_factory=_LiveAgentStreamState)

    def consume(self, stream_name: str, chunk: str) -> None:
        if self.log is None or not chunk:
            return

        for raw_line in chunk.splitlines():
            self._consume_line(stream_name, raw_line.rstrip("\r"))

    def _consume_line(self, stream_name: str, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return

        if stream_name == "stderr":
            if self.stderr_mode == "codex":
                filtered = _filter_codex_stderr_line(
                    stripped,
                    state=self._stderr_state,
                )
                if filtered is None:
                    return

                level, message = filtered
                if level == "AGENT":
                    self.log("AGENT", f"{self.agent_name}: {message}")
                else:
                    self.log("WARN", f"{self.agent_name} stderr: {message}")
                return

            self.log("WARN", f"{self.agent_name} stderr: {stripped}")
            return

        filtered_line = _filter_live_agent_stdout_line(
            stripped,
            state=self._stdout_state,
            stream_all_stdout=self.stream_all_stdout,
        )
        if filtered_line is not None:
            self.log("AGENT", f"{self.agent_name}: {filtered_line}")


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


@dataclass
class _GeminiStreamJsonMonitor:
    agent_name: str
    log: LogFn | None
    attempt_number: int
    max_attempts: int
    session_id: str | None = None
    tool_labels_by_id: dict[str, str] = field(default_factory=dict)
    assistant_chunks: list[str] = field(default_factory=list)
    _assistant_line_buffer: str = ""
    _rate_limit_monitor: _GeminiLiveRateLimitMonitor | None = field(init=False)

    def __post_init__(self) -> None:
        if self.log is None:
            self._rate_limit_monitor = None
            return
        self._rate_limit_monitor = _GeminiLiveRateLimitMonitor(
            attempt_number=self.attempt_number,
            max_attempts=self.max_attempts,
            log=self.log,
        )

    def consume_stdout(self, chunk: str) -> None:
        self._consume_stream("stdout", chunk)

    def consume_stderr(self, chunk: str) -> None:
        if not chunk:
            return
        if self._rate_limit_monitor is not None:
            self._rate_limit_monitor.consume("stderr", chunk)

    def finalize(self, result: _ProcessResult) -> _ProcessResult:
        self._flush_assistant_lines(flush_partial=True)
        if self._rate_limit_monitor is not None:
            self._rate_limit_monitor.finalize(
                result,
                command_succeeded=result.exit_code == 0,
            )
        assistant_text = "".join(self.assistant_chunks).strip()
        return _ProcessResult(
            stdout=assistant_text or result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            session_id=self.session_id or result.session_id,
        )

    def _consume_stream(self, stream_name: str, chunk: str) -> None:
        if not chunk:
            return

        if self._rate_limit_monitor is not None:
            self._rate_limit_monitor.consume(stream_name, chunk)
        for raw_line in chunk.splitlines():
            line = raw_line.rstrip("\r")
            if not line.strip():
                continue
            self._consume_line(stream_name, line)

    def _consume_line(self, stream_name: str, line: str) -> None:
        event = _parse_gemini_stream_json_line(
            line, agent_name=self.agent_name, log=self.log
        )
        if event is None:
            return
        if not isinstance(event, dict):
            return
        self._handle_event(stream_name, event)

    def _handle_event(self, stream_name: str, event: dict[str, object]) -> None:
        del stream_name
        event_type = str(event.get("type", ""))
        if event_type == "init":
            self._handle_init_event(event)
            return
        if event_type == "message":
            self._handle_message_event(event)
            return
        if event_type in {"text", "chunk"}:
            self._append_assistant_text(_extract_gemini_stream_text(event))
            return
        if event_type in {"tool_use", "tool_call"}:
            self._handle_tool_use(event)
            return
        if event_type == "tool_result":
            self._handle_tool_result(event)
            return
        if event_type == "result":
            self._handle_result_event(event)
            return
        if event_type == "error":
            self._handle_error_event(event)

    def _handle_init_event(self, event: dict[str, object]) -> None:
        if self.log is None:
            return
        session_id = event.get("session_id")
        model = event.get("model")
        summary = "Gemini session started"
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
            summary = f"Gemini session {session_id} started"
        if isinstance(model, str) and model:
            summary = f"{summary} (model: {model})"
        self.log("AGENT", f"{self.agent_name}: {summary}")

    def _handle_message_event(self, event: dict[str, object]) -> None:
        if str(event.get("role", "")).lower() != "assistant":
            return
        self._append_assistant_text(_extract_gemini_stream_text(event.get("content")))

    def _handle_tool_use(self, event: dict[str, object]) -> None:
        tool_name = event.get("tool_name")
        tool_id = event.get("tool_id")
        parameters = event.get("parameters")
        label = _format_gemini_tool_use_label(tool_name, parameters)
        if isinstance(tool_id, str):
            self.tool_labels_by_id[tool_id] = label
        if self.log is not None:
            self.log("AGENT", f"{self.agent_name}: {label}")

    def _handle_tool_result(self, event: dict[str, object]) -> None:
        tool_id = event.get("tool_id")
        if isinstance(tool_id, str):
            label = self.tool_labels_by_id.pop(tool_id, None)
        else:
            label = None
        if label is None:
            label = "Tool"

        status = str(event.get("status", "")).lower()
        if status in {"success", "ok"}:
            return

        detail = _first_nonempty_line(
            _extract_gemini_stream_text(event.get("output"))
            or _extract_gemini_stream_text(event.get("error"))
        )
        if self.log is not None:
            self.log(
                "WARN",
                f"{self.agent_name}: {label} failed: {detail or 'tool reported failure'}",
            )

    def _handle_result_event(self, event: dict[str, object]) -> None:
        if self.log is None:
            return
        status = str(event.get("status", "unknown"))
        stats = event.get("stats")
        tokens = _format_gemini_result_tokens(stats)
        suffix = f", tokens={tokens}" if tokens is not None else ""
        self.log("AGENT", f"{self.agent_name}: Gemini result (status={status}{suffix})")

    def _handle_error_event(self, event: dict[str, object]) -> None:
        if self.log is None:
            return
        detail = _first_nonempty_line(
            _extract_gemini_stream_text(event.get("message"))
            or _extract_gemini_stream_text(event.get("error"))
        )
        if detail:
            self.log("WARN", f"{self.agent_name}: {detail}")

    def _append_assistant_text(self, text: str) -> None:
        if not text:
            return
        self.assistant_chunks.append(text)
        self._assistant_line_buffer += text
        self._flush_assistant_lines(flush_partial=True)

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


@dataclass
class _CodexJsonMonitor:
    agent_name: str
    log: LogFn | None
    thread_id: str | None = None
    last_agent_message: str = ""
    _stderr_state: _LiveAgentStreamState = field(default_factory=_LiveAgentStreamState)

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
                    self.log(
                        "WARN",
                        f"{self.agent_name} JSON parse failed: {line[:120]}",
                    )
                continue
            self._handle_event(event)

    def consume_stderr(self, chunk: str) -> None:
        if self.log is None or not chunk:
            return

        for raw_line in chunk.splitlines():
            filtered = _filter_codex_json_stderr_line(
                raw_line.strip(),
                state=self._stderr_state,
            )
            if filtered is None:
                continue

            level, message = filtered
            if level == "AGENT":
                self.log("AGENT", f"{self.agent_name}: {message}")
            else:
                self.log("WARN", f"{self.agent_name} stderr: {message}")

    def _handle_event(self, event: dict[str, object]) -> None:
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                self.thread_id = thread_id
                if self.log is not None:
                    self.log(
                        "AGENT",
                        f"{self.agent_name}: Codex session {thread_id} started",
                    )
            return

        if event_type in {"item.started", "item.completed"}:
            item = event.get("item")
            if isinstance(item, dict):
                self._handle_item(event_type, item)
            return

        if event_type == "turn.completed":
            usage = event.get("usage")
            if not isinstance(usage, dict):
                return
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            cached_input_tokens = usage.get("cached_input_tokens")
            if (
                self.log is not None
                and isinstance(input_tokens, int)
                and isinstance(output_tokens, int)
            ):
                cached_suffix = ""
                if isinstance(cached_input_tokens, int):
                    cached_suffix = f", cached={cached_input_tokens}"
                self.log(
                    "INFO",
                    f"{self.agent_name}: turn completed (tokens={input_tokens}/{output_tokens}{cached_suffix})",
                )

    def _handle_item(self, event_type: str, item: dict[str, object]) -> None:
        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                self.last_agent_message = text
                if _parse_codex_structured_message(text) is not None:
                    return
                if self.log is not None:
                    for line in text.splitlines():
                        stripped_line = line.strip()
                        if stripped_line:
                            self.log("AGENT", f"{self.agent_name}: {stripped_line}")
            return

        if item_type == "command_execution":
            command = item.get("command")
            if not isinstance(command, str) or self.log is None:
                return
            if event_type == "item.started":
                self.log("AGENT", f"{self.agent_name}: exec")
                self.log("AGENT", f"{self.agent_name}: {command}")
                return

            exit_code = item.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                self.log(
                    "AGENT",
                    f"{self.agent_name}: command failed with exit code {exit_code}: {command}",
                )
            return

        if item_type == "error":
            message = item.get("message")
            if isinstance(message, str) and self.log is not None:
                self.log("WARN", f"{self.agent_name}: {message}")


def invoke_gemini(
    role: GeminiRole,
    handoff_path: Path,
    *,
    use_api_key_env: bool = False,
    additional_instruction: str | None = None,
    use_resume: bool = False,
    session_id: str | None = None,
    previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    log: LogFn | None = None,
) -> DispatchResult:
    repo_root = Path.cwd()
    resolved_handoff_path = _resolve_handoff_path(handoff_path, repo_root)
    should_resume = role == "PE" and use_resume and bool(session_id)
    command = _build_gemini_command(
        role,
        resolved_handoff_path,
        additional_instruction=additional_instruction,
        resume_session_id=session_id if should_resume else None,
        previous_handoff_sha=previous_handoff_sha,
        current_handoff_sha=current_handoff_sha,
    )

    start_time = time.monotonic()
    result = _run_gemini_with_retries(
        command,
        repo_root=repo_root,
        use_api_key_env=use_api_key_env,
        stop_on_resume_recoverable_failure=should_resume,
        log=log,
    )
    session_invalidated = False

    if (
        should_resume
        and result.exit_code != 0
        and _is_gemini_resume_recoverable_failure(result)
    ):
        session_invalidated = True
        if log is not None:
            log(
                "WARN",
                "Gemini PE managed session "
                f"{session_id} could not be resumed. Clearing the in-memory session and starting a fresh Gemini PE session for this dispatch.",
            )
        fresh_command = _build_gemini_command(
            role,
            resolved_handoff_path,
            additional_instruction=additional_instruction,
            resume_session_id=None,
            previous_handoff_sha=None,
            current_handoff_sha=current_handoff_sha,
        )
        result = _run_gemini_with_retries(
            fresh_command,
            repo_root=repo_root,
            use_api_key_env=use_api_key_env,
            stop_on_resume_recoverable_failure=False,
            log=log,
        )

    result_session_id = result.session_id
    if result_session_id is None and should_resume and result.exit_code == 0:
        result_session_id = session_id

    return DispatchResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=time.monotonic() - start_time,
        session_id=result_session_id,
        session_invalidated=session_invalidated,
    )


def _build_gemini_command(
    role: GeminiRole,
    resolved_handoff_path: Path,
    *,
    additional_instruction: str | None,
    resume_session_id: str | None,
    previous_handoff_sha: str | None,
    current_handoff_sha: str | None,
) -> list[str]:
    command = [config.GEMINI_BINARY]
    if resume_session_id:
        command.extend(["--resume", resume_session_id])
    command.extend(
        [
            "--output-format",
            "stream-json",
            "--yolo",
            "--sandbox=false",
            "-p",
            _build_gemini_prompt(
                role,
                resolved_handoff_path,
                additional_instruction=additional_instruction,
                use_resume=bool(resume_session_id),
                previous_handoff_sha=previous_handoff_sha,
                current_handoff_sha=current_handoff_sha,
            ),
        ]
    )
    return command


def _build_gemini_prompt(
    role: GeminiRole,
    resolved_handoff_path: Path,
    *,
    additional_instruction: str | None,
    use_resume: bool,
    previous_handoff_sha: str | None,
    current_handoff_sha: str | None,
) -> str:
    mention = (
        config.GEMINI_PE_MENTION if role == "PE" else config.GEMINI_FRONTEND_MENTION
    )
    if use_resume:
        prompt = (
            f"{mention} Continue your assigned task based on the current handoff state. "
            "The HANDOFF.md SHA at your prior turn was "
            f"{previous_handoff_sha or 'unknown'}; the current SHA is "
            f"{current_handoff_sha or 'unknown'}. If the SHA has changed, use your tools "
            f"to re-read the latest state of {resolved_handoff_path}. "
            "Treat HANDOFF.md and repo source-of-truth documents as authoritative over prior memory."
        )
    else:
        prompt = (
            f"{mention} Execute your assigned task based on the provided handoff "
            f"document. @{resolved_handoff_path}"
        )
    if additional_instruction:
        prompt = f"{prompt} Additional instruction: {additional_instruction.strip()}"
    return prompt


def _run_gemini_with_retries(
    command: list[str],
    *,
    repo_root: Path,
    use_api_key_env: bool,
    stop_on_resume_recoverable_failure: bool,
    log: LogFn | None,
) -> _ProcessResult:
    max_attempts = config.GEMINI_MAX_RETRIES + 1
    env = _build_gemini_env(use_api_key_env=use_api_key_env)
    attempt_count = 0

    def run_attempt() -> _ProcessResult:
        nonlocal attempt_count
        attempt_count += 1
        result = _run_gemini_command(
            command,
            cwd=repo_root,
            timeout_ms=config.AGENT_TIMEOUT_MS,
            env=env,
            attempt_number=attempt_count,
            max_attempts=max_attempts,
            log=log,
        )
        rate_limit_events = _extract_gemini_rate_limit_events(result)
        if (
            result.exit_code != 0
            and not rate_limit_events
            and not (
                stop_on_resume_recoverable_failure
                and _is_gemini_resume_recoverable_failure(result)
            )
        ):
            _log_gemini_failure(
                result=result,
                attempt_number=attempt_count,
                max_attempts=max_attempts,
                log=log,
            )
        return result

    retrying = Retrying(
        retry=retry_if_result(
            lambda result: _should_retry_gemini_result(
                result,
                stop_on_resume_recoverable_failure=stop_on_resume_recoverable_failure,
            )
        ),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=config.GEMINI_RETRY_BASE_SECONDS),
        sleep=time.sleep,
        before_sleep=lambda retry_state: _log_gemini_retry_wait(
            retry_state,
            max_attempts=max_attempts,
            log=log,
        ),
        retry_error_callback=_return_last_gemini_retry_result,
    )
    return _run_tenacity_with_real_clock(retrying, run_attempt)


def _run_tenacity_with_real_clock(
    retrying: Retrying,
    run_attempt: Callable[[], _ProcessResult],
) -> _ProcessResult:
    current_monotonic = time.monotonic
    if current_monotonic is _REAL_TIME_MONOTONIC:
        return retrying(run_attempt)

    # Keep Tenacity's internal stopwatch independent from the dispatch clock;
    # callers may replace agents.time.monotonic when measuring elapsed time.
    time.monotonic = _REAL_TIME_MONOTONIC
    try:
        return retrying(run_attempt)
    finally:
        time.monotonic = current_monotonic


def _should_retry_gemini_result(
    result: _ProcessResult,
    *,
    stop_on_resume_recoverable_failure: bool,
) -> bool:
    if result.exit_code == 0:
        return False
    if stop_on_resume_recoverable_failure and _is_gemini_resume_recoverable_failure(
        result
    ):
        return False
    return True


def _log_gemini_retry_wait(
    retry_state: RetryCallState,
    *,
    max_attempts: int,
    log: LogFn | None,
) -> None:
    if log is None or retry_state.next_action is None:
        return

    sleep_seconds = retry_state.next_action.sleep
    log(
        "WARN",
        "Gemini CLI dispatch attempt "
        f"{retry_state.attempt_number}/{max_attempts} failed; "
        f"retrying in {sleep_seconds:g}s.",
    )


def _return_last_gemini_retry_result(
    retry_state: RetryCallState,
) -> _ProcessResult:
    if retry_state.outcome is None:
        return _ProcessResult(stdout="", stderr="", exit_code=1)
    return retry_state.outcome.result()


def _is_gemini_resume_recoverable_failure(result: _ProcessResult) -> bool:
    failure_text = _gemini_failure_text(result).lower()
    resume_markers = (
        "error resuming session",
        "failed to resume session",
        "invalid session identifier",
        "no previous sessions found",
        "session not found",
    )
    return any(marker in failure_text for marker in resume_markers)


def invoke_codex(
    handoff_path: Path,
    *,
    log: LogFn | None = None,
    use_resume: bool = False,
) -> DispatchResult:
    repo_root = Path.cwd()
    artifact_paths = _codex_artifact_paths(repo_root)
    _cleanup_codex_output_artifacts(artifact_paths)
    start_time = time.monotonic()
    if use_resume:
        saved_thread_id = _read_codex_session_state(artifact_paths)
        if saved_thread_id:
            result = _run_codex_json_command(
                "Codex",
                _build_codex_resume_command(
                    handoff_path,
                    artifact_paths=artifact_paths,
                    thread_id=saved_thread_id,
                ),
                cwd=repo_root,
                timeout_ms=config.AGENT_TIMEOUT_MS,
                output_last_message_path=artifact_paths.output_last_message_path,
                log=log,
            )
            if (
                result.result.exit_code != 0
                and result.thread_id is None
                and _is_codex_resume_recoverable_failure(result.result)
            ):
                if log is not None:
                    log(
                        "WARN",
                        "Codex managed session "
                        f"{saved_thread_id} could not be resumed. Clearing the saved session and starting a fresh Codex session for this dispatch.",
                    )
                _clear_codex_session_state(artifact_paths)
                result = _run_codex_json_command(
                    "Codex",
                    _build_codex_exec_command(
                        _build_codex_managed_bootstrap_prompt(handoff_path),
                        artifact_paths=artifact_paths,
                    ),
                    cwd=repo_root,
                    timeout_ms=config.AGENT_TIMEOUT_MS,
                    output_last_message_path=artifact_paths.output_last_message_path,
                    log=log,
                )
        else:
            result = _run_codex_json_command(
                "Codex",
                _build_codex_exec_command(
                    _build_codex_managed_bootstrap_prompt(handoff_path),
                    artifact_paths=artifact_paths,
                ),
                cwd=repo_root,
                timeout_ms=config.AGENT_TIMEOUT_MS,
                output_last_message_path=artifact_paths.output_last_message_path,
                log=log,
            )
        if result.thread_id:
            _write_codex_session_state(artifact_paths, result.thread_id)
    else:
        result = _run_codex_json_command(
            "Codex",
            _build_codex_exec_command(
                _build_codex_stateless_prompt(handoff_path),
                artifact_paths=artifact_paths,
            ),
            cwd=repo_root,
            timeout_ms=config.AGENT_TIMEOUT_MS,
            output_last_message_path=artifact_paths.output_last_message_path,
            log=log,
        )
    return DispatchResult(
        stdout=result.final_message or result.result.stdout,
        stderr=result.result.stderr,
        exit_code=result.result.exit_code,
        elapsed_seconds=time.monotonic() - start_time,
    )


def _build_codex_exec_command(
    prompt: str,
    *,
    artifact_paths: _CodexArtifactPaths,
) -> list[str]:
    return [
        config.CODEX_BINARY,
        "--yolo",
        "exec",
        "--json",
        "-c",
        f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
        "--output-schema",
        str(artifact_paths.output_schema_path),
        "--output-last-message",
        str(artifact_paths.output_last_message_path),
        prompt,
    ]


def _build_codex_resume_command(
    handoff_path: Path,
    *,
    artifact_paths: _CodexArtifactPaths,
    thread_id: str,
) -> list[str]:
    return [
        config.CODEX_BINARY,
        "--yolo",
        "exec",
        "resume",
        "--json",
        "-c",
        f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
        "--output-last-message",
        str(artifact_paths.output_last_message_path),
        thread_id,
        _build_codex_resume_prompt(handoff_path),
    ]


def _build_codex_stateless_prompt(handoff_path: Path) -> str:
    return (
        f"Use the repo skill ${config.CODEX_SKILL_NAME}. Read {handoff_path} and treat it as the live task file. "
        "Work statelessly and do not assume prior session memory. "
        "The skill replaces any legacy shared bootstrap or Codex handoff prompt files for this repo. "
        "Follow the skill's repo operating procedure, read/write/follow HANDOFF.md as needed, "
        "and execute the current Codex assignment."
    )


def _build_codex_managed_bootstrap_prompt(handoff_path: Path) -> str:
    return (
        f"Use the repo skill ${config.CODEX_SKILL_NAME}. This is the first managed Codex dispatch session for this repo. "
        f"Read {config.DEFAULT_SHARED_INIT_PROMPT_PATH} and use it as the bootstrap checklist for repo context loading, but do not stop after the startup report. "
        f"After bootstrapping, read {handoff_path} and treat it as the live task file. "
        "Follow HANDOFF.md as needed, update it when you finish, and execute the current Codex assignment."
    )


def _build_codex_resume_prompt(handoff_path: Path) -> str:
    return (
        "Continue the existing Codex dispatch session for this repo. "
        f"Re-read {handoff_path} and treat it as the live task file. "
        f"Follow the repo skill ${config.CODEX_SKILL_NAME} as the governing workflow. "
        "Use prior session context where it helps, but treat HANDOFF.md and the repo source-of-truth docs as authoritative for the current assignment."
    )


def _codex_artifact_paths(repo_root: Path) -> _CodexArtifactPaths:
    return _CodexArtifactPaths(
        output_directory=(repo_root / config.CODEX_OUTPUT_DIRECTORY).resolve(),
        output_last_message_path=(
            repo_root / config.CODEX_OUTPUT_LAST_MESSAGE_PATH
        ).resolve(),
        output_schema_path=(repo_root / config.CODEX_OUTPUT_SCHEMA_PATH).resolve(),
        session_state_path=(repo_root / config.CODEX_SESSION_STATE_PATH).resolve(),
    )


def _cleanup_codex_output_artifacts(paths: _CodexArtifactPaths) -> None:
    paths.output_directory.mkdir(parents=True, exist_ok=True)
    if paths.output_last_message_path.exists():
        paths.output_last_message_path.unlink()


def _read_codex_session_state(paths: _CodexArtifactPaths) -> str | None:
    if not paths.session_state_path.exists():
        return None
    try:
        payload = json.loads(paths.session_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    thread_id = payload.get("thread_id")
    if isinstance(thread_id, str) and thread_id.strip():
        return thread_id.strip()
    return None


def _write_codex_session_state(paths: _CodexArtifactPaths, thread_id: str) -> None:
    paths.output_directory.mkdir(parents=True, exist_ok=True)
    paths.session_state_path.write_text(
        json.dumps({"thread_id": thread_id}, indent=2),
        encoding="utf-8",
    )


def _clear_codex_session_state(paths: _CodexArtifactPaths) -> None:
    if paths.session_state_path.exists():
        paths.session_state_path.unlink()


def _is_codex_resume_recoverable_failure(result: _ProcessResult) -> bool:
    failure_text = f"{result.stderr}\n{result.stdout}".lower()
    resume_markers = ("session not found", "thread not found", "unknown session")
    return any(marker in failure_text for marker in resume_markers)


def invoke_manual_frontend(
    handoff_path: Path,
    *,
    log: LogFn | None = None,
) -> DispatchResult:
    repo_root = Path.cwd()
    resolved_handoff_path = _resolve_handoff_path(handoff_path, repo_root)
    message = (
        "manual frontend is a manual GUI step. Complete the frontend work using "
        f"{resolved_handoff_path}, then press any key to continue dispatch."
    )
    start_time = time.monotonic()

    if log is not None:
        log("PAUSE", message)
    else:
        print(message, flush=True)

    _wait_for_manual_continue()

    return DispatchResult(
        stdout="",
        stderr="",
        exit_code=0,
        elapsed_seconds=time.monotonic() - start_time,
    )


def invoke_claude_subagent(
    subagent_name: str,
    prompt: str,
    *,
    log: LogFn | None = None,
) -> SubagentResult:
    repo_root = Path.cwd()
    start_time = time.monotonic()
    stream_json_command = [
        config.CLAUDE_BINARY,
        config.CLAUDE_PERMISSIONS_FLAG,
        "--model",
        config.CLAUDE_MODEL,
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
        prompt,
    ]
    result = _run_claude_stream_json_command(
        f"Claude {subagent_name}",
        stream_json_command,
        cwd=repo_root,
        timeout_ms=config.SUBAGENT_TIMEOUT_MS,
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
            f"Claude {subagent_name}",
            [
                config.CLAUDE_BINARY,
                config.CLAUDE_PERMISSIONS_FLAG,
                "--model",
                config.CLAUDE_MODEL,
                "-p",
                prompt,
            ],
            cwd=repo_root,
            timeout_ms=config.SUBAGENT_TIMEOUT_MS,
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


def invoke_backend_role(
    handoff_path: Path,
    *,
    log: LogFn | None = None,
    use_resume: bool = False,
) -> DispatchResult:
    return invoke_codex(handoff_path, log=log, use_resume=use_resume)


def invoke_planner_role(
    handoff_path: Path,
    *,
    use_api_key_env: bool = False,
    additional_instruction: str | None = None,
    use_resume: bool = False,
    session_id: str | None = None,
    previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    log: LogFn | None = None,
) -> DispatchResult:
    return invoke_gemini(
        "PE",
        handoff_path,
        use_api_key_env=use_api_key_env,
        additional_instruction=additional_instruction,
        use_resume=use_resume,
        session_id=session_id,
        previous_handoff_sha=previous_handoff_sha,
        current_handoff_sha=current_handoff_sha,
        log=log,
    )


def invoke_frontend_role(
    handoff_path: Path,
    *,
    use_manual_frontend: bool = False,
    use_api_key_env: bool = False,
    additional_instruction: str | None = None,
    log: LogFn | None = None,
) -> DispatchResult:
    if use_manual_frontend:
        return invoke_manual_frontend(handoff_path, log=log)
    return invoke_gemini(
        "Frontend",
        handoff_path,
        use_api_key_env=use_api_key_env,
        additional_instruction=additional_instruction,
        log=log,
    )


def invoke_support_role(
    subagent_name: str,
    prompt: str,
    *,
    log: LogFn | None = None,
) -> SubagentResult:
    return invoke_claude_subagent(subagent_name, prompt, log=log)


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


def _run_codex_json_command(
    agent_name: str,
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    output_last_message_path: Path,
    log: LogFn | None = None,
) -> _CodexCommandResult:
    monitor = _CodexJsonMonitor(agent_name=agent_name, log=log)
    result = _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        on_stdout=monitor.consume_stdout,
        on_stderr=monitor.consume_stderr,
    )
    final_message = monitor.last_agent_message
    saved_last_message = _read_codex_output_last_message(output_last_message_path)
    if saved_last_message:
        final_message = saved_last_message
    return _CodexCommandResult(
        result=result,
        thread_id=monitor.thread_id,
        final_message=final_message,
    )


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


def _gemini_stream_json_unsupported(result: _ProcessResult) -> bool:
    if result.exit_code == 0:
        return False
    normalized_output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    ).lower()
    if "output-format" not in normalized_output:
        return False
    markers = (
        "unknown flag",
        "unknown option",
        "unknown argument",
        "unrecognized",
        "unsupported",
        "invalid choice",
    )
    return any(marker in normalized_output for marker in markers)


def _read_codex_output_last_message(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _parse_codex_structured_message(text: str) -> dict[str, str] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    status = payload.get("status")
    summary = payload.get("summary")
    if isinstance(status, str) and isinstance(summary, str):
        return {"status": status, "summary": summary}
    return None


def _parse_gemini_stream_json_line(
    line: str,
    *,
    agent_name: str,
    log: LogFn | None,
) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        if log is not None:
            log(
                "WARN",
                f"{agent_name} stream-json parse failed: {stripped[:120]}",
            )
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _extract_gemini_stream_text(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return "".join(_extract_gemini_stream_text(item) for item in payload)
    if isinstance(payload, dict):
        for key in ("content", "text", "message", "output"):
            value = payload.get(key)
            text = _extract_gemini_stream_text(value)
            if text:
                return text
    return ""


def _format_gemini_tool_use_label(name: object, parameters: object) -> str:
    tool_name = str(name or "tool")
    payload = parameters if isinstance(parameters, dict) else {}
    normalized_name = tool_name.lower()

    if normalized_name in {"read_file", "read_many_files"}:
        path = _first_nonempty_value(payload, "file_path", "path")
        if path:
            return f"Reading {path}"
        return "Reading file"

    if normalized_name in {"replace", "write_file"}:
        path = _first_nonempty_value(payload, "file_path", "path")
        verb = "Editing" if normalized_name == "replace" else "Writing"
        if path:
            return f"{verb} {path}"
        return f"{verb} file"

    if normalized_name in {"run_shell_command", "shell"}:
        command = _first_nonempty_value(payload, "command", "description")
        if command:
            return f"Running: {command}"
        return "Running command"

    if normalized_name in {"search_file_content", "grep"}:
        pattern = _first_nonempty_value(payload, "pattern", "query")
        if pattern:
            return f"Searching: {pattern}"
        return "Searching"

    if normalized_name in {"find_files", "glob"}:
        pattern = _first_nonempty_value(payload, "pattern", "path")
        if pattern:
            return f"Finding files: {pattern}"
        return "Finding files"

    return f"Using tool: {tool_name}"


def _format_gemini_result_tokens(stats: object) -> str | None:
    if not isinstance(stats, dict):
        return None

    input_tokens = stats.get("inputTokens")
    if not isinstance(input_tokens, int):
        input_tokens = stats.get("input_tokens")

    output_tokens = stats.get("outputTokens")
    if not isinstance(output_tokens, int):
        output_tokens = stats.get("output_tokens")

    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        return f"{input_tokens} input / {output_tokens} output tokens"
    return None


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


def _first_nonempty_value(payload: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _coerce_claude_usage_value(usage: object, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if isinstance(value, int):
        return value
    return None


def _resolve_handoff_path(handoff_path: Path, repo_root: Path) -> Path:
    if handoff_path.is_absolute():
        return handoff_path
    return (repo_root / handoff_path).resolve()


def _build_claude_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _build_gemini_env(*, use_api_key_env: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("GOOGLE_API_KEY", None)
    if not use_api_key_env:
        env.pop("GEMINI_API_KEY", None)
    return env


def _log_gemini_failure(
    *,
    result: _ProcessResult,
    attempt_number: int,
    max_attempts: int,
    log: LogFn | None,
) -> None:
    if log is None:
        return

    detail = _format_process_failure_detail(result)
    if detail:
        log(
            "INFO",
            f"Gemini CLI failure detail for attempt {attempt_number}/{max_attempts}:\n{detail}",
        )


def _extract_gemini_rate_limit_events(
    result: _ProcessResult,
) -> list[_GeminiRateLimitEvent]:
    text = _gemini_failure_text(result)
    if not _text_has_rate_limit_markers(text):
        return []

    attempt_matches = list(
        re.finditer(r"Attempt\s+(\d+)\s+failed\s+with\s+status\s+429", text)
    )
    if not attempt_matches:
        return [_build_gemini_rate_limit_event(text=text, cli_attempt=None)]

    events: list[_GeminiRateLimitEvent] = []
    for index, match in enumerate(attempt_matches):
        block_end = (
            attempt_matches[index + 1].start()
            if index + 1 < len(attempt_matches)
            else len(text)
        )
        events.append(
            _build_gemini_rate_limit_event(
                text=text[match.start() : block_end],
                cli_attempt=match.group(1),
            )
        )
    return events


def _extract_ready_gemini_rate_limit_blocks(text: str) -> list[_GeminiRateLimitBlock]:
    return _extract_gemini_rate_limit_blocks(
        text,
        last_block_policy="emit_when_metadata_complete",
    )


def _extract_completed_gemini_rate_limit_blocks(
    text: str,
    *,
    include_last_block: bool,
) -> list[_GeminiRateLimitBlock]:
    return _extract_gemini_rate_limit_blocks(
        text,
        last_block_policy=(
            "emit_when_metadata_complete" if include_last_block else "exclude_last"
        ),
    )


def _extract_gemini_rate_limit_blocks(
    text: str,
    *,
    last_block_policy: RateLimitLastBlockPolicy,
) -> list[_GeminiRateLimitBlock]:
    if not _text_has_rate_limit_markers(text):
        return []

    attempt_matches = list(
        re.finditer(r"Attempt\s+(\d+)\s+failed\s+with\s+status\s+429", text)
    )
    if not attempt_matches:
        event = _build_gemini_rate_limit_event(text=text, cli_attempt=None)
        if (
            last_block_policy == "emit_when_metadata_complete"
            and _event_has_full_metadata(event)
        ):
            return [_GeminiRateLimitBlock(start=-1, text=text, event=event)]
        return []

    blocks: list[_GeminiRateLimitBlock] = []
    for index, match in enumerate(attempt_matches):
        block_end = (
            attempt_matches[index + 1].start()
            if index + 1 < len(attempt_matches)
            else len(text)
        )
        block_text = text[match.start() : block_end]
        event = _build_gemini_rate_limit_event(
            text=block_text,
            cli_attempt=match.group(1),
        )
        is_last_block = index + 1 == len(attempt_matches)
        is_complete = not is_last_block or (
            last_block_policy == "emit_when_metadata_complete"
            and _event_has_full_metadata(event)
        )
        if is_complete:
            blocks.append(
                _GeminiRateLimitBlock(
                    start=match.start(),
                    text=block_text,
                    event=event,
                )
            )
    return blocks


def _build_gemini_rate_limit_event(
    *,
    text: str,
    cli_attempt: str | None,
) -> _GeminiRateLimitEvent:
    metadata: dict[str, str] = {}
    status = _last_match(text, r'"status"\s*:\s*"([^"]+)"')
    if status:
        metadata["status"] = status

    reason_matches = re.findall(r'"reason"\s*:\s*"([^"]+)"', text)
    if reason_matches:
        preferred_reason = next(
            (
                candidate
                for candidate in reversed(reason_matches)
                if candidate.upper() == candidate and "_" in candidate
            ),
            reason_matches[-1],
        )
        metadata["reason"] = preferred_reason

    model = _last_match(text, r'"model"\s*:\s*"([^"]+)"')
    if model:
        metadata["model"] = model
    return _GeminiRateLimitEvent(cli_attempt=cli_attempt, metadata=metadata)


def _event_has_full_metadata(event: _GeminiRateLimitEvent) -> bool:
    return all(event.metadata.get(field) for field in ("status", "reason", "model"))


def _log_live_gemini_rate_limit_event(
    *,
    event: _GeminiRateLimitEvent,
    attempt_number: int,
    max_attempts: int,
    log: LogFn | None,
) -> None:
    if log is None:
        return

    detail_suffix = _format_gemini_rate_limit_metadata(event.metadata)
    if event.cli_attempt is not None:
        log(
            "WARN",
            "Gemini CLI emitted an internal 429 on CLI attempt "
            f"{event.cli_attempt}{detail_suffix} during dispatch attempt "
            f"{attempt_number}/{max_attempts}; Gemini CLI is still retrying. "
            "Completed retry blocks are logged to the dispatch log as they finish.",
        )
        return

    log(
        "WARN",
        f"Gemini CLI emitted a 429{detail_suffix} during dispatch attempt "
        f"{attempt_number}/{max_attempts}; Gemini CLI is still running. "
        "Completed retry blocks are logged to the dispatch log as they finish.",
    )


def _log_gemini_rate_limit_block_detail(
    *,
    block: _GeminiRateLimitBlock,
    stream_name: str,
    attempt_number: int,
    max_attempts: int,
    log: LogFn | None,
) -> None:
    if log is None:
        return

    if block.event.cli_attempt is not None:
        label = (
            "Gemini CLI internal 429 detail for CLI attempt "
            f"{block.event.cli_attempt} during dispatch attempt "
            f"{attempt_number}/{max_attempts} ({stream_name})"
        )
    else:
        label = (
            "Gemini CLI 429 detail during dispatch attempt "
            f"{attempt_number}/{max_attempts} ({stream_name})"
        )
    log("INFO", f"{label}:\n{block.text.rstrip()}")


def _log_gemini_rate_limit_events(
    *,
    result: _ProcessResult,
    events: list[_GeminiRateLimitEvent],
    attempt_number: int,
    max_attempts: int,
    command_succeeded: bool,
    log: LogFn | None,
    already_emitted_count: int = 0,
    detail_logged: bool = False,
) -> None:
    if log is None:
        return

    detail = _format_gemini_rate_limit_detail(result)
    if detail and not detail_logged:
        label = (
            "Gemini CLI internal 429 detail"
            if command_succeeded
            else "Gemini CLI failure detail"
        )
        log(
            "INFO",
            f"{label} for attempt {attempt_number}/{max_attempts}:\n{detail}",
        )

    remaining_events = events[min(already_emitted_count, len(events)) :]
    dispatch_suffix = (
        "retrying after backoff"
        if attempt_number < max_attempts
        else "no retries remain"
    )
    for event in remaining_events:
        detail_suffix = _format_gemini_rate_limit_metadata(event.metadata)
        if command_succeeded:
            if event.cli_attempt is not None:
                log(
                    "WARN",
                    "Gemini CLI emitted an internal 429 on CLI attempt "
                    f"{event.cli_attempt}{detail_suffix}; command eventually completed successfully. Full retry output captured in the dispatch log.",
                )
            else:
                log(
                    "WARN",
                    "Gemini CLI emitted one or more internal 429s"
                    f"{detail_suffix} before completing successfully. Full retry output captured in the dispatch log.",
                )
            continue

        if event.cli_attempt is not None:
            log(
                "WARN",
                "Gemini CLI emitted a 429 on CLI attempt "
                f"{event.cli_attempt}{detail_suffix} during dispatch attempt {attempt_number}/{max_attempts}; "
                f"{dispatch_suffix}. Full stderr captured in the dispatch log.",
            )
            continue

        log(
            "WARN",
            f"Gemini CLI emitted a 429 on attempt {attempt_number}/{max_attempts}{detail_suffix}; {dispatch_suffix}. Full stderr captured in the dispatch log.",
        )


def _format_gemini_rate_limit_metadata(metadata: dict[str, str]) -> str:
    if not metadata:
        return ""
    fields = []
    for key in ("status", "reason", "model"):
        value = metadata.get(key)
        if value:
            fields.append(f"{key}={value}")
    if not fields:
        return ""
    return f" ({', '.join(fields)})"


def _format_gemini_rate_limit_detail(result: _ProcessResult) -> str:
    parts: list[str] = []
    if _text_has_rate_limit_markers(result.stderr):
        parts.append(f"stderr:\n{result.stderr.strip()}")
    if _text_has_rate_limit_markers(result.stdout):
        parts.append(f"stdout:\n{result.stdout.strip()}")
    if parts:
        return "\n\n".join(parts)
    return _format_process_failure_detail(result)


def _format_process_failure_detail(result: _ProcessResult) -> str:
    parts: list[str] = []
    if result.stderr.strip():
        parts.append(f"stderr:\n{result.stderr.strip()}")
    if result.stdout.strip():
        parts.append(f"stdout:\n{result.stdout.strip()}")
    return "\n\n".join(parts)


def _gemini_failure_text(result: _ProcessResult) -> str:
    return "\n".join(
        part.strip() for part in (result.stderr, result.stdout) if part.strip()
    )


def _last_match(text: str, pattern: str) -> str | None:
    matches = re.findall(pattern, text)
    if not matches:
        return None
    return matches[-1]


def _text_has_rate_limit_markers(text: str) -> bool:
    normalized = text.lower()
    markers = (
        "status 429",
        '"code": 429',
        '"code":429',
        "ratelimitexceeded",
        "resource_exhausted",
        "model_capacity_exhausted",
    )
    return any(marker in normalized for marker in markers)


def _run_logged_agent_command(
    agent_name: str,
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None = None,
    log: LogFn | None = None,
    stream_all_stdout: bool = False,
    stderr_mode: StderrMode = "warn",
) -> _ProcessResult:
    if log is None:
        return _run_command_streaming(
            command,
            cwd=cwd,
            timeout_ms=timeout_ms,
            env=env,
            on_stdout=None,
            on_stderr=None,
        )

    monitor = _LiveAgentOutputMonitor(
        agent_name=agent_name,
        log=log,
        stream_all_stdout=stream_all_stdout,
        stderr_mode=stderr_mode,
    )
    return _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        env=env,
        on_stdout=lambda chunk: monitor.consume("stdout", chunk),
        on_stderr=lambda chunk: monitor.consume("stderr", chunk),
    )


def _run_gemini_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None,
    attempt_number: int,
    max_attempts: int,
    log: LogFn | None,
) -> _ProcessResult:
    if _command_requests_gemini_stream_json(command):
        result = _run_gemini_stream_json_command(
            command,
            cwd=cwd,
            timeout_ms=timeout_ms,
            env=env,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            log=log,
        )
        # The stream-json monitor rewrites stdout to assistant text but leaves
        # stderr intact so unsupported-output fallback detection can still see
        # Gemini CLI errors emitted during structured startup.
        if _gemini_stream_json_unsupported(result):
            if log is not None:
                log(
                    "WARN",
                    "Gemini stream-json output is not supported by the installed Gemini CLI. Falling back to mixed text mode; upgrade Gemini CLI to restore structured dispatch streaming.",
                )
            return _run_gemini_plain_text_command(
                _strip_gemini_stream_json_args(command),
                cwd=cwd,
                timeout_ms=timeout_ms,
                env=env,
                attempt_number=attempt_number,
                max_attempts=max_attempts,
                log=log,
            )
        return result

    return _run_gemini_plain_text_command(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        env=env,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        log=log,
    )


def _run_gemini_stream_json_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None,
    attempt_number: int,
    max_attempts: int,
    log: LogFn | None,
) -> _ProcessResult:
    monitor = _GeminiStreamJsonMonitor(
        agent_name=_gemini_agent_name_from_command(command),
        log=log,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
    )
    result = _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        env=env,
        on_stdout=monitor.consume_stdout,
        on_stderr=monitor.consume_stderr,
    )
    return monitor.finalize(result)


def _run_gemini_plain_text_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None,
    attempt_number: int,
    max_attempts: int,
    log: LogFn | None,
) -> _ProcessResult:
    monitor = _GeminiLiveRateLimitMonitor(
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        log=log,
    )
    result = _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        env=env,
        on_stdout=lambda chunk: monitor.consume("stdout", chunk),
        on_stderr=lambda chunk: monitor.consume("stderr", chunk),
    )
    monitor.finalize(result, command_succeeded=result.exit_code == 0)
    return result


def _command_requests_gemini_stream_json(command: list[str]) -> bool:
    for index, arg in enumerate(command):
        if arg != "--output-format":
            continue
        if index + 1 < len(command) and command[index + 1] == "stream-json":
            return True
    return False


def _gemini_agent_name_from_command(command: list[str]) -> str:
    command_text = " ".join(command)
    # The configured Gemini mentions are intentionally non-overlapping; simple
    # substring checks keep dispatch logging cheap and deterministic.
    if config.GEMINI_PE_MENTION in command_text:
        return "Gemini PE"
    if config.GEMINI_FRONTEND_MENTION in command_text:
        return "Gemini Frontend"
    return "Gemini CLI"


def _strip_gemini_stream_json_args(command: list[str]) -> list[str]:
    stripped_command: list[str] = []
    skip_next = False
    for index, arg in enumerate(command):
        if skip_next:
            skip_next = False
            continue
        if arg == "--output-format" and index + 1 < len(command):
            skip_next = True
            continue
        stripped_command.append(arg)
    return stripped_command


def _filter_live_agent_stdout_line(
    line: str,
    *,
    state: _LiveAgentStreamState,
    stream_all_stdout: bool = False,
) -> str | None:
    normalized_line = _trim_trailing_diff_text(line.strip())
    if not normalized_line:
        return None

    if state.in_diff_block:
        if _should_emit_live_agent_stdout_line(normalized_line):
            state.in_diff_block = False
            return normalized_line
        if not _looks_like_diff_line(normalized_line):
            state.in_diff_block = False
        return None

    if _looks_like_diff_line(normalized_line):
        state.in_diff_block = True
        return None

    if stream_all_stdout:
        return normalized_line

    if _should_emit_live_agent_stdout_line(normalized_line):
        return normalized_line
    return None


def _filter_codex_stderr_line(
    line: str,
    *,
    state: _LiveAgentStreamState,
) -> tuple[Literal["AGENT", "WARN"], str] | None:
    stripped = _trim_trailing_diff_text(line.strip())
    if not stripped:
        return None

    if stripped == "user" or stripped in _LIVE_AGENT_TOOL_LABELS:
        state.awaiting_tool_descriptor = stripped not in {"codex", "user"}
        state.suppress_tool_output = False
        return ("AGENT", stripped)

    if state.awaiting_tool_descriptor:
        state.awaiting_tool_descriptor = False
        state.suppress_tool_output = True
        return ("AGENT", stripped)

    if state.suppress_tool_output:
        if _COMMAND_RESULT_RE.search(stripped):
            state.suppress_tool_output = False
            return ("AGENT", stripped)
        return None

    if _CODEX_STDERR_METADATA_RE.match(stripped):
        return ("AGENT", stripped)

    if _CODEX_STDERR_ERROR_RE.match(stripped):
        return ("WARN", stripped)

    filtered_line = _filter_live_agent_stdout_line(
        stripped,
        state=state,
        stream_all_stdout=False,
    )
    if filtered_line is not None:
        return ("AGENT", filtered_line)

    return None


def _filter_codex_json_stderr_line(
    line: str,
    *,
    state: _LiveAgentStreamState,
) -> tuple[Literal["AGENT", "WARN"], str] | None:
    stripped = _trim_trailing_diff_text(line.strip())
    if not stripped:
        return None

    filtered_line = _filter_live_agent_stdout_line(
        stripped,
        state=state,
        stream_all_stdout=True,
    )
    if filtered_line is None:
        return None

    if _CODEX_STDERR_ERROR_RE.match(filtered_line):
        return ("WARN", filtered_line)
    return ("AGENT", filtered_line)


def _trim_trailing_diff_text(line: str) -> str:
    diff_index = line.find(" diff --git ")
    if diff_index == -1:
        return line
    return line[:diff_index].rstrip()


def _looks_like_diff_line(line: str) -> bool:
    stripped = line.strip()
    if _DIFF_BLOCK_START_RE.match(stripped):
        return True
    return _DIFF_BLOCK_LINE_RE.match(stripped) is not None


def _should_emit_live_agent_stdout_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.lower() in _LIVE_AGENT_TOOL_LABELS:
        return True
    if _COMMAND_LINE_RE.match(stripped):
        return True
    if _COMMAND_RESULT_RE.search(stripped):
        return True
    if _STRUCTURED_STATUS_RE.match(stripped):
        return True
    if _MARKDOWN_STATUS_RE.match(stripped):
        return True
    return _PROSE_LINE_RE.match(stripped) is not None


def _run_command_streaming(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> _ProcessResult:
    resolved_command = list(command)
    resolved_command[0] = _resolve_command_binary(resolved_command[0])
    popen_kwargs = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if env is not None:
        popen_kwargs["env"] = env

    try:
        process = subprocess.Popen(resolved_command, **popen_kwargs)
    except OSError as exc:
        return _ProcessResult(stdout="", stderr=str(exc), exit_code=1)

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stream_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    readers = [
        threading.Thread(
            target=_read_process_stream,
            args=(process.stdout, "stdout", stream_queue),
            daemon=True,
        ),
        threading.Thread(
            target=_read_process_stream,
            args=(process.stderr, "stderr", stream_queue),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + (timeout_ms / 1000)
    timed_out = False
    while process.poll() is None and time.monotonic() < deadline:
        _drain_stream_queue(
            stream_queue,
            stdout_parts=stdout_parts,
            stderr_parts=stderr_parts,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            timeout=0.1,
        )

    if process.poll() is None:
        timed_out = True
        process.kill()

    process.wait()
    for reader in readers:
        reader.join()
    _drain_stream_queue(
        stream_queue,
        stdout_parts=stdout_parts,
        stderr_parts=stderr_parts,
        on_stdout=on_stdout,
        on_stderr=on_stderr,
        timeout=0.0,
        drain_all=True,
    )

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    if timed_out:
        message = f"Process timed out after {timeout_ms / 1000:g} seconds."
        if stderr:
            message = f"{message}\n{stderr}"
        return _ProcessResult(stdout=stdout, stderr=message, exit_code=1)

    return _ProcessResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=int(process.returncode),
    )


def _read_process_stream(
    stream: TextIO | None,
    stream_name: str,
    stream_queue: queue.Queue[tuple[str, str | None]],
) -> None:
    if stream is None:
        return

    try:
        for chunk in iter(stream.readline, ""):
            stream_queue.put((stream_name, chunk))
    finally:
        stream.close()


def _drain_stream_queue(
    stream_queue: queue.Queue[tuple[str, str | None]],
    *,
    stdout_parts: list[str],
    stderr_parts: list[str],
    on_stdout: Callable[[str], None] | None,
    on_stderr: Callable[[str], None] | None,
    timeout: float,
    drain_all: bool = False,
) -> None:
    while True:
        try:
            if drain_all:
                stream_name, chunk = stream_queue.get_nowait()
            else:
                stream_name, chunk = stream_queue.get(timeout=timeout)
        except queue.Empty:
            return

        if chunk is None:
            continue

        if stream_name == "stdout":
            stdout_parts.append(chunk)
            if on_stdout is not None:
                on_stdout(chunk)
        else:
            stderr_parts.append(chunk)
            if on_stderr is not None:
                on_stderr(chunk)

        if not drain_all:
            return


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None = None,
) -> _ProcessResult:
    resolved_command = list(command)
    resolved_command[0] = _resolve_command_binary(resolved_command[0])
    run_kwargs = {
        "cwd": cwd,
        "capture_output": True,
        "text": True,
        "timeout": timeout_ms / 1000,
    }
    if env is not None:
        run_kwargs["env"] = env

    try:
        completed = subprocess.run(resolved_command, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_output(getattr(exc, "output", None))
        stderr = _coerce_output(getattr(exc, "stderr", None))
        message = f"Process timed out after {timeout_ms / 1000:g} seconds."
        if stderr:
            message = f"{message}\n{stderr}"
        return _ProcessResult(stdout=stdout, stderr=message, exit_code=1)
    except OSError as exc:
        return _ProcessResult(stdout="", stderr=str(exc), exit_code=1)

    return _ProcessResult(
        stdout=_coerce_output(completed.stdout),
        stderr=_coerce_output(completed.stderr),
        exit_code=int(completed.returncode),
    )


def _coerce_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _resolve_command_binary(command: str) -> str:
    if not command:
        return command

    if Path(command).is_absolute() or any(sep in command for sep in ("\\", "/")):
        return command

    resolved = shutil.which(command)
    if resolved:
        return resolved

    path = Path(command)
    if path.suffix.lower() == ".cmd":
        resolved_without_cmd = shutil.which(path.stem)
        if resolved_without_cmd:
            return resolved_without_cmd

    return command


def _wait_for_manual_continue() -> None:
    if os.name == "nt":
        try:
            import msvcrt

            msvcrt.getwch()
            return
        except (ImportError, OSError):
            pass

    if sys.stdin is None:
        return

    if os.name != "nt" and sys.stdin.isatty():
        try:
            import termios
            import tty

            file_descriptor = sys.stdin.fileno()
            original_settings = termios.tcgetattr(file_descriptor)
            try:
                tty.setraw(file_descriptor)
                sys.stdin.read(1)
            finally:
                termios.tcsetattr(
                    file_descriptor,
                    termios.TCSADRAIN,
                    original_settings,
                )
            return
        except (AttributeError, OSError, ValueError):
            pass

    try:
        input()
    except EOFError:
        return

