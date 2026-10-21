from __future__ import annotations

import io
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from llm_handoff import agent_process
from llm_handoff import chat_takeover
from llm_handoff import console_interrupt
from llm_handoff import supervisor


_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="exercises .cmd shim process trees, which only exist on Windows",
)


def _windows_pid_is_alive(pid: int) -> bool:
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _wait_until(predicate: Any, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


@_WINDOWS_ONLY
def test_timed_out_cmd_shim_child_is_killed_with_its_tree(tmp_path: Path) -> None:
    # Mirror the real layout: the configured binary is a .cmd shim (like the
    # npm-installed provider CLIs) that launches a long-lived inner process.
    pid_file = tmp_path / "inner.pid"
    inner_script = tmp_path / "inner.py"
    inner_script.write_text(
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    shim = tmp_path / "agent.cmd"
    shim.write_text(
        f'@echo off\r\n"{sys.executable}" "{inner_script}" "{pid_file}"\r\n',
        encoding="utf-8",
    )

    result = agent_process._run_command_streaming(
        [str(shim)],
        cwd=tmp_path,
        timeout_ms=3000,
    )

    assert result.exit_code == 1
    assert "Process timed out after 3 seconds." in result.stderr
    assert pid_file.exists(), "inner process never started inside the shim"
    inner_pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert _wait_until(
        lambda: not _windows_pid_is_alive(inner_pid),
        timeout_seconds=10,
    ), f"inner process {inner_pid} survived the timeout kill"


class _FakeStream:
    def readline(self) -> str:
        return ""

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, *, pid: int = 4242) -> None:
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode: int | None = None
        self.pid = pid
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = 1

    def wait(self) -> int:
        return self.returncode if self.returncode is not None else 1


def test_run_command_streaming_kills_process_tree_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    tree_kills: list[Any] = []

    def fake_kill_tree(target: Any) -> None:
        tree_kills.append(target)
        target.kill()

    monkeypatch.setattr(
        agent_process.subprocess,
        "Popen",
        lambda _command, **_kwargs: process,
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", fake_kill_tree)
    monotonic_values = iter([0.0, 1.0, 1.0])
    monkeypatch.setattr(
        agent_process.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    result = agent_process._run_command_streaming(
        ["agent.cmd"],
        cwd=Path.cwd(),
        timeout_ms=1,
    )

    assert result.exit_code == 1
    assert "Process timed out after" in result.stderr
    assert tree_kills == [process]


def test_run_command_streaming_stops_on_a_second_console_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A second Ctrl+C raises only on the main thread; a run on a worker
    # thread (the audit fan-out) sees the controller's flag instead.
    process = _FakeProcess()
    tree_kills: list[Any] = []

    def fake_kill_tree(target: Any) -> None:
        tree_kills.append(target)
        target.kill()

    monkeypatch.setattr(
        agent_process.subprocess,
        "Popen",
        lambda _command, **_kwargs: process,
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", fake_kill_tree)
    monkeypatch.setattr(agent_process, "interrupt_now_requested", lambda: True)

    with pytest.raises(KeyboardInterrupt):
        agent_process._run_command_streaming(
            ["agent.cmd"],
            cwd=Path.cwd(),
            timeout_ms=60_000,
        )

    assert tree_kills == [process]


class _ExitsAfterPollsProcess(_FakeProcess):
    """A fake child that runs for a few polls, then exits 0 on its own."""

    def __init__(self, *, running_polls: int) -> None:
        super().__init__()
        self._running_polls = running_polls

    def poll(self) -> int | None:
        if self.returncode is None and self._running_polls <= 0:
            self.returncode = 0
        self._running_polls -= 1
        return self.returncode


def test_run_command_streaming_stops_on_a_chat_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    tree_kills: list[Any] = []

    def fake_kill_tree(target: Any) -> None:
        tree_kills.append(target)
        target.kill()

    monkeypatch.setattr(
        agent_process.subprocess,
        "Popen",
        lambda _command, **_kwargs: process,
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", fake_kill_tree)
    monkeypatch.setattr(agent_process, "interrupt_now_requested", lambda: False)
    monkeypatch.setattr(agent_process, "takeover_requested", lambda: True)

    with pytest.raises(chat_takeover.ChatTakeover):
        agent_process._run_command_streaming(
            ["agent.cmd"],
            cwd=Path.cwd(),
            timeout_ms=60_000,
        )

    assert tree_kills == [process]


def test_run_command_streaming_ignores_a_chat_takeover_when_not_stoppable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _ExitsAfterPollsProcess(running_polls=3)
    tree_kills: list[Any] = []
    monkeypatch.setattr(
        agent_process.subprocess,
        "Popen",
        lambda _command, **_kwargs: process,
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)
    monkeypatch.setattr(agent_process, "interrupt_now_requested", lambda: False)
    monkeypatch.setattr(agent_process, "takeover_requested", lambda: True)

    result = agent_process._run_command_streaming(
        ["agent.cmd"],
        cwd=Path.cwd(),
        timeout_ms=60_000,
        stop_on_chat_takeover=False,
    )

    assert result.exit_code == 0
    assert tree_kills == []


def test_run_command_streaming_second_ctrl_c_wins_over_a_chat_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    tree_kills: list[Any] = []

    def fake_kill_tree(target: Any) -> None:
        tree_kills.append(target)
        target.kill()

    monkeypatch.setattr(
        agent_process.subprocess,
        "Popen",
        lambda _command, **_kwargs: process,
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", fake_kill_tree)
    monkeypatch.setattr(agent_process, "interrupt_now_requested", lambda: True)
    monkeypatch.setattr(agent_process, "takeover_requested", lambda: True)

    with pytest.raises(KeyboardInterrupt):
        agent_process._run_command_streaming(
            ["agent.cmd"],
            cwd=Path.cwd(),
            timeout_ms=60_000,
        )

    assert tree_kills == [process]


def test_run_command_streaming_starts_child_with_stdin_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An agent must never read the operator's console input.
    process = _FakeProcess()
    process.returncode = 0
    seen_kwargs: list[dict[str, Any]] = []

    def fake_start(_command: Any, **kwargs: Any) -> Any:
        seen_kwargs.append(kwargs)
        return process

    monkeypatch.setattr(agent_process, "_start_isolated_process", fake_start)

    result = agent_process._run_command_streaming(
        ["agent.cmd"],
        cwd=Path.cwd(),
        timeout_ms=60_000,
    )

    assert result.exit_code == 0
    [kwargs] = seen_kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_run_command_starts_child_with_stdin_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_kwargs: list[dict[str, Any]] = []

    def fake_run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_kwargs.append(kwargs)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(agent_process.subprocess, "run", fake_run)

    result = agent_process._run_command(
        ["reviewer.cmd"],
        cwd=Path.cwd(),
        timeout_ms=60_000,
    )

    assert result.exit_code == 0
    [kwargs] = seen_kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_run_command_streaming_child_reads_eof_from_stdin(tmp_path: Path) -> None:
    # A real child that reads stdin gets EOF at once instead of blocking on
    # the console.
    result = agent_process._run_command_streaming(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
        cwd=tmp_path,
        timeout_ms=30_000,
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "''"


def test_kill_process_tree_uses_taskkill_tree_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(pid=5150)
    run_calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_calls.append(list(command))
        return subprocess.CompletedProcess(args=command, returncode=0)

    monkeypatch.setattr(agent_process, "_WINDOWS", True)
    monkeypatch.setattr(agent_process.subprocess, "run", fake_run)

    agent_process._kill_process_tree(process)

    assert run_calls == [["taskkill", "/T", "/F", "/PID", "5150"]]
    assert process.kill_calls == 1


def test_kill_process_tree_kills_process_group_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(pid=6160)
    killpg_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(agent_process, "_WINDOWS", False)
    monkeypatch.setattr(agent_process.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(agent_process.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        agent_process.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
        raising=False,
    )

    agent_process._kill_process_tree(process)

    assert killpg_calls == [(6160, 9)]
    assert process.kill_calls == 1


def test_kill_process_tree_skips_exited_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    process.returncode = 0
    monkeypatch.setattr(agent_process, "_WINDOWS", True)
    monkeypatch.setattr(
        agent_process.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("taskkill must not run for an exited process")
        ),
    )

    agent_process._kill_process_tree(process)

    assert process.kill_calls == 0


def test_process_group_popen_kwargs_follow_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        monkeypatch.setattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
        )
    monkeypatch.setattr(agent_process, "_WINDOWS", True)
    assert agent_process._process_group_popen_kwargs() == {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
    }
    monkeypatch.setattr(agent_process, "_WINDOWS", False)
    assert agent_process._process_group_popen_kwargs() == {"start_new_session": True}


@pytest.fixture
def shared_controller() -> Any:
    console_interrupt.CONTROLLER.reset()
    try:
        yield console_interrupt.CONTROLLER
    finally:
        console_interrupt.CONTROLLER.reset()


def _popen_that_gets_ctrl_c(
    process: Any,
    presses: int,
    seen_kwargs: dict[str, Any] | None = None,
) -> Any:
    # Simulates Ctrl+C arriving while Popen is still creating the child.
    def fake_popen(_command: Any, **kwargs: Any) -> Any:
        if seen_kwargs is not None:
            seen_kwargs.update(kwargs)
        for _ in range(presses):
            console_interrupt.CONTROLLER.handle_sigint(
                signal.SIGINT, None, stream=io.StringIO()
            )
        return process

    return fake_popen


def test_start_isolated_process_kills_child_on_second_ctrl_c_during_creation(
    monkeypatch: pytest.MonkeyPatch,
    shared_controller: Any,
) -> None:
    process = _FakeProcess()
    tree_kills: list[Any] = []
    seen_kwargs: dict[str, Any] = {}
    shared_controller.set_active_role("backend")
    monkeypatch.setattr(
        agent_process.subprocess,
        "Popen",
        _popen_that_gets_ctrl_c(process, 2, seen_kwargs),
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)

    with pytest.raises(KeyboardInterrupt):
        agent_process._start_isolated_process(["agent"], cwd="x")

    assert tree_kills == [process]
    assert shared_controller.interrupt_now_requested() is True
    assert seen_kwargs["cwd"] == "x"
    for key, value in agent_process._process_group_popen_kwargs().items():
        assert seen_kwargs[key] == value


def test_start_isolated_process_kills_child_on_ctrl_c_with_no_active_role(
    monkeypatch: pytest.MonkeyPatch,
    shared_controller: Any,
) -> None:
    process = _FakeProcess()
    tree_kills: list[Any] = []
    monkeypatch.setattr(
        agent_process.subprocess, "Popen", _popen_that_gets_ctrl_c(process, 1)
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)

    with pytest.raises(KeyboardInterrupt):
        agent_process._start_isolated_process(["agent"])

    assert tree_kills == [process]


def test_start_isolated_process_keeps_child_on_first_ctrl_c_during_creation(
    monkeypatch: pytest.MonkeyPatch,
    shared_controller: Any,
) -> None:
    process = _FakeProcess()
    tree_kills: list[Any] = []
    shared_controller.set_active_role("backend")
    monkeypatch.setattr(
        agent_process.subprocess, "Popen", _popen_that_gets_ctrl_c(process, 1)
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)

    assert agent_process._start_isolated_process(["agent"]) is process

    assert tree_kills == []
    assert shared_controller.stop_requested() is True


def test_start_isolated_process_raises_deferred_ctrl_c_when_popen_fails(
    monkeypatch: pytest.MonkeyPatch,
    shared_controller: Any,
) -> None:
    tree_kills: list[Any] = []

    def failing_popen(_command: Any, **_kwargs: Any) -> Any:
        console_interrupt.CONTROLLER.handle_sigint(
            signal.SIGINT, None, stream=io.StringIO()
        )
        raise OSError("no such program")

    monkeypatch.setattr(agent_process.subprocess, "Popen", failing_popen)
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)

    with pytest.raises(KeyboardInterrupt):
        agent_process._start_isolated_process(["agent"])

    assert tree_kills == []


def test_run_command_streaming_kills_child_on_ctrl_c_during_creation(
    monkeypatch: pytest.MonkeyPatch,
    shared_controller: Any,
) -> None:
    process = _FakeProcess()
    tree_kills: list[Any] = []
    monkeypatch.setattr(
        agent_process.subprocess, "Popen", _popen_that_gets_ctrl_c(process, 1)
    )
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)

    with pytest.raises(KeyboardInterrupt):
        agent_process._run_command_streaming(
            ["agent.cmd"],
            cwd=Path.cwd(),
            timeout_ms=60_000,
        )

    assert tree_kills == [process]


def test_run_command_streaming_builds_its_queue_before_the_child_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An interrupt while the output queue is built must not leave a child
    # running: the queue exists before the launch, so no child is started.
    launches: list[Any] = []
    tree_kills: list[Any] = []

    def interrupted_queue(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    def fake_popen(command: Any, **_kwargs: Any) -> Any:
        launches.append(command)
        return _FakeProcess()

    monkeypatch.setattr(agent_process.queue, "Queue", interrupted_queue)
    monkeypatch.setattr(agent_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)

    with pytest.raises(KeyboardInterrupt):
        agent_process._run_command_streaming(
            ["agent.cmd"],
            cwd=Path.cwd(),
            timeout_ms=60_000,
        )

    assert launches == []
    assert tree_kills == []


def test_run_command_streaming_kills_child_when_reader_setup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    tree_kills: list[Any] = []

    def interrupted_thread(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        agent_process.subprocess, "Popen", lambda _command, **_kwargs: process
    )
    monkeypatch.setattr(agent_process.threading, "Thread", interrupted_thread)
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)

    with pytest.raises(KeyboardInterrupt):
        agent_process._run_command_streaming(
            ["agent.cmd"],
            cwd=Path.cwd(),
            timeout_ms=60_000,
        )

    assert tree_kills == [process]


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal handlers can only be installed on the main thread",
)
def test_supervisor_main_defers_ctrl_c_during_child_creation(
    monkeypatch: pytest.MonkeyPatch,
    shared_controller: Any,
) -> None:
    # Uses the supervisor's real handler setup: main() must install the
    # controller, or a Ctrl+C inside Popen raises before the supervisor
    # has a handle and the new child dispatcher outlives it.
    process = _FakeProcess()
    tree_kills: list[Any] = []
    seen_handlers: list[Any] = []
    handler_before = signal.getsignal(signal.SIGINT)

    def popen_that_gets_ctrl_c(_command: Any, **_kwargs: Any) -> Any:
        handler = signal.getsignal(signal.SIGINT)
        seen_handlers.append(handler)
        assert callable(handler)
        handler(signal.SIGINT, None)
        return process

    def fake_run_supervised_dispatch(_config: Any) -> int:
        supervisor._start_isolated_process(["dispatch"])
        return 0

    monkeypatch.setattr(supervisor, "_parse_args", lambda _argv: object())
    monkeypatch.setattr(
        supervisor, "run_supervised_dispatch", fake_run_supervised_dispatch
    )
    monkeypatch.setattr(agent_process.subprocess, "Popen", popen_that_gets_ctrl_c)
    monkeypatch.setattr(agent_process, "_kill_process_tree", tree_kills.append)

    with pytest.raises(KeyboardInterrupt):
        supervisor.main([])

    assert seen_handlers == [console_interrupt.CONTROLLER.handle_sigint]
    assert tree_kills == [process]
    assert signal.getsignal(signal.SIGINT) == handler_before


def test_supervisor_kills_child_tree_when_interrupted_before_the_read_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kill-on-exception block starts right after the child exists, so an
    # interrupt during the run-state snapshot also takes the tree down.
    child = _StalledChild(threading.Event())
    tree_kills: list[Any] = []

    def interrupted_snapshot(_repo_root: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        supervisor.subprocess, "Popen", lambda _command, **_kwargs: child
    )
    monkeypatch.setattr(supervisor, "_kill_process_tree", tree_kills.append)
    monkeypatch.setattr(supervisor, "_repo_snapshot", interrupted_snapshot)
    config = supervisor.SupervisorConfig(repo_root=tmp_path)

    with pytest.raises(KeyboardInterrupt):
        supervisor._run_dispatch_child(config, lambda level, message: None)

    assert tree_kills == [child]


class _BlockingStdout:
    def __init__(self, released: threading.Event) -> None:
        self._released = released

    def __iter__(self) -> Any:
        self._released.wait()
        return iter(())


class _StalledChild:
    def __init__(self, released: threading.Event) -> None:
        self.stdout = _BlockingStdout(released)
        self.pid = 7170
        self.returncode: int | None = None
        self._released = released

    def kill(self) -> None:
        self.returncode = 1
        self._released.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        self._released.wait()
        return self.returncode if self.returncode is not None else 1


def test_supervisor_stall_kills_dispatch_child_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = threading.Event()
    child = _StalledChild(released)
    tree_kills: list[Any] = []

    def fake_kill_tree(target: Any) -> None:
        tree_kills.append(target)
        target.kill()

    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda _command, **_kwargs: child,
    )
    monkeypatch.setattr(supervisor, "_kill_process_tree", fake_kill_tree)
    # Patching Popen also hijacks subprocess.run; keep git snapshots static.
    monkeypatch.setattr(
        supervisor,
        "_repo_snapshot",
        lambda _repo_root, **_kwargs: supervisor.RepoSnapshot(
            head_sha=None, dirty_files=(), handoff_sha=None
        ),
    )
    config = supervisor.SupervisorConfig(
        repo_root=tmp_path,
        stalled_output_timeout_seconds=1,
        heartbeat_interval_seconds=0,
    )

    result = supervisor._run_dispatch_child(config, lambda level, message: None)

    assert result.exit_code == 124
    assert tree_kills == [child]
    assert any("stalled-output timeout" in line for line in result.output_lines)
