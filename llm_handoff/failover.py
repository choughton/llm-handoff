"""Recognize fatal agent failures that a configured fallback agent may retry.

A role fails over only when its primary agent could not do the work at all:
the CLI would not launch, was not signed in, hit a rate or usage limit, or
stopped responding until the dispatch timeout killed it. Any other non-zero
exit is the agent's own failure and keeps the normal failure-count path.
"""

from __future__ import annotations

from typing import Literal

from llm_handoff.provider_auth import find_auth_failure


FailoverReason = Literal["launch", "auth", "rate_limit", "timeout"]

FAILOVER_REASON_TEXT: dict[FailoverReason, str] = {
    "launch": "the CLI could not be launched",
    "auth": "the CLI is not signed in",
    "rate_limit": "the provider reported a rate or usage limit",
    "timeout": "the agent stopped responding and hit its timeout",
}

# The agent transcript is in stdout, so only its tail is searched for limit
# messages; a provider prints its final error there, while text the agent
# read or wrote earlier in the run could mention a 429 for unrelated reasons.
_STDOUT_TAIL_CHARS = 4000

RATE_LIMIT_MARKERS = (
    "status 429",
    '"code": 429',
    '"code":429',
    "429 too many requests",
    "ratelimitexceeded",
    "rate_limit_exceeded",
    "rate_limit_error",
    "resource_exhausted",
    "model_capacity_exhausted",
    "overloaded_error",
    "insufficient_quota",
    "exceeded your current quota",
    "quota exceeded",
    "usage limit reached",
    "usage_limit_reached",
    "hit your usage limit",
)

TIMEOUT_MARKER = "process timed out after"

LAUNCH_FAILURE_MARKERS = (
    "[winerror 2]",
    "no such file or directory",
    "is not recognized as an internal or external command",
    "command not found",
)


def classify_fatal_failure(
    *,
    exit_code: int,
    stdout: str | None,
    stderr: str | None,
) -> FailoverReason | None:
    """Return why a failed run is fatal for its agent, or None if it is not."""

    if exit_code == 0:
        return None
    stdout_text = stdout or ""
    stderr_text = (stderr or "").lower()
    if not stdout_text.strip() and any(
        marker in stderr_text for marker in LAUNCH_FAILURE_MARKERS
    ):
        return "launch"
    if find_auth_failure(stdout_text, stderr) is not None:
        return "auth"
    stdout_tail = stdout_text[-_STDOUT_TAIL_CHARS:].lower()
    if any(
        marker in stderr_text or marker in stdout_tail
        for marker in RATE_LIMIT_MARKERS
    ):
        return "rate_limit"
    if TIMEOUT_MARKER in stderr_text:
        return "timeout"
    return None
