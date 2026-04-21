from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
from typing import Sequence

import click
import typer

from llm_handoff.agents import _cleanup_codex_output_artifacts, _codex_artifact_paths
from llm_handoff.config import (
    DISPATCH_WINDOW_TITLE,
    detect_repo_root,
    load_dispatch_config,
)
from llm_handoff.init_workflow import (
    InitConflictError,
    UnknownTemplateError,
    init_reference_workflow,
)
from llm_handoff.logging_util import DispatchLogger
from llm_handoff.orchestrator import run_loop

_WINDOWS = os.name == "nt"
app = typer.Typer(
    add_completion=False,
    help="llm-handoff dispatcher. llm-handoff dispatch loop.",
    invoke_without_command=True,
)


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
    use_gemini_api_key_env: bool,
    use_codex_resume: bool,
    use_gemini_resume: bool,
    repo_root: Path | None,
    config_path: Path | None,
) -> int:
    root_start = repo_root
    if root_start is None and config_path is not None:
        root_start = config_path.parent
    config = load_dispatch_config(
        repo_root=detect_repo_root(root_start),
        config_path=config_path,
        dry_run=dry_run,
        use_manual_frontend=use_manual_frontend,
        use_gemini_api_key_env=use_gemini_api_key_env,
        use_codex_resume=use_codex_resume,
        use_gemini_resume=use_gemini_resume,
    )
    _cleanup_codex_output_artifacts(_codex_artifact_paths(config.repo_root))
    logger = DispatchLogger(
        repo_root=config.repo_root,
        max_consecutive_failures=config.max_consecutive_failures,
        use_codex_resume=config.use_codex_resume,
        use_gemini_resume=config.use_gemini_resume,
    )
    previous_title, changed_title = _set_dispatch_console_title()
    try:
        return run_loop(config, log=logger)
    except KeyboardInterrupt:
        logger("WARN", "Dispatch interrupted by user. Exiting.")
        return 130
    finally:
        _restore_console_title(previous_title, changed=changed_title)


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
        help="Preview the files that would be copied without writing them.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "--overwrite",
        help="Overwrite existing files that differ from the template.",
    ),
) -> None:
    _configure_stdio_encoding()
    try:
        result = init_reference_workflow(
            target_root,
            template=template,
            dry_run=dry_run,
            force=force,
        )
    except UnknownTemplateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except InitConflictError as exc:
        typer.echo("Initialization aborted: target files already exist.", err=True)
        typer.echo("Use --force to overwrite them after reviewing the diff.", err=True)
        typer.echo(_format_path_list(exc.conflicts), err=True)
        raise typer.Exit(1) from exc

    action = "DRY RUN" if result.dry_run else "Initialized"
    typer.echo(f"{action}: {result.template} in {result.target_root}")
    typer.echo(f"Copy: {len(result.copied)} file(s)")
    if result.skipped:
        typer.echo(f"Skip identical: {len(result.skipped)} file(s)")
    if result.conflicts:
        typer.echo(f"Conflict: {len(result.conflicts)} file(s)")
        typer.echo(_format_path_list(result.conflicts))
        if result.dry_run:
            typer.echo(
                "Run without --dry-run to initialize, or use --force to overwrite."
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
    use_gemini_api_key_env: bool = typer.Option(
        False,
        "--use-gemini-api-key-env",
        help=(
            "Preserve GEMINI_API_KEY for Gemini CLI launches. "
            "GOOGLE_API_KEY is still stripped."
        ),
    ),
    # Typer exposes both forms from this option declaration. Keep the explicit
    # --use-codex-resume alias for older dispatch wrappers while the default is
    # resume-enabled and --no-codex-resume remains the operational opt-out.
    use_codex_resume: bool = typer.Option(
        True,
        "--use-codex-resume/--no-codex-resume",
        help=(
            "Reuse the managed Codex session, or start a fresh stateless Codex "
            "session for this dispatch."
        ),
    ),
    use_gemini_resume: bool = typer.Option(
        True,
        "--use-gemini-resume/--no-gemini-resume",
        help=(
            "Reuse the in-memory managed Gemini PE session, or run Gemini PE "
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
            use_gemini_api_key_env=use_gemini_api_key_env,
            use_codex_resume=use_codex_resume,
            use_gemini_resume=use_gemini_resume,
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
