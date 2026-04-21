from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import time
from typing import Callable, Literal

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from llm_handoff import config
from llm_handoff.agent_process import (
    _first_nonempty_line,
    _first_nonempty_value,
    _format_process_failure_detail,
    _resolve_handoff_path,
    _run_command_streaming,
)
from llm_handoff.agent_streams import _LiveAgentOutputMonitor
from llm_handoff.agent_types import DispatchResult, LogFn, _ProcessResult


GeminiRole = str
RateLimitLastBlockPolicy = Literal["emit_when_metadata_complete", "exclude_last"]
_REAL_TIME_MONOTONIC = time.monotonic


@dataclass(frozen=True)
class _GeminiRateLimitEvent:
    cli_attempt: str | None
    metadata: dict[str, str]


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
    agent_name: str = "Gemini CLI"
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
            agent_name=self.agent_name,
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


def invoke_gemini(
    role: GeminiRole,
    handoff_path: Path,
    *,
    mention: str | None = None,
    agent_name: str | None = None,
    binary: str | None = None,
    timeout_ms: int | None = None,
    max_retries: int | None = None,
    retry_base_seconds: float | None = None,
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
    should_resume = use_resume and bool(session_id)
    resolved_mention = mention or _default_gemini_mention(role)
    resolved_agent_name = agent_name or _gemini_role_label(role)
    command = _build_gemini_command(
        role,
        resolved_handoff_path,
        mention=resolved_mention,
        binary=binary,
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
        agent_name=resolved_agent_name,
        timeout_ms=timeout_ms,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
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
            role_phrase = str(role).strip().lower() or "agent"
            log(
                "WARN",
                f"Gemini {role_phrase} managed session "
                f"{session_id} could not be resumed. Clearing the in-memory session and starting a fresh Gemini {role_phrase} session for this dispatch.",
            )
        fresh_command = _build_gemini_command(
            role,
            resolved_handoff_path,
            mention=resolved_mention,
            binary=binary,
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
            agent_name=resolved_agent_name,
            timeout_ms=timeout_ms,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
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
    mention: str | None = None,
    binary: str | None = None,
    additional_instruction: str | None,
    resume_session_id: str | None,
    previous_handoff_sha: str | None,
    current_handoff_sha: str | None,
) -> list[str]:
    command = [binary or config.GEMINI_BINARY]
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
                mention=mention,
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
    mention: str | None = None,
    additional_instruction: str | None,
    use_resume: bool,
    previous_handoff_sha: str | None,
    current_handoff_sha: str | None,
) -> str:
    resolved_mention = mention or _default_gemini_mention(role)
    if use_resume:
        prompt = (
            f"{resolved_mention} Continue your assigned task based on the current handoff state. "
            "The HANDOFF.md SHA at your prior turn was "
            f"{previous_handoff_sha or 'unknown'}; the current SHA is "
            f"{current_handoff_sha or 'unknown'}. If the SHA has changed, use your tools "
            f"to re-read the latest state of {resolved_handoff_path}. "
            "Treat HANDOFF.md and repo source-of-truth documents as authoritative over prior memory."
        )
    else:
        prompt = (
            f"{resolved_mention} Execute your assigned task based on the provided handoff "
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
    agent_name: str = "Gemini CLI",
    timeout_ms: int | None = None,
    max_retries: int | None = None,
    retry_base_seconds: float | None = None,
    log: LogFn | None,
) -> _ProcessResult:
    max_attempts = (
        config.GEMINI_MAX_RETRIES if max_retries is None else max_retries
    ) + 1
    resolved_timeout_ms = timeout_ms or config.AGENT_TIMEOUT_MS
    env = _build_gemini_env(use_api_key_env=use_api_key_env)
    attempt_count = 0

    def run_attempt() -> _ProcessResult:
        nonlocal attempt_count
        attempt_count += 1
        result = _run_gemini_command(
            command,
            cwd=repo_root,
            timeout_ms=resolved_timeout_ms,
            env=env,
            attempt_number=attempt_count,
            max_attempts=max_attempts,
            agent_name=agent_name,
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
        wait=wait_exponential(
            multiplier=(
                config.GEMINI_RETRY_BASE_SECONDS
                if retry_base_seconds is None
                else retry_base_seconds
            )
        ),
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


def _run_gemini_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None,
    attempt_number: int,
    max_attempts: int,
    agent_name: str = "Gemini CLI",
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
            agent_name=agent_name,
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
                agent_name=agent_name,
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
        agent_name=agent_name,
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
    agent_name: str = "Gemini CLI",
    log: LogFn | None,
) -> _ProcessResult:
    monitor = _GeminiStreamJsonMonitor(
        agent_name=agent_name,
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
    agent_name: str = "Gemini CLI",
    log: LogFn | None,
) -> _ProcessResult:
    monitor = _GeminiLiveRateLimitMonitor(
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        agent_name=agent_name,
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
    if config.GEMINI_PLANNER_MENTION in command_text:
        return "Gemini planner"
    if config.GEMINI_FRONTEND_MENTION in command_text:
        return "Gemini Frontend"
    return "Gemini CLI"


def _default_gemini_mention(role: GeminiRole) -> str:
    normalized_role = str(role).strip().lower()
    if normalized_role == "planner":
        return config.GEMINI_PLANNER_MENTION
    if normalized_role == "frontend":
        return config.GEMINI_FRONTEND_MENTION
    return f"@{normalized_role or 'agent'}"


def _gemini_role_label(role: GeminiRole) -> str:
    normalized_role = str(role).strip()
    if not normalized_role:
        return "Gemini CLI"
    return f"Gemini {normalized_role}"


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
