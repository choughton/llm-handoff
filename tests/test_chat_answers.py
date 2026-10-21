from __future__ import annotations

import functools
import inspect
import io
import threading
from pathlib import Path

import pytest

from llm_handoff import __main__ as main_module
from llm_handoff import chat_answers
from llm_handoff import chat_session
from llm_handoff import config
from llm_handoff.chat_input import ChatInput
from llm_handoff.chat_answers import (
    CHAT_BLANK_MESSAGE,
    CHAT_NO_ROLE_MESSAGE,
    CHAT_STOPPING_MESSAGE,
    ChatAnswerWorker,
    ChatContext,
    RoleSession,
)
from llm_handoff.chat_changes import record_chat_changes
from llm_handoff.chat_side_session import ChatSessionResult, run_chat_session
from llm_handoff.chat_takeover import (
    handoff_content_sha,
    request_takeover_if_handoff_changed,
)

WAIT = 10.0


def _result(
    text: str = "ok",
    *,
    session_id: str | None = None,
    error: str | None = None,
    edited_paths: tuple[str, ...] = (),
) -> ChatSessionResult:
    return ChatSessionResult(
        text=text,
        session_id=session_id,
        exit_code=0 if error is None else 1,
        error=error,
        edited_paths=edited_paths,
    )


def _context(provider: str = "claude", role: str = "backend") -> ChatContext:
    return ChatContext(
        role=role,
        provider=provider,
        story_id="S-1",
        log_path="logs/dispatch/backend.log",
        agent_config=None,
    )


class Harness:
    """Fake collaborators that log every call in one shared event list."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo_root = tmp_path
        self.handoff = tmp_path / "HANDOFF.md"
        self.handoff.write_text("before\n", encoding="utf-8")
        self.changes = tmp_path / "logs" / "dispatch" / "chat-changes.json"
        self.events: list[tuple] = []
        self.lines: list[str] = []
        self.session_calls: list[dict] = []
        self.context_value: ChatContext | None = _context()
        self.results: dict[str, object] = {}
        self.gates: dict[str, threading.Event] = {}
        self.started: dict[str, threading.Event] = {}
        self.record_error: BaseException | None = None
        self.on_write = None
        self.role_sessions: dict[str, object] = {}
        self.lookups: list[str] = []
        self.use_lookup = True

    def role_session(self, role: str):
        self.lookups.append(role)
        outcome = self.role_sessions.get(role)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def context(self) -> ChatContext | None:
        return self.context_value

    def write_line(self, line: str) -> None:
        self.events.append(("write", line))
        self.lines.append(line)
        if self.on_write is not None:
            self.on_write(line)

    def run_session(self, provider, message, **kwargs):
        self.session_calls.append({"provider": provider, "message": message, **kwargs})
        self.events.append(("session", message))
        started = self.started.get(message)
        if started is not None:
            started.set()
        gate = self.gates.get(message)
        if gate is not None:
            assert gate.wait(WAIT)
        outcome = self.results.get(message, _result(f"answer to {message}"))
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    def record_changes(self, path, **kwargs):
        self.events.append(("record", kwargs["message"]))
        self.record_kwargs = {"path": path, **kwargs}
        if self.record_error is not None:
            raise self.record_error
        return None

    def request_takeover(self, **kwargs):
        self.events.append(("takeover", kwargs["handoff_sha_before"]))
        self.takeover_kwargs = kwargs
        return False

    def block(self, message: str) -> tuple[threading.Event, threading.Event]:
        started = threading.Event()
        gate = threading.Event()
        self.started[message] = started
        self.gates[message] = gate
        return started, gate

    def worker(self) -> ChatAnswerWorker:
        return ChatAnswerWorker(
            repo_root=self.repo_root,
            handoff_path=self.handoff,
            changes_path=self.changes,
            context=self.context,
            write_line=self.write_line,
            run_session=self.run_session,
            record_changes=self.record_changes,
            request_takeover=self.request_takeover,
            role_session=self.role_session if self.use_lookup else None,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


@pytest.fixture
def worker(harness: Harness):
    created = harness.worker()
    yield created
    created.stop()


def test_defaults_are_the_chat_17_21_27_functions() -> None:
    params = inspect.signature(ChatAnswerWorker).parameters
    assert params["run_session"].default is run_chat_session
    assert params["record_changes"].default is record_chat_changes
    assert params["request_takeover"].default is request_takeover_if_handoff_changed
    assert params["role_session"].default is None
    assert all(
        param.kind is inspect.Parameter.KEYWORD_ONLY for param in params.values()
    )


def test_context_is_frozen() -> None:
    context = _context()
    with pytest.raises(Exception):
        context.role = "frontend"  # type: ignore[misc]


def test_module_docstring_states_the_limit_and_the_wiring() -> None:
    doc = chat_answers.__doc__ or ""
    assert "Nothing in the loop calls this module yet" not in doc
    assert "__main__._attach_console_chat" in doc
    assert "CHAT-32" in doc
    assert "CHAT-34" in doc
    assert "role_session" in doc
    assert "role's own ``HANDOFF.md`` write" in doc
    # CHAT-35: the loop, not the worker, tells a role's handback apart.
    assert "CHAT-35" in doc
    assert "``producer``" in doc


def test_no_running_role_queues_nothing(harness: Harness, worker) -> None:
    harness.context_value = None
    assert worker.submit("hello") == CHAT_NO_ROLE_MESSAGE
    assert worker.busy() is False
    assert worker.wait_idle(timeout=WAIT) is True
    assert harness.session_calls == []


def test_agy_provider_returns_notice_and_queues_nothing(
    harness: Harness, worker
) -> None:
    harness.context_value = _context(provider="agy")
    assert worker.submit("hello") == "Chat is not available for agy yet."
    assert worker.busy() is False
    assert harness.session_calls == []


def test_blank_text_queues_nothing(harness: Harness, worker) -> None:
    worker.submit("   \n ")
    assert worker.busy() is False
    assert harness.session_calls == []


def test_answer_lines_are_prefixed_and_call_passes_context(
    harness: Harness, worker
) -> None:
    harness.results["  hi  ".strip()] = _result("line one\nline two")
    notice = worker.submit("  hi  ")
    assert notice == "Chat message sent to the chat session."
    assert worker.wait_idle(timeout=WAIT) is True
    assert harness.lines == ["chat: line one", "chat: line two"]
    call = harness.session_calls[0]
    assert call["provider"] == "claude"
    assert call["message"] == "hi"
    assert call["repo_root"] == harness.repo_root
    assert call["role"] == "backend"
    assert call["story_id"] == "S-1"
    assert call["handoff_path"] == harness.handoff
    assert call["log_path"] == "logs/dispatch/backend.log"
    assert call["session_id"] is None
    assert call["agent_config"] is None
    assert harness.record_kwargs["path"] == harness.changes
    assert harness.record_kwargs["message"] == "hi"
    assert harness.record_kwargs["role"] == "backend"
    assert harness.record_kwargs["story_id"] == "S-1"
    assert harness.record_kwargs["handoff_path"] == harness.handoff
    assert harness.record_kwargs["repo_root"] == harness.repo_root
    assert harness.takeover_kwargs["handoff_path"] == harness.handoff


def test_two_queued_messages_answer_in_order(harness: Harness, worker) -> None:
    started, gate = harness.block("first")
    assert worker.submit("first") == "Chat message sent to the chat session."
    assert started.wait(WAIT)
    assert worker.submit("second") == (
        "Chat message sent to the chat session. 1 message is ahead of it."
    )
    assert worker.submit("third") == (
        "Chat message sent to the chat session. 2 messages are ahead of it."
    )
    assert worker.busy() is True
    gate.set()
    assert worker.wait_idle(timeout=WAIT) is True
    assert worker.busy() is False
    assert [call["message"] for call in harness.session_calls] == [
        "first",
        "second",
        "third",
    ]
    sessions = [event[1] for event in harness.events if event[0] == "session"]
    assert sessions == ["first", "second", "third"]


def test_context_is_read_at_submit_time(harness: Harness, worker) -> None:
    started, gate = harness.block("first")
    worker.submit("first")
    assert started.wait(WAIT)
    harness.context_value = _context(role="auditor")
    worker.submit("second")
    gate.set()
    assert worker.wait_idle(timeout=WAIT) is True
    assert [call["role"] for call in harness.session_calls] == ["backend", "auditor"]


def test_session_resumes_per_provider(harness: Harness, worker) -> None:
    harness.results["c1"] = _result("a", session_id="claude-1")
    harness.results["c2"] = _result("b", session_id="claude-1")
    harness.results["x1"] = _result("c", session_id="codex-1")
    harness.results["x2"] = _result("d", session_id="codex-1")
    worker.submit("c1")
    assert worker.wait_idle(timeout=WAIT)
    worker.submit("c2")
    assert worker.wait_idle(timeout=WAIT)
    harness.context_value = _context(provider="codex")
    worker.submit("x1")
    assert worker.wait_idle(timeout=WAIT)
    worker.submit("x2")
    assert worker.wait_idle(timeout=WAIT)
    harness.context_value = _context(provider="claude")
    worker.submit("c3")
    assert worker.wait_idle(timeout=WAIT)
    resumed = [(call["provider"], call["session_id"]) for call in harness.session_calls]
    assert resumed == [
        ("claude", None),
        ("claude", "claude-1"),
        ("codex", None),
        ("codex", "codex-1"),
        ("claude", "claude-1"),
    ]


def test_record_runs_before_takeover_with_sha_taken_before_call(
    harness: Harness, worker
) -> None:
    sha_before = handoff_content_sha(harness.handoff)

    def rewrite_handoff() -> ChatSessionResult:
        harness.handoff.write_text("after\n", encoding="utf-8")
        return _result("done", edited_paths=("src/a.py",))

    harness.results["fix it"] = rewrite_handoff
    worker.submit("fix it")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.events == [
        ("session", "fix it"),
        ("write", "chat: done"),
        ("record", "fix it"),
        ("takeover", sha_before),
    ]
    assert sha_before != handoff_content_sha(harness.handoff)


def test_failed_result_writes_error_and_is_still_recorded(
    harness: Harness, worker
) -> None:
    harness.results["m"] = _result("", error="Claude exited with code 1.", session_id="s")
    worker.submit("m")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.events == [
        ("session", "m"),
        ("write", "chat error: Claude exited with code 1."),
        ("record", "m"),
        ("takeover", handoff_content_sha(harness.handoff)),
    ]


@pytest.mark.parametrize("error", [ValueError("bad provider"), OSError("no binary")])
def test_session_value_or_os_error_writes_one_line_and_checks_takeover(
    harness: Harness, worker, error: Exception
) -> None:
    harness.results["boom"] = error
    worker.submit("boom")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.lines == [f"chat error: {error}"]
    kinds = [event[0] for event in harness.events]
    assert kinds == ["session", "write", "takeover"]
    worker.submit("next")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.lines[-1] == "chat: answer to next"


def test_record_os_error_names_path_and_checks_takeover(
    harness: Harness, worker
) -> None:
    harness.record_error = OSError("disk full")
    worker.submit("m")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.lines[0] == "chat: answer to m"
    assert len(harness.lines) == 2
    assert harness.lines[1].startswith("chat error: ")
    assert str(harness.changes) in harness.lines[1]
    assert "disk full" in harness.lines[1]
    kinds = [event[0] for event in harness.events]
    assert kinds == ["session", "write", "record", "write", "takeover"]


def test_other_exception_writes_one_line_and_thread_moves_on(
    harness: Harness, worker
) -> None:
    harness.results["boom"] = RuntimeError("unexpected")
    worker.submit("boom")
    worker.submit("next")
    assert worker.wait_idle(timeout=WAIT)
    errors = [line for line in harness.lines if line.startswith("chat error: ")]
    assert errors == ["chat error: RuntimeError: unexpected"]
    assert harness.lines[-1] == "chat: answer to next"


def test_write_line_failure_does_not_skip_record_or_takeover(
    harness: Harness, worker
) -> None:
    def fail(_line: str) -> None:
        raise OSError("console closed")

    harness.on_write = fail
    worker.submit("m")
    assert worker.wait_idle(timeout=WAIT)
    kinds = [event[0] for event in harness.events]
    assert kinds == ["session", "write", "record", "takeover"]


def test_wait_idle_times_out_while_answer_runs(harness: Harness, worker) -> None:
    started, gate = harness.block("slow")
    worker.submit("slow")
    assert started.wait(WAIT)
    assert worker.busy() is True
    assert worker.wait_idle(timeout=0.05) is False
    gate.set()
    assert worker.wait_idle(timeout=WAIT) is True
    assert worker.busy() is False


def test_stop_finishes_running_answer_and_drops_queued(
    harness: Harness, worker
) -> None:
    started, gate = harness.block("first")
    worker.submit("first")
    assert started.wait(WAIT)
    worker.submit("second")
    worker.submit("third")
    dropped_written = threading.Event()

    def watch(line: str) -> None:
        if line.startswith("Chat stopped"):
            dropped_written.set()

    harness.on_write = watch
    stopper = threading.Thread(target=worker.stop)
    stopper.start()
    assert dropped_written.wait(WAIT)
    gate.set()
    stopper.join(WAIT)
    assert not stopper.is_alive()
    assert [call["message"] for call in harness.session_calls] == ["first"]
    assert "Chat stopped: 2 queued chat messages were not answered." in harness.lines
    assert harness.lines[-1] == "chat: answer to first"
    assert worker.busy() is False
    assert worker.wait_idle(timeout=WAIT) is True

    line_count = len(harness.lines)
    worker.stop()
    assert len(harness.lines) == line_count

    worker.submit("after stop")
    assert worker.wait_idle(timeout=WAIT) is True
    assert harness.session_calls[-1]["message"] == "after stop"
    assert harness.lines[-1] == "chat: answer to after stop"


def test_submit_during_stop_never_overlaps_the_running_answer(
    harness: Harness, worker
) -> None:
    started, gate = harness.block("first")
    worker.submit("first")
    assert started.wait(WAIT)
    worker.submit("queued")
    dropped_written = threading.Event()
    harness.on_write = lambda line: (
        dropped_written.set() if line.startswith("Chat stopped") else None
    )
    first_stopper = threading.Thread(target=worker.stop)
    first_stopper.start()
    assert dropped_written.wait(WAIT)

    # The first answer is still running: a submit now must not start a
    # second answer, and a second stop() must wait for the same thread.
    assert worker.submit("during stop") == CHAT_STOPPING_MESSAGE
    second_stopper = threading.Thread(target=worker.stop)
    second_stopper.start()
    second_stopper.join(timeout=0.05)
    assert second_stopper.is_alive()
    assert [call["message"] for call in harness.session_calls] == ["first"]

    gate.set()
    first_stopper.join(WAIT)
    second_stopper.join(WAIT)
    assert not first_stopper.is_alive()
    assert not second_stopper.is_alive()
    assert [call["message"] for call in harness.session_calls] == ["first"]
    assert harness.lines.count(
        "Chat stopped: 1 queued chat message was not answered."
    ) == 1
    assert worker.busy() is False

    assert worker.submit("after stop") == "Chat message sent to the chat session."
    assert worker.wait_idle(timeout=WAIT) is True
    assert [call["message"] for call in harness.session_calls] == [
        "first",
        "after stop",
    ]


def test_stop_with_nothing_queued_writes_nothing(harness: Harness, worker) -> None:
    worker.stop()
    worker.submit("m")
    assert worker.wait_idle(timeout=WAIT)
    lines = list(harness.lines)
    worker.stop()
    worker.stop()
    assert harness.lines == lines


# --- @role messages ----------------------------------------------------------

AUDITOR_CONFIG = config.AgentConfig(provider="claude", model="auditor-model")
SIDE_SENT = "Chat message sent to the chat session."
AUDITOR_SENT = "Chat message sent to the auditor session."


def _role_session(
    role: str = "auditor",
    *,
    provider: str = "claude",
    story_id: str | None = "S-0",
    session_id: str = "auditor-sess-1",
    agent_config: config.AgentConfig | None = AUDITOR_CONFIG,
) -> RoleSession:
    return RoleSession(
        role=role,
        provider=provider,
        story_id=story_id,
        session_id=session_id,
        agent_config=agent_config,
    )


def test_role_session_is_frozen_and_exported() -> None:
    assert "RoleSession" in chat_answers.__all__
    session = _role_session()
    with pytest.raises(Exception):
        session.role = "backend"  # type: ignore[misc]


def test_role_message_runs_in_the_role_session(harness: Harness, worker) -> None:
    harness.role_sessions["auditor"] = _role_session()
    harness.results["why the bounce?"] = _result("line one\nline two")
    assert worker.submit("  @auditor   why the bounce?  ") == AUDITOR_SENT
    assert harness.lookups == ["auditor"]
    assert worker.wait_idle(timeout=WAIT)
    [call] = harness.session_calls
    assert call == {
        "provider": "claude",
        "message": "why the bounce?",
        "repo_root": harness.repo_root,
        "role": "auditor",
        "story_id": "S-0",
        "handoff_path": harness.handoff,
        "log_path": "logs/dispatch/backend.log",
        "session_id": "auditor-sess-1",
        "agent_config": AUDITOR_CONFIG,
        "resumed_role": True,
    }
    assert harness.lines == ["chat auditor: line one", "chat auditor: line two"]


def test_role_message_body_keeps_inner_newlines(harness: Harness, worker) -> None:
    harness.role_sessions["planner"] = _role_session("planner")
    assert worker.submit("@planner\nfirst\nsecond") == (
        "Chat message sent to the planner session."
    )
    assert worker.wait_idle(timeout=WAIT)
    assert harness.session_calls[0]["message"] == "first\nsecond"
    assert harness.session_calls[0]["role"] == "planner"


@pytest.mark.parametrize("text", ["@auditor", "  @auditor   ", "@auditor \n\t "])
def test_role_message_with_blank_body_queues_nothing(
    harness: Harness, worker, text: str
) -> None:
    harness.role_sessions["auditor"] = _role_session()
    assert worker.submit(text) == CHAT_BLANK_MESSAGE
    assert worker.busy() is False
    assert harness.session_calls == []
    assert harness.lookups == []


@pytest.mark.parametrize(
    "text",
    ["@foo hi", "@Backend hi", "@AUDITOR hi", "hi @auditor there", "@auditor:hi"],
)
def test_unknown_names_go_to_side_session_unchanged(
    harness: Harness, worker, text: str
) -> None:
    harness.role_sessions["auditor"] = _role_session()
    assert worker.submit(text) == SIDE_SENT
    assert worker.wait_idle(timeout=WAIT)
    assert harness.lookups == []
    [call] = harness.session_calls
    assert call["message"] == text
    assert call["role"] == "backend"
    assert "resumed_role" not in call
    assert harness.lines == [f"chat: answer to {text}"]


def test_every_agent_role_name_is_accepted(harness: Harness, worker) -> None:
    roles = [role for role in config.AgentRole.__args__ if role != "backend"]
    for role in roles:
        harness.role_sessions[role] = _role_session(role, session_id=f"{role}-1")
        assert worker.submit(f"@{role} hi") == (
            f"Chat message sent to the {role} session."
        )
        assert worker.wait_idle(timeout=WAIT)
    assert [call["role"] for call in harness.session_calls] == roles


def test_role_message_with_no_running_role(harness: Harness, worker) -> None:
    harness.context_value = None
    harness.role_sessions["auditor"] = _role_session()
    assert worker.submit("@auditor hi") == CHAT_NO_ROLE_MESSAGE
    assert worker.busy() is False
    assert harness.session_calls == []


def test_role_message_while_stopping(harness: Harness, worker) -> None:
    harness.role_sessions["auditor"] = _role_session()
    started, gate = harness.block("first")
    worker.submit("first")
    assert started.wait(WAIT)
    dropped = threading.Event()
    harness.on_write = lambda line: (
        dropped.set() if line.startswith("Chat stopped") else None
    )
    worker.submit("queued")
    stopper = threading.Thread(target=worker.stop)
    stopper.start()
    assert dropped.wait(WAIT)
    assert worker.submit("@auditor hi") == CHAT_STOPPING_MESSAGE
    gate.set()
    stopper.join(WAIT)
    assert not stopper.is_alive()
    assert [call["message"] for call in harness.session_calls] == ["first"]


def test_target_is_running_role_falls_back(harness: Harness, worker) -> None:
    harness.role_sessions["backend"] = _role_session("backend")
    assert worker.submit("@backend status?") == (
        f"The backend role is running, so the side session answers. {SIDE_SENT}"
    )
    assert worker.wait_idle(timeout=WAIT)
    [call] = harness.session_calls
    assert call["message"] == "@backend status?"
    assert call["provider"] == "claude"
    assert call["role"] == "backend"
    assert "resumed_role" not in call
    assert harness.lines == ["chat: answer to @backend status?"]


NO_AUDITOR = "No finished auditor session is recorded, so the side session answers."


@pytest.mark.parametrize(
    "outcome",
    [
        None,
        RuntimeError("lookup broke"),
        KeyError("auditor"),
        "not a role session",
        _role_session(session_id=""),
        _role_session(session_id="   "),
    ],
)
def test_no_session_cases_fall_back(harness: Harness, worker, outcome) -> None:
    harness.role_sessions["auditor"] = outcome
    assert worker.submit("@auditor hi") == f"{NO_AUDITOR} {SIDE_SENT}"
    assert worker.wait_idle(timeout=WAIT)
    [call] = harness.session_calls
    assert call["message"] == "@auditor hi"
    assert call["session_id"] is None
    assert "resumed_role" not in call
    assert harness.lines == ["chat: answer to @auditor hi"]


def test_no_lookup_falls_back(harness: Harness) -> None:
    harness.use_lookup = False
    worker = harness.worker()
    try:
        assert worker.submit("@auditor hi") == f"{NO_AUDITOR} {SIDE_SENT}"
        assert worker.wait_idle(timeout=WAIT)
        assert harness.session_calls[0]["message"] == "@auditor hi"
    finally:
        worker.stop()


def test_unsupported_role_provider_falls_back(harness: Harness, worker) -> None:
    harness.role_sessions["frontend"] = _role_session(
        "frontend", provider="antigravity"
    )
    assert worker.submit("@frontend hi") == (
        "The frontend session runs on antigravity, which chat does not support "
        f"yet, so the side session answers. {SIDE_SENT}"
    )
    assert worker.wait_idle(timeout=WAIT)
    [call] = harness.session_calls
    assert call["provider"] == "claude"
    assert call["message"] == "@frontend hi"


def test_fallback_on_unsupported_running_provider_queues_nothing(
    harness: Harness, worker
) -> None:
    harness.context_value = _context(provider="antigravity")
    assert worker.submit("@backend hi") == "Chat is not available for antigravity yet."
    assert worker.submit("@auditor hi") == "Chat is not available for antigravity yet."
    assert worker.busy() is False
    assert harness.session_calls == []


def test_role_message_runs_while_running_provider_is_unsupported(
    harness: Harness, worker
) -> None:
    harness.context_value = _context(provider="antigravity")
    harness.role_sessions["auditor"] = _role_session()
    assert worker.submit("@auditor hi") == AUDITOR_SENT
    assert worker.wait_idle(timeout=WAIT)
    assert harness.session_calls[0]["provider"] == "claude"
    assert harness.session_calls[0]["resumed_role"] is True


def test_fallback_notice_counts_messages_ahead(harness: Harness, worker) -> None:
    started, gate = harness.block("first")
    worker.submit("first")
    assert started.wait(WAIT)
    assert worker.submit("@auditor hi") == (
        f"{NO_AUDITOR} {SIDE_SENT} 1 message is ahead of it."
    )
    harness.role_sessions["auditor"] = _role_session()
    assert worker.submit("@auditor again") == (
        f"{AUDITOR_SENT} 2 messages are ahead of it."
    )
    gate.set()
    assert worker.wait_idle(timeout=WAIT)


def test_mixed_side_and_role_messages_keep_fifo_order(
    harness: Harness, worker
) -> None:
    harness.role_sessions["auditor"] = _role_session()
    harness.role_sessions["planner"] = _role_session(
        "planner", provider="codex", session_id="planner-thread"
    )
    started, gate = harness.block("first")
    worker.submit("first")
    assert started.wait(WAIT)
    worker.submit("@auditor a1")
    worker.submit("side two")
    worker.submit("@planner p1")
    worker.submit("@auditor a2")
    gate.set()
    assert worker.wait_idle(timeout=WAIT)
    order = [
        (call["message"], call["role"], call["provider"])
        for call in harness.session_calls
    ]
    assert order == [
        ("first", "backend", "claude"),
        ("a1", "auditor", "claude"),
        ("side two", "backend", "claude"),
        ("p1", "planner", "codex"),
        ("a2", "auditor", "claude"),
    ]


def test_role_resume_chain_is_keyed_by_recorded_session(
    harness: Harness, worker
) -> None:
    harness.role_sessions["auditor"] = _role_session(session_id="rec-1")
    harness.results["q1"] = _result("a", session_id="rec-1-next")
    harness.results["q2"] = _result("b", session_id="  ")
    harness.results["q3"] = _result("c", session_id="rec-1-later")
    for message in ("q1", "q2", "q3", "q4"):
        worker.submit(f"@auditor {message}")
        assert worker.wait_idle(timeout=WAIT)
    # A later auditor dispatch records a new session id: start from it.
    harness.role_sessions["auditor"] = _role_session(session_id="rec-2")
    harness.results["r1"] = _result("d", session_id="rec-2-next")
    worker.submit("@auditor r1")
    assert worker.wait_idle(timeout=WAIT)
    worker.submit("@auditor r2")
    assert worker.wait_idle(timeout=WAIT)
    # A side message neither reads nor writes the role chain.
    worker.submit("side")
    assert worker.wait_idle(timeout=WAIT)
    resumed = [(call["message"], call["session_id"]) for call in harness.session_calls]
    assert resumed == [
        ("q1", "rec-1"),
        ("q2", "rec-1-next"),
        ("q3", "rec-1-next"),
        ("q4", "rec-1-later"),
        ("r1", "rec-2"),
        ("r2", "rec-2-next"),
        ("side", None),
    ]


def test_side_session_map_is_not_touched_by_role_messages(
    harness: Harness, worker
) -> None:
    harness.role_sessions["auditor"] = _role_session(provider="claude")
    harness.results["s1"] = _result("x", session_id="side-claude")
    harness.results["r1"] = _result("y", session_id="role-claude")
    worker.submit("s1")
    assert worker.wait_idle(timeout=WAIT)
    worker.submit("@auditor r1")
    assert worker.wait_idle(timeout=WAIT)
    worker.submit("s2")
    assert worker.wait_idle(timeout=WAIT)
    resumed = [(call["message"], call["session_id"]) for call in harness.session_calls]
    assert resumed == [
        ("s1", None),
        ("r1", "auditor-sess-1"),
        ("s2", "side-claude"),
    ]


def test_role_answer_records_then_takes_over(harness: Harness, worker) -> None:
    harness.role_sessions["auditor"] = _role_session(story_id="OLD-1")
    sha_before = handoff_content_sha(harness.handoff)

    def rewrite_handoff() -> ChatSessionResult:
        harness.handoff.write_text("after\n", encoding="utf-8")
        return _result("done", edited_paths=("src/a.py",))

    harness.results["fix it"] = rewrite_handoff
    worker.submit("@auditor fix it")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.events == [
        ("session", "fix it"),
        ("write", "chat auditor: done"),
        ("record", "@auditor fix it"),
        ("takeover", sha_before),
    ]
    assert harness.record_kwargs["role"] == "auditor"
    assert harness.record_kwargs["story_id"] == "S-1"
    assert harness.record_kwargs["path"] == harness.changes
    assert harness.record_kwargs["repo_root"] == harness.repo_root
    assert harness.record_kwargs["handoff_path"] == harness.handoff
    assert harness.takeover_kwargs == {
        "handoff_path": harness.handoff,
        "handoff_sha_before": sha_before,
    }


def test_failed_role_result_writes_role_error_and_is_recorded(
    harness: Harness, worker
) -> None:
    harness.role_sessions["auditor"] = _role_session()
    harness.results["m"] = _result("", error="Claude exited with code 1.")
    worker.submit("@auditor m")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.events == [
        ("session", "m"),
        ("write", "chat auditor error: Claude exited with code 1."),
        ("record", "@auditor m"),
        ("takeover", handoff_content_sha(harness.handoff)),
    ]
    assert harness.record_kwargs["role"] == "auditor"


@pytest.mark.parametrize(
    ("error", "line", "kinds"),
    [
        (
            ValueError("bad id"),
            "chat auditor error: bad id",
            ["session", "write", "takeover"],
        ),
        (
            OSError("no binary"),
            "chat auditor error: no binary",
            ["session", "write", "takeover"],
        ),
        # Any other exception is written once the takeover check has run.
        (
            RuntimeError("unexpected"),
            "chat auditor error: RuntimeError: unexpected",
            ["session", "takeover", "write"],
        ),
    ],
)
def test_role_session_errors_write_one_role_line(
    harness: Harness, worker, error: Exception, line: str, kinds: list[str]
) -> None:
    harness.role_sessions["auditor"] = _role_session()
    harness.results["boom"] = error
    worker.submit("@auditor boom")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.lines == [line]
    assert [event[0] for event in harness.events] == kinds
    worker.submit("next")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.lines[-1] == "chat: answer to next"


def test_role_record_os_error_uses_role_prefix(harness: Harness, worker) -> None:
    harness.role_sessions["auditor"] = _role_session()
    harness.record_error = OSError("disk full")
    worker.submit("@auditor m")
    assert worker.wait_idle(timeout=WAIT)
    assert harness.lines[0] == "chat auditor: answer to m"
    assert harness.lines[1].startswith("chat auditor error: ")
    assert str(harness.changes) in harness.lines[1]
    assert "disk full" in harness.lines[1]
    assert len(harness.lines) == 2


# --- console attachment supplies the role-session lookup (CHAT-34) ------------


class _TTYStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _IdleKeyReader:
    def __init__(self) -> None:
        self._idle = threading.Event()

    def open(self) -> None:
        return None

    def read_chars(self, timeout: float) -> str:
        self._idle.wait(min(timeout, 0.005))
        return ""

    def close(self) -> None:
        return None


class _Logger:
    def __init__(self) -> None:
        self.logged: list[tuple[str, str]] = []
        self.chat_line = None

    def __call__(self, level: str, text: str) -> None:
        self.logged.append((level, text))

    def attach_chat_line(self, chat_line) -> None:
        self.chat_line = chat_line


class Console:
    """``__main__._attach_console_chat`` with fake terminals and a fake session."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.session_calls: list[dict] = []
        self.on_submit = None
        self.worker: ChatAnswerWorker | None = None
        self.logger = _Logger()
        self.config = config.DispatchConfig(
            repo_root=tmp_path,
            audit={
                "reviewers": ["quality"],
                "security_opt_out_reason": "Tests cover the single-auditor route.",
            },
        )

    def run_session(self, provider, message, **kwargs):
        self.session_calls.append({"provider": provider, "message": message, **kwargs})
        return _result(f"answer to {message}")

    def attach(
        self,
        *,
        stdin_tty: bool = True,
        stdout_tty: bool = True,
        console_input: bool = True,
    ) -> ChatAnswerWorker | None:
        def factory(_stdin, chat_line, **kwargs):
            if not console_input:
                return None
            if "on_submit" in kwargs:
                self.on_submit = kwargs["on_submit"]
            return ChatInput(chat_line, _IdleKeyReader(), poll_interval=0.01)

        self.monkeypatch.setattr(main_module, "chat_input_for_console", factory)
        self.monkeypatch.setattr(
            main_module,
            "ChatAnswerWorker",
            functools.partial(
                ChatAnswerWorker,
                run_session=self.run_session,
                record_changes=lambda *_a, **_k: None,
                request_takeover=lambda **_k: False,
            ),
        )
        self.monkeypatch.setattr(main_module.sys, "stdin", _TTYStream(stdin_tty))
        self.monkeypatch.setattr(main_module.sys, "stdout", _TTYStream(stdout_tty))
        self.worker = main_module._attach_console_chat(
            self.logger, self.config  # type: ignore[arg-type]
        )
        return self.worker

    def close(self) -> None:
        if self.worker is not None:
            chat_session.set_role(None)
            self.worker.stop()
            chat_session.attach_worker(None)
        chat_session.detach()


@pytest.fixture
def console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    chat_session.detach()
    created = Console(tmp_path, monkeypatch)
    yield created
    created.close()


def _running(role: str = "auditor") -> ChatContext:
    return ChatContext(
        role=role,
        provider="claude",
        story_id="S-9",
        log_path="logs/dispatch/auditor.log",
        agent_config=None,
    )


def test_attached_worker_resumes_a_session_recorded_after_attachment(
    console: Console,
) -> None:
    worker = console.attach()
    assert worker is not None
    assert chat_session.SESSION.worker is worker
    chat_session.set_role("auditor", _running())
    assert console.on_submit is not None

    # Nothing is recorded yet: the lookup runs at submit time and falls back.
    assert console.on_submit("@backend before") == (
        "No finished backend session is recorded, so the side session answers. "
        "Chat message sent to the chat session."
    )
    assert worker.wait_idle(timeout=WAIT)

    # The recorded provider and story id are kept, even when they differ from
    # the configured ones.
    chat_session.record_role_dispatch(
        "backend", story_id="S-0", provider="claude", session_id="backend-1"
    )
    assert console.on_submit("@backend why?") == (
        "Chat message sent to the backend session."
    )
    assert worker.wait_idle(timeout=WAIT)

    side_call, role_call = console.session_calls
    assert side_call["message"] == "@backend before"
    assert "resumed_role" not in side_call or side_call["resumed_role"] is False
    assert role_call["provider"] == "claude"
    assert role_call["message"] == "why?"
    assert role_call["role"] == "backend"
    assert role_call["story_id"] == "S-0"
    assert role_call["session_id"] == "backend-1"
    assert role_call["agent_config"] is console.config.agents["backend"]
    assert role_call["resumed_role"] is True
    assert ("AGENT", "chat backend: answer to why?") in console.logger.logged


def test_attached_lookup_follows_the_latest_entry(console: Console) -> None:
    console.attach()
    lookup = console.worker._role_session  # type: ignore[union-attr]
    assert lookup is not None

    chat_session.record_role_dispatch(
        "planner", story_id="S-1", provider="codex", session_id="p-1"
    )
    first = lookup("planner")
    chat_session.record_role_dispatch(
        "planner", story_id="S-2", provider="codex", session_id="p-2"
    )
    second = lookup("planner")
    chat_session.record_role_dispatch(
        "planner", story_id="S-3", provider="codex", session_id=None
    )

    assert first == RoleSession(
        role="planner",
        provider="codex",
        story_id="S-1",
        session_id="p-1",
        agent_config=console.config.agents["planner"],
    )
    assert second is not None and second.session_id == "p-2"
    assert lookup("planner") is None
    assert lookup("backend") is None


def test_lookup_returns_none_for_a_role_with_no_configured_agent(
    console: Console,
) -> None:
    assert "tpm" not in console.config.agents
    chat_session.record_role_dispatch(
        "tpm", story_id="S-1", provider="codex", session_id="t-1"
    )

    assert main_module._role_session_lookup(console.config)("tpm") is None


def test_lookup_returns_none_after_detach(console: Console) -> None:
    console.attach()
    lookup = console.worker._role_session  # type: ignore[union-attr]
    chat_session.record_role_dispatch(
        "backend", story_id="S-1", provider="claude", session_id="b-1"
    )
    assert lookup("backend") is not None

    console.close()
    console.worker = None

    assert chat_session.SESSION.attached is False
    assert chat_session.SESSION.worker is None
    assert lookup("backend") is None


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "console_input"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_no_worker_or_lookup_without_an_interactive_console(
    console: Console, stdin_tty: bool, stdout_tty: bool, console_input: bool
) -> None:
    worker = console.attach(
        stdin_tty=stdin_tty, stdout_tty=stdout_tty, console_input=console_input
    )

    assert worker is None
    assert console.on_submit is None
    assert chat_session.SESSION.attached is False
    assert chat_session.SESSION.worker is None
    assert console.logger.chat_line is None
