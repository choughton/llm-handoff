from __future__ import annotations

from pathlib import Path

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
