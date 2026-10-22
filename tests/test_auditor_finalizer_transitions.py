"""TRANS-2: route and status legality for auditor and finalizer producers.

A `producer: finalizer` handoff routes only to `planner`, `tpm`, or `user`,
never sets `close_type`, and may emit only `verified_pass`,
`blocked_missing_context`, `escalate_to_user`, or no status. A
`producer: auditor` handoff never emits `ready_for_review` and never routes
to `auditor`. `verified_pass` and `verified_fail` belong to the auditor (and
the finalizer's epic-close `verified_pass`); validator producers carry them
only in dispatcher-written handoffs (TRANS-3).
"""

from __future__ import annotations

import pytest

from llm_handoff.roles import CANONICAL_NEXT_AGENT_ROLES
from llm_handoff.router import HandoffRouting
from llm_handoff.validator import validate_handoff_frontmatter, validate_handoff_text


SCOPE_SHA = "0a040ac"
FINALIZER_ALLOWED_ROUTES = ("planner", "tpm", "user")
FINALIZER_REJECTED_ROUTES = tuple(
    role for role in CANONICAL_NEXT_AGENT_ROLES if role not in FINALIZER_ALLOWED_ROUTES
)
STATUS_RULE = "frontmatter_status_producer_rule:"
ROUTING_RULE = "frontmatter_routing_rule:"


def _frontmatter_errors(**fields: str) -> list[str]:
    values = {"reason": "Transition handoff.", **fields}
    return validate_handoff_frontmatter(HandoffRouting(**values)).errors


def _finalizer_route_error(next_agent: str) -> str:
    return (
        "frontmatter_routing_rule: producer `finalizer` cannot route to "
        f"next_agent `{next_agent}`; a finalizer routes only to "
        "`planner`, `tpm`, `user`."
    )


def _finalizer_close_type_error(close_type: str) -> str:
    return (
        "frontmatter_routing_rule: producer `finalizer` emitted close_type "
        f"`{close_type}`; a finalizer records a closed epic and omits close_type."
    )


def _finalizer_status_error(status: str) -> str:
    return (
        "frontmatter_status_producer_rule: producer `finalizer` cannot emit "
        f"status `{status}`; a finalizer omits status or uses "
        "`verified_pass`, `blocked_missing_context`, or `escalate_to_user`."
    )


AUDITOR_SELF_ROUTE_ERROR = (
    "frontmatter_routing_rule: producer `auditor` cannot route to "
    "next_agent `auditor`; an auditor approves, fails, or escalates the work "
    "it reviewed."
)
AUDITOR_READY_ERROR = (
    "frontmatter_status_producer_rule: producer `auditor` cannot emit status "
    "`ready_for_review`; an auditor approves or fails work with "
    "`verified_pass` or `verified_fail`."
)


def _verified_status_error(producer: str, status: str) -> str:
    return (
        f"frontmatter_status_producer_rule: producer `{producer}` cannot emit "
        f"status `{status}`; only the auditor approves or fails work, and only "
        "the finalizer carries an epic-close `verified_pass` forward."
    )


def _finalizer_dispatch_error(previous_route: str) -> str:
    return (
        "frontmatter_status_producer_rule: producer `finalizer` emitted status "
        f"`verified_pass`, but the dispatched role is `{previous_route}`; only a "
        "finalizer dispatch carries the epic-close approval forward."
    )


def _rule_errors(errors: list[str]) -> list[str]:
    return [error for error in errors if error.startswith(STATUS_RULE)]


# Finalizer routes


@pytest.mark.parametrize("next_agent", FINALIZER_ALLOWED_ROUTES)
def test_finalizer_allowed_routes_pass(next_agent: str) -> None:
    assert _frontmatter_errors(next_agent=next_agent, producer="finalizer") == []


@pytest.mark.parametrize("next_agent", FINALIZER_REJECTED_ROUTES)
def test_finalizer_rejected_routes_fail(next_agent: str) -> None:
    errors = _frontmatter_errors(next_agent=next_agent, producer="finalizer")

    assert _finalizer_route_error(next_agent) in errors


def test_finalizer_rejected_routes_cover_every_other_role() -> None:
    assert set(FINALIZER_REJECTED_ROUTES) == {
        "backend",
        "frontend",
        "auditor",
        "validator",
        "finalizer",
    }


def test_finalizer_producer_label_is_normalized() -> None:
    errors = _frontmatter_errors(next_agent="backend", producer=" Finalizer ")

    assert _finalizer_route_error("backend") in errors


def test_finalizer_allowed_route_keeps_status_route_constraints() -> None:
    # blocked_missing_context pauses for user, so routing it to planner is
    # still a status/route conflict even though planner is an allowed route.
    errors = _frontmatter_errors(
        next_agent="planner", producer="finalizer", status="blocked_missing_context"
    )

    assert any(error.startswith("frontmatter_status_route_conflict:") for error in errors)
    assert _rule_errors(errors) == []


# Finalizer close types


@pytest.mark.parametrize("close_type", ["story", "epic"])
def test_finalizer_close_type_is_rejected(close_type: str) -> None:
    errors = _frontmatter_errors(
        next_agent="planner",
        producer="finalizer",
        close_type=close_type,
        scope_sha=SCOPE_SHA,
    )

    assert _finalizer_close_type_error(close_type) in errors


# Finalizer statuses


@pytest.mark.parametrize(
    ("status", "next_agent"),
    [
        (None, "planner"),
        (None, "tpm"),
        (None, "user"),
        ("verified_pass", "planner"),
        ("verified_pass", "tpm"),
        ("blocked_missing_context", "user"),
        ("escalate_to_user", "user"),
    ],
)
def test_finalizer_legal_statuses_pass(status: str | None, next_agent: str) -> None:
    fields = {"next_agent": next_agent, "producer": "finalizer"}
    if status is not None:
        fields["status"] = status

    assert _frontmatter_errors(**fields) == []


@pytest.mark.parametrize(
    "status", ["ready_for_review", "verified_fail", "blocked_implementation_failure"]
)
def test_finalizer_rejected_statuses_fail(status: str) -> None:
    errors = _frontmatter_errors(
        next_agent="planner", producer="finalizer", status=status
    )

    assert _finalizer_status_error(status) in errors


# Auditor


def test_auditor_ready_for_review_is_rejected() -> None:
    errors = _frontmatter_errors(
        next_agent="planner", producer="auditor", status="ready_for_review"
    )

    assert AUDITOR_READY_ERROR in errors


def test_auditor_self_route_is_rejected() -> None:
    errors = _frontmatter_errors(next_agent="auditor", producer="auditor")

    assert errors == [AUDITOR_SELF_ROUTE_ERROR]


def test_auditor_producer_label_is_normalized() -> None:
    errors = _frontmatter_errors(
        next_agent="planner", producer="AUDITOR", status="ready_for_review"
    )

    assert AUDITOR_READY_ERROR in errors


_AUDITOR_LEGAL_VERDICTS = [
    ("verified_pass", "planner", "story"),
    ("verified_pass", "tpm", "story"),
    ("verified_pass", "finalizer", "epic"),
    ("verified_fail", "backend", "story"),
    ("verified_fail", "frontend", "story"),
    ("verified_fail", "planner", "story"),
]


@pytest.mark.parametrize(("status", "next_agent", "close_type"), _AUDITOR_LEGAL_VERDICTS)
def test_auditor_legal_verdicts_pass(
    status: str, next_agent: str, close_type: str
) -> None:
    errors = _frontmatter_errors(
        next_agent=next_agent,
        producer="auditor",
        status=status,
        close_type=close_type,
        scope_sha=SCOPE_SHA,
    )

    assert errors == []


# Verdict statuses from other producers


@pytest.mark.parametrize(
    "producer", ["planner", "user", "backend", "frontend", "tpm", "reviewer-bot"]
)
@pytest.mark.parametrize("status", ["verified_pass", "verified_fail"])
def test_non_auditor_verdict_statuses_are_rejected(producer: str, status: str) -> None:
    errors = _frontmatter_errors(next_agent="planner", producer=producer, status=status)

    assert _rule_errors(errors), errors


@pytest.mark.parametrize("producer", ["planner", "user", "reviewer-bot"])
@pytest.mark.parametrize("status", ["verified_pass", "verified_fail"])
def test_generic_verdict_rule_names_the_producer(producer: str, status: str) -> None:
    errors = _frontmatter_errors(next_agent="planner", producer=producer, status=status)

    assert errors == [_verified_status_error(producer, status)]


@pytest.mark.parametrize(
    ("next_agent", "status"),
    [("backend", "verified_fail"), ("planner", "verified_pass")],
)
def test_validator_verdicts_need_dispatcher_authored(next_agent: str, status: str) -> None:
    # TRANS-3 locks validator verdicts behind dispatcher_authored; see
    # tests/test_validator_transitions.py.
    errors = _frontmatter_errors(
        next_agent=next_agent, producer="validator", status=status
    )
    trusted = validate_handoff_frontmatter(
        HandoffRouting(
            next_agent=next_agent,
            reason="Transition handoff.",
            producer="validator",
            status=status,
        ),
        dispatcher_authored=True,
    ).errors

    assert len(_rule_errors(errors)) == 1
    assert trusted == []


# Post-dispatch validation


_EVIDENCE = f"""
## Verification Evidence

- **Commands run:** `python -m pytest tests -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {SCOPE_SHA}
- **Files changed or reviewed:** llm_handoff/validator.py
- **Unresolved concerns:** none
"""
_NO_EVIDENCE = f"\nEpic recorded in PROJECT_STATE.md at {SCOPE_SHA}.\n"


def _handoff(
    *,
    producer: str,
    next_agent: str,
    status: str,
    close_type: str | None = None,
    body: str = _EVIDENCE,
) -> str:
    lines = [
        "---",
        f"next_agent: {next_agent}",
        'reason: "Transition handoff."',
        f"status: {status}",
        f"scope_sha: {SCOPE_SHA}",
    ]
    if close_type is not None:
        lines.append(f"close_type: {close_type}")
    lines.extend([f"producer: {producer}", "---", ""])
    return "\n".join(lines) + f"\n# {producer} Report\n\n**Agent:** {producer}\n{body}"


@pytest.mark.parametrize(("status", "next_agent", "close_type"), _AUDITOR_LEGAL_VERDICTS)
def test_post_dispatch_auditor_verdicts_pass(
    status: str, next_agent: str, close_type: str
) -> None:
    result = validate_handoff_text(
        _handoff(
            producer="auditor",
            next_agent=next_agent,
            status=status,
            close_type=close_type,
        ),
        "auditor",
    )

    assert result.errors == []
    assert result.routing_instruction == next_agent


def test_post_dispatch_auditor_verdict_still_needs_evidence() -> None:
    result = validate_handoff_text(
        _handoff(
            producer="auditor",
            next_agent="finalizer",
            status="verified_pass",
            close_type="epic",
            body=_NO_EVIDENCE,
        ),
        "auditor",
    )

    assert any(error.startswith("EVIDENCE_MISSING:") for error in result.errors)


def test_post_dispatch_auditor_ready_for_review_with_evidence_is_rejected() -> None:
    result = validate_handoff_text(
        _handoff(producer="auditor", next_agent="auditor", status="ready_for_review"),
        "auditor",
    )

    assert AUDITOR_READY_ERROR in result.errors
    assert AUDITOR_SELF_ROUTE_ERROR in result.errors
    assert result.verdict == "NO"


@pytest.mark.parametrize("producer", ["planner", "user", "reviewer-bot"])
@pytest.mark.parametrize("status", ["verified_pass", "verified_fail"])
def test_post_dispatch_verdict_with_evidence_from_non_auditor_is_rejected(
    producer: str, status: str
) -> None:
    result = validate_handoff_text(
        _handoff(producer=producer, next_agent="planner", status=status),
        "auditor",
    )

    assert _verified_status_error(producer, status) in result.errors
    assert result.verdict == "NO"


@pytest.mark.parametrize(
    "status", ["ready_for_review", "verified_fail", "blocked_implementation_failure"]
)
def test_post_dispatch_finalizer_rejected_status_with_evidence(status: str) -> None:
    result = validate_handoff_text(
        _handoff(producer="finalizer", next_agent="planner", status=status),
        "finalizer",
    )

    assert _finalizer_status_error(status) in result.errors


def test_post_dispatch_finalizer_rejected_route_with_evidence() -> None:
    result = validate_handoff_text(
        _handoff(producer="finalizer", next_agent="auditor", status="escalate_to_user"),
        "finalizer",
    )

    assert _finalizer_route_error("auditor") in result.errors


# Finalizer evidence exemption


@pytest.mark.parametrize("previous_agent", ["auditor", "planner", "backend", "tpm"])
def test_finalizer_verified_pass_after_other_dispatch_is_rejected(
    previous_agent: str,
) -> None:
    # Populated evidence does not make the claim legal.
    result = validate_handoff_text(
        _handoff(producer="finalizer", next_agent="planner", status="verified_pass"),
        previous_agent,
    )

    assert _finalizer_dispatch_error(previous_agent) in result.errors
    assert result.verdict == "NO"


def test_finalizer_verified_pass_after_other_dispatch_without_evidence() -> None:
    result = validate_handoff_text(
        _handoff(
            producer="finalizer",
            next_agent="planner",
            status="verified_pass",
            body=_NO_EVIDENCE,
        ),
        "auditor",
    )

    assert _finalizer_dispatch_error("auditor") in result.errors
    assert any(error.startswith("EVIDENCE_MISSING:") for error in result.errors)


@pytest.mark.parametrize("next_agent", ["planner", "tpm"])
def test_finalizer_dispatch_verified_pass_is_exempt_from_evidence(
    next_agent: str,
) -> None:
    result = validate_handoff_text(
        _handoff(
            producer="finalizer",
            next_agent=next_agent,
            status="verified_pass",
            body=_NO_EVIDENCE,
        ),
        "finalizer",
    )

    assert result.errors == []
    assert result.routing_instruction == next_agent


def test_preflight_passing_producer_as_previous_agent_keeps_exemption() -> None:
    # doctor and the supervisor preflight pass frontmatter.producer as the
    # previous agent; a normalized label must still match.
    result = validate_handoff_text(
        _handoff(
            producer="Finalizer",
            next_agent="planner",
            status="verified_pass",
            body=_NO_EVIDENCE,
        ),
        "Finalizer",
    )

    assert result.errors == []


@pytest.mark.parametrize("producer", ["auditor", "planner", "validator"])
def test_other_producer_after_finalizer_dispatch_needs_evidence(producer: str) -> None:
    result = validate_handoff_text(
        _handoff(
            producer=producer,
            next_agent="planner",
            status="verified_pass",
            body=_NO_EVIDENCE,
        ),
        "finalizer",
    )

    assert any(error.startswith("EVIDENCE_MISSING:") for error in result.errors)


def test_unknown_previous_role_grants_no_exemption() -> None:
    result = validate_handoff_text(
        _handoff(
            producer="finalizer",
            next_agent="planner",
            status="verified_pass",
            body=_NO_EVIDENCE,
        ),
        "mystery-bot",
    )

    assert any(error.startswith("EVIDENCE_MISSING:") for error in result.errors)
    assert not any("dispatched role" in error for error in result.errors)
