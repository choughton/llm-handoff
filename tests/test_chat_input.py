from __future__ import annotations

import io
import os
import threading
import time
from typing import Any

import pytest

from llm_handoff.chat_input import (
    CHAT_UNAVAILABLE_MESSAGE,
    ChatInput,
    PosixChatKeyReader,
    WindowsChatKeyReader,
    chat_input_for_console,
)
from llm_handoff.chat_line import ChatLine


# --- fakes -----------------------------------------------------------------


class _ScriptedReader:
    """Fake ChatKeyReader that returns scripted chunks, then times out."""

    def __init__(self, chunks: list[Any] | None = None) -> None:
        self.chunks: list[Any] = list(chunks or [])
        self.open_count = 0
        self.close_count = 0
        self.lock = threading.Lock()
        self.drained = threading.Event()

    def add(self, *chunks: Any) -> None:
        with self.lock:
            self.drained.clear()
            self.chunks.extend(chunks)

    def open(self) -> None:
        self.open_count += 1

    def read_chars(self, timeout: float) -> str:
        with self.lock:
            if self.chunks:
                chunk = self.chunks.pop(0)
                if not self.chunks:
                    self.drained.set()
            else:
                chunk = None
                self.drained.set()
        if chunk is None:
            time.sleep(min(timeout, 0.005))
            return ""
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self) -> None:
        self.close_count += 1


def _wait_until(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


def _chat(
    reader: Any | None = None,
) -> tuple[ChatInput, ChatLine, list[str], list[int]]:
    chat_line = ChatLine(io.StringIO(), terminal_columns=lambda: 120)
    notes: list[str] = []
    interrupts: list[int] = []
    chat = ChatInput(
        chat_line,
        reader if reader is not None else _ScriptedReader(),
        notify=notes.append,
        interrupt=lambda: interrupts.append(1),
        poll_interval=0.01,
    )
    return chat, chat_line, notes, interrupts


# --- editing rules -----------------------------------------------------------


def test_printable_characters_are_appended_and_shown() -> None:
    chat, chat_line, notes, _ = _chat()
    chat.feed("hi there")

    assert chat.buffer == "hi there"
    assert chat_line.buffer == "hi there"
    assert notes == []


def test_backspace_and_delete_remove_last_character() -> None:
    chat, chat_line, _, _ = _chat()
    chat.feed("abc\b")
    assert chat_line.buffer == "ab"
    chat.feed("\x7f")
    assert chat_line.buffer == "a"


def test_backspace_on_empty_buffer_does_nothing() -> None:
    chat, chat_line, notes, _ = _chat()
    chat.feed("\b\x7f\b")

    assert chat.buffer == ""
    assert chat_line.buffer == ""
    assert notes == []
    assert chat.take_pending() == []


def test_carriage_return_submits() -> None:
    chat, chat_line, notes, _ = _chat()
    chat.feed("hello\r")

    assert chat.take_pending() == ["hello"]
    assert chat_line.buffer == ""
    assert notes == [CHAT_UNAVAILABLE_MESSAGE]


def test_line_feed_submits() -> None:
    chat, _, notes, _ = _chat()
    chat.feed("hello\n")

    assert chat.take_pending() == ["hello"]
    assert notes == [CHAT_UNAVAILABLE_MESSAGE]


def test_crlf_is_one_submit_even_across_reads() -> None:
    chat, _, notes, _ = _chat()
    chat.feed("one\r\ntwo\r")
    chat.feed("\nthree\n\n")

    assert chat.take_pending() == ["one", "two", "three"]
    assert notes == [CHAT_UNAVAILABLE_MESSAGE] * 3


def test_ctrl_c_calls_interrupt_and_is_not_buffered() -> None:
    chat, chat_line, notes, interrupts = _chat()
    chat.feed("ab\x03c")

    assert interrupts == [1]
    assert chat_line.buffer == "abc"
    assert notes == []


def test_other_control_and_format_characters_are_ignored() -> None:
    chat, chat_line, _, interrupts = _chat()
    chat.feed("a\tb\x01\x1b​c d")

    assert chat_line.buffer == "abcd"
    assert interrupts == []


# --- submit -------------------------------------------------------------------


def test_empty_submit_does_nothing() -> None:
    chat, chat_line, notes, _ = _chat()
    chat.feed("\r")

    assert chat.take_pending() == []
    assert notes == []
    assert chat_line.buffer == ""


def test_whitespace_only_submit_clears_buffer_and_queues_nothing() -> None:
    chat, chat_line, notes, _ = _chat()
    chat.feed("   ")
    assert chat_line.buffer == "   "
    chat.feed("\r")

    assert chat.take_pending() == []
    assert notes == []
    assert chat.buffer == ""
    assert chat_line.buffer == ""


def test_submit_strips_text_and_take_pending_keeps_order_and_empties() -> None:
    chat, _, notes, _ = _chat()
    chat.feed("  first  \rsecond\r")

    assert chat.take_pending() == ["first", "second"]
    assert chat.take_pending() == []
    chat.feed("third\n")
    assert chat.take_pending() == ["third"]
    assert notes == [CHAT_UNAVAILABLE_MESSAGE] * 3


def test_notify_defaults_to_chat_line_write_above() -> None:
    stream = io.StringIO()
    chat_line = ChatLine(stream, terminal_columns=lambda: 120)
    chat = ChatInput(chat_line, _ScriptedReader(), interrupt=lambda: None)
    chat.feed("hello\r")

    assert CHAT_UNAVAILABLE_MESSAGE + "\n" in stream.getvalue()


def test_unavailable_message_text() -> None:
    assert CHAT_UNAVAILABLE_MESSAGE == (
        "Message received; chat answers are not available yet."
    )


# --- on_submit ------------------------------------------------------------------


def _submit_chat(
    on_submit: Any,
    reader: Any | None = None,
) -> tuple[ChatInput, ChatLine, list[str]]:
    chat_line = ChatLine(io.StringIO(), terminal_columns=lambda: 120)
    notes: list[str] = []
    chat = ChatInput(
        chat_line,
        reader if reader is not None else _ScriptedReader(),
        notify=notes.append,
        interrupt=lambda: None,
        poll_interval=0.01,
        on_submit=on_submit,
    )
    return chat, chat_line, notes


def test_on_submit_receives_stripped_text_and_its_notice_is_shown() -> None:
    calls: list[str] = []

    def on_submit(text: str) -> str:
        calls.append(text)
        return f"saved {text}"

    chat, chat_line, notes = _submit_chat(on_submit)
    chat.feed("  hello there  \r")

    assert calls == ["hello there"]
    assert notes == ["saved hello there"]
    assert chat.take_pending() == []
    assert chat.buffer == ""
    assert chat_line.buffer == ""


def test_on_submit_is_never_called_for_blank_lines() -> None:
    calls: list[str] = []
    chat, chat_line, notes = _submit_chat(lambda text: calls.append(text) or "x")
    chat.feed("\r   \r\n\t\r")

    assert calls == []
    assert notes == []
    assert chat.buffer == ""
    assert chat_line.buffer == ""


def test_on_submit_oserror_keeps_buffer_and_enter_retries() -> None:
    calls: list[str] = []
    failures = [OSError("disk full")]

    def on_submit(text: str) -> str:
        calls.append(text)
        if failures:
            raise failures.pop()
        return "saved"

    chat, chat_line, notes = _submit_chat(on_submit)
    chat.feed("hello\r")

    assert calls == ["hello"]
    assert notes == ["Chat message not saved; press Enter to try again."]
    assert chat.buffer == "hello"
    assert chat_line.buffer == "hello"
    assert chat.take_pending() == []

    chat.feed("\r")

    assert calls == ["hello", "hello"]
    assert notes[-1] == "saved"
    assert chat.buffer == ""
    assert chat.take_pending() == []


def test_on_submit_oserror_keeps_the_capture_thread_running() -> None:
    calls: list[str] = []
    failures = [OSError("disk full")]

    def on_submit(text: str) -> str:
        calls.append(text)
        if failures:
            raise failures.pop()
        return "saved"

    reader = _ScriptedReader(["hello\r"])
    chat, _chat_line, notes = _submit_chat(on_submit, reader)
    chat.start()
    try:
        assert _wait_until(
            lambda: notes == ["Chat message not saved; press Enter to try again."]
        )
        assert chat.running
        assert chat.buffer == "hello"
        reader.add("\r")
        assert _wait_until(lambda: notes[-1:] == ["saved"])
        assert calls == ["hello", "hello"]
        assert chat.running
    finally:
        chat.stop()
    assert chat.take_pending() == []


def test_stale_generation_submission_never_calls_on_submit() -> None:
    calls: list[str] = []
    chat, chat_line, notes = _submit_chat(lambda text: calls.append(text) or "x")
    chat.feed("hello")
    stale = chat._generation
    chat._generation += 1

    chat._submit(stale)

    assert calls == []
    assert notes == []
    assert chat.buffer == "hello"
    assert chat_line.buffer == "hello"


def test_stop_waits_for_an_in_progress_on_submit() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def on_submit(text: str) -> str:
        calls.append(text)
        entered.set()
        release.wait(5)
        return "saved"

    reader = _ScriptedReader(["hello\r"])
    chat, _chat_line, notes = _submit_chat(on_submit, reader)
    chat.start()
    stop_done = threading.Event()
    stopper: threading.Thread | None = None
    try:
        assert entered.wait(2)
        stopper = threading.Thread(target=lambda: (chat.stop(), stop_done.set()))
        stopper.start()
        # stop() is blocked behind the in-progress save.
        assert not stop_done.wait(0.05)
        release.set()
        assert stop_done.wait(2)
    finally:
        release.set()
        if stopper is not None:
            stopper.join(2)
        chat.stop()
    assert calls == ["hello"]
    assert chat.buffer == ""
    # The generation went stale during the save, so no late notice.
    assert notes == []
    assert reader.close_count == 1


def test_submission_after_stop_saves_nothing() -> None:
    calls: list[str] = []
    reader = _ScriptedReader(["par"])
    chat, _chat_line, _notes = _submit_chat(
        lambda text: calls.append(text) or "x", reader
    )
    chat.start()
    assert _wait_until(lambda: chat.buffer == "par" and reader.drained.is_set())
    chat.stop()
    stale = chat._generation - 1

    chat._submit(stale)

    assert calls == []
    assert chat.buffer == "par"


def test_factory_passes_on_submit_through() -> None:
    def on_submit(text: str) -> str:
        return text

    chat = chat_input_for_console(
        _Stream(True), _plain_chat_line(), on_submit=on_submit
    )

    assert isinstance(chat, ChatInput)
    assert chat._on_submit is on_submit


# --- lifecycle ------------------------------------------------------------------


def test_start_reads_on_thread_and_stop_closes_reader() -> None:
    reader = _ScriptedReader(["he", "llo\r"])
    chat, _, notes, _ = _chat(reader)

    chat.start()
    assert chat.running
    assert reader.open_count == 1
    assert _wait_until(lambda: notes == [CHAT_UNAVAILABLE_MESSAGE])
    chat.stop()

    assert not chat.running
    assert reader.close_count == 1
    assert chat.take_pending() == ["hello"]


def test_second_start_while_running_does_nothing() -> None:
    reader = _ScriptedReader()
    chat, _, _, _ = _chat(reader)

    chat.start()
    chat.start()
    assert reader.open_count == 1
    chat.stop()
    assert reader.close_count == 1


def test_stop_is_idempotent_and_safe_before_start() -> None:
    reader = _ScriptedReader()
    chat, _, _, _ = _chat(reader)

    chat.stop()
    assert reader.close_count == 0
    chat.start()
    chat.stop()
    chat.stop()
    assert reader.open_count == 1
    assert reader.close_count == 1


def test_start_stop_start_restarts_and_keeps_buffer_and_pending() -> None:
    reader = _ScriptedReader(["first\r", "par"])
    chat, chat_line, _, _ = _chat(reader)

    chat.start()
    assert _wait_until(lambda: chat.buffer == "par")
    chat.stop()
    assert chat.buffer == "par"
    assert chat_line.buffer == "par"

    reader.add("tial\r")
    chat.start()
    assert chat.running
    assert _wait_until(lambda: chat.buffer == "" and reader.drained.is_set())
    chat.stop()

    assert reader.open_count == 2
    assert reader.close_count == 2
    assert chat.take_pending() == ["first", "partial"]


def test_eof_ends_thread_quietly_and_stop_still_closes() -> None:
    reader = _ScriptedReader(["abc", EOFError()])
    chat, _, _, _ = _chat(reader)

    chat.start()
    assert _wait_until(lambda: not chat.running)
    assert chat.buffer == "abc"
    assert reader.close_count == 0
    chat.stop()
    assert reader.close_count == 1
    chat.stop()
    assert reader.close_count == 1


def test_oserror_ends_thread_and_start_after_eof_reopens() -> None:
    reader = _ScriptedReader([OSError("gone")])
    chat, _, _, _ = _chat(reader)

    chat.start()
    assert _wait_until(lambda: not chat.running)
    chat.start()
    assert reader.close_count == 1
    assert reader.open_count == 2
    chat.stop()
    assert reader.close_count == 2


def _blocking_notify_chat(
    reader: Any,
) -> tuple[ChatInput, threading.Event, threading.Event, list[str]]:
    entered = threading.Event()
    release = threading.Event()
    notes: list[str] = []

    def notify(text: str) -> None:
        notes.append(text)
        entered.set()
        release.wait(5)

    chat = ChatInput(
        ChatLine(io.StringIO(), terminal_columns=lambda: 120),
        reader,
        notify=notify,
        interrupt=lambda: None,
        poll_interval=0.01,
    )
    return chat, entered, release, notes


def _capture_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "llm-handoff-chat-input" and thread.is_alive()
    ]


def test_slow_callback_across_stop_and_restart_keeps_one_worker() -> None:
    reader = _ScriptedReader(["first\rsecond\r"])
    chat, entered, release, notes = _blocking_notify_chat(reader)

    chat.start()
    try:
        assert entered.wait(2)
        worker = chat._thread
        assert worker is not None
        chat.stop()
        # The worker outlived the bounded join but still belongs to chat.
        assert not chat.running
        assert worker.is_alive()
        assert chat._thread is worker
        assert reader.close_count == 1

        chat.start()
        assert chat.running
        assert reader.open_count == 2
        assert chat._thread is worker
        assert _capture_threads() == [worker]

        release.set()
        reader.add("third\r")
        assert _wait_until(lambda: len(notes) == 2)
        # "second" was read before stop and is dropped, not submitted late.
        assert chat.take_pending() == ["first", "third"]
        assert _capture_threads() == [worker]
    finally:
        release.set()
        chat.stop()
    assert _wait_until(lambda: not worker.is_alive())
    assert reader.close_count == 2


def test_slow_callback_after_stop_submits_nothing_more_and_exits() -> None:
    reader = _ScriptedReader(["first\rsecond\r"])
    chat, entered, release, notes = _blocking_notify_chat(reader)

    chat.start()
    assert entered.wait(2)
    worker = chat._thread
    assert worker is not None
    chat.stop()
    assert worker.is_alive()

    release.set()
    assert _wait_until(lambda: not worker.is_alive())
    assert chat._thread is None
    assert notes == [CHAT_UNAVAILABLE_MESSAGE]
    assert chat.take_pending() == ["first"]
    assert reader.close_count == 1
    chat.stop()
    assert reader.close_count == 1


def test_slow_renderer_across_stop_drops_rest_of_batch() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _SlowChatLine(ChatLine):
        def set_buffer(self, text: str) -> None:
            super().set_buffer(text)
            if text == "a":
                entered.set()
                release.wait(5)

    reader = _ScriptedReader(["abc"])
    chat = ChatInput(
        _SlowChatLine(io.StringIO(), terminal_columns=lambda: 120),
        reader,
        notify=lambda text: None,
        interrupt=lambda: None,
        poll_interval=0.01,
    )
    chat.start()
    assert entered.wait(2)
    worker = chat._thread
    assert worker is not None
    chat.stop()
    release.set()
    assert _wait_until(lambda: not worker.is_alive())
    assert chat.buffer == "a"


def _blocking_clear_chat(
    reader: Any,
) -> tuple[ChatInput, threading.Event, threading.Event, list[str]]:
    """ChatInput whose renderer blocks on the first submit's buffer clear."""

    entered = threading.Event()
    release = threading.Event()
    notes: list[str] = []

    class _SlowClearChatLine(ChatLine):
        def set_buffer(self, text: str) -> None:
            super().set_buffer(text)
            if text == "" and not entered.is_set():
                entered.set()
                release.wait(5)

    chat = ChatInput(
        _SlowClearChatLine(io.StringIO(), terminal_columns=lambda: 120),
        reader,
        notify=notes.append,
        interrupt=lambda: None,
        poll_interval=0.01,
    )
    return chat, entered, release, notes


def test_slow_renderer_during_submit_never_submits_or_notifies_after_stop() -> None:
    reader = _ScriptedReader(["hello\r"])
    chat, entered, release, notes = _blocking_clear_chat(reader)

    chat.start()
    try:
        assert entered.wait(2)
        worker = chat._thread
        assert worker is not None
        chat.stop()
        # Enter was handled before stop, so the message is already queued.
        assert chat.take_pending() == ["hello"]
        assert notes == []
    finally:
        release.set()
    assert _wait_until(lambda: not worker.is_alive())
    assert chat.take_pending() == []
    assert notes == []
    assert reader.close_count == 1


def test_slow_renderer_during_submit_across_restart_drops_late_notification() -> None:
    reader = _ScriptedReader(["hello\r"])
    chat, entered, release, notes = _blocking_clear_chat(reader)

    chat.start()
    try:
        assert entered.wait(2)
        worker = chat._thread
        assert worker is not None
        chat.stop()
        chat.start()
        assert chat._thread is worker
        assert chat.take_pending() == ["hello"]

        release.set()
        reader.add("next\r")
        assert _wait_until(lambda: notes == [CHAT_UNAVAILABLE_MESSAGE])
        assert chat.take_pending() == ["next"]
        time.sleep(0.05)
        # Only the post-restart submit notified; the stale one never did.
        assert notes == [CHAT_UNAVAILABLE_MESSAGE]
    finally:
        release.set()
        chat.stop()
    assert _wait_until(lambda: not worker.is_alive())


@pytest.fixture
def thread_errors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    errors: list[str] = []

    def hook(args: Any) -> None:
        errors.append(type(args.exc_value).__name__)

    monkeypatch.setattr(threading, "excepthook", hook)
    return errors


def test_notify_exception_releases_worker_and_stop_start_recovers(
    thread_errors: list[str],
) -> None:
    reader = _ScriptedReader()
    notes: list[str] = []
    failures = [OSError("output unavailable")]

    def notify(text: str) -> None:
        if failures:
            raise failures.pop()
        notes.append(text)

    chat = ChatInput(
        ChatLine(io.StringIO(), terminal_columns=lambda: 120),
        reader,
        notify=notify,
        interrupt=lambda: None,
        poll_interval=0.01,
    )
    chat.start()
    worker = chat._thread
    assert worker is not None
    reader.add("hello\r")
    assert _wait_until(lambda: not worker.is_alive())
    assert thread_errors == ["OSError"]
    assert chat._thread is None
    assert not chat.running

    chat.stop()
    assert reader.close_count == 1
    reader.add("again\r")
    chat.start()
    try:
        assert chat.running
        assert chat._thread is not None and chat._thread is not worker
        assert reader.open_count == 2
        assert _wait_until(lambda: notes == [CHAT_UNAVAILABLE_MESSAGE])
        assert chat.take_pending() == ["hello", "again"]
    finally:
        chat.stop()
    assert reader.close_count == 2


def test_interrupt_exception_releases_worker_and_start_reopens(
    thread_errors: list[str],
) -> None:
    reader = _ScriptedReader()
    calls: list[int] = []

    def interrupt() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("no main thread")

    chat = ChatInput(
        ChatLine(io.StringIO(), terminal_columns=lambda: 120),
        reader,
        notify=lambda text: None,
        interrupt=interrupt,
        poll_interval=0.01,
    )
    chat.start()
    worker = chat._thread
    assert worker is not None
    reader.add("a\x03")
    assert _wait_until(lambda: not worker.is_alive())
    assert thread_errors == ["RuntimeError"]
    assert not chat.running

    # start() without stop() closes the earlier open before reopening.
    reader.add("b")
    chat.start()
    try:
        assert chat.running
        assert reader.close_count == 1
        assert reader.open_count == 2
        assert _wait_until(lambda: chat.buffer == "ab")
    finally:
        chat.stop()
    assert reader.close_count == 2


def test_context_manager_starts_and_stops() -> None:
    reader = _ScriptedReader(["x"])
    chat, _, _, _ = _chat(reader)

    with chat as entered:
        assert entered is chat
        assert chat.running
        assert _wait_until(lambda: chat.buffer == "x")
    assert not chat.running
    assert reader.open_count == 1
    assert reader.close_count == 1


def test_running_is_read_only() -> None:
    chat, _, _, _ = _chat()
    with pytest.raises(AttributeError):
        chat.running = True  # type: ignore[misc]


# --- Windows reader ---------------------------------------------------------------


class _FakeMsvcrt:
    def __init__(self, keys: str) -> None:
        self.keys = list(keys)
        self.putwch_calls: list[str] = []

    def kbhit(self) -> bool:
        return bool(self.keys)

    def getwch(self) -> str:
        return self.keys.pop(0)

    def putwch(self, char: str) -> None:
        self.putwch_calls.append(char)


def test_windows_reader_reads_without_echo() -> None:
    msvcrt = _FakeMsvcrt("hi\r")
    reader = WindowsChatKeyReader(msvcrt)
    reader.open()

    assert reader.read_chars(0) == "hi\r"
    assert reader.read_chars(0) == ""
    assert msvcrt.putwch_calls == []
    reader.close()


def test_windows_reader_drops_prefix_keys() -> None:
    msvcrt = _FakeMsvcrt("a\x00;b\xe0Hc")
    reader = WindowsChatKeyReader(msvcrt)

    assert reader.read_chars(0) == "abc"


def test_windows_reader_drops_prefix_code_arriving_in_next_read() -> None:
    msvcrt = _FakeMsvcrt("a\xe0")
    reader = WindowsChatKeyReader(msvcrt)

    assert reader.read_chars(0) == "a"
    msvcrt.keys.extend("Kz")
    assert reader.read_chars(0) == "z"


def test_windows_reader_passes_ctrl_c_through() -> None:
    reader = WindowsChatKeyReader(_FakeMsvcrt("\x03"))

    assert reader.read_chars(0) == "\x03"


# --- POSIX reader -------------------------------------------------------------------


class _FakeTermiosError(Exception):
    pass


class _FakeTermios:
    ICANON = 0x0002
    ECHO = 0x0008
    ISIG = 0x0001
    IEXTEN = 0x8000
    VMIN = 6
    VTIME = 5
    TCSANOW = 0
    TCSADRAIN = 1
    error = _FakeTermiosError

    def __init__(self) -> None:
        self.lflag = self.ICANON | self.ECHO | self.ISIG | self.IEXTEN
        self.cc = [b"\x00"] * 20
        self.set_calls: list[tuple[int, int, list[Any]]] = []

    def tcgetattr(self, fd: int) -> list[Any]:
        return [0x100, 0x200, 0x300, self.lflag, 38400, 38400, list(self.cc)]

    def tcsetattr(self, fd: int, when: int, attrs: list[Any]) -> None:
        self.set_calls.append((fd, when, attrs))


class _FakeSelect:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[tuple[Any, ...]] = []

    def select(self, rlist: list[int], wlist: list[int], xlist: list[int], timeout: float):
        self.calls.append((rlist, wlist, xlist, timeout))
        return (list(rlist) if self.ready else [], [], [])


@pytest.fixture
def pipe_fds():
    read_fd, write_fd = os.pipe()
    try:
        yield read_fd, write_fd
    finally:
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_posix_open_clears_icanon_and_echo_and_keeps_isig() -> None:
    termios = _FakeTermios()
    reader = PosixChatKeyReader(7, "utf-8", termios, _FakeSelect())
    reader.open()

    assert len(termios.set_calls) == 1
    fd, when, attrs = termios.set_calls[0]
    assert fd == 7
    assert when == termios.TCSANOW
    lflag = attrs[3]
    assert not lflag & termios.ICANON
    assert not lflag & termios.ECHO
    assert lflag & termios.ISIG
    assert lflag & termios.IEXTEN
    assert attrs[6][termios.VMIN] == 1
    assert attrs[6][termios.VTIME] == 0
    assert attrs[:3] == [0x100, 0x200, 0x300]


def test_posix_close_restores_saved_attributes_with_tcsadrain() -> None:
    termios = _FakeTermios()
    saved = termios.tcgetattr(7)
    reader = PosixChatKeyReader(7, "utf-8", termios, _FakeSelect())
    reader.open()
    reader.close()

    assert len(termios.set_calls) == 2
    fd, when, attrs = termios.set_calls[1]
    assert fd == 7
    assert when == termios.TCSADRAIN
    assert attrs == saved
    assert attrs[3] & termios.ICANON and attrs[3] & termios.ECHO


def test_posix_close_twice_or_before_open_does_nothing() -> None:
    termios = _FakeTermios()
    reader = PosixChatKeyReader(7, "utf-8", termios, _FakeSelect())
    reader.close()
    assert termios.set_calls == []

    reader.open()
    reader.close()
    reader.close()
    assert len(termios.set_calls) == 2


def test_posix_close_swallows_termios_error() -> None:
    termios = _FakeTermios()
    reader = PosixChatKeyReader(7, "utf-8", termios, _FakeSelect())
    reader.open()

    def fail(fd: int, when: int, attrs: list[Any]) -> None:
        raise _FakeTermiosError("tty gone")

    termios.tcsetattr = fail  # type: ignore[method-assign]
    reader.close()


def test_posix_read_returns_empty_on_timeout(pipe_fds) -> None:
    read_fd, _write_fd = pipe_fds
    select = _FakeSelect(ready=False)
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), select)

    assert reader.read_chars(0.25) == ""
    assert select.calls == [([read_fd], [], [], 0.25)]


def test_posix_read_drops_escape_sequences(pipe_fds) -> None:
    read_fd, write_fd = pipe_fds
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), _FakeSelect())

    os.write(write_fd, b"ab\x1b[A")
    assert reader.read_chars(0) == "ab"
    os.write(write_fd, b"\x1b[1;5C")
    assert reader.read_chars(0) == ""
    os.write(write_fd, b"c")
    assert reader.read_chars(0) == "c"


def test_posix_read_keeps_text_after_escape_sequence_in_same_read(pipe_fds) -> None:
    read_fd, write_fd = pipe_fds
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), _FakeSelect())

    os.write(write_fd, b"\x1b[Ahello\x1bOPx\x1b[1;5Cy\x1bz!")
    assert reader.read_chars(0) == "hellox" + "y" + "!"


@pytest.mark.parametrize(
    "chunks",
    [
        (b"\x1b", b"[Ahello"),
        (b"\x1b[", b"Ahello"),
        (b"\x1b[1;", b"5Chello"),
        (b"\x1bO", b"Phello"),
    ],
)
def test_posix_read_drops_escape_sequence_split_across_reads(
    pipe_fds, chunks
) -> None:
    read_fd, write_fd = pipe_fds
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), _FakeSelect())

    os.write(write_fd, b"ab" + chunks[0])
    assert reader.read_chars(0) == "ab"
    os.write(write_fd, chunks[1])
    assert reader.read_chars(0) == "hello"


def test_posix_read_lone_escape_then_quiet_poll_keeps_next_key(pipe_fds) -> None:
    read_fd, write_fd = pipe_fds
    select = _FakeSelect()
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), select)

    os.write(write_fd, b"\x1b")
    assert reader.read_chars(0) == ""
    select.ready = False
    assert reader.read_chars(0) == ""
    select.ready = True
    os.write(write_fd, b"[x")
    assert reader.read_chars(0) == "[x"


@pytest.mark.parametrize(
    "chunks",
    [
        (b"\x1b[", b"Ahello"),
        (b"\x1b[1;", b"5Chello"),
        (b"\x1bO", b"Phello"),
        (b"\x1b(", b"Bhello"),
    ],
)
def test_posix_read_quiet_poll_inside_sequence_keeps_escape_state(
    pipe_fds, chunks
) -> None:
    read_fd, write_fd = pipe_fds
    select = _FakeSelect()
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), select)

    os.write(write_fd, b"ab" + chunks[0])
    assert reader.read_chars(0) == "ab"
    select.ready = False
    assert reader.read_chars(0) == ""
    assert reader.read_chars(0) == ""
    select.ready = True
    os.write(write_fd, chunks[1])
    assert reader.read_chars(0) == "hello"


def test_posix_read_control_char_ends_escape_and_is_kept(pipe_fds) -> None:
    read_fd, write_fd = pipe_fds
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), _FakeSelect())

    os.write(write_fd, b"hi\x1b\r")
    assert reader.read_chars(0) == "hi\r"
    os.write(write_fd, b"\x1b[\x7fz")
    assert reader.read_chars(0) == "\x7fz"


def test_posix_open_resets_split_escape_state(pipe_fds) -> None:
    read_fd, write_fd = pipe_fds
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), _FakeSelect())

    os.write(write_fd, b"\x1b[")
    assert reader.read_chars(0) == ""
    reader.open()
    os.write(write_fd, b"Ahello")
    assert reader.read_chars(0) == "Ahello"
    reader.close()


def test_posix_read_decodes_utf8_split_across_reads(pipe_fds) -> None:
    read_fd, write_fd = pipe_fds
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), _FakeSelect())
    encoded = "é€".encode("utf-8")

    os.write(write_fd, b"x" + encoded[:1])
    assert reader.read_chars(0) == "x"
    os.write(write_fd, encoded[1:3])
    assert reader.read_chars(0) == "é"
    os.write(write_fd, encoded[3:])
    assert reader.read_chars(0) == "€"


def test_posix_read_replaces_invalid_bytes(pipe_fds) -> None:
    read_fd, write_fd = pipe_fds
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), _FakeSelect())

    os.write(write_fd, b"a\xffb")
    assert reader.read_chars(0) == "a�b"


def test_posix_read_raises_eof_when_input_closed(pipe_fds) -> None:
    read_fd, write_fd = pipe_fds
    reader = PosixChatKeyReader(read_fd, "utf-8", _FakeTermios(), _FakeSelect())
    os.close(write_fd)

    with pytest.raises(EOFError):
        reader.read_chars(0)


# --- factory ----------------------------------------------------------------------


class _Stream:
    def __init__(self, tty: Any) -> None:
        self._tty = tty
        self.encoding = "utf-8"

    def isatty(self) -> bool:
        if isinstance(self._tty, BaseException):
            raise self._tty
        return self._tty

    def fileno(self) -> int:
        return 0


def _plain_chat_line() -> ChatLine:
    return ChatLine(io.StringIO(), terminal_columns=lambda: 80)


def test_factory_returns_none_for_non_tty_stream() -> None:
    assert chat_input_for_console(_Stream(False), _plain_chat_line()) is None
    assert chat_input_for_console(io.StringIO(), _plain_chat_line()) is None


def test_factory_returns_none_when_isatty_raises() -> None:
    assert chat_input_for_console(_Stream(OSError("bad")), _plain_chat_line()) is None
    assert chat_input_for_console(_Stream(ValueError("closed")), _plain_chat_line()) is None


def test_factory_returns_none_when_isatty_is_missing() -> None:
    assert chat_input_for_console(object(), _plain_chat_line()) is None


def test_factory_builds_platform_reader_for_tty_and_passes_kwargs() -> None:
    notes: list[str] = []
    chat = chat_input_for_console(
        _Stream(True), _plain_chat_line(), notify=notes.append, poll_interval=0.02
    )

    assert isinstance(chat, ChatInput)
    expected = WindowsChatKeyReader if os.name == "nt" else PosixChatKeyReader
    assert isinstance(chat._reader, expected)
    assert chat._poll_interval == 0.02
    assert not chat.running
