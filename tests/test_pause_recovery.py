"""Bounded planner recovery at dispatcher pauses (`pause_recovery`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from llm_handoff import orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import DispatchConfig
from llm_handoff.ledger import EpicCloseResult
from llm_handoff.validator import ValidationResult


NO_ROUTE_HANDOFF = """# No routing data

There is no next step in this file.
"""
BACKEND_HANDOFF = """## Next Step

- **backend:** Implement Story 1.
"""
PLANNER_HANDOFF = """## Next Step

- **planner:** Scope the next epic.
"""
LOW_CONFIDENCE_HANDOFF = """## Task Assignment

**Agent:** backend

## Next Step

- **planner:** Scope the next story.
"""
FINALIZER_HANDOFF = "Next: close epic\n"
USER_HANDOFF = """---
next_agent: user
reason: "Human decision required."
status: escalate_to_user
producer: planner
---

## Question

Which story should run next?
"""
PLANNER_SELF_LOOP_HANDOFF = """---
next_agent: planner
reason: "Planner self-loop fixture."
producer: planner
---

## Task Assignment

**Agent:** planner
"""


def _config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "poll_interval_seconds": 0,
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        "enforce_work_packet": False,
        "work_packet_opt_out_reason": "Tests cover routing without the gate.",
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _write_repo(tmp_path: Path, handoff_content: str) -> Path:
    handoff_path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(handoff_content, encoding="utf-8")
    (tmp_path / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("dispatch test fixture\n", encoding="utf-8")
    return handoff_path


def _dispatch_result() -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


def _validator_output(diagnosis: str) -> SubagentResult:
    return SubagentResult(
        stdout=f"VALID: NO\n  ROUTING: FAIL - {diagnosis}\n",
        stderr="",
        exit_code=0,
        elapsed_seconds=0.01,
    )


def _yes() -> ValidationResult:
    return ValidationResult(verdict="YES", warnings=[], errors=[])


class _Harness:
    """Stubbed agents for one run_loop call."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        handoff_path: Path,
        *,
        planner_writes: list[str],
        backend_writes: list[str | None] | None = None,
        validator_diagnosis: str = "validator diagnosis fixture",
        post_dispatch: Callable[[str], ValidationResult] | None = None,
    ) -> None:
        self.handoff_path = handoff_path
        self.planner_instructions: list[str | None] = []
        self.backend_calls = 0
        self.validator_prompts: list[str] = []
        self.logs: list[tuple[str, str]] = []
        planner_queue = list(planner_writes)
        backend_queue = list(backend_writes or [])

        def fake_planner(path: Path, **kwargs: Any) -> DispatchResult:
            self.planner_instructions.append(kwargs.get("additional_instruction"))
            if planner_queue:
                path.write_text(planner_queue.pop(0), encoding="utf-8")
            return _dispatch_result()

        def fake_backend(path: Path, **_kwargs: Any) -> DispatchResult:
            self.backend_calls += 1
            if backend_queue:
                content = backend_queue.pop(0)
                if content is not None:
                    path.write_text(content, encoding="utf-8")
            return _dispatch_result()

        def fake_support(_name: str, prompt: str, **_kwargs: Any) -> SubagentResult:
            self.validator_prompts.append(prompt)
            return _validator_output(validator_diagnosis)

        def fake_validate(_path: Path, previous_agent: str, **_kwargs: Any):
            if post_dispatch is not None:
                return post_dispatch(previous_agent)
            return _yes()

        monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
        monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
        monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
        monkeypatch.setattr(orchestrator, "validate_handoff", fake_validate)
        monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)

    def run(self, config: DispatchConfig, **kwargs: Any) -> int:
        return orchestrator.run_loop(
            config,
            log=lambda level, message: self.logs.append((level, message)),
            **kwargs,
        )

    @property
    def recovery_instructions(self) -> list[str]:
        return [text for text in self.planner_instructions if text is not None]

    def pause_messages(self) -> list[str]:
        return [message for level, message in self.logs if level == "PAUSE"]


def _post_dispatch_error(
    error: str,
) -> Callable[[str], ValidationResult]:
    """NO with `error` for the first planner dispatch, YES afterwards."""

    calls: list[str] = []

    def validate(previous_agent: str) -> ValidationResult:
        calls.append(previous_agent)
        if len(calls) == 1:
            return ValidationResult(verdict="NO", warnings=[], errors=[error])
        return _yes()

    return validate


@pytest.mark.parametrize(
    ("site", "initial_handoff", "planner_writes", "reason_fragment"),
    [
        (
            "unknown_route",
            NO_ROUTE_HANDOFF,
            [USER_HANDOFF],
            "No routing instruction found in HANDOFF.md.",
        ),
        (
            "low_confidence",
            LOW_CONFIDENCE_HANDOFF,
            [USER_HANDOFF],
            "Routing instruction planner is only LOW confidence.",
        ),
        (
            "missing_route_after_dispatch",
            PLANNER_HANDOFF,
            ["# Planner left no route\n", USER_HANDOFF],
            "planner produced HANDOFF.md without a dispatchable route.",
        ),
        (
            "self_loop",
            PLANNER_HANDOFF,
            [PLANNER_SELF_LOOP_HANDOFF, USER_HANDOFF],
            "The planner routed HANDOFF.md back to itself.",
        ),
    ],
)
def test_pause_site_schedules_one_planner_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    initial_handoff: str,
    planner_writes: list[str],
    reason_fragment: str,
) -> None:
    handoff_path = _write_repo(tmp_path, initial_handoff)
    post_dispatch = {
        "missing_route_after_dispatch": _post_dispatch_error(
            "routing_instruction_missing: no dispatchable route."
        ),
        "self_loop": _post_dispatch_error(
            "planner_self_loop: planner handoff routes work back to planner."
        ),
    }.get(site)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        planner_writes=planner_writes,
        validator_diagnosis=f"{site} diagnosis fixture",
        post_dispatch=post_dispatch,
    )

    exit_code = harness.run(_config(tmp_path))

    assert exit_code == 0
    assert len(harness.recovery_instructions) == 1
    instruction = harness.recovery_instructions[0]
    assert reason_fragment in instruction
    assert f"routing: {site} diagnosis fixture" in instruction
    assert "escalate_to_user" in instruction
    assert instruction.isascii()
    assert any(
        reason_fragment in message
        and "scheduling one bounded planner recovery" in message
        for message in harness.pause_messages()
    )


def test_stale_route_pause_schedules_planner_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, BACKEND_HANDOFF)
    # The backend leaves the handoff unchanged, so the next cycle is stale.
    harness = _Harness(
        monkeypatch,
        handoff_path,
        planner_writes=[USER_HANDOFF],
        backend_writes=[None],
        validator_diagnosis="stale diagnosis fixture",
    )

    exit_code = harness.run(_config(tmp_path))

    assert exit_code == 0
    assert harness.backend_calls == 1
    assert len(harness.recovery_instructions) == 1
    instruction = harness.recovery_instructions[0]
    assert "STALE ROUTING DETECTED: same route (backend)" in instruction
    assert "routing: stale diagnosis fixture" in instruction


def test_finalizer_parse_error_pause_schedules_planner_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, FINALIZER_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        planner_writes=[USER_HANDOFF],
        validator_diagnosis="finalizer diagnosis fixture",
    )
    monkeypatch.setattr(
        orchestrator,
        "run_epic_close",
        lambda **_kwargs: EpicCloseResult(
            subagent_exit_code=0,
            stdout="prose instead of structured fields",
            stderr="",
            parse_error="Missing LEDGER UPDATED line.",
        ),
    )

    exit_code = harness.run(_config(tmp_path))

    assert exit_code == 0
    assert len(harness.recovery_instructions) == 1
    instruction = harness.recovery_instructions[0]
    assert "finalizer ledger-updater output was unparseable." in instruction
    assert "routing: finalizer diagnosis fixture" in instruction


def test_recovery_reports_missing_validator_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, planner_writes=[USER_HANDOFF])
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda _name, _prompt, **_kwargs: SubagentResult(
            stdout="", stderr="", exit_code=0, elapsed_seconds=0.01
        ),
    )

    assert harness.run(_config(tmp_path)) == 0

    assert len(harness.recovery_instructions) == 1
    assert "The validator returned no diagnosis." in harness.recovery_instructions[0]


def test_second_pause_in_same_streak_waits_instead_of_recovering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        planner_writes=["# Planner still left no route\n"],
    )

    exit_code = harness.run(_config(tmp_path))

    assert exit_code == 0
    assert len(harness.planner_instructions) == 1
    pauses = harness.pause_messages()
    assert sum("bounded planner recovery" in message for message in pauses) == 1
    assert pauses[-1] == (
        "No routing instruction found in HANDOFF.md. Update the handoff; "
        "dispatch will resume after the file changes."
    )


def test_streak_resets_after_a_normal_dispatch_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        # Recovery routes to backend; the backend then leaves no route.
        planner_writes=[BACKEND_HANDOFF, USER_HANDOFF],
        backend_writes=["# Backend left no route\n"],
    )

    exit_code = harness.run(_config(tmp_path))

    assert exit_code == 0
    assert harness.backend_calls == 1
    assert len(harness.recovery_instructions) == 2
    assert all(
        "No routing instruction found" in text
        for text in harness.recovery_instructions
    )


def test_forced_instruction_cycle_does_not_reset_the_streak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    # The recovery planner's dispatch passes validation, but it ran with a
    # forced instruction, so the next pause still waits.
    harness = _Harness(
        monkeypatch,
        handoff_path,
        planner_writes=[LOW_CONFIDENCE_HANDOFF],
    )

    assert harness.run(_config(tmp_path)) == 0

    assert len(harness.planner_instructions) == 1
    assert harness.pause_messages()[-1].startswith(
        "Routing instruction planner is only LOW confidence."
    )


def test_user_route_never_dispatches_the_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, USER_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, planner_writes=[])

    assert harness.run(_config(tmp_path)) == 0

    assert harness.planner_instructions == []
    assert not any(
        "bounded planner recovery" in message for message in harness.pause_messages()
    )


def test_protected_file_escalation_by_pause_validator_blocks_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, planner_writes=[BACKEND_HANDOFF])

    def protected_change(_name: str, prompt: str, **_kwargs: Any) -> SubagentResult:
        harness.validator_prompts.append(prompt)
        (tmp_path / "AGENTS.md").write_text("edited by validator\n", encoding="utf-8")
        return _validator_output("protected fixture")

    monkeypatch.setattr(orchestrator, "invoke_support_role", protected_change)

    assert harness.run(_config(tmp_path)) == 0

    assert harness.planner_instructions == []
    assert harness.backend_calls == 0
    handoff = handoff_path.read_text(encoding="utf-8")
    assert "next_agent: user" in handoff
    assert "protected_file_modified" in handoff
    assert not any(
        "bounded planner recovery" in message for message in harness.pause_messages()
    )
    assert (
        "PAUSE",
        "HANDOFF.md now routes to user; not scheduling planner recovery.",
    ) in harness.logs


def test_planner_route_stale_pause_dispatches_recovery_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, PLANNER_HANDOFF)
    # The first planner leaves the handoff unchanged, so the next cycle is a
    # stale planner route; the recovery cycle is also a planner route on the
    # same content and must not pause on the stale check again.
    harness = _Harness(
        monkeypatch,
        handoff_path,
        planner_writes=[PLANNER_HANDOFF, USER_HANDOFF],
        validator_diagnosis="stale planner diagnosis fixture",
    )

    assert harness.run(_config(tmp_path)) == 0

    assert len(harness.planner_instructions) == 2
    assert harness.planner_instructions[0] is None
    instruction = harness.planner_instructions[1]
    assert instruction is not None
    assert "STALE ROUTING DETECTED: same route (planner)" in instruction
    assert sum(
        "STALE ROUTING DETECTED" in message for message in harness.pause_messages()
    ) == 1


_UNREPAIRABLE_ERROR = (
    "evidence_sha_unknown: Commit SHA verified `abc` names no commit SHA; "
    "write the 7-40 character SHA of the verified commit."
)
_UNHANDLED_REASON = (
    "Post-dispatch validation failed for planner and no automatic repair "
    f"applies: {_UNREPAIRABLE_ERROR}."
)


def test_unhandled_post_dispatch_failure_schedules_planner_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, PLANNER_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        planner_writes=["# Planner handback\n", USER_HANDOFF],
        post_dispatch=_post_dispatch_error(_UNREPAIRABLE_ERROR),
    )

    exit_code = harness.run(_config(tmp_path))

    assert exit_code == 0
    assert any(
        level == "ERROR" and message.startswith(_UNHANDLED_REASON)
        for level, message in harness.logs
    )
    assert len(harness.recovery_instructions) == 1
    assert _UNHANDLED_REASON in harness.recovery_instructions[0]


def test_unhandled_post_dispatch_failure_pauses_with_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, PLANNER_HANDOFF)
    harness = _Harness(
        monkeypatch,
        handoff_path,
        planner_writes=["# Planner handback\n"],
        post_dispatch=_post_dispatch_error(_UNREPAIRABLE_ERROR),
    )

    exit_code = harness.run(_config(tmp_path, pause_recovery="off"))

    # Without polling the pause cannot wait, so the failure still exits 1, but
    # only after the reason is logged.
    assert exit_code == 1
    assert harness.planner_instructions == [None]
    errors = [message for level, message in harness.logs if level == "ERROR"]
    assert any(message.startswith(_UNHANDLED_REASON) for message in errors)
    assert any(
        message.startswith(_UNHANDLED_REASON) for message in harness.pause_messages()
    )


def test_unhandled_failure_reason_names_the_dispatcher_commit() -> None:
    result = ValidationResult(verdict="NO", warnings=[], errors=[_UNREPAIRABLE_ERROR])
    commit = orchestrator.DispatcherCommitResult(sha="c" * 40, committed=("a.py",))

    reason = orchestrator._post_dispatch_failure_pause_reason(
        "backend", result, commit
    )

    assert "already committed backend's work as cccccccccccc" in reason
    assert reason.isascii()


def test_pause_recovery_off_keeps_the_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, planner_writes=[USER_HANDOFF])

    assert harness.run(_config(tmp_path, pause_recovery="off")) == 0

    assert harness.planner_instructions == []
    assert harness.pause_messages() == [
        "No routing instruction found in HANDOFF.md. Update the handoff; "
        "dispatch will resume after the file changes."
    ]


def test_dry_run_does_not_schedule_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_path = _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, planner_writes=[USER_HANDOFF])

    assert harness.run(_config(tmp_path, dry_run=True)) == 0

    assert harness.planner_instructions == []
    assert not any(
        "bounded planner recovery" in message for message in harness.pause_messages()
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("planner", "Pause recovery:     planner"), ("off", "Pause recovery:     off")],
)
def test_startup_banner_shows_pause_recovery_mode(
    tmp_path: Path,
    mode: str,
    expected: str,
) -> None:
    logs: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(
        _config(tmp_path, pause_recovery=mode),
        lambda level, message: logs.append((level, message)),
    )

    banner = [message for level, message in logs if message.startswith("Pause recovery:")]
    assert len(banner) == 1
    assert banner[0].startswith(expected)


def test_run_validator_returns_parsed_result_or_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_repo(tmp_path, NO_ROUTE_HANDOFF)
    config = _config(tmp_path)
    outputs = [
        _validator_output("parsed fixture"),
        SubagentResult(stdout="x", stderr="boom", exit_code=1, elapsed_seconds=0.01),
        SubagentResult(stdout="  ", stderr="", exit_code=0, elapsed_seconds=0.01),
    ]
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda _name, _prompt, **_kwargs: outputs.pop(0),
    )

    def log(_level: str, _message: str) -> None:
        return None

    parsed = orchestrator._run_unknown_route_validator(config, log)
    assert parsed is not None
    assert parsed.verdict == "NO"
    assert "routing: parsed fixture" in parsed.errors
    assert orchestrator._run_unknown_route_validator(config, log) is None
    assert orchestrator._run_unknown_route_validator(config, log) is None
