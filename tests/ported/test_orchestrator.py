from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from llm_handoff.agents import DispatchResult, SubagentResult
from llm_handoff.ledger import EpicCloseResult
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
    planner_api_key_env: bool = False,
    backend_resume: bool = True,
    planner_resume: bool = True,
    poll_interval_seconds: int = 0,
    max_consecutive_failures: int = 3,
    agents: dict[str, Any] | None = None,
):
    config_module = importlib.import_module("llm_handoff.config")
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "dry_run": dry_run,
        "use_manual_frontend": use_manual_frontend,
        "planner_api_key_env": planner_api_key_env,
        "backend_resume": backend_resume,
        "planner_resume": planner_resume,
        "poll_interval_seconds": poll_interval_seconds,
        "max_consecutive_failures": max_consecutive_failures,
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
        use_api_key_env=False,
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
        use_api_key_env=False,
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
        handoff_path.write_text(
            """## Next Step

- **backend:** Preview the dispatch.
""",
            encoding="utf-8",
        )

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


def test_run_loop_runs_handoff_validator_and_pauses_on_gemini_self_loop(
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
        lambda path, use_api_key_env=False, additional_instruction=None, log=None, **kwargs: (
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
        handoff_path.write_text(
            """## Next Step

- **backend:** Implement Story 1.
""",
            encoding="utf-8",
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
        handoff_path.write_text(
            """## Next Step

- **backend:** Implement Story 1.
""",
            encoding="utf-8",
        )

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


def test_run_loop_schedules_gemini_recovery_for_non_dispatchable_audit_handoff(
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
    planner_calls: list[tuple[Path, bool, str | None, Any]] = []

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
        use_api_key_env: bool = False,
        additional_instruction: str | None = None,
        log=None,
        **kwargs,
    ) -> DispatchResult:
        assert callable(log)
        planner_calls.append((path, use_api_key_env, additional_instruction, log))
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
    assert planner_calls[0][:2] == (handoff_path, False)
    assert planner_calls[0][2] is not None
    assert "does not contain a dispatchable routing instruction" in planner_calls[0][2]
    assert "canonical dispatchable route" in planner_calls[0][2]
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
        use_api_key_env: bool = False,
        additional_instruction: str | None = None,
        log=None,
        **kwargs,
    ) -> DispatchResult:
        del use_api_key_env, additional_instruction, kwargs
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
        handoff_path.write_text(
            """## Next Step

- **backend:** Implement Story 1.
""",
            encoding="utf-8",
        )

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
        use_api_key_env: bool = False,
        additional_instruction: str | None = None,
        log=None,
        **kwargs,
    ) -> DispatchResult:
        del use_api_key_env, additional_instruction, kwargs
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
    planner_calls: list[tuple[Path, bool, str | None, Any]] = []

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
        lambda path, use_api_key_env=False, additional_instruction=None, log=None, **kwargs: (
            planner_calls.append((path, use_api_key_env, additional_instruction, log))
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
    planner_calls: list[tuple[Path, bool, str | None, Any]] = []
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
        lambda path, use_api_key_env=False, additional_instruction=None, log=None, **kwargs: (
            planner_calls.append((path, use_api_key_env, additional_instruction, log))
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
    assert planner_calls[0][:2] == (handoff_path, False)
    assert planner_calls[0][2] is not None
    assert "prior finalizer cycle already completed" in planner_calls[0][2]
    assert (
        "WARN",
        "STALE finalizer detected after a completed finalizer cycle; redirecting this cycle to the planner for forward routing.",
    ) in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages
    assert ("AGENT", "Post-dispatch gate PASSED for planner.") in log_messages


def test_stale_finalizer_recovery_instruction_is_provider_agnostic() -> None:
    orchestrator = _load_orchestrator_module()

    instruction = orchestrator.STALE_FINALIZER_RECOVERY_INSTRUCTION.lower()

    assert "gemini" not in instruction
    assert "claude" not in instruction
    assert "codex" not in instruction
    old_name = "STALE_EPIC_CLOSE_" + "GEM" + "INI_RECOVERY_INSTRUCTION"
    assert not hasattr(orchestrator, old_name)


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
    planner_calls: list[tuple[Path, bool, str | None, Any]] = []

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
        lambda path, use_api_key_env=False, additional_instruction=None, log=None, **kwargs: (
            planner_calls.append((path, use_api_key_env, additional_instruction, log))
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
    assert planner_calls[0][:2] == (handoff_path, False)
    assert planner_calls[0][2] is not None
    assert "prior finalizer cycle already completed" in planner_calls[0][2]
    assert "repeating finalizer" in planner_calls[0][2]
    assert (
        "WARN",
        "STALE finalizer detected after a completed finalizer cycle; redirecting this cycle to the planner for forward routing.",
    ) in log_messages
    assert ("INFO", "Routing instruction: planner") in log_messages


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
    assert (
        "INFO",
        "Planner API env:    STRIP configured provider API keys",
    ) in log_messages
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
        lambda path, use_manual_frontend=False, use_api_key_env=False, additional_instruction=None, log=None: (
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


def test_run_loop_passes_planner_api_key_opt_in_to_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, handoff_path = _write_repo(
        tmp_path,
        """## Next Step

- **planner:** Review the backend slice.
""",
    )
    orchestrator = _load_orchestrator_module()
    config = _dispatch_config(repo_root, planner_api_key_env=True)
    planner_calls: list[tuple[Path, bool, Any]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda path, use_api_key_env=False, additional_instruction=None, log=None, **kwargs: (
            planner_calls.append((path, use_api_key_env, log)) or _dispatch_result()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *args, **kwargs: _validation_result(),
    )

    exit_code = orchestrator.run_loop(config, max_cycles=1)

    assert exit_code == 0
    assert len(planner_calls) == 1
    assert planner_calls[0][:2] == (handoff_path, True)
    assert callable(planner_calls[0][2])


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
        use_api_key_env: bool = False,
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
                "use_api_key_env": use_api_key_env,
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
                session_id="gemini-session-1",
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
    assert planner_calls[1]["session_id"] == "gemini-session-1"
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
    frontend_calls: list[tuple[Path, bool, bool, Any]] = []
    log_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        orchestrator,
        "invoke_frontend_role",
        lambda path, use_manual_frontend=False, use_api_key_env=False, additional_instruction=None, log=None: (
            frontend_calls.append((path, use_manual_frontend, use_api_key_env, log))
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
    assert frontend_calls == [(handoff_path, False, False, fake_log)]


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
        provider="gemini",
        binary="gemini-custom",
        mention="@auditor",
        retries=2,
        timeout_ms=456,
        agent_name="auditor-gemini",
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
    assert subagent_calls[0]["prompt"] == orchestrator.AUDIT_PROMPT
    assert subagent_calls[0]["role"] == "auditor"
    assert subagent_calls[0]["handoff_path"] == handoff_path
    assert subagent_calls[0]["agent_config"].provider == "gemini"
    assert subagent_calls[0]["agent_config"].agent_name == "auditor-gemini"
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
        handoff_path.write_text(
            """## Next Step

- **backend:** Implement Story 1.
""",
            encoding="utf-8",
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
        handoff_path.write_text(
            """## Next Step

- **backend:** Implement Story 1.
""",
            encoding="utf-8",
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
            "--use-planner-api-key-env",
            "--repo-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 41
    config = captured["config"]
    assert config.repo_root == tmp_path
    assert config.dry_run is True
    assert config.use_manual_frontend is True
    assert config.planner_api_key_env is True
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
        planner_api_key_env=False,
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
