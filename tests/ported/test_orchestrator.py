from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from llm_handoff.agents import DispatchResult, SubagentResult
from llm_handoff.ledger import EpicCloseResult
import llm_handoff.ledger as ledger
from llm_handoff.validator import ValidationResult


def _load_orchestrator_module():
    return importlib.import_module("llm_handoff.orchestrator")


def _load_main_module():
    return importlib.import_module("llm_handoff.__main__")


def _dispatch_config(
    repo_root: Path,
    *,
    dry_run: bool = False,
    use_manual_frontend: bool = False,
    backend_resume: bool = True,
    planner_resume: bool = True,
    poll_interval_seconds: int = 0,
    max_consecutive_failures: int = 3,
    agents: dict[str, Any] | None = None,
    pause_recovery: str = "off",
):
    config_module = importlib.import_module("llm_handoff.config")
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        # These tests expect pauses to exit; bounded planner recovery at
        # pauses is covered in tests/test_pause_recovery.py.
        "pause_recovery": pause_recovery,
        "dry_run": dry_run,
        "use_manual_frontend": use_manual_frontend,
        "backend_resume": backend_resume,
        "planner_resume": planner_resume,
        "poll_interval_seconds": poll_interval_seconds,
        "max_consecutive_failures": max_consecutive_failures,
        # These tests cover the single-auditor route; the security reviewer
        # fan-out is covered in tests/test_audit_review.py.
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        # These tests route handoffs without a Work Packet; the work-claim
        # gate is covered in tests/test_work_claim.py.
        "enforce_work_packet": False,
        "work_packet_opt_out_reason": "Tests cover routing without the gate.",
    }
    if agents is not None:
        kwargs["agents"] = agents
    return config_module.DispatchConfig(**kwargs)


def _write_repo(
    tmp_path: Path,
    handoff_content: str,
    *,
    project_state_content: str = "# PROJECT STATE\n",
) -> tuple[Path, Path]:
    repo_root = tmp_path
    handoff_path = repo_root / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(handoff_content, encoding="utf-8")
    (repo_root / "PROJECT_STATE.md").write_text(
        project_state_content,
        encoding="utf-8",
    )
    (repo_root / "AGENTS.md").write_text("dispatch test fixture\n", encoding="utf-8")
    return repo_root, handoff_path


# An operator's fix made while the dispatcher pauses. An operator edit
# without valid frontmatter goes to the planner for interpretation (HUMAN-2),
# so a fix meant to route straight to backend carries frontmatter.
_OPERATOR_BACKEND_FIX = """---
next_agent: backend
reason: "Implement Story 1."
producer: user
---

## Next Step

- **backend:** Implement Story 1.
"""


def test_read_required_text_accepts_utf16_le_bom(tmp_path: Path) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text("# Frontend Handback\n", encoding="utf-16")
    orchestrator = _load_orchestrator_module()

    assert orchestrator._read_required_text(handoff_path) == "# Frontend Handback\n"


def _dispatch_result(*, exit_code: int = 0) -> DispatchResult:
    return DispatchResult(
        stdout="",
        stderr="",
        exit_code=exit_code,
        elapsed_seconds=0.01,
    )


def _subagent_result(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> SubagentResult:
    return SubagentResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        elapsed_seconds=0.01,
    )


def _validation_result(verdict: str = "YES") -> ValidationResult:
    return ValidationResult(
        verdict=verdict,
        warnings=[],
        errors=[],
        routing_instruction="backend",
    )


def test_orchestrator_routes_rationalization_to_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: backend
reason: Implement the backend change.
story_id: rationalization-story
producer: planner
---

## Task Assignment

**Agent:** backend
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    dispatches: list[str] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        path.write_text(
            """---
next_agent: auditor
reason: "Backend work complete; audit requested."
status: ready_for_review
story_id: rationalization-story
scope_sha: 877f54d
close_type: story
producer: backend
---

# Backend Handback

**Agent:** backend

Let me first explore the codebase before writing the fix.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    def fake_planner(path: Path, **kwargs: object) -> DispatchResult:
        dispatches.append("planner")
        assert (
            "rationalization" in str(kwargs.get("additional_instruction", "")).lower()
        )
        path.write_text(
            """---
next_agent: user
reason: "Human review required."
producer: planner
---

## user

Recovery complete.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    validation_results = [
        ValidationResult(
            verdict="WARNINGS-ONLY",
            warnings=["RATIONALIZATION_DETECTED: line 13: `Let me first explore`"],
            errors=[],
            routing_instruction="auditor",
        ),
        _validation_result(),
    ]

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: validation_results.pop(0),
    )

    exit_code = orchestrator.run_loop(config, max_cycles=2)

    assert exit_code == 0
    assert dispatches == ["backend", "planner"]
    assert "next_agent: user" in handoff_path.read_text(encoding="utf-8")


def test_orchestrator_three_fix_circuit_breaker(
    tmp_path: Path,
) -> None:
    story_id = "circuit-story"
    ledger.reset_bounce(story_id)
    for _ in range(3):
        ledger.increment_bounce(story_id)
    repo_root, _handoff_path = _write_repo(
        tmp_path,
        f"""---
next_agent: backend
reason: Retry implementation.
story_id: {story_id}
producer: auditor
---

## Retry
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert ("INFO", "Routing instruction: planner") in log_messages
    assert any("THREE_FIX_CIRCUIT_BREAKER" in message for _, message in log_messages)
    ledger.reset_bounce(story_id)


def test_orchestrator_post_planner_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    story_id = "circuit-story-escalate"
    ledger.reset_bounce(story_id)
    for _ in range(3):
        ledger.increment_bounce(story_id)
    repo_root, handoff_path = _write_repo(
        tmp_path,
        f"""---
next_agent: backend
reason: Retry implementation.
story_id: {story_id}
producer: auditor
---

## Retry
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    dispatches: list[str] = []
    log_messages: list[tuple[str, str]] = []

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("planner")
        path.write_text(
            f"""---
next_agent: backend
reason: Planner re-scoped but still returned to implementation.
story_id: {story_id}
producer: planner
---

## Task Assignment

**Agent:** backend
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator, "validate_handoff", lambda *a, **k: _validation_result()
    )
    monkeypatch.setattr(
        orchestrator,
        "_pause_until_handoff_changes",
        lambda *a, **k: False,
    )

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert dispatches == ["planner"]
    assert any(
        "THREE_FIX_CIRCUIT_BREAKER_ESCALATE" in message for _, message in log_messages
    )
    assert "next_agent: backend" in handoff_path.read_text(encoding="utf-8")
    ledger.reset_bounce(story_id)


def test_run_loop_dispatches_only_one_agent_per_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Implement Story 5.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    dispatches: list[str] = []
    log_messages: list[tuple[str, str]] = []

    def fake_codex(path: Path, *, log=None, use_resume=False) -> DispatchResult:
        assert path == handoff_path
        assert callable(log)
        assert use_resume is True
        dispatches.append("backend")
        handoff_path.write_text(
            """## Next Step

- **auditor:** Audit Story 5.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert prompt
        assert callable(log)
        dispatches.append(subagent_name)
        handoff_path.write_text(
            """## user

Manual handoff review required.
""",
            encoding="utf-8",
        )
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_codex)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert dispatches == ["backend", "auditor"]
    assert any("Routing instruction: backend" in message for _, message in log_messages)
    assert any("Routing instruction: auditor" in message for _, message in log_messages)


def test_run_loop_logs_dispatch_progress_for_completed_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl_sha = "9482fcf8cb0eb1099ef90b02fe2a8238c551d383"
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Implement Story 5.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []

    def fake_codex(path: Path, *, log=None, use_resume=False) -> DispatchResult:
        assert path == handoff_path
        assert callable(log)
        assert use_resume is True
        handoff_path.write_text(
            f"""# backend Handback

**Agent:** backend
**Latest Commit SHA:** `{impl_sha}`

## Summary

Completed work.

## Next Step

- **auditor:** Audit Story 5.
""",
            encoding="utf-8",
        )
        return _dispatch_result(exit_code=0)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_codex)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert ("DISPATCH", "Dispatching backend.") in log_messages
    assert ("INFO", "backend exited with code 0") in log_messages
    assert (
        "INFO",
        f"backend updated {handoff_path} (hash changed)",
    ) in log_messages
    assert (
        "INFO",
        "New SHA(s) found in handoff file: 9482fcf8cb0e... (1 added)",
    ) in log_messages
    assert ("AGENT", "Running post-dispatch validation for backend...") in log_messages
    assert ("AGENT", "Handoff validation verdict: YES") in log_messages
    assert ("AGENT", "Post-dispatch gate PASSED for backend.") in log_messages


def test_run_loop_logs_handoff_scope_metadata(
    tmp_path: Path,
) -> None:
    repo_root, _ = _write_repo(
        tmp_path,
        """---
next_agent: backend
reason: Implement synthesis schema story.
epic_id: E-SYN-1
story_id: E-SYN-1-S1
story_title: Synthesis Schema Update
remaining_stories:
  - E-SYN-1-S2 HTML Export Template Redesign
producer: planner
---

## Task Assignment

**Agent:** backend
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert (
        "INFO",
        "Handoff scope: epic=E-SYN-1; story=E-SYN-1-S1 (Synthesis Schema Update); remaining=E-SYN-1-S2 HTML Export Template Redesign",
    ) in log_messages


def test_run_loop_repairs_malformed_reason_frontmatter_before_routing(
    tmp_path: Path,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: backend
reason: Dispatch E2-S1: Implement backend as_completed loop.
producer: planner
---

## Task Assignment

**Agent:** backend
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert ("INFO", "Routing instruction: backend") in log_messages
    assert (
        "WARN",
        "Auto-repaired HANDOFF YAML frontmatter by quoting reason.",
    ) in log_messages
    assert "Dispatch E2-S1: Implement backend as_completed loop." in (
        handoff_path.read_text(encoding="utf-8")
    )


def test_run_loop_skips_next_agent_normalizer_for_exact_frontmatter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: backend
reason: Implement the next backend story.
producer: planner
---

## Task Assignment

**Agent:** backend
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)

    def fail_normalizer(_freeform: str) -> str:
        raise AssertionError("canonical next_agent should not call the normalizer")

    monkeypatch.setattr(orchestrator, "normalize_next_agent", fail_normalizer)

    assert orchestrator.run_loop(config, max_cycles=1) == 0


def test_run_loop_normalizes_fuzzy_next_agent_before_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: auditor (Auditor)
reason: Frontend implementation complete; audit requested.
scope_sha: 25c45ca
close_type: story
producer: frontend
---

## Implementer Handback

**Agent:** frontend
**Latest verified repo SHA:** `25c45ca`

## Completed Work

- Built the Round History panel.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    log_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "normalize_next_agent",
        lambda freeform, **_kwargs: (
            "auditor" if freeform == "auditor (Auditor)" else "unknown"
        ),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert ("INFO", "Routing instruction: auditor") in log_messages
    assert (
        "WARN",
        "router: next_agent 'auditor (Auditor)' is not a deterministic enum match; invoking normalizer.",
    ) in log_messages
    assert (
        "INFO",
        "router: next_agent normalizer returned 'auditor' for 'auditor (Auditor)'.",
    ) in log_messages
    assert (
        "INFO",
        "router: rewrote next_agent to deterministic output 'auditor' in HANDOFF.md.",
    ) in log_messages
    handoff_content = handoff_path.read_text(encoding="utf-8")
    assert "next_agent: auditor\n" in handoff_content
    assert (
        "reason: Frontend implementation complete; audit requested.\n"
        in handoff_content
    )
    assert "scope_sha: 25c45ca\n" in handoff_content


def test_run_loop_pauses_when_next_agent_normalizer_returns_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: banana
reason: Invalid route.
producer: planner
---

## Task Assignment

**Agent:** RouterBot
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    log_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "normalize_next_agent",
        lambda _freeform, **_kwargs: "unknown",
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_unknown_route_validator",
        lambda _config, _log: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_pause_until_handoff_changes",
        lambda *_args, **_kwargs: False,
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert (
        "WARN",
        "router: next_agent 'banana' is not a deterministic enum match; invoking normalizer.",
    ) in log_messages
    assert (
        "INFO",
        "router: next_agent normalizer returned 'unknown' for 'banana'.",
    ) in log_messages
    assert (
        "WARN",
        "router: next_agent 'banana' could not be normalized; leaving HANDOFF.md unchanged.",
    ) in log_messages


def test_run_loop_accepts_utf16_manual_frontend_handback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl_sha = "236f82f812e405c595dd3fb194c98d8f1d11d89e"
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: frontend
reason: Frontend implementation requested.
producer: planner
---

## Task Assignment

**Agent:** frontend
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, use_manual_frontend=True)
    log_messages: list[tuple[str, str]] = []

    def fake_manual_frontend(
        path: Path,
        *,
        use_manual_frontend=False,
        additional_instruction=None,
        log=None,
    ) -> DispatchResult:
        assert path == handoff_path
        assert callable(log)
        handoff_path.write_text(
            f"""---
next_agent: auditor
reason: E1-S6 frontend implementation complete; audit requested.
scope_sha: 236f82f
close_type: story
producer: frontend
---

# E1-S6 Frontend Handback

**Agent:** frontend (manual frontend)
**Latest verified repo SHA:** `{impl_sha}`

## Completed Work

- Added the frontend stale-state signaling UI.

## Verification

- `npm test`
- `npm run build`
""",
            encoding="utf-16",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_frontend_role", fake_manual_frontend)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert ("INFO", "Routing instruction: frontend") in log_messages
    assert ("INFO", "frontend exited with code 0") in log_messages
    assert ("AGENT", "Handoff validation verdict: YES") in log_messages


def test_run_loop_normalizes_post_dispatch_handoff_before_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl_sha = "236f82f812e405c595dd3fb194c98d8f1d11d89e"
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: frontend
reason: Frontend implementation requested.
producer: planner
---

## Task Assignment

**Agent:** frontend
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, use_manual_frontend=True)
    log_messages: list[tuple[str, str]] = []

    def fake_manual_frontend(
        path: Path,
        *,
        use_manual_frontend=False,
        additional_instruction=None,
        log=None,
    ) -> DispatchResult:
        assert path == handoff_path
        handoff_path.write_text(
            f"""---
next_agent: auditor (Auditor)
reason: E1-S6 frontend implementation complete; audit requested.
scope_sha: 236f82f
close_type: story
producer: frontend
---

# E1-S6 Frontend Handback

**Agent:** frontend (manual frontend)
**Latest verified repo SHA:** `{impl_sha}`

## Completed Work

- Added the frontend stale-state signaling UI.

## Verification

- `npm test`
- `npm run build`
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_frontend_role", fake_manual_frontend)
    monkeypatch.setattr(
        orchestrator,
        "normalize_next_agent",
        lambda freeform, **_kwargs: (
            "auditor" if freeform == "auditor (Auditor)" else "unknown"
        ),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert (
        "WARN",
        "router: next_agent 'auditor (Auditor)' is not a deterministic enum match; invoking normalizer.",
    ) in log_messages
    assert (
        "INFO",
        "router: next_agent normalizer returned 'auditor' for 'auditor (Auditor)'.",
    ) in log_messages
    assert (
        "INFO",
        "router: rewrote next_agent to deterministic output 'auditor' in HANDOFF.md.",
    ) in log_messages
    assert ("AGENT", "Handoff validation verdict: YES") in log_messages
    assert "next_agent: auditor\n" in handoff_path.read_text(encoding="utf-8")
    assert (
        "AGENT",
        "Post-dispatch gate PASSED for frontend.",
    ) in log_messages


@pytest.mark.parametrize(
    ("handoff_content", "expected_fragment"),
    [
        pytest.param(
            """## user

Human review needed.
""",
            "ESCALATION DETECTED",
            id="escalation",
        ),
        pytest.param(
            """# No routing data

There is no next step in this file.
""",
            "No routing instruction found",
            id="unknown",
        ),
    ],
)
def test_run_loop_pauses_cleanly_for_escalation_or_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handoff_content: str,
    expected_fragment: str,
) -> None:
    repo_root, _ = _write_repo(tmp_path, handoff_content)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    codex_calls: list[Path] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: (
            codex_calls.append(path) or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: _subagent_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert codex_calls == []
    assert any(expected_fragment in message for _, message in log_messages)


def test_run_loop_sends_a_free_form_edit_at_a_pause_to_the_planner_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    escalation = """---
next_agent: user
reason: "Human decision required."
status: escalate_to_user
producer: planner
---

## Question

Which story should run next?
"""
    repo_root, handoff_path = _write_repo(tmp_path, escalation)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, poll_interval_seconds=1)
    log_messages: list[tuple[str, str]] = []
    sleeps: list[float] = []
    planner_instructions: list[str | None] = []

    class _StopPolling(Exception):
        pass

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            handoff_path.write_text("I made the edits.\n", encoding="utf-8")
            return
        raise _StopPolling

    def fake_planner(
        path: Path, additional_instruction: str | None = None, log=None, **kwargs
    ) -> DispatchResult:
        del log, kwargs
        planner_instructions.append(additional_instruction)
        # The planner asks the operator one plain question.
        path.write_text(
            escalation.replace(
                "Which story should run next?", "Did you commit the edits?"
            ),
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator.time, "sleep", sleep)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: _subagent_result(),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[]
        ),
    )

    with pytest.raises(_StopPolling):
        orchestrator.run_loop(
            config,
            log=lambda level, message: log_messages.append((level, message)),
        )

    assert log_messages.count(("INFO", "Operator edited HANDOFF.md.")) == 1
    # HUMAN-2: the free-form edit goes to the planner once, not to the
    # unknown-route pause or a cleanup pass.
    assert len(planner_instructions) == 1
    instruction = planner_instructions[0] or ""
    assert "<<<OPERATOR MESSAGE>>>\nI made the edits.\n\n<<<END OPERATOR MESSAGE>>>" in (
        instruction
    )
    assert ("INFO", "Routing source: operator_edit_interpretation") in log_messages
    assert not any(
        "No routing instruction found" in message for _, message in log_messages
    )
    # The save failed, so the snapshot path is not offered as this edit's.
    assert "It could not be saved to" in instruction
    assert "<<<PREVIOUS HANDOFF>>>" in instruction
    # This checkout is not a Git repository, so nothing is saved, and the
    # log says why instead of claiming a snapshot.
    assert not (
        repo_root / "logs" / "dispatch" / "operator-edit" / "previous-handoff.md"
    ).exists()
    assert not any(
        message.startswith("Saved the previous valid handoff")
        for _, message in log_messages
    )
    assert any(
        level == "WARN"
        and message.startswith("Could not save the previous valid handoff")
        for level, message in log_messages
    )


def test_run_loop_runs_handoff_validator_before_pausing_on_unknown_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _ = _write_repo(
        tmp_path,
        """# No routing data

There is no next step in this file.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: (
            subagent_calls.append((subagent_name, prompt, log))
            or _subagent_result(
                stdout="""VALID: NO
  ROUTING: FAIL - HANDOFF does not provide a dispatchable next step.
  SHA-PRESENT: WARN - Planner handoff does not yet include a git commit SHA.
"""
            )
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: _dispatch_result(),
    )

    def fake_log(level: str, message: str) -> None:
        log_messages.append((level, message))

    exit_code = orchestrator.run_loop(config, log=fake_log)

    assert exit_code == 0
    assert len(subagent_calls) == 1
    assert subagent_calls[0][0] == "handoff-validator"
    assert "could not find a dispatchable routing instruction" in subagent_calls[0][1]
    assert subagent_calls[0][2] is fake_log
    assert (
        "AGENT",
        "No dispatchable route found. Invoking the validator role...",
    ) in log_messages
    assert ("AGENT", "Handoff-validator exited with code 0") in log_messages
    assert ("AGENT", "Handoff validation verdict: NO") in log_messages
    assert (
        "ERROR",
        "routing: HANDOFF does not provide a dispatchable next step.",
    ) in log_messages
    assert (
        "WARN",
        "sha-present: Planner handoff does not yet include a git commit SHA.",
    ) in log_messages
    assert (
        "PAUSE",
        "No routing instruction found in HANDOFF.md. Update the handoff; dispatch will resume after the file changes.",
    ) in log_messages


def test_run_loop_dispatches_parseable_invalid_planner_frontmatter_as_planner_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: planner
producer: auditor
---

# Auditor Handback

Diagnostic commit SHA: 476c2b0.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    planner_calls: list[str | None] = []

    def fail_support_role(*_args: Any, **_kwargs: Any) -> SubagentResult:
        raise AssertionError("startup recovery should not invoke handoff-validator")

    def fake_planner(
        path: Path,
        *,
        additional_instruction: str | None = None,
        log=None,
        **_kwargs: Any,
    ) -> DispatchResult:
        assert path == handoff_path
        assert callable(log)
        planner_calls.append(additional_instruction)
        path.write_text(
            """---
next_agent: backend
reason: Continue with the scoped backend implementation.
producer: planner
---

## Task Assignment

**Agent:** backend

### Objective
Continue the intended backend route.

### Acceptance Criteria
- Keep the route dispatchable.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fail_support_role)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(planner_calls) == 1
    assert "normal planner planning/scoping dispatch" in (planner_calls[0] or "")
    assert "Do not stop after only fixing frontmatter" in (planner_calls[0] or "")
    assert "reason is required" in (planner_calls[0] or "")
    assert (
        "WARN",
        "HANDOFF.md routes to planner but has invalid frontmatter; dispatching planner normally with frontmatter repair instruction.",
    ) in log_messages
    assert (
        "INFO",
        "Routing source: pre_dispatch_planner_frontmatter_repair",
    ) in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages
    assert ("DISPATCH", "Dispatching planner.") in log_messages


def test_status_next_agent_conflict_dispatches_planner_instead_of_pausing() -> None:
    orchestrator = _load_orchestrator_module()
    from llm_handoff.router import route

    handoff_content = """---
next_agent: planner
reason: "Work-claim gate failed; planner should re-scope."
status: blocked_missing_context
producer: backend
---

# backend Handback
"""

    decision = route(handoff_content)
    recovery = orchestrator._pre_dispatch_invalid_frontmatter_recovery(
        decision,
        handoff_content,
        handoff_path=Path("docs/handoff/HANDOFF.md"),
    )

    assert decision.route == "unknown"
    assert recovery is not None
    assert recovery.decision.route == "planner"
    assert recovery.decision.source == "pre_dispatch_planner_frontmatter_repair"
    assert (
        "status `blocked_missing_context` routes to user, but next_agent is `planner`"
        in recovery.additional_instruction
    )


def test_status_next_agent_conflict_naming_user_pauses() -> None:
    orchestrator = _load_orchestrator_module()
    from llm_handoff.router import route

    handoff_content = """---
next_agent: user
reason: "Assignment needs re-scoping."
status: blocked_implementation_failure
producer: backend
---

# backend Handback
"""

    decision = route(handoff_content)

    assert decision.route == "unknown"
    assert decision.confidence == "LOW"
    assert (
        orchestrator._pre_dispatch_invalid_frontmatter_recovery(
            decision,
            handoff_content,
            handoff_path=Path("docs/handoff/HANDOFF.md"),
        )
        is None
    )


def test_run_loop_routes_parseable_invalid_non_planner_frontmatter_to_planner_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: backend
producer: auditor
---

# Auditor Handback

Diagnostic commit SHA: 476c2b0.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    planner_calls: list[str | None] = []

    def fail_support_role(*_args: Any, **_kwargs: Any) -> SubagentResult:
        raise AssertionError("startup recovery should not invoke handoff-validator")

    def fake_planner(
        path: Path,
        *,
        additional_instruction: str | None = None,
        log=None,
        **_kwargs: Any,
    ) -> DispatchResult:
        assert path == handoff_path
        assert callable(log)
        planner_calls.append(additional_instruction)
        path.write_text(
            """---
next_agent: backend
reason: Repair startup handoff routing before backend dispatch.
producer: planner
---

## Task Assignment

**Agent:** backend

### Objective
Continue the intended backend route.

### Acceptance Criteria
- Keep the route dispatchable.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fail_support_role)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(planner_calls) == 1
    assert "cleanup-only recovery" in (planner_calls[0] or "")
    assert "next_agent `backend`" in (planner_calls[0] or "")
    assert "reason is required" in (planner_calls[0] or "")
    assert (
        "WARN",
        "HANDOFF.md has invalid but parseable routing frontmatter; routing to planner for cleanup-only recovery.",
    ) in log_messages
    assert ("INFO", "Routing source: pre_dispatch_frontmatter_recovery") in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages
    assert ("DISPATCH", "Dispatching planner.") in log_messages


def test_run_loop_waits_for_handoff_change_before_resuming_unknown_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """# No routing data

There is no next step in this file.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(
        repo_root,
        dry_run=True,
        poll_interval_seconds=1,
    )
    log_messages: list[tuple[str, str]] = []
    sleep_calls: list[int] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: _subagent_result(
            stdout="""VALID: NO
  ROUTING: FAIL - HANDOFF does not provide a dispatchable next step.
"""
        ),
    )

    def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        handoff_path.write_text(_OPERATOR_BACKEND_FIX, encoding="utf-8")

    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert sleep_calls == [1]
    assert (
        "PAUSE",
        "Waiting for HANDOFF.md to change; polling every 1s.",
    ) in log_messages
    assert (
        "PAUSE",
        "Detected HANDOFF.md change. Resuming dispatch loop.",
    ) in log_messages
    assert ("INFO", "Routing instruction: backend") in log_messages
    assert ("DISPATCH", "[DRY RUN] Would dispatch backend") in log_messages


def test_run_loop_runs_handoff_validator_and_pauses_on_planner_self_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **planner:** Scope the next epic.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, poll_interval_seconds=1)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []
    sleep_calls: list[int] = []
    codex_calls: list[Path] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda path, additional_instruction=None, log=None, **kwargs: (
            (
                handoff_path.write_text(
                    """---
next_agent: planner
reason: Planner self-loop fixture.
producer: planner
---

Mode: Non-Implementing Principal Engineer (review/orchestration only)

## Task Assignment

**Agent:** planner
**Epic/Story:** Scope the next epic
""",
                    encoding="utf-8",
                ),
                _dispatch_result(),
            )[1]
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: (
            subagent_calls.append((subagent_name, prompt, log))
            or _subagent_result(
                stdout="""VALID: NO
  ROUTING: FAIL - planner routed the handoff back to itself instead of handing off to another agent.
"""
            )
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: (
            codex_calls.append(path),
            path.write_text(
                """---
next_agent: auditor
reason: Story 1 complete; audit requested.
scope_sha: 9482fcf
close_type: story
producer: backend
---

# backend Handback

**Agent:** backend
**Latest Commit SHA:** `9482fcf8cb0eb1099ef90b02fe2a8238c551d383`

## Completed Work

Completed work.

## Next Step

- **auditor:** Audit Story 1.
""",
                encoding="utf-8",
            ),
            _dispatch_result(),
        )[2],
    )

    def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        handoff_path.write_text(_OPERATOR_BACKEND_FIX, encoding="utf-8")

    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(subagent_calls) == 1
    assert subagent_calls[0][0] == "handoff-validator"
    assert "detected a planner self-loop" in subagent_calls[0][1]
    assert codex_calls == [handoff_path]
    assert sleep_calls == [1]
    assert (
        "ERROR",
        "planner_self_loop: planner handoff routes work back to planner, which would immediately re-dispatch the planner. Route to a backend agent, auditor, or explicit pause state instead.",
    ) in log_messages
    assert (
        "AGENT",
        "Post-dispatch gate PAUSED for planner; the planner routed the handoff back to itself.",
    ) in log_messages
    assert (
        "AGENT",
        "The planner produced a self-loop. Invoking the validator role...",
    ) in log_messages
    assert (
        "PAUSE",
        "The planner routed HANDOFF.md back to itself. Update the handoff; dispatch will resume after the file changes.",
    ) in log_messages
    assert (
        "PAUSE",
        "Detected HANDOFF.md change. Resuming dispatch loop.",
    ) in log_messages
    assert ("INFO", "Routing instruction: backend") in log_messages
    assert ("AGENT", "Post-dispatch gate PASSED for backend.") in log_messages


def test_run_loop_runs_handoff_validator_and_pauses_on_claude_audit_self_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit the current story.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, poll_interval_seconds=1)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []
    codex_calls: list[Path] = []
    sleep_calls: list[int] = []

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert callable(log)
        subagent_calls.append((subagent_name, prompt, log))
        if subagent_name == "auditor":
            handoff_path.write_text(
                """---
next_agent: auditor
reason: Auditor self-loop fixture.
scope_sha: 59206fb
producer: auditor
---

# Auditor Handback

**Agent:** auditor (auditor)
**Latest verified repo SHA:** `59206fb3f3ac027ef3ba07f4d7c8db0410edc926`

## Audit Summary

Audit complete.

## Next Step

- **auditor:** Audit the next item.
""",
                encoding="utf-8",
            )
            return _subagent_result()
        return _subagent_result(
            stdout="""VALID: NO
  ROUTING: FAIL - auditor (audit) routed the handoff back to itself instead of handing off to another agent.
"""
        )

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: (
            codex_calls.append(path),
            path.write_text(
                """---
next_agent: auditor
reason: Story 1 complete; audit requested.
scope_sha: 9482fcf
close_type: story
producer: backend
---

# backend Handback

**Agent:** backend
**Latest Commit SHA:** `9482fcf8cb0eb1099ef90b02fe2a8238c551d383`

## Completed Work

Completed work.

## Next Step

- **auditor:** Audit Story 1.
""",
                encoding="utf-8",
            ),
            _dispatch_result(),
        )[2],
    )

    def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        handoff_path.write_text(_OPERATOR_BACKEND_FIX, encoding="utf-8")

    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert [call[0] for call in subagent_calls] == [
        "auditor",
        "handoff-validator",
    ]
    assert "detected a self-loop" in subagent_calls[1][1]
    assert codex_calls == [handoff_path]
    assert sleep_calls == [1]
    assert (
        "ERROR",
        "agent_self_loop: auditor handoff routes work back to auditor, which would immediately re-dispatch the same agent. Route to a different agent or explicit pause state instead.",
    ) in log_messages
    assert (
        "AGENT",
        "Post-dispatch gate PAUSED for auditor (audit); HANDOFF routed work back to the same agent.",
    ) in log_messages
    assert (
        "AGENT",
        "auditor (audit) produced a self-loop. Invoking the validator role...",
    ) in log_messages
    assert (
        "PAUSE",
        "auditor (audit) routed HANDOFF.md back to itself. Update the handoff; dispatch will resume after the file changes.",
    ) in log_messages
    assert (
        "PAUSE",
        "Detected HANDOFF.md change. Resuming dispatch loop.",
    ) in log_messages
    assert ("INFO", "Routing instruction: backend") in log_messages
    assert ("AGENT", "Post-dispatch gate PASSED for backend.") in log_messages


def test_run_loop_allows_one_producer_repair_for_handoff_hygiene_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit the current story.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []

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
    repaired_handoff = """---
next_agent: planner
reason: Story audit approved; scope next story.
scope_sha: 59206fb
close_type: story
producer: auditor
---

# Auditor Handback

**Agent:** auditor

## Audit Summary

Audit complete with verification.

## Prior Backend Handback

**Agent:** backend
"""

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert callable(log)
        subagent_calls.append((subagent_name, prompt, log))
        if len(subagent_calls) == 1:
            handoff_path.write_text(invalid_handoff, encoding="utf-8")
        else:
            assert subagent_name == "auditor"
            assert "Repair only docs/handoff/HANDOFF.md" in prompt
            handoff_path.write_text(repaired_handoff, encoding="utf-8")
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(
        orchestrator,
        "_git_head",
        Mock(side_effect=["a" * 40, "b" * 40]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_dirty_files",
        Mock(side_effect=[("docs/handoff/HANDOFF.md",), ()]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_changed_files_since",
        Mock(return_value=("docs/handoff/HANDOFF.md",)),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert [call[0] for call in subagent_calls] == [
        "auditor",
        "auditor",
    ]
    assert (
        "AGENT",
        "Running one-shot producer repair for docs/handoff/HANDOFF.md only.",
    ) in log_messages
    assert (
        "AGENT",
        "Producer handoff hygiene repair passed post-checks.",
    ) in log_messages
    assert (
        "AGENT",
        "Post-dispatch gate PASSED for auditor (audit).",
    ) in log_messages


@pytest.mark.parametrize(
    "error",
    [
        "EVIDENCE_MISSING: status ready_for_review requires populated evidence "
        "fields at line 12: missing Commands run.",
        "EVIDENCE_MISSING: Verification Evidence cannot use HEAD as Commit SHA verified.",
    ],
)
def test_evidence_missing_is_a_repairable_handoff_hygiene_failure(error: str) -> None:
    orchestrator = _load_orchestrator_module()
    result = ValidationResult(
        verdict="NO",
        warnings=[],
        errors=[error],
        routing_instruction="auditor",
    )

    assert orchestrator._is_repairable_handoff_hygiene_failure(
        result,
        handoff_content="",
    )


def test_run_loop_repairs_missing_evidence_field_with_one_producer_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit the current story.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str]] = []

    handback = """---
next_agent: planner
reason: Story audit approved; scope next story.
status: verified_pass
scope_sha: 59206fb
close_type: story
producer: auditor
---

# Auditor Handback

**Agent:** auditor

## Audit Summary

Audit complete with verification.

## Verification Evidence

{commands}- **Output summary:** exit 0
- **Commit SHA verified:** 59206fb
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        subagent_calls.append((subagent_name, prompt))
        if len(subagent_calls) == 1:
            handoff_path.write_text(handback.format(commands=""), encoding="utf-8")
        else:
            assert "EVIDENCE_MISSING" in prompt
            handoff_path.write_text(
                handback.format(
                    commands="- **Commands run:**\n  - `python -m pytest tests -q`\n"
                ),
                encoding="utf-8",
            )
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(
        orchestrator,
        "_git_head",
        Mock(side_effect=["a" * 40, "b" * 40]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_dirty_files",
        Mock(side_effect=[("docs/handoff/HANDOFF.md",), ()]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_changed_files_since",
        Mock(return_value=("docs/handoff/HANDOFF.md",)),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert [call[0] for call in subagent_calls] == ["auditor", "auditor"]
    assert (
        "AGENT",
        "Producer handoff hygiene repair passed post-checks.",
    ) in log_messages


def test_run_loop_allows_handoff_repair_with_existing_unrelated_dirty_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit the current story.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []

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
    repaired_handoff = """---
next_agent: planner
reason: Story audit approved; scope next story.
scope_sha: 59206fb
close_type: story
producer: auditor
---

# Auditor Handback

**Agent:** auditor

## Audit Summary

Audit complete with verification.

## Prior Backend Handback

**Agent:** backend
"""

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert callable(log)
        subagent_calls.append((subagent_name, prompt, log))
        if len(subagent_calls) == 1:
            handoff_path.write_text(invalid_handoff, encoding="utf-8")
        else:
            assert "Repair only docs/handoff/HANDOFF.md" in prompt
            handoff_path.write_text(repaired_handoff, encoding="utf-8")
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(
        orchestrator,
        "_git_head",
        Mock(side_effect=["a" * 40, "b" * 40]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_dirty_files",
        Mock(
            side_effect=[
                ("docs/handoff/HANDOFF.md", "notes.txt"),
                ("notes.txt",),
            ]
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_changed_files_since",
        Mock(return_value=("docs/handoff/HANDOFF.md",)),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert [call[0] for call in subagent_calls] == [
        "auditor",
        "auditor",
    ]
    assert (
        "AGENT",
        "Producer handoff hygiene repair passed post-checks.",
    ) in log_messages


def test_run_loop_allows_repair_to_add_missing_frontmatter_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit the current story.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []

    invalid_handoff = """---
next_agent: planner
close_type: story-close
---

# Auditor Handback

**Agent:** auditor

## Audit Summary

Audit complete. Verification passed for commit 59206fb.
"""
    repaired_handoff = """---
next_agent: planner
reason: Story audit approved; scope next story.
scope_sha: 59206fb
close_type: story
producer: auditor
---

# Auditor Handback

**Agent:** auditor

## Audit Summary

Audit complete. Verification passed for commit 59206fb.
"""

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert callable(log)
        subagent_calls.append((subagent_name, prompt, log))
        if len(subagent_calls) == 1:
            handoff_path.write_text(invalid_handoff, encoding="utf-8")
        else:
            assert subagent_name == "auditor"
            assert "frontmatter_reason_missing" in prompt
            assert "frontmatter_producer_missing" in prompt
            assert "frontmatter_scope_sha_missing" in prompt
            assert "frontmatter_close_type_invalid" in prompt
            handoff_path.write_text(repaired_handoff, encoding="utf-8")
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(
        orchestrator,
        "_git_head",
        Mock(side_effect=["a" * 40, "b" * 40]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_dirty_files",
        Mock(side_effect=[("docs/handoff/HANDOFF.md",), ()]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_changed_files_since",
        Mock(return_value=("docs/handoff/HANDOFF.md",)),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert [call[0] for call in subagent_calls] == [
        "auditor",
        "auditor",
    ]
    assert (
        "AGENT",
        "Producer handoff hygiene repair passed post-checks.",
    ) in log_messages


def test_run_loop_routes_failed_handoff_hygiene_repair_to_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit the current story.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []
    planner_calls: list[tuple[str | None, Any]] = []

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
    planner_handoff = """---
next_agent: backend
reason: Dispatch the next story to backend.
scope_sha: 59206fb
producer: planner
---

# Task Assignment

**Agent:** backend

## Objective

Implement the next story.

## Acceptance Criteria

- Complete the scoped work.
"""

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert callable(log)
        subagent_calls.append((subagent_name, prompt, log))
        handoff_path.write_text(invalid_handoff, encoding="utf-8")
        return _subagent_result()

    def fake_planner(
        path: Path,
        *,
        additional_instruction: str | None = None,
        log=None,
        **_kwargs: Any,
    ) -> DispatchResult:
        planner_calls.append((additional_instruction, log))
        path.write_text(planner_handoff, encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "_git_head",
        Mock(side_effect=["a" * 40, "a" * 40]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_dirty_files",
        Mock(side_effect=[("docs/handoff/HANDOFF.md",), ("docs/handoff/HANDOFF.md",)]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_changed_files_since",
        Mock(return_value=()),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert [call[0] for call in subagent_calls] == [
        "auditor",
        "auditor",
    ]
    assert len(planner_calls) == 1
    assert "producer failed one-shot HANDOFF hygiene repair" in (
        planner_calls[0][0] or ""
    )
    assert (
        "ERROR",
        "Producer repair failed invariant: HEAD did not change; repair did not create a commit",
    ) in log_messages
    assert (
        "AGENT",
        "Post-dispatch handoff hygiene repair failed. Scheduling the planner to clean HANDOFF.md or escalate.",
    ) in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages


def test_run_loop_routes_manual_frontend_handoff_hygiene_failure_to_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: frontend
reason: "Run frontend diagnostic."
producer: planner
---

## Task Assignment

**Agent:** frontend

### Objective
Diagnose the UI issue.

### Acceptance Criteria
- Commit the diagnostic note.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, use_manual_frontend=True)
    log_messages: list[tuple[str, str]] = []
    planner_calls: list[tuple[str | None, Any]] = []

    invalid_handoff = """---
next_agent: planner
---

# Frontend Handback

The diagnostic is complete.

Diagnostic commit SHA: 476c2b0
"""
    planner_handoff = """---
next_agent: backend
reason: "Implement the follow-up story."
producer: planner
---

## Task Assignment

**Agent:** backend

### Objective
Implement the follow-up.

### Acceptance Criteria
- Complete the scoped work.
"""

    def fake_frontend(
        path: Path,
        *,
        use_manual_frontend: bool = False,
        **_kwargs: Any,
    ) -> DispatchResult:
        assert use_manual_frontend is True
        path.write_text(invalid_handoff, encoding="utf-8")
        return _dispatch_result()

    def fake_planner(
        path: Path,
        *,
        additional_instruction: str | None = None,
        log=None,
        **_kwargs: Any,
    ) -> DispatchResult:
        planner_calls.append((additional_instruction, log))
        path.write_text(planner_handoff, encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_frontend_role", fake_frontend)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, "_git_head", Mock(return_value="a" * 40))

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(planner_calls) == 1
    assert "producer failed one-shot HANDOFF hygiene repair" in (
        planner_calls[0][0] or ""
    )
    assert "frontmatter_reason_missing" in (planner_calls[0][0] or "")
    assert "scope_claim_missing" in (planner_calls[0][0] or "")
    assert (
        "ERROR",
        "No supported producer repair path for frontend.",
    ) in log_messages
    assert (
        "AGENT",
        "Post-dispatch handoff hygiene repair failed. Scheduling the planner to clean HANDOFF.md or escalate.",
    ) in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages


def test_handoff_hygiene_repair_requires_body_sha_for_missing_scope_sha() -> None:
    orchestrator = _load_orchestrator_module()
    result = ValidationResult(
        verdict="NO",
        warnings=[],
        errors=[
            "frontmatter_scope_sha_missing: producer backend omitted scope_sha while close_type is set.",
        ],
        routing_instruction="auditor",
    )

    assert (
        orchestrator._is_repairable_handoff_hygiene_failure(
            result,
            handoff_content="Body references commit 476c2b0.",
        )
        is True
    )
    assert (
        orchestrator._is_repairable_handoff_hygiene_failure(
            result,
            handoff_content="Body has no commit pointer.",
        )
        is False
    )


def test_handoff_hygiene_repair_prompt_includes_acceptance_warnings() -> None:
    orchestrator = _load_orchestrator_module()
    result = ValidationResult(
        verdict="NO",
        warnings=[
            "acceptance_coverage_unclear: Task Assignment is missing an Objective section.",
            "sha_missing: Planner handoff does not yet include a git commit SHA.",
        ],
        errors=[
            "scope_claim_mismatch: planner handoffs must use a Task Assignment block.",
        ],
        routing_instruction="backend",
    )

    issues = orchestrator._format_validation_repair_issues(result)

    assert "scope_claim_mismatch" in issues
    assert "acceptance_coverage_unclear" in issues
    assert "sha_missing" not in issues


def test_run_loop_aborts_when_planner_cannot_recover_failed_handoff_hygiene_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit the current story.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []
    planner_calls: list[str | None] = []

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

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert callable(log)
        subagent_calls.append((subagent_name, prompt, log))
        handoff_path.write_text(invalid_handoff, encoding="utf-8")
        return _subagent_result()

    def fake_planner(
        path: Path,
        *,
        additional_instruction: str | None = None,
        **_kwargs: Any,
    ) -> DispatchResult:
        planner_calls.append(additional_instruction)
        path.write_text(
            "# Planner Notes\n\nI could not determine a safe route.\n",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "_git_head",
        Mock(side_effect=["a" * 40, "a" * 40]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_dirty_files",
        Mock(side_effect=[("docs/handoff/HANDOFF.md",), ("docs/handoff/HANDOFF.md",)]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_changed_files_since",
        Mock(return_value=()),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 1
    assert [call[0] for call in subagent_calls] == [
        "auditor",
        "auditor",
    ]
    assert len(planner_calls) == 1
    assert "producer failed one-shot HANDOFF hygiene repair" in (planner_calls[0] or "")
    assert (
        "ERROR",
        "Planner failed to operationalize HANDOFF.md after producer hygiene repair was exhausted.",
    ) in log_messages
    assert (
        "ERROR",
        "Automatic HANDOFF hygiene recovery is exhausted; user repair is required.",
    ) in log_messages


def test_run_loop_schedules_planner_recovery_for_non_dispatchable_audit_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit the current dispatch fix.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    planner_calls: list[tuple[Path, str | None, Any]] = []

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert callable(log)
        assert subagent_name == "auditor"
        handoff_path.write_text(
            """# Auditor Handback

**Agent:** auditor
**Latest verified repo SHA:** `a41a24f`

## Audit Verdict: APPROVED

The change is correct.

## Next Step

Next: finalizer — ledger-updater: append the ledger entry and push.
""",
            encoding="utf-8",
        )
        return _subagent_result()

    validation_results = iter(
        [
            ValidationResult(
                verdict="NO",
                warnings=[],
                errors=[
                    "routing_instruction_missing: HANDOFF does not provide a dispatchable next step."
                ],
                routing_instruction=None,
            ),
            ValidationResult(
                verdict="WARNINGS-ONLY",
                warnings=[
                    "sha_missing: Planner handoff does not yet include a git commit SHA."
                ],
                errors=[],
                routing_instruction="user",
            ),
        ]
    )

    def fake_planner(
        path: Path,
        additional_instruction: str | None = None,
        log=None,
        **kwargs,
    ) -> DispatchResult:
        assert callable(log)
        planner_calls.append((path, additional_instruction, log))
        path.write_text(
            """Mode: Non-Implementing Principal Engineer (review/orchestration only)

## user

The handoff routing is malformed. Human clarification required before dispatch.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: next(validation_results),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(planner_calls) == 1
    assert planner_calls[0][0] == handoff_path
    assert planner_calls[0][1] is not None
    assert "does not contain a dispatchable routing instruction" in planner_calls[0][1]
    assert "canonical dispatchable route" in planner_calls[0][1]
    assert (
        "AGENT",
        "Post-dispatch handoff for auditor (audit) is not dispatchable. Scheduling the planner to repair routing or escalate on the next cycle.",
    ) in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages
    assert (
        "AGENT",
        "Post-dispatch gate PASSED WITH WARNINGS for planner.",
    ) in log_messages


def test_run_loop_validates_and_pauses_when_planner_handoff_lacks_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **planner:** Repair the malformed handoff.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, poll_interval_seconds=1)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []
    codex_calls: list[Path] = []
    sleep_calls: list[int] = []

    def fake_planner(
        path: Path,
        additional_instruction: str | None = None,
        log=None,
        **kwargs,
    ) -> DispatchResult:
        del additional_instruction, kwargs
        assert callable(log)
        path.write_text(
            """# Planner Review

The implementation should be audited by auditor, but this handoff is missing a dispatchable route.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    validation_results = iter(
        [
            ValidationResult(
                verdict="NO",
                warnings=[],
                errors=[
                    "routing_instruction_missing: HANDOFF does not provide a dispatchable next step."
                ],
                routing_instruction=None,
            ),
            ValidationResult(
                verdict="YES",
                warnings=[],
                errors=[],
                routing_instruction="auditor",
            ),
        ]
    )

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        assert callable(log)
        subagent_calls.append((subagent_name, prompt, log))
        return _subagent_result(
            stdout="""VALID: NO
  ROUTING: FAIL - HANDOFF does not provide a dispatchable next step.
"""
        )

    def fake_codex(path: Path, *, log=None, use_resume=False) -> DispatchResult:
        del log, use_resume
        codex_calls.append(path)
        path.write_text(
            """# backend Handback

**Agent:** backend
**Latest Commit SHA:** `9482fcf8cb0eb1099ef90b02fe2a8238c551d383`

## Completed Work

Completed work.

## Next Step

- **auditor:** Audit Story 1.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        handoff_path.write_text(_OPERATOR_BACKEND_FIX, encoding="utf-8")

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_codex)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: next(validation_results),
    )
    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(subagent_calls) == 1
    assert subagent_calls[0][0] == "handoff-validator"
    assert "missing a dispatchable route" in subagent_calls[0][1]
    assert codex_calls == [handoff_path]
    assert sleep_calls == [1]
    assert (
        "AGENT",
        "planner produced a handoff without a dispatchable route. Invoking the validator role...",
    ) in log_messages
    assert (
        "PAUSE",
        "planner produced HANDOFF.md without a dispatchable route. Update the handoff; dispatch will resume after the file changes.",
    ) in log_messages
    assert (
        "PAUSE",
        "Detected HANDOFF.md change. Resuming dispatch loop.",
    ) in log_messages
    assert ("INFO", "Routing instruction: backend") in log_messages
    assert ("AGENT", "Post-dispatch gate PASSED for backend.") in log_messages


def test_run_loop_repairs_planner_written_frontmatter_before_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: planner
reason: Scope next backend story.
producer: auditor
---

## Handoff
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []

    def fake_planner(
        path: Path,
        additional_instruction: str | None = None,
        log=None,
        **kwargs,
    ) -> DispatchResult:
        del additional_instruction, kwargs
        assert callable(log)
        path.write_text(
            """---
next_agent: backend
reason: Dispatch E2-S1: Implement backend as_completed loop.
producer: planner
---

## Task Assignment

**Agent:** backend

### Objective
Implement the backend dispatch refactor.

### Acceptance Criteria
- Persist per-model completion incrementally.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert (
        "WARN",
        "Auto-repaired HANDOFF YAML frontmatter by quoting reason.",
    ) in log_messages
    assert ("AGENT", "Handoff validation verdict: WARNINGS-ONLY") in log_messages
    assert (
        "AGENT",
        "Post-dispatch gate PASSED WITH WARNINGS for planner.",
    ) in log_messages
    assert "Dispatch E2-S1: Implement backend as_completed loop." in (
        handoff_path.read_text(encoding="utf-8")
    )


def test_run_loop_detects_stale_route_after_two_unchanged_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Implement Story 5.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    codex_calls: list[Path] = []
    subagent_calls: list[tuple[str, str, Any]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: (
            codex_calls.append(path) or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: (
            subagent_calls.append((subagent_name, prompt, log)) or _subagent_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert codex_calls == [handoff_path]
    assert len(subagent_calls) == 1
    assert subagent_calls[0][0] == "handoff-validator"
    assert "detected stale routing" in subagent_calls[0][1].lower()
    assert any("STALE ROUTING DETECTED" in message for _, message in log_messages)
    assert (
        "AGENT",
        "Stale HANDOFF detected for route backend. Invoking the validator role...",
    ) in log_messages


def test_run_loop_does_not_redirect_fresh_epic_close_when_project_state_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Close the epic, update the ledger, and push to origin.
""",
        project_state_content=(
            "## 2. CURRENT STATUS\n"
            "- **Active Epic:** **None — awaiting next epic dispatch (planner scoping).**\n"
        ),
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    ledger_calls: list[Any] = []
    planner_calls: list[tuple[Path, str | None, Any]] = []

    monkeypatch.setattr(
        orchestrator,
        "run_epic_close",
        lambda log=None: (
            ledger_calls.append(log)
            or EpicCloseResult(subagent_exit_code=0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda path, additional_instruction=None, log=None, **kwargs: (
            planner_calls.append((path, additional_instruction, log))
            or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(ledger_calls) == 1
    assert callable(ledger_calls[0])
    assert planner_calls == []
    assert (
        "DISPATCH",
        "Dispatching auditor ledger-updater for epic close.",
    ) in log_messages
    assert all("STALE finalizer detected" not in message for _, message in log_messages)


def test_run_loop_redirects_completed_stale_epic_close_to_planner_scoping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Close the epic, update the ledger, and push to origin.
""",
        project_state_content=(
            "## 2. CURRENT STATUS\n"
            "- **Active Epic:** UAT Remediation Epic 1 — ACTIVE.\n"
        ),
    )
    project_state_path = repo_root / "PROJECT_STATE.md"
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    ledger_calls: list[Any] = []
    planner_calls: list[tuple[Path, str | None, Any]] = []
    subagent_calls: list[tuple[str, str, Any]] = []

    def fake_epic_close(log=None) -> EpicCloseResult:
        ledger_calls.append(log)
        project_state_path.write_text(
            "## 2. CURRENT STATUS\n"
            "- **Active Epic:** **None — awaiting next epic dispatch (planner scoping).**\n",
            encoding="utf-8",
        )
        return EpicCloseResult(
            subagent_exit_code=0,
            stdout="",
            stderr="",
            handoff_rewritten=False,
        )

    monkeypatch.setattr(orchestrator, "run_epic_close", fake_epic_close)
    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda path, additional_instruction=None, log=None, **kwargs: (
            planner_calls.append((path, additional_instruction, log))
            or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: (
            subagent_calls.append((subagent_name, prompt, log)) or _subagent_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(ledger_calls) == 1
    assert callable(ledger_calls[0])
    assert subagent_calls == []
    assert len(planner_calls) == 1
    assert planner_calls[0][0] == handoff_path
    assert planner_calls[0][1] is not None
    assert "prior finalizer cycle already completed" in planner_calls[0][1]
    assert "PROJECT_STATE.md" in planner_calls[0][1]
    assert (
        "WARN",
        "STALE finalizer detected after a completed finalizer cycle; redirecting this cycle to the planner for forward routing.",
    ) in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages
    assert ("AGENT", "Post-dispatch gate PASSED for planner.") in log_messages


def test_stale_finalizer_recovery_instruction_is_provider_agnostic() -> None:
    orchestrator = _load_orchestrator_module()

    instruction = orchestrator.stale_finalizer_recovery_instruction(
        Path("PROJECT_STATE.md")
    ).lower()

    assert "gemini" not in instruction
    assert "claude" not in instruction
    assert "codex" not in instruction
    old_name = "STALE_EPIC_CLOSE_" + "GEM" + "INI_RECOVERY_INSTRUCTION"
    assert not hasattr(orchestrator, old_name)
    assert not hasattr(orchestrator, "STALE_FINALIZER_RECOVERY_INSTRUCTION")


def test_stale_finalizer_recovery_instruction_names_the_configured_state_path() -> None:
    orchestrator = _load_orchestrator_module()

    instruction = orchestrator.stale_finalizer_recovery_instruction(
        Path("docs/state/STATUS.md")
    )

    assert "docs/state/STATUS.md" in instruction
    assert "PROJECT_STATE.md" not in instruction


def test_run_loop_redirects_stale_epic_close_after_ledger_advances_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: finalizer
reason: E2 approved; close ledger.
scope_sha: a656195
close_type: epic
producer: auditor
---

# E2 Close

**auditor (ledger-updater):** Close the E2 epic.
""",
        project_state_content=(
            "## 2. CURRENT STATUS\n- **Active Epic:** UAT Remediation E2 — ACTIVE.\n"
        ),
    )
    project_state_path = repo_root / "PROJECT_STATE.md"
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    ledger_calls: list[Any] = []
    planner_calls: list[tuple[Path, str | None, Any]] = []

    def fake_epic_close(log=None) -> EpicCloseResult:
        ledger_calls.append(log)
        project_state_path.write_text(
            "## 2. CURRENT STATUS\n"
            "- **Active Epic:** UAT Remediation (E3-E6) + Doc-Role Anchor Correctness (S2.5).\n",
            encoding="utf-8",
        )
        return EpicCloseResult(
            subagent_exit_code=0,
            stdout="",
            stderr="",
            project_state_updated=True,
            handoff_rewritten=False,
        )

    monkeypatch.setattr(orchestrator, "run_epic_close", fake_epic_close)
    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda path, additional_instruction=None, log=None, **kwargs: (
            planner_calls.append((path, additional_instruction, log))
            or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(ledger_calls) == 1
    assert len(planner_calls) == 1
    assert planner_calls[0][0] == handoff_path
    assert planner_calls[0][1] is not None
    assert "prior finalizer cycle already completed" in planner_calls[0][1]
    assert "repeating finalizer" in planner_calls[0][1]
    assert (
        "WARN",
        "STALE finalizer detected after a completed finalizer cycle; redirecting this cycle to the planner for forward routing.",
    ) in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages


_FINALIZER_ASSIGNMENT = """---
next_agent: finalizer
reason: "E2 approved; close the ledger."
scope_sha: a656195
close_type: epic
producer: auditor
---

# E2 Close

**auditor (ledger-updater):** Close the E2 epic.
"""


def _finalizer_validation_spy(
    orchestrator: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Run real validation for finalizer output; stub every other role."""

    real_validate = orchestrator.validate_handoff
    validated_roles: list[str] = []

    def spy(path: Path, previous_agent: str, **kwargs: Any) -> ValidationResult:
        validated_roles.append(previous_agent)
        if previous_agent == "finalizer":
            assert kwargs.get("dispatcher_authored", False) is False
            assert kwargs.get("chat_takeover", False) is False
            assert kwargs["prior_handoff_content"] == _FINALIZER_ASSIGNMENT
            assert kwargs["project_state_path"] is not None
            return real_validate(path, previous_agent, **kwargs)
        return _validation_result()

    monkeypatch.setattr(orchestrator, "validate_handoff", spy)
    return validated_roles


def _finalizer_rewrite_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rewrite: str,
    *,
    pause_recovery: str = "off",
    max_cycles: int = 3,
) -> tuple[int, list[str], list[str | None], list[str], list[tuple[str, str]]]:
    repo_root, handoff_path = _write_repo(tmp_path, _FINALIZER_ASSIGNMENT)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, pause_recovery=pause_recovery)
    log_messages: list[tuple[str, str]] = []
    backend_calls: list[str] = []
    planner_instructions: list[str | None] = []

    def fake_epic_close(log=None) -> EpicCloseResult:
        handoff_path.write_text(rewrite, encoding="utf-8")
        # The flag is false on purpose: the dispatcher reads the file itself.
        return EpicCloseResult(
            subagent_exit_code=0,
            stdout="",
            stderr="",
            handoff_rewritten=False,
        )

    monkeypatch.setattr(orchestrator, "run_epic_close", fake_epic_close)
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, **kwargs: backend_calls.append("backend") or _dispatch_result(),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda path, additional_instruction=None, log=None, **kwargs: (
            planner_instructions.append(additional_instruction)
            or _dispatch_result()
        ),
    )
    validated_roles = _finalizer_validation_spy(orchestrator, monkeypatch)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=max_cycles,
        log=lambda level, message: log_messages.append((level, message)),
    )
    return exit_code, backend_calls, planner_instructions, validated_roles, log_messages


def test_run_loop_rejects_finalizer_rewrite_with_forged_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, backend_calls, planner_instructions, validated_roles, log_messages = (
        _finalizer_rewrite_run(
            tmp_path,
            monkeypatch,
            """---
next_agent: backend
reason: "Start the next story."
producer: planner
---

# Next Story
""",
        )
    )

    assert exit_code == 1
    assert backend_calls == []
    assert planner_instructions == []
    assert validated_roles == ["finalizer"]
    assert any(
        level == "ERROR"
        and message.startswith("frontmatter_producer_mismatch:")
        and "`finalizer`" in message
        for level, message in log_messages
    )
    assert ("AGENT", "Post-dispatch gate FAILED for finalizer.") in log_messages
    assert any(
        level == "PAUSE"
        and "Post-dispatch validation failed for finalizer" in message
        for level, message in log_messages
    )


@pytest.mark.parametrize(
    ("frontmatter_lines", "expected_error"),
    [
        (
            "next_agent: backend\n",
            "producer `finalizer` cannot route to next_agent `backend`",
        ),
        (
            "next_agent: planner\nstatus: ready_for_review\n",
            "producer `finalizer` cannot emit status `ready_for_review`",
        ),
    ],
)
def test_run_loop_rejects_finalizer_rewrite_with_illegal_route_or_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frontmatter_lines: str,
    expected_error: str,
) -> None:
    exit_code, backend_calls, planner_instructions, validated_roles, log_messages = (
        _finalizer_rewrite_run(
            tmp_path,
            monkeypatch,
            "---\n"
            + frontmatter_lines
            + 'reason: "E2 closed."\nproducer: finalizer\n---\n\n# E2 Closed\n',
        )
    )

    assert exit_code == 1
    assert backend_calls == []
    assert planner_instructions == []
    assert validated_roles == ["finalizer"]
    assert any(
        level == "ERROR" and expected_error in message
        for level, message in log_messages
    )
    assert ("AGENT", "Post-dispatch gate FAILED for finalizer.") in log_messages


def test_run_loop_sends_rejected_finalizer_rewrite_to_pause_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, backend_calls, planner_instructions, validated_roles, log_messages = (
        _finalizer_rewrite_run(
            tmp_path,
            monkeypatch,
            """---
next_agent: backend
reason: "Start the next story."
producer: auditor
---

# Next Story
""",
            pause_recovery="planner",
            max_cycles=2,
        )
    )

    assert exit_code == 0
    assert backend_calls == []
    assert len(planner_instructions) == 1
    assert planner_instructions[0] is not None
    assert "frontmatter_producer_mismatch" in planner_instructions[0]
    assert validated_roles[0] == "finalizer"


def test_run_loop_accepts_legal_finalizer_rewrite_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, backend_calls, planner_instructions, validated_roles, log_messages = (
        _finalizer_rewrite_run(
            tmp_path,
            monkeypatch,
            """---
next_agent: planner
reason: "E2 closed; choose the next epic."
status: verified_pass
producer: finalizer
---

# E2 Closed
""",
            max_cycles=2,
        )
    )

    assert exit_code == 0
    assert backend_calls == []
    # The planner runs as a normal route, not as stale-finalizer recovery.
    assert planner_instructions == [None]
    assert validated_roles == ["finalizer", "planner"]
    assert any(
        level == "AGENT"
        and message.startswith("Post-dispatch gate PASSED")
        and message.endswith("for finalizer.")
        for level, message in log_messages
    )
    assert ("AGENT", "Post-dispatch gate FAILED for finalizer.") not in log_messages
    assert all("EVIDENCE_MISSING" not in message for _, message in log_messages)
    assert all(
        "STALE finalizer detected" not in message for _, message in log_messages
    )


_LEGAL_FINALIZER_REWRITE = """---
next_agent: planner
reason: "E2 closed; choose the next epic."
status: verified_pass
producer: finalizer
---

# E2 Closed
"""


def _finalizer_chat_rewrite_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_rewrite: str,
) -> tuple[int, list[str], list[str | None], list[tuple[str, bool]], list[tuple[str, str]]]:
    """A legal finalizer rewrite, then a chat answer that rewrites the handoff
    while the finalizer's handoff is validated."""

    from llm_handoff import chat_takeover

    repo_root, handoff_path = _write_repo(tmp_path, _FINALIZER_ASSIGNMENT)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    backend_calls: list[str] = []
    planner_instructions: list[str | None] = []
    validations: list[tuple[str, bool]] = []
    chat_written: list[bool] = []
    real_validate = orchestrator.validate_handoff

    def fake_epic_close(log=None) -> EpicCloseResult:
        handoff_path.write_text(_LEGAL_FINALIZER_REWRITE, encoding="utf-8")
        return EpicCloseResult(subagent_exit_code=0, stdout="", stderr="")

    def spy(path: Path, previous_agent: str, **kwargs: Any) -> ValidationResult:
        chat = bool(kwargs.get("chat_takeover", False))
        validations.append((previous_agent, chat))
        if previous_agent == "finalizer" or chat:
            return real_validate(path, previous_agent, **kwargs)
        return _validation_result()

    def fake_wait(log=None) -> bool:
        # The answer lands once the finalizer's handoff has been validated.
        if ("finalizer", False) in validations and not chat_written:
            chat_written.append(True)
            handoff_path.write_text(chat_rewrite, encoding="utf-8")
            chat_takeover.request_takeover(
                chat_takeover.handoff_content_sha(handoff_path)
            )
            return True
        return False

    monkeypatch.setattr(orchestrator, "run_epic_close", fake_epic_close)
    monkeypatch.setattr(orchestrator, "validate_handoff", spy)
    monkeypatch.setattr(orchestrator.chat_session, "wait_for_answers", fake_wait)
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, **kwargs: backend_calls.append("backend") or _dispatch_result(),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda path, additional_instruction=None, log=None, **kwargs: (
            planner_instructions.append(additional_instruction)
            or _dispatch_result()
        ),
    )

    chat_takeover.CONTROLLER.clear()
    try:
        exit_code = orchestrator.run_loop(
            config,
            max_cycles=2,
            log=lambda level, message: log_messages.append((level, message)),
        )
    finally:
        chat_takeover.CONTROLLER.clear()
    assert chat_written == [True]
    return exit_code, backend_calls, planner_instructions, validations, log_messages


def test_run_loop_follows_chat_rewrite_after_finalizer_validation_as_takeover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, backend_calls, planner_instructions, validations, log_messages = (
        _finalizer_chat_rewrite_run(
            tmp_path,
            monkeypatch,
            """---
next_agent: backend
reason: "The operator assigned the next story from chat."
producer: user
---

## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** src/app.py
- **Files out of bounds:** src/api.py
- **Context:** Keep the module import-free.
- **Verification command:** python -c "import sys; sys.exit(0)"
- **Expected next route:** auditor
""",
        )
    )

    assert exit_code == 0
    # The chat's handoff is validated as a chat takeover before it is followed,
    # never passed by the finalizer gate.
    assert validations[:2] == [("finalizer", False), ("planner", True)]
    assert any(
        level == "AGENT" and message.startswith("The chat's handoff is valid")
        for level, message in log_messages
    )
    assert backend_calls == ["backend"]
    assert planner_instructions == []


def test_run_loop_rejects_illegal_chat_rewrite_after_finalizer_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, backend_calls, planner_instructions, validations, log_messages = (
        _finalizer_chat_rewrite_run(
            tmp_path,
            monkeypatch,
            """---
next_agent: backend
reason: "Start the next story."
producer: backend
---

# Next Story
""",
        )
    )

    assert exit_code == 1
    assert backend_calls == []
    assert planner_instructions == []
    assert validations == [("finalizer", False), ("planner", True)]
    assert any(
        level == "ERROR" and message.startswith("The chat's handoff failed validation")
        for level, message in log_messages
    )


def test_run_loop_unchanged_finalizer_handoff_keeps_stale_recovery_with_real_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(tmp_path, _FINALIZER_ASSIGNMENT)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []
    planner_instructions: list[str | None] = []

    monkeypatch.setattr(
        orchestrator,
        "run_epic_close",
        lambda log=None: EpicCloseResult(
            subagent_exit_code=0,
            stdout="",
            stderr="",
            # A true flag with an unchanged file still skips validation.
            handoff_rewritten=True,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda path, additional_instruction=None, log=None, **kwargs: (
            planner_instructions.append(additional_instruction)
            or _dispatch_result()
        ),
    )
    validated_roles = _finalizer_validation_spy(orchestrator, monkeypatch)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert handoff_path.read_text(encoding="utf-8") == _FINALIZER_ASSIGNMENT
    assert "finalizer" not in validated_roles
    assert len(planner_instructions) == 1
    assert planner_instructions[0] is not None
    assert "prior finalizer cycle already completed" in planner_instructions[0]
    assert all(
        "frontmatter_producer_mismatch" not in message for _, message in log_messages
    )
    assert (
        "WARN",
        "STALE finalizer detected after a completed finalizer cycle; redirecting this cycle to the planner for forward routing.",
    ) in log_messages


def test_run_loop_trips_circuit_breaker_after_three_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _ = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Keep retrying.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, max_consecutive_failures=3)
    log_messages: list[tuple[str, str]] = []
    attempts: list[int] = []

    def failing_codex(_path: Path, *, log=None, use_resume=False) -> DispatchResult:
        assert callable(log)
        assert use_resume is True
        attempts.append(len(attempts) + 1)
        return _dispatch_result(exit_code=9)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", failing_codex)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 1
    assert attempts == [1, 2, 3]
    assert any(
        "Hit maximum consecutive failures" in message for _, message in log_messages
    )


def test_run_loop_dry_run_skips_dispatch_and_logs_startup_banner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _ = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Preview the dispatch.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    log_messages: list[tuple[str, str]] = []

    def unexpected_call(*args: Any, **kwargs: Any) -> DispatchResult:
        raise AssertionError("dry-run must not dispatch an agent")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", unexpected_call)

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert any("Smart router:" in message for _, message in log_messages)
    assert any("Handoff validation:" in message for _, message in log_messages)
    assert any("Finalizer route:" in message for _, message in log_messages)
    assert any("Chaining:" in message for _, message in log_messages)
    assert any("Frontend agent:" in message for _, message in log_messages)
    assert (
        "INFO",
        "backend session:      MANAGED RESUME (persisted thread id)",
    ) in log_messages
    assert (
        "INFO",
        "Planner session:    MANAGED RESUME (in-memory session id)",
    ) in log_messages
    assert not any("Planner API env:" in message for _, message in log_messages)
    assert any("[DRY RUN]" in message for _, message in log_messages)


def test_run_loop_uses_manual_frontend_for_frontend_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **frontend:** Build the UI slice.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, use_manual_frontend=True)
    manual_frontend_calls: list[tuple[Path, Any]] = []
    log_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_frontend_role",
        lambda path, use_manual_frontend=False, additional_instruction=None, log=None: (
            manual_frontend_calls.append((path, log)) or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    def fake_log(level: str, message: str) -> None:
        log_messages.append((level, message))

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=fake_log)

    assert exit_code == 0
    assert manual_frontend_calls == [(handoff_path, fake_log)]
    assert (
        "DISPATCH",
        "Dispatching manual frontend (GUI, manual pause).",
    ) in log_messages


def test_run_loop_tracks_planner_session_in_memory_across_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_handoff = """## Next Step

- **planner:** Scope the next implementation.
"""
    codex_handoff = """## Next Step

- **backend:** Implement the scoped backend work.
"""
    resumed_planner_handoff = """## Next Step

- **planner:** Continue orchestration after backend handback.
"""
    repo_root, handoff_path = _write_repo(tmp_path, initial_handoff)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    planner_calls: list[dict[str, Any]] = []

    def fake_planner(
        path: Path,
        additional_instruction: str | None = None,
        log=None,
        use_resume: bool = False,
        session_id: str | None = None,
        previous_handoff_sha: str | None = None,
        current_handoff_sha: str | None = None,
    ) -> DispatchResult:
        planner_calls.append(
            {
                "path": path,
                "additional_instruction": additional_instruction,
                "log": log,
                "use_resume": use_resume,
                "session_id": session_id,
                "previous_handoff_sha": previous_handoff_sha,
                "current_handoff_sha": current_handoff_sha,
            }
        )
        if len(planner_calls) == 1:
            path.write_text(codex_handoff, encoding="utf-8")
            return DispatchResult(
                stdout="",
                stderr="",
                exit_code=0,
                elapsed_seconds=0.01,
                session_id="planner-session-1",
            )
        path.write_text("## user\n\nDone for test.\n", encoding="utf-8")
        return DispatchResult(
            stdout="",
            stderr="",
            exit_code=0,
            elapsed_seconds=0.01,
            session_id=session_id,
        )

    def fake_codex(path: Path, log=None, use_resume: bool = False) -> DispatchResult:
        del log, use_resume
        path.write_text(resumed_planner_handoff, encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_codex)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(config, max_cycles=3)

    assert exit_code == 0
    assert len(planner_calls) == 2
    assert planner_calls[0]["use_resume"] is True
    assert planner_calls[0]["session_id"] is None
    assert planner_calls[0]["previous_handoff_sha"] is None
    assert planner_calls[0]["current_handoff_sha"] == orchestrator._content_sha(
        initial_handoff
    )
    assert planner_calls[1]["use_resume"] is True
    assert planner_calls[1]["session_id"] == "planner-session-1"
    assert planner_calls[1]["previous_handoff_sha"] == orchestrator._content_sha(
        initial_handoff
    )
    assert planner_calls[1]["current_handoff_sha"] == orchestrator._content_sha(
        resumed_planner_handoff
    )


def test_run_loop_passes_logger_to_frontend_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **frontend:** Review the UI slice.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    frontend_calls: list[tuple[Path, bool, Any]] = []
    log_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_frontend_role",
        lambda path, use_manual_frontend=False, additional_instruction=None, log=None: (
            frontend_calls.append((path, use_manual_frontend, log))
            or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    def fake_log(level: str, message: str) -> None:
        log_messages.append((level, message))

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=fake_log)

    assert exit_code == 0
    assert frontend_calls == [(handoff_path, False, fake_log)]


def test_run_loop_passes_logger_to_codex_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Review the backend slice.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    codex_calls: list[tuple[Path, Any, bool]] = []
    log_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: (
            codex_calls.append((path, log, use_resume)) or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    def fake_log(level: str, message: str) -> None:
        log_messages.append((level, message))

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=fake_log)

    assert exit_code == 0
    assert codex_calls == [(handoff_path, fake_log, True)]


def test_run_loop_passes_configured_agent_provider_to_backend_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Review the backend slice.
""",
    )
    config_module = importlib.import_module("llm_handoff.config")
    agents = config_module._default_agent_configs()
    agents["backend"] = config_module.AgentConfig(
        provider="claude",
        binary="claude-custom",
        model="claude-test",
        unsafe_bypass=True,
        permissions_flag="--allowed",
        timeout_ms=123,
        agent_name="backend-worker",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, agents=agents)
    backend_calls: list[dict[str, Any]] = []

    def fake_backend(
        path: Path,
        *,
        log=None,
        use_resume: bool = False,
        additional_instruction: str | None = None,
        agent_config=None,
    ) -> DispatchResult:
        backend_calls.append(
            {
                "path": path,
                "log": log,
                "use_resume": use_resume,
                "additional_instruction": additional_instruction,
                "agent_config": agent_config,
            }
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(config, max_cycles=1)

    assert exit_code == 0
    assert len(backend_calls) == 1
    assert backend_calls[0]["path"] == handoff_path
    assert backend_calls[0]["use_resume"] is True
    assert backend_calls[0]["additional_instruction"] is None
    assert backend_calls[0]["agent_config"].provider == "claude"
    assert backend_calls[0]["agent_config"].agent_name == "backend-worker"


def test_run_loop_passes_codex_resume_mode_to_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Review the backend slice.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, backend_resume=True)
    codex_calls: list[tuple[Path, Any, bool]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: (
            codex_calls.append((path, log, use_resume)) or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(config, max_cycles=1)

    assert exit_code == 0
    assert len(codex_calls) == 1
    assert codex_calls[0][0] == handoff_path
    assert codex_calls[0][2] is True


def test_run_loop_passes_logger_to_claude_subagent_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _ = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit Story 5.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    subagent_calls: list[tuple[str, str, Any]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: (
            subagent_calls.append((subagent_name, prompt, log)) or _subagent_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    def fake_log(level: str, message: str) -> None:
        del level, message

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=fake_log)

    assert exit_code == 0
    assert len(subagent_calls) == 1
    assert subagent_calls[0][0] == "auditor"
    assert subagent_calls[0][2] is fake_log


def test_run_loop_passes_configured_agent_provider_to_auditor_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **auditor:** Audit Story 5.
""",
    )
    config_module = importlib.import_module("llm_handoff.config")
    agents = config_module._default_agent_configs()
    agents["auditor"] = config_module.AgentConfig(
        provider="antigravity",
        binary="agy-custom",
        timeout_ms=456,
        agent_name="auditor-agy",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, agents=agents)
    subagent_calls: list[dict[str, Any]] = []

    def fake_subagent(
        subagent_name: str,
        prompt: str,
        *,
        role=None,
        handoff_path=None,
        agent_config=None,
        log=None,
    ) -> SubagentResult:
        subagent_calls.append(
            {
                "subagent_name": subagent_name,
                "prompt": prompt,
                "role": role,
                "handoff_path": handoff_path,
                "agent_config": agent_config,
                "log": log,
            }
        )
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(config, max_cycles=1)

    assert exit_code == 0
    assert len(subagent_calls) == 1
    assert subagent_calls[0]["subagent_name"] == "auditor"
    assert subagent_calls[0]["prompt"] == orchestrator.AUDIT_PROMPT_TEMPLATE.format(
        handoff_path="docs/handoff/HANDOFF.md"
    )
    assert subagent_calls[0]["role"] == "auditor"
    assert subagent_calls[0]["handoff_path"] == handoff_path
    assert subagent_calls[0]["agent_config"].provider == "antigravity"
    assert subagent_calls[0]["agent_config"].agent_name == "auditor-agy"
    assert callable(subagent_calls[0]["log"])


def test_run_loop_routes_epic_close_to_ledger_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _ = _write_repo(tmp_path, "Next: close epic\n")
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    ledger_calls: list[dict[str, Any]] = []
    log_messages: list[tuple[str, str]] = []

    def fake_run_epic_close(*, config=None, log=None) -> EpicCloseResult:
        ledger_calls.append({"config": config, "log": log})
        return EpicCloseResult(subagent_exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator, "run_epic_close", fake_run_epic_close)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(ledger_calls) == 1
    assert ledger_calls[0]["config"] is config
    assert callable(ledger_calls[0]["log"])
    assert (
        "DISPATCH",
        "Dispatching auditor ledger-updater for epic close.",
    ) in log_messages


def test_run_loop_runs_handoff_validator_and_pauses_on_epic_close_parse_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(tmp_path, "Next: close epic\n")
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, poll_interval_seconds=1)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []
    codex_calls: list[Path] = []
    sleep_calls: list[int] = []

    monkeypatch.setattr(
        orchestrator,
        "run_epic_close",
        lambda log=None: EpicCloseResult(
            subagent_exit_code=1,
            stdout="ledger updater wrote prose instead of structured fields",
            stderr="",
            parse_error="Missing LEDGER UPDATED line.",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: (
            subagent_calls.append((subagent_name, prompt, log))
            or _subagent_result(
                stdout="""VALID: NO
  ROUTING: FAIL - finalizer was attempted before auditor approval was present.
"""
            )
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: (
            codex_calls.append(path) or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        handoff_path.write_text(_OPERATOR_BACKEND_FIX, encoding="utf-8")

    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(subagent_calls) == 1
    assert subagent_calls[0][0] == "handoff-validator"
    assert "attempted finalizer" in subagent_calls[0][1].lower()
    assert codex_calls == [handoff_path]
    assert sleep_calls == [1]
    assert any(
        level == "AGENT"
        and "finalizer produced unparseable ledger-updater output" in message
        for level, message in log_messages
    )
    assert any(
        level == "PAUSE" and "ledger-updater output was unparseable" in message
        for level, message in log_messages
    )
    assert any(
        level == "PAUSE" and "Detected HANDOFF.md change" in message
        for level, message in log_messages
    )
    assert ("INFO", "Routing instruction: backend") in log_messages


def test_run_loop_runs_handoff_validator_before_low_confidence_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Task Assignment

**Agent:** backend

## Next Step

- **planner:** Scope the next story.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, poll_interval_seconds=1)
    log_messages: list[tuple[str, str]] = []
    subagent_calls: list[tuple[str, str, Any]] = []
    codex_calls: list[Path] = []
    sleep_calls: list[int] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda subagent_name, prompt, log=None: (
            subagent_calls.append((subagent_name, prompt, log))
            or _subagent_result(
                stdout="""VALID: NO
  ROUTING: FAIL - Multiple routing signals conflict; clarify the handoff before dispatch.
"""
            )
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda path, log=None, use_resume=False: (
            codex_calls.append(path) or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        handoff_path.write_text(_OPERATOR_BACKEND_FIX, encoding="utf-8")

    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert len(subagent_calls) == 1
    assert subagent_calls[0][0] == "handoff-validator"
    assert (
        "low-confidence routing decision to planner".lower()
        in subagent_calls[0][1].lower()
    )
    assert sleep_calls == [1]
    assert codex_calls == [handoff_path]
    assert (
        "AGENT",
        "Routing decision for planner is only LOW confidence. Invoking the validator role before dispatch...",
    ) in log_messages
    assert (
        "PAUSE",
        "Routing instruction planner is only LOW confidence. Update HANDOFF.md; dispatch will resume after the file changes.",
    ) in log_messages
    assert (
        "ERROR",
        "routing: Multiple routing signals conflict; clarify the handoff before dispatch.",
    ) in log_messages
    assert ("INFO", "Routing instruction: backend") in log_messages


def test_main_returns_zero_for_help_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_module = _load_main_module()

    monkeypatch.setattr(
        main_module,
        "_cleanup_codex_output_artifacts",
        Mock(side_effect=AssertionError("help should not clean artifacts")),
    )
    monkeypatch.setattr(
        main_module,
        "run_loop",
        Mock(side_effect=AssertionError("help should not dispatch")),
    )

    assert main_module.main(["--help"]) == 0
    assert "File-based handoff dispatcher" in capsys.readouterr().out


def test_main_parses_cli_flags_and_dispatches_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    captured: dict[str, Any] = {}

    class FakeDispatchLogger:
        def __init__(
            self,
            *,
            repo_root,
            max_consecutive_failures,
            backend_resume,
            planner_resume,
        ):
            captured["logger_repo_root"] = repo_root
            captured["logger_max_consecutive_failures"] = max_consecutive_failures
            captured["logger_backend_resume"] = backend_resume
            captured["logger_planner_resume"] = planner_resume

        def __call__(self, level: str, message: str) -> None:
            captured.setdefault("log_calls", []).append((level, message))

        def mark_startup_complete(self) -> None:
            captured["startup_marked"] = True

    def fake_run_loop(config, *, log=None):
        captured["config"] = config
        captured["log"] = log
        return 41

    monkeypatch.setattr(
        main_module,
        "_set_dispatch_console_title",
        lambda: ("previous title", True),
    )
    monkeypatch.setattr(
        main_module,
        "_restore_console_title",
        lambda previous_title, *, changed: captured.setdefault(
            "restored_titles",
            [],
        ).append((previous_title, changed)),
    )
    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module.main(
        [
            "--dry-run",
            "--manual-frontend",
            "--repo-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 41
    config = captured["config"]
    assert config.repo_root == tmp_path
    assert config.dry_run is True
    assert config.use_manual_frontend is True
    assert config.backend_resume is True
    assert config.planner_resume is True
    assert captured["logger_repo_root"] == tmp_path
    assert (
        captured["logger_max_consecutive_failures"] == config.max_consecutive_failures
    )
    assert captured["logger_backend_resume"] is True
    assert captured["logger_planner_resume"] is True
    assert isinstance(captured["log"], FakeDispatchLogger)
    assert captured["restored_titles"] == [("previous title", True)]


def test_run_dispatch_removes_dispatch_lock_after_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    (tmp_path / ".git").mkdir()
    lock_path = tmp_path / ".dispatch.lock"
    captured: dict[str, object] = {}

    class FakeDispatchLogger:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __call__(self, level: str, message: str) -> None:
            captured.setdefault("log_calls", []).append((level, message))

        def mark_startup_complete(self) -> None:
            captured["startup_marked"] = True

    def fake_run_loop(config, *, log=None):
        captured["lock_exists_during_run"] = lock_path.exists()
        captured["config"] = config
        captured["log"] = log
        return 0

    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module._run_dispatch(
        dry_run=True,
        use_manual_frontend=False,
        repo_root=tmp_path,
    )

    assert exit_code == 0
    assert captured["lock_exists_during_run"] is True
    assert not lock_path.exists()


def test_run_dispatch_refuses_existing_dispatch_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_module = _load_main_module()
    (tmp_path / ".git").mkdir()
    lock_path = tmp_path / ".dispatch.lock"
    lock_path.write_text("pid: 123\n", encoding="utf-8")
    run_loop = Mock(return_value=0)

    monkeypatch.setattr(main_module, "run_loop", run_loop)

    exit_code = main_module._run_dispatch(
        dry_run=True,
        use_manual_frontend=False,
        repo_root=tmp_path,
    )

    assert exit_code == 1
    assert lock_path.exists()
    run_loop.assert_not_called()
    assert (
        "Another llm-handoff dispatcher appears to be running"
        in capsys.readouterr().err
    )


def test_run_dispatch_can_run_from_source_checkout_against_target_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    source_checkout = tmp_path / "llm-handoff-source"
    target_repo = tmp_path / "target-repo"
    source_checkout.mkdir()
    target_repo.mkdir()
    (target_repo / ".git").mkdir()
    (target_repo / "dispatch_config.yaml").write_text(
        "handoff_path: docs/handoff/HANDOFF.md\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeDispatchLogger:
        def __init__(self, **kwargs: object) -> None:
            captured["logger_kwargs"] = kwargs

        def __call__(self, level: str, message: str) -> None:
            captured.setdefault("log_calls", []).append((level, message))

        def mark_startup_complete(self) -> None:
            captured["startup_marked"] = True

    def fake_run_loop(config, *, log=None):
        captured["config"] = config
        captured["log"] = log
        captured["target_lock_exists_during_run"] = (
            target_repo / ".dispatch.lock"
        ).exists()
        captured["source_lock_exists_during_run"] = (
            source_checkout / ".dispatch.lock"
        ).exists()
        return 0

    monkeypatch.chdir(source_checkout)
    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module._run_dispatch(
        dry_run=True,
        use_manual_frontend=False,
        backend_resume=True,
        planner_resume=True,
        repo_root=target_repo,
        config_path=target_repo / "dispatch_config.yaml",
    )

    assert exit_code == 0
    assert captured["config"].repo_root == target_repo.resolve()
    assert captured["target_lock_exists_during_run"] is True
    assert captured["source_lock_exists_during_run"] is False
    assert not (target_repo / ".dispatch.lock").exists()


def test_main_supports_opting_out_of_backend_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    captured: dict[str, Any] = {}

    class FakeDispatchLogger:
        def __init__(
            self,
            *,
            repo_root,
            max_consecutive_failures,
            backend_resume,
            planner_resume,
        ):
            captured["logger_repo_root"] = repo_root
            captured["logger_backend_resume"] = backend_resume
            captured["logger_planner_resume"] = planner_resume
            del max_consecutive_failures

        def __call__(self, level: str, message: str) -> None:
            captured.setdefault("log_calls", []).append((level, message))

        def mark_startup_complete(self) -> None:
            captured["startup_marked"] = True

    def fake_run_loop(config, *, log=None):
        captured["config"] = config
        captured["log"] = log
        return 0

    monkeypatch.setattr(
        main_module,
        "_set_dispatch_console_title",
        lambda: ("previous title", True),
    )
    monkeypatch.setattr(
        main_module,
        "_restore_console_title",
        lambda previous_title, *, changed: captured.setdefault(
            "restored_titles",
            [],
        ).append((previous_title, changed)),
    )
    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module.main(["--no-backend-resume", "--repo-root", str(tmp_path)])

    assert exit_code == 0
    assert captured["config"].backend_resume is False
    assert captured["logger_backend_resume"] is False
    assert captured["config"].planner_resume is True
    assert captured["logger_planner_resume"] is True


def test_main_supports_opting_out_of_planner_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    captured: dict[str, Any] = {}

    class FakeDispatchLogger:
        def __init__(
            self,
            *,
            repo_root,
            max_consecutive_failures,
            backend_resume,
            planner_resume,
        ):
            captured["logger_repo_root"] = repo_root
            captured["logger_backend_resume"] = backend_resume
            captured["logger_planner_resume"] = planner_resume
            del max_consecutive_failures

        def __call__(self, level: str, message: str) -> None:
            captured.setdefault("log_calls", []).append((level, message))

        def mark_startup_complete(self) -> None:
            captured["startup_marked"] = True

    def fake_run_loop(config, *, log=None):
        captured["config"] = config
        captured["log"] = log
        return 0

    monkeypatch.setattr(
        main_module,
        "_set_dispatch_console_title",
        lambda: ("previous title", True),
    )
    monkeypatch.setattr(
        main_module,
        "_restore_console_title",
        lambda previous_title, *, changed: captured.setdefault(
            "restored_titles",
            [],
        ).append((previous_title, changed)),
    )
    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module.main(["--no-planner-resume", "--repo-root", str(tmp_path)])

    assert exit_code == 0
    assert captured["config"].planner_resume is False
    assert captured["logger_planner_resume"] is False
    assert captured["config"].backend_resume is True
    assert captured["logger_backend_resume"] is True


def test_main_returns_130_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    captured: dict[str, Any] = {}

    class FakeDispatchLogger:
        def __init__(
            self,
            *,
            repo_root,
            max_consecutive_failures,
            backend_resume,
            planner_resume,
        ):
            captured["logger_repo_root"] = repo_root
            captured["logger_max_consecutive_failures"] = max_consecutive_failures
            captured["logger_backend_resume"] = backend_resume
            captured["logger_planner_resume"] = planner_resume

        def __call__(self, level: str, message: str) -> None:
            captured.setdefault("log_calls", []).append((level, message))

        def mark_startup_complete(self) -> None:
            captured["startup_marked"] = True

    def fake_run_loop(config, *, log=None):
        del config, log
        raise KeyboardInterrupt

    monkeypatch.setattr(
        main_module,
        "_set_dispatch_console_title",
        lambda: ("previous title", True),
    )
    monkeypatch.setattr(
        main_module,
        "_restore_console_title",
        lambda previous_title, *, changed: captured.setdefault(
            "restored_titles",
            [],
        ).append((previous_title, changed)),
    )
    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module.main(["--repo-root", str(tmp_path)])

    assert exit_code == 130
    assert captured["logger_repo_root"] == tmp_path
    assert captured["logger_backend_resume"] is True
    assert captured["log_calls"] == [("WARN", "Dispatch interrupted by user. Exiting.")]
    assert captured["restored_titles"] == [("previous title", True)]


def test_main_cleans_stale_codex_output_artifacts_before_running_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    captured: dict[str, Any] = {}

    class FakeDispatchLogger:
        def __init__(
            self,
            *,
            repo_root,
            max_consecutive_failures,
            backend_resume,
            planner_resume,
        ):
            del repo_root, max_consecutive_failures, backend_resume, planner_resume

        def __call__(self, level: str, message: str) -> None:
            captured.setdefault("log_calls", []).append((level, message))

        def mark_startup_complete(self) -> None:
            return None

    monkeypatch.setattr(
        main_module,
        "_set_dispatch_console_title",
        lambda: ("previous title", True),
    )
    monkeypatch.setattr(
        main_module,
        "_restore_console_title",
        lambda previous_title, *, changed: captured.setdefault(
            "restored_titles",
            [],
        ).append((previous_title, changed)),
    )
    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(
        main_module,
        "_codex_artifact_paths",
        lambda repo_root: f"paths-for:{repo_root}",
    )
    monkeypatch.setattr(
        main_module,
        "_cleanup_codex_output_artifacts",
        lambda artifact_paths: captured.setdefault("cleanup_calls", []).append(
            artifact_paths
        ),
    )
    monkeypatch.setattr(main_module, "run_loop", lambda config, *, log=None: 0)

    exit_code = main_module.main(["--repo-root", str(tmp_path)])

    assert exit_code == 0
    assert captured["cleanup_calls"] == [f"paths-for:{tmp_path.resolve()}"]


def test_set_dispatch_console_title_sets_and_restores_windows_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    call_log: list[tuple[str, object, object | None]] = []

    class FakeKernel32:
        def GetConsoleTitleW(self, buffer, size):
            del size
            call_log.append(("get", buffer, None))
            buffer.value = "Previous Tab Title"
            return len(buffer.value)

        def SetConsoleTitleW(self, title):
            call_log.append(("set", title, None))
            return 1

    class FakeCtypes:
        def __init__(self):
            self.windll = type("FakeWindll", (), {"kernel32": FakeKernel32()})()

        @staticmethod
        def create_unicode_buffer(size):
            call_log.append(("buffer", size, None))
            return type("FakeBuffer", (), {"value": ""})()

    monkeypatch.setattr(main_module, "ctypes", FakeCtypes())
    monkeypatch.setattr(main_module, "_WINDOWS", True)

    previous_title, changed = main_module._set_dispatch_console_title()
    main_module._restore_console_title(previous_title, changed=changed)

    assert previous_title == "Previous Tab Title"
    assert changed is True
    assert ("buffer", 32768, None) in call_log
    assert any(entry[0] == "get" for entry in call_log)
    assert ("set", "llm-handoff dispatcher", None) in call_log
    assert ("set", "Previous Tab Title", None) in call_log


def test_set_dispatch_console_title_preserves_previous_title_when_set_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    call_log: list[tuple[str, object, object | None]] = []

    class FakeKernel32:
        def GetConsoleTitleW(self, buffer, size):
            del size
            call_log.append(("get", buffer, None))
            buffer.value = "Previous Tab Title"
            return len(buffer.value)

        def SetConsoleTitleW(self, title):
            call_log.append(("set", title, None))
            return 0

    class FakeCtypes:
        def __init__(self):
            self.windll = type("FakeWindll", (), {"kernel32": FakeKernel32()})()

        @staticmethod
        def create_unicode_buffer(size):
            call_log.append(("buffer", size, None))
            return type("FakeBuffer", (), {"value": ""})()

    monkeypatch.setattr(main_module, "ctypes", FakeCtypes())
    monkeypatch.setattr(main_module, "_WINDOWS", True)

    previous_title, changed = main_module._set_dispatch_console_title()
    main_module._restore_console_title(previous_title, changed=changed)

    assert previous_title == "Previous Tab Title"
    assert changed is False
    assert ("set", "llm-handoff dispatcher", None) in call_log
    assert ("set", "Previous Tab Title", None) not in call_log


def test_configure_stdio_encoding_reconfigures_non_utf8_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _load_main_module()
    stdout_calls: list[tuple[str, str]] = []
    stderr_calls: list[tuple[str, str]] = []

    class FakeStream:
        def __init__(self, encoding: str | None, calls: list[tuple[str, str]]) -> None:
            self.encoding = encoding
            self._calls = calls

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self._calls.append((encoding, errors))

    monkeypatch.setattr(
        main_module.sys,
        "stdout",
        FakeStream("cp1252", stdout_calls),
    )
    monkeypatch.setattr(
        main_module.sys,
        "stderr",
        FakeStream("cp1252", stderr_calls),
    )

    main_module._configure_stdio_encoding()

    assert stdout_calls == [("utf-8", "replace")]
    assert stderr_calls == [("utf-8", "replace")]


def test_dispatch_cmd_wraps_repo_venv_python() -> None:
    wrapper_path = Path("scripts") / "dispatch.cmd"
    assert wrapper_path.read_text(encoding="utf-8").strip() == (
        "@echo off & %~dp0..\\venv\\Scripts\\python.exe -m llm_handoff %*"
    )


def test_dispatch_ps1_wraps_repo_venv_python() -> None:
    wrapper_path = Path("scripts") / "dispatch.ps1"
    assert wrapper_path.read_text(encoding="utf-8").strip() == (
        '$dispatchWindowTitle = "llm-handoff dispatcher"\n'
        "$previousWindowTitle = $null\n"
        "$exitCode = 0\n"
        "\n"
        "try {\n"
        "    try {\n"
        "        $previousWindowTitle = $Host.UI.RawUI.WindowTitle\n"
        "        $Host.UI.RawUI.WindowTitle = $dispatchWindowTitle\n"
        "    }\n"
        "    catch {\n"
        "    }\n"
        "\n"
        '    & "$PSScriptRoot\\..\\venv\\Scripts\\python.exe" -m llm_handoff @args\n'
        "    $exitCode = $LASTEXITCODE\n"
        "}\n"
        "finally {\n"
        "    if ($null -ne $previousWindowTitle) {\n"
        "        try {\n"
        "            $Host.UI.RawUI.WindowTitle = $previousWindowTitle\n"
        "        }\n"
        "        catch {\n"
        "        }\n"
        "    }\n"
        "}\n"
        "\n"
        "exit $exitCode"
    )


def test_run_loop_accepts_shipped_planner_work_packet_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """---
next_agent: planner
reason: Scope the next backend story.
producer: user
---

## Handoff
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    log_messages: list[tuple[str, str]] = []

    def fake_planner(path: Path, **kwargs: object) -> DispatchResult:
        del kwargs
        # Exactly the body shape shipped in .agents/agents/planner.md.
        path.write_text(
            """---
next_agent: backend
reason: "Dispatch EXAMPLE-EPIC-S1: reject empty widget names."
epic_id: EXAMPLE-EPIC
story_id: EXAMPLE-EPIC-S1
story_title: Reject empty widget names
producer: planner
---

# Handoff: Reject empty widget names

## Work Packet

- **Objective:** Reject empty `name` values in `create_widget`.
- **Files in scope:** src/foo/bar.py, tests/test_bar.py
- **Files out of bounds:** src/foo/api.py, frontend/
- **Context:** Preserve the existing `WidgetError` contract.
- **Verification command:** python -m pytest tests/test_bar.py -q
- **Expected next route:** auditor

## Next Step
- **backend:** Implement the validation and route to auditor.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert ("AGENT", "Handoff validation verdict: WARNINGS-ONLY") in log_messages
    assert (
        "AGENT",
        "Post-dispatch gate PASSED WITH WARNINGS for planner.",
    ) in log_messages
    assert all(
        "scope_claim_mismatch" not in message for _level, message in log_messages
    )
    assert all(
        "producer repair" not in message.lower() for _level, message in log_messages
    )


def test_circuit_breaker_survives_dispatcher_restart(
    tmp_path: Path,
) -> None:
    story_id = "restart-story"
    ledger.reset_bounce(story_id)
    repo_root, handoff_path = _write_repo(
        tmp_path,
        f"""---
next_agent: backend
reason: "Audit found a defect; fix it."
story_id: {story_id}
status: verified_fail
scope_sha: 877f54d
close_type: story
producer: auditor
---

# Audit Report

**Agent:** auditor
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    state_path = repo_root / "logs" / "dispatch" / "bounce-counts.json"
    first_run_messages: list[tuple[str, str]] = []

    # Three audited failures in the first dispatcher process.
    for _ in range(3):
        orchestrator._record_post_dispatch_status_transition(
            config=config,
            log=lambda level, message: first_run_messages.append((level, message)),
        )

    assert ("INFO", f"Bounce count for story_id={story_id} is now 3.") in (
        first_run_messages
    )
    assert state_path.exists()

    # Simulate a restart (for example the supervisor relaunching after a
    # repair): the in-memory table starts empty, the file does not.
    ledger._BOUNCE_COUNTS.pop(story_id, None)
    assert ledger.bounce_count(story_id) == 0
    second_run_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: second_run_messages.append((level, message)),
    )

    assert exit_code == 0
    assert (
        "INFO",
        f"Restored bounce counts from {state_path.resolve()}: {story_id}=3",
    ) in second_run_messages
    assert any(
        "THREE_FIX_CIRCUIT_BREAKER" in message for _, message in second_run_messages
    )
    assert ("INFO", "Routing instruction: planner") in second_run_messages
    ledger.reset_bounce(story_id)


def test_status_transition_warns_when_bounce_state_cannot_be_written(
    tmp_path: Path,
) -> None:
    story_id = "unwritable-bounce-story"
    ledger.reset_bounce(story_id)
    repo_root, _handoff_path = _write_repo(
        tmp_path,
        f"""---
next_agent: backend
reason: "Audit found a defect; fix it."
story_id: {story_id}
status: verified_fail
scope_sha: 877f54d
close_type: story
producer: auditor
---

# Audit Report
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    # A directory where the state file should be makes the atomic replace fail.
    (repo_root / "logs" / "dispatch" / "bounce-counts.json").mkdir(parents=True)
    log_messages: list[tuple[str, str]] = []

    orchestrator._record_post_dispatch_status_transition(
        config=config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert any(
        level == "WARN" and message.startswith("Bounce count could not be persisted")
        for level, message in log_messages
    )
    assert ledger.bounce_count(story_id) == 1
    ledger.reset_bounce(story_id)


@pytest.mark.parametrize(
    ("raw_next_agent", "expected"),
    [
        ("Backend", "backend"),
        ("BACKEND", "backend"),
        ('"backend "', "backend"),
        ("'Frontend'", "frontend"),
        ("Auditor", "auditor"),
    ],
)
def test_run_loop_resolves_next_agent_variants_without_model_normalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_next_agent: str,
    expected: str,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        f"""---
next_agent: {raw_next_agent}
reason: Route the next step.
scope_sha: 25c45ca
close_type: story
producer: planner
---

## Handoff
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    log_messages: list[tuple[str, str]] = []

    def fail_normalizer(freeform: str, **_kwargs: object) -> str:
        raise AssertionError(
            f"model normalizer must not run for deterministic value {freeform!r}"
        )

    monkeypatch.setattr(orchestrator, "normalize_next_agent", fail_normalizer)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert ("INFO", f"Routing instruction: {expected}") in log_messages
    assert f"next_agent: {expected}\n" in handoff_path.read_text(encoding="utf-8")
    assert all("invoking normalizer" not in message for _level, message in log_messages)
    assert all("normalizer returned" not in message for _level, message in log_messages)
    assert any(
        message.startswith("router: next_agent ")
        and f"resolved deterministically to '{expected}'" in message
        for _level, message in log_messages
    )
    assert (
        "INFO",
        f"router: rewrote next_agent to deterministic output '{expected}' in HANDOFF.md.",
    ) in log_messages


def test_run_validator_redacts_secrets_in_stderr_warn_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _handoff_path = _write_repo(tmp_path, "# Handoff\n")
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    planted = "sk-proj-" + "Zq9Xw" * 8
    raw_stderr = f"request failed with OPENAI_API_KEY={planted}\n"
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda _name, _prompt, **_kwargs: _subagent_result(
            exit_code=1, stdout="", stderr=raw_stderr
        ),
    )
    log_messages: list[tuple[str, str]] = []

    result = orchestrator._run_validator(
        config,
        "validator",
        "prompt",
        "Validator",
        lambda level, message: log_messages.append((level, message)),
    )

    assert result is None
    warns = [message for level, message in log_messages if level == "WARN"]
    assert warns == [
        "Validator stderr: request failed with OPENAI_API_KEY=[REDACTED]"
    ]
    assert all(planted not in message for _level, message in log_messages)


_LINEAR_VALIDATOR_HANDOFF = """---
next_agent: planner
reason: "Validator repaired the route."
story_id: ENG-1
tracker_issue: ENG-1
tracker_url: https://linear.app/example/issue/ENG-1
selection_basis: priority
producer: validator
---

## Handoff
"""


def _linear_validator_config(repo_root: Path):
    config_module = importlib.import_module("llm_handoff.config")
    return config_module.DispatchConfig(
        repo_root=repo_root,
        tracker={
            "provider": "linear",
            "team": "ENG",
            "project": "Platform",
            "eligible_state": "Todo",
            "in_progress_state": "In Progress",
            "success_state": "In Review",
            "mcp_server": "linear",
        },
        # Linear mode rejects antigravity, the planner and frontend default.
        agents={
            **config_module._default_agent_configs(),
            "planner": config_module.AgentConfig(provider="codex"),
            "frontend": config_module.AgentConfig(provider="codex"),
        },
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the validator prompt.",
        },
        # Linear mode requires the work-claim gate.
        enforce_work_packet=True,
    )


def _capture_validator_prompt(
    orchestrator: Any, config: Any, monkeypatch: pytest.MonkeyPatch
) -> str:
    prompts: list[str] = []

    def fake_invoke(_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
        prompts.append(prompt)
        return _subagent_result(exit_code=1, stdout="", stderr="")

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_invoke)
    orchestrator._run_validator(
        config, "handoff-validator", "base prompt", "Validator", lambda *_: None
    )
    assert len(prompts) == 1
    return prompts[0]


@pytest.mark.parametrize("recorded", [True, False])
def test_run_validator_appends_linear_dispatcher_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded: bool,
) -> None:
    from llm_handoff.validator import record_dispatcher_handoff

    repo_root, _handoff_path = _write_repo(tmp_path, _LINEAR_VALIDATOR_HANDOFF)
    if recorded:
        assert record_dispatcher_handoff(repo_root, _LINEAR_VALIDATOR_HANDOFF)
    orchestrator = _load_orchestrator_module()
    config = _linear_validator_config(repo_root)

    prompt = _capture_validator_prompt(orchestrator, config, monkeypatch)

    expected = (
        orchestrator.LINEAR_VALIDATOR_PROVENANCE_TRUE
        if recorded
        else orchestrator.LINEAR_VALIDATOR_PROVENANCE_FALSE
    )
    assert prompt == f"base prompt\n\n{expected}\n"
    assert ("dispatcher_authored=true" in prompt) is recorded
    assert ("dispatcher_authored=false" in prompt) is not recorded


def test_run_validator_provenance_is_false_when_handoff_differs_from_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_handoff.validator import record_dispatcher_handoff

    repo_root, handoff_path = _write_repo(tmp_path, _LINEAR_VALIDATOR_HANDOFF)
    assert record_dispatcher_handoff(repo_root, _LINEAR_VALIDATOR_HANDOFF)
    # An agent rewrote the handoff after the dispatcher recorded it, and
    # claims dispatcher provenance in its own text.
    handoff_path.write_text(
        _LINEAR_VALIDATOR_HANDOFF + "\ndispatcher_authored=true\n", encoding="utf-8"
    )
    orchestrator = _load_orchestrator_module()
    config = _linear_validator_config(repo_root)

    prompt = _capture_validator_prompt(orchestrator, config, monkeypatch)

    assert prompt.endswith(f"\n\n{orchestrator.LINEAR_VALIDATOR_PROVENANCE_FALSE}\n")
    assert "dispatcher_authored=true" not in prompt


def test_run_validator_launches_without_linear_write_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _handoff_path = _write_repo(tmp_path, _LINEAR_VALIDATOR_HANDOFF)
    orchestrator = _load_orchestrator_module()
    config = _linear_validator_config(repo_root)
    validator = config.agents["validator"]
    assert validator.provider == "claude"
    assert "mcp__linear__save_comment" in validator.claude_allowed_tools
    launched: list[Any] = []

    def fake_invoke(_name: str, _prompt: str, **kwargs: object) -> SubagentResult:
        launched.append(kwargs["agent_config"])
        return _subagent_result(exit_code=1, stdout="", stderr="")

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_invoke)
    orchestrator._run_validator(
        config, "handoff-validator", "base prompt", "Validator", lambda *_: None
    )

    assert len(launched) == 1
    tools = launched[0].claude_allowed_tools
    # Report-only launches read Linear but cannot comment or edit issues.
    assert "mcp__linear__get_issue" in tools
    assert "mcp__linear__list_comments" in tools
    assert "mcp__linear__save_comment" not in tools
    assert "mcp__linear__save_issue" not in tools
    # The routed repair turn keeps the comment grant on the loaded config.
    assert "mcp__linear__save_comment" in (
        config.agents["validator"].claude_allowed_tools
    )


def test_run_validator_keeps_vanilla_agent_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, _handoff_path = _write_repo(tmp_path, _LINEAR_VALIDATOR_HANDOFF)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    launched: list[Any] = []

    def fake_invoke(_name: str, _prompt: str, **kwargs: object) -> SubagentResult:
        launched.append(kwargs["agent_config"])
        return _subagent_result(exit_code=1, stdout="", stderr="")

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_invoke)
    orchestrator._run_validator(
        config, "handoff-validator", "base prompt", "Validator", lambda *_: None
    )

    assert launched == [config.agents["validator"]]
    assert launched[0] is config.agents["validator"]


@pytest.mark.parametrize("recorded", [True, False])
def test_run_validator_leaves_non_linear_prompt_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded: bool,
) -> None:
    from llm_handoff.validator import record_dispatcher_handoff

    repo_root, _handoff_path = _write_repo(tmp_path, _LINEAR_VALIDATOR_HANDOFF)
    if recorded:
        assert record_dispatcher_handoff(repo_root, _LINEAR_VALIDATOR_HANDOFF)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    assert config.tracker.provider == "none"

    prompt = _capture_validator_prompt(orchestrator, config, monkeypatch)

    assert prompt == "base prompt"


@pytest.fixture
def console_controller():
    console_interrupt = importlib.import_module("llm_handoff.console_interrupt")
    console_interrupt.CONTROLLER.reset()
    yield console_interrupt.CONTROLLER
    console_interrupt.CONTROLLER.reset()


@pytest.mark.parametrize("with_stop_file", [False, True])
def test_run_loop_console_ctrl_c_during_dispatch_stops_before_next_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    console_controller: Any,
    with_stop_file: bool,
) -> None:
    import io
    import signal

    from llm_handoff.stop_request import request_stop, stop_request_path

    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **backend:** Implement Story 1.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    dispatches: list[str] = []
    seen_roles: list[str | None] = []
    stderr = io.StringIO()

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        seen_roles.append(console_controller.active_role)
        # The operator presses Ctrl+C once while the backend runs.
        console_controller.handle_sigint(signal.SIGINT, None, stream=stderr)
        if with_stop_file:
            # `llm-handoff stop` during the same dispatch.
            request_stop(repo_root)
        path.write_text(
            """## Next Step

- **auditor:** Audit Story 1.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda *_a, **_k: pytest.fail("no dispatch may start after the stop"),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=3,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert dispatches == ["backend"]
    assert seen_roles == ["backend"]
    assert stderr.getvalue() == (
        "Stop requested; finishing the current backend run. "
        "Press Ctrl+C again to stop now.\n"
    )
    stop_lines = [
        message
        for level, message in log_messages
        if level == "PAUSE" and message.startswith("Stop requested (")
    ]
    assert len(stop_lines) == 1
    if with_stop_file:
        assert str(stop_request_path(repo_root)) in stop_lines[0]
    else:
        assert stop_lines[0].startswith("Stop requested (Ctrl+C).")
    assert (
        "PAUSE",
        "Stopped at the operator's request before the next dispatch. "
        "Exiting with code 0.",
    ) in log_messages
    assert not stop_request_path(repo_root).exists()
    # The loop top cleared the active role, so a later Ctrl+C stops at once.
    assert console_controller.active_role is None
    assert sum(1 for _l, m in log_messages if m.startswith("--- Cycle")) == 1
    assert "- **auditor:** Audit Story 1." in handoff_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Verification preflight in the dispatcher (ACTIVE-4)
# ---------------------------------------------------------------------------

# Never on PATH, so the shared preflight always reports a missing executable.
_PREFLIGHT_MISSING_TOOL = "llm-handoff-preflight-missing-tool"


def _preflight_packet(
    command: str,
    *,
    next_agent: str = "backend",
    producer: str = "planner",
) -> str:
    return (
        "---\n"
        f"next_agent: {next_agent}\n"
        'reason: "Planner assigns the story."\n'
        'epic_id: "E1"\n'
        'story_id: "S1"\n'
        'story_title: "First story"\n'
        f"producer: {producer}\n"
        "---\n\n"
        "# Handoff: S1 First story\n\n"
        "## Work Packet\n\n"
        "- **Objective:** Add a small feature.\n"
        "- **Files in scope:** `src/app.py`\n"
        "- **Files out of bounds:** none\n"
        "- **Context:** None.\n"
        "- **Verification command:**\n"
        f"  - {command}\n"
        "- **Expected next route:** auditor\n"
    )


def _preflight_lines(
    log_messages: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    return [
        (level, message)
        for level, message in log_messages
        if message.startswith("verification preflight:")
    ]


def _expected_preflight_warnings(config: Any, content: str) -> list[tuple[str, str]]:
    doctor = importlib.import_module("llm_handoff.doctor")
    expected = [
        ("WARN", f"{check.name}: {check.message}")
        for check in doctor.check_verification_preflight(config, content)
        if check.status == "WARN"
    ]
    assert expected, "the fixture must produce a shared preflight warning"
    return expected


def _forbid_agents_and_verification(
    orchestrator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must not launch an agent or run verification")

    for name in (
        "invoke_backend_role",
        "invoke_frontend_role",
        "invoke_planner_role",
        "verify_work_claim",
    ):
        monkeypatch.setattr(orchestrator, name, unexpected)


@pytest.mark.parametrize(
    ("route", "command"),
    [
        # Missing executable (backend runs on Codex by default).
        ("backend", f"{_PREFLIGHT_MISSING_TOOL} --check"),
        # Denied provider grant: agy gets no interpreter grant by default.
        ("frontend", "python -m pytest tests -q"),
    ],
)
def test_startup_verification_preflight_logs_shared_warning_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    command: str,
) -> None:
    content = _preflight_packet(command, next_agent=route)
    repo_root, handoff_path = _write_repo(tmp_path, content)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    expected = _expected_preflight_warnings(config, content)
    _forbid_agents_and_verification(orchestrator, monkeypatch)
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    # Startup logs the shared lines once; the first cycle skips the same
    # content instead of repeating them.
    assert _preflight_lines(log_messages) == expected
    startup_index = log_messages.index(expected[0])
    first_cycle_index = next(
        index
        for index, (_level, message) in enumerate(log_messages)
        if message.startswith("--- Cycle")
    )
    assert startup_index < first_cycle_index
    if route == "frontend":
        assert any("denied provider grant" in message for _l, message in expected)
    else:
        assert any("missing executable" in message for _l, message in expected)
    assert any(message.startswith("[DRY RUN] Would") for _l, message in log_messages)
    assert handoff_path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("route", ["backend", "frontend"])
def test_cycle_verification_preflight_runs_in_dry_run_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    content = _preflight_packet(
        f"{_PREFLIGHT_MISSING_TOOL} --check", next_agent=route
    )
    repo_root, _handoff_path = _write_repo(tmp_path, content)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    expected = _expected_preflight_warnings(config, content)
    _forbid_agents_and_verification(orchestrator, monkeypatch)
    # Without the startup check, the dry-run preview must still report it:
    # the per-cycle call sits before the dry-run early return.
    monkeypatch.setattr(
        orchestrator,
        "_log_startup_verification_preflight",
        lambda _config, _log: None,
    )
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert _preflight_lines(log_messages) == expected
    first_cycle_index = next(
        index
        for index, (_level, message) in enumerate(log_messages)
        if message.startswith("--- Cycle")
    )
    assert log_messages.index(expected[0]) > first_cycle_index


def test_verification_preflight_reports_rejected_gate_prefix_as_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _preflight_packet("make check")
    repo_root, _handoff_path = _write_repo(tmp_path, content)
    orchestrator = _load_orchestrator_module()
    config_module = importlib.import_module("llm_handoff.config")
    config = config_module.DispatchConfig(
        repo_root=repo_root,
        pause_recovery="off",
        dry_run=True,
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        enforce_work_packet=True,
        verification_allowed_prefixes=("python -m pytest",),
    )
    expected = _expected_preflight_warnings(config, content)
    _forbid_agents_and_verification(orchestrator, monkeypatch)
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert any("rejected by gate prefix" in message for _l, message in expected)
    assert _preflight_lines(log_messages) == expected


@pytest.mark.parametrize("route", ["backend", "frontend"])
def test_cycle_verification_preflight_checks_work_packet_written_mid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    startup_content = _preflight_packet(
        "python -m pytest tests -q", next_agent="planner", producer="user"
    )
    planner_packet = _preflight_packet(
        f"{_PREFLIGHT_MISSING_TOOL} --mid-run", next_agent=route
    )
    repo_root, handoff_path = _write_repo(tmp_path, startup_content)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root)
    expected = _expected_preflight_warnings(config, planner_packet)
    events: list[tuple[str, str]] = []

    def fake_planner(path: Path, **_kwargs: Any) -> DispatchResult:
        events.append(("DISPATCHED", "planner"))
        path.write_text(planner_packet, encoding="utf-8")
        return _dispatch_result()

    def fake_implementer(path: Path, **_kwargs: Any) -> DispatchResult:
        events.append(("DISPATCHED", route))
        path.write_text(
            "---\n"
            "next_agent: user\n"
            'reason: "Need a decision."\n'
            "status: blocked_missing_context\n"
            f"producer: {route}\n"
            "---\n\n"
            "## user\n\nWhich tool should verify this?\n",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, f"invoke_{route}_role", fake_implementer)
    monkeypatch.setattr(
        orchestrator, "validate_handoff", lambda *_args, **_kwargs: _validation_result()
    )
    monkeypatch.setattr(orchestrator, "_git_head", Mock(return_value="a" * 40))

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=2,
        log=lambda level, message: events.append((level, message)),
    )

    assert exit_code == 0
    # The warning names the planner's new packet, not the startup content,
    # and the implementer still runs after it.
    assert _preflight_lines(events) == expected
    assert all("--mid-run" in message for _l, message in expected)
    planner_index = events.index(("DISPATCHED", "planner"))
    warning_index = events.index(expected[0])
    implementer_index = events.index(("DISPATCHED", route))
    assert planner_index < warning_index < implementer_index
    assert "next_agent: user" in handoff_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    [
        # Non-implementer route with a Work Packet.
        _preflight_packet(
            f"{_PREFLIGHT_MISSING_TOOL} --check", next_agent="auditor"
        ),
        # Implementer route without a Work Packet.
        """---
next_agent: backend
reason: "Implement the story."
producer: planner
---

## Next Step

- **backend:** Implement the story.
""",
        # Implementer route whose Work Packet lists no Verification command.
        _preflight_packet("placeholder-command").replace(
            "  - placeholder-command\n", ""
        ),
        # Malformed frontmatter.
        "---\nnext_agent: [\n---\n",
    ],
    ids=[
        "auditor-route",
        "no-work-packet",
        "empty-verification-command",
        "malformed-frontmatter",
    ],
)
def test_verification_preflight_silent_without_implementer_work_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    repo_root, _handoff_path = _write_repo(tmp_path, content)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    doctor = importlib.import_module("llm_handoff.doctor")
    assert doctor.check_verification_preflight(config, content) == []
    _forbid_agents_and_verification(orchestrator, monkeypatch)
    # An unroutable handoff asks the validator for a diagnosis; stub it.
    monkeypatch.setattr(
        orchestrator, "_run_unknown_route_validator", lambda *_a, **_k: None
    )
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert _preflight_lines(log_messages) == []


def test_verification_preflight_logs_pass_as_info_and_survives_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doctor = importlib.import_module("llm_handoff.doctor")
    content = _preflight_packet("python -m pytest tests -q")
    repo_root, _handoff_path = _write_repo(tmp_path, content)
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, dry_run=True)
    _forbid_agents_and_verification(orchestrator, monkeypatch)
    passed = doctor.DoctorCheck(
        "PASS", doctor.VERIFICATION_PREFLIGHT_CHECK_NAME, "backend (codex) can run it"
    )
    monkeypatch.setattr(
        orchestrator, "check_verification_preflight", lambda *_a: [passed]
    )
    log_messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert _preflight_lines(log_messages) == [
        ("INFO", "verification preflight: backend (codex) can run it")
    ]

    def broken_check(*_args: Any) -> list[Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "check_verification_preflight", broken_check)
    log_messages.clear()

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    lines = _preflight_lines(log_messages)
    assert lines and all(level == "WARN" for level, _m in lines)
    assert "RuntimeError: boom" in lines[0][1]
    assert any(message.startswith("[DRY RUN] Would") for _l, message in log_messages)
