"""Console chat line renderer.

Draws a ``[<role> running] chat> <buffer>`` prompt on the bottom console line
and keeps it there while log lines are written above it. This module only
renders; nothing creates a ``ChatLine`` outside tests yet (CHAT-10 wires it
in and adds keystroke capture).
"""

from __future__ import annotations

import shutil
import threading
from typing import Callable, Final, TextIO

from llm_handoff.logging_util import terminal_safe


ERASE_LINE: Final[str] = "\r\x1b[2K"
FALLBACK_COLUMNS: Final[int] = 80


def _default_terminal_columns() -> int:
    return shutil.get_terminal_size().columns


class ChatLine:
    """Bottom-of-console chat prompt that log lines are written above."""

    def __init__(
        self,
        stream: TextIO,
        *,
        terminal_columns: Callable[[], int] | None = None,
    ) -> None:
        self.stream = stream
        self._terminal_columns = terminal_columns or _default_terminal_columns
        self.role: str | None = None
        self.buffer: str = ""
        self.drawn: bool = False
        self._lock = threading.Lock()

    def set_role(self, role: str | None) -> None:
        with self._lock:
            self.role = role
            if role is not None:
                self._erase_if_drawn()
                self._draw()
            else:
                self._erase_if_drawn()
            self._flush()

    def set_buffer(self, text: str) -> None:
        with self._lock:
            self.buffer = text
            if self.role is not None:
                self._erase_if_drawn()
                self._draw()
            self._flush()

    def prompt_text(self) -> str:
        with self._lock:
            return self._prompt_text()

    def write_above(self, line: str) -> None:
        with self._lock:
            self._erase_if_drawn()
            self.stream.write(line + "\n")
            if self.role is not None:
                self._draw()
            self._flush()

    def clear(self) -> None:
        with self._lock:
            self._erase_if_drawn()
            self._flush()

    def redraw(self) -> None:
        with self._lock:
            if self.role is not None:
                self._erase_if_drawn()
                self._draw()
            self._flush()

    def _prompt_prefix(self) -> str:
        return f"[{terminal_safe(self.role or '')} running] chat> "

    def _prompt_text(self) -> str:
        if self.role is None:
            return ""
        return self._prompt_prefix() + terminal_safe(self.buffer)

    def _columns(self) -> int:
        try:
            columns = int(self._terminal_columns())
        except (OSError, ValueError):
            return FALLBACK_COLUMNS
        if columns < 2:
            return FALLBACK_COLUMNS
        return columns

    def _fitted_prompt(self) -> str:
        max_width = self._columns() - 1
        prefix = self._prompt_prefix()
        buffer = terminal_safe(self.buffer)
        if len(prefix) + len(buffer) <= max_width:
            return prefix + buffer
        available = max_width - len(prefix)
        if available <= 0:
            return prefix[:max_width]
        return prefix + buffer[-available:]

    def _draw(self) -> None:
        self.stream.write(self._fitted_prompt())
        self.drawn = True

    def _erase_if_drawn(self) -> None:
        if self.drawn:
            self.stream.write(ERASE_LINE)
            self.drawn = False

    def _flush(self) -> None:
        flush = getattr(self.stream, "flush", None)
        if callable(flush):
            flush()


__all__ = ["ChatLine", "ERASE_LINE"]
