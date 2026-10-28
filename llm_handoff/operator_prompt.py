"""Operator prompt shown at dispatcher pauses.

When the dispatcher pauses and waits for ``HANDOFF.md`` to change, and an
operator is at a terminal, the orchestrator shows this prompt. The operator
can send the planner a best-judgment instruction, send the planner free-text
guidance, dispatch a role directly with ``@<role> <message>``, retry the
cycle, or quit. Editing ``HANDOFF.md`` still resumes. At a proposal review
(a user-route pause whose handoff holds a nonempty ``## Proposed Backlog``),
Enter approves the proposal instead and typed text revises it.

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
import unicodedata

from llm_handoff.text_io import read_dispatch_text


# `approve` comes only from an empty line at a proposal review prompt
# (``wait_for_operator_or_handoff_change`` with ``proposal_review``); the
# parser itself never returns it, so typed text can never approve.
OperatorActionKind = Literal["planner", "role", "retry", "quit", "invalid", "approve"]
LogFn = Callable[[str, str], None]

# Roles an operator can dispatch directly with `@<role> <message>`.
TARGETABLE_ROLES: frozenset[str] = frozenset({"backend", "frontend", "auditor"})
ROLE_TARGET_INVALID = (
    "Unknown @<role> target. Use @backend, @frontend, or @auditor with an "
    "optional message to dispatch that role, or @planner <message> to send the "
    "planner guidance."
)
# `@tpm` is a target only when `agents.tpm` is configured; the parser is told
# so with `tpm_configured`, and these messages cover the two cases.
TPM_TARGET_NOT_CONFIGURED = (
    "@tpm is not available: agents.tpm is not configured in the dispatch "
    "config, so the planner picks stories. Use @planner <message> to send the "
    "planner guidance."
)
ROLE_TARGET_INVALID_WITH_TPM = (
    "Unknown @<role> target. Use @backend, @frontend, @auditor, or @tpm with "
    "an optional message to dispatch that role, or @planner <message> to send "
    "the planner guidance."
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
# Shown instead of PROMPT_LINE at a user-route pause whose handoff holds a
# nonempty `## Proposed Backlog` (Work Intake proposal review).
PROPOSAL_PROMPT_LINE = (
    "Press Enter to approve this proposed backlog and start the plan, or type "
    "changes for the planner to revise it and press Enter."
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


def parse_operator_input(
    line: str, *, tpm_configured: bool = False
) -> OperatorAction:
    """Parse one operator line.

    ``tpm_configured`` makes ``@tpm`` a role target; without it ``@tpm`` is
    refused with ``TPM_TARGET_NOT_CONFIGURED``.
    """

    stripped = line.strip()
    if not stripped:
        return OperatorAction(kind="planner", text=None)
    lowered = stripped.lower()
    if lowered == "retry":
        return OperatorAction(kind="retry")
    if lowered == "quit":
        return OperatorAction(kind="quit")
    if stripped.startswith("@"):
        return _parse_role_target(stripped, tpm_configured=tpm_configured)
    return OperatorAction(kind="planner", text=stripped)


def _parse_role_target(stripped: str, *, tpm_configured: bool) -> OperatorAction:
    """Parse `@<role> <message>`; the role name ends at the first whitespace."""

    match = _ROLE_TARGET_RE.fullmatch(stripped)
    name = match.group(1).lower() if match else ""
    message = match.group(2).strip() if match else ""
    if name in TARGETABLE_ROLES:
        return OperatorAction(kind="role", role=name, text=message or None)
    if name == "planner":
        return OperatorAction(kind="planner", text=message or None)
    if name == "tpm":
        if tpm_configured:
            return OperatorAction(kind="role", role="tpm", text=message or None)
        return OperatorAction(kind="invalid", text=TPM_TARGET_NOT_CONFIGURED)
    return OperatorAction(
        kind="invalid",
        text=ROLE_TARGET_INVALID_WITH_TPM if tpm_configured else ROLE_TARGET_INVALID,
    )


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


# --- user-pause preview ---------------------------------------------------
#
# At a user-route pause the operator reads the handoff's `## Proposed
# Backlog`, or the escalation body when there is none, in the terminal. The
# preview is bounded by both limits so one huge handoff or one huge line
# cannot flood the terminal; the full text stays in the handoff file.
PREVIEW_MAX_LINES = 40
PREVIEW_MAX_CHARS = 4000
PROPOSED_BACKLOG_TITLE = "proposed backlog"
PREVIEW_INDENT = "  "
_PREVIEW_TAB = "    "
_ATX_HEADING_RE = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*")
_HEADING_CLOSING_HASHES_RE = re.compile(r"(?:^|[ \t]+)#+$")
_FENCE_OPEN_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
# Bidirectional overrides and isolates can reorder what a terminal shows.
_BIDI_CONTROLS = frozenset(
    "؜‎‏‪‫‬‭‮"
    "⁦⁧⁨⁩"
)


def _split_frontmatter(content: str) -> str:
    """Return the handoff body without its YAML frontmatter.

    An unterminated frontmatter block yields an empty body, so routing
    fields never reach the preview.
    """

    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return content
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return "\n".join(lines[index + 1 :])
    return ""


def _heading(line: str) -> tuple[int, str] | None:
    match = _ATX_HEADING_RE.fullmatch(line)
    if match is None:
        return None
    title = _HEADING_CLOSING_HASHES_RE.sub("", match.group(2) or "")
    return len(match.group(1)), title.strip()


def _proposed_backlog(lines: list[str]) -> list[str] | None:
    """The first nonempty `## Proposed Backlog` section, heading included.

    The section runs through the line before the next level-one or
    level-two heading. Heading-like lines inside fenced blocks are text.
    """

    fence: tuple[str, int] | None = None
    section: list[str] | None = None
    for line in lines:
        if fence is not None:
            if section is not None:
                section.append(line)
            stripped = line.strip()
            char, length = fence
            if (
                stripped
                and set(stripped) == {char}
                and len(stripped) >= length
                and len(line) - len(line.lstrip(" ")) <= 3
            ):
                fence = None
            continue
        fence_match = _FENCE_OPEN_RE.fullmatch(line)
        if fence_match is not None:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            if section is not None:
                section.append(line)
            continue
        heading = _heading(line)
        if heading is not None and heading[0] <= 2:
            if section is not None and any(item.strip() for item in section[1:]):
                return section
            section = (
                [line]
                if heading[0] == 2 and heading[1].lower() == PROPOSED_BACKLOG_TITLE
                else None
            )
            continue
        if section is not None:
            section.append(line)
    if section is not None and any(item.strip() for item in section[1:]):
        return section
    return None


def pending_proposal(content: str) -> str | None:
    """The handoff's first nonempty `## Proposed Backlog` section, or None.

    Uses the same section rules as the user-pause preview: the YAML
    frontmatter is skipped, a heading-only section does not count, and
    heading-like lines inside fenced blocks, block quotes, or indented code
    are text. The section is returned whole, heading included and without
    the preview bounds, so an approval can quote exactly what was proposed.
    """

    body = _split_frontmatter(content.lstrip("﻿").replace("\r\n", "\n"))
    section = _proposed_backlog(body.split("\n"))
    if section is None:
        return None
    return "\n".join(_trim_blank_edges(section))


def _escape_char(char: str) -> str:
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}"


def escape_terminal_controls(text: str) -> str:
    """Escape characters that could drive or garble a terminal.

    Line breaks stay readable: CRLF and CR become LF. Tabs become spaces.
    Every other control character, the Unicode line and paragraph
    separators, and bidirectional controls are shown as escapes.
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    escaped: list[str] = []
    for char in text:
        if char == "\n":
            escaped.append(char)
        elif char == "\t":
            escaped.append(_PREVIEW_TAB)
        elif (
            unicodedata.category(char) in ("Cc", "Zl", "Zp")
            or char in _BIDI_CONTROLS
        ):
            escaped.append(_escape_char(char))
        else:
            escaped.append(char)
    return "".join(escaped)


def _trim_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _bound_lines(lines: list[str]) -> tuple[list[str], bool]:
    """Keep at most PREVIEW_MAX_LINES lines and PREVIEW_MAX_CHARS characters.

    Line breaks count as one character each. Returns the kept lines and
    whether anything was cut.
    """

    kept: list[str] = []
    used = 0
    for line in lines:
        if len(kept) >= PREVIEW_MAX_LINES:
            return kept, True
        separator = 1 if kept else 0
        remaining = PREVIEW_MAX_CHARS - used - separator
        if remaining <= 0:
            return kept, True
        if len(line) > remaining:
            kept.append(line[:remaining])
            return kept, True
        kept.append(line)
        used += separator + len(line)
    return kept, False


def user_pause_preview(content: str, handoff_path: str) -> list[str]:
    """Bounded preview lines for a user-route pause; empty when none fits.

    Prefers the first nonempty `## Proposed Backlog` section; otherwise the
    escalation body after the YAML frontmatter. An empty body returns no
    lines, so the caller shows only its pause reason. Control characters
    are escaped before the bounds apply. The full handoff is never returned
    as a fallback: a truncated preview ends with a notice naming
    ``handoff_path`` instead.
    """

    body = _split_frontmatter(content.lstrip("﻿").replace("\r\n", "\n"))
    body_lines = body.split("\n")
    section = _proposed_backlog(body_lines)
    if section is not None:
        header = f"Proposed backlog from {handoff_path}:"
        selected = section
    else:
        header = f"Escalation from {handoff_path}:"
        selected = body_lines
    escaped = escape_terminal_controls("\n".join(selected)).split("\n")
    trimmed = _trim_blank_edges(escaped)
    if not trimmed:
        return []
    kept, truncated = _bound_lines(trimmed)
    preview = [escape_terminal_controls(header).replace("\n", " ")]
    preview.extend(f"{PREVIEW_INDENT}{line}" if line else "" for line in kept)
    if truncated:
        preview.append(
            escape_terminal_controls(
                f"Preview truncated at {PREVIEW_MAX_LINES} lines or "
                f"{PREVIEW_MAX_CHARS} characters; read the full text in "
                f"{handoff_path}."
            ).replace("\n", " ")
        )
    return preview


REVIEW_CHOICES: tuple[str, ...] = ("merge", "retry", "quit")
REVIEW_PROMPT_HELP_LINES: tuple[str, ...] = (
    "Operator prompt: type `merge` to merge the pull request without a Codex "
    "review.",
    "Operator prompt: type `retry` to ask Codex to review the head commit "
    "again and wait for it.",
    "Operator prompt: type `quit` to leave the pull request open and stop the "
    "dispatcher.",
)


def ask_operator_choice(
    reader: OperatorInputReader,
    choices: tuple[str, ...],
    help_lines: tuple[str, ...],
    poll_interval_seconds: float,
    log: LogFn,
    *,
    stop_requested: Callable[[], bool] | None = None,
) -> str | None:
    """Wait for the operator to type one of ``choices``.

    Any other line, including an empty one, is answered with the choices and
    the wait goes on. Returns ``None`` when the operator stream closes or a
    stop request arrives.
    """

    discarded = reader.drain()
    if discarded:
        log(
            "PAUSE",
            f"Discarded {discarded} operator input line(s) typed before the prompt.",
        )
    for line in help_lines:
        log("PAUSE", line)
    while True:
        line = reader.read_line(timeout=poll_interval_seconds)
        if line is not None:
            choice = line.strip().lower()
            if choice in choices:
                return choice
            log("PAUSE", f"Type one of: {', '.join(choices)}.")
            continue
        if reader.eof_reached:
            return None
        if stop_requested is not None and stop_requested():
            return None


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
    tpm_configured: bool = False,
    proposal_review: bool = False,
) -> HandoffChanged | StopRequested | OperatorAction:
    """Wait for an operator action, a HANDOFF.md change, or a stop request.

    ``planner_tried`` adds the edit-HANDOFF.md line under the prompt, for a
    pause the planner has already tried to resolve. ``tpm_configured`` is
    passed to ``parse_operator_input`` and makes ``@tpm`` a role target.

    ``proposal_review`` is set only when the paused handoff holds a pending
    `## Proposed Backlog`: the prompt line becomes ``PROPOSAL_PROMPT_LINE``
    and an empty line returns an ``approve`` action instead of the
    best-judgment planner action. Every other line parses as usual, so typed
    text stays revision guidance whatever it says.

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
    log("PAUSE", PROPOSAL_PROMPT_LINE if proposal_review else PROMPT_LINE)
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
            if proposal_review and not line.strip():
                return OperatorAction(kind="approve")
            action = parse_operator_input(line, tpm_configured=tpm_configured)
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
