"""Console Ctrl+C handling for the dispatch loop.

Agent processes start in their own process group (see
``agent_process._process_group_popen_kwargs``), so a console Ctrl+C reaches
only the dispatcher. This module decides what that Ctrl+C means:

- While a role's dispatch cycle runs, the first Ctrl+C asks for the same
  graceful stop as ``llm-handoff stop``: it sets an in-process flag that the
  dispatch loop reads at its stop checks, and the current cycle finishes.
- A second Ctrl+C while a role is active raises ``KeyboardInterrupt``, which
  kills the running agent's process tree and ends the run at once.
- With no active role (startup, a pause wait, the operator prompt, between
  cycles), Ctrl+C raises ``KeyboardInterrupt`` at once.

The handler never writes files; the flag lives only in this process.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
import signal
import sys
import threading
from types import FrameType
from typing import TextIO

STOP_MESSAGE_TEMPLATE = (
    "Stop requested; finishing the current {role} run. "
    "Press Ctrl+C again to stop now."
)


class ConsoleInterruptController:
    """Tracks the active role and the Ctrl+C stop flag for one process."""

    # No lock: the SIGINT handler runs on the main thread between bytecodes,
    # so a lock the main thread already held would deadlock it. Each update
    # is a single attribute assignment.

    def __init__(self) -> None:
        self._active_role: str | None = None
        self._stop_requested = False
        # Set by the second Ctrl+C. Agent runs on worker threads (the audit
        # fan-out) poll it, because KeyboardInterrupt reaches only the main
        # thread and their children no longer receive the console Ctrl+C.
        self._interrupt_now = threading.Event()
        # Inside ``deferred_interrupts`` (a child process being created), a
        # Ctrl+C that would raise KeyboardInterrupt is recorded here instead
        # and raised when the block exits.
        self._defer_depth = 0
        self._interrupt_pending = False

    @property
    def active_role(self) -> str | None:
        return self._active_role

    def set_active_role(self, role: str | None) -> None:
        """Record the role whose dispatch cycle is running, or ``None``."""

        self._active_role = role

    def stop_requested(self) -> bool:
        """True once a first Ctrl+C asked for a graceful stop."""

        return self._stop_requested

    def interrupt_now_requested(self) -> bool:
        """True once a second Ctrl+C asked to stop at once."""

        return self._interrupt_now.is_set()

    def reset(self) -> None:
        self._active_role = None
        self._stop_requested = False
        self._interrupt_now.clear()
        self._defer_depth = 0
        self._interrupt_pending = False

    @contextmanager
    def deferred_interrupts(self) -> Iterator[None]:
        """Hold back a Ctrl+C KeyboardInterrupt until the block exits.

        Wrap child process creation in this block. A KeyboardInterrupt raised
        while ``subprocess.Popen`` is still running would leave the caller
        without a process handle, and a child in its own process group would
        then outlive the dispatcher. Here the handler records the interrupt
        instead, and the block raises it on exit, once the caller can kill
        the child. A graceful-stop request is not deferred: it only sets a
        flag.

        Off the main thread this is a no-op: KeyboardInterrupt reaches only
        the main thread, and worker-thread runs poll
        ``interrupt_now_requested`` instead.
        """

        if threading.current_thread() is not threading.main_thread():
            yield
            return
        self._defer_depth += 1
        try:
            yield
        finally:
            self._defer_depth -= 1
            if self._defer_depth == 0 and self._interrupt_pending:
                self._interrupt_pending = False
                raise KeyboardInterrupt

    def _interrupt(self) -> None:
        if self._defer_depth > 0:
            self._interrupt_pending = True
            return
        raise KeyboardInterrupt

    def handle_sigint(
        self,
        signum: int,
        frame: FrameType | None,
        *,
        stream: TextIO | None = None,
    ) -> None:
        """SIGINT handler: request a graceful stop, or interrupt at once."""

        role = self._active_role
        if role is None:
            self._interrupt()
            return
        if self._stop_requested:
            self._interrupt_now.set()
            self._interrupt()
            return
        # Set the flag before writing, so the request holds even if the
        # write below fails.
        self._stop_requested = True
        out = stream if stream is not None else sys.stderr
        try:
            print(STOP_MESSAGE_TEMPLATE.format(role=role), file=out, flush=True)
        except (OSError, ValueError, RuntimeError):
            # A closed or broken stderr must not turn the request into a
            # crash. RuntimeError covers a reentrant write: the signal can
            # arrive while the main thread is inside a write to the same
            # buffered stream, and buffered streams are not reentrant.
            pass


CONTROLLER = ConsoleInterruptController()


def set_active_role(role: str | None) -> None:
    CONTROLLER.set_active_role(role)


def stop_requested() -> bool:
    return CONTROLLER.stop_requested()


def interrupt_now_requested() -> bool:
    return CONTROLLER.interrupt_now_requested()


def deferred_interrupts() -> AbstractContextManager[None]:
    """Defer a Ctrl+C KeyboardInterrupt on the process-wide controller."""

    return CONTROLLER.deferred_interrupts()


@contextmanager
def installed(
    controller: ConsoleInterruptController | None = None,
) -> Iterator[ConsoleInterruptController]:
    """Install the controller's SIGINT handler for the duration of the block.

    Python only lets the main thread set signal handlers, so off the main
    thread this is a no-op and Ctrl+C keeps its previous behavior. On exit
    the previous handler is restored and the controller's state cleared.
    """

    active = controller if controller is not None else CONTROLLER
    if threading.current_thread() is not threading.main_thread():
        yield active
        return
    previous = signal.getsignal(signal.SIGINT)
    active.reset()
    signal.signal(signal.SIGINT, active.handle_sigint)
    try:
        yield active
    finally:
        signal.signal(
            signal.SIGINT,
            previous if previous is not None else signal.SIG_DFL,
        )
        active.reset()
