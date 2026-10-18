"""Antigravity CLI (`agy`) provider adapter.

Launches ``agy`` in print mode with stream-json output, logs its progress,
and maps the final ``result`` event onto a ``DispatchResult``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time

from llm_handoff import config
from llm_handoff.agent_process import (
    build_agent_env,
    _resolve_handoff_path,
    _run_command_streaming,
)
from llm_handoff.agent_types import DispatchResult, LogFn, _ProcessResult
from llm_handoff.provider_auth import (
    antigravity_auth_failure_line,
    find_antigravity_auth_failure,
    find_auth_failure,
    find_permission_soft_deny,
    permission_soft_deny_message,
)
from llm_handoff.usage_log import format_usage_line


ANTIGRAVITY_SUCCESS_STATUS = "SUCCESS"
ANTIGRAVITY_UNSAFE_BYPASS_ARGS: tuple[str, ...] = ("--dangerously-skip-permissions",)
# Marker on the stderr and log line written for each deny-rule refusal.
ANTIGRAVITY_DENY_RULE_MARKER = "Antigravity deny rule refused a command"
# Fixed text agy puts in a tool error when a permissions.deny entry matched.
# The rule text before it varies (for example ``command(regex:...)``), so
# detection keys on this text only.
_DENY_RULE_ERROR_TEXT = "Matches user-configured deny rule"
# Replacement text for credentials removed from a logged refusal.
_REDACTED = "[REDACTED]"
_SECRET_NAME = (
    r"[A-Za-z0-9_.-]*(?:token|secret|password|passwd|api[_-]?key|apikey"
    r"|access[_-]?key|private[_-]?key|credentials?|client[_-]?secret)"
    r"[A-Za-z0-9_.-]*"
)
_SECRET_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)"
# Each pattern keeps group 1 and replaces the rest of the match with
# _REDACTED. A refused command can carry credentials (for example
# ``git push https://user:TOKEN@host/repo.git``), and agy repeats the
# command in its error message, so both fields go through these patterns.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # URL userinfo: scheme://user:pass@host or scheme://token@host.
    re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@\"']+(?=@)"),
    # Authorization headers, with an optional scheme word.
    re.compile(
        r"(?i)(authorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?)"
        + _SECRET_VALUE
    ),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+"),
    # NAME=value where NAME names a secret (env vars, --password=...).
    re.compile(r"(?i)(\b" + _SECRET_NAME + r"\s*=\s*)" + _SECRET_VALUE),
    # --token VALUE style flags; a following flag is not taken as the value.
    re.compile(
        r"(?i)((?<![\w-])--?" + _SECRET_NAME + r"\s+)(?!-)" + _SECRET_VALUE
    ),
    # Well-known token prefixes that appear without a name.
    re.compile(
        r"()\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|glpat-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
        r"|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})"
    ),
)
# step_type values whose text_delta is not the agent's own reply.
_NON_RESPONSE_STEP_TYPES = frozenset({"user_input", "tool", "checkpoint"})


def antigravity_permission_args(*, unsafe_bypass: bool = False) -> list[str]:
    """Return the agy permission arguments for one role launch.

    The default passes no permission flag: workspace reads and writes are
    allowed in print mode, and shell commands need ``permissions.allow``
    grants in the agy user settings. ``unsafe_bypass`` approves everything.
    """
    if unsafe_bypass:
        return list(ANTIGRAVITY_UNSAFE_BYPASS_ARGS)
    return []


def invoke_antigravity(
    role: str,
    handoff_path: Path,
    *,
    agent_name: str | None = None,
    binary: str | None = None,
    model: str | None = None,
    timeout_ms: int | None = None,
    additional_instruction: str | None = None,
    support_prompt: str | None = None,
    subagent_name: str | None = None,
    use_resume: bool = False,
    session_id: str | None = None,
    previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    log: LogFn | None = None,
    repo_root: Path | None = None,
    agent_config: config.AgentConfig | None = None,
) -> DispatchResult:
    repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
    resolved_handoff_path = _resolve_handoff_path(Path(handoff_path), repo_root)
    resolved_agent_config = agent_config or config.AgentConfig(provider="antigravity")
    resolved_agent_name = agent_name or _antigravity_role_label(role)
    resolved_timeout_ms = timeout_ms or config.AGENT_TIMEOUT_MS
    agy_agent = resolved_agent_config.agent_name or subagent_name or str(role)
    should_resume = use_resume and bool(session_id)
    env = build_agent_env(resolved_agent_config)

    def build(resume_session_id: str | None) -> list[str]:
        return _build_antigravity_command(
            binary=binary,
            prompt=_build_antigravity_prompt(
                role,
                resolved_handoff_path,
                support_prompt=support_prompt,
                additional_instruction=additional_instruction,
                use_resume=resume_session_id is not None,
                previous_handoff_sha=previous_handoff_sha,
                current_handoff_sha=current_handoff_sha,
            ),
            agent=agy_agent,
            timeout_ms=resolved_timeout_ms,
            model=model,
            resume_session_id=resume_session_id,
            unsafe_bypass=resolved_agent_config.unsafe_bypass,
        )

    def run(command: list[str]) -> _ProcessResult:
        return _run_antigravity_command(
            command,
            cwd=repo_root,
            timeout_ms=resolved_timeout_ms,
            idle_timeout_ms=resolved_agent_config.idle_timeout_ms,
            env=env,
            agent_name=resolved_agent_name,
            log=log,
        )

    start_time = time.monotonic()
    result = run(build(session_id if should_resume else None))
    session_invalidated = False

    if (
        should_resume
        and result.exit_code != 0
        and find_permission_soft_deny(result.stderr) is None
        and find_auth_failure(result.stderr) is None
        and ANTIGRAVITY_DENY_RULE_MARKER not in result.stderr
    ):
        # A soft-denied, deny-rule-refused, or signed-out run would fail the
        # same way in a fresh conversation, so only other resume failures get
        # the one fresh rerun.
        session_invalidated = True
        if log is not None:
            role_phrase = str(role).strip().lower() or "agent"
            log(
                "WARN",
                f"Antigravity {role_phrase} conversation {session_id} failed to "
                "resume (exit code "
                f"{result.exit_code}). Starting a fresh {role_phrase} "
                "conversation for this dispatch.",
            )
        result = run(build(None))

    result_session_id = result.session_id
    if (
        result_session_id is None
        and should_resume
        and not session_invalidated
        and result.exit_code == 0
    ):
        result_session_id = session_id

    return DispatchResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=time.monotonic() - start_time,
        session_id=result_session_id,
        session_invalidated=session_invalidated,
        usage=result.usage,
    )


def _build_antigravity_command(
    *,
    binary: str | None,
    prompt: str,
    agent: str,
    timeout_ms: int,
    model: str | None,
    resume_session_id: str | None,
    unsafe_bypass: bool,
) -> list[str]:
    command = [
        binary or config.ANTIGRAVITY_BINARY,
        "-p",
        prompt,
        "--agent",
        agent,
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{max(1, timeout_ms // 1000)}s",
    ]
    if model:
        command.extend(["--model", model])
    if resume_session_id:
        command.extend(["--conversation", resume_session_id])
    command.extend(antigravity_permission_args(unsafe_bypass=unsafe_bypass))
    return command


def _build_antigravity_prompt(
    role: str,
    resolved_handoff_path: Path,
    *,
    support_prompt: str | None,
    additional_instruction: str | None,
    use_resume: bool,
    previous_handoff_sha: str | None,
    current_handoff_sha: str | None,
) -> str:
    if support_prompt:
        prompt = support_prompt.strip()
    elif use_resume:
        prompt = (
            "Continue your assigned task based on the current handoff state. "
            "The HANDOFF.md SHA at your prior turn was "
            f"{previous_handoff_sha or 'unknown'}; the current SHA is "
            f"{current_handoff_sha or 'unknown'}. If the SHA has changed, use "
            f"your tools to re-read the latest state of {resolved_handoff_path}. "
            "Treat HANDOFF.md and repo source-of-truth documents as "
            "authoritative over prior memory."
        )
    else:
        prompt = (
            f"Act as the {role} role and execute the work described in "
            f"{resolved_handoff_path}. Read the repository instructions and "
            "relevant state files before acting. Follow HANDOFF.md as the live "
            "task file and update it when you finish."
        )
    if additional_instruction and additional_instruction.strip():
        prompt = f"{prompt} Additional instruction: {additional_instruction.strip()}"
    return prompt


def _antigravity_role_label(role: str) -> str:
    normalized_role = str(role).strip()
    if not normalized_role:
        return "Antigravity"
    return f"Antigravity {normalized_role}"


@dataclass
class _AntigravityStreamJsonMonitor:
    agent_name: str
    log: LogFn | None
    session_id: str | None = None
    result_event: dict[str, object] | None = None
    text_deltas: list[str] = field(default_factory=list)
    last_step_usage: object = None
    received_stdout: bool = False
    _line_buffer: str = ""
    _response_line_buffer: str = ""
    _logged_tool_steps: set[object] = field(default_factory=set)
    deny_rule_hits: list[tuple[str, str]] = field(default_factory=list)
    _deny_rule_steps: set[object] = field(default_factory=set)

    def consume_stdout(self, chunk: str) -> None:
        if not chunk:
            return
        self.received_stdout = True
        self._line_buffer += chunk
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            self._consume_line(line)

    def consume_stderr(self, chunk: str) -> None:
        # stderr is kept in the process result; nothing to parse live.
        del chunk

    def finalize(self, result: _ProcessResult) -> _ProcessResult:
        if self._line_buffer:
            line, self._line_buffer = self._line_buffer, ""
            self._consume_line(line)
        self._flush_response_lines(flush_partial=True)
        usage_fields = self._usage_fields()
        self._log_usage(usage_fields)

        stdout = self._final_stdout(result.stdout)
        stderr = result.stderr
        exit_code = result.exit_code
        if self.result_event is not None:
            status = str(self.result_event.get("status") or "").strip().upper()
            if status != ANTIGRAVITY_SUCCESS_STATUS:
                exit_code = exit_code or 1
                stderr = _append_line(
                    stderr,
                    f"Antigravity result status={status or 'UNKNOWN'}: "
                    f"{_format_error(self.result_event.get('error'))}",
                )
        elif exit_code == 0:
            exit_code = 1
            stderr = _append_line(
                stderr,
                "Antigravity stream-json output had no result event; the run "
                "did not report a final status.",
            )

        if self.deny_rule_hits:
            # agy reports SUCCESS after a deny rule refuses a command, even
            # when the role recovers; the refused action was still not done.
            exit_code = exit_code or 1
            for command_line, message in self.deny_rule_hits:
                line = f"{ANTIGRAVITY_DENY_RULE_MARKER}: {command_line} ({message})"
                stderr = _append_line(stderr, line)
                self._emit("ERROR", f"{self.agent_name}: {line}")

        soft_deny = find_permission_soft_deny(result.stderr)
        if soft_deny is not None:
            # agy exits 0 after auto-denying a tool, but the work was not done.
            exit_code = exit_code or 1
            message = permission_soft_deny_message(soft_deny)
            stderr = _append_line(stderr, message)
            self._emit("ERROR", f"{self.agent_name}: {message}")

        if exit_code != 0:
            sign_in_failure = find_antigravity_auth_failure(result.stderr)
            if sign_in_failure is not None:
                # Restate agy's sign-in error with a shared marker so the
                # orchestrator and failover recognize it without provider context.
                stderr = _append_line(
                    stderr, antigravity_auth_failure_line(sign_in_failure)
                )

        result_session = (
            self.result_event.get("conversation_id")
            if self.result_event is not None
            else None
        )
        session_id = (
            result_session
            if isinstance(result_session, str) and result_session
            else self.session_id
        )
        return _ProcessResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            session_id=session_id,
            usage=dict(usage_fields) if usage_fields else None,
        )

    def _final_stdout(self, raw_stdout: str) -> str:
        if self.result_event is not None:
            response = self.result_event.get("response")
            if isinstance(response, str) and response.strip():
                return response
        joined = "".join(self.text_deltas).strip()
        return joined or raw_stdout

    def _consume_line(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = None
        if not isinstance(event, dict):
            self._emit(
                "WARN",
                f"{self.agent_name}: ignored non-JSON output line: {line[:120]}",
            )
            return
        event_type, payload = _unwrap_stream_event(event)
        if event_type == "init":
            self._handle_init(payload)
        elif event_type == "step_update":
            self._handle_step_update(payload)
        elif event_type == "result":
            self._handle_result(payload)

    def _handle_init(self, event: dict[str, object]) -> None:
        summary = "Antigravity session started"
        permission_mode = event.get("permission_mode")
        if isinstance(permission_mode, str) and permission_mode:
            summary = f"{summary} (permission mode: {permission_mode})"
        self._emit("AGENT", f"{self.agent_name}: {summary}")

    def _handle_step_update(self, event: dict[str, object]) -> None:
        conversation_id = event.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            self.session_id = conversation_id
        usage = event.get("usage")
        if usage is not None:
            self.last_step_usage = usage

        step_type = event.get("step_type")
        if step_type == "tool":
            self._record_deny_rule_hit(event)
            tool_name = event.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                step_key = event.get("step_index", object())
                if step_key not in self._logged_tool_steps:
                    self._logged_tool_steps.add(step_key)
                    self._emit("AGENT", f"{self.agent_name}: Using tool: {tool_name}")
            return
        if step_type in _NON_RESPONSE_STEP_TYPES:
            return
        text_delta = event.get("text_delta")
        if isinstance(text_delta, str) and text_delta:
            self.text_deltas.append(text_delta)
            self._response_line_buffer += text_delta
            self._flush_response_lines()

    def _record_deny_rule_hit(self, event: dict[str, object]) -> None:
        if event.get("state") != "ERROR":
            return
        tool_info = event.get("tool_info")
        if not isinstance(tool_info, dict):
            return
        error = tool_info.get("error")
        if not isinstance(error, dict):
            return
        message = error.get("message")
        if not isinstance(message, str) or _DENY_RULE_ERROR_TEXT not in message:
            return
        step_key = event.get("step_index", object())
        if step_key in self._deny_rule_steps:
            return
        self._deny_rule_steps.add(step_key)
        parameters = tool_info.get("parameters")
        command_line = (
            parameters.get("CommandLine") if isinstance(parameters, dict) else None
        )
        if not isinstance(command_line, str) or not command_line.strip():
            command_line = "unknown command"
        # Redact before storing: these fields reach stderr and the dispatch log.
        self.deny_rule_hits.append(
            (
                redact_credentials(command_line.strip()),
                redact_credentials(message.strip()),
            )
        )

    def _handle_result(self, event: dict[str, object]) -> None:
        self.result_event = event
        status = str(event.get("status") or "unknown")
        self._flush_response_lines(flush_partial=True)
        self._emit("AGENT", f"{self.agent_name}: Antigravity result (status={status})")

    def _usage_fields(self) -> dict[str, object]:
        usage = (
            self.result_event.get("usage") if self.result_event is not None else None
        )
        if usage is None:
            usage = self.last_step_usage
        return _antigravity_usage_fields(usage)

    def _log_usage(self, fields: dict[str, object]) -> None:
        if fields:
            self._emit(
                "DEBUG",
                format_usage_line(self.agent_name, "antigravity", fields),
            )

    def _flush_response_lines(self, *, flush_partial: bool = False) -> None:
        while "\n" in self._response_line_buffer:
            line, self._response_line_buffer = self._response_line_buffer.split(
                "\n", 1
            )
            if line.rstrip():
                self._emit("AGENT", f"{self.agent_name}: {line.rstrip()}")
        if flush_partial:
            if self._response_line_buffer.rstrip():
                self._emit(
                    "AGENT",
                    f"{self.agent_name}: {self._response_line_buffer.rstrip()}",
                )
            self._response_line_buffer = ""

    def _emit(self, level: str, message: str) -> None:
        if self.log is not None:
            self.log(level, message)


def _unwrap_stream_event(event: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Return the event name and its payload for one stream-json line.

    agy names each event with a top-level ``event`` discriminator and nests
    the payload under the key it names, for example
    ``{"event": "result", "result": {...}}``. A flat object with a ``type``
    discriminator and inline fields is also accepted. Top-level fields
    outside the nested payload are kept, and the nested payload wins on a
    key collision.
    """
    discriminator = event.get("event")
    discriminator_key = "event"
    if not isinstance(discriminator, str) or not discriminator:
        discriminator = event.get("type")
        discriminator_key = "type"
    if not isinstance(discriminator, str) or not discriminator:
        return "", event
    nested = event.get(discriminator)
    payload = {key: value for key, value in event.items() if key != discriminator_key}
    if isinstance(nested, dict):
        del payload[discriminator]
        payload.update(nested)
    return discriminator, payload


def _antigravity_usage_fields(usage: object) -> dict[str, object]:
    if not isinstance(usage, dict):
        return {}
    fields: dict[str, object] = {}
    known = (
        ("input_tokens", ("input_tokens", "inputTokens")),
        ("output_tokens", ("output_tokens", "outputTokens")),
        ("total_tokens", ("total_tokens", "totalTokens")),
    )
    consumed: set[str] = set()
    for target, keys in known:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                fields[target] = value
                consumed.update(keys)
                break
    for key, value in usage.items():
        if key in consumed or not isinstance(key, str):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields[key] = value
    return fields


def _format_error(error: object) -> str:
    if isinstance(error, str) and error.strip():
        return error.strip()
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return json.dumps(error, sort_keys=True)
    if error:
        return str(error)
    return "no error detail reported"


def redact_credentials(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced.

    Covers URL userinfo, authorization headers, bearer tokens, secret-named
    ``NAME=value`` pairs and ``--flag value`` arguments, and common token
    prefixes. This is a best-effort filter for log lines, not a guarantee.
    """
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}{_REDACTED}", text)
    return text


def _append_line(text: str, line: str) -> str:
    if text and text.strip():
        return f"{text.rstrip()}\n{line}"
    return line


def _run_antigravity_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    idle_timeout_ms: int | None,
    env: dict[str, str] | None,
    agent_name: str,
    log: LogFn | None,
) -> _ProcessResult:
    monitor = _AntigravityStreamJsonMonitor(agent_name=agent_name, log=log)
    result = _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        idle_timeout_ms=idle_timeout_ms,
        env=env,
        on_stdout=monitor.consume_stdout,
        on_stderr=monitor.consume_stderr,
    )
    if not monitor.received_stdout and result.stdout:
        # A runner that did not stream live callbacks still returns the
        # captured stdout; parse it once here.
        monitor.consume_stdout(result.stdout)
    return monitor.finalize(result)
