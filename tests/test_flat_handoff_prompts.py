from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from llm_handoff import ledger
from llm_handoff import orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import DispatchConfig
from llm_handoff.router import RoutingDecision
from llm_handoff.validator import ValidationResult


# CONFIGURATION.md advertises ``handoff_path: HANDOFF.md`` as a supported flat
# layout. Every prompt the dispatcher hands to a Claude-side agent must name
# the configured file, not the nested default.

NESTED = "docs/handoff/"


def _flat_config(tmp_path: Path, **kwargs: Any) -> DispatchConfig:
    repo_root = tmp_path / "flat-repo"
    repo_root.mkdir()
    (repo_root / "HANDOFF.md").write_text(
        """---
next_agent: auditor
reason: "Backend work complete; audit requested."
status: ready_for_review
scope_sha: 877f54d
close_type: story
producer: backend
---

# Backend Handback

**Agent:** backend
""",
        encoding="utf-8",
    )
    (repo_root / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    return DispatchConfig(
        repo_root=repo_root,
        handoff_path=Path("HANDOFF.md"),
        poll_interval_seconds=0,
        # Pauses exit here; pause recovery is covered in test_pause_recovery.py.
        pause_recovery="off",
        # These prompts are the single-auditor route's; the security reviewer
        # fan-out is covered in tests/test_audit_review.py.
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        **kwargs,
    )


def _subagent_result() -> SubagentResult:
    return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


def _capture_support_prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []

    def fake_support(subagent_name: str, prompt: str, **_kwargs: Any) -> SubagentResult:
        del subagent_name
        prompts.append(prompt)
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    return prompts


def _assert_flat(prompt: str) -> None:
    assert NESTED not in prompt, prompt
    assert "HANDOFF.md" in prompt, prompt


def _handoff_path_templates(module: object) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str) and "{handoff_path}" in value
    ]


def test_no_prompt_hardcodes_the_nested_handoff_path() -> None:
    for module in (orchestrator, ledger):
        assert NESTED + "HANDOFF.md" not in inspect.getsource(module), module
    # Every template that mentions the handoff file does so through the
    # placeholder, and renders cleanly for the flat layout.
    orchestrator_templates = _handoff_path_templates(orchestrator)
    assert {name for name, _ in orchestrator_templates} >= {
        "AUDIT_PROMPT_TEMPLATE",
        "MISROUTE_PROMPT_TEMPLATE",
        "HANDOFF_VALIDATOR_PROMPT_TEMPLATE",
        "STALE_ROUTE_VALIDATOR_PROMPT_TEMPLATE",
        "EPIC_CLOSE_VALIDATOR_PROMPT_TEMPLATE",
        "LOW_CONFIDENCE_ROUTE_VALIDATOR_PROMPT_TEMPLATE",
        "PLANNER_SELF_LOOP_VALIDATOR_PROMPT_TEMPLATE",
        "AGENT_SELF_LOOP_VALIDATOR_PROMPT_TEMPLATE",
        "POST_DISPATCH_MISSING_ROUTE_VALIDATOR_PROMPT_TEMPLATE",
        "POST_DISPATCH_HANDOFF_HYGIENE_REPAIR_PROMPT_TEMPLATE",
        "POST_DISPATCH_HANDOFF_HYGIENE_PLANNER_RECOVERY_TEMPLATE",
        "PRE_DISPATCH_INVALID_FRONTMATTER_PLANNER_RECOVERY_TEMPLATE",
        "PRE_DISPATCH_INVALID_PLANNER_FRONTMATTER_TEMPLATE",
        "RATIONALIZATION_RECOVERY_INSTRUCTION_TEMPLATE",
    }
    assert {name for name, _ in _handoff_path_templates(ledger)} == {
        "LEDGER_UPDATER_PROMPT_TEMPLATE",
        "_AGENT_COMMIT_INSTRUCTION",
        "_DISPATCHER_COMMIT_INSTRUCTION",
    }
    for commit_mode in ("agent", "dispatcher"):
        _assert_flat(ledger.ledger_updater_prompt(Path("HANDOFF.md"), commit_mode))


def test_render_prompt_uses_posix_relative_handoff_path() -> None:
    rendered = orchestrator._render_prompt(
        orchestrator.AUDIT_PROMPT_TEMPLATE,
        Path("docs") / "handoff" / "HANDOFF.md",
    )
    assert "docs/handoff/HANDOFF.md" in rendered
    assert "\\" not in rendered
    _assert_flat(
        orchestrator._render_prompt(
            orchestrator.AUDIT_PROMPT_TEMPLATE,
            Path("HANDOFF.md"),
        )
    )


@pytest.mark.parametrize("route", ["auditor", "validator"])
def test_dispatch_route_prompts_use_flat_handoff_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    config = _flat_config(tmp_path)
    prompts = _capture_support_prompts(monkeypatch)

    orchestrator._dispatch_route(route, config, lambda level, message: None)

    assert len(prompts) == 1
    _assert_flat(prompts[0])


def test_validator_prompts_use_flat_handoff_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _flat_config(tmp_path)
    prompts = _capture_support_prompts(monkeypatch)
    log = Mock()
    decision = RoutingDecision(
        route="backend",
        confidence="LOW",
        source="prose_next_agent",
        reasoning="Prose mentions backend.",
        warnings=[],
    )

    orchestrator._run_unknown_route_validator(config, log)
    orchestrator._run_stale_route_validator(config, "backend", log)
    orchestrator._run_epic_close_validator(config, "Missing COMMIT SHA line.", log)
    orchestrator._run_low_confidence_route_validator(config, decision, log)
    orchestrator._run_self_loop_validator(
        config=config, previous_agent="planner", self_loop_kind="planner", log=log
    )
    orchestrator._run_self_loop_validator(
        config=config, previous_agent="backend", self_loop_kind="agent", log=log
    )
    orchestrator._run_post_dispatch_missing_route_validator(
        config=config, previous_agent="backend", log=log
    )

    assert len(prompts) == 7
    for prompt in prompts:
        _assert_flat(prompt)


def test_hygiene_repair_prompt_uses_flat_handoff_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _flat_config(tmp_path)
    captured: list[str] = []

    def fake_backend(path: Path, **kwargs: Any) -> DispatchResult:
        del path
        captured.append(str(kwargs["additional_instruction"]))
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.0)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    validation_result = ValidationResult(
        verdict="NO",
        warnings=[],
        errors=[
            "scope_claim_missing: Handoff is missing an **Agent:** ownership line."
        ],
        routing_instruction="auditor",
    )

    orchestrator._dispatch_handoff_hygiene_repair(
        config=config,
        previous_agent="backend",
        validation_result=validation_result,
        log=lambda level, message: None,
    )

    assert len(captured) == 1
    assert captured[0].startswith("Repair only HANDOFF.md from")
    _assert_flat(captured[0])


def test_recovery_instructions_use_flat_handoff_path() -> None:
    decision = RoutingDecision(
        route="unknown",
        confidence="LOW",
        source="frontmatter.invalid",
        reasoning="Frontmatter is missing reason.",
        warnings=["Invalid HANDOFF routing frontmatter: reason is required."],
    )
    planner_handoff = "---\nnext_agent: planner\nproducer: user\n---\n"
    backend_handoff = "---\nnext_agent: backend\nproducer: planner\n---\n"

    planner_recovery = orchestrator._pre_dispatch_invalid_frontmatter_recovery(
        decision,
        planner_handoff,
        handoff_path=Path("HANDOFF.md"),
    )
    backend_recovery = orchestrator._pre_dispatch_invalid_frontmatter_recovery(
        decision,
        backend_handoff,
        handoff_path=Path("HANDOFF.md"),
    )

    assert planner_recovery is not None
    assert backend_recovery is not None
    _assert_flat(planner_recovery.additional_instruction)
    _assert_flat(backend_recovery.additional_instruction)


def test_run_loop_repair_lane_uses_flat_handoff_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _flat_config(tmp_path)
    handoff_path = config.handoff_full_path
    log_messages: list[tuple[str, str]] = []
    prompts: list[str] = []
    invalid_handoff = """---
next_agent: planner
reason: Story audit approved; scope next story.
scope_sha: 59206fb
close_type: story
producer: auditor
---

# Auditor Handback

## Audit Summary

Audit complete with verification.

## Prior Backend Handback

**Agent:** backend
"""
    repaired_handoff = invalid_handoff.replace(
        "# Auditor Handback\n",
        "# Auditor Handback\n\n**Agent:** auditor\n",
    )

    def fake_support(subagent_name: str, prompt: str, **_kwargs: Any) -> SubagentResult:
        del subagent_name
        prompts.append(prompt)
        handoff_path.write_text(
            invalid_handoff if len(prompts) == 1 else repaired_handoff,
            encoding="utf-8",
        )
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(
        orchestrator, "_git_head", Mock(side_effect=["a" * 40, "b" * 40])
    )
    monkeypatch.setattr(
        orchestrator, "_git_dirty_files", Mock(side_effect=[("HANDOFF.md",), ()])
    )
    monkeypatch.setattr(
        orchestrator, "_git_changed_files_since", Mock(return_value=("HANDOFF.md",))
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(prompts) == 2
    for prompt in prompts:
        _assert_flat(prompt)
    assert (
        "AGENT",
        "Running one-shot producer repair for HANDOFF.md only.",
    ) in log_messages
    assert ("AGENT", "Post-dispatch gate PASSED for auditor (audit).") in log_messages


def test_epic_close_prompt_uses_flat_handoff_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _flat_config(tmp_path)
    captured: list[str] = []

    def fake_support(subagent_name: str, prompt: str, **_kwargs: Any) -> SubagentResult:
        del subagent_name
        captured.append(prompt)
        return SubagentResult(
            stdout="""PROJECT STATE UPDATED: YES
HANDOFF.MD REWRITTEN: YES
SCOPE CLOSED: Example epic
NEXT ROUTE: planner
AUDIT SHA: 939702b
COMMIT SHA: 0123456789abcdef0123456789abcdef01234567
PUSH RESULT: SKIPPED
CHANGES MADE:
  - PROJECT_STATE.md: appended the epic closure entry
""",
            stderr="",
            exit_code=0,
            elapsed_seconds=0.1,
        )

    monkeypatch.setattr(ledger, "invoke_support_role", fake_support)

    result = ledger.run_epic_close(config=config)

    assert result.subagent_exit_code == 0
    assert len(captured) == 1
    _assert_flat(captured[0])
    assert captured[0] == ledger.ledger_updater_prompt(Path("HANDOFF.md"))
    assert ledger.LEDGER_UPDATER_PROMPT == ledger.ledger_updater_prompt()
