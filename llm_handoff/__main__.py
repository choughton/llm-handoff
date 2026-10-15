from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import sys
from typing import Sequence
import unicodedata

import click
import typer

from llm_handoff.agent_providers.codex import (
    _cleanup_codex_output_artifacts,
    _codex_artifact_paths,
)
from llm_handoff.config import (
    DISPATCH_WINDOW_TITLE,
    detect_repo_root,
    load_dispatch_config,
)
from llm_handoff.doctor import run_doctor
from llm_handoff.init_workflow import (
    REFERENCE_MAPPING,
    InitConflictError,
    InvalidVerificationCommandError,
    UnknownProviderError,
    UnknownTemplateError,
    init_reference_workflow,
    verification_executable,
)
from llm_handoff.logging_util import DispatchLogger
from llm_handoff.stack_detect import Candidate
from llm_handoff.orchestrator import run_loop

_WINDOWS = os.name == "nt"
DISPATCH_LOCK_FILENAME = ".dispatch.lock"
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


def _run_dispatch(
    *,
    dry_run: bool,
    use_manual_frontend: bool,
    planner_api_key_env: bool = False,
    backend_resume: bool = True,
    planner_resume: bool = True,
    repo_root: Path | None = None,
    config_path: Path | None = None,
) -> int:
    root_start = repo_root
    if root_start is None and config_path is not None:
        root_start = config_path.parent
    config = load_dispatch_config(
        repo_root=detect_repo_root(root_start),
        config_path=config_path,
        dry_run=dry_run,
        use_manual_frontend=use_manual_frontend,
        planner_api_key_env=planner_api_key_env,
        backend_resume=backend_resume,
        planner_resume=planner_resume,
    )
    lock = _try_acquire_dispatch_lock(config.repo_root)
    if lock is None:
        lock_path = _dispatch_lock_path(config.repo_root)
        print(
            "Another llm-handoff dispatcher appears to be running for this repo. "
            f"Remove {lock_path} only after confirming no dispatcher is active.",
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
        previous_title, changed_title = _set_dispatch_console_title()
        try:
            return run_loop(config, log=logger)
        except KeyboardInterrupt:
            logger("WARN", "Dispatch interrupted by user. Exiting.")
            return 130
        finally:
            _restore_console_title(previous_title, changed=changed_title)
    finally:
        _release_dispatch_lock(lock)


def _dispatch_lock_path(repo_root: Path) -> Path:
    return Path(repo_root).resolve() / DISPATCH_LOCK_FILENAME


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
            "Put every role on one provider CLI (currently: claude). "
            "Default: claude when Claude Code is the only provider CLI on "
            "PATH, otherwise the reference mapping."
        ),
    ),
    verification_command: str | None = typer.Option(
        None,
        "--verification-command",
        help=(
            "Confirmed verification command; its executable is added to "
            "Claude roles' allowed_tools."
        ),
    ),
) -> None:
    _configure_stdio_encoding()
    try:
        result = init_reference_workflow(
            target_root,
            template=template,
            dry_run=dry_run,
            force=force,
            provider=provider,
            verification_command=verification_command,
        )
    except (
        UnknownTemplateError,
        UnknownProviderError,
        InvalidVerificationCommandError,
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
) -> None:
    """Check Git, provider CLIs, sandbox runtime, and the handoff file."""
    _configure_stdio_encoding()
    report = run_doctor(repo_root=repo_root, config_path=config_path)
    for line in report.lines():
        typer.echo(line)
    raise typer.Exit(report.exit_code)


@app.callback(invoke_without_command=True)
def _dispatch_callback(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run"),
    use_manual_frontend: bool = typer.Option(
        False,
        "--manual-frontend",
        help="Pause for manual frontend work instead of launching the frontend CLI.",
    ),
    planner_api_key_env: bool = typer.Option(
        False,
        "--use-planner-api-key-env",
        help=(
            "Preserve GEMINI_API_KEY for planner launches. GOOGLE_API_KEY is still stripped."
        ),
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
    repo_root: Path | None = typer.Option(None, "--repo-root"),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Path to dispatch_config.yaml. Relative paths resolve from the repo root.",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _configure_stdio_encoding()
    raise typer.Exit(
        _run_dispatch(
            dry_run=dry_run,
            use_manual_frontend=use_manual_frontend,
            planner_api_key_env=planner_api_key_env,
            backend_resume=backend_resume,
            planner_resume=planner_resume,
            repo_root=repo_root,
            config_path=config_path,
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
