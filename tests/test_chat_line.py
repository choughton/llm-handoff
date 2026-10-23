from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path

import pytest

from llm_handoff.chat_line import ChatLine, display_width
from llm_handoff.logging_util import LEVEL_TO_COLOR, RESET_COLOR, DispatchLogger


ERASE = "\r\x1b[2K"
FIXED_NOW = datetime(2026, 4, 17, 12, 34, 56, tzinfo=timezone(timedelta(hours=-7)))


def _chat_line(columns: int = 120) -> tuple[ChatLine, io.StringIO]:
    stream = io.StringIO()
    return ChatLine(stream, terminal_columns=lambda: columns), stream


def test_prompt_text_with_role_and_buffer() -> None:
    chat_line, _ = _chat_line()
    chat_line.set_role("backend")
    chat_line.set_buffer("hello")

    assert chat_line.prompt_text() == "[backend running] chat> hello"


def test_prompt_text_without_role_is_empty() -> None:
    chat_line, stream = _chat_line()
    chat_line.set_buffer("ignored")

    assert chat_line.prompt_text() == ""
    assert stream.getvalue() == ""


class _CountingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def test_prompt_text_flushes_stream_with_and_without_role() -> None:
    stream = _CountingStream()
    chat_line = ChatLine(stream, terminal_columns=lambda: 120)

    before = stream.flush_count
    assert chat_line.prompt_text() == ""
    assert stream.flush_count == before + 1

    chat_line.set_role("backend")
    before = stream.flush_count
    assert chat_line.prompt_text() == "[backend running] chat> "
    assert stream.flush_count == before + 1


def test_every_public_method_flushes_stream() -> None:
    stream = _CountingStream()
    chat_line = ChatLine(stream, terminal_columns=lambda: 120)
    calls = [
        lambda: chat_line.set_role("backend"),
        lambda: chat_line.set_buffer("hi"),
        chat_line.prompt_text,
        lambda: chat_line.write_above("line"),
        chat_line.clear,
        chat_line.redraw,
        lambda: chat_line.set_role(None),
    ]
    for call in calls:
        before = stream.flush_count
        call()
        assert stream.flush_count == before + 1


def test_write_above_without_role_writes_plain_line() -> None:
    chat_line, stream = _chat_line()

    chat_line.write_above("log line")

    assert stream.getvalue() == "log line\n"
    assert "\x1b" not in stream.getvalue()
    assert chat_line.drawn is False


def test_write_above_with_role_erases_writes_and_redraws() -> None:
    chat_line, stream = _chat_line()
    chat_line.set_role("backend")

    chat_line.write_above("log line")

    prompt = "[backend running] chat> "
    assert stream.getvalue() == prompt + ERASE + "log line\n" + prompt
    assert chat_line.drawn is True


def test_set_buffer_redraws_prompt() -> None:
    chat_line, stream = _chat_line()
    chat_line.set_role("auditor")

    chat_line.set_buffer("hi")

    assert stream.getvalue() == (
        "[auditor running] chat> " + ERASE + "[auditor running] chat> hi"
    )


def test_clear_when_drawn_erases_and_keeps_state() -> None:
    chat_line, stream = _chat_line()
    chat_line.set_role("backend")
    chat_line.set_buffer("abc")
    before = stream.getvalue()

    chat_line.clear()

    assert stream.getvalue() == before + ERASE
    assert chat_line.drawn is False
    assert chat_line.role == "backend"
    assert chat_line.buffer == "abc"


def test_clear_when_not_drawn_writes_nothing() -> None:
    chat_line, stream = _chat_line()

    chat_line.clear()

    assert stream.getvalue() == ""


def test_redraw_after_clear_draws_prompt_again() -> None:
    chat_line, stream = _chat_line()
    chat_line.set_role("backend")
    chat_line.clear()
    before = stream.getvalue()

    chat_line.redraw()

    assert stream.getvalue() == before + "[backend running] chat> "
    assert chat_line.drawn is True


def test_set_role_none_erases_line() -> None:
    chat_line, stream = _chat_line()
    chat_line.set_role("backend")

    chat_line.set_role(None)

    assert stream.getvalue() == "[backend running] chat> " + ERASE
    assert chat_line.drawn is False

    chat_line.write_above("after")
    assert stream.getvalue().endswith(ERASE + "after\n")


def test_buffer_is_cut_from_the_left_at_narrow_width() -> None:
    chat_line, stream = _chat_line(columns=30)
    chat_line.set_role("qa")
    prefix = "[qa running] chat> "
    chat_line.clear()
    stream.seek(0)
    stream.truncate()

    chat_line.set_buffer("abcdefghijklmnopqrstuvwxyz")

    drawn = stream.getvalue()
    assert len(drawn) == 29
    assert drawn == prefix + "qrstuvwxyz"


def test_prefix_is_cut_when_it_does_not_fit() -> None:
    chat_line, stream = _chat_line(columns=10)

    chat_line.set_role("backend")

    assert stream.getvalue() == "[backend "


def test_wide_buffer_characters_are_measured_in_terminal_cells() -> None:
    chat_line, stream = _chat_line(columns=30)
    prefix = "[qa running] chat> "
    chat_line.set_role("qa")
    chat_line.clear()
    stream.seek(0)
    stream.truncate()

    # Each CJK character takes two cells: 19 prefix cells leave 10 cells.
    chat_line.set_buffer("一二三四五六七八")

    drawn = stream.getvalue()
    assert drawn == prefix + "四五六七八"
    assert display_width(drawn) == 29


def test_wide_character_that_does_not_fit_whole_is_dropped() -> None:
    chat_line, stream = _chat_line(columns=30)
    prefix = "[qa running] chat> "
    chat_line.set_role("qa")
    chat_line.clear()
    stream.seek(0)
    stream.truncate()

    # 10 cells available: "a" + four wide characters is 9 cells, and the
    # next wide character would need 2 more, so it is dropped.
    chat_line.set_buffer("一二a三四五六")

    drawn = stream.getvalue()
    assert drawn == prefix + "a三四五六"
    assert display_width(drawn) <= 29


def test_emoji_buffer_is_measured_in_terminal_cells() -> None:
    chat_line, stream = _chat_line(columns=30)
    chat_line.set_role("qa")

    chat_line.set_buffer("\U0001f600" * 20)

    drawn = stream.getvalue().rsplit(ERASE, 1)[-1]
    assert drawn == "[qa running] chat> " + "\U0001f600" * 5
    assert display_width(drawn) == 29


def test_cut_buffer_does_not_start_with_combining_mark() -> None:
    chat_line, stream = _chat_line(columns=21)
    chat_line.set_role("qa")

    # 19 prefix cells leave 1: the last "e" + combining acute fits, and the
    # cut drops the orphaned combining mark before it.
    chat_line.set_buffer("xéé")

    drawn = stream.getvalue().rsplit(ERASE, 1)[-1]
    assert drawn == "[qa running] chat> é"


def test_display_width_counts_cells() -> None:
    assert display_width("abc") == 3
    assert display_width("一") == 2
    assert display_width("\U0001f600") == 2
    assert display_width("é") == 1


def test_terminal_columns_oserror_falls_back_to_80() -> None:
    def broken() -> int:
        raise OSError("no terminal")

    stream = io.StringIO()
    chat_line = ChatLine(stream, terminal_columns=broken)
    chat_line.set_role("backend")
    stream.seek(0)
    stream.truncate()
    chat_line.drawn = False

    chat_line.set_buffer("x" * 200)

    drawn = stream.getvalue()
    assert len(drawn) == 79
    assert drawn.startswith("[backend running] chat> x")


def test_tiny_terminal_columns_falls_back_to_80() -> None:
    chat_line, stream = _chat_line(columns=1)
    chat_line.set_role("backend")
    stream.seek(0)
    stream.truncate()
    chat_line.drawn = False

    chat_line.set_buffer("y" * 200)

    assert len(stream.getvalue()) == 79


def test_buffer_escape_character_is_made_terminal_safe() -> None:
    chat_line, stream = _chat_line()
    chat_line.set_role("backend")

    chat_line.set_buffer("a\x1b[31mb")

    assert chat_line.prompt_text() == "[backend running] chat> a\\x1b[31mb"
    assert "\x1b[31m" not in stream.getvalue()


def test_dispatch_logger_writes_through_attached_chat_line(tmp_path: Path) -> None:
    console = io.StringIO()
    chat_stream = io.StringIO()
    chat_line = ChatLine(chat_stream, terminal_columns=lambda: 120)
    logger = DispatchLogger(
        repo_root=tmp_path,
        console=console,
        now=lambda: FIXED_NOW,
        chat_line=chat_line,
    )
    chat_line.set_role("backend")
    chat_stream.seek(0)
    chat_stream.truncate()

    logger.warn("careful")

    expected_line = (
        f"{LEVEL_TO_COLOR['WARN']}[2026-04-17T12:34:56] [WARN] careful{RESET_COLOR}"
    )
    assert console.getvalue() == ""
    assert chat_stream.getvalue() == (
        ERASE + expected_line + "\n" + "[backend running] chat> "
    )


def test_dispatch_logger_attach_and_detach_chat_line(tmp_path: Path) -> None:
    console = io.StringIO()
    chat_stream = io.StringIO()
    logger = DispatchLogger(repo_root=tmp_path, console=console, now=lambda: FIXED_NOW)
    assert logger.chat_line is None

    chat_line = ChatLine(chat_stream, terminal_columns=lambda: 120)
    logger.attach_chat_line(chat_line)
    logger.warn("through chat")
    assert console.getvalue() == ""
    assert "through chat" in chat_stream.getvalue()

    logger.attach_chat_line(None)
    logger.warn("direct")
    expected_line = (
        f"{LEVEL_TO_COLOR['WARN']}[2026-04-17T12:34:56] [WARN] direct{RESET_COLOR}\n"
    )
    assert console.getvalue() == expected_line


def test_detaching_chat_line_clears_drawn_prompt(tmp_path: Path) -> None:
    stream = io.StringIO()
    chat_line = ChatLine(stream, terminal_columns=lambda: 120)
    logger = DispatchLogger(
        repo_root=tmp_path,
        console=stream,
        now=lambda: FIXED_NOW,
        chat_line=chat_line,
    )
    chat_line.set_role("backend")

    logger.attach_chat_line(None)
    logger.warn("direct")

    expected_line = (
        f"{LEVEL_TO_COLOR['WARN']}[2026-04-17T12:34:56] [WARN] direct{RESET_COLOR}\n"
    )
    assert stream.getvalue() == "[backend running] chat> " + ERASE + expected_line
    assert chat_line.drawn is False
    assert chat_line.role == "backend"


def test_replacing_chat_line_clears_previous_prompt(tmp_path: Path) -> None:
    stream = io.StringIO()
    old_line = ChatLine(stream, terminal_columns=lambda: 120)
    new_line = ChatLine(stream, terminal_columns=lambda: 120)
    logger = DispatchLogger(
        repo_root=tmp_path,
        console=io.StringIO(),
        now=lambda: FIXED_NOW,
        chat_line=old_line,
    )
    old_line.set_role("backend")

    logger.attach_chat_line(new_line)

    assert stream.getvalue() == "[backend running] chat> " + ERASE
    assert old_line.drawn is False


def _attached_steady_state_logger(
    tmp_path: Path,
) -> tuple[DispatchLogger, ChatLine, io.StringIO, io.StringIO]:
    console = io.StringIO()
    chat_stream = io.StringIO()
    chat_line = ChatLine(chat_stream, terminal_columns=lambda: 120)
    logger = DispatchLogger(
        repo_root=tmp_path,
        console=console,
        now=lambda: FIXED_NOW,
    )
    logger.mark_startup_complete()
    logger.attach_chat_line(chat_line)
    chat_line.set_role("auditor")
    chat_line.set_buffer("half typed")
    chat_stream.seek(0)
    chat_stream.truncate()
    return logger, chat_line, chat_stream, console


AUDITOR_PROMPT = "[auditor running] chat> half typed"


@pytest.mark.parametrize("level", sorted(LEVEL_TO_COLOR))
def test_attached_chat_line_prints_every_level_once_after_startup(
    tmp_path: Path,
    level: str,
) -> None:
    logger, chat_line, chat_stream, console = _attached_steady_state_logger(
        tmp_path
    )

    logger.log(level, "Steady-state detail")

    expected_line = f"[2026-04-17T12:34:56] [{level}] Steady-state detail"
    rendered = f"{LEVEL_TO_COLOR[level]}{expected_line}{RESET_COLOR}"
    assert chat_stream.getvalue() == ERASE + rendered + "\n" + AUDITOR_PROMPT
    assert chat_stream.getvalue().count(expected_line) == 1
    assert console.getvalue() == ""
    assert chat_line.role == "auditor"
    assert chat_line.buffer == "half typed"
    assert chat_line.drawn is True
    assert logger.log_file_path.read_text(encoding="utf-8").endswith(
        expected_line + "\n"
    )


@pytest.mark.parametrize(
    ("level", "message"),
    [
        pytest.param(
            "INFO",
            "Dispatcher verification (epic close): running Work Packet "
            "Verification command before the audit: python -m pytest tests -q",
            id="epic-close-verification-running",
        ),
        pytest.param(
            "INFO",
            "Dispatcher verification: `python -m pytest tests -q` exited 0 "
            "after 12.3s.",
            id="epic-close-verification-exited",
        ),
        pytest.param(
            "INFO",
            "Steady-state detail that the direct console omits",
            id="ordinary-info",
        ),
        pytest.param("DEBUG", "Debug detail after startup", id="ordinary-debug"),
        pytest.param(
            "INFO",
            "work-claim | 2674 passed, 20 skipped in 188.9s",
            id="work-claim-output-tail",
        ),
        pytest.param(
            "INFO",
            "work-claim | gitleaks | no leaks found",
            id="work-claim-scanner-tail",
        ),
    ],
)
def test_attached_chat_line_shows_previously_file_only_messages(
    tmp_path: Path,
    level: str,
    message: str,
) -> None:
    logger, chat_line, chat_stream, console = _attached_steady_state_logger(
        tmp_path
    )

    logger.log(level, message)

    expected_line = f"[2026-04-17T12:34:56] [{level}] {message}"
    rendered = f"{LEVEL_TO_COLOR[level]}{expected_line}{RESET_COLOR}"
    assert chat_stream.getvalue() == ERASE + rendered + "\n" + AUDITOR_PROMPT
    assert console.getvalue() == ""
    assert chat_line.buffer == "half typed"
    assert logger.log_file_path.read_text(encoding="utf-8").endswith(
        expected_line + "\n"
    )


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_attached_chat_line_escapes_control_characters_in_newly_visible_levels(
    tmp_path: Path,
    level: str,
) -> None:
    logger, chat_line, chat_stream, console = _attached_steady_state_logger(
        tmp_path
    )
    spoof = "\x1b]0;owned\x07 \x1b[31mred \rPASS ‮abc\n[fake] [AGENT] PASSED"
    message = f"work-claim | {spoof}"

    logger.log(level, message)

    chat_text = chat_stream.getvalue()
    assert "\x1b]0;owned" not in chat_text
    assert "\x1b[31m" not in chat_text
    assert "‮" not in chat_text
    # Only the erase sequence's carriage return remains.
    assert chat_text.count("\r") == 1
    assert chat_text.count("\n") == 1
    # Trusted sequences only: the erase, the level color, and the reset.
    assert chat_text.count("\x1b") == 3
    assert chat_text.startswith(ERASE + LEVEL_TO_COLOR[level])
    assert chat_text.endswith(RESET_COLOR + "\n" + AUDITOR_PROMPT)
    assert "work-claim | \\x1b]0;owned\\x07 \\x1b[31mred \\rPASS" in chat_text
    assert console.getvalue() == ""
    assert chat_line.buffer == "half typed"
    file_text = logger.log_file_path.read_bytes().decode("utf-8")
    assert message in file_text


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_detached_logger_keeps_steady_state_filter(
    tmp_path: Path,
    level: str,
) -> None:
    logger, chat_line, chat_stream, console = _attached_steady_state_logger(
        tmp_path
    )
    logger.attach_chat_line(None)
    chat_stream.seek(0)
    chat_stream.truncate()

    logger.log(level, "Steady-state detail after detach")
    logger.info("--- Cycle 2 ---")

    expected_line = f"[2026-04-17T12:34:56] [{level}] Steady-state detail after detach"
    assert chat_stream.getvalue() == ""
    assert console.getvalue() == (
        f"{LEVEL_TO_COLOR['INFO']}[2026-04-17T12:34:56] [INFO] --- Cycle 2 ---"
        f"{RESET_COLOR}\n"
    )
    assert expected_line + "\n" in logger.log_file_path.read_text(encoding="utf-8")


def test_reattaching_same_chat_line_keeps_prompt(tmp_path: Path) -> None:
    stream = io.StringIO()
    chat_line = ChatLine(stream, terminal_columns=lambda: 120)
    logger = DispatchLogger(
        repo_root=tmp_path,
        console=io.StringIO(),
        now=lambda: FIXED_NOW,
        chat_line=chat_line,
    )
    chat_line.set_role("backend")

    logger.attach_chat_line(chat_line)

    assert stream.getvalue() == "[backend running] chat> "
    assert chat_line.drawn is True
