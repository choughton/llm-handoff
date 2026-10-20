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
