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
    backend_resume: bool = True,
    planner_resume: bool = True,
):
    return logging_util.DispatchLogger(
        repo_root=repo_root,
        console=console,
        now=lambda: FIXED_NOW,
        backend_resume=backend_resume,
        planner_resume=planner_resume,
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
        "# Backend session: MANAGED RESUME (persisted thread id)\n"
        "# Planner session: MANAGED RESUME (in-memory session id)\n"
        "# Smart router:   ON (always)\n"
        "# Validate HOs:   ON (hard gate)\n"
        "# Auto ledger:    ON (always)\n"
        "# Chaining:       NONE (single dispatch per cycle, HANDOFF.md routes next)\n"
        "# Max failures:   3\n"
        "# ============================================================================\n"
        "\n"
    )


def test_dispatch_logger_header_reflects_stateless_backend_opt_out_mode(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    logger = _build_logger(logging_util, repo_root, backend_resume=False)

    assert logger.log_file_path.read_text(encoding="utf-8").startswith(
        "# ============================================================================\n"
        "# llm-handoff Dispatch Log (single-dispatch-per-cycle)\n"
        "# ============================================================================\n"
        "# Started:        2026-04-17 12:34:56 -07:00\n"
        f"# Repo root:      {repo_root.resolve()}\n"
        "# Backend session: STATELESS (new session per dispatch)\n"
        "# Planner session: MANAGED RESUME (in-memory session id)\n"
    )


def test_dispatch_logger_header_reflects_stateless_planner_opt_out_mode(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    logger = _build_logger(logging_util, repo_root, planner_resume=False)

    assert logger.log_file_path.read_text(encoding="utf-8").startswith(
        "# ============================================================================\n"
        "# llm-handoff Dispatch Log (single-dispatch-per-cycle)\n"
        "# ============================================================================\n"
        "# Started:        2026-04-17 12:34:56 -07:00\n"
        f"# Repo root:      {repo_root.resolve()}\n"
        "# Backend session: MANAGED RESUME (persisted thread id)\n"
        "# Planner session: STATELESS (new session per dispatch)\n"
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
        pytest.param(
            "work-claim: running scanner gitleaks: gitleaks git --no-banner .",
            True,
            id="work-claim-scanner-start",
        ),
        pytest.param(
            "work-claim: running verification command: python -m pytest tests -q",
            True,
            id="work-claim-verification-start",
        ),
        pytest.param(
            "work-claim: pre-dispatch HEAD 67e116c6; HEAD ead2ef23; scope_sha ead2ef23",
            True,
            id="work-claim-shas",
        ),
        pytest.param(
            "work-claim: changed files since dispatch: CHANGELOG.md, llm_handoff/x.py",
            True,
            id="work-claim-changed-files",
        ),
        pytest.param(
            "work-claim: PASS diff_scope: every changed file is in scope.",
            True,
            id="work-claim-check-pass",
        ),
        pytest.param(
            "work-claim: FAIL verification: `python -m pytest` exited 1 after 3.0s.",
            True,
            id="work-claim-check-fail",
        ),
        pytest.param(
            "work-claim: SKIP scanner bandit: not run; the scope checks did not pass.",
            True,
            id="work-claim-scanner-outcome",
        ),
        pytest.param(
            "work-claim: verification command `python -m pytest tests -q` exited 0 after 190.2s",
            True,
            id="work-claim-verification-outcome",
        ),
        pytest.param(
            "work-claim | 2674 passed, 20 skipped in 188.9s",
            False,
            id="work-claim-output-tail-stays-file-only",
        ),
        pytest.param(
            "work-claim | gitleaks | no leaks found",
            False,
            id="work-claim-scanner-tail-stays-file-only",
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


@pytest.mark.parametrize("level", ["AGENT", "INFO", "WARN"])
def test_dispatch_logger_escapes_terminal_control_characters_on_console_only(
    tmp_path: Path,
    level: str,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    logger = _build_logger(logging_util, repo_root, console=console)
    logger.mark_startup_complete()
    # An OSC title-set sequence, a bare ESC, a carriage return that could
    # overwrite the line, a bidi override, and a line feed that could forge
    # a second log line.
    spoof = "\x1b]0;owned\x07 \x1b[31mred \rPASS \u202eabc\n[fake] [AGENT] PASSED"
    message = f"work-claim: running verification command: python -c {spoof}"

    logger.log(level, message)

    console_text = console.getvalue()
    assert "\x1b]0;owned" not in console_text
    assert "\r" not in console_text
    assert "\u202e" not in console_text
    assert console_text.count("\n") == 1
    # Only the level color and the reset remain as real escape characters.
    assert console_text.count("\x1b") == 2
    assert (
        "python -c \\x1b]0;owned\\x07 \\x1b[31mred \\rPASS \\u202eabc\\n[fake]"
        in console_text
    )
    # Read bytes: text mode would translate the carriage return on read.
    file_text = logger.log_file_path.read_bytes().decode("utf-8")
    assert message in file_text


def test_terminal_safe_keeps_ordinary_unicode() -> None:
    logging_util = _load_logging_module()

    text = "work-claim: changed files since dispatch: docs/r\u00e9sum\u00e9.md"

    assert logging_util.terminal_safe(text) == text


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


def test_dispatch_logger_routes_non_startup_debug_to_log_only_without_chat_line(
    tmp_path: Path,
) -> None:
    logging_util = _load_logging_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    console = io.StringIO()
    logger = _build_logger(logging_util, repo_root, console=console)
    logger.mark_startup_complete()

    logger.log("DEBUG", "Steady-state debug detail")

    expected_line = "[2026-04-17T12:34:56] [DEBUG] Steady-state debug detail"
    assert logger.chat_line is None
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
