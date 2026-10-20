from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time

from llm_handoff import config
from llm_handoff.agent_process import (
    build_agent_env,
    _append_agent_additional_instruction,
    _run_command_streaming,
)
from llm_handoff.agent_streams import (
    _LiveAgentStreamState,
    _filter_codex_json_stderr_line,
)
from llm_handoff.agent_types import DispatchResult, LogFn, _ProcessResult
from llm_handoff.redaction import redact_secrets
from llm_handoff.roles import CANONICAL_DISPATCH_ROLES
from llm_handoff.usage_log import format_usage_line, usage_fields_from_codex_turn


@dataclass(frozen=True)
class _CodexCommandResult:
    result: _ProcessResult
    thread_id: str | None
    final_message: str
    usage: dict[str, object] | None = None


@dataclass(frozen=True)
class _CodexArtifactPaths:
    output_directory: Path
    output_last_message_path: Path
    output_schema_path: Path
    session_state_path: Path


@dataclass
class _CodexJsonMonitor:
    agent_name: str
    log: LogFn | None
    thread_id: str | None = None
    last_agent_message: str = ""
    _stderr_state: _LiveAgentStreamState = field(default_factory=_LiveAgentStreamState)
    usage_totals: dict[str, int] = field(default_factory=dict)

    @property
    def usage(self) -> dict[str, object] | None:
        """Token counts summed over every ``turn.completed`` event, or None."""

        if not self.usage_totals:
            return None
        return dict(self.usage_totals)

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
                self.log("AGENT", f"{self.agent_name}: {redact_secrets(message)}")
            else:
                self.log("WARN", f"{self.agent_name} stderr: {redact_secrets(message)}")

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
            fields = usage_fields_from_codex_turn(event)
            for key, value in fields.items():
                if isinstance(value, int):
                    self.usage_totals[key] = self.usage_totals.get(key, 0) + value
            if self.log is not None and fields:
                # Codex reports tokens but no price. Log file only (DEBUG).
                self.log(
                    "DEBUG",
                    format_usage_line(self.agent_name, "codex", fields),
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


def invoke_codex(
    handoff_path: Path,
    *,
    log: LogFn | None = None,
    use_resume: bool = False,
    additional_instruction: str | None = None,
    role_name: str = "backend",
    agent_name: str = "Codex",
    binary: str | None = None,
    skill_name: str | None = None,
    timeout_ms: int | None = None,
    repo_root: Path | None = None,
    agent_config: config.AgentConfig | None = None,
) -> DispatchResult:
    # The dispatcher may run from its own source checkout against a different
    # target repo, so the launch directory must come from the config rather
    # than from the process cwd.
    repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
    artifact_paths = _codex_artifact_paths(repo_root, role=role_name)
    _cleanup_codex_output_artifacts(artifact_paths)
    resolved_binary = binary or config.CODEX_BINARY
    resolved_skill_name = skill_name or config.CODEX_SKILL_NAME
    resolved_timeout_ms = timeout_ms or config.AGENT_TIMEOUT_MS
    idle_timeout_ms = agent_config.idle_timeout_ms if agent_config else None
    env = _build_codex_env(agent_config)
    unsafe_bypass = bool(agent_config is not None and agent_config.unsafe_bypass)
    start_time = time.monotonic()
    if use_resume:
        saved_thread_id = _read_codex_session_state(artifact_paths)
        if saved_thread_id:
            result = _run_codex_json_command(
                agent_name,
                _build_codex_resume_command(
                    handoff_path,
                    artifact_paths=artifact_paths,
                    thread_id=saved_thread_id,
                    additional_instruction=additional_instruction,
                    binary=resolved_binary,
                    skill_name=resolved_skill_name,
                    unsafe_bypass=unsafe_bypass,
                ),
                cwd=repo_root,
                timeout_ms=resolved_timeout_ms,
                idle_timeout_ms=idle_timeout_ms,
                output_last_message_path=artifact_paths.output_last_message_path,
                log=log,
                env=env,
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
                    agent_name,
                    _build_codex_exec_command(
                        _build_codex_managed_bootstrap_prompt(
                            handoff_path,
                            role_name=role_name,
                            skill_name=resolved_skill_name,
                            additional_instruction=additional_instruction,
                        ),
                        artifact_paths=artifact_paths,
                        binary=resolved_binary,
                        unsafe_bypass=unsafe_bypass,
                    ),
                    cwd=repo_root,
                    timeout_ms=resolved_timeout_ms,
                    idle_timeout_ms=idle_timeout_ms,
                    output_last_message_path=artifact_paths.output_last_message_path,
                    log=log,
                    env=env,
                )
        else:
            result = _run_codex_json_command(
                agent_name,
                _build_codex_exec_command(
                    _build_codex_managed_bootstrap_prompt(
                        handoff_path,
                        role_name=role_name,
                        skill_name=resolved_skill_name,
                        additional_instruction=additional_instruction,
                    ),
                    artifact_paths=artifact_paths,
                    binary=resolved_binary,
                    unsafe_bypass=unsafe_bypass,
                ),
                cwd=repo_root,
                timeout_ms=resolved_timeout_ms,
                idle_timeout_ms=idle_timeout_ms,
                output_last_message_path=artifact_paths.output_last_message_path,
                log=log,
                env=env,
            )
        if result.thread_id:
            _write_codex_session_state(artifact_paths, result.thread_id)
    else:
        result = _run_codex_json_command(
            agent_name,
            _build_codex_exec_command(
                _build_codex_stateless_prompt(
                    handoff_path,
                    role_name=role_name,
                    skill_name=resolved_skill_name,
                    additional_instruction=additional_instruction,
                ),
                artifact_paths=artifact_paths,
                binary=resolved_binary,
                unsafe_bypass=unsafe_bypass,
            ),
            cwd=repo_root,
            timeout_ms=resolved_timeout_ms,
            idle_timeout_ms=idle_timeout_ms,
            output_last_message_path=artifact_paths.output_last_message_path,
            log=log,
            env=env,
        )
    return DispatchResult(
        stdout=result.final_message or result.result.stdout,
        stderr=result.result.stderr,
        exit_code=result.result.exit_code,
        elapsed_seconds=time.monotonic() - start_time,
        # The thread id of the run that produced this result; None when that
        # run emitted no `thread.started` event.
        session_id=result.thread_id,
        usage=result.usage,
    )


def codex_permission_args(*, unsafe_bypass: bool = False) -> list[str]:
    """Return the Codex sandbox/approval arguments for one role launch.

    The default keeps Codex inside its workspace-write sandbox and never asks
    for approval; ``unsafe_bypass`` restores the previous ``--yolo`` launch.
    """
    if unsafe_bypass:
        return list(config.CODEX_UNSAFE_BYPASS_ARGS)
    return list(config.CODEX_SANDBOX_ARGS)


def _build_codex_exec_command(
    prompt: str,
    *,
    artifact_paths: _CodexArtifactPaths,
    binary: str | None = None,
    unsafe_bypass: bool = False,
) -> list[str]:
    return [
        binary or config.CODEX_BINARY,
        *codex_permission_args(unsafe_bypass=unsafe_bypass),
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
    additional_instruction: str | None = None,
    binary: str | None = None,
    skill_name: str | None = None,
    unsafe_bypass: bool = False,
) -> list[str]:
    return [
        binary or config.CODEX_BINARY,
        *codex_permission_args(unsafe_bypass=unsafe_bypass),
        "exec",
        "resume",
        "--json",
        "-c",
        f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
        "--output-last-message",
        str(artifact_paths.output_last_message_path),
        thread_id,
        _build_codex_resume_prompt(
            handoff_path,
            skill_name=skill_name or config.CODEX_SKILL_NAME,
            additional_instruction=additional_instruction,
        ),
    ]


def _build_codex_stateless_prompt(
    handoff_path: Path,
    *,
    role_name: str = "backend",
    skill_name: str | None = None,
    additional_instruction: str | None = None,
) -> str:
    resolved_skill_name = skill_name or config.CODEX_SKILL_NAME
    prompt = (
        f"Use the repo skill ${resolved_skill_name}. Read {handoff_path} and treat it as the live task file. "
        "Work statelessly and do not assume prior session memory. "
        "The skill replaces any legacy shared bootstrap or Codex handoff prompt files for this repo. "
        "Follow the skill's repo operating procedure, read/write/follow HANDOFF.md as needed, "
        f"and execute the current {role_name} assignment."
    )
    return _append_agent_additional_instruction(prompt, additional_instruction)


def _build_codex_managed_bootstrap_prompt(
    handoff_path: Path,
    *,
    role_name: str = "backend",
    skill_name: str | None = None,
    additional_instruction: str | None = None,
) -> str:
    resolved_skill_name = skill_name or config.CODEX_SKILL_NAME
    prompt = (
        f"Use the repo skill ${resolved_skill_name}. This is the first managed Codex dispatch session for this repo. "
        f"Read {config.DEFAULT_SHARED_INIT_PROMPT_PATH} and use it as the bootstrap checklist for repo context loading, but do not stop after the startup report. "
        f"After bootstrapping, read {handoff_path} and treat it as the live task file. "
        "Follow HANDOFF.md as needed, update it when you finish, "
        f"and execute the current {role_name} assignment."
    )
    return _append_agent_additional_instruction(prompt, additional_instruction)


def _build_codex_resume_prompt(
    handoff_path: Path,
    *,
    skill_name: str | None = None,
    additional_instruction: str | None = None,
) -> str:
    resolved_skill_name = skill_name or config.CODEX_SKILL_NAME
    prompt = (
        "Continue the existing Codex dispatch session for this repo. "
        f"Re-read {handoff_path} and treat it as the live task file. "
        f"Follow the repo skill ${resolved_skill_name} as the governing workflow. "
        "Use prior session context where it helps, but treat HANDOFF.md and the repo source-of-truth docs as authoritative for the current assignment."
    )
    return _append_agent_additional_instruction(prompt, additional_instruction)


def _codex_artifact_paths(
    repo_root: Path,
    *,
    role: str = "backend",
) -> _CodexArtifactPaths:
    """Return the Codex artifact paths for one dispatcher role.

    The backend keeps the original ``logs/dispatch/codex/`` directory; every
    other role gets ``logs/dispatch/codex-<role>/`` so two Codex roles never
    resume each other's session or share a last-message file. The role must
    be a canonical dispatch role, so the path segment never comes from
    arbitrary text.
    """
    if role not in CANONICAL_DISPATCH_ROLES:
        raise ValueError(f"Unknown Codex artifact role: {role!r}")
    if role == "backend":
        output_directory = config.CODEX_OUTPUT_DIRECTORY
        last_message_path = config.CODEX_OUTPUT_LAST_MESSAGE_PATH
        session_state_path = config.CODEX_SESSION_STATE_PATH
    else:
        output_directory = config.CODEX_OUTPUT_DIRECTORY.with_name(f"codex-{role}")
        last_message_path = output_directory / config.CODEX_OUTPUT_LAST_MESSAGE_PATH.name
        session_state_path = output_directory / config.CODEX_SESSION_STATE_PATH.name
    return _CodexArtifactPaths(
        output_directory=(repo_root / output_directory).resolve(),
        output_last_message_path=(repo_root / last_message_path).resolve(),
        output_schema_path=config.CODEX_OUTPUT_SCHEMA_PATH.resolve(),
        session_state_path=(repo_root / session_state_path).resolve(),
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


def _run_codex_json_command(
    agent_name: str,
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    output_last_message_path: Path,
    log: LogFn | None = None,
    env: dict[str, str] | None = None,
    idle_timeout_ms: int | None = None,
) -> _CodexCommandResult:
    monitor = _CodexJsonMonitor(agent_name=agent_name, log=log)
    result = _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        idle_timeout_ms=idle_timeout_ms,
        env=env,
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
        usage=monitor.usage,
    )


def _build_codex_env(agent_config: config.AgentConfig | None = None) -> dict[str, str]:
    # OPENAI_API_KEY is passed only when the role sets use_api_key_env: true.
    return build_agent_env(agent_config or config.AgentConfig(provider="codex"))


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


def unwrap_codex_final_message(text: str) -> str:
    """Return the ``summary`` of a ``{status, summary}`` final message.

    A fresh ``codex exec`` run must answer in the output schema's JSON
    envelope, so a support role's report (a validation verdict or a ledger
    update) arrives inside ``summary``. Text that is not an envelope, such as
    a resumed session's plain answer, is returned unchanged.
    """

    structured = _parse_codex_structured_message(text.strip())
    return structured["summary"] if structured is not None else text
