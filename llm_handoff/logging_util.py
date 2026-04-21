from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import sys
from typing import Callable, Final, TextIO


DEFAULT_LOG_DIRECTORY: Final[Path] = Path("logs") / "dispatch"
RESET_COLOR: Final[str] = "\x1b[0m"
DEFAULT_MAX_CONSECUTIVE_FAILURES: Final[int] = 3
CONSOLE_ALWAYS_LEVELS: Final[frozenset[str]] = frozenset(
    {"WARN", "ERROR", "PAUSE", "DISPATCH", "AGENT"}
)
STARTUP_CONSOLE_LEVELS: Final[frozenset[str]] = frozenset({"INFO", "DEBUG"})
STEADY_STATE_INFO_CONSOLE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^--- Cycle \d+ ---$"),
    re.compile(r"^--- End of cycle \d+ ---$"),
    re.compile(r"^Routing instruction: "),
    re.compile(r"^[^:]+ exited with code \d+$"),
    re.compile(r"^[^:]+ updated .+HANDOFF\.md \(hash changed\)$"),
    re.compile(r"^New SHA\(s\) found in handoff file: "),
)

LEVEL_TO_COLOR: Final[dict[str, str]] = {
    "DEBUG": "\x1b[90m",
    "INFO": "\x1b[97m",
    "WARN": "\x1b[93m",
    "ERROR": "\x1b[91m",
    "DISPATCH": "\x1b[96m",
    "PAUSE": "\x1b[95m",
    "AGENT": "\x1b[92m",
}

LogClock = Callable[[], datetime]


class DispatchLogger:
    def __init__(
        self,
        repo_root: Path,
        *,
        console: TextIO | None = None,
        now: LogClock | None = None,
        log_directory: Path = DEFAULT_LOG_DIRECTORY,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        backend_resume: bool = True,
        planner_resume: bool = True,
        use_codex_resume: bool | None = None,
        use_gemini_resume: bool | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.console = console or sys.stdout
        self._now = now or datetime.now
        self.max_consecutive_failures = max_consecutive_failures
        if use_codex_resume is not None:
            backend_resume = use_codex_resume
        if use_gemini_resume is not None:
            planner_resume = use_gemini_resume
        self.backend_resume = backend_resume
        self.planner_resume = planner_resume
        self.use_codex_resume = backend_resume
        self.use_gemini_resume = planner_resume
        self.log_directory = self._resolve_log_directory(log_directory)
        self.log_file_path: Path | None = None
        self._startup_phase = True
        self._initialize_log_file()

    def info(self, message: str) -> None:
        self.log("INFO", message)

    def warn(self, message: str) -> None:
        self.log("WARN", message)

    def error(self, message: str) -> None:
        self.log("ERROR", message)

    def dispatch(self, message: str) -> None:
        self.log("DISPATCH", message)

    def pause(self, message: str) -> None:
        self.log("PAUSE", message)

    def agent(self, message: str) -> None:
        self.log("AGENT", message)

    def mark_startup_complete(self) -> None:
        self._startup_phase = False

    def __call__(self, level: str, message: str) -> None:
        self.log(level, message)

    def log(self, level: str, message: str) -> None:
        if level not in LEVEL_TO_COLOR:
            raise ValueError(f"Unsupported log level: {level}")

        timestamp = self._current_time()
        formatted_line = self._format_line(timestamp, level, message)
        if self._should_write_console(level, message):
            self._write_console(level, formatted_line)

        if self.log_file_path is None:
            return

        try:
            with self.log_file_path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(formatted_line + "\n")
        except OSError as exc:
            warning_line = self._format_line(
                timestamp,
                "WARN",
                f"Failed to write to log file: {exc}",
            )
            self._write_console("WARN", warning_line)

    def _initialize_log_file(self) -> None:
        try:
            self.log_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._write_console(
                "WARN",
                f"[WARN] Could not create log directory: {self.log_directory} -- {exc}",
            )
            self._write_console(
                "WARN",
                "[WARN] Continuing with terminal-only logging.",
            )
            return

        current_time = self._current_time()
        self.log_file_path = self.log_directory / (
            f"dispatch-v2-{current_time.strftime('%Y-%m-%dT%H%M%S')}.log"
        )

        try:
            with self.log_file_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(self._build_header(current_time))
        except OSError as exc:
            self.log_file_path = None
            self._write_console(
                "WARN",
                f"[WARN] Failed to initialize log file: {exc}",
            )
            self._write_console(
                "WARN",
                "[WARN] Continuing with terminal-only logging.",
            )

    def _resolve_log_directory(self, log_directory: Path) -> Path:
        if log_directory.is_absolute():
            return log_directory
        return self.repo_root / log_directory

    def _current_time(self) -> datetime:
        current_time = self._now()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            return current_time.astimezone()
        return current_time

    def _build_header(self, current_time: datetime) -> str:
        backend_session_mode = (
            "MANAGED RESUME (persisted thread id)"
            if self.backend_resume
            else "STATELESS (new session per dispatch)"
        )
        planner_session_mode = (
            "MANAGED RESUME (in-memory session id)"
            if self.planner_resume
            else "STATELESS (new session per dispatch)"
        )
        return (
            "# ============================================================================\n"
            "# llm-handoff Dispatch Log (single-dispatch-per-cycle)\n"
            "# ============================================================================\n"
            f"# Started:        {current_time.strftime('%Y-%m-%d %H:%M:%S %z')[:-2]}:{current_time.strftime('%z')[-2:]}\n"
            f"# Repo root:      {self.repo_root}\n"
            f"# Backend session: {backend_session_mode}\n"
            f"# Planner session: {planner_session_mode}\n"
            "# Smart router:   ON (always)\n"
            "# Validate HOs:   ON (hard gate)\n"
            "# Auto ledger:    ON (always)\n"
            "# Chaining:       NONE (single dispatch per cycle, HANDOFF.md routes next)\n"
            f"# Max failures:   {self.max_consecutive_failures}\n"
            "# ============================================================================\n"
            "\n"
        )

    def _format_line(
        self,
        current_time: datetime,
        level: str,
        message: str,
    ) -> str:
        return f"[{current_time.strftime('%Y-%m-%dT%H:%M:%S')}] [{level}] {message}"

    def _write_console(self, level: str, text: str) -> None:
        color = LEVEL_TO_COLOR[level]
        self.console.write(f"{color}{text}{RESET_COLOR}\n")
        flush = getattr(self.console, "flush", None)
        if callable(flush):
            flush()

    def _should_write_console(self, level: str, message: str) -> bool:
        if self.log_file_path is None:
            return True
        if level in CONSOLE_ALWAYS_LEVELS:
            return True
        if self._startup_phase and level in STARTUP_CONSOLE_LEVELS:
            return True
        return level == "INFO" and any(
            pattern.search(message) for pattern in STEADY_STATE_INFO_CONSOLE_PATTERNS
        )


__all__ = ["DispatchLogger", "DEFAULT_LOG_DIRECTORY", "LEVEL_TO_COLOR"]
