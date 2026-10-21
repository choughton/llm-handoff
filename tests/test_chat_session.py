"""The process-wide chat session and the console reads that suspend it."""

from __future__ import annotations

import builtins
import io
import sys
import threading
import types
from typing import Any, Iterator

import pytest

from llm_handoff import agent_process, chat_session, orchestrator
from llm_handoff.chat_answers import ChatContext
from llm_handoff.chat_input import ChatInput
from llm_handoff.chat_line import ERASE_LINE, ChatLine


PROMPT = "[backend running] chat> "


class _IdleReader:
    """Fake ChatKeyReader that never sees a key."""

    def __init__(self, *, open_error: BaseException | None = None) -> None:
        self.open_count = 0
        self.close_count = 0
        self._open_error = open_error
        self._idle = threading.Event()

    def open(self) -> None:
        self.open_count += 1
        if self._open_error is not None:
            raise self._open_error

    def read_chars(self, timeout: float) -> str:
        self._idle.wait(min(timeout, 0.005))
        return ""

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture(autouse=True)
def _detached_session() -> Iterator[None]:
    chat_session.detach()
    yield
    chat_session.detach()


def _attached(
    reader: _IdleReader | None = None,
) -> tuple[ChatLine, ChatInput, io.StringIO, _IdleReader]:
    stream = io.StringIO()
    chat_line = ChatLine(stream, terminal_columns=lambda: 120)
    idle = reader if reader is not None else _IdleReader()
    chat_input = ChatInput(chat_line, idle, notify=lambda _m: None, poll_interval=0.01)
    chat_session.attach(chat_line, chat_input)
    return chat_line, chat_input, stream, idle


# --- nothing attached --------------------------------------------------------


def test_every_function_is_a_no_op_with_nothing_attached() -> None:
    chat_session.set_role("backend")
    chat_session.set_role(None)
    with chat_session.suspended():
        with chat_session.suspended():
            pass
    chat_session.detach()
    chat_session.detach()

    assert chat_session.SESSION.attached is False
    assert chat_session.SESSION.role is None


# --- set_role ------------------------------------------------------------------


def test_set_role_draws_the_line_and_starts_capture_then_none_stops_it() -> None:
    chat_line, chat_input, stream, reader = _attached()

    chat_session.set_role("backend")

    assert stream.getvalue() == PROMPT
    assert chat_line.drawn is True
    assert chat_input.running is True
    assert reader.open_count == 1

    chat_session.set_role(None)

    assert chat_input.running is False
    assert reader.close_count == 1
    assert chat_line.drawn is False
    assert stream.getvalue() == PROMPT + ERASE_LINE


def test_set_role_twice_with_the_same_value_is_harmless() -> None:
    chat_line, chat_input, _stream, reader = _attached()

    chat_session.set_role("backend")
    chat_session.set_role("backend")
    assert chat_input.running is True
    assert reader.open_count == 1

    chat_session.set_role(None)
    chat_session.set_role(None)
    assert chat_input.running is False
    assert reader.close_count == 1
    assert chat_line.drawn is False


def test_set_role_none_keeps_the_role_readable_while_capture_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chat_line, chat_input, _stream, _reader = _attached()
    chat_session.set_role("backend")
    seen: list[str | None] = []
    real_stop = chat_input.stop

    def recording_stop() -> None:
        seen.append(chat_session.SESSION.role)
        real_stop()
        seen.append(chat_session.SESSION.role)

    monkeypatch.setattr(chat_input, "stop", recording_stop)

    chat_session.set_role(None)

    assert seen == ["backend", "backend"]
    assert chat_session.SESSION.role is None
    assert chat_input.running is False


def test_a_start_that_raises_oserror_leaves_capture_off() -> None:
    chat_line, chat_input, stream, reader = _attached(
        _IdleReader(open_error=OSError("no console"))
    )

    chat_session.set_role("backend")

    assert reader.open_count == 1
    assert chat_input.running is False
    assert stream.getvalue() == PROMPT
    chat_session.set_role(None)
    assert chat_line.drawn is False


def test_detach_stops_capture_clears_the_line_and_forgets_both() -> None:
    chat_line, chat_input, _stream, reader = _attached()
    chat_session.set_role("backend")

    chat_session.detach()

    assert chat_input.running is False
    assert reader.close_count == 1
    assert chat_line.drawn is False
    assert chat_line.role is None
    assert chat_session.SESSION.attached is False
    chat_session.set_role("backend")
    assert chat_line.drawn is False
    assert reader.open_count == 1


def test_detach_keeps_the_role_readable_while_capture_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chat_line, chat_input, _stream, _reader = _attached()
    chat_session.set_role("backend")
    seen: list[str | None] = []
    real_stop = chat_input.stop

    def recording_stop() -> None:
        seen.append(chat_session.SESSION.role)
        real_stop()
        seen.append(chat_session.SESSION.role)

    monkeypatch.setattr(chat_input, "stop", recording_stop)

    chat_session.detach()

    assert seen == ["backend", "backend"]
    assert chat_session.SESSION.role is None
    assert chat_input.running is False


# --- suspended -------------------------------------------------------------------


def test_suspended_stops_capture_and_restores_the_remembered_role() -> None:
    chat_line, chat_input, stream, reader = _attached()
    chat_session.set_role("backend")

    with chat_session.suspended():
        assert chat_input.running is False
        assert chat_line.drawn is False
        assert stream.getvalue() == PROMPT + ERASE_LINE
        with chat_session.suspended():
            assert chat_input.running is False
        assert chat_input.running is False
        assert chat_line.drawn is False

    assert chat_input.running is True
    assert chat_line.drawn is True
    assert chat_line.role == "backend"
    assert stream.getvalue() == PROMPT + ERASE_LINE + PROMPT
    assert reader.open_count == 2
    assert reader.close_count == 1


def test_suspended_restores_the_role_when_the_block_raises() -> None:
    _chat_line, chat_input, _stream, _reader = _attached()
    chat_session.set_role("backend")

    with pytest.raises(RuntimeError):
        with chat_session.suspended():
            raise RuntimeError("boom")

    assert chat_input.running is True


def test_suspended_restarts_nothing_when_no_role_was_set() -> None:
    chat_line, chat_input, stream, reader = _attached()

    with chat_session.suspended():
        assert chat_input.running is False

    assert chat_input.running is False
    assert chat_line.drawn is False
    assert stream.getvalue() == ""
    assert reader.open_count == 0


# --- console reads run with capture stopped --------------------------------------


class _ScriptedLineReader:
    """Fake OperatorInputReader for the carry-over confirm."""

    def __init__(self, chat_input: ChatInput) -> None:
        self._chat_input = chat_input
        self.running_during_read: list[bool] = []
        self.eof_reached = False

    def drain(self) -> None:
        self.running_during_read.append(self._chat_input.running)

    def read_line(self, timeout: float) -> str | None:
        self.running_during_read.append(self._chat_input.running)
        return "yes"


def test_carry_over_confirm_reads_with_capture_stopped() -> None:
    _chat_line, chat_input, _stream, _reader = _attached()
    chat_session.set_role("backend")
    reader = _ScriptedLineReader(chat_input)
    logged: list[tuple[str, str]] = []

    confirmed = orchestrator._confirm_carry_over_at_terminal(
        reader,  # type: ignore[arg-type]
        "Restore the carry-over record? [y/N]",
        lambda level, message: logged.append((level, message)),
    )

    assert confirmed is True
    assert reader.running_during_read == [False, False]
    assert chat_input.running is True


def test_manual_continue_waits_with_capture_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chat_line, chat_input, _stream, _reader = _attached()
    chat_session.set_role("frontend")
    seen: list[bool] = []

    def getwch() -> str:
        seen.append(chat_input.running)
        return "x"

    def fake_input(*_args: Any) -> str:
        seen.append(chat_input.running)
        return ""

    # Windows reads the key through msvcrt; elsewhere a non-TTY stdin falls
    # through to input().
    monkeypatch.setitem(
        sys.modules, "msvcrt", types.SimpleNamespace(getwch=getwch)
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(builtins, "input", fake_input)

    agent_process._wait_for_manual_continue()

    assert seen == [False]
    assert chat_input.running is True


# --- chat context and answer worker ------------------------------------------


def _context(role: str = "backend") -> ChatContext:
    return ChatContext(
        role=role,
        provider="claude",
        story_id="S1",
        log_path=None,
        agent_config=None,
    )


class _FakeWorker:
    def __init__(self, *, busy: bool, wait_results: list[bool] | None = None) -> None:
        self._busy = busy
        self._wait_results = list(wait_results or [])
        self.wait_timeouts: list[float | None] = []
        self.stop_count = 0

    def busy(self) -> bool:
        return self._busy

    def wait_idle(self, timeout: float | None = None) -> bool:
        self.wait_timeouts.append(timeout)
        return self._wait_results.pop(0) if self._wait_results else True

    def stop(self) -> None:
        self.stop_count += 1


def test_set_role_stores_the_context_and_none_clears_it() -> None:
    _attached()
    context = _context()

    chat_session.set_role("backend", context=context)

    assert chat_session.SESSION.context is context

    chat_session.set_role(None)

    assert chat_session.SESSION.context is None


def test_set_role_without_a_context_stores_none() -> None:
    _attached()
    chat_session.set_role("backend", context=_context())

    chat_session.set_role("auditor")

    assert chat_session.SESSION.role == "auditor"
    assert chat_session.SESSION.context is None


def test_set_role_none_keeps_the_context_readable_while_capture_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chat_line, chat_input, _stream, _reader = _attached()
    context = _context()
    chat_session.set_role("backend", context=context)
    seen: list[ChatContext | None] = []
    real_stop = chat_input.stop

    def recording_stop() -> None:
        seen.append(chat_session.SESSION.context)
        real_stop()
        seen.append(chat_session.SESSION.context)

    monkeypatch.setattr(chat_input, "stop", recording_stop)

    chat_session.set_role(None)

    assert seen == [context, context]
    assert chat_session.SESSION.context is None


def test_suspended_restores_the_context_with_the_role() -> None:
    _attached()
    context = _context()
    chat_session.set_role("backend", context=context)

    with chat_session.suspended():
        assert chat_session.SESSION.context is None

    assert chat_session.SESSION.context is context


def test_set_role_stores_no_context_when_nothing_is_attached() -> None:
    chat_session.set_role("backend", context=_context())

    assert chat_session.SESSION.role is None
    assert chat_session.SESSION.context is None


def test_detach_clears_the_context_and_drops_the_worker_without_stopping_it() -> None:
    _attached()
    chat_session.set_role("backend", context=_context())
    worker = _FakeWorker(busy=True)
    chat_session.attach_worker(worker)

    chat_session.detach()

    assert chat_session.SESSION.context is None
    assert chat_session.SESSION.worker is None
    assert worker.stop_count == 0
    assert chat_session.wait_for_answers() is False


def test_wait_for_answers_returns_false_with_no_worker() -> None:
    logged: list[tuple[str, str]] = []

    assert chat_session.wait_for_answers(
        lambda level, message: logged.append((level, message))
    ) is False
    assert logged == []


def test_wait_for_answers_returns_false_and_logs_nothing_when_idle() -> None:
    worker = _FakeWorker(busy=False)
    chat_session.attach_worker(worker)
    logged: list[tuple[str, str]] = []

    assert chat_session.wait_for_answers(
        lambda level, message: logged.append((level, message))
    ) is False
    assert logged == []
    assert worker.wait_timeouts == []


def test_wait_for_answers_logs_once_and_waits_until_idle() -> None:
    worker = _FakeWorker(busy=True, wait_results=[False, True])
    chat_session.attach_worker(worker)
    logged: list[tuple[str, str]] = []

    waited = chat_session.wait_for_answers(
        lambda level, message: logged.append((level, message)), step=0.25
    )

    assert waited is True
    assert logged == [("INFO", "Waiting for the chat answer to finish.")]
    assert worker.wait_timeouts == [0.25, 0.25]


def test_wait_for_answers_without_a_log_still_waits() -> None:
    worker = _FakeWorker(busy=True, wait_results=[False, False, True])
    chat_session.attach_worker(worker)

    assert chat_session.wait_for_answers() is True
    assert worker.wait_timeouts == [0.5, 0.5, 0.5]
    chat_session.attach_worker(None)
    assert chat_session.SESSION.worker is None


# --- role dispatches kept for @role chat (CHAT-34) ------------------------------


def _record(
    role: str,
    session_id: str | None,
    *,
    story_id: str | None = "S1",
    provider: str | None = "claude",
) -> None:
    chat_session.record_role_dispatch(
        role, story_id=story_id, provider=provider, session_id=session_id
    )


def test_a_recorded_dispatch_is_the_latest_for_its_role() -> None:
    _record("backend", "b-1", story_id="S1", provider="codex")

    entry = chat_session.latest_role_dispatch("backend")

    assert entry == chat_session.RoleDispatch(
        role="backend", story_id="S1", provider="codex", session_id="b-1"
    )
    assert chat_session.latest_role_dispatch("auditor") is None


def test_a_later_dispatch_replaces_the_role_latest_entry() -> None:
    _record("backend", "b-1", story_id="S1")
    _record("backend", "b-2", story_id="S2")

    entry = chat_session.latest_role_dispatch("backend")

    assert entry is not None
    assert (entry.session_id, entry.story_id) == ("b-2", "S2")


def test_roles_keep_separate_entries() -> None:
    _record("backend", "b-1", provider="codex")
    _record("auditor", "a-1", provider="claude")

    backend = chat_session.latest_role_dispatch("backend")
    auditor = chat_session.latest_role_dispatch("auditor")

    assert backend is not None and auditor is not None
    assert (backend.session_id, backend.provider) == ("b-1", "codex")
    assert (auditor.session_id, auditor.provider) == ("a-1", "claude")


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_a_later_dispatch_without_a_session_hides_the_older_one(
    missing: str | None,
) -> None:
    _record("backend", "b-1")
    _record("backend", missing, story_id="S2")

    entry = chat_session.latest_role_dispatch("backend")

    assert entry is not None
    assert entry.session_id is None
    assert entry.story_id == "S2"


def test_entries_survive_role_changes_and_suspension() -> None:
    _attached()
    chat_session.set_role("backend", context=_context())
    _record("backend", "b-1")

    chat_session.set_role("auditor", context=_context("auditor"))
    with chat_session.suspended():
        entry = chat_session.latest_role_dispatch("backend")
        assert entry is not None and entry.session_id == "b-1"
    chat_session.set_role(None)

    entry = chat_session.latest_role_dispatch("backend")
    assert entry is not None and entry.session_id == "b-1"


def test_clear_drops_every_entry() -> None:
    _record("backend", "b-1")
    _record("auditor", "a-1")

    chat_session.clear_role_dispatches()

    assert chat_session.latest_role_dispatch("backend") is None
    assert chat_session.latest_role_dispatch("auditor") is None


@pytest.mark.parametrize("attached", [True, False])
def test_detach_drops_every_entry(attached: bool) -> None:
    if attached:
        _attached()
    _record("backend", "b-1")

    chat_session.detach()

    assert chat_session.latest_role_dispatch("backend") is None


def test_run_loop_start_drops_an_earlier_run_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record("backend", "old")
    seen: list[object] = []

    def fake_run(*_args: object, **_kwargs: object) -> int:
        seen.append(chat_session.latest_role_dispatch("backend"))
        return 0

    monkeypatch.setattr(orchestrator, "_run_loop", fake_run)
    monkeypatch.setattr(orchestrator, "_clear_handback_record", lambda *_a: True)

    assert orchestrator.run_loop(object(), log=lambda *_a: None) == 0  # type: ignore[arg-type]
    assert seen == [None]


def test_reads_from_another_thread_see_a_whole_entry() -> None:
    stop = threading.Event()
    bad: list[object] = []

    def reader() -> None:
        while not stop.is_set():
            entry = chat_session.latest_role_dispatch("backend")
            if entry is not None and entry.session_id != f"s-{entry.story_id}":
                bad.append(entry)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(2000):
            _record("backend", f"s-{index}", story_id=str(index))
    finally:
        stop.set()
        thread.join()

    assert bad == []


def test_chat_session_imports_no_chat_answers_at_runtime() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(chat_session))
    runtime_imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    modules = {
        getattr(node, "module", None) or node.names[0].name
        for node in runtime_imports
    }
    assert "llm_handoff.chat_answers" not in modules
