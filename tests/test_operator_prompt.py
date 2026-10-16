"""Operator prompt at dispatcher pauses (`interactive_pause`)."""

from __future__ import annotations

import io
from pathlib import Path
import threading
import time
from typing import Any, Callable

import pytest

from llm_handoff import orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import DispatchConfig
from llm_handoff.operator_prompt import (
    PROMPT_HELP_LINES,
    HandoffChanged,
    OperatorAction,
    OperatorInputReader,
    content_sha,
    parse_operator_input,
    wait_for_operator_or_handoff_change,
)
from llm_handoff.validator import ValidationResult


NO_ROUTE_HANDOFF = """# No routing data

There is no next step in this file.
"""
BACKEND_HANDOFF = """## Next Step

- **backend:** Implement Story 1.
"""
PLANNER_HANDOFF = """## Next Step

- **planner:** Scope the next epic.
"""
USER_HANDOFF = """---
next_agent: user
reason: "Human decision required."
status: escalate_to_user
producer: planner
---

## Question

Which story should run next?
"""
OTHER_USER_HANDOFF = USER_HANDOFF.replace("Which story", "Which epic")
PROMPT_LAST_LINE = PROMPT_HELP_LINES[-1]
PLANNER_SCHEDULED_PREFIX = "Operator prompt: scheduling a planner dispatch"


# --- parse_operator_input -------------------------------------------------


@pytest.mark.parametrize("line", ["", "   ", "\n", " \t \n"])
def test_empty_input_is_best_judgment_planner(line: str) -> None:
    assert parse_operator_input(line) == OperatorAction(kind="planner", text=None)


@pytest.mark.parametrize(
    ("line", "kind"),
    [("retry", "retry"), ("RETRY", "retry"), (" Retry \n", "retry"),
     ("quit", "quit"), ("QUIT", "quit")],
)
def test_retry_and_quit_match_case_insensitively(line: str, kind: str) -> None:
    action = parse_operator_input(line)
    assert action.kind == kind
    assert action.text is None


def test_role_targeting_is_invalid_for_now() -> None:
    action = parse_operator_input("@backend fix it")
    assert action.kind == "invalid"
    assert action.text is not None
    assert "not available yet" in action.text


def test_free_text_is_planner_guidance() -> None:
    action = parse_operator_input("  route to backend for story 2  \n")
    assert action == OperatorAction(kind="planner", text="route to backend for story 2")


# --- OperatorInputReader --------------------------------------------------


def _wait_for_eof(reader: OperatorInputReader) -> None:
    deadline = time.monotonic() + 1.0
    while not reader.eof_reached and time.monotonic() < deadline:
        time.sleep(0.005)
    assert reader.eof_reached


def test_reader_returns_lines_then_detects_eof() -> None:
    reader = OperatorInputReader(io.StringIO("first\nsecond\r\n"))

    assert reader.started is False
    assert reader.read_line(timeout=0.5) == "first"
    assert reader.read_line(timeout=0.5) == "second"
    assert reader.read_line(timeout=0.5) is None
    assert reader.eof_reached is True
    assert reader.read_line(timeout=0.5) is None


def test_reader_drain_discards_type_ahead() -> None:
    reader = OperatorInputReader(io.StringIO("typed\nahead\n"))
    assert reader.drain() == 0
    reader.start()
    _wait_for_eof(reader)

    assert reader.drain() == 2
    assert reader.drain() == 0
    assert reader.read_line(timeout=0.05) is None


def test_reader_starts_one_thread() -> None:
    reader = OperatorInputReader(io.StringIO(""))
    reader.start()
    first = reader._thread
    reader.start()
    assert reader._thread is first


# --- wait_for_operator_or_handoff_change ----------------------------------


def test_content_sha_matches_the_orchestrator_hash() -> None:
    for content in (USER_HANDOFF, "\ufeff" + USER_HANDOFF, ""):
        assert content_sha(content) == orchestrator._content_sha(content)


class _BlockingStream:
    """A stream whose readline blocks until it is closed."""

    def __init__(self) -> None:
        self._closed = threading.Event()

    def readline(self) -> str:
        self._closed.wait(timeout=5)
        return ""

    def close(self) -> None:
        self._closed.set()


def _handoff(tmp_path: Path, content: str = USER_HANDOFF) -> Path:
    path = tmp_path / "HANDOFF.md"
    path.write_text(content, encoding="utf-8")
    return path


def _logs() -> tuple[list[tuple[str, str]], Callable[[str, str], None]]:
    logs: list[tuple[str, str]] = []
    return logs, lambda level, message: logs.append((level, message))


def test_wait_returns_handoff_changed_when_file_is_rewritten(tmp_path: Path) -> None:
    path = _handoff(tmp_path)
    paused_sha = content_sha(USER_HANDOFF)
    path.write_text(OTHER_USER_HANDOFF, encoding="utf-8")
    stream = _BlockingStream()
    logs, log = _logs()
    try:
        result = wait_for_operator_or_handoff_change(
            OperatorInputReader(stream), path, paused_sha, 0.01, log
        )
    finally:
        stream.close()

    assert isinstance(result, HandoffChanged)
    assert [message for _level, message in logs] == list(PROMPT_HELP_LINES)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\n", OperatorAction(kind="planner", text=None)),
        ("please split the story\n", OperatorAction(kind="planner", text="please split the story")),
        ("retry\n", OperatorAction(kind="retry")),
        ("quit\n", OperatorAction(kind="quit")),
    ],
)
def test_wait_returns_each_action_kind(
    tmp_path: Path, text: str, expected: OperatorAction
) -> None:
    path = _handoff(tmp_path)
    _logs_list, log = _logs()

    result = wait_for_operator_or_handoff_change(
        OperatorInputReader(io.StringIO(text)),
        path,
        content_sha(USER_HANDOFF),
        0.01,
        log,
    )

    assert result == expected


def test_wait_logs_and_skips_invalid_actions(tmp_path: Path) -> None:
    path = _handoff(tmp_path)
    logs, log = _logs()

    result = wait_for_operator_or_handoff_change(
        OperatorInputReader(io.StringIO("@backend fix it\nquit\n")),
        path,
        content_sha(USER_HANDOFF),
        0.01,
        log,
    )

    assert result == OperatorAction(kind="quit")
    assert any(
        level == "PAUSE" and "not available yet" in message
        for level, message in logs
    )


def test_wait_keeps_polling_the_file_after_eof(tmp_path: Path) -> None:
    path = _handoff(tmp_path)
    paused_sha = content_sha(USER_HANDOFF)
    calls: list[int] = []

    def hash_content(content: str) -> str:
        calls.append(1)
        return paused_sha if len(calls) < 5 else "changed"

    logs, log = _logs()
    result = wait_for_operator_or_handoff_change(
        OperatorInputReader(io.StringIO("")),
        path,
        paused_sha,
        0.01,
        log,
        hash_content=hash_content,
    )

    assert isinstance(result, HandoffChanged)
    assert len(calls) == 5
    assert (
        "PAUSE",
        "Operator input closed; waiting for HANDOFF.md to change.",
    ) in logs


def test_wait_discards_type_ahead_and_says_how_many(tmp_path: Path) -> None:
    path = _handoff(tmp_path)
    reader = OperatorInputReader(io.StringIO("typed\nquit\n"))
    reader.start()
    _wait_for_eof(reader)
    paused_sha = content_sha(USER_HANDOFF)
    calls: list[int] = []

    def hash_content(_content: str) -> str:
        calls.append(1)
        return paused_sha if len(calls) < 2 else "changed"

    logs, log = _logs()
    result = wait_for_operator_or_handoff_change(
        reader, path, paused_sha, 0.01, log, hash_content=hash_content
    )

    assert isinstance(result, HandoffChanged)
    assert logs[0] == (
        "PAUSE",
        "Discarded 2 operator input line(s) typed before the prompt.",
    )


# --- run_loop integration -------------------------------------------------


class _PromptStream:
    """Gives the reader one scripted line each time the prompt is shown."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._release = threading.Semaphore(0)
        self._closed = False

    def on_log(self, level: str, message: str) -> None:
        if level == "PAUSE" and message == PROMPT_LAST_LINE:
            self._release.release()

    def readline(self) -> str:
        if not self._release.acquire(timeout=5):
            return ""
        if self._closed or not self._lines:
            return ""
        return self._lines.pop(0) + "\n"

    def close(self) -> None:
        self._closed = True
        self._release.release()


class _StopPolling(Exception):
    pass


def _config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "poll_interval_seconds": 1,
        "pause_recovery": "off",
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        "enforce_work_packet": False,
        "work_packet_opt_out_reason": "Tests cover routing without the gate.",
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _write_repo(tmp_path: Path, handoff_content: str) -> Path:
    handoff_path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(handoff_content, encoding="utf-8")
    (tmp_path / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("dispatch test fixture\n", encoding="utf-8")
    return handoff_path


def _dispatch_result() -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


class _Harness:
    """Stubbed agents and a scripted operator for one run_loop call."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        handoff_path: Path,
        *,
        operator_lines: list[str] | None,
        planner_writes: list[str] | None = None,
        backend_writes: list[str | None] | None = None,
        tty: bool = True,
        on_log: Callable[[str, str], None] | None = None,
    ) -> None:
        self.handoff_path = handoff_path
        self.planner_instructions: list[str | None] = []
        self.backend_calls = 0
        self.logs: list[tuple[str, str]] = []
        self.stream = _PromptStream(operator_lines or [])
        self._on_log = on_log
        self.stream_requests = 0
        planner_queue = list(planner_writes or [])
        backend_queue = list(backend_writes or [])

        def fake_planner(path: Path, **kwargs: Any) -> DispatchResult:
            self.planner_instructions.append(kwargs.get("additional_instruction"))
            if planner_queue:
                path.write_text(planner_queue.pop(0), encoding="utf-8")
            return _dispatch_result()

        def fake_backend(path: Path, **_kwargs: Any) -> DispatchResult:
            self.backend_calls += 1
            if backend_queue:
                content = backend_queue.pop(0)
                if content is not None:
                    path.write_text(content, encoding="utf-8")
            return _dispatch_result()

        def fake_support(_name: str, _prompt: str, **_kwargs: Any) -> SubagentResult:
            return SubagentResult(
                stdout="VALID: NO\n  ROUTING: FAIL - diagnosis fixture\n",
                stderr="",
                exit_code=0,
                elapsed_seconds=0.01,
            )

        def fake_stream() -> _PromptStream:
            self.stream_requests += 1
            return self.stream

        monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
        monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
        monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
        monkeypatch.setattr(
            orchestrator,
            "validate_handoff",
            lambda *_a, **_k: ValidationResult(verdict="YES", warnings=[], errors=[]),
        )
        monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: tty)
        monkeypatch.setattr(orchestrator, "_operator_input_stream", fake_stream)

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))
        if self._on_log is not None:
            self._on_log(level, message)
        self.stream.on_log(level, message)

    def run(self, config: DispatchConfig, **kwargs: Any) -> int:
        try:
            return orchestrator.run_loop(config, log=self.log, **kwargs)
        finally:
            self.stream.close()

    def pause_messages(self) -> list[str]:
        return [message for level, message in self.logs if level == "PAUSE"]

    def prompt_count(self) -> int:
        return self.pause_messages().count(PROMPT_LAST_LINE)


def test_enter_on_user_pause_dispatches_planner_with_best_judgment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["", "quit"],
        planner_writes=[USER_HANDOFF],
    )

    assert harness.run(_config(tmp_path)) == 0

    assert len(harness.planner_instructions) == 1
    instruction = harness.planner_instructions[0]
    assert instruction is not None
    assert "ESCALATION DETECTED in HANDOFF.md." in instruction
    assert "A person at the dispatcher console sent this instruction" in instruction
    assert orchestrator.OPERATOR_BEST_JUDGMENT_REQUEST in instruction
    assert "escalate_to_user" in instruction
    assert "Do not modify source, tests, project-state files" in instruction
    assert instruction.isascii()
    assert harness.stream_requests == 1
    assert harness.prompt_count() == 2
    assert (
        "PAUSE",
        "The operator stopped the dispatcher from the prompt.",
    ) in harness.logs


def test_free_text_on_unknown_route_reaches_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["route this to backend for story 3", "quit"],
        planner_writes=[USER_HANDOFF],
    )

    assert harness.run(_config(tmp_path)) == 0

    assert len(harness.planner_instructions) == 1
    instruction = harness.planner_instructions[0]
    assert instruction is not None
    assert "No routing instruction found in HANDOFF.md." in instruction
    assert "Operator guidance: route this to backend for story 3" in instruction
    assert orchestrator.OPERATOR_BEST_JUDGMENT_REQUEST not in instruction


def test_operator_text_with_braces_reaches_planner_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    text = "keep {handoff_path} and {0} and {{literal}} as typed"
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=[text, "quit"],
        planner_writes=[USER_HANDOFF],
    )

    assert harness.run(_config(tmp_path)) == 0

    instruction = harness.planner_instructions[0]
    assert instruction is not None
    assert f"Operator guidance: {text}" in instruction


def test_quit_returns_zero_without_dispatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, operator_lines=["quit"])

    assert harness.run(_config(tmp_path)) == 0

    assert harness.planner_instructions == []
    assert harness.backend_calls == 0
    pauses = harness.pause_messages()
    # The PAUSE reason comes before the prompt help.
    reason_index = pauses.index(
        "No routing instruction found in HANDOFF.md. Update the handoff; "
        "dispatch will resume after the file changes."
    )
    assert reason_index < pauses.index(PROMPT_HELP_LINES[0])


def test_retry_on_stale_route_redispatches_same_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, BACKEND_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["retry", "quit"],
        backend_writes=[None, None],
    )

    assert harness.run(_config(tmp_path)) == 0

    assert harness.backend_calls == 2
    assert harness.planner_instructions == []
    assert sum(
        message.startswith("STALE ROUTING DETECTED")
        for message in harness.pause_messages()
    ) == 2
    assert (
        "PAUSE",
        "The operator asked to retry; re-running the cycle.",
    ) in harness.logs


def test_operator_planner_cycle_skips_stale_route_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, PLANNER_HANDOFF)
    # The planner never rewrites the handoff, so every planner cycle repeats
    # the same route on unchanged content.
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["try a smaller story", "quit"],
    )

    assert harness.run(_config(tmp_path)) == 0

    assert len(harness.planner_instructions) == 2
    assert harness.planner_instructions[0] is None
    operator_instruction = harness.planner_instructions[1]
    assert operator_instruction is not None
    assert "Operator guidance: try a smaller story" in operator_instruction
    # One stale pause after the first planner cycle, one after the operator
    # planner cycle; none before the operator planner cycle ran.
    assert sum(
        message.startswith("STALE ROUTING DETECTED")
        for message in harness.pause_messages()
    ) == 2


def test_prompt_follows_the_automatic_pause_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["quit"],
        planner_writes=["# Planner still left no route\n"],
    )

    assert harness.run(_config(tmp_path, pause_recovery="planner")) == 0

    assert len(harness.planner_instructions) == 1
    assert "bounded planner recovery" in (harness.planner_instructions[0] or "")
    pauses = harness.pause_messages()
    assert harness.prompt_count() == 1
    recovery_index = next(
        index
        for index, message in enumerate(pauses)
        if "scheduling one bounded planner recovery" in message
    )
    assert recovery_index < pauses.index(PROMPT_HELP_LINES[0])


@pytest.mark.parametrize(
    ("overrides", "tty"),
    [
        ({"interactive_pause": False}, True),
        ({}, False),
        ({"dry_run": True}, True),
    ],
    ids=["interactive_pause_false", "non_tty", "dry_run"],
)
def test_non_interactive_conditions_keep_the_old_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    tty: bool,
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, operator_lines=["quit"], tty=tty)
    sleeps: list[float] = []

    def stop_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise _StopPolling

    monkeypatch.setattr(orchestrator.time, "sleep", stop_sleep)

    with pytest.raises(_StopPolling):
        harness.run(_config(tmp_path, **overrides))

    assert sleeps == [1]
    assert harness.stream_requests == 0
    assert harness.prompt_count() == 0
    assert harness.pause_messages()[-1] == (
        "Waiting for HANDOFF.md to change; polling every 1s."
    )


def test_zero_poll_interval_keeps_the_old_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, operator_lines=["quit"])

    assert harness.run(_config(tmp_path, poll_interval_seconds=0)) == 0

    assert harness.stream_requests == 0
    assert harness.prompt_count() == 0
    assert harness.planner_instructions == []


def test_discarded_operator_recovery_logs_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)

    def edit_after_scheduling(level: str, message: str) -> None:
        if level == "PAUSE" and message.startswith(PLANNER_SCHEDULED_PREFIX):
            handoff_path.write_text(OTHER_USER_HANDOFF, encoding="utf-8")

    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["", "quit"],
        on_log=edit_after_scheduling,
    )

    assert harness.run(_config(tmp_path)) == 0

    assert harness.planner_instructions == []
    warnings = [
        message
        for level, message in harness.logs
        if level == "WARN" and message.startswith("Dropped the scheduled")
    ]
    assert warnings == [
        "Dropped the scheduled operator planner recovery because HANDOFF.md "
        "changed before it ran; routing the current handoff normally."
    ]


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [
        (True, "Interactive pause:  enabled"),
        (False, "Interactive pause:  disabled"),
    ],
)
def test_startup_banner_shows_interactive_pause(
    tmp_path: Path, enabled: bool, expected: str
) -> None:
    logs, log = _logs()

    orchestrator._log_startup_banner(
        _config(tmp_path, interactive_pause=enabled), log
    )

    banner = [
        message for _level, message in logs if message.startswith("Interactive pause:")
    ]
    assert len(banner) == 1
    assert banner[0].startswith(expected)
    assert ("only when stdin is a TTY" in banner[0]) is enabled
