from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time

from llm_handoff import config
from llm_handoff.agent_process import (
    _append_agent_additional_instruction,
    _run_command_streaming,
)
from llm_handoff.agent_streams import (
    _LiveAgentStreamState,
    _filter_codex_json_stderr_line,
)
from llm_handoff.agent_types import DispatchResult, LogFn, _ProcessResult


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


def invoke_codex(
    handoff_path: Path,
    *,
    log: LogFn | None = None,
    use_resume: bool = False,
    additional_instruction: str | None = None,
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
                    additional_instruction=additional_instruction,
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
                        _build_codex_managed_bootstrap_prompt(
                            handoff_path,
                            additional_instruction=additional_instruction,
                        ),
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
                    _build_codex_managed_bootstrap_prompt(
                        handoff_path,
                        additional_instruction=additional_instruction,
                    ),
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
                _build_codex_stateless_prompt(
                    handoff_path,
                    additional_instruction=additional_instruction,
                ),
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
    additional_instruction: str | None = None,
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
        _build_codex_resume_prompt(
            handoff_path,
            additional_instruction=additional_instruction,
        ),
    ]


def _build_codex_stateless_prompt(
    handoff_path: Path,
    *,
    additional_instruction: str | None = None,
) -> str:
    prompt = (
        f"Use the repo skill ${config.CODEX_SKILL_NAME}. Read {handoff_path} and treat it as the live task file. "
        "Work statelessly and do not assume prior session memory. "
        "The skill replaces any legacy shared bootstrap or Codex handoff prompt files for this repo. "
        "Follow the skill's repo operating procedure, read/write/follow HANDOFF.md as needed, "
        "and execute the current Codex assignment."
    )
    return _append_agent_additional_instruction(prompt, additional_instruction)


def _build_codex_managed_bootstrap_prompt(
    handoff_path: Path,
    *,
    additional_instruction: str | None = None,
) -> str:
    prompt = (
        f"Use the repo skill ${config.CODEX_SKILL_NAME}. This is the first managed Codex dispatch session for this repo. "
        f"Read {config.DEFAULT_SHARED_INIT_PROMPT_PATH} and use it as the bootstrap checklist for repo context loading, but do not stop after the startup report. "
        f"After bootstrapping, read {handoff_path} and treat it as the live task file. "
        "Follow HANDOFF.md as needed, update it when you finish, and execute the current Codex assignment."
    )
    return _append_agent_additional_instruction(prompt, additional_instruction)


def _build_codex_resume_prompt(
    handoff_path: Path,
    *,
    additional_instruction: str | None = None,
) -> str:
    prompt = (
        "Continue the existing Codex dispatch session for this repo. "
        f"Re-read {handoff_path} and treat it as the live task file. "
        f"Follow the repo skill ${config.CODEX_SKILL_NAME} as the governing workflow. "
        "Use prior session context where it helps, but treat HANDOFF.md and the repo source-of-truth docs as authoritative for the current assignment."
    )
    return _append_agent_additional_instruction(prompt, additional_instruction)


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
