from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import ANY, Mock

import pytest

from llm_handoff import agent_roles
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import AgentConfig
from llm_handoff.validator import parse_validation_output


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
            unsafe_bypass=True,
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
        "repo_root": None,
        "agent_config": ANY,
    }
    assert invoke_mock.call_args.kwargs["agent_config"].binary == "claude-custom"


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
        repo_root=None,
        agent_config=ANY,
    )
    assert invoke_mock.call_args.kwargs["agent_config"].skill_name == "custom-skill"


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
        repo_root=None,
        agent_config=ANY,
    )
    assert invoke_mock.call_args.kwargs["agent_config"].mention == "@validator"


@pytest.mark.parametrize("role", ["planner", "backend", "frontend", "finalizer"])
def test_invoke_role_dispatches_to_configured_antigravity(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    invoke_mock = Mock(return_value=_dispatch_result())
    monkeypatch.setattr(agent_roles, "invoke_antigravity", invoke_mock)

    result = agent_roles.invoke_role(
        role,
        Path("docs/handoff/HANDOFF.md"),
        agent_config=AgentConfig(
            provider="antigravity",
            binary="agy-custom",
            model="pro",
            timeout_ms=321,
        ),
        use_resume=True,
        session_id="conv-1",
        previous_handoff_sha="aaa",
        current_handoff_sha="bbb",
        additional_instruction="Stay in scope.",
    )

    assert result.exit_code == 0
    invoke_mock.assert_called_once_with(
        role,
        Path("docs/handoff/HANDOFF.md"),
        agent_name=f"Antigravity {role}",
        binary="agy-custom",
        model="pro",
        timeout_ms=321,
        additional_instruction="Stay in scope.",
        support_prompt=None,
        subagent_name=None,
        use_resume=True,
        session_id="conv-1",
        previous_handoff_sha="aaa",
        current_handoff_sha="bbb",
        log=None,
        repo_root=None,
        agent_config=ANY,
    )


def test_invoke_support_role_dispatches_validator_to_configured_antigravity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoke_mock = Mock(
        return_value=DispatchResult(
            stdout="VALID: YES", stderr="", exit_code=0, elapsed_seconds=0.01
        )
    )
    monkeypatch.setattr(agent_roles, "invoke_antigravity", invoke_mock)

    result = agent_roles.invoke_support_role(
        "handoff-validator",
        "Return ONLY the structured validation output.",
        role="validator",
        handoff_path=Path("docs/handoff/HANDOFF.md"),
        agent_config=AgentConfig(provider="antigravity"),
    )

    assert isinstance(result, SubagentResult)
    assert result.stdout == "VALID: YES"
    kwargs = invoke_mock.call_args.kwargs
    assert invoke_mock.call_args.args == ("validator", Path("docs/handoff/HANDOFF.md"))
    assert kwargs["support_prompt"] == "Return ONLY the structured validation output."
    assert kwargs["subagent_name"] == "handoff-validator"
    assert kwargs["agent_name"] == "Antigravity handoff-validator"
    assert kwargs["use_resume"] is False
    assert kwargs["session_id"] is None


@pytest.mark.parametrize(
    ("binary", "label"),
    [
        (None, "antigravity"),
        ("agy", "antigravity"),
        ("agy.cmd", "antigravity"),
        ("C:/tools/agy-beta.cmd", "antigravity via agy-beta"),
    ],
)
def test_describe_agent_labels_default_agy_binary_plainly(
    binary: str | None,
    label: str,
) -> None:
    agent = AgentConfig(provider="antigravity", binary=binary)

    assert agent_roles._describe_agent(agent) == label


def test_describe_agent_still_names_custom_binaries_for_other_providers() -> None:
    assert agent_roles._describe_agent(AgentConfig(provider="codex", binary="codex")) == (
        "codex"
    )
    assert agent_roles._describe_agent(AgentConfig(provider="codex", binary="agy")) == (
        "codex via agy"
    )


VALIDATION_REPORT = (
    "VALID: NO\n"
    "  ROUTING: FAIL - next_agent is missing.\n"
    "  SHA: PASS - scope_sha is HEAD.\n"
)


def _codex_envelope(summary: str) -> DispatchResult:
    return DispatchResult(
        stdout=json.dumps({"status": "fail", "summary": summary}),
        stderr="",
        exit_code=0,
        elapsed_seconds=0.01,
    )


def test_codex_validator_output_is_unwrapped_for_the_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoke_mock = Mock(return_value=_codex_envelope(VALIDATION_REPORT))
    monkeypatch.setattr(agent_roles, "invoke_codex", invoke_mock)

    result = agent_roles.invoke_support_role(
        "handoff-validator",
        "Return ONLY the structured validation output.",
        role="validator",
        handoff_path=Path("docs/handoff/HANDOFF.md"),
        agent_config=AgentConfig(provider="codex"),
    )

    instruction = invoke_mock.call_args.kwargs["additional_instruction"]
    assert instruction.startswith("Return ONLY the structured validation output.")
    assert instruction.endswith(agent_roles.CODEX_SUPPORT_REPORT_INSTRUCTION)
    assert result.stdout == VALIDATION_REPORT
    parsed = parse_validation_output(result.stdout)
    assert parsed.verdict == "NO"
    assert parsed.errors == ["routing: next_agent is missing."]


def test_codex_plain_output_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A resumed Codex session runs without the output schema.
    plain = DispatchResult(
        stdout=VALIDATION_REPORT, stderr="", exit_code=0, elapsed_seconds=0.01
    )
    monkeypatch.setattr(agent_roles, "invoke_codex", Mock(return_value=plain))

    result = agent_roles.invoke_support_role(
        "handoff-validator",
        "Return ONLY the structured validation output.",
        role="validator",
        handoff_path=Path("docs/handoff/HANDOFF.md"),
        agent_config=AgentConfig(provider="codex"),
    )

    assert result.stdout == VALIDATION_REPORT


def test_codex_finalizer_report_is_unwrapped_for_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_handoff.config import DispatchConfig, _default_agent_configs
    from llm_handoff.ledger import run_epic_close

    commit_sha = "0123456789abcdef0123456789abcdef01234567"
    report = (
        "PROJECT STATE UPDATED: YES\n"
        "HANDOFF.MD REWRITTEN: YES\n"
        "SCOPE CLOSED: Example epic\n"
        "NEXT ROUTE: planner\n"
        "AUDIT SHA: 939702b\n"
        f"COMMIT SHA: {commit_sha}\n"
        "PUSH RESULT: SKIPPED (auto_push is false.)\n"
        "CHANGES MADE:\n"
        "  - PROJECT_STATE.md: recorded the epic close\n"
    )
    monkeypatch.setattr(
        agent_roles, "invoke_codex", Mock(return_value=_codex_envelope(report))
    )
    config = DispatchConfig(
        repo_root=tmp_path,
        agents={**_default_agent_configs(), "finalizer": AgentConfig(provider="codex")},
    )

    result = run_epic_close(config=config)

    assert result.parse_error is None
    assert result.project_state_updated is True
    assert result.commit_sha == commit_sha


def test_unwrap_codex_final_message_needs_a_full_envelope() -> None:
    from llm_handoff.agent_providers.codex import unwrap_codex_final_message

    assert unwrap_codex_final_message('{"status": "ok", "summary": "VALID: YES"}') == (
        "VALID: YES"
    )
    assert unwrap_codex_final_message('{"summary": "no status"}') == (
        '{"summary": "no status"}'
    )
    assert unwrap_codex_final_message("VALID: YES") == "VALID: YES"
    assert unwrap_codex_final_message("[1, 2]") == "[1, 2]"
