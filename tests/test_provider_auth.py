from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff import orchestrator
from llm_handoff.agent_types import DispatchResult
from llm_handoff.config import DispatchConfig
from llm_handoff.provider_auth import (
    auth_failure_messages,
    auth_fix_instructions,
    find_auth_failure,
)


CLAUDE_EXPIRED = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)


@pytest.mark.parametrize(
    "text",
    [
        CLAUDE_EXPIRED,
        "Error: Invalid API key. Please run /login.",
        "Not logged in. Please run codex login.",
        "HTTP 401 Unauthorized",
        "Please run `gemini auth login` to authenticate.",
    ],
)
def test_find_auth_failure_recognizes_provider_messages(text: str) -> None:
    assert find_auth_failure("some earlier output\n" + text) == text


def test_find_auth_failure_checks_every_stream() -> None:
    assert find_auth_failure("", None, CLAUDE_EXPIRED) == CLAUDE_EXPIRED


def test_find_auth_failure_ignores_ordinary_failures() -> None:
    assert find_auth_failure("Traceback: KeyError 'story_id'", "exit 9") is None


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("claude", "/login"),
        ("codex", "codex login"),
        ("gemini", "GEMINI_API_KEY"),
        (None, "Sign in to the provider CLI"),
    ],
)
def test_auth_fix_instructions_name_the_sign_in_step(
    provider: str | None, expected: str
) -> None:
    assert expected in auth_fix_instructions(provider)


def test_auth_failure_messages_name_the_command_not_the_shim() -> None:
    messages = auth_failure_messages(
        role="planner",
        provider="claude",
        binary=r"C:\tools\claude.cmd",
        detail=CLAUDE_EXPIRED,
    )

    assert messages[0] == (
        f"planner could not start: the `claude` CLI is not signed in ({CLAUDE_EXPIRED})."
    )
    assert messages[1].startswith("Fix: Run `claude` in a terminal, type /login")
    assert "stops here" in messages[2]


def _write_backend_handoff(repo_root: Path) -> None:
    handoff_path = repo_root / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        "## Next Step\n\n- **backend:** Do the work.\n", encoding="utf-8"
    )
    (repo_root / "AGENTS.md").write_text("fixture\n", encoding="utf-8")


def test_run_loop_stops_on_the_first_sign_in_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_backend_handoff(tmp_path)
    config = DispatchConfig(
        repo_root=tmp_path,
        poll_interval_seconds=0,
        max_consecutive_failures=3,
    )
    attempts: list[int] = []
    logs: list[tuple[str, str]] = []

    def signed_out_backend(
        _path: Path, *, log=None, use_resume=False
    ) -> DispatchResult:
        attempts.append(1)
        return DispatchResult(
            stdout=CLAUDE_EXPIRED,
            stderr="",
            exit_code=1,
            elapsed_seconds=0.1,
        )

    monkeypatch.setattr(orchestrator, "invoke_backend_role", signed_out_backend)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: logs.append((level, message)),
    )

    assert exit_code == 1
    assert attempts == [1]
    errors = [message for level, message in logs if level == "ERROR"]
    assert any("backend could not start" in message for message in errors)
    assert any(message.startswith("Fix: Run `codex login`") for message in errors)
    assert not any("Failure count" in message for _, message in logs)
