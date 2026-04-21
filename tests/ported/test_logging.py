from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import io
from pathlib import Path

import pytest


FIXED_NOW = datetime(
    2026,
    4,
    17,
    12,
    34,
    56,
    tzinfo=timezone(timedelta(hours=-7)),
)


def _load_logging_module():
    return importlib.import_module("llm_handoff.logging_util")


def _build_logger(
    logging_util,
    repo_root: Path,
    *,
    console: io.StringIO | None = None,
    use_codex_resume: bool = True,
    use_gemini_resume: bool = True,
):
    return logging_util.DispatchLogger(
        repo_root=repo_root,
        console=console,
        now=lambda: FIXED_NOW,
        use_codex_resume=use_codex_resume,
        use_gemini_resume=use_gemini_resume,
    )


def _strip_ansi(text: str) -> str:
    for color_code in (
        "\x1b[90m",
        "\x1b[91m",
        "\x1b[92m",
        "\x1b[93m",
        "\x1b[95m",
        "\x1b[96m",
        "\x1b[97m",
        "\x1b[0m",
    ):
        text = text.replace(color_code, "")
    return text


def test_dispatch_logger_initializes_log_file_with_expected_header(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    logger = _build_logger(logging_util, repo_root)

    expected_path = (
        repo_root / "logs" / "dispatch" / "dispatch-v2-2026-04-17T123456.log"
    )
    assert logger.log_file_path == expected_path

    assert expected_path.read_text(encoding="utf-8") == (
        "# ============================================================================\n"
        "# llm-handoff Dispatch Log (single-dispatch-per-cycle)\n"
        "# ============================================================================\n"
        "# Started:        2026-04-17 12:34:56 -07:00\n"
        f"# Repo root:      {repo_root.resolve()}\n"
        "# Codex session:  MANAGED RESUME (persisted thread id)\n"
        "# Gemini PE:      MANAGED RESUME (in-memory session id)\n"
        "# Smart router:   ON (always)\n"
        "# Validate HOs:   ON (hard gate)\n"
        "# Auto ledger:    ON (always)\n"
        "# Chaining:       NONE (single dispatch per cycle, HANDOFF.md routes next)\n"
        "# Max failures:   3\n"
        "# ============================================================================\n"
        "\n"
    )


def test_dispatch_logger_header_reflects_stateless_codex_opt_out_mode(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    logger = _build_logger(logging_util, repo_root, use_codex_resume=False)

    assert logger.log_file_path.read_text(encoding="utf-8").startswith(
        "# ============================================================================\n"
        "# llm-handoff Dispatch Log (single-dispatch-per-cycle)\n"
        "# ============================================================================\n"
        "# Started:        2026-04-17 12:34:56 -07:00\n"
        f"# Repo root:      {repo_root.resolve()}\n"
        "# Codex session:  STATELESS (new session per dispatch)\n"
        "# Gemini PE:      MANAGED RESUME (in-memory session id)\n"
    )


def test_dispatch_logger_header_reflects_stateless_gemini_opt_out_mode(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    logger = _build_logger(logging_util, repo_root, use_gemini_resume=False)

    assert logger.log_file_path.read_text(encoding="utf-8").startswith(
        "# ============================================================================\n"
        "# llm-handoff Dispatch Log (single-dispatch-per-cycle)\n"
        "# ============================================================================\n"
        "# Started:        2026-04-17 12:34:56 -07:00\n"
        f"# Repo root:      {repo_root.resolve()}\n"
        "# Codex session:  MANAGED RESUME (persisted thread id)\n"
        "# Gemini PE:      STATELESS (new session per dispatch)\n"
    )


@pytest.mark.parametrize(
    ("method_name", "level", "color_code", "expect_console"),
    [
        pytest.param("warn", "WARN", "\x1b[93m", True, id="warn"),
        pytest.param("error", "ERROR", "\x1b[91m", True, id="error"),
        pytest.param("dispatch", "DISPATCH", "\x1b[96m", True, id="dispatch"),
        pytest.param("pause", "PAUSE", "\x1b[95m", True, id="pause"),
        pytest.param("agent", "AGENT", "\x1b[92m", True, id="agent"),
    ],
)
def test_dispatch_logger_routes_non_startup_messages_to_console_and_log_per_level(
    tmp_path: Path,
    method_name: str,
    level: str,
    color_code: str,
    expect_console: bool,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    logger = _build_logger(logging_util, repo_root, console=console)
    logger.mark_startup_complete()

    getattr(logger, method_name)("Message body")

    expected_line = f"[2026-04-17T12:34:56] [{level}] Message body"
    if expect_console:
        assert console.getvalue() == f"{color_code}{expected_line}\x1b[0m\n"
    else:
        assert console.getvalue() == ""
    assert logger.log_file_path.read_text(encoding="utf-8").endswith(
        expected_line + "\n"
    )


@pytest.mark.parametrize(
    ("message", "expect_console"),
    [
        pytest.param("--- Cycle 1 ---", True, id="cycle-start"),
        pytest.param("--- End of cycle 1 ---", True, id="cycle-end"),
        pytest.param("Routing instruction: Codex", True, id="routing"),
        pytest.param("Codex exited with code 0", True, id="exit-code"),
        pytest.param(
            "Codex updated C:\\repo\\docs\\handoff\\HANDOFF.md (hash changed)",
            True,
            id="handoff-update",
        ),
        pytest.param(
            "New SHA(s) found in handoff file: 9482fcf8cb0e... (1 added)",
            True,
            id="new-sha",
        ),
        pytest.param(
            "Steady-state detail that should stay file-only", False, id="other-info"
        ),
    ],
)
def test_dispatch_logger_routes_selected_progress_info_to_console(
    tmp_path: Path,
    message: str,
    expect_console: bool,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    logger = _build_logger(logging_util, repo_root, console=console)
    logger.mark_startup_complete()

    logger.info(message)

    expected_line = f"[2026-04-17T12:34:56] [INFO] {message}"
    if expect_console:
        assert console.getvalue() == f"\x1b[97m{expected_line}\x1b[0m\n"
    else:
        assert console.getvalue() == ""
    assert logger.log_file_path.read_text(encoding="utf-8").endswith(
        expected_line + "\n"
    )


def test_dispatch_logger_routes_startup_info_to_console_and_log(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    logger = _build_logger(logging_util, repo_root, console=console)

    logger.info("Repo root:          C:\\repo")

    expected_line = "[2026-04-17T12:34:56] [INFO] Repo root:          C:\\repo"
    assert console.getvalue() == f"\x1b[97m{expected_line}\x1b[0m\n"
    assert logger.log_file_path.read_text(encoding="utf-8").endswith(
        expected_line + "\n"
    )


def test_dispatch_logger_routes_non_startup_info_to_log_only(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    logger = _build_logger(logging_util, repo_root, console=console)
    logger.mark_startup_complete()

    logger.info("Steady-state detail that should stay file-only")

    expected_line = (
        "[2026-04-17T12:34:56] [INFO] Steady-state detail that should stay file-only"
    )
    assert console.getvalue() == ""
    assert logger.log_file_path.read_text(encoding="utf-8").endswith(
        expected_line + "\n"
    )


def test_dispatch_logger_warns_and_keeps_console_logging_on_file_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    logger = _build_logger(logging_util, repo_root, console=console)

    original_open = logging_util.Path.open

    def failing_open(path: Path, mode: str = "r", *args, **kwargs):
        if path == logger.log_file_path and "a" in mode:
            raise OSError("disk full")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(logging_util.Path, "open", failing_open)

    logger.mark_startup_complete()
    logger.info("Primary message")

    assert console.getvalue() == (
        "\x1b[93m[2026-04-17T12:34:56] [WARN] Failed to write to log file: disk full\x1b[0m\n"
    )
    assert "Primary message" not in logger.log_file_path.read_text(encoding="utf-8")


def test_dispatch_logger_warns_when_log_directory_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    expected_log_dir = repo_root / "logs" / "dispatch"

    original_mkdir = logging_util.Path.mkdir

    def failing_mkdir(path: Path, *args, **kwargs):
        if path == expected_log_dir:
            raise OSError("permission denied")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(logging_util.Path, "mkdir", failing_mkdir)

    logger = _build_logger(logging_util, repo_root, console=console)
    logger.mark_startup_complete()
    logger.info("Console only message")

    assert logger.log_file_path is None
    assert console.getvalue() == (
        "\x1b[93m[WARN] Could not create log directory: "
        f"{expected_log_dir} -- permission denied\x1b[0m\n"
        "\x1b[93m[WARN] Continuing with terminal-only logging.\x1b[0m\n"
        "\x1b[97m[2026-04-17T12:34:56] [INFO] Console only message\x1b[0m\n"
    )
    assert not expected_log_dir.exists()


def test_dispatch_logger_warns_when_log_file_initialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    expected_path = (
        repo_root / "logs" / "dispatch" / "dispatch-v2-2026-04-17T123456.log"
    )

    original_open = logging_util.Path.open

    def failing_open(path: Path, mode: str = "r", *args, **kwargs):
        if path == expected_path and "w" in mode:
            raise OSError("file locked")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(logging_util.Path, "open", failing_open)

    logger = _build_logger(logging_util, repo_root, console=console)
    logger.mark_startup_complete()
    logger.info("Console fallback message")

    assert logger.log_file_path is None
    assert console.getvalue() == (
        "\x1b[93m[WARN] Failed to initialize log file: file locked\x1b[0m\n"
        "\x1b[93m[WARN] Continuing with terminal-only logging.\x1b[0m\n"
        "\x1b[97m[2026-04-17T12:34:56] [INFO] Console fallback message\x1b[0m\n"
    )
    assert not expected_path.exists()


def test_dispatch_logger_is_callable_and_marks_startup_complete(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    logger = _build_logger(logging_util, repo_root, console=console)

    logger("INFO", "Startup line")
    logger.mark_startup_complete()
    logger("INFO", "Steady-state line")

    assert _strip_ansi(console.getvalue()) == (
        "[2026-04-17T12:34:56] [INFO] Startup line\n"
    )
    assert logger.log_file_path.read_text(encoding="utf-8").endswith(
        "[2026-04-17T12:34:56] [INFO] Startup line\n"
        "[2026-04-17T12:34:56] [INFO] Steady-state line\n"
    )
