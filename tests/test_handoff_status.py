from __future__ import annotations

import pytest

from llm_handoff.agent_types import HandoffStatus
from llm_handoff.roles import HandoffStatus as ExportedHandoffStatus


def test_handoff_status_enum_values() -> None:
    assert {status.value for status in HandoffStatus} == {
        "ready_for_review",
        "verified_pass",
        "verified_fail",
        "blocked_missing_context",
        "blocked_implementation_failure",
        "escalate_to_user",
    }
    assert HandoffStatus.ready_for_review is HandoffStatus.READY_FOR_REVIEW
    assert ExportedHandoffStatus is HandoffStatus


def test_handoff_status_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        HandoffStatus("done")
