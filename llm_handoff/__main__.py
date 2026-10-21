from __future__ import annotations

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import sys
import time
from typing import Sequence, cast
import unicodedata

import click
import typer

from llm_handoff.agent_providers.codex import (
    _cleanup_codex_output_artifacts,
    _codex_artifact_paths,
)
from llm_handoff.antigravity_permissions import (
    ANTIGRAVITY_REQUIRED_DENY,
    antigravity_allow_entries,
    format_antigravity_permissions,
)
from llm_handoff import chat_session, console_interrupt
from llm_handoff.chat_answers import ChatAnswerWorker, RoleSession
from llm_handoff.chat_changes import chat_changes_path
from llm_handoff.chat_input import chat_input_for_console
from llm_handoff.chat_line import ChatLine
from llm_handoff.config import (
    DISPATCH_WINDOW_TITLE,
    AgentRole,
    DispatchConfig,
    detect_repo_root,
    load_dispatch_config,
)
from llm_handoff.doctor import run_doctor
from llm_handoff.init_workflow import (
    DEFAULT_TRACKER_MCP_SERVER,
    REFERENCE_MAPPING,
    TRACKER_NONE,
    InitConflictError,
    InvalidTrackerOptionsError,
    InvalidVerificationCommandError,
    TrackerRequiresFreshRepoError,
    UnknownProviderError,
    UnknownTemplateError,
    UnknownTrackerError,
    init_reference_workflow,
    is_interpreter_executable,
    verification_executable,
)
from llm_handoff.logging_util import DispatchLogger
from llm_handoff.stack_detect import Candidate
from llm_handoff.orchestrator import run_loop
from llm_handoff.stop_request import (
    DISPATCH_LOCK_FILENAME,
    DispatchLockInfo,
    clear_stale_stop_request,
    clear_stop_request,
    describe_lock_holder,
    dispatch_lock_path,
    process_is_running,
    read_dispatch_lock,
    request_stop,
    stop_request_is_ignored,
    stop_request_path,
)

_WINDOWS = os.name == "nt"
# `init --provider gemini` wrote the all-Gemini mapping, which was removed.
_INIT_GEMINI_REMOVED_MESSAGE = (
    "The gemini provider was removed: Gemini CLI no longer serves personal "
    "Google accounts. Use --provider antigravity (the agy CLI)."
)
# `--use-planner-api-key-env` only fed the removed gemini adapter.
_PLANNER_API_KEY_ENV_FLAG_REMOVED_MESSAGE = (
    "--use-planner-api-key-env was removed with the gemini provider: Gemini "
    "CLI no longer serves personal Google accounts. Use provider: antigravity "
    "(the agy CLI)."
)
app = typer.Typer(
    add_completion=False,
    help="File-based handoff dispatcher for multi-CLI AI coding workflows.",
    invoke_without_command=True,
)


@dataclass(frozen=True, slots=True)
class DispatchLock:
    path: Path
    fd: int


def _configure_stdio_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        encoding = getattr(stream, "encoding", None)
        reconfigure = getattr(stream, "reconfigure", None)
        if (
            isinstance(encoding, str)
            and encoding.lower() != "utf-8"
            and callable(reconfigure)
        ):
            reconfigure(encoding="utf-8", errors="replace")


def _set_dispatch_console_title() -> tuple[str, bool]:
    if not _WINDOWS:
        return "", False

    try:
        kernel32 = ctypes.windll.kernel32
        buffer_size = 32768
        buffer = ctypes.create_unicode_buffer(buffer_size)
        title_length = kernel32.GetConsoleTitleW(buffer, buffer_size)
        previous_title = buffer.value if title_length > 0 else ""
        # Preserve the captured title even if SetConsoleTitleW fails so restore
        # logic can distinguish "unchanged" from "unknown previous title".
        if not kernel32.SetConsoleTitleW(DISPATCH_WINDOW_TITLE):
            return previous_title, False
        return previous_title, True
    except (AttributeError, OSError):
        return "", False


def _restore_console_title(previous_title: str, *, changed: bool) -> None:
    if not changed or not _WINDOWS:
        return

    try:
        ctypes.windll.kernel32.SetConsoleTitleW(previous_title)
    except (AttributeError, OSError):
        return


def _stream_is_tty(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001 - any failure means "not a terminal"
        return False


def _role_session_lookup(
    config: DispatchConfig,
) -> Callable[[str], RoleSession | None]:
    """The ``@role`` lookup: the role's latest dispatch kept in this run.

    Reads ``chat_session``'s in-memory entries at call time, never the
    repository dispatch record. Returns None when no entry is kept, it has no
    session id or provider, or the role has no configured agent. The recorded
    provider and story id are kept; the worker checks provider support.
    """

    def lookup(role: str) -> RoleSession | None:
        entry = chat_session.latest_role_dispatch(role)
        if entry is None or entry.session_id is None or entry.provider is None:
            return None
        agent_config = config.agents.get(cast(AgentRole, role))
        if agent_config is None:
            return None
        return RoleSession(
            role=entry.role,
            provider=entry.provider,
            story_id=entry.story_id,
            session_id=entry.session_id,
            agent_config=agent_config,
        )

    return lookup


def _attach_console_chat(
    logger: DispatchLogger, config: DispatchConfig
) -> ChatAnswerWorker | None:
    """Attach the console chat line, key capture, and chat answer worker.

    Only when both stdin and stdout are terminals; under the supervisor the
    child's stdout is a pipe, so the chat line stays off there. A sent line
    goes to the returned ``ChatAnswerWorker``, which answers it for the role
    running at submit time and writes the answer through the logger. An
    ``@role`` line resumes that role's latest finished session through
    ``_role_session_lookup``. The caller stops the worker. Returns None when
    chat stays off.
    """

    if not (_stream_is_tty(sys.stdin) and _stream_is_tty(sys.stdout)):
        return None
    chat_line = ChatLine(sys.stdout)
    # ``ChatInput`` takes its ``on_submit`` only when it is built, and the
    # worker must not be built when console key input is unavailable. So ask
    # the factory first with no callback: building a ``ChatInput`` starts no
    # thread and opens no reader, and the probe is dropped unused.
    if chat_input_for_console(sys.stdin, chat_line) is None:
        return None

    def write_line(line: str) -> None:
        logger("AGENT", line)

    worker = ChatAnswerWorker(
        repo_root=config.repo_root,
        handoff_path=config.handoff_full_path,
        changes_path=chat_changes_path(config.repo_root),
        # Read the session at submit time, not at attach time.
        context=lambda: chat_session.SESSION.context,
        write_line=write_line,
        role_session=_role_session_lookup(config),
    )
    chat_input = chat_input_for_console(
        sys.stdin, chat_line, on_submit=worker.submit
    )
    if chat_input is None:
        return None
    logger.attach_chat_line(chat_line)
    chat_session.attach(chat_line, chat_input)
    chat_session.attach_worker(worker)
    return worker


def _run_dispatch(
    *,
    dry_run: bool,
    use_manual_frontend: bool,
    backend_resume: bool = True,
    planner_resume: bool = True,
    repo_root: Path | None = None,
    config_path: Path | None = None,
    no_interactive_pause: bool = False,
) -> int:
    root_start = repo_root
    if root_start is None and config_path is not None:
        root_start = config_path.parent
    config = load_dispatch_config(
        repo_root=detect_repo_root(root_start),
        config_path=config_path,
        dry_run=dry_run,
        use_manual_frontend=use_manual_frontend,
        backend_resume=backend_resume,
        planner_resume=planner_resume,
        interactive_pause=False if no_interactive_pause else None,
    )
    lock = _try_acquire_dispatch_lock(config.repo_root)
    if lock is None:
        lock_path = _dispatch_lock_path(config.repo_root)
        holder = read_dispatch_lock(config.repo_root)
        detail = f" {describe_lock_holder(holder)}" if holder is not None else ""
        print(
            "Another llm-handoff dispatcher appears to be running for this repo."
            f"{detail} Remove {lock_path} only after confirming no dispatcher "
            "is active.",
            file=sys.stderr,
        )
        return 1
    try:
        _cleanup_codex_output_artifacts(_codex_artifact_paths(config.repo_root))
        logger = DispatchLogger(
            repo_root=config.repo_root,
            max_consecutive_failures=config.max_consecutive_failures,
            backend_resume=config.backend_resume,
            planner_resume=config.planner_resume,
        )
        if clear_stale_stop_request(config.repo_root, pid=os.getpid()):
            # A request left by an earlier run is not meant for this one. A
            # request `llm-handoff stop` wrote for this pid after the lock
            # appeared is kept.
            logger(
                "WARN",
                "Removed a stop request left from before this run: "
                f"{stop_request_path(config.repo_root)}.",
            )
        previous_title, changed_title = _set_dispatch_console_title()
        chat_worker: ChatAnswerWorker | None = None
        try:
            try:
                if not dry_run:
                    chat_worker = _attach_console_chat(logger, config)
                # The first Ctrl+C during a role's cycle asks for a graceful
                # stop; a second one, or one outside a cycle, raises
                # KeyboardInterrupt.
                with console_interrupt.installed():
                    return run_loop(config, log=logger)
            finally:
                # Stop key capture first: ChatInput.stop() returns only after
                # any on_submit call in progress, and clearing the role makes
                # a late submit queue nothing. Only then is the worker's
                # restartable stop() safe, since no new submission can start
                # an answer after it returns. Finish the running chat answer
                # and drop the worker while the chat line still exists for
                # its final lines, then detach and erase the chat line before
                # any later console line, on every exit path.
                try:
                    if chat_worker is not None:
                        try:
                            chat_session.set_role(None)
                        finally:
                            chat_worker.stop()
                finally:
                    if chat_worker is not None:
                        chat_session.attach_worker(None)
                    chat_session.detach()
                    if getattr(logger, "chat_line", None) is not None:
                        logger.attach_chat_line(None)
        except KeyboardInterrupt:
            logger("WARN", "Dispatch interrupted by user. Exiting.")
            return 130
        finally:
            _restore_console_title(previous_title, changed=changed_title)
    finally:
        # A stop request only applies to the run that holds the lock.
        clear_stop_request(config.repo_root)
        _release_dispatch_lock(lock)


def _dispatch_lock_path(repo_root: Path) -> Path:
    return dispatch_lock_path(repo_root)


def _try_acquire_dispatch_lock(repo_root: Path) -> DispatchLock | None:
    lock_path = _dispatch_lock_path(repo_root)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
    except FileExistsError:
        return None
    except OSError as exc:
        print(f"Unable to create dispatch lock {lock_path}: {exc}", file=sys.stderr)
        return None

    lock_text = (
        f"pid: {os.getpid()}\n"
        f"created_at: {datetime.now().isoformat(timespec='seconds')}\n"
        f"repo_root: {Path(repo_root).resolve()}\n"
    )
    try:
        os.write(fd, lock_text.encode("utf-8"))
    except OSError as exc:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        print(f"Unable to write dispatch lock {lock_path}: {exc}", file=sys.stderr)
        return None
    return DispatchLock(path=lock_path, fd=fd)


def _release_dispatch_lock(lock: DispatchLock) -> None:
    try:
        os.close(lock.fd)
    finally:
        try:
            lock.path.unlink()
        except FileNotFoundError:
            pass


def _format_path_list(paths: tuple[Path, ...]) -> str:
    return "\n".join(f"  - {path.as_posix()}" for path in paths)


@app.command("init")
def init_command(
    target_root: Path = typer.Argument(
        Path("."),
        help="Target repository root to initialize.",
    ),
    template: str = typer.Option(
        "reference-workflow",
        "--template",
        help="Reference template to copy.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview copies and merges without writing files.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "--overwrite",
        help="Overwrite non-managed files; preserve user content in managed files.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help=(
            "Put every role on one provider CLI: claude, codex, or "
            "antigravity. Default: claude when Claude Code is the only provider "
            "CLI on PATH, codex when Codex is the only provider CLI on PATH, "
            "antigravity when agy is the only provider CLI on PATH, otherwise "
            "the reference mapping. Linear mode does not support antigravity."
        ),
    ),
    verification_command: str | None = typer.Option(
        None,
        "--verification-command",
        help=(
            "Confirmed verification command; its executable is added to "
            "Claude roles' allowed_tools, except python, python3, and pytest, "
            "which need the interpreter_grants opt-in."
        ),
    ),
    tracker: str = typer.Option(
        TRACKER_NONE,
        "--tracker",
        help=(
            "Issue tracker mode: none (default) or linear. linear only sets "
            "up a repository that has no dispatch_config.yaml yet."
        ),
    ),
    tracker_team: str | None = typer.Option(
        None,
        "--tracker-team",
        help="Linear team key. Required with --tracker linear.",
    ),
    tracker_project: str | None = typer.Option(
        None,
        "--tracker-project",
        help=(
            "Linear project that defines the issue pool. With --tracker "
            "linear, give exactly one of --tracker-project and --tracker-label."
        ),
    ),
    tracker_label: str | None = typer.Option(
        None,
        "--tracker-label",
        help=(
            "Linear label that defines the issue pool. With --tracker "
            "linear, give exactly one of --tracker-project and --tracker-label."
        ),
    ),
    tracker_mcp_server: str | None = typer.Option(
        None,
        "--tracker-mcp-server",
        help=(
            "Linear MCP server name in each CLI's MCP config. "
            f"Default: {DEFAULT_TRACKER_MCP_SERVER}."
        ),
    ),
) -> None:
    _configure_stdio_encoding()
    if provider == "gemini":
        typer.echo(_INIT_GEMINI_REMOVED_MESSAGE, err=True)
        raise typer.Exit(2)
    try:
        result = init_reference_workflow(
            target_root,
            template=template,
            dry_run=dry_run,
            force=force,
            provider=provider,
            verification_command=verification_command,
            tracker=tracker,
            tracker_team=tracker_team,
            tracker_project=tracker_project,
            tracker_label=tracker_label,
            tracker_mcp_server=tracker_mcp_server,
        )
    except (
        UnknownTemplateError,
        UnknownProviderError,
        InvalidVerificationCommandError,
        UnknownTrackerError,
        InvalidTrackerOptionsError,
        TrackerRequiresFreshRepoError,
    ) as exc:
        typer.echo(_terminal_safe(str(exc)), err=True)
        raise typer.Exit(2) from exc
    except InitConflictError as exc:
        typer.echo("Initialization aborted: target files already exist.", err=True)
        typer.echo("Review conflicts; --force only overwrites non-managed files.", err=True)
        typer.echo(_format_path_list(exc.conflicts), err=True)
        raise typer.Exit(1) from exc

    action = "DRY RUN" if result.dry_run else "Initialized"
    typer.echo(f"{action}: {result.template} in {result.target_root}")
    detected = ", ".join(result.detected_providers) or "none"
    mapping_note = (
        "reference (mixed providers)"
        if result.provider_mapping == REFERENCE_MAPPING
        else f"all roles on {result.provider_mapping}"
    )
    typer.echo(f"Provider mapping: {mapping_note}; provider CLIs found: {detected}")
    typer.echo(f"Tracker: {result.tracker}")
    typer.echo(f"Copy: {len(result.copied)} file(s)")
    typer.echo(f"Merge: {len(result.merged)} file(s)")
    if result.merged:
        typer.echo(_format_path_list(result.merged))
    if result.skipped:
        typer.echo(f"Skip identical: {len(result.skipped)} file(s)")
    if result.conflicts:
        typer.echo(f"Conflict: {len(result.conflicts)} file(s)")
        typer.echo(_format_path_list(result.conflicts))
        if result.dry_run:
            typer.echo(
                "Review conflicts; --force only overwrites non-managed files."
            )
    _echo_candidates(result.candidates, result.verification_command)
    if result.antigravity_detected:
        _echo_antigravity_permissions(result.verification_command)


def _echo_antigravity_permissions(verification_command: str | None) -> None:
    """Print the agy permission entries; init never touches the settings file."""
    commands = () if verification_command is None else (verification_command,)
    for line in format_antigravity_permissions(
        # init never writes interpreter_grants, so print the flag-off set.
        antigravity_allow_entries(commands, interpreter_grants=False),
        ANTIGRAVITY_REQUIRED_DENY,
    ):
        typer.echo(line)
    typer.echo(
        "Add these entries to that settings file yourself, then run "
        "`llm-handoff doctor` to check them."
    )


_UNSAFE_TERMINAL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _terminal_safe(text: str) -> str:
    """Escape control, format, surrogate, and line/paragraph separators.

    Detected candidates come from files in the target repository, which may be
    untrusted. ANSI/OSC sequences or bidi overrides could spoof the output, and
    a lone surrogate raises UnicodeEncodeError on a strict UTF-8 stream.
    """
    return "".join(
        ch.encode("unicode_escape").decode("ascii")
        if unicodedata.category(ch) in _UNSAFE_TERMINAL_CATEGORIES
        else ch
        for ch in text
    )


def _echo_candidates(
    candidates: tuple[Candidate, ...], verification_command: str | None
) -> None:
    if candidates:
        typer.echo(f"Candidate commands: {len(candidates)}")
        for candidate in candidates:
            command = _terminal_safe(candidate.command)
            source = _terminal_safe(candidate.source)
            typer.echo(f"  - {command}  (from {source})")
    else:
        typer.echo("Candidate commands: none found")
    if verification_command is not None:
        executable = verification_executable(verification_command)
        if is_interpreter_executable(executable):
            typer.echo(
                f"Verification command: {_terminal_safe(verification_command)}; "
                f"no role gets a {executable} grant. The dispatcher gate runs "
                "this command itself. Roles can run it only after you set "
                "interpreter_grants: true in dispatch_config.yaml, which lets "
                "an agent run any command, git push included."
            )
        else:
            typer.echo(
                f"Verification command: {_terminal_safe(verification_command)}; "
                f"allowed_tools adds Bash({_terminal_safe(executable)}:*) "
                "for claude roles"
            )
    elif candidates:
        typer.echo(
            "No verification command selected; rerun with "
            '--verification-command "<cmd>" to allow its executable.'
        )


@app.command("doctor")
def doctor_command(
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        help="Repository root to check. Defaults to the repo containing the CWD.",
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Path to dispatch_config.yaml. Relative paths resolve from the repo root.",
    ),
    probe_tracker: str | None = typer.Option(
        None,
        "--probe-tracker",
        metavar="ISSUE",
        help=(
            "Linear mode only: launch the Claude CLI once with Linear read "
            "tools to read this issue (for example ENG-123) and the team's "
            "workflow states, and confirm the configured state names."
        ),
    ),
) -> None:
    """Check Git, provider CLIs, sandbox runtime, and the handoff file."""
    _configure_stdio_encoding()
    report = run_doctor(
        repo_root=repo_root,
        config_path=config_path,
        probe_tracker_issue=probe_tracker,
    )
    for line in report.lines():
        typer.echo(line)
    raise typer.Exit(report.exit_code)


def _read_lock_with_pid(
    root: Path, *, attempts: int = 10, delay_seconds: float = 0.1
) -> DispatchLockInfo | None:
    # A starting dispatcher creates the lock file before it writes its pid.
    lock = read_dispatch_lock(root)
    for _ in range(attempts):
        if lock is None or lock.pid is not None:
            break
        time.sleep(delay_seconds)
        lock = read_dispatch_lock(root)
    return lock


@app.command("stop")
def stop_command(
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        help="Repository root of the running dispatcher. Defaults to the repo containing the CWD.",
    ),
) -> None:
    """Ask the running dispatcher to exit after its current cycle."""
    _configure_stdio_encoding()
    root = detect_repo_root(repo_root)
    lock = _read_lock_with_pid(root)
    if lock is None:
        typer.echo(
            f"No llm-handoff dispatcher is running for {root} "
            f"(no {DISPATCH_LOCK_FILENAME}). Nothing to stop.",
            err=True,
        )
        raise typer.Exit(1)
    if lock.pid is None:
        typer.echo(
            f"No stop request written. {describe_lock_holder(lock)} Check "
            "whether a dispatcher is running for this repo.",
            err=True,
        )
        raise typer.Exit(1)
    if not process_is_running(lock.pid):
        typer.echo(
            f"No stop request written. {describe_lock_holder(lock)} Remove "
            f"{lock.path} only after confirming no dispatcher is active.",
            err=True,
        )
        raise typer.Exit(1)
    if not stop_request_is_ignored(root):
        typer.echo(
            "No stop request written: Git does not ignore "
            f"{stop_request_path(root)}, so the running dispatch's work-claim "
            "gate would report it as an uncommitted file. Add /logs/dispatch/ "
            "to .gitignore (as `llm-handoff init` does), or stop the "
            "dispatcher at a pause.",
            err=True,
        )
        raise typer.Exit(1)
    path = request_stop(root, target_pid=lock.pid)
    typer.echo(
        f"Stop requested: wrote {path}. The dispatcher (pid {lock.pid}) finishes its "
        "current cycle, including a running agent and the dispatcher commit, "
        "then exits with code 0 before the next dispatch. Delete the file "
        "to cancel."
    )


@app.callback(invoke_without_command=True)
def _dispatch_callback(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run"),
    use_manual_frontend: bool = typer.Option(
        False,
        "--manual-frontend",
        help="Pause for manual frontend work instead of launching the frontend CLI.",
    ),
    removed_planner_api_key_env: bool = typer.Option(
        False,
        "--use-planner-api-key-env",
        hidden=True,
        help="Removed with the gemini provider; passing it exits with code 2.",
    ),
    backend_resume: bool = typer.Option(
        True,
        "--use-backend-resume/--no-backend-resume",
        help=(
            "Reuse the managed backend session, or start a fresh stateless backend "
            "session for this dispatch."
        ),
    ),
    planner_resume: bool = typer.Option(
        True,
        "--use-planner-resume/--no-planner-resume",
        help=(
            "Reuse the in-memory managed planner session, or run the planner "
            "statelessly for this dispatch."
        ),
    ),
    no_interactive_pause: bool = typer.Option(
        False,
        "--no-interactive-pause",
        help=(
            "Do not show the operator prompt at pauses; wait for a HANDOFF.md "
            "edit instead."
        ),
    ),
    repo_root: Path | None = typer.Option(None, "--repo-root"),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Path to dispatch_config.yaml. Relative paths resolve from the repo root.",
    ),
) -> None:
    if removed_planner_api_key_env:
        _configure_stdio_encoding()
        typer.echo(_PLANNER_API_KEY_ENV_FLAG_REMOVED_MESSAGE, err=True)
        raise typer.Exit(2)
    if ctx.invoked_subcommand is not None:
        return
    _configure_stdio_encoding()
    raise typer.Exit(
        _run_dispatch(
            dry_run=dry_run,
            use_manual_frontend=use_manual_frontend,
            backend_resume=backend_resume,
            planner_resume=planner_resume,
            repo_root=repo_root,
            config_path=config_path,
            no_interactive_pause=no_interactive_pause,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    command = typer.main.get_command(app)
    try:
        result = command.main(
            args=list(argv) if argv is not None else None,
            prog_name="python -m llm_handoff",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
