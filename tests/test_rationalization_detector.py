from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llm_handoff.agent_types import HandoffStatus
from llm_handoff.rationalization_detector import detect


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_rationalization_patterns_yaml_parses() -> None:
    patterns = yaml.safe_load(
        (REPO_ROOT / "llm_handoff" / "rationalization_patterns.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert {"backend", "planner", "auditor"}.issubset(patterns)
    assert all(len(patterns[role]) >= 3 for role in ("backend", "planner", "auditor"))


def test_rationalization_detector_no_match() -> None:
    assert (
        detect(
            "backend",
            "## Verification Evidence\n\n- **Commands run:** `pytest`",
            HandoffStatus.READY_FOR_REVIEW,
        )
        == []
    )


def test_rationalization_detector_match_without_done_state() -> None:
    assert (
        detect(
            "backend",
            "Let me first explore the codebase before writing the fix.",
            HandoffStatus.BLOCKED_MISSING_CONTEXT,
        )
        == []
    )


def test_rationalization_detector_match_with_done_state() -> None:
    matches = detect(
        "backend",
        "Let me first explore the codebase before writing the fix.",
        HandoffStatus.READY_FOR_REVIEW,
    )

    assert len(matches) == 1
    assert matches[0].line_number == 1
    assert matches[0].phrase == "Let me first explore"


# The gate-verified handback wording from HANDBOOK.md "Gate-Verified Handback".
GATE_DEFERRAL_EVIDENCE = (
    "## Verification Evidence\n\n"
    '- **Commands run:** deferred to the work-claim gate: "Permission to use '
    'Bash with command python -m pytest tests -q has been denied."\n'
    "- **Output summary:** `python -m pytest tests -q` was refused by a "
    "permission rule before it ran; no test result.\n"
)


@pytest.mark.parametrize("role", ["backend", "frontend"])
def test_gate_deferral_wording_is_not_a_verification_deferral(role: str) -> None:
    matches = detect(role, GATE_DEFERRAL_EVIDENCE, HandoffStatus.READY_FOR_REVIEW)

    assert [match.failure_mode for match in matches] == []


TEST_SCOPE_STOP_FAILURE = (
    "planner told the implementer to stop for a test outside Files in scope"
)


@pytest.mark.parametrize(
    "line",
    [
        # The canonical forbidden instruction from the handbook.
        "- Stop if a test outside Files in scope must change.",
        # Close variants: other stop verbs, word order, and case.
        "- HALT and route to planner when any tests outside the listed scope "
        "need updating.",
        "- If a test outside Files in scope has to be changed, stop and report.",
        # A negation in an earlier clause does not cover a later stop clause.
        "- Do not edit other files. Stop if a test outside Files in scope "
        "must change.",
    ],
)
def test_planner_test_scope_stop_instruction_is_flagged(line: str) -> None:
    body = f"## Work Packet\n\n- **Objective:** Change the value.\n{line}\n"

    matches = detect("planner", body, HandoffStatus.READY_FOR_REVIEW)

    assert [match.failure_mode for match in matches] == [TEST_SCOPE_STOP_FAILURE]
    assert matches[0].line_number == 4


@pytest.mark.parametrize(
    "line",
    [
        "- **Files in scope:** `src/app.py`, `tests/`",
        "- Tests that assert the changed behavior are in scope; list them or "
        "`tests/` in Files in scope.",
        "- Do not stop because a test outside the listed files needs updating; "
        "update it.",
        # The policy sentences themselves, as the planner artifacts and the
        # handbook state them: the negation is several words before "stop".
        "Never write an instruction to stop because a test outside Files in "
        "scope must change, in any wording.",
        "Never write an instruction to stop because a test outside Files in "
        "scope must change",
        "A Work Packet never tells the implementer to stop because a test "
        "outside Files in scope must change, in that wording or any other.",
        "- Prohibit instructions to stop because a test outside Files in scope "
        "must change.",
        "- Don't tell the implementer to halt when a test outside the listed "
        "scope needs updating.",
        "- If a test outside Files in scope must change, do not stop; update it.",
        'The planner never writes "stop if a test outside Files in scope must '
        'change" or a variant.',
    ],
)
def test_planner_affirmative_test_scope_wording_is_not_flagged(line: str) -> None:
    body = f"## Work Packet\n\n{line}\n"

    matches = detect("planner", body, HandoffStatus.READY_FOR_REVIEW)

    assert TEST_SCOPE_STOP_FAILURE not in [match.failure_mode for match in matches]


def test_planner_test_scope_stop_instruction_needs_a_completion_status() -> None:
    assert (
        detect(
            "planner",
            "- Stop if a test outside Files in scope must change.",
            HandoffStatus.BLOCKED_IMPLEMENTATION_FAILURE,
        )
        == []
    )


@pytest.mark.parametrize("role", ["backend", "frontend"])
def test_will_test_later_is_still_a_verification_deferral(role: str) -> None:
    matches = detect(
        role,
        GATE_DEFERRAL_EVIDENCE + "- **Unresolved concerns:** I will test later.\n",
        HandoffStatus.READY_FOR_REVIEW,
    )

    assert [match.failure_mode for match in matches] == ["verification deferred"]
    assert matches[0].phrase == "will test later"
