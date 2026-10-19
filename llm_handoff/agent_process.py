from __future__ import annotations

import codecs
import io
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping, TextIO

from llm_handoff.agent_streams import StderrMode, _LiveAgentOutputMonitor
from llm_handoff.agent_types import LogFn, _ProcessResult
from llm_handoff.config import VENDOR_API_KEY_ENV_NAMES, AgentConfig
from llm_handoff.console_interrupt import (
    deferred_interrupts,
    interrupt_now_requested,
)


PROCESS_READER_JOIN_TIMEOUT_SECONDS = 0.2
PROCESS_READ_CHUNK_BYTES = 65536
PROCESS_TREE_KILL_TIMEOUT_SECONDS = 30
_WINDOWS = os.name == "nt"

# The launcher lines npm's cmd-shim writes into ``<prefix>\<name>.cmd``:
#   SET "_prog=node"
#   "%_prog%"  "%dp0%\node_modules\<pkg>\bin\<entry>.js" %*
_NPM_SHIM_PROGRAM_RE = re.compile(r'SET\s+"_prog=([^"%]+)"', re.IGNORECASE)
# Anchored to ``"%_prog%"`` so a shim whose shebang carried interpreter
# options (``"%_prog%" --loader x "%dp0%\..."``) is not recognized.
_NPM_SHIM_TARGET_RE = re.compile(
    r'"%_prog%"\s+"%dp0%\\([^"%]+)"\s+%\*', re.IGNORECASE
)
# cmd-shim writes shebang environment assignments as ``@SET name=value``.
_NPM_SHIM_ENV_RE = re.compile(r"^@SET\s", re.IGNORECASE | re.MULTILINE)
# Script suffixes that a ``PATHEXT`` lookup may return for a bare program
# name (``node`` -> ``node.js``); a bypass launch needs a real executable.
_SCRIPT_SUFFIXES = frozenset({".js", ".cjs", ".mjs", ".cmd", ".bat", ".ps1", ".py"})

# Environment variables every provider CLI receives in ``allowlist`` mode.
# Names are compared case-insensitively because Windows environment names are
# case-insensitive.
BASE_AGENT_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "TERM",
        # Account name, not a secret. Tools such as pytest name per-user
        # directories after it; without it they fall back to a shared
        # "unknown" directory another account may own.
        "USERNAME",
        "USER",
        "LOGNAME",
    }
)
# Non-secret process plumbing the CLIs need to start at all: Windows `.cmd`
# shims need COMSPEC/PATHEXT/WINDIR, Node-based CLIs read APPDATA and
# LOCALAPPDATA, and POSIX tools use TMPDIR and SHELL.
PLATFORM_AGENT_ENV_NAMES = frozenset(
    {
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "TMPDIR",
        "SHELL",
    }
)
# Provider-owned auth and config variables. Vendor API keys are handled
# separately by ``_granted_vendor_keys``.
PROVIDER_AGENT_ENV_PREFIXES: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_",),
    "codex": ("CODEX_",),
    # agy reuses the sign-in stored by an interactive session under the user
    # profile (HOME/USERPROFILE/APPDATA/LOCALAPPDATA), so it needs no
    # provider variables.
    "antigravity": (),
}
PROVIDER_AGENT_ENV_NAMES: dict[str, frozenset[str]] = {
    "claude": frozenset(),
    "codex": frozenset(),
    "antigravity": frozenset(),
}


def build_agent_env(
    agent_config: AgentConfig,
    *,
    use_api_key_env: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment for one provider CLI launch.

    ``allowlist`` mode (the default) passes only process plumbing, the
    provider's own auth/config variables, and names the role lists under
    ``env_passthrough``. ``inherit`` mode restores the previous behavior of
    copying the dispatcher's environment. In both modes vendor API keys only
    reach their own provider, and only when the provider rule grants them.
    """
    source = os.environ if environ is None else environ
    provider = agent_config.provider
    effective_use_api_key_env = (
        bool(agent_config.use_api_key_env)
        if use_api_key_env is None
        else use_api_key_env
    )
    granted_keys = _granted_vendor_keys(
        provider,
        use_api_key_env=effective_use_api_key_env,
    )

    if agent_config.env_mode == "inherit":
        return {
            key: value
            for key, value in source.items()
            if key.upper() not in VENDOR_API_KEY_ENV_NAMES
            or key.upper() in granted_keys
        }

    allowed_names = (
        BASE_AGENT_ENV_NAMES
        | PLATFORM_AGENT_ENV_NAMES
        | PROVIDER_AGENT_ENV_NAMES.get(provider, frozenset())
        | {name.upper() for name in agent_config.env_passthrough}
    )
    allowed_prefixes = PROVIDER_AGENT_ENV_PREFIXES.get(provider, ())
    env: dict[str, str] = {}
    for key, value in source.items():
        upper_key = key.upper()
        if upper_key in VENDOR_API_KEY_ENV_NAMES:
            if upper_key in granted_keys:
                env[key] = value
            continue
        if upper_key in allowed_names or upper_key.startswith(allowed_prefixes):
            env[key] = value
    return env


def _granted_vendor_keys(provider: str, *, use_api_key_env: bool) -> frozenset[str]:
    # Claude CLI launches always use the CLI's own login, never an API key.
    # The Antigravity CLI has no documented API key and never receives one.
    if not use_api_key_env:
        return frozenset()
    if provider == "codex":
        return frozenset({"OPENAI_API_KEY"})
    return frozenset()


def _process_group_popen_kwargs() -> dict[str, object]:
    # Start the child outside the console's process group so a console Ctrl+C
    # reaches only the dispatcher, which then decides whether to stop.
    # On Windows, CREATE_NEW_PROCESS_GROUP also makes the child ignore Ctrl+C;
    # _kill_process_tree still uses taskkill /T /F for the tree kill.
    # On POSIX, a new session gives the whole tree one process group that
    # _kill_process_tree can signal at once.
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _start_isolated_process(
    command: list[str] | str,
    **popen_kwargs: object,
) -> subprocess.Popen[str]:
    """Start ``command`` in its own process group and return the process.

    A Ctrl+C that arrives while the child is being created is held back
    until ``Popen`` returns. If it asked to stop at once, the new child's
    tree is killed here and the KeyboardInterrupt re-raised, so a child that
    no longer receives the console Ctrl+C cannot outlive the dispatcher.
    Callers should enter their own kill-on-exception ``try`` block directly
    after this call returns. ``OSError`` from ``Popen`` propagates unchanged.
    """
    process: subprocess.Popen[str] | None = None
    try:
        with deferred_interrupts():
            process = subprocess.Popen(  # type: ignore[call-overload]
                command,
                **popen_kwargs,
                **_process_group_popen_kwargs(),
            )
    except KeyboardInterrupt:
        if process is not None:
            _kill_process_tree(process)
        raise
    assert process is not None
    return process


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate ``process`` and every descendant it spawned.

    On Windows the provider CLIs spawn children of their own (an npm shim
    launched through ``cmd.exe`` starts ``node``, and the Codex launcher
    script starts the native ``codex`` binary), so ``Popen.kill()`` alone
    leaves the inner process running and editing the repo. ``taskkill /T``
    walks the tree while the parent is still alive. The child was started in
    its own process group (``CREATE_NEW_PROCESS_GROUP``), so a console Ctrl+C
    does not reach it; this function is how the dispatcher ends it. On POSIX
    the child was started in its own session, so killing its process group
    reaches every descendant.
    """
    if process.poll() is not None:
        return
    if _WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=PROCESS_TREE_KILL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Always fall back to a direct kill so a failed tree kill still ends the
    # immediate child and lets the caller's wait() return.
    try:
        process.kill()
    except OSError:
        pass


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
    idle_timeout_ms: int | None = None,
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
            idle_timeout_ms=idle_timeout_ms,
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
        idle_timeout_ms=idle_timeout_ms,
        env=env,
        on_stdout=lambda chunk: monitor.consume("stdout", chunk),
        on_stderr=lambda chunk: monitor.consume("stderr", chunk),
    )


def _run_command_streaming(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    idle_timeout_ms: int | None = None,
    env: dict[str, str] | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> _ProcessResult:
    """Run ``command``, streaming its output, and kill it on a timeout.

    ``timeout_ms`` bounds the whole run. ``idle_timeout_ms``, when set, also
    kills the process once it has written nothing to stdout or stderr for
    that long, so a hung CLI is caught before the full timeout.
    """
    resolved_command = _resolve_command_argv(command)
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
        process = _start_isolated_process(resolved_command, **popen_kwargs)
    except OSError as exc:
        return _ProcessResult(stdout="", stderr=str(exc), exit_code=1)

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stream_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    timed_out = False
    idled_out = False
    # The kill-on-exception block starts right after the child exists, so
    # an interrupt during the reader setup also takes the tree down.
    try:
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

        last_output_at = time.monotonic()
        deadline = last_output_at + (timeout_ms / 1000)
        idle_limit = None if idle_timeout_ms is None else idle_timeout_ms / 1000
        while process.poll() is None and time.monotonic() < deadline:
            if interrupt_now_requested():
                # A second console Ctrl+C. It raises KeyboardInterrupt only
                # on the main thread, so a run on a worker thread (the audit
                # fan-out) stops here instead.
                raise KeyboardInterrupt
            if _drain_stream_queue(
                stream_queue,
                stdout_parts=stdout_parts,
                stderr_parts=stderr_parts,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                timeout=0.1,
            ):
                last_output_at = time.monotonic()
            elif (
                idle_limit is not None
                and time.monotonic() - last_output_at >= idle_limit
            ):
                idled_out = True
                break
    except BaseException:
        # A KeyboardInterrupt no longer reaches a child that runs in its own
        # process group, so take the tree down before propagating.
        _kill_process_tree(process)
        raise

    if process.poll() is None:
        timed_out = True
        _kill_process_tree(process)

    process.wait()
    for reader in readers:
        if timed_out:
            reader.join(timeout=PROCESS_READER_JOIN_TIMEOUT_SECONDS)
        else:
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
        # Both messages keep the "Process timed out after" prefix that the
        # failover classifier treats as a timeout.
        if idled_out:
            message = (
                f"Process timed out after {idle_timeout_ms / 1000:g} seconds "
                "without output (idle_timeout_ms)."
            )
        else:
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
    """Queue each complete line of ``stream`` as one chunk.

    Reads whatever bytes are available rather than blocking in ``readline``,
    so output written without a newline (token streams, progress bars) still
    counts as activity: it is queued as a ``None`` chunk, which resets the
    idle timer without reaching the output buffers. Line endings are
    translated as in text-mode ``readline``.
    """
    if stream is None:
        return

    try:
        raw = getattr(stream, "buffer", None)
        if raw is None or not hasattr(raw, "read1"):
            for chunk in iter(stream.readline, ""):
                stream_queue.put((stream_name, chunk))
            return

        decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder(stream.encoding or "utf-8")(
                errors=stream.errors or "strict"
            ),
            translate=True,
        )
        pending = ""
        while True:
            data = raw.read1(PROCESS_READ_CHUNK_BYTES)
            pending += decoder.decode(data, final=not data)
            emitted = False
            while (end := pending.find("\n")) >= 0:
                stream_queue.put((stream_name, pending[: end + 1]))
                pending = pending[end + 1 :]
                emitted = True
            if not data:
                if pending:
                    stream_queue.put((stream_name, pending))
                return
            if not emitted:
                stream_queue.put((stream_name, None))
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
) -> bool:
    """Move queued output into the buffers; return True if any chunk arrived."""
    received = False
    while True:
        try:
            if drain_all:
                stream_name, chunk = stream_queue.get_nowait()
            else:
                stream_name, chunk = stream_queue.get(timeout=timeout)
        except queue.Empty:
            return received

        # A None chunk marks a partial line: activity, but nothing to record.
        received = True
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
            return received


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: dict[str, str] | None = None,
) -> _ProcessResult:
    resolved_command = _resolve_command_argv(command)
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


def _resolve_command_argv(command: list[str]) -> list[str]:
    """Return the argv to hand to ``Popen`` for ``command``.

    ``argv[0]`` is resolved with :func:`_resolve_command_binary`. When that
    lands on an npm ``.cmd``/``.bat`` shim, the shim is bypassed and its
    target script is launched directly (``node <entry>.js <args>``).

    Running a ``.cmd`` file makes ``cmd.exe`` re-parse the command line, and
    it does not understand the ``\\"`` escapes Python writes for inner
    quotes. A prompt such as ``"summary": "<one line>"`` then puts ``<one``
    outside quotes, which ``cmd.exe`` reads as input redirection from a
    missing file and fails with "The system cannot find the file
    specified." before the CLI ever starts. Launching the target directly
    keeps every argument intact and makes the quoting rules the same as on
    POSIX.
    """
    resolved = list(command)
    if not resolved:
        return resolved
    resolved[0] = _resolve_command_binary(resolved[0])
    direct = _npm_shim_direct_launch(Path(resolved[0]))
    if direct is not None:
        return [*direct, *resolved[1:]]
    return resolved


def _npm_shim_direct_launch(shim: Path) -> list[str] | None:
    """Return ``[program, script]`` for an npm cmd-shim, or None.

    Only the plain shape npm's ``cmd-shim`` writes for a zero-option shebang
    is recognized: a ``SET "_prog=<program>"`` line plus a
    ``"%_prog%" "%dp0%\\<script>" %*`` launcher, with no interpreter options
    between them and no shebang environment assignments. Anything else,
    including a hand-written ``.cmd`` wrapper, is left to run through
    ``cmd.exe`` as before.
    """
    if shim.suffix.lower() not in (".cmd", ".bat"):
        return None
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    program_match = _NPM_SHIM_PROGRAM_RE.search(text)
    target_match = _NPM_SHIM_TARGET_RE.search(text)
    if program_match is None or target_match is None:
        return None
    if _NPM_SHIM_ENV_RE.search(text) is not None:
        return None
    program = program_match.group(1).strip()
    script = shim.parent.joinpath(*target_match.group(1).split("\\"))
    if not program or not script.is_file():
        return None
    # cmd-shim prefers a program installed next to the shim (the npm prefix)
    # and falls back to PATH, so do the same.
    local_program = shim.parent / f"{program}.exe"
    if local_program.is_file():
        return [str(local_program), str(script)]
    resolved_program = shutil.which(program)
    if resolved_program is None or not _is_bypass_executable(Path(resolved_program)):
        return None
    return [resolved_program, str(script)]


def _is_bypass_executable(path: Path) -> bool:
    """Return whether a PATH lookup found a program ``Popen`` can run directly.

    The shim strips ``.JS`` from ``PATHEXT`` before it resolves its program;
    ``shutil.which`` does not, so ``node`` can come back as a ``node.js``
    script on PATH. On Windows only a native executable qualifies.
    """
    suffix = path.suffix.lower()
    if _WINDOWS:
        return suffix in (".exe", ".com")
    return suffix not in _SCRIPT_SUFFIXES


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
