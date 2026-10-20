"""Keystroke capture for the console chat line.

While a role runs, a background thread reads keystrokes from the console
without echo and edits a ``ChatLine`` buffer. Enter queues the line as a
pending message. Nothing creates a ``ChatInput`` outside tests yet (CHAT-11
wires it in, and CHAT-2 consumes the pending messages).

The platform readers take their platform modules (``msvcrt``, or ``termios``
and ``select``) as constructor arguments, so tests can pass fakes on any OS.
"""

from __future__ import annotations

import _thread
import codecs
import os
import threading
import time
import unicodedata
from typing import Any, Callable, Final, Protocol

from llm_handoff.chat_line import ChatLine


CHAT_UNAVAILABLE_MESSAGE: Final[str] = (
    "Message received; chat answers are not available yet."
)

_BACKSPACE_CHARS: Final[frozenset[str]] = frozenset({"\b", "\x7f"})
_WINDOWS_PREFIX_CHARS: Final[frozenset[str]] = frozenset({"\x00", "\xe0"})
_CTRL_C: Final[str] = "\x03"
_ESCAPE: Final[str] = "\x1b"


class ChatKeyReader(Protocol):
    """Non-blocking, non-echoing console key input for the capture thread."""

    def open(self) -> None:
        """Prepare the console for reading keys."""

    def read_chars(self, timeout: float) -> str:
        """Return the characters typed within ``timeout`` seconds.

        Returns ``""`` on timeout and raises ``EOFError`` when the input is
        closed.
        """

    def close(self) -> None:
        """Undo what ``open`` changed."""


class WindowsChatKeyReader:
    """Console keys through ``msvcrt.kbhit`` and ``msvcrt.getwch`` (no echo).

    Function and arrow keys arrive as a ``"\\x00"`` or ``"\\xe0"`` prefix plus
    a code; both are dropped.
    """

    def __init__(self, msvcrt_module: Any) -> None:
        self._msvcrt = msvcrt_module
        self._drop_next = False

    def open(self) -> None:
        return None

    def read_chars(self, timeout: float) -> str:
        deadline = time.monotonic() + max(timeout, 0)
        chars: list[str] = []
        while True:
            while self._msvcrt.kbhit():
                char = self._msvcrt.getwch()
                if self._drop_next:
                    self._drop_next = False
                    continue
                if char in _WINDOWS_PREFIX_CHARS:
                    if self._msvcrt.kbhit():
                        self._msvcrt.getwch()
                    else:
                        self._drop_next = True
                    continue
                chars.append(char)
            remaining = deadline - time.monotonic()
            if chars or remaining <= 0:
                return "".join(chars)
            time.sleep(min(0.01, remaining))

    def close(self) -> None:
        return None


class PosixChatKeyReader:
    """Console keys through ``select`` and ``os.read`` in non-canonical mode.

    ``open`` clears ``ICANON`` and ``ECHO`` and keeps ``ISIG``, so the
    terminal still turns Ctrl+C into SIGINT. ``close`` restores the saved
    attributes.
    """

    def __init__(
        self,
        fd: int,
        encoding: str | None,
        termios_module: Any,
        select_module: Any,
    ) -> None:
        self._fd = fd
        self._encoding = encoding or "utf-8"
        self._termios = termios_module
        self._select = select_module
        self._saved: list[Any] | None = None
        self._decoder = self._new_decoder()

    def _new_decoder(self) -> codecs.IncrementalDecoder:
        try:
            factory = codecs.getincrementaldecoder(self._encoding)
        except LookupError:
            factory = codecs.getincrementaldecoder("utf-8")
        return factory(errors="replace")

    def open(self) -> None:
        termios = self._termios
        saved = termios.tcgetattr(self._fd)
        attrs = list(saved)
        # ISIG is deliberately left set: Ctrl+C must still raise SIGINT.
        attrs[3] = attrs[3] & ~(termios.ICANON | termios.ECHO)
        cc = list(attrs[6])
        cc[termios.VMIN] = 1
        cc[termios.VTIME] = 0
        attrs[6] = cc
        termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
        self._saved = saved
        self._decoder = self._new_decoder()

    def read_chars(self, timeout: float) -> str:
        ready, _write, _error = self._select.select(
            [self._fd], [], [], max(timeout, 0)
        )
        if not ready:
            return ""
        data = os.read(self._fd, 4096)
        if not data:
            raise EOFError
        text = self._decoder.decode(data)
        escape_at = text.find(_ESCAPE)
        if escape_at >= 0:
            # Arrow and function keys arrive as an escape sequence in one read.
            text = text[:escape_at]
        return text

    def close(self) -> None:
        saved, self._saved = self._saved, None
        if saved is None:
            return
        errors: tuple[type[BaseException], ...] = (OSError, ValueError)
        termios_error = getattr(self._termios, "error", None)
        if isinstance(termios_error, type) and issubclass(
            termios_error, BaseException
        ):
            errors = errors + (termios_error,)
        try:
            self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, saved)
        except errors:
            return None


def _is_ignored_control(char: str) -> bool:
    return unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


class ChatInput:
    """Background keystroke capture that edits a ``ChatLine`` buffer."""

    def __init__(
        self,
        chat_line: ChatLine,
        reader: ChatKeyReader,
        *,
        notify: Callable[[str], None] | None = None,
        interrupt: Callable[[], None] | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        self._chat_line = chat_line
        self._reader = reader
        self._notify = notify if notify is not None else chat_line.write_above
        self._interrupt = interrupt if interrupt is not None else _thread.interrupt_main
        self._poll_interval = poll_interval
        self._buffer = ""
        self._after_cr = False
        self._pending: list[str] = []
        self._pending_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def buffer(self) -> str:
        return self._buffer

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    return
                # The thread ended on its own (input closed); finish that
                # start before opening the reader again.
                self._finish_locked()
            self._reader.open()
            self._stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(self._stop_event,),
                name="llm-handoff-chat-input",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._thread is None:
                return
            self._finish_locked()

    def _finish_locked(self) -> None:
        thread = self._thread
        self._thread = None
        self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(self._poll_interval * 4, 0.2))
        self._reader.close()

    def __enter__(self) -> ChatInput:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def take_pending(self) -> list[str]:
        with self._pending_lock:
            pending, self._pending = self._pending, []
        return pending

    def feed(self, chars: str) -> None:
        """Apply the editing rules to ``chars`` (the capture thread's input)."""

        for char in chars:
            self._handle_char(char)

    def _run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                chars = self._reader.read_chars(self._poll_interval)
            except (EOFError, OSError):
                return
            if chars and not stop_event.is_set():
                self.feed(chars)

    def _handle_char(self, char: str) -> None:
        if char == "\n" and self._after_cr:
            self._after_cr = False
            return
        self._after_cr = char == "\r"
        if char in ("\r", "\n"):
            self._submit()
        elif char in _BACKSPACE_CHARS:
            if self._buffer:
                self._set_buffer(self._buffer[:-1])
        elif char == _CTRL_C:
            self._interrupt()
        elif _is_ignored_control(char):
            return
        else:
            self._set_buffer(self._buffer + char)

    def _set_buffer(self, text: str) -> None:
        self._buffer = text
        self._chat_line.set_buffer(text)

    def _submit(self) -> None:
        text = self._buffer.strip()
        self._set_buffer("")
        if not text:
            return
        with self._pending_lock:
            self._pending.append(text)
        self._notify(CHAT_UNAVAILABLE_MESSAGE)


def _stream_is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001 - any failure means "not a terminal"
        return False


def chat_input_for_console(
    stdin: Any,
    chat_line: ChatLine,
    **kwargs: Any,
) -> ChatInput | None:
    """Return a ``ChatInput`` for a TTY ``stdin``, else None."""

    if not _stream_is_tty(stdin):
        return None
    reader: ChatKeyReader
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            return None
        reader = WindowsChatKeyReader(msvcrt)
    else:
        try:
            import select
            import termios

            fd = stdin.fileno()
        except (AttributeError, ImportError, OSError, ValueError):
            return None
        reader = PosixChatKeyReader(
            fd, getattr(stdin, "encoding", None), termios, select
        )
    return ChatInput(chat_line, reader, **kwargs)


__all__ = [
    "CHAT_UNAVAILABLE_MESSAGE",
    "ChatInput",
    "ChatKeyReader",
    "PosixChatKeyReader",
    "WindowsChatKeyReader",
    "chat_input_for_console",
]
