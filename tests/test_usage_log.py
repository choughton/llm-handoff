from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from llm_handoff import agent_roles
from llm_handoff.agent_providers.antigravity import (
    _AntigravityStreamJsonMonitor,
    _antigravity_usage_fields,
)
from llm_handoff.agent_providers.claude import _ClaudeStreamJsonMonitor
from llm_handoff.agent_providers.codex import _CodexJsonMonitor
from llm_handoff.agent_types import DispatchResult, SubagentResult, _ProcessResult
from llm_handoff.audit_review import _log_claude_json_usage
from llm_handoff.config import AgentConfig
from llm_handoff.logging_util import DispatchLogger
from llm_handoff.usage_log import (
    format_usage_line,
    usage_fields_from_claude_result,
    usage_fields_from_codex_turn,
)


CLAUDE_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "done",
    "stop_reason": "end_turn",
    "total_cost_usd": 0.221,
    "num_turns": 7,
    "duration_ms": 41234,
    "usage": {
        "input_tokens": 222,
        "output_tokens": 44,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 1200,
    },
}


def test_format_usage_line_orders_known_fields_and_skips_missing_ones() -> None:
    line = format_usage_line(
        "Claude auditor",
        "claude",
        {"cost_usd": 0.5, "zeta": 1, "model": "claude-opus-5-5", "num_turns": None},
    )

    assert line == (
        "usage: Claude auditor provider=claude model=claude-opus-5-5 "
        "cost_usd=0.5 zeta=1"
    )


def test_usage_fields_from_claude_result_reads_tokens_cache_and_cost() -> None:
    assert usage_fields_from_claude_result(CLAUDE_RESULT) == {
        "input_tokens": 222,
        "output_tokens": 44,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 1200,
        "cost_usd": 0.221,
        "num_turns": 7,
        "duration_ms": 41234,
    }


def test_usage_fields_from_codex_turn_reads_tokens() -> None:
    event = {
        "type": "turn.completed",
        "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3},
    }

    assert usage_fields_from_codex_turn(event) == {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 3,
    }


def test_claude_monitor_keeps_cost_off_the_agent_line() -> None:
    logs: list[tuple[str, str]] = []
    monitor = _ClaudeStreamJsonMonitor(
        agent_name="Claude auditor",
        log=lambda level, message: logs.append((level, message)),
    )
    init = {
        "type": "system",
        "subtype": "init",
        "session_id": "s-1",
        "model": "claude-opus-5-5",
        "tools": [],
    }

    monitor.consume_stdout(json.dumps(init) + "\n" + json.dumps(CLAUDE_RESULT) + "\n")

    assert ("AGENT", "Claude auditor: Claude result (stop=end_turn)") in logs
    assert not any("cost_usd" in message for level, message in logs if level != "DEBUG")
    debug = [message for level, message in logs if level == "DEBUG"]
    assert debug == [
        "usage: Claude auditor provider=claude model=claude-opus-5-5 "
        "input_tokens=222 output_tokens=44 cache_read_input_tokens=9000 "
        "cache_creation_input_tokens=1200 cost_usd=0.221 num_turns=7 "
        "duration_ms=41234"
    ]


CLAUDE_INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "s-1",
    "model": "claude-opus-5-5",
    "tools": [],
}

CLAUDE_EXPECTED_USAGE = {
    "model": "claude-opus-5-5",
    "input_tokens": 222,
    "output_tokens": 44,
    "cache_read_input_tokens": 9000,
    "cache_creation_input_tokens": 1200,
    "cost_usd": 0.221,
    "num_turns": 7,
    "duration_ms": 41234,
}


def _empty_process_result() -> _ProcessResult:
    return _ProcessResult(stdout="", stderr="", exit_code=0)


@pytest.mark.parametrize("with_logger", [False, True])
def test_claude_monitor_returns_usage_on_finalize(with_logger: bool) -> None:
    logs: list[tuple[str, str]] = []
    monitor = _ClaudeStreamJsonMonitor(
        agent_name="Claude auditor",
        log=(lambda level, message: logs.append((level, message)))
        if with_logger
        else None,
    )

    monitor.consume_stdout(
        json.dumps(CLAUDE_INIT) + "\n" + json.dumps(CLAUDE_RESULT) + "\n"
    )
    result = monitor.finalize(_empty_process_result())

    assert result.stdout == "done"
    assert result.usage == CLAUDE_EXPECTED_USAGE


def test_claude_monitor_without_result_event_has_no_usage() -> None:
    monitor = _ClaudeStreamJsonMonitor(agent_name="Claude auditor", log=None)

    monitor.consume_stdout(json.dumps(CLAUDE_INIT) + "\n")
    result = monitor.finalize(_empty_process_result())

    assert result.usage is None


def test_claude_monitor_result_without_usage_fields_has_no_usage() -> None:
    monitor = _ClaudeStreamJsonMonitor(agent_name="Claude auditor", log=None)

    monitor.consume_stdout(
        json.dumps(CLAUDE_INIT)
        + "\n"
        + json.dumps({"type": "result", "result": "done"})
        + "\n"
    )
    result = monitor.finalize(_empty_process_result())

    assert result.usage is None


def test_codex_monitor_sums_usage_across_turns() -> None:
    monitor = _CodexJsonMonitor(agent_name="Codex", log=None)
    turns = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 4},
        },
    ]

    monitor.consume_stdout("".join(json.dumps(turn) + "\n" for turn in turns))

    assert monitor.usage == {
        "input_tokens": 15,
        "cached_input_tokens": 3,
        "output_tokens": 7,
    }


def test_codex_monitor_logs_each_turn_while_summing() -> None:
    logs: list[tuple[str, str]] = []
    monitor = _CodexJsonMonitor(
        agent_name="Codex",
        log=lambda level, message: logs.append((level, message)),
    )
    turn = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}

    monitor.consume_stdout(json.dumps(turn) + "\n" + json.dumps(turn) + "\n")

    assert [message for level, message in logs if level == "DEBUG"] == [
        "usage: Codex provider=codex input_tokens=10 output_tokens=3",
        "usage: Codex provider=codex input_tokens=10 output_tokens=3",
    ]
    assert monitor.usage == {"input_tokens": 20, "output_tokens": 6}


def test_codex_monitor_without_turns_has_no_usage() -> None:
    monitor = _CodexJsonMonitor(agent_name="Codex", log=None)

    monitor.consume_stdout(
        json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n"
    )

    assert monitor.usage is None


def test_antigravity_monitor_returns_result_usage_on_finalize() -> None:
    logs: list[tuple[str, str]] = []
    monitor = _AntigravityStreamJsonMonitor(
        agent_name="Antigravity auditor",
        log=lambda level, message: logs.append((level, message)),
    )
    usage = {"input_tokens": 10, "output_tokens": 4, "cached_tokens": 2}

    monitor.consume_stdout(
        json.dumps(
            {"type": "result", "status": "SUCCESS", "response": "ok", "usage": usage}
        )
        + "\n"
    )
    result = monitor.finalize(_empty_process_result())

    assert result.usage == _antigravity_usage_fields(usage)
    assert result.usage == {"input_tokens": 10, "output_tokens": 4, "cached_tokens": 2}
    assert [message for level, message in logs if level == "DEBUG"] == [
        "usage: Antigravity auditor provider=antigravity input_tokens=10 "
        "output_tokens=4 cached_tokens=2"
    ]


def test_antigravity_monitor_without_usage_has_none() -> None:
    monitor = _AntigravityStreamJsonMonitor(agent_name="Antigravity auditor", log=None)

    monitor.consume_stdout(
        json.dumps({"type": "result", "status": "SUCCESS", "response": "ok"}) + "\n"
    )
    result = monitor.finalize(_empty_process_result())

    assert result.usage is None


def test_dispatch_from_subagent_passes_usage_through() -> None:
    usage = {"model": "claude-opus-5-5", "input_tokens": 5, "cost_usd": 0.01}
    subagent = SubagentResult(
        stdout="out",
        stderr="",
        exit_code=0,
        elapsed_seconds=1.5,
        usage=usage,
    )

    dispatch = agent_roles._dispatch_from_subagent(subagent)

    assert dispatch.usage == usage
    assert dispatch.stdout == "out"
    assert dispatch.elapsed_seconds == 1.5


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_invoke_support_provider_passes_usage_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    usage = {"input_tokens": 7, "output_tokens": 2}

    def fake_invoke_role_provider(*args: object, **kwargs: object) -> DispatchResult:
        del args, kwargs
        return DispatchResult(
            stdout="report",
            stderr="",
            exit_code=0,
            elapsed_seconds=2.0,
            usage=usage,
        )

    monkeypatch.setattr(agent_roles, "_invoke_role_provider", fake_invoke_role_provider)

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
    assert result.stdout == "report"
    assert result.usage == usage


def test_invoke_support_provider_claude_passes_usage_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = SubagentResult(
        stdout="report",
        stderr="",
        exit_code=0,
        elapsed_seconds=2.0,
        usage=dict(CLAUDE_EXPECTED_USAGE),
    )
    monkeypatch.setattr(
        agent_roles,
        "invoke_claude_subagent",
        lambda *args, **kwargs: expected,
    )

    result = agent_roles._invoke_support_provider(
        "auditor",
        "review this",
        role="auditor",
        handoff_path=tmp_path / "HANDOFF.md",
        agent_config=AgentConfig(provider="claude"),
        use_resume=False,
        log=None,
        repo_root=tmp_path,
    )

    assert result.usage == CLAUDE_EXPECTED_USAGE


def test_security_reviewer_json_usage_is_logged_at_debug() -> None:
    logs: list[tuple[str, str]] = []

    _log_claude_json_usage(
        "Security reviewer (claude)",
        json.dumps(CLAUDE_RESULT),
        ["claude", "--model", "claude-opus-5-5", "-p", "review"],
        lambda level, message: logs.append((level, message)),
    )

    assert len(logs) == 1
    level, message = logs[0]
    assert level == "DEBUG"
    assert message.startswith(
        "usage: Security reviewer (claude) provider=claude model=claude-opus-5-5 "
    )
    assert "cost_usd=0.221" in message


def test_security_reviewer_usage_ignores_non_json_output() -> None:
    logs: list[tuple[str, str]] = []

    _log_claude_json_usage(
        "Security reviewer (claude)",
        "not json",
        ["claude"],
        lambda level, message: logs.append((level, message)),
    )

    assert logs == []


def test_debug_lines_reach_the_log_file_but_not_the_console(tmp_path: Path) -> None:
    console = io.StringIO()
    logger = DispatchLogger(tmp_path, console=console)
    logger.mark_startup_complete()

    logger("DEBUG", "usage: Codex provider=codex input_tokens=10 output_tokens=3")

    assert "usage:" not in console.getvalue()
    assert logger.log_file_path is not None
    assert "usage: Codex provider=codex input_tokens=10 output_tokens=3" in (
        logger.log_file_path.read_text(encoding="utf-8")
    )
