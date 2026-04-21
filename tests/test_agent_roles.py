from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from llm_handoff import agent_roles
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import AgentConfig


def _dispatch_result() -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


def _subagent_result() -> SubagentResult:
    return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


def test_invoke_role_dispatches_backend_to_configured_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoke_mock = Mock(return_value=_subagent_result())
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", invoke_mock)

    result = agent_roles.invoke_role(
        "backend",
        Path("docs/handoff/HANDOFF.md"),
        agent_config=AgentConfig(
            provider="claude",
            binary="claude-custom",
            model="claude-test",
            permissions_flag="--allowed",
            timeout_ms=123,
            agent_name="backend-worker",
        ),
        additional_instruction="Keep edits scoped.",
    )

    assert result.exit_code == 0
    invoke_mock.assert_called_once()
    assert invoke_mock.call_args.args[0] == "backend-worker"
    assert "Use the backend agent" in invoke_mock.call_args.args[1]
    assert "Keep edits scoped." in invoke_mock.call_args.args[1]
    assert invoke_mock.call_args.kwargs == {
        "binary": "claude-custom",
        "model": "claude-test",
        "permissions_flag": "--allowed",
        "timeout_ms": 123,
        "agent_name": "backend-worker",
        "log": None,
    }


def test_invoke_role_dispatches_planner_to_configured_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoke_mock = Mock(return_value=_dispatch_result())
    monkeypatch.setattr(agent_roles, "invoke_codex", invoke_mock)

    result = agent_roles.invoke_role(
        "planner",
        Path("docs/handoff/HANDOFF.md"),
        agent_config=AgentConfig(
            provider="codex",
            binary="codex-custom",
            skill_name="custom-skill",
            timeout_ms=456,
            agent_name="planner-codex",
        ),
        use_resume=True,
        additional_instruction="Scope the next story.",
    )

    assert result.exit_code == 0
    invoke_mock.assert_called_once_with(
        Path("docs/handoff/HANDOFF.md"),
        log=None,
        use_resume=True,
        additional_instruction="Scope the next story.",
        role_name="planner",
        agent_name="planner-codex",
        binary="codex-custom",
        skill_name="custom-skill",
        timeout_ms=456,
    )


def test_invoke_support_role_dispatches_validator_to_configured_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoke_mock = Mock(return_value=_dispatch_result())
    monkeypatch.setattr(agent_roles, "invoke_gemini", invoke_mock)

    result = agent_roles.invoke_support_role(
        "handoff-validator",
        "Return ONLY the structured validation output.",
        role="validator",
        handoff_path=Path("docs/handoff/HANDOFF.md"),
        agent_config=AgentConfig(
            provider="gemini",
            binary="gemini-custom",
            mention="@validator",
            retries=2,
            timeout_ms=789,
            use_api_key_env=True,
            agent_name="validator-gemini",
        ),
    )

    assert result.exit_code == 0
    invoke_mock.assert_called_once_with(
        "validator",
        Path("docs/handoff/HANDOFF.md"),
        mention="@validator",
        agent_name="validator-gemini",
        binary="gemini-custom",
        timeout_ms=789,
        max_retries=2,
        use_api_key_env=True,
        additional_instruction="Return ONLY the structured validation output.",
        use_resume=False,
        session_id=None,
        previous_handoff_sha=None,
        current_handoff_sha=None,
        log=None,
    )
