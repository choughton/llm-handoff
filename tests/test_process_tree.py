from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from llm_handoff import agent_process
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
    monkeypatch.setattr(agent_process, "_WINDOWS", True)
    assert agent_process._process_group_popen_kwargs() == {}
    monkeypatch.setattr(agent_process, "_WINDOWS", False)
    assert agent_process._process_group_popen_kwargs() == {"start_new_session": True}


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
        lambda _repo_root: supervisor.RepoSnapshot(
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
