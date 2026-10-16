"""Operator prompt shown at dispatcher pauses.

When the dispatcher pauses and waits for ``HANDOFF.md`` to change, and an
operator is at a terminal, the orchestrator shows this prompt. The operator
can send the planner a best-judgment instruction, send the planner free-text
guidance, retry the cycle, or quit. Editing ``HANDOFF.md`` still resumes.

This module must not import ``llm_handoff.orchestrator``.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import queue
import threading
import time
from typing import Callable, Literal, TextIO

from llm_handoff.text_io import read_dispatch_text


OperatorActionKind = Literal["planner", "retry", "quit", "invalid"]
LogFn = Callable[[str, str], None]

ROLE_TARGETING_UNAVAILABLE = (
    "Role targeting with @<role> is not available yet. Press Enter, type "
    "guidance for the planner, `retry`, or `quit`."
)
PROMPT_HELP_LINES: tuple[str, ...] = (
    "Operator prompt: press Enter to send the planner a best judgment "
    "instruction for this pause.",
    "Operator prompt: type text and press Enter to send it to the planner as "
    "guidance.",
    "Operator prompt: type `retry` to re-run the cycle, or `quit` to stop the "
    "dispatcher.",
    "Operator prompt: editing HANDOFF.md also resumes the dispatch loop.",
)


@dataclass(frozen=True, slots=True)
class OperatorAction:
    kind: OperatorActionKind
    text: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffChanged:
    """The paused HANDOFF.md content changed while the prompt was waiting."""


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
        return OperatorAction(kind="invalid", text=ROLE_TARGETING_UNAVAILABLE)
    return OperatorAction(kind="planner", text=stripped)


_EOF = object()


class OperatorInputReader:
    """One daemon thread that reads lines from ``stream`` into a queue.

    The thread starts on the first ``read_line`` call. Lines typed before it
    starts stay in the stream and are read as ordinary input; lines queued
    between prompts are type-ahead that ``drain`` discards.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._queue: queue.Queue[object] = queue.Queue()
        self._eof = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def started(self) -> bool:
        return self._thread is not None

    @property
    def eof_reached(self) -> bool:
        return self._eof.is_set()

    def start(self) -> None:
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

    def drain(self) -> int:
        """Discard queued lines and return how many were discarded."""

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
) -> HandoffChanged | OperatorAction:
    """Wait for an operator action or a HANDOFF.md change.

    Never returns an ``invalid`` action: those are logged and the wait goes on.
    After EOF on the operator stream it keeps polling the handoff file only.
    """

    discarded = reader.drain()
    if discarded:
        log(
            "PAUSE",
            f"Discarded {discarded} operator input line(s) typed before the prompt.",
        )
    for line in PROMPT_HELP_LINES:
        log("PAUSE", line)

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
                log("PAUSE", action.text or ROLE_TARGETING_UNAVAILABLE)
                continue
            return action

        current = _read_handoff(handoff_path)
        if current is None:
            log("WARN", f"Handoff file not found at: {handoff_path}")
            continue
        if hash_content(current) != handoff_sha:
            return HandoffChanged()
