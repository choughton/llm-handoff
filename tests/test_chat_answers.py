from __future__ import annotations

import inspect
import threading
from pathlib import Path

import pytest

from llm_handoff import chat_answers
from llm_handoff.chat_answers import (
    CHAT_NO_ROLE_MESSAGE,
    ChatAnswerWorker,
    ChatContext,
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
    assert all(
        param.kind is inspect.Parameter.KEYWORD_ONLY for param in params.values()
    )


def test_context_is_frozen() -> None:
    context = _context()
    with pytest.raises(Exception):
        context.role = "frontend"  # type: ignore[misc]


def test_module_docstring_states_the_limit_and_is_not_wired() -> None:
    doc = chat_answers.__doc__ or ""
    assert "Nothing in the loop calls this module yet" in doc
    assert "role's own ``HANDOFF.md`` write" in doc


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


def test_stop_with_nothing_queued_writes_nothing(harness: Harness, worker) -> None:
    worker.stop()
    worker.submit("m")
    assert worker.wait_idle(timeout=WAIT)
    lines = list(harness.lines)
    worker.stop()
    worker.stop()
    assert harness.lines == lines
