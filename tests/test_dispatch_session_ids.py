from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from llm_handoff import agent_roles
from llm_handoff.agent_providers import claude, codex
from llm_handoff.agent_providers.claude import _ClaudeStreamJsonMonitor
from llm_handoff.agent_types import DispatchResult, SubagentResult, _ProcessResult
from llm_handoff.config import AgentConfig


CLAUDE_INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "claude-session-1",
    "model": "claude-opus-5-5",
    "tools": [],
}
CLAUDE_RESULT = {"type": "result", "result": "done", "stop_reason": "end_turn"}


def _lines(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _empty_process_result() -> _ProcessResult:
    return _ProcessResult(stdout="", stderr="", exit_code=0)


class _FakeStreamingRunner:
    """Stands in for ``_run_command_streaming``; replays one script per call."""

    def __init__(self, *scripts: tuple[str, _ProcessResult]) -> None:
        self.scripts = list(scripts)
        self.commands: list[list[str]] = []

    def __call__(self, command, *, cwd, timeout_ms, on_stdout=None, **_kwargs):
        self.commands.append(list(command))
        stdout, result = self.scripts.pop(0)
        if on_stdout is not None and stdout:
            on_stdout(stdout)
        return result


# --- Claude stream monitor --------------------------------------------------


@pytest.mark.parametrize("with_logger", [False, True])
def test_claude_monitor_returns_init_session_id(with_logger: bool) -> None:
    logs: list[tuple[str, str]] = []
    monitor = _ClaudeStreamJsonMonitor(
        agent_name="Claude planner",
        log=(lambda level, message: logs.append((level, message)))
        if with_logger
        else None,
    )

    monitor.consume_stdout(_lines(CLAUDE_INIT, CLAUDE_RESULT))
    result = monitor.finalize(_empty_process_result())

    assert result.session_id == "claude-session-1"
    if with_logger:
        assert any("Claude session claude-session-1 started" in m for _, m in logs)


def test_claude_monitor_keeps_the_first_init_session_id() -> None:
    monitor = _ClaudeStreamJsonMonitor(agent_name="Claude planner", log=None)
    second_init = {**CLAUDE_INIT, "session_id": "claude-session-2"}

    monitor.consume_stdout(_lines(CLAUDE_INIT, second_init, CLAUDE_RESULT))

    assert monitor.finalize(_empty_process_result()).session_id == "claude-session-1"


def test_claude_monitor_ignores_non_init_system_events() -> None:
    monitor = _ClaudeStreamJsonMonitor(agent_name="Claude planner", log=None)
    other = {"type": "system", "subtype": "compact", "session_id": "other"}

    monitor.consume_stdout(_lines(other, CLAUDE_RESULT))

    assert monitor.finalize(_empty_process_result()).session_id is None


@pytest.mark.parametrize("with_logger", [False, True])
@pytest.mark.parametrize("session_value", ["missing", None, 42, ["x"]])
def test_claude_monitor_missing_or_non_string_session_id_is_none(
    session_value: object,
    with_logger: bool,
) -> None:
    logs: list[tuple[str, str]] = []
    monitor = _ClaudeStreamJsonMonitor(
        agent_name="Claude planner",
        log=(lambda level, message: logs.append((level, message)))
        if with_logger
        else None,
    )
    init = dict(CLAUDE_INIT)
    if session_value == "missing":
        del init["session_id"]
    else:
        init["session_id"] = session_value

    monitor.consume_stdout(_lines(init, CLAUDE_RESULT))
    result = monitor.finalize(_empty_process_result())

    assert result.session_id is None
    assert result.session_id != "unknown"


def test_invoke_claude_subagent_returns_stream_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _FakeStreamingRunner(
        (_lines(CLAUDE_INIT, CLAUDE_RESULT), _empty_process_result())
    )
    monkeypatch.setattr(claude, "_run_command_streaming", runner)

    result = claude.invoke_claude_subagent("planner", "plan", repo_root=tmp_path)

    assert isinstance(result, SubagentResult)
    assert result.stdout == "done"
    assert result.session_id == "claude-session-1"


def test_claude_buffered_text_fallback_has_no_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unsupported = _ProcessResult(
        stdout="",
        stderr="error: unknown option '--output-format'",
        exit_code=1,
    )
    monkeypatch.setattr(
        claude,
        "_run_command_streaming",
        _FakeStreamingRunner(("", unsupported)),
    )
    buffered = Mock(
        return_value=_ProcessResult(
            stdout="plain text",
            stderr="",
            exit_code=0,
            session_id="should-not-leak",
        )
    )
    monkeypatch.setattr(claude, "_run_logged_agent_command", buffered)

    result = claude.invoke_claude_subagent("planner", "plan", repo_root=tmp_path)

    buffered.assert_called_once()
    assert result.stdout == "plain text"
    assert result.session_id is None


# --- agent_roles pass-through -----------------------------------------------


def test_subagent_result_session_id_defaults_to_none() -> None:
    result = SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.0)

    assert result.session_id is None


def test_dispatch_from_subagent_passes_session_id_through() -> None:
    subagent = SubagentResult(
        stdout="out",
        stderr="",
        exit_code=0,
        elapsed_seconds=1.5,
        session_id="claude-session-1",
    )

    dispatch = agent_roles._dispatch_from_subagent(subagent)

    assert dispatch.session_id == "claude-session-1"
    assert dispatch.session_invalidated is False


def test_claude_role_dispatch_returns_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        claude,
        "_run_command_streaming",
        _FakeStreamingRunner(
            (_lines(CLAUDE_INIT, CLAUDE_RESULT), _empty_process_result())
        ),
    )

    result = agent_roles.invoke_role(
        "planner",
        tmp_path / "HANDOFF.md",
        agent_config=AgentConfig(provider="claude"),
        repo_root=tmp_path,
    )

    assert isinstance(result, DispatchResult)
    assert result.session_id == "claude-session-1"


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_invoke_support_provider_passes_session_id_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    monkeypatch.setattr(
        agent_roles,
        "_invoke_role_provider",
        lambda *args, **kwargs: DispatchResult(
            stdout="report",
            stderr="",
            exit_code=0,
            elapsed_seconds=1.0,
            session_id="provider-session",
        ),
    )

    result = agent_roles._invoke_support_provider(
        "auditor",
        "review this",
        role="auditor",
        handoff_path=tmp_path / "HANDOFF.md",
        agent_config=AgentConfig(provider=provider),
        use_resume=False,
        log=None,
        repo_root=tmp_path,
    )

    assert isinstance(result, SubagentResult)
    assert result.session_id == "provider-session"


# --- Codex ------------------------------------------------------------------


def _codex_stream(thread_id: str | None) -> str:
    events: list[dict[str, object]] = []
    if thread_id is not None:
        events.append({"type": "thread.started", "thread_id": thread_id})
    events.append(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "finished"},
        }
    )
    return _lines(*events)


def test_codex_stateless_run_returns_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        codex,
        "_run_command_streaming",
        _FakeStreamingRunner((_codex_stream("thread-1"), _empty_process_result())),
    )

    result = codex.invoke_codex(tmp_path / "HANDOFF.md", repo_root=tmp_path)

    assert result.session_id == "thread-1"


def test_codex_fresh_managed_run_returns_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _FakeStreamingRunner((_codex_stream("thread-new"), _empty_process_result()))
    monkeypatch.setattr(codex, "_run_command_streaming", runner)

    result = codex.invoke_codex(
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        use_resume=True,
    )

    assert "resume" not in runner.commands[0]
    assert result.session_id == "thread-new"


def test_codex_resumed_managed_run_returns_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = codex._codex_artifact_paths(tmp_path)
    codex._write_codex_session_state(paths, "thread-saved")
    runner = _FakeStreamingRunner(
        (_codex_stream("thread-saved"), _empty_process_result())
    )
    monkeypatch.setattr(codex, "_run_command_streaming", runner)

    result = codex.invoke_codex(
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        use_resume=True,
    )

    assert "resume" in runner.commands[0]
    assert "thread-saved" in runner.commands[0]
    assert result.session_id == "thread-saved"


def test_codex_resume_failed_then_fresh_returns_fresh_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = codex._codex_artifact_paths(tmp_path)
    codex._write_codex_session_state(paths, "thread-gone")
    runner = _FakeStreamingRunner(
        ("", _ProcessResult(stdout="", stderr="Error: session not found", exit_code=1)),
        (_codex_stream("thread-fresh"), _empty_process_result()),
    )
    monkeypatch.setattr(codex, "_run_command_streaming", runner)

    result = codex.invoke_codex(
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        use_resume=True,
    )

    assert len(runner.commands) == 2
    assert "resume" in runner.commands[0]
    assert "resume" not in runner.commands[1]
    assert result.session_id == "thread-fresh"


@pytest.mark.parametrize("use_resume", [False, True])
def test_codex_run_without_thread_started_has_no_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    use_resume: bool,
) -> None:
    monkeypatch.setattr(
        codex,
        "_run_command_streaming",
        _FakeStreamingRunner((_codex_stream(None), _empty_process_result())),
    )

    result = codex.invoke_codex(
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        use_resume=use_resume,
    )

    assert result.session_id is None


# --- failover and resume behavior -------------------------------------------


def test_failover_result_still_has_no_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_text("---\nnext_agent: backend\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        agent_roles,
        "invoke_codex",
        Mock(
            return_value=DispatchResult(
                stdout="",
                stderr="Process timed out after 1200 seconds.",
                exit_code=1,
                elapsed_seconds=1.0,
                session_id="codex-thread",
            )
        ),
    )
    monkeypatch.setattr(
        agent_roles,
        "invoke_claude_subagent",
        Mock(
            return_value=SubagentResult(
                stdout="done",
                stderr="",
                exit_code=0,
                elapsed_seconds=1.0,
                session_id="claude-fallback-session",
            )
        ),
    )

    result = agent_roles.invoke_role(
        "backend",
        handoff,
        agent_config=AgentConfig(
            provider="codex",
            fallback=AgentConfig(provider="claude", model="claude-sonnet-4-6"),
        ),
        repo_root=tmp_path,
    )

    assert result.exit_code == 0
    assert result.session_id is None
    assert result.session_invalidated is True


def test_claude_planner_launch_command_ignores_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _FakeStreamingRunner(
        (_lines(CLAUDE_INIT, CLAUDE_RESULT), _empty_process_result()),
        (_lines(CLAUDE_INIT, CLAUDE_RESULT), _empty_process_result()),
    )
    monkeypatch.setattr(claude, "_run_command_streaming", runner)
    agent_config = AgentConfig(provider="claude")

    agent_roles.invoke_role(
        "planner",
        tmp_path / "HANDOFF.md",
        agent_config=agent_config,
        use_resume=True,
        repo_root=tmp_path,
    )
    agent_roles.invoke_role(
        "planner",
        tmp_path / "HANDOFF.md",
        agent_config=agent_config,
        use_resume=True,
        session_id="claude-session-1",
        previous_handoff_sha="abc1234",
        current_handoff_sha="def5678",
        repo_root=tmp_path,
    )

    assert len(runner.commands) == 2
    assert runner.commands[0] == runner.commands[1]
    assert not any("claude-session-1" in part for part in runner.commands[1])
    assert "--resume" not in runner.commands[1]
