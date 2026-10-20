from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time

from llm_handoff import config
from llm_handoff.agent_process import (
    build_agent_env,
    _first_nonempty_line,
    _first_nonempty_value,
    _run_command_streaming,
    _run_logged_agent_command,
)
from llm_handoff.agent_types import LogFn, SubagentResult, _ProcessResult
from llm_handoff.redaction import redact_secrets
from llm_handoff.usage_log import format_usage_line, usage_fields_from_claude_result


# `Bash(git diff:*)` and `Bash(git log *)` approve a prefix; `Bash(npm test)`
# approves that exact command.
_BASH_RULE_RE = re.compile(r"Bash\((.+?)(:\*| \*)?\)")
# Claude Code's tool_result text when dontAsk mode refuses a tool call.
_PERMISSION_REFUSAL_MARKERS = ("Permission to use", "has been denied")
# Refusal summary prefixes: a shell program basename, a git subcommand, or a
# tool name. Anything outside these character sets is replaced with fixed
# text so no raw command arguments reach the log.
_SHELL_TOOL_NAMES = frozenset({"bash", "powershell"})
_REFUSAL_PROGRAM_RE = re.compile(r"^[A-Za-z0-9._+-]{1,40}$")
_REFUSAL_GIT_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_REFUSAL_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_REFUSAL_UNSAFE_PROGRAM = "<command>"
_REFUSAL_UNKNOWN_TOOL = "Tool"


@dataclass
class _ClaudeStreamJsonMonitor:
    agent_name: str
    log: LogFn | None
    tool_labels_by_id: dict[str, str] = field(default_factory=dict)
    # Refusal summary prefix per tool id; never holds raw command arguments.
    tool_refusal_prefixes_by_id: dict[str, str] = field(default_factory=dict)
    refusal_count: int = 0
    refused_prefixes: list[str] = field(default_factory=list)
    assistant_chunks: list[str] = field(default_factory=list)
    _assistant_line_buffer: str = ""
    final_result_text: str | None = None
    model: str | None = None
    usage: dict[str, object] | None = None
    # From the first `system`/`init` event; None when absent or not a string.
    session_id: str | None = None
    _saw_init: bool = False

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
                self.log("INFO", f"{self.agent_name} stderr: {redact_secrets(line)}")

    def finalize(self, result: _ProcessResult) -> _ProcessResult:
        self._flush_assistant_lines(flush_partial=True)
        final_stdout = self.final_result_text
        if final_stdout is None:
            final_stdout = "".join(self.assistant_chunks)
        usage = self.usage
        if usage is not None and not any(key != "model" for key in usage):
            usage = None
        if self.refusal_count > 0 and self.log is not None:
            # One summary per process run; a fallback launch has its own
            # monitor and logs its own summary.
            self.log(
                "WARN",
                f"{self.agent_name}: permission rules refused "
                f"{self.refusal_count} tool call(s) this dispatch "
                f"({', '.join(self.refused_prefixes)})",
            )
        return _ProcessResult(
            stdout=final_stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            usage=dict(usage) if usage is not None else None,
            session_id=self.session_id,
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
        if event.get("subtype") != "init":
            return
        if isinstance(event.get("model"), str):
            self.model = str(event["model"])
        if not self._saw_init:
            self._saw_init = True
            raw_session_id = event.get("session_id")
            if isinstance(raw_session_id, str) and raw_session_id:
                self.session_id = raw_session_id
        if self.log is None:
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

        usage_fields = {"model": self.model, **usage_fields_from_claude_result(event)}
        self.usage = usage_fields

        if self.log is None:
            return

        stop_reason = str(event.get("stop_reason", "unknown"))
        self.log(
            "AGENT",
            f"{self.agent_name}: Claude result (stop={stop_reason})",
        )
        # Token usage and cost go to the log file only (DEBUG).
        self.log(
            "DEBUG",
            format_usage_line(self.agent_name, "claude", usage_fields),
        )

    def _handle_tool_use(self, item: dict[str, object]) -> None:
        tool_id = item.get("id")
        name = item.get("name")
        input_payload = item.get("input")
        label = _format_claude_tool_use_label(name, input_payload)
        if isinstance(tool_id, str):
            self.tool_labels_by_id[tool_id] = label
            self.tool_refusal_prefixes_by_id[tool_id] = _refusal_prefix(
                name, input_payload
            )
        if self.log is not None:
            self.log("AGENT", f"{self.agent_name}: {label}")

    def _handle_tool_result(self, item: dict[str, object]) -> None:
        tool_use_id = item.get("tool_use_id")
        if isinstance(tool_use_id, str):
            label = self.tool_labels_by_id.pop(tool_use_id, None)
            prefix = self.tool_refusal_prefixes_by_id.pop(tool_use_id, None)
        else:
            label = None
            prefix = None
        if label is None:
            label = "Tool"
        if prefix is None:
            prefix = _REFUSAL_UNKNOWN_TOOL

        content = _extract_claude_tool_result_text(item.get("content"))
        first_line = _first_nonempty_line(content)
        is_error = bool(item.get("is_error"))
        is_refusal = is_error and is_permission_refusal(content)
        if is_refusal:
            self.refusal_count += 1
            if prefix not in self.refused_prefixes:
                self.refused_prefixes.append(prefix)

        if self.log is None:
            return
        if is_refusal:
            # The agent asked for a command its allowlist does not cover. That
            # is the agent's to fix, not a dispatcher error.
            self.log(
                "WARN",
                f"{self.agent_name}: refused by the permission rules: "
                f"{_shorten(label)}",
            )
        elif is_error:
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
    repo_root: Path | None = None,
    agent_config: config.AgentConfig | None = None,
) -> SubagentResult:
    # The dispatcher may run from its own source checkout against a different
    # target repo, so the launch directory must come from the config rather
    # than from the process cwd.
    resolved_repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
    resolved_binary = binary or config.CLAUDE_BINARY
    permission_args = claude_permission_args(
        agent_config or config.AgentConfig(provider="claude"),
        permissions_flag=permissions_flag,
    )
    resolved_model = model or config.CLAUDE_MODEL
    resolved_timeout_ms = timeout_ms or config.SUBAGENT_TIMEOUT_MS
    idle_timeout_ms = agent_config.idle_timeout_ms if agent_config else None
    resolved_agent_name = agent_name or f"Claude {subagent_name}"
    start_time = time.monotonic()
    stream_json_command = [
        resolved_binary,
        *permission_args,
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
        cwd=resolved_repo_root,
        timeout_ms=resolved_timeout_ms,
        idle_timeout_ms=idle_timeout_ms,
        env=_build_claude_env(agent_config),
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
                *permission_args,
                "--model",
                resolved_model,
                "-p",
                prompt,
            ],
            cwd=resolved_repo_root,
            timeout_ms=resolved_timeout_ms,
            idle_timeout_ms=idle_timeout_ms,
            env=_build_claude_env(agent_config),
            log=log,
            stream_all_stdout=True,
        )
        # Buffered text mode carries no usage events and no session id.
        return SubagentResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            elapsed_seconds=time.monotonic() - start_time,
            usage=None,
            session_id=None,
        )
    return SubagentResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=time.monotonic() - start_time,
        usage=result.usage,
        session_id=result.session_id,
    )


def claude_permission_args(
    agent_config: config.AgentConfig,
    *,
    permissions_flag: str | None = None,
) -> list[str]:
    """Return the Claude Code permission arguments for one role launch.

    The default runs in ``dontAsk`` mode. ``--tools`` limits the built-in
    tools that exist at all to the base names in ``allowed_tools`` (so user
    or project settings cannot approve anything else), ``--strict-mcp-config``
    loads no MCP servers unless the list names an ``mcp__`` tool,
    ``--allowedTools`` pre-approves the listed rules, and web tools are always
    denied. In Linear mode with ``tracker.claude_mcp_config`` the launch also
    passes ``--mcp-config <path> --strict-mcp-config`` so only that file's MCP
    servers load. ``unsafe_bypass`` restores the previous
    ``--dangerously-skip-permissions`` launch (or the role's
    ``permissions_flag``).
    """
    if agent_config.unsafe_bypass:
        return [
            permissions_flag
            or agent_config.permissions_flag
            or config.CLAUDE_PERMISSIONS_FLAG
        ]
    # `allowed_tools` as written, or the default list for the loaded config's
    # `interpreter_grants` (`config.claude_default_allowed_tools`).
    allowed_tools = agent_config.claude_allowed_tools
    args = [
        "--permission-mode",
        config.CLAUDE_PERMISSION_MODE,
        *claude_tool_restriction_args(allowed_tools),
    ]
    if allowed_tools:
        args.extend(["--allowedTools", ",".join(allowed_tools)])
    args.extend(["--disallowedTools", ",".join(config.CLAUDE_DISALLOWED_TOOLS)])
    guidance = claude_shell_guidance(allowed_tools)
    if guidance:
        args.extend(["--append-system-prompt", guidance])
    mcp_config_path = agent_config.claude_mcp_config_path
    if mcp_config_path is not None:
        # Linear mode with tracker.claude_mcp_config: load only that file's
        # MCP servers, not every server in the user's Claude configuration.
        args.extend(["--mcp-config", str(mcp_config_path)])
        if "--strict-mcp-config" not in args:
            args.append("--strict-mcp-config")
    return args


def claude_shell_guidance(allowed_tools: tuple[str, ...]) -> str | None:
    """Explain the Bash allowlist so the agent does not waste refused calls.

    In ``dontAsk`` mode a command outside the allowlist is refused outright.
    Agents tend to chain commands or reach for ``cat``/``find``, get refused,
    and sometimes conclude they have no shell at all.
    """

    prefixes: list[str] = []
    exact: list[str] = []
    for rule in allowed_tools:
        match = _BASH_RULE_RE.fullmatch(rule.strip())
        if match is None:
            continue
        command, wildcard = match.group(1).strip(), match.group(2)
        bucket = prefixes if wildcard else exact
        if command and command not in bucket:
            bucket.append(command)
    if not prefixes and not exact:
        return None
    approved = []
    if prefixes:
        approved.append("by prefix: " + ", ".join(f"`{p}`" for p in prefixes))
    if exact:
        approved.append("as exact commands: " + ", ".join(f"`{c}`" for c in exact))
    tools = {rule.split("(", 1)[0].strip() for rule in allowed_tools}
    readers = [name for name in ("Read", "Glob", "Grep") if name in tools]
    writers = [name for name in ("Write", "Edit") if name in tools]
    file_tools = ""
    if readers:
        file_tools += (
            f"Use the {_join_names(readers, 'and')} tool"
            + ("s" if len(readers) > 1 else "")
            + " instead of `cat`, `ls`, `find`, or `head`"
        )
        if writers:
            file_tools += f", and {_join_names(writers, 'or')} to change files"
        file_tools += ". "
    elif writers:
        file_tools = f"Use {_join_names(writers, 'or')} to change files. "
    return (
        f"Shell commands in this session are pre-approved {'; '.join(approved)}. "
        "Anything else is refused without a prompt. Run one command per Bash "
        "call, from the repository root. Do not join commands with `&&`, `;`, "
        "or `|`, and do not use redirections, loops, `cd`, environment-variable "
        f"prefixes, or `git -C <path>`. {file_tools}If a command is refused, "
        "rewrite it as one allowed command; a refusal does not mean the shell "
        "is unavailable."
    )


def _join_names(names: list[str], conjunction: str) -> str:
    if len(names) <= 2:
        return f" {conjunction} ".join(names)
    return ", ".join(names[:-1]) + f", {conjunction} {names[-1]}"


def is_permission_refusal(tool_result_text: str) -> bool:
    return all(marker in tool_result_text for marker in _PERMISSION_REFUSAL_MARKERS)


def _refusal_prefix(name: object, input_payload: object) -> str:
    """Return the refusal summary prefix for one tool call.

    Shell tools give the program basename, plus the subcommand for `git`;
    other tools give their tool name. Text outside a safe character set is
    replaced with fixed text, so the prefix never carries raw arguments.
    """
    tool_name = name if isinstance(name, str) else ""
    if tool_name.lower() in _SHELL_TOOL_NAMES:
        command = (
            input_payload.get("command") if isinstance(input_payload, dict) else None
        )
        tokens = command.split() if isinstance(command, str) else []
        if not tokens:
            return tool_name
        program = tokens[0].strip("\"'")
        program = re.split(r"[/\\]", program)[-1]
        if not _REFUSAL_PROGRAM_RE.match(program):
            return _REFUSAL_UNSAFE_PROGRAM
        if (
            program.lower() == "git"
            and len(tokens) > 1
            and _REFUSAL_GIT_SUBCOMMAND_RE.match(tokens[1])
        ):
            return f"git {tokens[1]}"
        return program
    if _REFUSAL_TOOL_NAME_RE.match(tool_name):
        return tool_name
    return _REFUSAL_UNKNOWN_TOOL


def _shorten(text: str, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def claude_tool_restriction_args(allowed_tools: tuple[str, ...]) -> list[str]:
    """Return ``--tools`` (and ``--strict-mcp-config``) for an allowlist.

    ``--allowedTools`` only pre-approves rules; tools approved elsewhere in
    settings would still run. ``--tools`` restricts which built-in tools are
    available, and an empty value disables them all.
    """
    built_in: list[str] = []
    uses_mcp = False
    for rule in allowed_tools:
        name = rule.split("(", 1)[0].strip()
        if name.startswith("mcp__"):
            uses_mcp = True
        elif name and name not in built_in:
            built_in.append(name)
    args = ["--tools", ",".join(built_in)]
    if not uses_mcp:
        args.append("--strict-mcp-config")
    return args


def _run_claude_stream_json_command(
    agent_name: str,
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    idle_timeout_ms: int | None = None,
    env: dict[str, str] | None = None,
    log: LogFn | None = None,
) -> _ProcessResult:
    monitor = _ClaudeStreamJsonMonitor(agent_name=agent_name, log=log)
    result = _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        idle_timeout_ms=idle_timeout_ms,
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


def _build_claude_env(
    agent_config: config.AgentConfig | None = None,
) -> dict[str, str]:
    # Claude Code launches always use the CLI's own login; ANTHROPIC_API_KEY
    # is never passed, in either env mode.
    return build_agent_env(agent_config or config.AgentConfig(provider="claude"))
