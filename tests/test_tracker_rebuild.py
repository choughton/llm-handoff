"""Linear mode rebuilds a missing or empty HANDOFF.md with one planner cycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import llm_handoff.orchestrator as orchestrator
from llm_handoff.agents import DispatchResult
from llm_handoff.config import DispatchConfig
from llm_handoff.tracker_prompts import linear_role_section
from llm_handoff.validator import ValidationResult

REBUILT_HANDOFF = """---
next_agent: planner
reason: "Planner continues scoping the leased issue."
story_id: ENG-1
tracker_issue: ENG-1
producer: planner
---

## Work Packet

- **Objective:** Continue.
"""


def _linear_tracker() -> dict[str, object]:
    return {
        "provider": "linear",
        "team": "ENG",
        "project": "Platform",
        "eligible_state": "Todo",
        "in_progress_state": "In Progress",
        "success_state": "In Review",
        "mcp_server": "linear",
    }


def _config(
    repo_root: Path,
    *,
    linear: bool,
    pause_recovery: str = "off",
) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "pause_recovery": pause_recovery,
        "poll_interval_seconds": 0,
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
    }
    if linear:
        kwargs["tracker"] = _linear_tracker()
        kwargs["enforce_work_packet"] = True
    else:
        kwargs["enforce_work_packet"] = False
        kwargs["work_packet_opt_out_reason"] = "Tests cover routing only."
    return DispatchConfig(**kwargs)


def _repo(tmp_path: Path, handoff_content: str | None) -> Path:
    handoff_path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    if handoff_content is not None:
        handoff_path.write_text(handoff_content, encoding="utf-8")
    (tmp_path / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("dispatch test fixture\n", encoding="utf-8")
    return handoff_path


def _result() -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


def _validation_yes() -> ValidationResult:
    return ValidationResult(
        verdict="YES", warnings=[], errors=[], routing_instruction="planner"
    )


def _no_validation(*_args: object, **_kwargs: object) -> ValidationResult:
    raise AssertionError("post-dispatch validation must not run")


class _Log:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def __call__(self, level: str, message: str) -> None:
        self.messages.append((level, message))

    def has(self, level: str, fragment: str) -> bool:
        return any(
            lvl == level and fragment in message for lvl, message in self.messages
        )


def _record_planner(
    monkeypatch: pytest.MonkeyPatch, actions: list[Any]
) -> list[str | None]:
    """Patch the planner; each call pops one action applied to the handoff."""

    instructions: list[str | None] = []

    def fake_planner(path: Path, **kwargs: object) -> DispatchResult:
        instruction = kwargs.get("additional_instruction")
        instructions.append(instruction if isinstance(instruction, str) else None)
        action = actions.pop(0)
        if action == "delete":
            path.unlink(missing_ok=True)
        elif isinstance(action, str):
            path.write_text(action, encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    return instructions


def _forbid_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_planner(_path: Path, **_kwargs: object) -> DispatchResult:
        raise AssertionError("the planner must not be dispatched")

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)


def test_rebuild_instruction_is_ascii_and_generic() -> None:
    text = orchestrator.TRACKER_LEASE_REBUILD_INSTRUCTION
    assert text.isascii()
    for fragment in (
        "Linear sections of AGENTS.md",
        "lease_opened",
        "tracker_issue",
        "tracker_url",
        "selection_basis",
        "story_id",
        "## Work Packet",
        "user",
    ):
        assert fragment in text
    assert "status: blocked_missing_context" in text
    assert "escalate_to_user" not in text
    assert "commit" not in text.lower()
    assert "ENG" not in text
    assert orchestrator.TRACKER_LEASE_REBUILD_PAUSE_REASON.isascii()


def test_rebuild_instruction_empty_pool_status_matches_planner_rule() -> None:
    status = "status: blocked_missing_context"
    assert status in orchestrator.TRACKER_LEASE_REBUILD_INSTRUCTION
    assert status in linear_role_section("planner")


def test_vanilla_missing_handoff_still_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _repo(tmp_path, None)
    _forbid_planner(monkeypatch)
    log = _Log()

    exit_code = orchestrator.run_loop(_config(tmp_path, linear=False), log=log)

    assert exit_code == 1
    assert ("ERROR", f"Handoff file not found at: {handoff_path}") in log.messages
    assert not log.has("WARN", "lease-rebuild")


@pytest.mark.parametrize("content", ["", "  \n\t\n"])
def test_vanilla_empty_handoff_uses_unknown_route_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _repo(tmp_path, content)
    _forbid_planner(monkeypatch)
    validator_calls: list[str] = []

    def fake_unknown_validator(*_args: object, **_kwargs: object) -> None:
        validator_calls.append("unknown")
        return None

    monkeypatch.setattr(
        orchestrator, "_run_unknown_route_validator", fake_unknown_validator
    )
    log = _Log()

    exit_code = orchestrator.run_loop(_config(tmp_path, linear=False), log=log)

    assert exit_code == 0
    assert validator_calls == ["unknown"]
    assert log.has("PAUSE", "No routing instruction found in HANDOFF.md")
    assert not log.has("WARN", "lease-rebuild")
    assert not log.has("INFO", "Routing source: tracker_lease_rebuild")


@pytest.mark.parametrize("content", [None, "", "   \n\n"])
def test_linear_first_miss_dispatches_one_rebuild_planner_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    handoff_path = _repo(tmp_path, content)
    instructions = _record_planner(monkeypatch, [REBUILT_HANDOFF])
    monkeypatch.setattr(
        orchestrator, "validate_handoff", lambda *a, **k: _validation_yes()
    )
    log = _Log()

    exit_code = orchestrator.run_loop(
        _config(tmp_path, linear=True), max_cycles=1, log=log
    )

    assert exit_code == 0
    assert instructions == [orchestrator.TRACKER_LEASE_REBUILD_INSTRUCTION]
    rebuild_warnings = [
        message
        for level, message in log.messages
        if level == "WARN" and "lease-rebuild cycle from the tracker pool" in message
    ]
    assert len(rebuild_warnings) == 1
    assert "missing or empty" in rebuild_warnings[0]
    assert ("INFO", "Routing source: tracker_lease_rebuild") in log.messages
    assert not log.has("ERROR", "Handoff file not found")
    assert handoff_path.read_text(encoding="utf-8") == REBUILT_HANDOFF


@pytest.mark.parametrize("content", [None, "\n"])
@pytest.mark.parametrize("planner_action", ["delete", "  \n"])
def test_linear_second_miss_pauses_without_second_planner_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
    planner_action: str,
) -> None:
    _repo(tmp_path, content)
    # A second planner action would mean a second dispatch; pop() would fail.
    instructions = _record_planner(monkeypatch, [planner_action])
    monkeypatch.setattr(orchestrator, "validate_handoff", _no_validation)
    log = _Log()

    # Pause recovery is on, and still no recovery planner is scheduled.
    exit_code = orchestrator.run_loop(
        _config(tmp_path, linear=True, pause_recovery="planner"), log=log
    )

    assert exit_code == 0
    assert instructions == [orchestrator.TRACKER_LEASE_REBUILD_INSTRUCTION]
    assert log.has("WARN", "skipping post-dispatch validation")
    assert ("PAUSE", orchestrator.TRACKER_LEASE_REBUILD_PAUSE_REASON) in log.messages
    assert "did not produce a handoff" in orchestrator.TRACKER_LEASE_REBUILD_PAUSE_REASON
    assert "has content" in orchestrator.TRACKER_LEASE_REBUILD_PAUSE_REASON
    assert not log.has("PAUSE", "Pause recovery: scheduling")


def test_linear_second_miss_resumes_after_the_handoff_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _repo(tmp_path, None)
    instructions = _record_planner(monkeypatch, ["delete", "delete", "delete"])
    monkeypatch.setattr(
        orchestrator, "validate_handoff", lambda *a, **k: _validation_yes()
    )
    pauses: list[dict[str, object]] = []

    def fake_pause(_config: DispatchConfig, **kwargs: object) -> bool:
        pauses.append(kwargs)
        if len(pauses) == 1:
            # An operator writes content; the next cycle routes it normally.
            handoff_path.write_text(REBUILT_HANDOFF, encoding="utf-8")
            return True
        return False

    monkeypatch.setattr(orchestrator, "_pause_until_handoff_changes", fake_pause)
    log = _Log()

    exit_code = orchestrator.run_loop(_config(tmp_path, linear=True), log=log)

    assert exit_code == 0
    assert pauses[0]["reason"] == orchestrator.TRACKER_LEASE_REBUILD_PAUSE_REASON
    assert pauses[0]["handoff_sha"] == orchestrator._content_sha("")
    assert len(pauses) == 2
    # Rebuild, pause, then the operator's handoff routes to planner normally
    # and that planner deletes it, then a new rebuild, then a pause again.
    rebuild = orchestrator.TRACKER_LEASE_REBUILD_INSTRUCTION
    assert instructions[0] == rebuild
    assert instructions[1] != rebuild
    assert instructions[2] == rebuild


def test_linear_non_empty_read_resets_the_miss_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path, None)
    # Rebuild writes a handoff; the normal planner cycle deletes it; the next
    # miss gets a new rebuild cycle, which leaves it missing; then a pause.
    instructions = _record_planner(
        monkeypatch, [REBUILT_HANDOFF, "delete", "delete"]
    )
    monkeypatch.setattr(
        orchestrator, "validate_handoff", lambda *a, **k: _validation_yes()
    )
    log = _Log()

    exit_code = orchestrator.run_loop(_config(tmp_path, linear=True), log=log)

    assert exit_code == 0
    rebuild = orchestrator.TRACKER_LEASE_REBUILD_INSTRUCTION
    assert len(instructions) == 3
    assert instructions[0] == rebuild
    assert instructions[1] != rebuild
    assert instructions[2] == rebuild
    sources = [
        message
        for level, message in log.messages
        if level == "INFO" and message.startswith("Routing source:")
    ]
    rebuild_source = "Routing source: tracker_lease_rebuild"
    assert len(sources) == 3
    assert sources[0] == rebuild_source
    assert sources[1] != rebuild_source
    assert sources[2] == rebuild_source
    assert ("PAUSE", orchestrator.TRACKER_LEASE_REBUILD_PAUSE_REASON) in log.messages
