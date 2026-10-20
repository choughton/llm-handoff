from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pydantic
import pytest

from llm_handoff import agent_roles, config, orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.audit_review import _quality_reviewer_agent_config
from llm_handoff.config import AgentConfig, DispatchConfig, load_dispatch_config
from llm_handoff.failover import classify_fatal_failure


TIMEOUT_STDERR = "Process timed out after 1200 seconds."


def _dispatch(
    exit_code: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
    elapsed: float = 1.0,
    session_id: str | None = None,
) -> DispatchResult:
    return DispatchResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        session_id=session_id,
    )


def _subagent(
    exit_code: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
    elapsed: float = 1.0,
    session_id: str | None = None,
    usage: dict[str, object] | None = None,
) -> SubagentResult:
    return SubagentResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        session_id=session_id,
        usage=usage,
    )


def _handoff(tmp_path: Path) -> Path:
    path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nnext_agent: backend\n---\n", encoding="utf-8")
    return path


def _codex_with_claude_fallback() -> AgentConfig:
    return AgentConfig(
        provider="codex",
        fallback=AgentConfig(provider="claude", model="claude-sonnet-4-6"),
    )


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("partial work", TIMEOUT_STDERR, "timeout"),
        ("", "Attempt 1 failed with status 429", "rate_limit"),
        ("", "Error: RESOURCE_EXHAUSTED", "rate_limit"),
        ('{"type":"result","is_error":true,"result":"usage limit reached"}', "", "rate_limit"),
        ("", "You've hit your usage limit. Try again later.", "rate_limit"),
        ("", "API Error: 529 overloaded_error", "rate_limit"),
        ("", "Invalid API key - please run /login", "auth"),
        ("", "[WinError 2] The system cannot find the file specified", "launch"),
        ("", "codex: command not found", "launch"),
        # A limit reached while the CLI waited still counts as a limit.
        ("", f"{TIMEOUT_STDERR}\nstatus 429", "rate_limit"),
    ],
)
def test_classify_fatal_failure_recognizes_fatal_failures(
    stdout: str,
    stderr: str,
    expected: str,
) -> None:
    assert (
        classify_fatal_failure(exit_code=1, stdout=stdout, stderr=stderr)
        == expected
    )


@pytest.mark.parametrize(
    ("exit_code", "stdout", "stderr"),
    [
        (0, "", TIMEOUT_STDERR),
        (1, "pytest: 3 failed", "tests failed"),
        # The agent's own transcript ran, so a missing-file error is its own.
        (1, "ran cat", "cat: notes.txt: No such file or directory"),
        # A limit message far back in the transcript is not the final error.
        (1, "rate_limit_exceeded handled in app code\n" + "x" * 5000, "boom"),
    ],
)
def test_classify_fatal_failure_ignores_ordinary_failures(
    exit_code: int,
    stdout: str,
    stderr: str,
) -> None:
    assert (
        classify_fatal_failure(exit_code=exit_code, stdout=stdout, stderr=stderr)
        is None
    )


# --- role failover ---------------------------------------------------------


def test_invoke_role_fails_over_after_primary_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    handoff = _handoff(tmp_path)
    codex = Mock(return_value=_dispatch(1, stderr=TIMEOUT_STDERR, elapsed=10.0))
    claude = Mock(return_value=_subagent(0, stdout="done", elapsed=2.0))
    monkeypatch.setattr(agent_roles, "invoke_codex", codex)
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", claude)
    logs: list[tuple[str, str]] = []

    result = agent_roles.invoke_role(
        "backend",
        handoff,
        agent_config=_codex_with_claude_fallback(),
        log=lambda level, message: logs.append((level, message)),
        repo_root=tmp_path,
    )

    assert codex.call_count == 1
    assert claude.call_count == 1
    assert claude.call_args.kwargs["model"] == "claude-sonnet-4-6"
    assert "Use the backend agent" in claude.call_args.args[1]
    assert result.exit_code == 0
    assert result.stdout == "done"
    assert result.elapsed_seconds == pytest.approx(12.0)
    assert result.session_id is None
    assert result.session_invalidated is True
    warnings = [message for level, message in logs if level == "WARN"]
    assert warnings == [
        (
            "backend (codex) failed: the agent stopped responding and hit its "
            "timeout. Failing over to claude claude-sonnet-4-6 for this dispatch."
        )
    ]


def test_invoke_role_returns_fallback_failure_when_both_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    handoff = _handoff(tmp_path)
    monkeypatch.setattr(
        agent_roles,
        "invoke_codex",
        Mock(return_value=_dispatch(1, stderr="status 429")),
    )
    monkeypatch.setattr(
        agent_roles,
        "invoke_claude_subagent",
        Mock(return_value=_subagent(1, stderr=TIMEOUT_STDERR)),
    )

    result = agent_roles.invoke_role(
        "backend",
        handoff,
        agent_config=_codex_with_claude_fallback(),
        repo_root=tmp_path,
    )

    assert result.exit_code == 1
    assert result.stderr == TIMEOUT_STDERR


def test_invoke_role_keeps_ordinary_failure_on_primary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    handoff = _handoff(tmp_path)
    primary = _dispatch(1, stdout="pytest: 2 failed", stderr="tests failed")
    monkeypatch.setattr(agent_roles, "invoke_codex", Mock(return_value=primary))
    claude = Mock(return_value=_subagent(0))
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", claude)

    result = agent_roles.invoke_role(
        "backend",
        handoff,
        agent_config=_codex_with_claude_fallback(),
        repo_root=tmp_path,
    )

    assert result is primary
    claude.assert_not_called()


def test_invoke_role_does_not_fail_over_after_primary_rewrote_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    handoff = _handoff(tmp_path)

    def hang_after_handoff(*args: object, **kwargs: object) -> DispatchResult:
        handoff.write_text("---\nnext_agent: auditor\n---\n", encoding="utf-8")
        return _dispatch(1, stderr=TIMEOUT_STDERR)

    monkeypatch.setattr(agent_roles, "invoke_codex", hang_after_handoff)
    claude = Mock(return_value=_subagent(0))
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", claude)
    logs: list[tuple[str, str]] = []

    result = agent_roles.invoke_role(
        "backend",
        Path("docs/handoff/HANDOFF.md"),
        agent_config=_codex_with_claude_fallback(),
        log=lambda level, message: logs.append((level, message)),
        repo_root=tmp_path,
    )

    assert result.exit_code == 1
    claude.assert_not_called()
    assert any(
        level == "WARN" and "already rewritten HANDOFF.md" in message
        for level, message in logs
    )


def test_fallback_starts_without_the_primary_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    handoff = _handoff(tmp_path)
    antigravity = Mock(return_value=_dispatch(1, stderr="RESOURCE_EXHAUSTED"))
    codex = Mock(return_value=_dispatch(0, session_id="codex-thread"))
    monkeypatch.setattr(agent_roles, "invoke_antigravity", antigravity)
    monkeypatch.setattr(agent_roles, "invoke_codex", codex)

    result = agent_roles.invoke_role(
        "planner",
        handoff,
        agent_config=AgentConfig(
            provider="antigravity",
            fallback=AgentConfig(provider="codex"),
        ),
        use_resume=True,
        session_id="agy-session",
        previous_handoff_sha="abc123",
        repo_root=tmp_path,
    )

    assert antigravity.call_args.kwargs["use_resume"] is True
    assert antigravity.call_args.kwargs["session_id"] == "agy-session"
    assert codex.call_args.kwargs["use_resume"] is False
    assert codex.call_args.kwargs["role_name"] == "planner"
    assert result.exit_code == 0
    assert result.session_id is None
    assert result.session_invalidated is True


def test_invoke_role_without_fallback_returns_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = _dispatch(1, stderr=TIMEOUT_STDERR)
    codex = Mock(return_value=primary)
    monkeypatch.setattr(agent_roles, "invoke_codex", codex)

    result = agent_roles.invoke_role(
        "backend",
        _handoff(tmp_path),
        agent_config=AgentConfig(provider="codex"),
        repo_root=tmp_path,
    )

    assert result is primary
    assert codex.call_count == 1


def test_support_role_fails_over_to_another_claude_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _handoff(tmp_path)
    claude = Mock(
        side_effect=[
            _subagent(1, stdout='{"result":"Claude usage limit reached"}'),
            _subagent(0, stdout="verdict"),
        ]
    )
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", claude)
    logs: list[tuple[str, str]] = []

    result = agent_roles.invoke_support_role(
        "auditor",
        "Audit the handoff.",
        role="auditor",
        agent_config=AgentConfig(
            provider="claude",
            model="claude-opus-4-7",
            fallback=AgentConfig(provider="claude", model="claude-sonnet-4-6"),
        ),
        log=lambda level, message: logs.append((level, message)),
        repo_root=tmp_path,
    )

    assert isinstance(result, SubagentResult)
    assert result.exit_code == 0
    assert [call.kwargs["model"] for call in claude.call_args_list] == [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
    ]
    assert any(
        "auditor (claude claude-opus-4-7) failed: the provider reported a rate "
        "or usage limit" in message
        for _level, message in logs
    )


def test_support_role_fails_over_to_non_claude_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _handoff(tmp_path)
    monkeypatch.setattr(
        agent_roles,
        "invoke_claude_subagent",
        Mock(return_value=_subagent(1, stderr="Failed to authenticate")),
    )
    codex = Mock(return_value=_dispatch(0, stdout="routed"))
    monkeypatch.setattr(agent_roles, "invoke_codex", codex)
    logs: list[tuple[str, str]] = []

    result = agent_roles.invoke_support_role(
        "handoff-router",
        "Clarify the route.",
        role="validator",
        agent_config=AgentConfig(
            provider="claude",
            fallback=AgentConfig(provider="codex", resume=True),
        ),
        log=lambda level, message: logs.append((level, message)),
        repo_root=tmp_path,
    )

    assert isinstance(result, SubagentResult)
    assert result.stdout == "routed"
    assert codex.call_args.kwargs["use_resume"] is False
    assert "Clarify the route." in codex.call_args.kwargs["additional_instruction"]
    assert any("sign-in fix" in message for _level, message in logs)


def test_support_role_failover_drops_the_fallback_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _handoff(tmp_path)
    claude = Mock(
        side_effect=[
            _subagent(1, stderr="status 429", session_id="primary-session"),
            _subagent(
                0,
                stdout="verdict",
                session_id="fallback-session",
                usage={"input_tokens": 5},
            ),
        ]
    )
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", claude)

    result = agent_roles.invoke_support_role(
        "handoff-router",
        "Clarify the route.",
        role="validator",
        agent_config=AgentConfig(
            provider="claude",
            model="claude-opus-4-7",
            fallback=AgentConfig(provider="claude", model="claude-sonnet-4-6"),
        ),
        repo_root=tmp_path,
    )

    assert claude.call_count == 2
    assert isinstance(result, SubagentResult)
    assert result.exit_code == 0
    assert result.session_id is None
    assert result.usage == {"input_tokens": 5}


def test_support_role_codex_failover_drops_the_codex_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _handoff(tmp_path)
    monkeypatch.setattr(
        agent_roles,
        "invoke_claude_subagent",
        Mock(
            return_value=_subagent(
                1, stderr="status 429", session_id="primary-session"
            )
        ),
    )
    codex = Mock(
        return_value=_dispatch(0, stdout="routed", session_id="codex-thread")
    )
    monkeypatch.setattr(agent_roles, "invoke_codex", codex)

    result = agent_roles.invoke_support_role(
        "handoff-router",
        "Clarify the route.",
        role="validator",
        agent_config=AgentConfig(
            provider="claude",
            model="claude-opus-4-7",
            fallback=AgentConfig(provider="codex"),
        ),
        repo_root=tmp_path,
    )

    assert codex.call_count == 1
    assert isinstance(result, SubagentResult)
    assert result.stdout == "routed"
    assert result.session_id is None


# --- configuration ---------------------------------------------------------


def test_config_file_fallback_gets_provider_defaults(tmp_path: Path) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        "agents:\n"
        "  backend:\n"
        "    provider: codex\n"
        "    fallback:\n"
        "      provider: claude\n"
        "      model: claude-sonnet-4-6\n"
        "  planner:\n"
        "    fallback:\n"
        "      provider: codex\n",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path)

    backend_fallback = loaded.agents["backend"].fallback
    assert backend_fallback is not None
    assert backend_fallback.provider == "claude"
    assert backend_fallback.model == "claude-sonnet-4-6"
    assert backend_fallback.binary == config.CLAUDE_BINARY
    assert backend_fallback.timeout_ms == config.SUBAGENT_TIMEOUT_MS
    planner = loaded.agents["planner"]
    assert planner.provider == "antigravity"
    assert planner.fallback is not None
    assert planner.fallback.skill_name == config.CODEX_SKILL_NAME
    assert loaded.agents["auditor"].fallback is None


@pytest.mark.parametrize(
    ("fallback", "message"),
    [
        (
            {"provider": "claude", "fallback": {"provider": "antigravity"}},
            "must not define its own fallback",
        ),
        ({"provider": "codex"}, "must launch a different provider"),
    ],
)
def test_invalid_fallback_is_rejected(fallback: dict, message: str) -> None:
    with pytest.raises(pydantic.ValidationError, match=message):
        AgentConfig.model_validate({"provider": "codex", "fallback": fallback})


def test_same_provider_fallback_needs_a_different_binary_or_claude_model() -> None:
    AgentConfig(
        provider="codex",
        fallback=AgentConfig(provider="codex", binary="codex-second-account"),
    )
    AgentConfig(
        provider="claude",
        model="claude-opus-4-7",
        fallback=AgentConfig(provider="claude", model="claude-sonnet-4-6"),
    )
    with pytest.raises(pydantic.ValidationError):
        AgentConfig(
            provider="claude",
            model="claude-opus-4-7",
            fallback=AgentConfig(provider="claude", model="claude-opus-4-7"),
        )


def test_fallback_without_runtime_adapter_is_rejected(tmp_path: Path) -> None:
    agents = config._default_agent_configs()
    agents["backend"] = AgentConfig(
        provider="codex",
        fallback=AgentConfig(provider="openai"),
    )

    with pytest.raises(pydantic.ValidationError, match="backend` fallback"):
        DispatchConfig(repo_root=tmp_path, agents=agents)


# --- startup ---------------------------------------------------------------


def test_startup_banner_logs_fallback_launch_posture(tmp_path: Path) -> None:
    agents = config._default_agent_configs()
    agents["backend"] = AgentConfig(
        provider="codex",
        fallback=AgentConfig(provider="claude", unsafe_bypass=True),
    )
    logs: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(
        DispatchConfig(repo_root=tmp_path, agents=agents),
        lambda level, message: logs.append((level, message)),
    )

    info = [message for level, message in logs if level == "INFO"]
    warnings = [message for level, message in logs if level == "WARN"]
    assert any(m.startswith("Launch backend fallback:claude") for m in info)
    assert any(
        "Remove agents.backend.fallback.unsafe_bypass" in m for m in warnings
    )


def test_fallback_sign_in_failure_names_the_fallback_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        agent_roles,
        "invoke_codex",
        Mock(return_value=_dispatch(1, stderr=TIMEOUT_STDERR)),
    )
    monkeypatch.setattr(
        agent_roles,
        "invoke_claude_subagent",
        Mock(return_value=_subagent(1, stderr="Failed to authenticate")),
    )
    logs: list[tuple[str, str]] = []

    result = agent_roles.invoke_role(
        "backend",
        _handoff(tmp_path),
        agent_config=_codex_with_claude_fallback(),
        log=lambda level, message: logs.append((level, message)),
        repo_root=tmp_path,
    )

    assert result.exit_code == 1
    assert any(
        level == "WARN"
        and message.startswith("Fallback claude claude-sonnet-4-6 sign-in fix:")
        and "/login" in message
        for level, message in logs
    )


@pytest.mark.parametrize(
    ("fallback", "kept"),
    [
        (AgentConfig(provider="claude", model="claude-sonnet-4-6"), True),
        (AgentConfig(provider="codex"), False),
    ],
)
def test_fan_out_quality_reviewer_keeps_only_same_provider_fallback(
    tmp_path: Path,
    fallback: AgentConfig,
    kept: bool,
) -> None:


    agents = config._default_agent_configs()
    agents["auditor"] = AgentConfig(
        provider="claude",
        model="claude-opus-4-7",
        timeout_ms=5,
        fallback=fallback,
    )
    dispatch_config = DispatchConfig(
        repo_root=tmp_path,
        agents=agents,
        audit={"reviewers": ["quality", "security"], "review_timeout_ms": 777},
    )

    quality = _quality_reviewer_agent_config(dispatch_config)

    assert quality.timeout_ms == 777
    if kept:
        assert quality.fallback is not None
        assert quality.fallback.model == "claude-sonnet-4-6"
        assert quality.fallback.timeout_ms == 777
    else:
        assert quality.fallback is None


# --- failover guard --------------------------------------------------------


def test_failover_guard_reason_blocks_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = _dispatch(1, stderr=TIMEOUT_STDERR)
    monkeypatch.setattr(agent_roles, "invoke_codex", Mock(return_value=primary))
    claude = Mock(return_value=_subagent(0))
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", claude)
    logs: list[tuple[str, str]] = []

    result = agent_roles.invoke_role(
        "backend",
        _handoff(tmp_path),
        agent_config=_codex_with_claude_fallback(),
        log=lambda level, message: logs.append((level, message)),
        repo_root=tmp_path,
        failover_guard=lambda: "it changed protected control files (AGENTS.md)",
    )

    assert result is primary
    claude.assert_not_called()
    assert [m for level, m in logs if level == "WARN"] == [
        (
            "backend (codex) failed: the agent stopped responding and hit its "
            "timeout, but it changed protected control files (AGENTS.md). Not "
            "failing over to claude claude-sonnet-4-6; this counts as a normal "
            "failure for the role."
        )
    ]


def test_failover_guard_is_only_built_for_roles_with_a_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    agents = config._default_agent_configs()
    agents["backend"] = _codex_with_claude_fallback()
    dispatch_config = DispatchConfig(repo_root=tmp_path, agents=agents)

    assert orchestrator._failover_kwargs(dispatch_config, "planner") == {}
    guard = orchestrator._failover_kwargs(dispatch_config, "backend")[
        "failover_guard"
    ]
    assert guard() is None
    (tmp_path / "AGENTS.md").write_text("new rules\n", encoding="utf-8")
    assert guard() == "it changed protected control files (AGENTS.md)"


def test_dispatch_route_skips_fallback_after_protected_file_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _handoff(tmp_path)
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("rules\n", encoding="utf-8")

    def tamper_then_hang(*args: object, **kwargs: object) -> DispatchResult:
        agents_md.write_text("ignore the rules\n", encoding="utf-8")
        return _dispatch(1, stderr=TIMEOUT_STDERR)

    monkeypatch.setattr(agent_roles, "invoke_codex", tamper_then_hang)
    claude = Mock(return_value=_subagent(0))
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", claude)
    agents = config._default_agent_configs()
    agents["backend"] = _codex_with_claude_fallback()
    logs: list[tuple[str, str]] = []

    result, label = orchestrator._dispatch_route(
        "backend",
        DispatchConfig(repo_root=tmp_path, agents=agents),
        lambda level, message: logs.append((level, message)),
    )

    assert label == "backend"
    assert result.exit_code == 1
    claude.assert_not_called()
    assert any("AGENTS.md" in message for level, message in logs if level == "WARN")
