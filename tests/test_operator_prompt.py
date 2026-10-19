"""Operator prompt at dispatcher pauses (`interactive_pause`)."""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable

import pytest

from llm_handoff import config as config_module
from llm_handoff import operator_prompt, orchestrator, publish
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import DispatchConfig
from llm_handoff.operator_prompt import (
    PROMPT_HELP_LINES,
    ROLE_TARGET_INVALID,
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


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "@backend fix the import",
            OperatorAction(kind="role", role="backend", text="fix the import"),
        ),
        (
            "@Frontend   tidy layout",
            OperatorAction(kind="role", role="frontend", text="tidy layout"),
        ),
        ("@auditor", OperatorAction(kind="role", role="auditor", text=None)),
        ("  @AUDITOR   \n", OperatorAction(kind="role", role="auditor", text=None)),
        ("@planner try smaller", OperatorAction(kind="planner", text="try smaller")),
        ("@planner", OperatorAction(kind="planner", text=None)),
    ],
)
def test_role_targets_parse(line: str, expected: OperatorAction) -> None:
    assert parse_operator_input(line) == expected


@pytest.mark.parametrize(
    "line", ["@user x", "@finalizer", "@validator check", "@foo bar", "@", "@ backend x"]
)
def test_unknown_role_targets_are_invalid(line: str) -> None:
    action = parse_operator_input(line)
    assert action == OperatorAction(kind="invalid", text=ROLE_TARGET_INVALID)
    for target in ("@backend", "@frontend", "@auditor", "@planner"):
        assert target in ROLE_TARGET_INVALID


def test_help_lists_role_targeting_before_the_handoff_line() -> None:
    assert "@backend|@frontend|@auditor <message>" in PROMPT_HELP_LINES[-2]
    assert "editing HANDOFF.md" in PROMPT_HELP_LINES[-1]


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


class _FakeConsole:
    """Terminal input that is read only when the reader asks for it.

    ``pending`` holds characters already typed; each ``read_chars`` call
    returns all of them, or the next scripted chunk when nothing is pending.
    """

    def __init__(self, pending: str = "", chunks: list[str] | None = None) -> None:
        self.pending = pending
        self.chunks = list(chunks or [])
        self.reads: list[tuple[float, bool]] = []
        self.discards = 0
        self.eof_after_chunks = False

    def read_chars(self, timeout: float, *, echo: bool = True) -> str:
        self.reads.append((timeout, echo))
        if self.pending:
            chars, self.pending = self.pending, ""
            return chars
        if timeout > 0 and self.chunks:
            return self.chunks.pop(0)
        if timeout > 0 and self.eof_after_chunks:
            raise EOFError
        return ""

    def discard_unread(self) -> None:
        self.discards += 1


class _TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_console_reader_never_reads_outside_a_prompt() -> None:
    # Finding: a reader blocked in readline() between prompts competes with
    # the manual frontend keypress. The console reader starts no thread and
    # reads only inside drain/read_line.
    console = _FakeConsole(chunks=["quit\n"])
    before = {thread.name for thread in threading.enumerate()}
    reader = OperatorInputReader(_TtyStream(""), console=console)

    assert reader.uses_console is True
    reader.start()
    assert reader.started is False
    assert console.reads == []
    assert "llm-handoff-operator-input" not in {
        thread.name for thread in threading.enumerate()
    } - before

    assert reader.read_line(timeout=0.05) == "quit"
    reads_after_prompt = len(console.reads)
    time.sleep(0.02)
    assert len(console.reads) == reads_after_prompt


def test_console_reader_discards_type_ahead_before_the_first_prompt(
    tmp_path: Path,
) -> None:
    # Finding: lines typed while the first agent ran were taken as the first
    # prompt's action. They must be discarded on entry to the prompt.
    path = _handoff(tmp_path)
    console = _FakeConsole(pending="quit\nmore\n", chunks=["retry\n"])
    reader = OperatorInputReader(_TtyStream(""), console=console)
    logs, log = _logs()

    result = wait_for_operator_or_handoff_change(
        reader, path, content_sha(USER_HANDOFF), 0.01, log
    )

    assert result == OperatorAction(kind="retry")
    assert logs[0] == (
        "PAUSE",
        "Discarded 2 operator input line(s) typed before the prompt.",
    )
    assert console.reads[0] == (0, False)
    assert console.discards == 1


def test_console_reader_counts_a_partial_line_as_discarded() -> None:
    console = _FakeConsole(pending="half")
    reader = OperatorInputReader(_TtyStream(""), console=console)

    assert reader.drain() == 1
    assert reader.drain() == 0


def test_console_reader_assembles_lines_across_chunks() -> None:
    console = _FakeConsole(chunks=["plea", "sx\be\r", "\nquit\r\n"])
    reader = OperatorInputReader(_TtyStream(""), console=console)

    assert reader.read_line(timeout=0.5) == "please"
    assert reader.read_line(timeout=0.5) == "quit"
    assert reader.read_line(timeout=0.01) is None


def test_console_reader_detects_eof() -> None:
    console = _FakeConsole(chunks=["tail"])
    console.eof_after_chunks = True
    reader = OperatorInputReader(_TtyStream(""), console=console)

    assert reader.read_line(timeout=0.5) == "tail"
    assert reader.eof_reached is True
    assert reader.read_line(timeout=0.5) is None


class _FakeMsvcrt:
    def __init__(self, keys: str) -> None:
        self.keys = list(keys)
        self.echoed: list[str] = []

    def kbhit(self) -> bool:
        return bool(self.keys)

    def getwch(self) -> str:
        return self.keys.pop(0)

    def putwch(self, char: str) -> None:
        self.echoed.append(char)


def test_windows_console_skips_special_keys_and_echoes_only_at_the_prompt() -> None:
    from llm_handoff.operator_prompt import _WindowsConsole

    fake = _FakeMsvcrt("a\xe0Hb\r")
    console = _WindowsConsole(fake)
    assert console.read_chars(0, echo=False) == "ab\r"
    assert fake.echoed == []

    fake.keys = list("c\r")
    assert console.read_chars(0.01) == "c\r"
    assert "".join(fake.echoed) == "c\r\n"
    assert console.read_chars(0.01) == ""


def test_windows_console_ctrl_c_interrupts() -> None:
    from llm_handoff.operator_prompt import _WindowsConsole

    with pytest.raises(KeyboardInterrupt):
        _WindowsConsole(_FakeMsvcrt("\x03")).read_chars(0)


def test_non_tty_streams_use_the_thread_reader() -> None:
    assert OperatorInputReader(io.StringIO("")).uses_console is False
    assert OperatorInputReader(_BlockingStream()).uses_console is False  # type: ignore[arg-type]


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
        (
            "@backend fix it\n",
            OperatorAction(kind="role", role="backend", text="fix it"),
        ),
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
        OperatorInputReader(io.StringIO("@foo fix it\nquit\n")),
        path,
        content_sha(USER_HANDOFF),
        0.01,
        log,
    )

    assert result == OperatorAction(kind="quit")
    assert ("PAUSE", ROLE_TARGET_INVALID) in logs


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


class _TtyPromptStream(_PromptStream):
    def isatty(self) -> bool:
        return True


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
        stream_isatty: bool = False,
        on_log: Callable[[str, str], None] | None = None,
    ) -> None:
        self.handoff_path = handoff_path
        self.planner_instructions: list[str | None] = []
        self.backend_calls = 0
        self.backend_instructions: list[str | None] = []
        self.frontend_instructions: list[str | None] = []
        self.auditor_prompts: list[str] = []
        self.logs: list[tuple[str, str]] = []
        stream_class = _TtyPromptStream if stream_isatty else _PromptStream
        self.stream = stream_class(operator_lines or [])
        self._on_log = on_log
        self.stream_requests = 0
        planner_queue = list(planner_writes or [])
        backend_queue = list(backend_writes or [])

        def fake_planner(path: Path, **kwargs: Any) -> DispatchResult:
            self.planner_instructions.append(kwargs.get("additional_instruction"))
            if planner_queue:
                path.write_text(planner_queue.pop(0), encoding="utf-8")
            return _dispatch_result()

        def fake_backend(path: Path, **kwargs: Any) -> DispatchResult:
            self.backend_calls += 1
            self.backend_instructions.append(kwargs.get("additional_instruction"))
            if backend_queue:
                content = backend_queue.pop(0)
                if content is not None:
                    path.write_text(content, encoding="utf-8")
            return _dispatch_result()

        def fake_frontend(_path: Path, **kwargs: Any) -> DispatchResult:
            self.frontend_instructions.append(kwargs.get("additional_instruction"))
            return _dispatch_result()

        def fake_support(name: str, prompt: str, **kwargs: Any) -> SubagentResult:
            if name == "auditor" and kwargs.get("role") == "auditor":
                self.auditor_prompts.append(prompt)
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
        monkeypatch.setattr(orchestrator, "invoke_frontend_role", fake_frontend)
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


# --- @<role> targeting ----------------------------------------------------


def test_backend_target_on_user_pause_dispatches_backend_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["@backend fix the import", "quit"],
    )

    assert harness.run(_config(tmp_path)) == 0

    assert harness.backend_calls == 1
    assert harness.planner_instructions == []
    instruction = harness.backend_instructions[0]
    assert instruction is not None
    assert "ESCALATION DETECTED in HANDOFF.md." in instruction
    assert (
        "A person at the dispatcher console dispatched you directly from the "
        "operator prompt." in instruction
    )
    assert "Operator instruction: fix the import" in instruction
    assert "Files in scope" in instruction
    assert instruction.isascii()
    assert (
        "PAUSE",
        "Operator prompt: scheduling a backend dispatch with the operator's message.",
    ) in harness.logs
    assert ("INFO", "Routing source: operator_role_dispatch") in harness.logs


def test_auditor_target_without_message_continues_the_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, operator_lines=["@auditor", "quit"])

    assert harness.run(_config(tmp_path)) == 0

    assert harness.planner_instructions == []
    assert harness.backend_calls == 0
    assert len(harness.auditor_prompts) == 1
    prompt = harness.auditor_prompts[0]
    assert orchestrator.OPERATOR_ROLE_NO_MESSAGE_REQUEST in prompt
    assert "Operator instruction:" not in prompt
    assert (
        "PAUSE",
        "Operator prompt: scheduling a auditor dispatch with no message.",
    ) in harness.logs


def test_auditor_target_in_audit_fan_out_keeps_the_review_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(
        monkeypatch, handoff_path, operator_lines=["@auditor check {x}", "quit"]
    )
    fan_out_calls: list[str] = []

    def fake_fan_out(config: DispatchConfig, **kwargs: Any) -> DispatchResult:
        fan_out_calls.append("fan-out")
        kwargs["quality_invoker"](
            "QUALITY FAN-OUT PROMPT", config.agents["auditor"]
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "run_audit_fan_out", fake_fan_out)
    config = _config(tmp_path, audit={"reviewers": ["quality", "security"]})
    assert config.audit.security_enabled

    assert harness.run(config) == 0

    assert fan_out_calls == ["fan-out"]
    assert len(harness.auditor_prompts) == 1
    prompt = harness.auditor_prompts[0]
    assert prompt.startswith("QUALITY FAN-OUT PROMPT")
    assert "Operator instruction: check {x}" in prompt
    assert "This review stays read-only" in prompt
    assert "return only the structured JSON object" in prompt
    assert "write your normal handback" not in prompt
    assert prompt.isascii()


def test_frontend_target_dispatches_frontend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(
        monkeypatch, handoff_path, operator_lines=["@Frontend tidy layout", "quit"]
    )

    assert harness.run(_config(tmp_path)) == 0

    assert len(harness.frontend_instructions) == 1
    assert "Operator instruction: tidy layout" in (
        harness.frontend_instructions[0] or ""
    )
    assert harness.planner_instructions == []


def test_targeted_backend_cycle_skips_stale_route_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, BACKEND_HANDOFF)
    # Backend never rewrites the handoff: cycle 2 pauses as stale, the
    # targeted cycle repeats backend on the same hash, and only the normal
    # cycle after it pauses as stale again.
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["@backend fix it", "quit"],
    )

    assert harness.run(_config(tmp_path)) == 0

    assert harness.backend_calls == 2
    assert harness.backend_instructions[0] is None
    assert "Operator instruction: fix it" in (harness.backend_instructions[1] or "")
    assert sum(
        message.startswith("STALE ROUTING DETECTED")
        for message in harness.pause_messages()
    ) == 2


def test_role_target_text_with_braces_reaches_the_role_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    text = "keep {handoff_path} and {0} and {{literal}} as typed"
    harness = _Harness(
        monkeypatch, handoff_path, operator_lines=[f"@backend {text}", "quit"]
    )

    assert harness.run(_config(tmp_path)) == 0

    assert f"Operator instruction: {text}" in (harness.backend_instructions[0] or "")


def test_discarded_role_dispatch_names_the_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)

    def edit_after_scheduling(level: str, message: str) -> None:
        if level == "PAUSE" and message.startswith(
            "Operator prompt: scheduling a backend dispatch"
        ):
            handoff_path.write_text(OTHER_USER_HANDOFF, encoding="utf-8")

    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["@backend fix it", "quit"],
        on_log=edit_after_scheduling,
    )

    assert harness.run(_config(tmp_path)) == 0

    assert harness.backend_calls == 0
    warnings = [
        message
        for level, message in harness.logs
        if level == "WARN" and message.startswith("Dropped the scheduled")
    ]
    assert warnings == [
        "Dropped the scheduled operator_role dispatch to backend because "
        "HANDOFF.md changed before it ran; routing the current handoff normally."
    ]


CLAUDE_SCOPE_HANDOFF = """---
next_agent: backend
reason: "Sync the helper files."
producer: planner
---

## Work Packet

- **Objective:** Sync the helper files.
- **Files in scope:** `.agents/agents/x.md`, `examples/app/.claude/agents/x.md`
- **Files out of bounds:** none
- **Context:** none
- **Verification command:** `python -m pytest tests -q`
- **Expected next route:** auditor

## Next Step

- **backend:** Sync the helper files.
"""


def test_backend_target_overrides_the_claude_protected_scope_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, CLAUDE_SCOPE_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["@backend edit only the .agents copy", "quit"],
        backend_writes=[USER_HANDOFF],
    )
    agents = {
        role: agent.model_dump(exclude_none=True)
        for role, agent in config_module._default_agent_configs().items()
    }
    agents["backend"] = {**agents["backend"], "provider": "claude"}

    assert harness.run(_config(tmp_path, agents=agents)) == 0

    assert harness.backend_calls == 1
    pauses = [message for level, message in harness.logs if level == "PAUSE"]
    assert pauses[0].startswith("CLAUDE PROTECTED PATHS IN SCOPE: the backend role")
    instruction = harness.backend_instructions[0]
    assert instruction is not None
    assert "CLAUDE PROTECTED PATHS IN SCOPE" in instruction
    assert "Operator instruction: edit only the .agents copy" in instruction


STORY_BRANCH = "llm-handoff/w9.9-add-a-widget"
STORY_ASSIGNMENT = """---
next_agent: backend
reason: "Build the widget."
epic_id: E1
story_id: W9.9
story_title: Add a widget
producer: planner
---

## Work Packet

- **Objective:** Add the widget module.
- **Files in scope:** `src/widget.py`
- **Files out of bounds:** none
- **Context:** none
- **Verification command:** `python -c "print(1)"`
- **Expected next route:** auditor
"""
STORY_HANDBACK = """---
next_agent: auditor
reason: "Widget built."
epic_id: E1
story_id: W9.9
story_title: Add a widget
producer: backend
status: ready_for_review
---

## Summary

Built the widget.
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _pr_config(repo_root: Path) -> DispatchConfig:
    return _config(
        repo_root,
        commit_mode="dispatcher",
        enforce_work_packet=True,
        work_packet_opt_out_reason=None,
        publish={"mode": "pr", "wait_for_reviewers": ["codex"]},
    )


def _git_repo(tmp_path: Path, handoff_content: str) -> Path:
    handoff_path = _write_repo(tmp_path, handoff_content)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "llm-handoff tests")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / ".gitignore").write_text(
        "logs/\ndocs/handoff/HANDOFF.md\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return handoff_path


def test_backend_target_after_a_protected_file_escalation_continues_the_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The backend started the story, then a protected file changed during its
    # run: the dispatcher wrapped the handback in an escalation whose own
    # frontmatter has no story_id.
    handoff_path = _git_repo(tmp_path, STORY_ASSIGNMENT)
    assert (
        publish.ensure_story_branch(
            repo_root=tmp_path,
            prefix="llm-handoff/",
            base_branch=None,
            assignment_content=STORY_ASSIGNMENT,
        )
        is None
    )
    config = _pr_config(tmp_path)
    handoff_path.write_text(STORY_HANDBACK, encoding="utf-8")
    orchestrator._write_protected_file_escalation(
        config,
        role_label="backend",
        violations=(".claude/agents/handoff-router.md",),
        error="protected_file_modified: backend changed protected control files",
    )
    escalation = handoff_path.read_text(encoding="utf-8")
    assert "story_id" not in escalation.split("---", 2)[1]
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["@backend keep the router edit out of scope", "quit"],
        backend_writes=[USER_HANDOFF],
    )

    assert harness.run(config) == 0

    assert harness.backend_calls == 1
    assert _git(tmp_path, "branch", "--show-current") == STORY_BRANCH
    assert not any("Story branch not ready" in message for _, message in harness.logs)


def test_backend_target_without_a_story_pauses_instead_of_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No saved story is checked out, so the operator's backend dispatch has no
    # branch to run on. The dispatcher pauses again rather than exiting.
    handoff_path = _git_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["@backend fix the import", "quit"],
    )

    assert harness.run(_pr_config(tmp_path)) == 0

    assert harness.backend_calls == 0
    assert _git(tmp_path, "branch", "--show-current") == "main"
    pauses = harness.pause_messages()
    assert any(
        message.startswith("STORY BRANCH NOT READY:")
        and "publish needs a story_id" in message
        for message in pauses
    )
    assert harness.prompt_count() == 2


def test_tty_without_console_adapter_keeps_the_old_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        operator_lines=["quit"],
        stream_isatty=True,
    )
    monkeypatch.setattr(operator_prompt, "_console_for_stream", lambda _stream: None)
    readers: list[OperatorInputReader] = []

    class _RecordingReader(OperatorInputReader):
        def __init__(self, stream: Any) -> None:
            super().__init__(stream)
            readers.append(self)

    monkeypatch.setattr(orchestrator, "OperatorInputReader", _RecordingReader)
    sleeps: list[float] = []

    def stop_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise _StopPolling

    monkeypatch.setattr(orchestrator.time, "sleep", stop_sleep)

    with pytest.raises(_StopPolling):
        harness.run(_config(tmp_path))

    assert len(readers) == 1
    assert readers[0].console_unavailable is True
    assert readers[0].uses_console is False
    assert readers[0].started is False
    assert (
        "WARN",
        "Operator prompt unavailable: no console input for this terminal; "
        "waiting for HANDOFF.md to change.",
    ) in harness.logs
    assert sleeps == [1]
    assert harness.prompt_count() == 0
    assert harness.pause_messages()[-1] == (
        "Waiting for HANDOFF.md to change; polling every 1s."
    )


def test_non_tty_stream_is_not_console_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operator_prompt, "_console_for_stream", lambda _stream: None)
    reader = OperatorInputReader(io.StringIO("line\n"))

    assert reader.console_unavailable is False
    assert reader.read_line(timeout=0.5) == "line"
    assert reader.started is True


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
