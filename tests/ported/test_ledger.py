from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

import llm_handoff.ledger as ledger
from llm_handoff.agents import SubagentResult


FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
AUDIT_SHA = "939702b"
EXPECTED_PROMPT = ledger.LEDGER_UPDATER_PROMPT


def _ledger_output(*, push_result: str) -> str:
    return f"""PROJECT STATE UPDATED: YES
HANDOFF.MD REWRITTEN: YES
SCOPE CLOSED: Security & Supply Chain Hardening (Semgrep)
NEXT ROUTE: planner
AUDIT SHA: {AUDIT_SHA}
COMMIT SHA: {FULL_SHA}
PUSH RESULT: {push_result}
CHANGES MADE:
  - PROJECT_STATE.md: appended the epic closure entry
  - PROJECT_STATE.md: advanced the active epic pointer
  - docs/handoff/HANDOFF.md: routed the next cycle to planner
"""


def test_run_epic_close_invokes_ledger_updater_and_parses_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()
    invoke_mock = Mock(
        return_value=SubagentResult(
            stdout=_ledger_output(push_result="PUSHED (origin/main now matches HEAD.)"),
            stderr="",
            exit_code=0,
            elapsed_seconds=2.5,
        )
    )

    monkeypatch.setattr(ledger, "invoke_claude_subagent", invoke_mock)

    result = ledger.run_epic_close(log=log_mock)

    assert result.subagent_exit_code == 0
    assert result.commit_sha == FULL_SHA
    assert result.push_status == "PUSHED"
    assert result.push_detail == "origin/main now matches HEAD."
    assert result.ledger_updated is True
    assert result.claude_md_updated is True
    assert result.handoff_rewritten is True
    assert result.epic_closed == "Security & Supply Chain Hardening (Semgrep)"
    assert result.next_epic == "planner"
    assert result.audit_sha == AUDIT_SHA
    invoke_mock.assert_called_once_with(
        subagent_name="ledger-updater",
        prompt=EXPECTED_PROMPT,
        log=log_mock,
    )


def test_run_epic_close_logs_exit_code_to_dispatch_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()
    invoke_mock = Mock(
        return_value=SubagentResult(
            stdout=_ledger_output(push_result="PUSHED (origin/main now matches HEAD.)"),
            stderr="",
            exit_code=0,
            elapsed_seconds=2.5,
        )
    )

    monkeypatch.setattr(ledger, "invoke_claude_subagent", invoke_mock)

    ledger.run_epic_close(log=log_mock)

    assert ("AGENT", "Subagent ledger-updater exited with code 0") in [
        call.args for call in log_mock.call_args_list
    ]


def test_ledger_updater_prompt_uses_public_finalizer_contract() -> None:
    assert "PROJECT STATE UPDATED: YES or NO" in EXPECTED_PROMPT
    assert "SCOPE CLOSED: <scope name>" in EXPECTED_PROMPT
    assert "NEXT ROUTE: <planner, user, or another supported next_agent>" in EXPECTED_PROMPT
    assert "PUSH RESULT: SKIPPED, PUSHED, or FAILED" in EXPECTED_PROMPT
    assert "Do not push unless the repository instructions explicitly allow" in EXPECTED_PROMPT


def test_run_epic_close_parses_audit_sha_from_rich_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()
    invoke_mock = Mock(
        return_value=SubagentResult(
            stdout="""LEDGER UPDATED: YES
PROJECT_STATE.MD UPDATED: YES
HANDOFF.MD REWRITTEN: NO
EPIC CLOSED: Dispatch Gemini Stream-JSON + Default Codex Resume
NEXT EPIC (routed to Gemini-PE): None (Gemini-PE to scope from docs/uat/EPICS_UAT_REMEDIATION_2026-04-18.md)
AUDIT SHA: 82ce839 (impl), 3407c66 (tests), audit recorded at 15d9118
COMMIT SHA: bc1d3d5
PUSH RESULT: PUSHED (2ead6e2..bc1d3d5 main -> main)
CHANGES MADE:
- PROJECT_STATE.md: appended one-line entry for Dispatch Gemini Stream-JSON + Default Codex Resume
- PROJECT_STATE.md: advanced active epic pointer to None — awaiting next epic dispatch (Gemini-PE scoping)
""",
            stderr="",
            exit_code=0,
            elapsed_seconds=2.5,
        )
    )

    monkeypatch.setattr(ledger, "invoke_claude_subagent", invoke_mock)

    result = ledger.run_epic_close(log=log_mock)

    assert result.subagent_exit_code == 0
    assert result.audit_sha == "15d9118"
    assert result.commit_sha == "bc1d3d5"
    assert result.push_status == "PUSHED"
    assert result.handoff_rewritten is False
    assert (
        "WARN",
        "ledger-updater reported HANDOFF.MD REWRITTEN: NO; dispatcher will treat repeated finalizer routing as stale and redirect to the planner.",
    ) in [call.args for call in log_mock.call_args_list]


def test_run_epic_close_parses_first_commit_sha_from_rich_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()
    invoke_mock = Mock(
        return_value=SubagentResult(
            stdout="""LEDGER UPDATED: YES
PROJECT_STATE.MD UPDATED: YES
HANDOFF.MD REWRITTEN: YES
EPIC CLOSED: Post-UAT Audit Nit Cleanup
NEXT EPIC (routed to Gemini-PE): None — awaiting next epic dispatch (Gemini-PE scoping)
AUDIT SHA: 4fd7b14
COMMIT SHA: 4281d3a (ledger/PROJECT_STATE.md/HANDOFF.md), 32a5771 (scope_sha patch)
PUSH RESULT: PUSHED (84e3854..32a5771 main -> main)
CHANGES MADE:
- PROJECT_STATE.md: appended one compact line for Post-UAT Audit Nit Cleanup
- PROJECT_STATE.md: Active Epic set to None — awaiting next epic dispatch
- docs/handoff/HANDOFF.md: rewritten with YAML frontmatter routing to gemini-pe
""",
            stderr="",
            exit_code=0,
            elapsed_seconds=2.5,
        )
    )

    monkeypatch.setattr(ledger, "invoke_claude_subagent", invoke_mock)

    result = ledger.run_epic_close(log=log_mock)

    assert result.subagent_exit_code == 0
    assert result.parse_error is None
    assert result.commit_sha == "4281d3a"
    assert result.push_status == "PUSHED"
    assert result.push_detail == "84e3854..32a5771 main -> main"


def test_run_epic_close_treats_unparseable_output_as_failure_and_uses_dispatch_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()
    invoke_mock = Mock(
        return_value=SubagentResult(
            stdout="ledger updater wrote prose instead of structured fields",
            stderr="",
            exit_code=0,
            elapsed_seconds=2.5,
        )
    )
    logger_error_mock = Mock()
    logger_warning_mock = Mock()
    logger_info_mock = Mock()

    monkeypatch.setattr(ledger, "invoke_claude_subagent", invoke_mock)
    monkeypatch.setattr(ledger.logger, "error", logger_error_mock)
    monkeypatch.setattr(ledger.logger, "warning", logger_warning_mock)
    monkeypatch.setattr(ledger.logger, "info", logger_info_mock)

    result = ledger.run_epic_close(log=log_mock)

    assert result.subagent_exit_code == 1
    assert result.commit_sha is None
    assert (
        "ERROR",
        "ledger-updater returned unparseable output: Missing PROJECT STATE UPDATED line.",
    ) in [call.args for call in log_mock.call_args_list]
    assert (
        "WARN",
        "ledger-updater stdout (first 500 chars): ledger updater wrote prose instead of structured fields",
    ) in [call.args for call in log_mock.call_args_list]
    logger_error_mock.assert_not_called()
    logger_warning_mock.assert_not_called()
    logger_info_mock.assert_not_called()


def test_run_epic_close_logs_push_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    invoke_mock = Mock(
        return_value=SubagentResult(
            stdout=_ledger_output(
                push_result="FAILED (ssh timeout while pushing origin/main)"
            ),
            stderr="",
            exit_code=0,
            elapsed_seconds=2.5,
        )
    )

    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(ledger, "invoke_claude_subagent", invoke_mock)

    result = ledger.run_epic_close()

    assert result.commit_sha == FULL_SHA
    assert result.push_status == "FAILED"
    assert result.push_detail == "ssh timeout while pushing origin/main"
    assert invoke_mock.call_count == 1
    assert "push failed" in caplog.text.lower()


def test_run_epic_close_returns_failed_result_when_subagent_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    invoke_mock = Mock(
        return_value=SubagentResult(
            stdout="",
            stderr="claude crashed",
            exit_code=17,
            elapsed_seconds=1.5,
        )
    )

    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(ledger, "invoke_claude_subagent", invoke_mock)

    result = ledger.run_epic_close()

    assert result.subagent_exit_code == 17
    assert result.commit_sha is None
    assert result.push_status == "UNKNOWN"
    assert result.push_detail is None
    assert "ledger-updater failed" in caplog.text.lower()
