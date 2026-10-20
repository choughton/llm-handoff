"""Keystroke capture for the console chat line.

While a role runs, a background thread reads keystrokes from the console
without echo and edits a ``ChatLine`` buffer. Enter hands the line to the
``on_submit`` callback when one is given (``__main__`` saves it to the chat
queue, CHAT-2) and shows the notice it returns; without one, the line is kept
as a pending message for ``take_pending``. ``__main__._run_dispatch`` creates
one for an interactive, non-dry run, and ``chat_session`` starts and stops it
as roles run and while the dispatcher reads the console itself.

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
CHAT_SAVE_FAILED_MESSAGE: Final[str] = (
    "Chat message not saved; press Enter to try again."
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
    attributes. Escape sequences (arrow, function, and Alt keys) are removed
    even when a read splits them; the text around them is kept.
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
        # Escape-sequence parse state carried across reads: None, "esc",
        # "csi", "ss3", or "intermediate".
        self._escape_state: str | None = None

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
        self._escape_state = None

    def read_chars(self, timeout: float) -> str:
        ready, _write, _error = self._select.select(
            [self._fd], [], [], max(timeout, 0)
        )
        if not ready:
            # A quiet poll interval right after ESC means the Esc key was
            # pressed on its own, so the next key starts fresh. Once a CSI,
            # SS3, or intermediate sequence has begun, the ESC was not a lone
            # key press: keep that state so a delayed continuation is still
            # dropped instead of leaking into the chat buffer.
            if self._escape_state == "esc":
                self._escape_state = None
            return ""
        data = os.read(self._fd, 4096)
        if not data:
            raise EOFError
        return self._strip_escapes(self._decoder.decode(data))

    def _strip_escapes(self, text: str) -> str:
        """Remove escape sequences, tracking one that a read split.

        Arrow, function, and Alt keys arrive as ``ESC [ ... final`` (CSI),
        ``ESC O x`` (SS3), or ``ESC`` plus one character. Only the sequence
        is removed; text before and after it is kept. A control character
        inside a sequence ends it and is kept, so Enter after a lone Esc
        still submits.
        """

        kept: list[str] = []
        state = self._escape_state
        for char in text:
            code = ord(char)
            if state is None:
                if char == _ESCAPE:
                    state = "esc"
                else:
                    kept.append(char)
            elif char == _ESCAPE:
                state = "esc"
            elif code < 0x20 or code == 0x7F:
                state = None
                kept.append(char)
            elif state == "esc":
                if char == "[":
                    state = "csi"
                elif char == "O":
                    state = "ss3"
                elif 0x20 <= code <= 0x2F:
                    state = "intermediate"
                else:
                    state = None
            elif state == "csi":
                if 0x40 <= code <= 0x7E:
                    state = None
                elif not 0x20 <= code <= 0x3F:
                    # Not a CSI byte: the sequence was malformed; keep it.
                    state = None
                    kept.append(char)
            elif state == "intermediate":
                if not 0x20 <= code <= 0x2F:
                    state = None
            else:  # "ss3": exactly one final character.
                state = None
        self._escape_state = state
        return "".join(kept)

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
        on_submit: Callable[[str], str] | None = None,
    ) -> None:
        self._chat_line = chat_line
        self._reader = reader
        self._on_submit = on_submit
        # Held by the worker around the generation check plus the on_submit
        # call, and taken by stop() after it bumps the generation, so a
        # submission is saved before stop() returns or not at all. Never
        # taken while holding _state_lock.
        self._save_lock = threading.Lock()
        self._notify = notify if notify is not None else chat_line.write_above
        self._interrupt = interrupt if interrupt is not None else _thread.interrupt_main
        self._poll_interval = poll_interval
        self._buffer = ""
        self._after_cr = False
        self._pending: list[str] = []
        self._pending_lock = threading.Lock()
        # Held for a whole start() or stop(), including the bounded join.
        self._lifecycle_lock = threading.Lock()
        # Short-held lock shared with the worker; never held across a join,
        # a reader call, or a callback.
        self._state_lock = threading.Lock()
        # The one capture worker. It stays set until the worker itself
        # commits to exit, even when stop() times out waiting for it.
        self._thread: threading.Thread | None = None
        # True from start() until stop(), or until the input closes.
        self._active = False
        # Bumped by every start() and stop(). The worker drops the rest of
        # a batch as soon as the generation it read under changes.
        self._generation = 0
        # True while reader.open() has run without a matching close().
        self._opened = False

    @property
    def running(self) -> bool:
        with self._state_lock:
            thread = self._thread
            return self._active and thread is not None and thread.is_alive()

    @property
    def buffer(self) -> str:
        return self._buffer

    def start(self) -> None:
        with self._lifecycle_lock:
            with self._state_lock:
                if self._active:
                    return
            if self._opened:
                # The worker ended on its own (input closed); finish that
                # start before opening the reader again.
                self._opened = False
                self._reader.close()
            self._reader.open()
            self._opened = True
            with self._state_lock:
                self._generation += 1
                self._active = True
                if self._thread is not None:
                    # A worker from an earlier start outlasted stop()'s
                    # bounded join (a slow callback or renderer). It has not
                    # committed to exit, so it is adopted: its stale batch is
                    # dropped and it resumes reading once the callback
                    # returns. There is never a second worker.
                    return
                thread = threading.Thread(
                    target=self._run,
                    name="llm-handoff-chat-input",
                    daemon=True,
                )
                self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            with self._state_lock:
                if not self._opened:
                    return
                self._active = False
                self._generation += 1
                thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                # Wait out an on_submit call already in progress; any later
                # submission sees the bumped generation and saves nothing.
                with self._save_lock:
                    pass
                # Bounded: a worker still inside a slow callback after this
                # keeps ownership (see start) and exits or is adopted later.
                thread.join(timeout=max(self._poll_interval * 4, 0.2))
            self._opened = False
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
            self._handle_char(char, None)

    def _run(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                with self._state_lock:
                    if not self._active:
                        # Commit to exit under the lock, so start() either
                        # sees this worker as gone or adopts it before this
                        # check.
                        if self._thread is current:
                            self._thread = None
                        return
                    generation = self._generation
                try:
                    chars = self._reader.read_chars(self._poll_interval)
                except (EOFError, OSError):
                    with self._state_lock:
                        if self._generation == generation:
                            self._active = False
                    continue
                if chars:
                    self._feed_batch(chars, generation)
        finally:
            # Any other exit (a renderer, notify, or interrupt callback
            # raised) still releases ownership, so the next start() creates
            # a fresh worker instead of adopting this dead one. The normal
            # exit above already cleared _thread, and start() never replaces
            # a non-None _thread, so a newer worker is never disturbed.
            with self._state_lock:
                if self._thread is current:
                    self._thread = None
                    self._active = False

    def _is_current(self, generation: int | None) -> bool:
        if generation is None:
            return True
        with self._state_lock:
            return self._generation == generation

    def _feed_batch(self, chars: str, generation: int) -> None:
        # Checked before every character, so a batch read before stop() (or
        # before a restart) does nothing once its callback returns.
        for char in chars:
            if not self._is_current(generation):
                return
            self._handle_char(char, generation)

    def _handle_char(self, char: str, generation: int | None) -> None:
        if char == "\n" and self._after_cr:
            self._after_cr = False
            return
        self._after_cr = char == "\r"
        if char in ("\r", "\n"):
            self._submit(generation)
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

    def _submit(self, generation: int | None) -> None:
        """Queue the buffer, clear it, then notify.

        ``generation`` is the capture generation the character was read
        under (None for ``feed``). The queue append is checked against it
        under the state lock, so a submission either lands before stop()
        bumps the generation or not at all; a stale submission leaves the
        buffer intact. The notification is checked again after the
        (possibly slow) renderer returns, so a stopped or restarted
        generation never begins a late notification.
        """

        if self._on_submit is not None:
            self._submit_to_callback(generation, self._on_submit)
            return
        text = self._buffer.strip()
        if text:
            with self._state_lock:
                if generation is not None and self._generation != generation:
                    return
                with self._pending_lock:
                    self._pending.append(text)
        self._set_buffer("")
        if not text:
            return
        if not self._is_current(generation):
            return
        self._notify(CHAT_UNAVAILABLE_MESSAGE)

    def _submit_to_callback(
        self, generation: int | None, on_submit: Callable[[str], str]
    ) -> None:
        """Hand the buffer to ``on_submit``, clear it, then notify its notice.

        The generation check and the call run under the save lock, which
        stop() takes after it bumps the generation: a submission is either
        saved before stop() returns or not at all, and a stale one leaves the
        buffer intact. An ``OSError`` from ``on_submit`` keeps the buffer, so
        Enter retries.
        """

        text = self._buffer.strip()
        if not text:
            self._set_buffer("")
            return
        with self._save_lock:
            if not self._is_current(generation):
                return
            try:
                notice = on_submit(text)
                saved = True
            except OSError:
                notice = CHAT_SAVE_FAILED_MESSAGE
                saved = False
        if saved:
            self._set_buffer("")
        if not self._is_current(generation):
            return
        self._notify(notice)


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
    "CHAT_SAVE_FAILED_MESSAGE",
    "CHAT_UNAVAILABLE_MESSAGE",
    "ChatInput",
    "ChatKeyReader",
    "PosixChatKeyReader",
    "WindowsChatKeyReader",
    "chat_input_for_console",
]
