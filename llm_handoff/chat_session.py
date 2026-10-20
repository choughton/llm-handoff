"""The process-wide console chat session for the dispatch loop.

``__main__._run_dispatch`` attaches a ``ChatLine`` and its ``ChatInput`` for
an interactive, non-dry run. The orchestrator then mirrors every
``console_interrupt.set_active_role`` call with ``set_role``: while a role's
cycle runs, the chat line is drawn and keystrokes are captured; with no
active role, capture is stopped and the line erased.

Code that reads the console itself (the carry-over confirm and the manual
frontend keypress wait) runs inside ``suspended()``, so capture never reads
keys at the same time.

Every function is a no-op when nothing is attached. A ``ChatInput.start()``
that raises ``OSError`` leaves capture off and raises nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from llm_handoff.chat_input import ChatInput
from llm_handoff.chat_line import ChatLine


class ChatSession:
    """The attached chat line and keystroke capture for one process."""

    # Every call comes from the main thread, as with
    # ``console_interrupt.ConsoleInterruptController``.

    def __init__(self) -> None:
        self._chat_line: ChatLine | None = None
        self._chat_input: ChatInput | None = None
        self._role: str | None = None

    @property
    def attached(self) -> bool:
        return self._chat_line is not None and self._chat_input is not None

    @property
    def role(self) -> str | None:
        return self._role

    def attach(self, chat_line: ChatLine, chat_input: ChatInput) -> None:
        if self.attached:
            self.detach()
        self._chat_line = chat_line
        self._chat_input = chat_input
        self._role = None

    def detach(self) -> None:
        chat_line, chat_input = self._chat_line, self._chat_input
        self._chat_line = None
        self._chat_input = None
        self._role = None
        if chat_input is not None:
            chat_input.stop()
        if chat_line is not None:
            chat_line.set_role(None)

    def set_role(self, role: str | None) -> None:
        chat_line, chat_input = self._chat_line, self._chat_input
        if chat_line is None or chat_input is None:
            return
        self._role = role
        if role is None:
            chat_input.stop()
            chat_line.set_role(None)
            return
        chat_line.set_role(role)
        try:
            chat_input.start()
        except OSError:
            # The console refused key input; the line stays, capture is off.
            return

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Stop capture and erase the line for the block; restore on exit."""

        if not self.attached:
            yield
            return
        role = self._role
        self.set_role(None)
        try:
            yield
        finally:
            if role is not None:
                self.set_role(role)


SESSION = ChatSession()


def attach(chat_line: ChatLine, chat_input: ChatInput) -> None:
    SESSION.attach(chat_line, chat_input)


def detach() -> None:
    SESSION.detach()


def set_role(role: str | None) -> None:
    SESSION.set_role(role)


def suspended() -> AbstractContextManager[None]:
    """Stop chat capture while the dispatcher reads the console itself."""

    return SESSION.suspended()


__all__ = [
    "ChatSession",
    "SESSION",
    "attach",
    "detach",
    "set_role",
    "suspended",
]
