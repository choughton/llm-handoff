from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


LogFn = Callable[[str, str], None]


class HandoffStatus(str, Enum):
    ready_for_review = "ready_for_review"
    verified_pass = "verified_pass"
    verified_fail = "verified_fail"
    blocked_missing_context = "blocked_missing_context"
    blocked_implementation_failure = "blocked_implementation_failure"
    escalate_to_user = "escalate_to_user"

    READY_FOR_REVIEW = "ready_for_review"
    VERIFIED_PASS = "verified_pass"
    VERIFIED_FAIL = "verified_fail"
    BLOCKED_MISSING_CONTEXT = "blocked_missing_context"
    BLOCKED_IMPLEMENTATION_FAILURE = "blocked_implementation_failure"
    ESCALATE_TO_USER = "escalate_to_user"


COMPLETION_STATUSES = frozenset(
    {
        HandoffStatus.READY_FOR_REVIEW,
        HandoffStatus.VERIFIED_PASS,
        HandoffStatus.VERIFIED_FAIL,
    }
)

BLOCKED_STATUSES = frozenset(
    {
        HandoffStatus.BLOCKED_MISSING_CONTEXT,
        HandoffStatus.BLOCKED_IMPLEMENTATION_FAILURE,
        HandoffStatus.ESCALATE_TO_USER,
    }
)


@dataclass(frozen=True)
class DispatchResult:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float
    session_id: str | None = None
    session_invalidated: bool = False
    usage: dict[str, object] | None = None
    # Set when a run failed in a way a retry cannot fix (for example, an
    # audit reviewer CLI that could not start); run_loop stops with exit 1.
    stop_loop: bool = False
    # Optional message run_loop logs when it stops for stop_loop; without
    # it, run_loop logs the audit-reviewer start failure message.
    stop_reason: str | None = None


@dataclass(frozen=True)
class SubagentResult:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float
    usage: dict[str, object] | None = None
    # Provider session id (Claude stream-json `system`/`init` event), or None.
    session_id: str | None = None


@dataclass(frozen=True)
class _ProcessResult:
    stdout: str
    stderr: str
    exit_code: int
    session_id: str | None = None
    usage: dict[str, object] | None = None
