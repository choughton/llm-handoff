"""Start or resume one chat side session for an operator message.

``run_chat_session`` launches a side session on the active role's provider
(``claude``, ``codex``, or ``antigravity``) in the repository root, with that
role's normal permission arguments, ``unsafe_bypass`` included. The session gets the
role's tools and permissions: this module adds no narrower tool allowlist,
no ``--disallowedTools`` of its own, no read-only sandbox, and no MCP or
hook restriction beyond what the role's own permission arguments carry. It
may edit files and ``HANDOFF.md`` to do what the operator asks.

The result names the files the session's own edit events reported:

- Claude: an ``Edit``, ``Write``, or ``MultiEdit`` (``input.file_path``) or
  ``NotebookEdit`` (``input.notebook_path``) tool call whose matching
  ``tool_result`` is not ``is_error: true``. A refused or failed edit, or one
  with no result, does not count.
- Codex: an ``item.completed`` event whose item has ``type: "file_change"``
  and a ``status`` other than ``"failed"`` adds each ``changes[].path``. This
  event shape is taken from the Codex JSON event stream as documented; it has
  not been checked against a live Codex CLI.
- Antigravity (``agy``): a ``step_update`` event with ``step_type: "tool"``
  for ``write_to_file``, ``replace_file_content``, or
  ``multi_replace_file_content`` adds ``tool_info.parameters.TargetFile`` once
  that step reaches ``state: "DONE"`` with no ``tool_info.error``. The path
  may arrive on an earlier event of the same ``step_index``. A step that
  ends in ``ERROR`` (a refused or failed edit), or never reaches ``DONE``,
  does not count. Basis: a live agy 1.4.2 stream-json capture of
  ``write_to_file`` and ``replace_file_content`` steps (replayed in
  ``tests/test_chat_side_session.py``). Each edit step emits
  ``tool_info.parameters.TargetFile`` as an absolute path and moves from
  ``state: "ACTIVE"`` to ``state: "DONE"`` with no ``tool_info.error`` on
  success, or to ``state: "ERROR"`` with ``tool_info.error`` (``type:
  "TOOL_ERROR"``) on failure. agy 1.4.2 did multi-chunk replacement through
  ``replace_file_content``; ``multi_replace_file_content`` was not seen live
  and stays in ``ANTIGRAVITY_EDIT_TOOLS`` for compatibility with agent files
  that list it.

Codex launches pass no ``--output-schema`` or ``--output-last-message`` and
this module never reads or writes anything under ``logs/dispatch/codex*/``,
so a chat never touches a role's saved Codex session.

Antigravity launches use the role dispatch command contract
(``_build_antigravity_command``): the configured binary, model, print
timeout, environment, and agent name (the role name when none is
configured), ``--conversation`` only to resume, and ``--mode accept-edits`` or
the ``unsafe_bypass`` argument. The answer text, conversation id, and the
result-status, missing-result, deny-rule, soft-deny, and sign-in failure
handling come from the role dispatch stream monitor. A chat launch never
retries a failed resume in a fresh conversation.

With ``resumed_role=True`` the session resumes a finished role's own session
(``@role`` chat) with a prompt that tells it which role it is and that the
dispatch loop has moved on; the launch arguments are the same.

``__main__._attach_console_chat`` wires in the chat answer worker that calls
this module (CHAT-32); CHAT-34 supplies the ``role_session`` lookup that
routes ``@role`` messages to a finished role's session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from llm_handoff import config
from llm_handoff.agent_process import _run_command_streaming, build_agent_env
from llm_handoff.agent_providers.antigravity import (
    _AntigravityStreamJsonMonitor,
    _build_antigravity_command,
    _unwrap_stream_event,
)
from llm_handoff.agent_providers.claude import (
    _ClaudeStreamJsonMonitor,
    _build_claude_env,
    claude_permission_args,
)
from llm_handoff.agent_providers.codex import (
    _CodexJsonMonitor,
    _build_codex_env,
    codex_permission_args,
    unwrap_codex_final_message,
)
from llm_handoff.redaction import redact_stderr_summary


SUPPORTED_CHAT_PROVIDERS: tuple[str, ...] = ("claude", "codex", "antigravity")

# agy edit tools (the write tools the shipped agy agent files grant) and the
# tool argument that names the edited file. write_to_file and
# replace_file_content were verified against a live agy 1.4.2 capture;
# multi_replace_file_content is kept for agent files that list it.
ANTIGRAVITY_EDIT_TOOLS: frozenset[str] = frozenset(
    {"write_to_file", "replace_file_content", "multi_replace_file_content"}
)
ANTIGRAVITY_EDIT_PATH_KEY = "TargetFile"
_ANTIGRAVITY_DONE_STATE = "DONE"
_ANTIGRAVITY_ERROR_STATE = "ERROR"

# Claude tool name -> the input key that names the edited file.
CLAUDE_EDIT_TOOL_PATH_KEYS: dict[str, str] = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

_ERROR_DETAIL_LIMIT = 200


@dataclass(frozen=True)
class ChatSessionResult:
    """One chat side-session answer.

    ``text`` is the answer (``""`` when none was produced). ``session_id`` is
    the session the answer ran in, so a follow-up can resume it. ``error`` is
    None only when ``exit_code`` is 0 and ``text`` is non-empty. ``edited_paths``
    lists the repo-relative files the session's edit events named, in
    first-seen order, and is filled on a failed run too.
    """

    text: str
    session_id: str | None
    exit_code: int
    error: str | None
    edited_paths: tuple[str, ...]


def build_chat_prompt(
    message: str,
    *,
    role: str,
    story_id: str | None,
    handoff_path: Path | str,
    log_path: Path | str | None,
    resumed_role: bool = False,
) -> str:
    """Return the chat prompt; the stripped operator message is last.

    With ``resumed_role`` the prompt is for a finished role's own resumed
    session instead of a side session.
    """

    story_text = story_id if story_id else "none"
    if log_path is None:
        log_text = "No log path is known for that role."
    else:
        log_text = f"The `{role}` role's log: {log_path}"
    if resumed_role:
        return (
            f"You are the `{role}` role, resumed in your own session from the "
            "finished dispatch for the story below. The dispatch loop has moved "
            "on, and another role may be running now.\n"
            f"Role: {role}\n"
            f"Story id: {story_text}\n"
            f"HANDOFF.md path: {handoff_path}\n"
            f"{log_text}\n\n"
            "You may edit files and HANDOFF.md to do what the operator asks. "
            "Do not push to any remote. Answer the operator in plain text.\n\n"
            f"Operator message:\n{message.strip()}"
        )
    return (
        f"The operator is talking to you in a side session while the `{role}` "
        "role keeps running in the dispatch loop.\n"
        f"Active role: {role}\n"
        f"Story id: {story_text}\n"
        f"HANDOFF.md path: {handoff_path}\n"
        f"{log_text}\n\n"
        "You may edit files and HANDOFF.md to do what the operator asks. "
        "Do not push to any remote. Answer the operator in plain text.\n\n"
        f"Operator message:\n{message.strip()}"
    )


def build_claude_chat_command(
    prompt: str,
    *,
    agent_config: config.AgentConfig,
    session_id: str | None = None,
) -> list[str]:
    """Return the Claude side-session command with the role's permissions."""

    binary = agent_config.binary or config.CLAUDE_BINARY
    model = agent_config.model or config.CLAUDE_MODEL
    resume_args = ["--resume", session_id] if session_id is not None else []
    return [
        binary,
        *resume_args,
        *claude_permission_args(agent_config),
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
        prompt,
    ]


def build_codex_chat_command(
    prompt: str,
    *,
    agent_config: config.AgentConfig,
    session_id: str | None = None,
) -> list[str]:
    """Return the Codex side-session command with the role's permissions.

    No output file is named, so the role's saved Codex session is untouched.
    """

    binary = agent_config.binary or config.CODEX_BINARY
    permission_args = codex_permission_args(unsafe_bypass=agent_config.unsafe_bypass)
    web_search = f'web_search="{config.CODEX_WEB_SEARCH_MODE}"'
    if session_id is None:
        return [binary, *permission_args, "exec", "--json", "-c", web_search, prompt]
    return [
        binary,
        *permission_args,
        "exec",
        "resume",
        "--json",
        "-c",
        web_search,
        session_id,
        prompt,
    ]


def build_antigravity_chat_command(
    prompt: str,
    *,
    role: str,
    agent_config: config.AgentConfig,
    timeout_ms: int,
    session_id: str | None = None,
) -> list[str]:
    """Return the agy side-session command with the role's agent and grants.

    The command is the role dispatch command with the chat prompt: the
    configured agent name (else the role), model, and print timeout,
    ``--conversation`` only when resuming, and the role's permission
    arguments.
    """

    return _build_antigravity_command(
        binary=agent_config.binary,
        prompt=prompt,
        agent=agent_config.agent_name or str(role),
        timeout_ms=timeout_ms,
        model=agent_config.model,
        resume_session_id=session_id,
        unsafe_bypass=agent_config.unsafe_bypass,
    )


def run_chat_session(
    provider: str,
    message: str,
    *,
    repo_root: Path,
    role: str,
    story_id: str | None,
    handoff_path: Path | str,
    log_path: Path | str | None = None,
    session_id: str | None = None,
    agent_config: config.AgentConfig | None = None,
    timeout_ms: int | None = None,
    resumed_role: bool = False,
) -> ChatSessionResult:
    """Start a chat side session, or resume ``session_id`` when given.

    With ``resumed_role`` the session is a finished role's own session,
    resumed from ``session_id`` with the resumed-role prompt.

    Raises ``ValueError`` before anything launches for a provider other than
    ``claude``, ``codex``, or ``antigravity``, a blank ``message``, a blank
    ``session_id``, or ``resumed_role`` without a ``session_id``.
    """

    if provider not in SUPPORTED_CHAT_PROVIDERS:
        raise ValueError(
            f"Chat is not supported for provider `{provider}`; "
            "chat supports only claude, codex, and antigravity."
        )
    if not isinstance(message, str) or not message.strip():
        raise ValueError("A chat session needs a non-blank message.")
    resolved_session_id: str | None = None
    if session_id is not None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("A chat session resume needs a non-blank session id.")
        resolved_session_id = session_id.strip()
    if resumed_role and resolved_session_id is None:
        raise ValueError("A resumed role chat needs the role's session id.")

    prompt = build_chat_prompt(
        message,
        role=role,
        story_id=story_id,
        handoff_path=handoff_path,
        log_path=log_path,
        resumed_role=resumed_role,
    )
    cwd = Path(repo_root)
    resolved_config = agent_config or config.AgentConfig(provider=provider)
    resolved_timeout_ms = _resolve_timeout_ms(timeout_ms, resolved_config)
    if provider == "claude":
        return _run_claude_chat(
            prompt,
            cwd=cwd,
            agent_config=resolved_config,
            session_id=resolved_session_id,
            timeout_ms=resolved_timeout_ms,
        )
    if provider == "antigravity":
        return _run_antigravity_chat(
            prompt,
            cwd=cwd,
            role=role,
            agent_config=resolved_config,
            session_id=resolved_session_id,
            timeout_ms=resolved_timeout_ms,
        )
    return _run_codex_chat(
        prompt,
        cwd=cwd,
        agent_config=resolved_config,
        session_id=resolved_session_id,
        timeout_ms=resolved_timeout_ms,
    )


def _resolve_timeout_ms(timeout_ms: int | None, agent_config: config.AgentConfig) -> int:
    if timeout_ms is not None:
        return timeout_ms
    if agent_config.timeout_ms is not None:
        return agent_config.timeout_ms
    return config.AGENT_TIMEOUT_MS


@dataclass
class _ClaudeEditTracker:
    """Collect files named by Claude edit tool calls that did not fail."""

    pending_paths_by_id: dict[str, str] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)

    def consume_stdout(self, chunk: str) -> None:
        for event in _json_events(chunk):
            event_type = event.get("type")
            if event_type == "assistant":
                for item in _message_content(event):
                    if item.get("type") == "tool_use":
                        self._handle_tool_use(item)
            elif event_type == "user":
                for item in _message_content(event):
                    if item.get("type") == "tool_result":
                        self._handle_tool_result(item)

    def _handle_tool_use(self, item: dict[str, object]) -> None:
        tool_id = item.get("id")
        name = item.get("name")
        payload = item.get("input")
        if not isinstance(tool_id, str) or not isinstance(name, str):
            return
        key = CLAUDE_EDIT_TOOL_PATH_KEYS.get(name)
        if key is None or not isinstance(payload, dict):
            return
        path = payload.get(key)
        if isinstance(path, str) and path.strip():
            self.pending_paths_by_id[tool_id] = path

    def _handle_tool_result(self, item: dict[str, object]) -> None:
        tool_use_id = item.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            return
        path = self.pending_paths_by_id.pop(tool_use_id, None)
        if path is None or item.get("is_error") is True:
            return
        self.paths.append(path)


@dataclass
class _CodexEditTracker:
    """Collect files named by Codex ``file_change`` items that did not fail."""

    paths: list[str] = field(default_factory=list)

    def consume_stdout(self, chunk: str) -> None:
        for event in _json_events(chunk):
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "file_change":
                continue
            if item.get("status") == "failed":
                continue
            changes = item.get("changes")
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict):
                    continue
                path = change.get("path")
                if isinstance(path, str) and path.strip():
                    self.paths.append(path)


@dataclass
class _AntigravityEditTracker:
    """Collect files named by agy edit tool steps that reached ``DONE``.

    Stdout is buffered by line, so an event split across chunks is parsed
    once whole. A step's path is remembered by ``step_index`` until the step
    ends; an ``ERROR`` step drops it.
    """

    paths: list[str] = field(default_factory=list)
    received_stdout: bool = False
    _line_buffer: str = ""
    _pending_paths_by_step: dict[int, str] = field(default_factory=dict)
    _finished_steps: set[int] = field(default_factory=set)

    def consume_stdout(self, chunk: str) -> None:
        if not chunk:
            return
        self.received_stdout = True
        self._line_buffer += chunk
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            self._consume_line(line)

    def finalize(self) -> None:
        if self._line_buffer:
            line, self._line_buffer = self._line_buffer, ""
            self._consume_line(line)

    def _consume_line(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        event_type, payload = _unwrap_stream_event(event)
        if event_type != "step_update" or payload.get("step_type") != "tool":
            return
        tool_info = payload.get("tool_info")
        if not isinstance(tool_info, dict):
            tool_info = {}
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = tool_info.get("name")
        if tool_name not in ANTIGRAVITY_EDIT_TOOLS:
            return
        step_index = payload.get("step_index")
        step: int | None = (
            step_index
            if isinstance(step_index, int) and not isinstance(step_index, bool)
            else None
        )
        if step is not None and step in self._finished_steps:
            return
        path = _antigravity_edit_path(tool_info)
        if step is not None and path is not None:
            self._pending_paths_by_step[step] = path
        state = payload.get("state")
        if state == _ANTIGRAVITY_ERROR_STATE:
            self._finish(step)
            return
        if state != _ANTIGRAVITY_DONE_STATE:
            return
        if step is not None:
            path = self._pending_paths_by_step.get(step, path)
        self._finish(step)
        if tool_info.get("error") or path is None:
            return
        self.paths.append(path)

    def _finish(self, step: int | None) -> None:
        if step is None:
            return
        self._pending_paths_by_step.pop(step, None)
        self._finished_steps.add(step)


def _antigravity_edit_path(tool_info: dict[str, object]) -> str | None:
    parameters = tool_info.get("parameters")
    if not isinstance(parameters, dict):
        return None
    path = parameters.get(ANTIGRAVITY_EDIT_PATH_KEY)
    if isinstance(path, str) and path.strip():
        return path
    return None


def _json_events(chunk: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    if not chunk:
        return events
    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _message_content(event: dict[str, object]) -> list[dict[str, object]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _run_claude_chat(
    prompt: str,
    *,
    cwd: Path,
    agent_config: config.AgentConfig,
    session_id: str | None,
    timeout_ms: int,
) -> ChatSessionResult:
    monitor = _ClaudeStreamJsonMonitor(agent_name="Claude chat", log=None)
    tracker = _ClaudeEditTracker()

    def on_stdout(chunk: str) -> None:
        monitor.consume_stdout(chunk)
        tracker.consume_stdout(chunk)

    result = _run_command_streaming(
        build_claude_chat_command(
            prompt, agent_config=agent_config, session_id=session_id
        ),
        cwd=cwd,
        timeout_ms=timeout_ms,
        idle_timeout_ms=agent_config.idle_timeout_ms,
        env=_build_claude_env(agent_config),
        on_stdout=on_stdout,
        # A chat answer is never stopped by a takeover its predecessor asked for.
        stop_on_chat_takeover=False,
    )
    text = (monitor.final_result_text or "").strip()
    return ChatSessionResult(
        text=text,
        session_id=monitor.session_id or session_id,
        exit_code=result.exit_code,
        error=_chat_error(
            "Claude", exit_code=result.exit_code, text=text, stderr=result.stderr
        ),
        edited_paths=normalize_edited_paths(tracker.paths, repo_root=cwd),
    )


def _run_codex_chat(
    prompt: str,
    *,
    cwd: Path,
    agent_config: config.AgentConfig,
    session_id: str | None,
    timeout_ms: int,
) -> ChatSessionResult:
    monitor = _CodexJsonMonitor(agent_name="Codex chat", log=None)
    tracker = _CodexEditTracker()

    def on_stdout(chunk: str) -> None:
        monitor.consume_stdout(chunk)
        tracker.consume_stdout(chunk)

    result = _run_command_streaming(
        build_codex_chat_command(
            prompt, agent_config=agent_config, session_id=session_id
        ),
        cwd=cwd,
        timeout_ms=timeout_ms,
        idle_timeout_ms=agent_config.idle_timeout_ms,
        env=_build_codex_env(agent_config),
        on_stdout=on_stdout,
        # A chat answer is never stopped by a takeover its predecessor asked for.
        stop_on_chat_takeover=False,
    )
    text = unwrap_codex_final_message(monitor.last_agent_message).strip()
    return ChatSessionResult(
        text=text,
        session_id=monitor.thread_id or session_id,
        exit_code=result.exit_code,
        error=_chat_error(
            "Codex", exit_code=result.exit_code, text=text, stderr=result.stderr
        ),
        edited_paths=normalize_edited_paths(tracker.paths, repo_root=cwd),
    )


def _run_antigravity_chat(
    prompt: str,
    *,
    cwd: Path,
    role: str,
    agent_config: config.AgentConfig,
    session_id: str | None,
    timeout_ms: int,
) -> ChatSessionResult:
    monitor = _AntigravityStreamJsonMonitor(agent_name="Antigravity chat", log=None)
    tracker = _AntigravityEditTracker()

    def on_stdout(chunk: str) -> None:
        monitor.consume_stdout(chunk)
        tracker.consume_stdout(chunk)

    result = _run_command_streaming(
        build_antigravity_chat_command(
            prompt,
            role=role,
            agent_config=agent_config,
            timeout_ms=timeout_ms,
            session_id=session_id,
        ),
        cwd=cwd,
        timeout_ms=timeout_ms,
        idle_timeout_ms=agent_config.idle_timeout_ms,
        env=build_agent_env(agent_config),
        on_stdout=on_stdout,
        on_stderr=monitor.consume_stderr,
        # A chat answer is never stopped by a takeover its predecessor asked for.
        stop_on_chat_takeover=False,
    )
    if not monitor.received_stdout and result.stdout:
        # A runner that did not stream live callbacks still returns stdout.
        monitor.consume_stdout(result.stdout)
    if not tracker.received_stdout and result.stdout:
        tracker.consume_stdout(result.stdout)
    tracker.finalize()
    # finalize maps result-status, missing-result, deny-rule, soft-deny, and
    # sign-in failures onto the exit code and stderr, as for a role dispatch.
    finalized = monitor.finalize(result)
    text = monitor.response_text().strip()
    return ChatSessionResult(
        text=text,
        session_id=finalized.session_id or session_id,
        exit_code=finalized.exit_code,
        error=_chat_error(
            "Antigravity",
            exit_code=finalized.exit_code,
            text=text,
            stderr=finalized.stderr,
        ),
        edited_paths=normalize_edited_paths(tracker.paths, repo_root=cwd),
    )


def normalize_edited_paths(
    paths: list[str] | tuple[str, ...],
    *,
    repo_root: Path,
) -> tuple[str, ...]:
    """Return repo-relative forward-slash paths inside ``repo_root``.

    Relative paths resolve against ``repo_root``. Paths outside it, or ones
    that cannot be resolved, are dropped; duplicates keep first-seen order.
    """

    try:
        root = Path(repo_root).resolve()
    except (OSError, RuntimeError, ValueError):
        return ()
    normalized: list[str] = []
    for raw_path in paths:
        # A NUL byte is never a valid file name, but non-strict resolve()
        # accepts it on some Python versions, so reject it here.
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            continue
        try:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = root / candidate
            relative = candidate.resolve().relative_to(root)
        except (OSError, RuntimeError, ValueError, TypeError):
            continue
        relative_text = relative.as_posix()
        if relative_text in ("", "."):
            continue
        if relative_text not in normalized:
            normalized.append(relative_text)
    return tuple(normalized)


def _chat_error(agent: str, *, exit_code: int, text: str, stderr: str) -> str | None:
    if exit_code != 0:
        reason = f"{agent} exited with code {exit_code}."
        detail = redact_stderr_summary(stderr or "", limit=_ERROR_DETAIL_LIMIT)
        return f"{reason} {detail}" if detail else reason
    if not text:
        return f"{agent} produced no answer text."
    return None
