"""Saved chat messages hold the dispatch loop at its safe points.

The loop holds while ``logs/dispatch/chat-queue.json`` has messages and
resumes only when the operator presses Enter, which clears the queue. EOF on
the operator stream or a stop request ends the run and keeps the queue.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Callable

import pytest

from llm_handoff import chat_session, console_interrupt, ledger, orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.chat_queue import (
    ChatMessage,
    append_chat_messages,
    chat_queue_path,
)
from llm_handoff.config import DispatchConfig
from llm_handoff.publish import PublishOutcome
from llm_handoff.stop_request import request_stop
from llm_handoff.validator import ValidationResult


STORY_ID = "CHAT-T1"
USER_HANDOFF = """---
next_agent: user
reason: "Human decision required."
status: escalate_to_user
producer: planner
---

## Question

Which story should run next?
"""
STOPPED_LINE = (
    "Stopped at the operator's request before the next dispatch. "
    "Exiting with code 0."
)
PROMPT_LINE = ("PAUSE", "Press Enter to continue")
CONTINUING_LINE = ("PAUSE", "Continuing.")


def _claim(*, status: str, producer: str, next_agent: str) -> str:
    sha = "a" * 40
    return f"""---
next_agent: {next_agent}
reason: "Backend work complete; audit requested."
epic_id: CHAT-T
story_id: {STORY_ID}
story_title: Change the value
status: {status}
scope_sha: {sha}
close_type: story
producer: {producer}
---

# Handback

## Verification Evidence

- **Commands run:** `python -m pytest tests/test_app.py -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {sha}
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    )


def _git_repo(tmp_path: Path, handoff: str = USER_HANDOFF) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "llm-handoff tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text("/logs/dispatch/\n", encoding="utf-8")
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(handoff, encoding="utf-8", newline="\n")
    (repo / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "poll_interval_seconds": 0,
        "pause_recovery": "off",
        "interactive_pause": True,
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        "enforce_work_packet": False,
        "work_packet_opt_out_reason": "Tests cover routing without the gate.",
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _pr_config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    return _config(
        repo_root,
        commit_mode="dispatcher",
        enforce_work_packet=True,
        work_packet_opt_out_reason=None,
        publish={"mode": "pr"},
        **overrides,
    )


def _plant(repo_root: Path, *messages: ChatMessage) -> Path:
    path = chat_queue_path(repo_root)
    append_chat_messages(
        path,
        messages
        or (ChatMessage(text="Why this file?", role="backend", received_at="t1"),),
    )
    return path


class FakeReader:
    """Operator input: each read_line returns the next scripted value.

    ``None`` is a timeout; ``EOF`` closes the stream.
    """

    EOF = object()

    def __init__(
        self,
        script: list[object] | None = None,
        *,
        console_unavailable: bool = False,
        pending: int = 0,
        on_read: Callable[[], None] | None = None,
    ) -> None:
        self.script = list(script or [])
        self.console_unavailable = console_unavailable
        self.eof_reached = False
        self.pending = pending
        self.reads: list[float] = []
        self.drains = 0
        self.on_read = on_read

    def drain(self) -> int:
        self.drains += 1
        discarded, self.pending = self.pending, 0
        return discarded

    def read_line(self, timeout: float) -> str | None:
        self.reads.append(timeout)
        if self.on_read is not None:
            self.on_read()
        if not self.script:
            pytest.fail("the hold read more input than the test scripted")
        item = self.script.pop(0)
        if item is FakeReader.EOF:
            self.eof_reached = True
            return None
        return item  # type: ignore[return-value]


def _prompt_state(reader: FakeReader) -> orchestrator._OperatorPromptState:
    state = orchestrator._OperatorPromptState()
    state._reader = reader  # type: ignore[assignment]
    return state


def _hold(
    config: DispatchConfig,
    reader: FakeReader,
    logs: list[tuple[str, str]],
) -> bool:
    return orchestrator._hold_for_chat_messages(
        config,
        lambda level, text: logs.append((level, text)),
        _prompt_state(reader),
        orchestrator._StopRequestWatch(config.repo_root),
    )


@pytest.fixture(autouse=True)
def _clean_console_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    console_interrupt.CONTROLLER.reset()
    yield
    chat_session.detach()
    console_interrupt.CONTROLLER.reset()


def _no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "invoke_backend_role",
        "invoke_frontend_role",
        "invoke_planner_role",
        "invoke_auditor_role",
        "invoke_finalizer_role",
        "invoke_support_role",
    ):
        if hasattr(orchestrator, name):
            monkeypatch.setattr(
                orchestrator,
                name,
                lambda *_a, **_k: pytest.fail("no role may be dispatched"),
            )


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_missing_queue_returns_true_with_no_output(tmp_path: Path) -> None:
    reader = FakeReader(console_unavailable=True)
    logs: list[tuple[str, str]] = []

    assert _hold(_config(tmp_path), reader, logs) is True

    assert logs == []
    assert reader.reads == []
    assert not chat_queue_path(tmp_path).exists()


def test_empty_queue_returns_true_with_no_output_or_file_change(
    tmp_path: Path,
) -> None:
    path = chat_queue_path(tmp_path)
    path.parent.mkdir(parents=True)
    content = '{\n  "version": 1,\n  "messages": []\n}\n'
    path.write_text(content, encoding="utf-8")
    reader = FakeReader()
    logs: list[tuple[str, str]] = []

    assert _hold(_config(tmp_path), reader, logs) is True

    assert logs == []
    assert reader.reads == []
    assert path.read_text(encoding="utf-8") == content


def test_dry_run_warns_once_and_leaves_the_file(tmp_path: Path) -> None:
    path = _plant(
        tmp_path,
        ChatMessage(text="one", role="backend", received_at="t1"),
        ChatMessage(text="two", role=None, received_at="t2"),
    )
    before = path.read_bytes()
    reader = FakeReader()
    logs: list[tuple[str, str]] = []

    assert _hold(_config(tmp_path, dry_run=True), reader, logs) is True

    assert logs == [
        (
            "WARN",
            f"A real run would hold here for 2 saved chat message(s) in {path}.",
        )
    ]
    assert reader.reads == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("cause", ["config", "stdin", "console"])
def test_no_interactive_console_warns_and_leaves_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    path = _plant(tmp_path)
    before = path.read_bytes()
    reader = FakeReader(console_unavailable=cause == "console")
    if cause == "stdin":
        monkeypatch.setattr(
            orchestrator, "_operator_stdin_is_interactive", lambda: False
        )
    config = _config(tmp_path, interactive_pause=cause != "config")
    logs: list[tuple[str, str]] = []

    assert _hold(config, reader, logs) is True

    assert len(logs) == 1
    level, text = logs[0]
    assert level == "WARN"
    assert text.startswith(f"1 saved chat message(s) in {path};")
    assert "does not hold" in text
    assert "without an interactive console" in text
    assert reader.reads == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("answer", ["", "anything at all"])
def test_enter_clears_the_queue_and_continues(tmp_path: Path, answer: str) -> None:
    path = _plant(
        tmp_path,
        ChatMessage(text="Why this file?", role="backend", received_at="t1"),
        ChatMessage(text="And this one?", role=None, received_at="t2"),
    )
    reader = FakeReader([None, answer], pending=2)
    logs: list[tuple[str, str]] = []

    assert _hold(_config(tmp_path, poll_interval_seconds=0), reader, logs) is True

    assert not path.exists()
    assert logs == [
        ("PAUSE", "Holding for 2 chat message(s):"),
        ("PAUSE", "[backend] Why this file?"),
        ("PAUSE", "[no role] And this one?"),
        ("PAUSE", "Discarded 2 operator input line(s) typed before the prompt."),
        PROMPT_LINE,
        CONTINUING_LINE,
    ]
    assert reader.drains == 1
    # The poll timeout never drops below 0.1s, even with polling off.
    assert reader.reads == [0.1, 0.1]


def test_poll_interval_sets_the_read_timeout(tmp_path: Path) -> None:
    _plant(tmp_path)
    reader = FakeReader([""])
    logs: list[tuple[str, str]] = []

    assert _hold(_config(tmp_path, poll_interval_seconds=3), reader, logs) is True

    assert reader.reads == [3]
    assert not any("Discarded" in text for _level, text in logs)


def test_eof_returns_false_and_keeps_the_file(tmp_path: Path) -> None:
    path = _plant(tmp_path)
    before = path.read_bytes()
    reader = FakeReader([None, FakeReader.EOF])
    logs: list[tuple[str, str]] = []

    assert _hold(_config(tmp_path), reader, logs) is False

    assert path.read_bytes() == before
    assert PROMPT_LINE in logs
    assert CONTINUING_LINE not in logs


def test_stop_request_returns_false_and_keeps_the_file(tmp_path: Path) -> None:
    path = _plant(tmp_path)
    before = path.read_bytes()
    reader = FakeReader([None], on_read=lambda: request_stop(tmp_path))
    logs: list[tuple[str, str]] = []

    assert _hold(_config(tmp_path), reader, logs) is False

    assert path.read_bytes() == before
    assert ("PAUSE", STOPPED_LINE) in logs
    assert CONTINUING_LINE not in logs


def test_message_control_characters_are_escaped(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        ChatMessage(
            text="\x1b[2Jfake\nPAUSE line‮",
            role="back\x07end",
            received_at="t1",
        ),
    )
    reader = FakeReader([""])
    logs: list[tuple[str, str]] = []

    assert _hold(_config(tmp_path), reader, logs) is True

    assert logs[1] == (
        "PAUSE",
        "[back\\x07end] \\x1b[2Jfake\\x0aPAUSE line\\u202e",
    )


def test_queue_path_control_characters_are_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo‮"
    root.mkdir()
    _plant(root)
    logs: list[tuple[str, str]] = []

    assert _hold(_config(root, dry_run=True), FakeReader(), logs) is True

    assert "‮" not in logs[0][1]
    assert "\\u202e" in logs[0][1]


class _FakeChatLine:
    def __init__(self) -> None:
        self.roles: list[str | None] = []

    def set_role(self, role: str | None) -> None:
        self.roles.append(role)


class _FakeChatInput:
    def __init__(self) -> None:
        self.running = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


@pytest.mark.parametrize("answer", ["", FakeReader.EOF])
def test_console_and_chat_roles_are_restored_after_the_hold(
    tmp_path: Path, answer: object
) -> None:
    _plant(tmp_path)
    chat_line = _FakeChatLine()
    chat_input = _FakeChatInput()
    chat_session.attach(chat_line, chat_input)  # type: ignore[arg-type]
    console_interrupt.set_active_role("auditor")
    chat_session.set_role("auditor")
    seen: list[tuple[str | None, str | None, bool]] = []

    def record() -> None:
        seen.append(
            (
                console_interrupt.CONTROLLER.active_role,
                chat_session.SESSION.role,
                chat_input.running,
            )
        )

    reader = FakeReader([answer], on_read=record)

    _hold(_config(tmp_path), reader, [])

    # During the hold Ctrl+C stops at once and chat capture is off.
    assert seen == [(None, None, False)]
    assert console_interrupt.CONTROLLER.active_role == "auditor"
    assert chat_session.SESSION.role == "auditor"
    assert chat_input.running is True


def test_console_role_is_restored_when_the_hold_raises(tmp_path: Path) -> None:
    _plant(tmp_path)
    console_interrupt.set_active_role("planner")

    def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _hold(_config(tmp_path), FakeReader([""], on_read=interrupt), [])

    assert console_interrupt.CONTROLLER.active_role == "planner"


# ---------------------------------------------------------------------------
# Call sites in run_loop
# ---------------------------------------------------------------------------


def _use_reader(monkeypatch: pytest.MonkeyPatch, reader: FakeReader) -> None:
    monkeypatch.setattr(orchestrator, "OperatorInputReader", lambda _stream: reader)


def test_run_loop_holds_before_the_paused_publish_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    path = _plant(repo)
    order: list[str] = []
    _no_dispatch(monkeypatch)

    class StopStartup(Exception):
        pass

    def resume(**_kwargs: Any) -> PublishOutcome:
        order.append("resume_paused_publish")
        assert not path.exists()
        return PublishOutcome()

    def recover(*_args: Any, **_kwargs: Any) -> None:
        order.append("recover_interrupted_dispatch")
        raise StopStartup

    monkeypatch.setattr(
        orchestrator,
        "leave_merged_story_branch",
        lambda **_kwargs: order.append("leave_merged_story_branch"),
    )
    monkeypatch.setattr(orchestrator, "resume_paused_publish", resume)
    monkeypatch.setattr(
        orchestrator,
        "resume_interrupted_publish",
        lambda **_kwargs: order.append("resume_interrupted_publish")
        or PublishOutcome(),
    )
    monkeypatch.setattr(orchestrator, "_recover_interrupted_dispatch", recover)
    _use_reader(monkeypatch, FakeReader([""], on_read=lambda: order.append("hold")))

    with pytest.raises(StopStartup):
        orchestrator.run_loop(_pr_config(repo), log=lambda _level, _text: None)

    assert order == [
        "hold",
        "leave_merged_story_branch",
        "resume_paused_publish",
        "resume_interrupted_publish",
        "recover_interrupted_dispatch",
    ]


def test_run_loop_stops_at_the_startup_hold_without_resuming_or_dispatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    path = _plant(repo)
    before = path.read_bytes()
    _no_dispatch(monkeypatch)
    for name in (
        "leave_merged_story_branch",
        "resume_paused_publish",
        "resume_interrupted_publish",
    ):
        monkeypatch.setattr(
            orchestrator,
            name,
            lambda *_a, _name=name, **_k: pytest.fail(f"{_name} must not run"),
        )
    _use_reader(monkeypatch, FakeReader([FakeReader.EOF]))
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _pr_config(repo),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert path.read_bytes() == before
    assert PROMPT_LINE in logs
    assert not any(text.startswith("--- Cycle") for _level, text in logs)


def test_run_loop_continues_to_the_first_cycle_after_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    path = _plant(repo)
    _no_dispatch(monkeypatch)
    _use_reader(monkeypatch, FakeReader([""]))
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert not path.exists()
    texts = [text for _level, text in logs]
    # One hold only: the loop-top check finds the queue already cleared.
    assert texts.count("Press Enter to continue") == 1
    assert texts.index("Continuing.") < texts.index("--- Cycle 1 ---")


def test_run_loop_without_a_queue_never_reads_operator_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    _no_dispatch(monkeypatch)
    _use_reader(monkeypatch, FakeReader())
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert not any("chat message" in text for _level, text in logs)
    assert "--- Cycle 1 ---" in [text for _level, text in logs]


def test_run_loop_dry_run_warns_and_dispatches_nothing_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    path = _plant(repo)
    before = path.read_bytes()
    _use_reader(monkeypatch, FakeReader())
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo, dry_run=True),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert path.read_bytes() == before
    warnings = [text for level, text in logs if "would hold here" in text]
    assert warnings
    assert PROMPT_LINE not in logs


def test_queue_planted_during_an_auditor_cycle_holds_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(
        tmp_path,
        _claim(status="ready_for_review", producer="backend", next_agent="auditor"),
    )
    config = _pr_config(repo)
    order: list[str] = []
    queue = chat_queue_path(repo)

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object):
        order.append(subagent_name)
        config.handoff_full_path.write_text(
            _claim(status="verified_pass", producer="auditor", next_agent="planner"),
            encoding="utf-8",
        )
        _plant(repo)
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    chat_input = _FakeChatInput()
    chat_session.attach(_FakeChatLine(), chat_input)  # type: ignore[arg-type]
    capture: list[tuple[str | None, str | None, bool]] = []

    def fake_publish(**_kwargs: object) -> PublishOutcome:
        order.append("publish_story")
        assert not queue.exists()
        capture.append(
            (
                console_interrupt.CONTROLLER.active_role,
                chat_session.SESSION.role,
                chat_input.running,
            )
        )
        return PublishOutcome()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *_args, **_kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        ),
    )
    monkeypatch.setattr(orchestrator, "publish_story", fake_publish)
    monkeypatch.setattr(
        orchestrator, "leave_merged_story_branch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "resume_paused_publish", lambda **_kwargs: PublishOutcome()
    )
    monkeypatch.setattr(
        orchestrator, "resume_interrupted_publish", lambda **_kwargs: PublishOutcome()
    )
    _use_reader(monkeypatch, FakeReader([""], on_read=lambda: order.append("hold")))
    ledger.reset_bounce(STORY_ID)
    try:
        exit_code = orchestrator.run_loop(
            config, max_cycles=1, log=lambda _level, _text: None
        )
    finally:
        ledger.reset_bounce(STORY_ID)

    assert exit_code == 0
    assert order == ["auditor", "hold", "publish_story"]
    assert not queue.exists()
    # Chat capture stays off through the publish, so no message is saved
    # after the hold's check; the auditor's console Ctrl+C behavior stays.
    assert capture == [("auditor", None, False)]


def test_queue_planted_during_a_planner_cycle_holds_before_settling_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(
        tmp_path,
        _claim(status="verified_pass", producer="validator", next_agent="planner"),
    )
    config = _pr_config(repo)
    order: list[str] = []
    queue = chat_queue_path(repo)

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        order.append("planner")
        path.write_text(
            _claim(status="verified_fail", producer="planner", next_agent="backend"),
            encoding="utf-8",
        )
        _plant(repo)
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    chat_input = _FakeChatInput()
    chat_session.attach(_FakeChatLine(), chat_input)  # type: ignore[arg-type]
    capture: list[tuple[str | None, bool]] = []

    def fake_settle(**_kwargs: object) -> PublishOutcome:
        order.append("settle_deferred_findings")
        assert not queue.exists()
        capture.append((chat_session.SESSION.role, chat_input.running))
        return PublishOutcome(stop=True, exit_code=0, message="paused")

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *_args, **_kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="backend"
        ),
    )
    monkeypatch.setattr(orchestrator, "settle_deferred_findings", fake_settle)
    monkeypatch.setattr(
        orchestrator, "leave_merged_story_branch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "resume_paused_publish", lambda **_kwargs: PublishOutcome()
    )
    monkeypatch.setattr(
        orchestrator, "resume_interrupted_publish", lambda **_kwargs: PublishOutcome()
    )
    _use_reader(monkeypatch, FakeReader([""], on_read=lambda: order.append("hold")))
    ledger.reset_bounce(STORY_ID)
    try:
        exit_code = orchestrator.run_loop(
            config, max_cycles=1, log=lambda _level, _text: None
        )
    finally:
        ledger.reset_bounce(STORY_ID)

    assert exit_code == 0
    assert order == ["planner", "hold", "settle_deferred_findings"]
    # Chat capture stays off through the settle.
    assert capture == [(None, False)]


def test_eof_at_the_publish_hold_stops_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(
        tmp_path,
        _claim(status="ready_for_review", producer="backend", next_agent="auditor"),
    )
    config = _pr_config(repo)

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object):
        config.handoff_full_path.write_text(
            _claim(status="verified_pass", producer="auditor", next_agent="planner"),
            encoding="utf-8",
        )
        _plant(repo)
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *_args, **_kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "publish_story",
        lambda **_kwargs: pytest.fail("publish_story must not run"),
    )
    monkeypatch.setattr(
        orchestrator, "leave_merged_story_branch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "resume_paused_publish", lambda **_kwargs: PublishOutcome()
    )
    monkeypatch.setattr(
        orchestrator, "resume_interrupted_publish", lambda **_kwargs: PublishOutcome()
    )
    _use_reader(monkeypatch, FakeReader([FakeReader.EOF]))
    ledger.reset_bounce(STORY_ID)
    try:
        exit_code = orchestrator.run_loop(
            config, max_cycles=1, log=lambda _level, _text: None
        )
    finally:
        ledger.reset_bounce(STORY_ID)

    assert exit_code == 0
    assert chat_queue_path(repo).exists()
