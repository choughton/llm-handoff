from __future__ import annotations

import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, TextIO

from llm_handoff.agent_streams import StderrMode, _LiveAgentOutputMonitor
from llm_handoff.agent_types import LogFn, _ProcessResult


def _append_agent_additional_instruction(
    prompt: str,
    additional_instruction: str | None,
) -> str:
    if not additional_instruction:
        return prompt
    return f"{prompt} Additional instruction: {additional_instruction.strip()}"


def _first_nonempty_value(payload: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _resolve_handoff_path(handoff_path: Path, repo_root: Path) -> Path:
    if handoff_path.is_absolute():
        return handoff_path
    return (repo_root / handoff_path).resolve()


def _format_process_failure_detail(result: _ProcessResult) -> str:
    parts: list[str] = []
    if result.stderr.strip():
        parts.append(f"stderr:\n{result.stderr.strip()}")
    if result.stdout.strip():
        parts.append(f"stdout:\n{result.stdout.strip()}")
    return "\n\n".join(parts)


def _run_logged_agent_command(
    agent_name: str,
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None = None,
    log: LogFn | None = None,
    stream_all_stdout: bool = False,
    stderr_mode: StderrMode = "warn",
) -> _ProcessResult:
    if log is None:
        return _run_command_streaming(
            command,
            cwd=cwd,
            timeout_ms=timeout_ms,
            env=env,
            on_stdout=None,
            on_stderr=None,
        )

    monitor = _LiveAgentOutputMonitor(
        agent_name=agent_name,
        log=log,
        stream_all_stdout=stream_all_stdout,
        stderr_mode=stderr_mode,
    )
    return _run_command_streaming(
        command,
        cwd=cwd,
        timeout_ms=timeout_ms,
        env=env,
        on_stdout=lambda chunk: monitor.consume("stdout", chunk),
        on_stderr=lambda chunk: monitor.consume("stderr", chunk),
    )


def _run_command_streaming(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> _ProcessResult:
    resolved_command = list(command)
    resolved_command[0] = _resolve_command_binary(resolved_command[0])
    popen_kwargs = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if env is not None:
        popen_kwargs["env"] = env

    try:
        process = subprocess.Popen(resolved_command, **popen_kwargs)
    except OSError as exc:
        return _ProcessResult(stdout="", stderr=str(exc), exit_code=1)

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stream_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    readers = [
        threading.Thread(
            target=_read_process_stream,
            args=(process.stdout, "stdout", stream_queue),
            daemon=True,
        ),
        threading.Thread(
            target=_read_process_stream,
            args=(process.stderr, "stderr", stream_queue),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + (timeout_ms / 1000)
    timed_out = False
    while process.poll() is None and time.monotonic() < deadline:
        _drain_stream_queue(
            stream_queue,
            stdout_parts=stdout_parts,
            stderr_parts=stderr_parts,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            timeout=0.1,
        )

    if process.poll() is None:
        timed_out = True
        process.kill()

    process.wait()
    for reader in readers:
        reader.join()
    _drain_stream_queue(
        stream_queue,
        stdout_parts=stdout_parts,
        stderr_parts=stderr_parts,
        on_stdout=on_stdout,
        on_stderr=on_stderr,
        timeout=0.0,
        drain_all=True,
    )

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    if timed_out:
        message = f"Process timed out after {timeout_ms / 1000:g} seconds."
        if stderr:
            message = f"{message}\n{stderr}"
        return _ProcessResult(stdout=stdout, stderr=message, exit_code=1)

    return _ProcessResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=int(process.returncode),
    )


def _read_process_stream(
    stream: TextIO | None,
    stream_name: str,
    stream_queue: queue.Queue[tuple[str, str | None]],
) -> None:
    if stream is None:
        return

    try:
        for chunk in iter(stream.readline, ""):
            stream_queue.put((stream_name, chunk))
    finally:
        stream.close()


def _drain_stream_queue(
    stream_queue: queue.Queue[tuple[str, str | None]],
    *,
    stdout_parts: list[str],
    stderr_parts: list[str],
    on_stdout: Callable[[str], None] | None,
    on_stderr: Callable[[str], None] | None,
    timeout: float,
    drain_all: bool = False,
) -> None:
    while True:
        try:
            if drain_all:
                stream_name, chunk = stream_queue.get_nowait()
            else:
                stream_name, chunk = stream_queue.get(timeout=timeout)
        except queue.Empty:
            return

        if chunk is None:
            continue

        if stream_name == "stdout":
            stdout_parts.append(chunk)
            if on_stdout is not None:
                on_stdout(chunk)
        else:
            stderr_parts.append(chunk)
            if on_stderr is not None:
                on_stderr(chunk)

        if not drain_all:
            return


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None = None,
) -> _ProcessResult:
    resolved_command = list(command)
    resolved_command[0] = _resolve_command_binary(resolved_command[0])
    run_kwargs = {
        "cwd": cwd,
        "capture_output": True,
        "text": True,
        "timeout": timeout_ms / 1000,
    }
    if env is not None:
        run_kwargs["env"] = env

    try:
        completed = subprocess.run(resolved_command, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_output(getattr(exc, "output", None))
        stderr = _coerce_output(getattr(exc, "stderr", None))
        message = f"Process timed out after {timeout_ms / 1000:g} seconds."
        if stderr:
            message = f"{message}\n{stderr}"
        return _ProcessResult(stdout=stdout, stderr=message, exit_code=1)
    except OSError as exc:
        return _ProcessResult(stdout="", stderr=str(exc), exit_code=1)

    return _ProcessResult(
        stdout=_coerce_output(completed.stdout),
        stderr=_coerce_output(completed.stderr),
        exit_code=int(completed.returncode),
    )


def _coerce_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _resolve_command_binary(command: str) -> str:
    if not command:
        return command

    if Path(command).is_absolute() or any(sep in command for sep in ("\\", "/")):
        return command

    resolved = shutil.which(command)
    if resolved:
        return resolved

    path = Path(command)
    if path.suffix.lower() == ".cmd":
        resolved_without_cmd = shutil.which(path.stem)
        if resolved_without_cmd:
            return resolved_without_cmd

    return command


def _wait_for_manual_continue() -> None:
    if os.name == "nt":
        try:
            import msvcrt

            msvcrt.getwch()
            return
        except (ImportError, OSError):
            pass

    if sys.stdin is None:
        return

    if os.name != "nt" and sys.stdin.isatty():
        try:
            import termios
            import tty

            file_descriptor = sys.stdin.fileno()
            original_settings = termios.tcgetattr(file_descriptor)
            try:
                tty.setraw(file_descriptor)
                sys.stdin.read(1)
            finally:
                termios.tcsetattr(
                    file_descriptor,
                    termios.TCSADRAIN,
                    original_settings,
                )
            return
        except (AttributeError, OSError, ValueError):
            pass

    try:
        input()
    except EOFError:
        return
