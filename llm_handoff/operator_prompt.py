"""Operator prompt shown at dispatcher pauses.

When the dispatcher pauses and waits for ``HANDOFF.md`` to change, and an
operator is at a terminal, the orchestrator shows this prompt. The operator
can send the planner a best-judgment instruction, send the planner free-text
guidance, dispatch a role directly with ``@<role> <message>``, retry the
cycle, or quit. Editing ``HANDOFF.md`` still resumes.

This module must not import ``llm_handoff.orchestrator``.
"""

from __future__ import annotations

import codecs
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any, Callable, Literal, Protocol, TextIO

from llm_handoff.text_io import read_dispatch_text


OperatorActionKind = Literal["planner", "role", "retry", "quit", "invalid"]
LogFn = Callable[[str, str], None]

# Roles an operator can dispatch directly with `@<role> <message>`.
TARGETABLE_ROLES: frozenset[str] = frozenset({"backend", "frontend", "auditor"})
ROLE_TARGET_INVALID = (
    "Unknown @<role> target. Use @backend, @frontend, or @auditor with an "
    "optional message to dispatch that role, or @planner <message> to send the "
    "planner guidance."
)
# The one line shown at every operator prompt. `retry`, `quit`, and
# `@<role>` still work but are documented rather than printed, so a new
# operator sees only the action that fits most pauses.
PROMPT_LINE = (
    "Press Enter to have the planner fix this, or type instructions for it "
    "and press Enter."
)
# Added once the planner has already tried to resolve this pause streak:
# the manual escape hatch for when the automatic path did not work.
PROMPT_PLANNER_TRIED_LINE = (
    "Or edit HANDOFF.md yourself; the loop resumes when it changes."
)
_ROLE_TARGET_RE = re.compile(r"@(\S*)(.*)", re.DOTALL)


@dataclass(frozen=True, slots=True)
class OperatorAction:
    kind: OperatorActionKind
    text: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffChanged:
    """The paused HANDOFF.md content changed while the prompt was waiting."""


@dataclass(frozen=True, slots=True)
class StopRequested:
    """A stop request arrived (``llm-handoff stop``) while the prompt waited."""


def parse_operator_input(line: str) -> OperatorAction:
    stripped = line.strip()
    if not stripped:
        return OperatorAction(kind="planner", text=None)
    lowered = stripped.lower()
    if lowered == "retry":
        return OperatorAction(kind="retry")
    if lowered == "quit":
        return OperatorAction(kind="quit")
    if stripped.startswith("@"):
        return _parse_role_target(stripped)
    return OperatorAction(kind="planner", text=stripped)


def _parse_role_target(stripped: str) -> OperatorAction:
    """Parse `@<role> <message>`; the role name ends at the first whitespace."""

    match = _ROLE_TARGET_RE.fullmatch(stripped)
    name = match.group(1).lower() if match else ""
    message = match.group(2).strip() if match else ""
    if name in TARGETABLE_ROLES:
        return OperatorAction(kind="role", role=name, text=message or None)
    if name == "planner":
        return OperatorAction(kind="planner", text=message or None)
    return OperatorAction(kind="invalid", text=ROLE_TARGET_INVALID)


_EOF = object()


class ConsoleInput(Protocol):
    """Non-blocking terminal input, read only while a prompt is shown."""

    def read_chars(self, timeout: float, *, echo: bool = True) -> str:
        """Return the characters typed within ``timeout`` seconds.

        Returns ``""`` when nothing arrives in time and raises ``EOFError``
        when the terminal input is closed.
        """

    def discard_unread(self) -> None:
        """Drop input the terminal holds but ``read_chars`` cannot see yet."""


class _WindowsConsole:
    """Console input through ``msvcrt``; nothing is read between prompts."""

    def __init__(self, msvcrt_module: Any) -> None:
        self._msvcrt = msvcrt_module

    def read_chars(self, timeout: float, *, echo: bool = True) -> str:
        deadline = time.monotonic() + max(timeout, 0)
        chars: list[str] = []
        while True:
            while self._msvcrt.kbhit():
                char = self._msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    # Function and arrow keys arrive as a prefix plus a code.
                    if self._msvcrt.kbhit():
                        self._msvcrt.getwch()
                    continue
                if char == "\x03":
                    raise KeyboardInterrupt
                if echo:
                    self._echo(char)
                chars.append(char)
            remaining = deadline - time.monotonic()
            if chars or remaining <= 0:
                return "".join(chars)
            time.sleep(min(0.05, remaining))

    def _echo(self, char: str) -> None:
        if char == "\r":
            self._msvcrt.putwch("\r")
            self._msvcrt.putwch("\n")
        elif char == "\b":
            for echoed in "\b \b":
                self._msvcrt.putwch(echoed)
        else:
            self._msvcrt.putwch(char)

    def discard_unread(self) -> None:
        return None


class _PosixConsole:
    """Terminal input through ``select`` and ``os.read`` on the stdin fd.

    In canonical mode the terminal echoes and edits the line itself and
    ``select`` reports the fd readable only once a full line is typed.
    """

    def __init__(self, fd: int, encoding: str | None) -> None:
        self._fd = fd
        self._decoder = codecs.getincrementaldecoder(encoding or "utf-8")(
            errors="replace"
        )

    def read_chars(self, timeout: float, *, echo: bool = True) -> str:
        import select

        ready, _write, _error = select.select([self._fd], [], [], max(timeout, 0))
        if not ready:
            return ""
        data = os.read(self._fd, 4096)
        if not data:
            raise EOFError
        return self._decoder.decode(data)

    def discard_unread(self) -> None:
        try:
            import termios

            termios.tcflush(self._fd, termios.TCIFLUSH)
        except (ImportError, OSError, ValueError):
            return None


def _stream_is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


def _console_for_stream(stream: Any) -> ConsoleInput | None:
    """Return terminal input for ``stream`` when it is a TTY, else None."""

    if not _stream_is_tty(stream):
        return None
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            return None
        return _WindowsConsole(msvcrt)
    try:
        import select  # noqa: F401
        import termios  # noqa: F401

        fd = stream.fileno()
    except (AttributeError, ImportError, OSError, ValueError):
        return None
    return _PosixConsole(fd, getattr(stream, "encoding", None))


class OperatorInputReader:
    """Reads operator input lines for the pause prompt.

    For a terminal stream the reader polls the console only while a prompt
    is shown, and no thread or blocking read stays active while agents run.
    A manual frontend step can then read its own keypress from the same
    console, and ``drain`` discards everything typed while no prompt was
    shown, including before the first prompt.

    For any other stream, such as a scripted stream in tests, one daemon
    thread calls ``stream.readline()`` in a loop and puts each line on a
    queue. The thread starts on the first ``read_line`` call; lines queued
    between prompts are type-ahead that ``drain`` discards.

    A terminal stream with no console adapter (no ``fileno``, ``termios``, or
    ``msvcrt``) gets neither: ``console_unavailable`` is True, no thread
    starts, and the caller falls back to polling ``HANDOFF.md``.
    """

    def __init__(
        self, stream: TextIO, *, console: ConsoleInput | None = None
    ) -> None:
        self._stream = stream
        self._console = console if console is not None else _console_for_stream(stream)
        self._console_unavailable = self._console is None and _stream_is_tty(stream)
        self._queue: queue.Queue[object] = queue.Queue()
        self._eof = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._console_lines: deque[str] = deque()
        self._partial: list[str] = []
        self._after_cr = False

    @property
    def started(self) -> bool:
        return self._thread is not None

    @property
    def uses_console(self) -> bool:
        return self._console is not None

    @property
    def console_unavailable(self) -> bool:
        """True for a terminal stream that has no console input adapter."""

        return self._console_unavailable

    @property
    def eof_reached(self) -> bool:
        return self._eof.is_set()

    def start(self) -> None:
        if self._console is not None or self._console_unavailable:
            # A blocking readline() on a terminal would compete with a manual
            # frontend keypress, so a terminal never gets the thread reader.
            return
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._read_lines,
                name="llm-handoff-operator-input",
                daemon=True,
            )
            self._thread.start()

    def _read_lines(self) -> None:
        while True:
            try:
                line = self._stream.readline()
            except (OSError, ValueError):
                line = ""
            if not line:
                self._eof.set()
                self._queue.put(_EOF)
                return
            self._queue.put(line.rstrip("\r\n"))

    def _feed(self, chars: str) -> None:
        """Assemble console characters into complete lines."""

        for char in chars:
            if char == "\n" and self._after_cr:
                self._after_cr = False
                continue
            self._after_cr = char == "\r"
            if char in ("\r", "\n"):
                self._console_lines.append("".join(self._partial))
                self._partial = []
            elif char == "\b":
                if self._partial:
                    self._partial.pop()
            else:
                self._partial.append(char)

    def _console_eof(self) -> None:
        if self._partial:
            self._console_lines.append("".join(self._partial))
            self._partial = []
        self._eof.set()

    def _drain_console(self, console: ConsoleInput) -> int:
        while not self._eof.is_set():
            try:
                chars = console.read_chars(0, echo=False)
            except EOFError:
                self._console_eof()
                break
            if not chars:
                break
            self._feed(chars)
        discarded = len(self._console_lines) + (1 if self._partial else 0)
        self._console_lines.clear()
        self._partial = []
        self._after_cr = False
        try:
            console.discard_unread()
        except (OSError, ValueError):
            pass
        return discarded

    def _read_console_line(self, console: ConsoleInput, timeout: float) -> str | None:
        deadline = time.monotonic() + max(timeout, 0)
        while True:
            if self._console_lines:
                return self._console_lines.popleft()
            if self._eof.is_set():
                return None
            remaining = deadline - time.monotonic()
            try:
                chars = console.read_chars(max(remaining, 0))
            except EOFError:
                self._console_eof()
                continue
            self._feed(chars)
            if self._console_lines:
                return self._console_lines.popleft()
            if time.monotonic() >= deadline:
                return None

    def drain(self) -> int:
        """Discard queued lines and return how many were discarded."""

        if self._console is not None:
            return self._drain_console(self._console)
        discarded = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return discarded
            if item is not _EOF:
                discarded += 1

    def read_line(self, timeout: float) -> str | None:
        """Wait up to ``timeout`` seconds for the next line.

        Returns ``None`` on timeout, or at once after EOF once the queued
        lines are used up.
        """

        if self._console is not None:
            return self._read_console_line(self._console, timeout)
        self.start()
        while True:
            if self._eof.is_set() and self._queue.empty():
                return None
            try:
                item = self._queue.get(timeout=max(timeout, 0))
            except queue.Empty:
                return None
            if item is _EOF:
                continue
            return str(item)


def content_sha(content: str) -> str:
    """The same content hash as ``orchestrator._content_sha``."""

    return sha256(content.lstrip("\ufeff").encode("utf-8")).hexdigest()


def _read_handoff(path: Path) -> str | None:
    if not path.exists():
        return None
    return read_dispatch_text(path)


def wait_for_operator_or_handoff_change(
    reader: OperatorInputReader,
    handoff_path: Path,
    handoff_sha: str,
    poll_interval_seconds: float,
    log: LogFn,
    *,
    hash_content: Callable[[str], str] = content_sha,
    stop_requested: Callable[[], bool] | None = None,
    planner_tried: bool = False,
) -> HandoffChanged | StopRequested | OperatorAction:
    """Wait for an operator action, a HANDOFF.md change, or a stop request.

    ``planner_tried`` adds the edit-HANDOFF.md line under the prompt, for a
    pause the planner has already tried to resolve.

    Never returns an ``invalid`` action: those are logged and the wait goes on.
    After EOF on the operator stream it keeps polling the handoff file only.
    ``stop_requested`` is checked once per poll when given.
    """

    discarded = reader.drain()
    if discarded:
        log(
            "PAUSE",
            f"Discarded {discarded} operator input line(s) typed before the prompt.",
        )
    log("PAUSE", PROMPT_LINE)
    if planner_tried:
        log("PAUSE", PROMPT_PLANNER_TRIED_LINE)

    eof_logged = False
    while True:
        line = reader.read_line(timeout=poll_interval_seconds)
        if line is None and reader.eof_reached:
            # The operator stream is closed, so read_line no longer waits;
            # keep the poll interval between handoff checks.
            if not eof_logged:
                log(
                    "PAUSE",
                    "Operator input closed; waiting for HANDOFF.md to change.",
                )
                eof_logged = True
            time.sleep(poll_interval_seconds)

        if line is not None:
            action = parse_operator_input(line)
            if action.kind == "invalid":
                log("PAUSE", action.text or ROLE_TARGET_INVALID)
                continue
            return action

        if stop_requested is not None and stop_requested():
            return StopRequested()
        current = _read_handoff(handoff_path)
        if current is None:
            log("WARN", f"Handoff file not found at: {handoff_path}")
            continue
        if hash_content(current) != handoff_sha:
            return HandoffChanged()
