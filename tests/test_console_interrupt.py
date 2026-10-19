"""Console Ctrl+C handling (RECOVERY-1).

The handler is called directly; no test sends a real signal.
"""

from __future__ import annotations

import io
import signal
import threading

import pytest

from llm_handoff import console_interrupt
from llm_handoff.console_interrupt import ConsoleInterruptController


def test_first_ctrl_c_with_an_active_role_sets_the_flag_and_prints() -> None:
    controller = ConsoleInterruptController()
    controller.set_active_role("backend")
    stream = io.StringIO()

    controller.handle_sigint(signal.SIGINT, None, stream=stream)

    assert controller.stop_requested() is True
    assert controller.interrupt_now_requested() is False
    assert stream.getvalue() == (
        "Stop requested; finishing the current backend run. "
        "Press Ctrl+C again to stop now.\n"
    )


def test_second_ctrl_c_with_an_active_role_raises() -> None:
    controller = ConsoleInterruptController()
    controller.set_active_role("auditor")
    stream = io.StringIO()
    controller.handle_sigint(signal.SIGINT, None, stream=stream)

    with pytest.raises(KeyboardInterrupt):
        controller.handle_sigint(signal.SIGINT, None, stream=stream)

    # Agent runs on worker threads poll this to stop as well.
    assert controller.interrupt_now_requested() is True
    # The message is printed once, for the first press only.
    assert stream.getvalue().count("Stop requested;") == 1


def test_ctrl_c_with_no_active_role_raises_at_once() -> None:
    controller = ConsoleInterruptController()
    stream = io.StringIO()

    with pytest.raises(KeyboardInterrupt):
        controller.handle_sigint(signal.SIGINT, None, stream=stream)

    assert controller.stop_requested() is False
    assert stream.getvalue() == ""


def test_ctrl_c_after_the_role_is_cleared_raises_at_once() -> None:
    controller = ConsoleInterruptController()
    controller.set_active_role("planner")
    controller.set_active_role(None)

    with pytest.raises(KeyboardInterrupt):
        controller.handle_sigint(signal.SIGINT, None, stream=io.StringIO())

    assert controller.stop_requested() is False


class _ReentrantStream(io.StringIO):
    """A stream whose write fails like a reentrant buffered-stream write."""

    def write(self, text: str) -> int:
        raise RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>")


def test_first_ctrl_c_survives_a_reentrant_stderr_write() -> None:
    controller = ConsoleInterruptController()
    controller.set_active_role("backend")

    controller.handle_sigint(signal.SIGINT, None, stream=_ReentrantStream())

    assert controller.stop_requested() is True
    assert controller.interrupt_now_requested() is False


def test_deferred_interrupts_hold_a_second_ctrl_c_until_the_block_exits() -> None:
    controller = ConsoleInterruptController()
    controller.set_active_role("backend")
    controller.handle_sigint(signal.SIGINT, None, stream=io.StringIO())
    reached_end_of_block = False

    with pytest.raises(KeyboardInterrupt):
        with controller.deferred_interrupts():
            controller.handle_sigint(signal.SIGINT, None, stream=io.StringIO())
            reached_end_of_block = True

    assert reached_end_of_block is True
    assert controller.interrupt_now_requested() is True
    # The pending interrupt is consumed; a later block exits cleanly.
    with controller.deferred_interrupts():
        pass


def test_deferred_interrupts_hold_ctrl_c_with_no_active_role() -> None:
    controller = ConsoleInterruptController()

    with pytest.raises(KeyboardInterrupt):
        with controller.deferred_interrupts():
            with controller.deferred_interrupts():
                controller.handle_sigint(signal.SIGINT, None, stream=io.StringIO())
            # Nested blocks raise only when the outermost one exits.


def test_deferred_interrupts_do_not_defer_a_graceful_stop_request() -> None:
    controller = ConsoleInterruptController()
    controller.set_active_role("backend")

    with controller.deferred_interrupts():
        controller.handle_sigint(signal.SIGINT, None, stream=io.StringIO())
        assert controller.stop_requested() is True

    assert controller.interrupt_now_requested() is False


def test_deferred_interrupts_are_a_no_op_off_the_main_thread() -> None:
    controller = ConsoleInterruptController()
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            with controller.deferred_interrupts():
                outcome["depth"] = controller._defer_depth
        except BaseException as exc:  # noqa: BLE001 - report to the main thread
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()

    assert "error" not in outcome
    assert outcome["depth"] == 0


def test_installed_sets_and_restores_the_previous_handler() -> None:
    controller = ConsoleInterruptController()
    previous = signal.getsignal(signal.SIGINT)
    controller.set_active_role("backend")
    controller.handle_sigint(signal.SIGINT, None, stream=io.StringIO())

    with console_interrupt.installed(controller) as active:
        assert active is controller
        assert signal.getsignal(signal.SIGINT) == controller.handle_sigint
        # Installing starts from a clean state.
        assert controller.stop_requested() is False
        assert controller.active_role is None
        controller.set_active_role("backend")

    assert signal.getsignal(signal.SIGINT) == previous
    assert controller.active_role is None
    assert controller.stop_requested() is False


def test_installed_restores_the_handler_when_the_block_raises() -> None:
    controller = ConsoleInterruptController()
    previous = signal.getsignal(signal.SIGINT)

    with pytest.raises(KeyboardInterrupt):
        with console_interrupt.installed(controller):
            raise KeyboardInterrupt

    assert signal.getsignal(signal.SIGINT) == previous


def test_installed_is_a_no_op_off_the_main_thread() -> None:
    controller = ConsoleInterruptController()
    previous = signal.getsignal(signal.SIGINT)
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            with console_interrupt.installed(controller) as active:
                outcome["active"] = active
                outcome["handler"] = signal.getsignal(signal.SIGINT)
        except BaseException as exc:  # noqa: BLE001 - report to the main thread
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()

    assert "error" not in outcome
    assert outcome["active"] is controller
    assert outcome["handler"] == previous
    assert signal.getsignal(signal.SIGINT) == previous


def test_module_helpers_use_the_shared_controller() -> None:
    console_interrupt.CONTROLLER.reset()
    try:
        console_interrupt.set_active_role("frontend")
        assert console_interrupt.CONTROLLER.active_role == "frontend"
        assert console_interrupt.stop_requested() is False
        console_interrupt.CONTROLLER.handle_sigint(
            signal.SIGINT, None, stream=io.StringIO()
        )
        assert console_interrupt.stop_requested() is True
        assert console_interrupt.interrupt_now_requested() is False
    finally:
        console_interrupt.CONTROLLER.reset()
