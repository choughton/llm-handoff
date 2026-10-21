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

The session also carries the running role's ``ChatContext`` and an optional
attached chat answer worker. ``wait_for_answers`` blocks the loop while that
worker is answering, so commits, gates, and publishes see the answer's
changes. Nothing attaches a worker yet; CHAT-32 wires it in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Protocol

from llm_handoff.chat_input import ChatInput
from llm_handoff.chat_line import ChatLine

if TYPE_CHECKING:
    # Type-only: chat_answers imports chat_side_session, which imports
    # agent_process, which imports this module.
    from llm_handoff.chat_answers import ChatContext

CHAT_ANSWER_WAIT_MESSAGE = "Waiting for the chat answer to finish."


class ChatAnswerWaiter(Protocol):
    """The part of a chat answer worker the loop waits on."""

    def busy(self) -> bool: ...

    def wait_idle(self, timeout: float | None) -> bool: ...


class ChatSession:
    """The attached chat line and keystroke capture for one process."""

    # Every call comes from the main thread, as with
    # ``console_interrupt.ConsoleInterruptController``.

    def __init__(self) -> None:
        self._chat_line: ChatLine | None = None
        self._chat_input: ChatInput | None = None
        self._role: str | None = None
        self._context: ChatContext | None = None
        self._worker: ChatAnswerWaiter | None = None

    @property
    def attached(self) -> bool:
        return self._chat_line is not None and self._chat_input is not None

    @property
    def role(self) -> str | None:
        return self._role

    @property
    def context(self) -> ChatContext | None:
        return self._context

    @property
    def worker(self) -> ChatAnswerWaiter | None:
        return self._worker

    def attach(self, chat_line: ChatLine, chat_input: ChatInput) -> None:
        if self.attached:
            self.detach()
        self._chat_line = chat_line
        self._chat_input = chat_input
        self._role = None
        self._context = None

    def attach_worker(self, worker: ChatAnswerWaiter | None) -> None:
        self._worker = worker

    def detach(self) -> None:
        chat_line, chat_input = self._chat_line, self._chat_input
        self._chat_line = None
        self._chat_input = None
        # Stop capture before clearing the role, as in ``set_role(None)``: a
        # save racing the detach still reads the role that was running.
        if chat_input is not None:
            chat_input.stop()
        self._role = None
        self._context = None
        # The worker's owner stops it; detach only drops the reference.
        self._worker = None
        if chat_line is not None:
            chat_line.set_role(None)

    def set_role(
        self, role: str | None, context: ChatContext | None = None
    ) -> None:
        chat_line, chat_input = self._chat_line, self._chat_input
        if chat_line is None or chat_input is None:
            return
        if role is None:
            # Stop capture first: a save racing this clear still reads the
            # role that was running.
            chat_input.stop()
            self._role = None
            self._context = None
            chat_line.set_role(None)
            return
        self._role = role
        self._context = context
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
        context = self._context
        self.set_role(None)
        try:
            yield
        finally:
            if role is not None:
                self.set_role(role, context)

    def wait_for_answers(
        self,
        log: Callable[[str, str], None] | None = None,
        *,
        step: float = 0.5,
    ) -> bool:
        """Block while the attached worker answers; True when it waited."""

        worker = self._worker
        if worker is None or not worker.busy():
            return False
        if log is not None:
            log("INFO", CHAT_ANSWER_WAIT_MESSAGE)
        # A short timeout per wait lets Ctrl+C through on Windows.
        while not worker.wait_idle(timeout=step):
            pass
        return True


SESSION = ChatSession()


def attach(chat_line: ChatLine, chat_input: ChatInput) -> None:
    SESSION.attach(chat_line, chat_input)


def detach() -> None:
    SESSION.detach()


def set_role(role: str | None, context: ChatContext | None = None) -> None:
    SESSION.set_role(role, context)


def attach_worker(worker: ChatAnswerWaiter | None) -> None:
    """Attach the chat answer worker the loop waits on, or None to drop it."""

    SESSION.attach_worker(worker)


def wait_for_answers(
    log: Callable[[str, str], None] | None = None, *, step: float = 0.5
) -> bool:
    """Wait for a running chat answer; False at once when none is running."""

    return SESSION.wait_for_answers(log, step=step)


def suspended() -> AbstractContextManager[None]:
    """Stop chat capture while the dispatcher reads the console itself."""

    return SESSION.suspended()


__all__ = [
    "CHAT_ANSWER_WAIT_MESSAGE",
    "ChatAnswerWaiter",
    "ChatSession",
    "SESSION",
    "attach",
    "attach_worker",
    "detach",
    "set_role",
    "suspended",
    "wait_for_answers",
]
