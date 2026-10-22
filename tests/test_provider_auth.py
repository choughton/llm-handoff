from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff import orchestrator
from llm_handoff.agent_types import DispatchResult
from llm_handoff.config import AgentConfig, DispatchConfig
from llm_handoff.provider_auth import (
    antigravity_auth_failure_line,
    antigravity_quota_exhausted_line,
    auth_failure_messages,
    auth_fix_instructions,
    find_antigravity_auth_failure,
    find_antigravity_quota_exhaustion,
    find_auth_failure,
    find_permission_soft_deny,
    find_quota_exhaustion,
    permission_soft_deny_message,
    quota_exhaustion_messages,
    quota_reset_time,
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
        "Please run `gh auth login` to authenticate.",
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
        ("antigravity", "Run `agy` once in a terminal"),
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


def test_removed_gemini_provider_has_no_sign_in_markers_or_fix_text() -> None:
    # The gemini provider was removed, so its sign-in markers and fix steps
    # went with it; an unknown provider gets the generic sign-in text.
    assert find_auth_failure("Please run gemini login first.") is None
    assert (
        find_auth_failure(
            "IneligibleTierError: no longer supported for Gemini Code Assist"
        )
        is None
    )
    assert auth_fix_instructions("gemini") == auth_fix_instructions(None)


# Antigravity (agy)

AGY_SIGNED_OUT = "Error: authentication required"
AGY_SOFT_DENY = (
    'jetski: no output produced - a tool required the "command" permission '
    "that headless mode cannot prompt for, so it was auto-denied. Add an "
    "allow-rule under permissions.allow in settings.json (e.g. "
    "command(<target>))."
)


@pytest.mark.parametrize(
    "text",
    [AGY_SIGNED_OUT, "AUTHENTICATION REQUIRED: sign in first"],
)
def test_find_antigravity_auth_failure_recognizes_agy_sign_in_failure(
    text: str,
) -> None:
    assert find_antigravity_auth_failure("", "noise\n" + text) == text


@pytest.mark.parametrize(
    "text",
    [
        AGY_SIGNED_OUT,
        "authentication required for deployment",
        "AUTHENTICATION REQUIRED: sign in first",
    ],
)
def test_shared_matcher_does_not_treat_authentication_required_as_sign_in(
    text: str,
) -> None:
    # Claude and Codex output can say this about anything; only the
    # agy adapter treats it as a sign-in failure.
    assert find_auth_failure(text) is None


def test_find_antigravity_auth_failure_ignores_ordinary_failures() -> None:
    assert find_antigravity_auth_failure("Traceback: KeyError", None, "") is None


def test_antigravity_auth_failure_line_is_recognized_by_the_shared_matcher() -> None:
    line = antigravity_auth_failure_line(AGY_SIGNED_OUT)

    assert AGY_SIGNED_OUT in line
    assert find_auth_failure("", AGY_SIGNED_OUT + "\n" + line) == line


def test_antigravity_fix_instructions() -> None:
    text = auth_fix_instructions("antigravity")

    assert text.startswith("Run `agy` once in a terminal and finish signing in.")
    assert 'agy -p "Reply with OK"' in text


def test_auth_failure_messages_map_antigravity_to_the_agy_cli() -> None:
    messages = auth_failure_messages(
        role="backend",
        provider="antigravity",
        binary=None,
        detail=AGY_SIGNED_OUT,
    )

    assert messages[0] == (
        f"backend could not start: the `agy` CLI is not signed in ({AGY_SIGNED_OUT})."
    )
    assert messages[1].startswith("Fix: Run `agy`")


def test_auth_failure_messages_without_binary_fall_back_to_provider() -> None:
    messages = auth_failure_messages(
        role="planner", provider="claude", binary=None, detail="x"
    )

    assert "the `claude` CLI" in messages[0]


def test_find_permission_soft_deny_matches_the_agy_notice() -> None:
    assert find_permission_soft_deny(None, "", "noise\n  " + AGY_SOFT_DENY) == (
        AGY_SOFT_DENY
    )
    assert find_permission_soft_deny(AGY_SOFT_DENY.upper()) == AGY_SOFT_DENY.upper()


@pytest.mark.parametrize(
    "text",
    [
        AGY_SIGNED_OUT,
        CLAUDE_EXPIRED,
        "Traceback: KeyError 'story_id'",
        "the request was auto-denied",
        "agy cannot prompt for input",
        "",
    ],
)
def test_find_permission_soft_deny_ignores_other_failures(text: str) -> None:
    assert find_permission_soft_deny(text) is None


def test_permission_soft_deny_message_for_the_observed_notice() -> None:
    message = permission_soft_deny_message(AGY_SOFT_DENY)

    assert "\n" not in message
    assert '"command" permission' in message
    assert "`command(<target>)`" in message
    assert "permissions.allow" in message
    assert "~/.gemini/antigravity-cli/settings.json" in message
    assert "every agy run for that user" in message


def test_permission_soft_deny_message_without_a_kind() -> None:
    message = permission_soft_deny_message(
        "a tool required a permission that headless mode cannot prompt for, "
        "so it was auto-denied."
    )

    assert "a tool permission" in message
    assert "`<kind>(<target>)`" in message
    assert "permissions.allow" in message


def test_permission_soft_deny_message_without_an_example() -> None:
    message = permission_soft_deny_message(
        'a tool required the "browser" permission that headless mode cannot '
        "prompt for, so it was auto-denied."
    )

    assert '"browser" permission' in message
    assert "`browser(<target>)`" in message
    assert "permissions.allow" in message


def test_stop_on_auth_failure_names_agy_for_an_antigravity_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_handoff.agent_providers import antigravity
    from llm_handoff.agent_types import _ProcessResult

    monkeypatch.setattr(
        antigravity,
        "_run_command_streaming",
        lambda *_args, **_kwargs: _ProcessResult(
            stdout="", stderr=AGY_SIGNED_OUT, exit_code=1
        ),
    )
    agent_config = AgentConfig(provider="antigravity")
    result = antigravity.invoke_antigravity(
        "backend",
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        agent_config=agent_config,
    )
    dispatch_config = DispatchConfig(repo_root=tmp_path)
    dispatch_config.agents["backend"] = agent_config
    logs: list[tuple[str, str]] = []

    stopped = orchestrator._stop_on_auth_failure(
        dispatch_config,
        role="backend",
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        log=lambda level, message: logs.append((level, message)),
    )

    assert result.exit_code == 1
    assert stopped is True
    errors = [message for level, message in logs if level == "ERROR"]
    assert any("the `agy` CLI is not signed in" in message for message in errors)
    assert any(message.startswith("Fix: Run `agy`") for message in errors)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_stop_on_auth_failure_ignores_authentication_required_for_other_providers(
    tmp_path: Path,
    provider: str,
) -> None:
    dispatch_config = DispatchConfig(repo_root=tmp_path)
    dispatch_config.agents["backend"] = AgentConfig(provider=provider)
    logs: list[tuple[str, str]] = []

    stopped = orchestrator._stop_on_auth_failure(
        dispatch_config,
        role="backend",
        exit_code=1,
        stdout="Deploy step failed: authentication required for deployment",
        stderr="",
        log=lambda level, message: logs.append((level, message)),
    )

    assert stopped is False
    assert logs == []


# agy 1.4.4's quota error, as its log recorded it on 2026-10-21.
AGY_QUOTA_MESSAGE = (
    "Individual quota reached. Please upgrade your subscription to increase "
    "your limits. Resets in 72h11m35s."
)


@pytest.mark.parametrize(
    "text",
    [
        AGY_QUOTA_MESSAGE,
        "RESOURCE_EXHAUSTED (code 429): " + AGY_QUOTA_MESSAGE,
        "Error: request failed with code 429",
        "rpc error: resource_exhausted",
    ],
)
def test_find_antigravity_quota_exhaustion_recognizes_agy_quota_errors(
    text: str,
) -> None:
    assert find_antigravity_quota_exhaustion(None, "noise\n" + text) == text


def test_find_antigravity_quota_exhaustion_ignores_ordinary_failures() -> None:
    assert find_antigravity_quota_exhaustion("Traceback: KeyError", None, "") is None


def test_find_quota_exhaustion_only_matches_the_adapter_line() -> None:
    # Raw quota text from any provider gets normal failure handling; only
    # the line the agy adapter appends stops the loop.
    raw = "RESOURCE_EXHAUSTED (code 429): " + AGY_QUOTA_MESSAGE
    assert find_quota_exhaustion(raw) is None
    line = antigravity_quota_exhausted_line(AGY_QUOTA_MESSAGE)
    assert find_quota_exhaustion("agy notice", line) == AGY_QUOTA_MESSAGE


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (AGY_QUOTA_MESSAGE, "72h11m35s"),
        ("quota reached; resets in 3h 5m", "3h5m"),
        ("Individual quota reached.", None),
    ],
)
def test_quota_reset_time(detail: str, expected: str | None) -> None:
    assert quota_reset_time(detail) == expected


def test_quota_exhaustion_messages_name_role_provider_reset_and_fix() -> None:
    messages = quota_exhaustion_messages(
        role="planner",
        provider="antigravity",
        binary=r"C:\tools\agy.cmd",
        detail=AGY_QUOTA_MESSAGE,
    )

    assert messages[0] == (
        "planner could not run: the antigravity (`agy`) account quota is "
        f"exhausted; it resets in 72h11m35s ({AGY_QUOTA_MESSAGE})."
    )
    assert messages[1].startswith("Fix: wait for the quota to reset")
    assert "`agents.planner`" in messages[1]
    assert "stops here" in messages[2]


def test_quota_exhaustion_messages_without_a_reset_time() -> None:
    first = quota_exhaustion_messages(
        role="backend",
        provider="antigravity",
        binary=None,
        detail="Individual quota reached.",
    )[0]

    assert first == (
        "backend could not run: the antigravity (`agy`) account quota is "
        "exhausted (Individual quota reached.)."
    )


def test_run_loop_stops_on_the_first_quota_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_backend_handoff(tmp_path)
    config = DispatchConfig(
        repo_root=tmp_path,
        poll_interval_seconds=0,
        max_consecutive_failures=3,
    )
    config.agents["backend"] = AgentConfig(provider="antigravity", binary="agy")
    attempts: list[int] = []
    logs: list[tuple[str, str]] = []

    def out_of_quota_backend(
        _path: Path, *, log=None, use_resume=False
    ) -> DispatchResult:
        attempts.append(1)
        return DispatchResult(
            stdout="",
            stderr=(
                f"Antigravity result status=ERROR: {AGY_QUOTA_MESSAGE}\n"
                f"{antigravity_quota_exhausted_line(AGY_QUOTA_MESSAGE)}"
            ),
            exit_code=3,
            elapsed_seconds=0.1,
        )

    monkeypatch.setattr(orchestrator, "invoke_backend_role", out_of_quota_backend)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: logs.append((level, message)),
    )

    assert exit_code == 1
    assert attempts == [1]
    errors = [message for level, message in logs if level == "ERROR"]
    assert any(
        "backend could not run: the antigravity (`agy`) account quota is "
        "exhausted; it resets in 72h11m35s" in message
        for message in errors
    )
    assert any(message.startswith("Fix: wait for the quota") for message in errors)
    assert not any("Failure count" in message for _, message in logs)


def test_stop_on_quota_exhaustion_for_an_antigravity_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from llm_handoff.agent_providers import antigravity
    from llm_handoff.agent_types import _ProcessResult

    stdout = json.dumps(
        {"event": "result", "result": {"status": "ERROR", "error": AGY_QUOTA_MESSAGE}}
    )
    monkeypatch.setattr(
        antigravity,
        "_run_command_streaming",
        lambda *_args, **_kwargs: _ProcessResult(
            stdout=stdout + "\n", stderr="", exit_code=3
        ),
    )
    agent_config = AgentConfig(provider="antigravity")
    result = antigravity.invoke_antigravity(
        "planner",
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        agent_config=agent_config,
    )
    dispatch_config = DispatchConfig(repo_root=tmp_path)
    dispatch_config.agents["planner"] = agent_config
    logs: list[tuple[str, str]] = []

    stopped = orchestrator._stop_on_quota_exhaustion(
        dispatch_config,
        role="planner",
        exit_code=result.exit_code,
        stderr=result.stderr,
        log=lambda level, message: logs.append((level, message)),
    )

    assert stopped is True
    errors = [message for level, message in logs if level == "ERROR"]
    assert errors[0].startswith(
        "planner could not run: the antigravity (`agy`) account quota is exhausted"
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_stop_on_quota_exhaustion_ignores_raw_429_text_from_other_providers(
    tmp_path: Path,
    provider: str,
) -> None:
    dispatch_config = DispatchConfig(repo_root=tmp_path)
    dispatch_config.agents["backend"] = AgentConfig(provider=provider)
    logs: list[tuple[str, str]] = []

    stopped = orchestrator._stop_on_quota_exhaustion(
        dispatch_config,
        role="backend",
        exit_code=1,
        stderr="RESOURCE_EXHAUSTED (code 429): " + AGY_QUOTA_MESSAGE,
        log=lambda level, message: logs.append((level, message)),
    )

    assert stopped is False
    assert logs == []
