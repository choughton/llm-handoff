from __future__ import annotations

import io
import json
from pathlib import Path

from llm_handoff.agent_providers.claude import _ClaudeStreamJsonMonitor
from llm_handoff.audit_review import _log_claude_json_usage
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
