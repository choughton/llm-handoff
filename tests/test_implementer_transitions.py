"""TRANS-1: route and status legality for implementer producers.

A handoff with `producer: backend` or `producer: frontend` may route only to
`auditor`, `planner`, or `user`, and may not emit `verified_pass`,
`verified_fail`, or `close_type: epic`. Other producers are left to later
TRANSITION-RULES stories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff.roles import CANONICAL_NEXT_AGENT_ROLES
from llm_handoff.router import (
    HandoffFrontmatterError,
    HandoffRouting,
    parse_handoff_frontmatter_text,
)
from llm_handoff.validator import validate_handoff_frontmatter, validate_handoff_text


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).parent / "ported" / "fixtures" / "handoffs"
STARTER_HANDOFF = (
    REPO_ROOT / "examples" / "reference-workflow" / "docs" / "handoff" / "HANDOFF.md"
)

IMPLEMENTERS = ("backend", "frontend")
ALLOWED_ROUTES = ("auditor", "planner", "user")
REJECTED_ROUTES = tuple(
    role for role in CANONICAL_NEXT_AGENT_ROLES if role not in ALLOWED_ROUTES
)
SCOPE_SHA = "0a040ac"
PRODUCER_RULE_PREFIXES = (
    "frontmatter_routing_rule:",
    "frontmatter_status_producer_rule:",
)


def _routing_error(producer: str, next_agent: str) -> str:
    return (
        f"frontmatter_routing_rule: producer `{producer}` cannot route to "
        f"next_agent `{next_agent}`; an implementer routes only to "
        "`auditor`, `planner`, `user`."
    )


def _status_error(producer: str, status: str) -> str:
    return (
        f"frontmatter_status_producer_rule: producer `{producer}` cannot emit "
        f"status `{status}`; only the auditor approves or fails work."
    )


def _epic_close_error(producer: str) -> str:
    return (
        f"frontmatter_status_producer_rule: producer `{producer}` cannot emit "
        "close_type `epic`; an implementer hands back with close_type "
        "`story` and the auditor approves the epic close."
    )


def _frontmatter_errors(**fields: str) -> list[str]:
    values = {"reason": "Implementer handback.", **fields}
    return validate_handoff_frontmatter(HandoffRouting(**values)).errors


def _implementer_rule_errors(errors: list[str], producer: str) -> list[str]:
    return [
        error
        for error in errors
        if error.startswith(PRODUCER_RULE_PREFIXES)
        and f"producer `{producer}`" in error
    ]


def _producer_rule_errors(errors: list[str]) -> list[str]:
    return [
        error
        for error in errors
        if error.startswith("frontmatter_status_producer_rule:")
        or (
            error.startswith("frontmatter_routing_rule:")
            and "an implementer" in error
        )
    ]


# Routes


@pytest.mark.parametrize("producer", IMPLEMENTERS)
@pytest.mark.parametrize("next_agent", ALLOWED_ROUTES)
def test_implementer_allowed_routes_pass(producer: str, next_agent: str) -> None:
    errors = _frontmatter_errors(next_agent=next_agent, producer=producer)

    assert errors == []


@pytest.mark.parametrize("producer", IMPLEMENTERS)
@pytest.mark.parametrize("next_agent", REJECTED_ROUTES)
def test_implementer_rejected_routes_fail(producer: str, next_agent: str) -> None:
    errors = _frontmatter_errors(next_agent=next_agent, producer=producer)

    assert _implementer_rule_errors(errors, producer) == [
        _routing_error(producer, next_agent)
    ]


def test_rejected_routes_cover_every_other_recognized_role() -> None:
    assert set(REJECTED_ROUTES) == {
        "backend",
        "frontend",
        "tpm",
        "validator",
        "finalizer",
    }


@pytest.mark.parametrize("producer", IMPLEMENTERS)
def test_implementer_unknown_route_keeps_invalid_route_diagnostic(
    producer: str,
) -> None:
    errors = _frontmatter_errors(next_agent="reviewer-bot", producer=producer)

    assert errors == [
        "frontmatter_next_agent_invalid: "
        f"producer {producer} used unsupported next_agent `reviewer-bot`."
    ]


def test_implementer_producer_label_is_normalized() -> None:
    errors = _frontmatter_errors(next_agent="frontend", producer=" Backend ")

    assert _implementer_rule_errors(errors, "backend") == [
        _routing_error("backend", "frontend")
    ]


# Statuses and epic closure


@pytest.mark.parametrize("producer", IMPLEMENTERS)
@pytest.mark.parametrize("status", ["verified_pass", "verified_fail"])
def test_implementer_verified_status_is_rejected(producer: str, status: str) -> None:
    # verified_pass and verified_fail both route to planner here, so the
    # status/route check passes and only the producer rule fires.
    errors = _frontmatter_errors(
        next_agent="planner", producer=producer, status=status
    )

    assert errors == [_status_error(producer, status)]


@pytest.mark.parametrize("producer", IMPLEMENTERS)
def test_implementer_epic_close_to_auditor_is_rejected(producer: str) -> None:
    errors = _frontmatter_errors(
        next_agent="auditor",
        producer=producer,
        status="ready_for_review",
        close_type="epic",
        scope_sha=SCOPE_SHA,
    )

    assert errors == [_epic_close_error(producer)]


@pytest.mark.parametrize("producer", IMPLEMENTERS)
@pytest.mark.parametrize(
    ("status", "next_agent", "close_type"),
    [
        ("ready_for_review", "auditor", "story"),
        ("ready_for_review", "auditor", None),
        ("blocked_missing_context", "user", None),
        ("blocked_implementation_failure", "planner", None),
        ("escalate_to_user", "user", None),
    ],
)
def test_implementer_legal_handbacks_pass(
    producer: str, status: str, next_agent: str, close_type: str | None
) -> None:
    fields = {
        "next_agent": next_agent,
        "producer": producer,
        "status": status,
        "scope_sha": SCOPE_SHA,
    }
    if close_type is not None:
        fields["close_type"] = close_type

    assert _frontmatter_errors(**fields) == []


# Other producers are untouched by TRANS-1.


@pytest.mark.parametrize(
    ("producer", "next_agent", "status"),
    [
        ("auditor", "backend", "verified_fail"),
        ("auditor", "planner", "verified_pass"),
        ("planner", "frontend", None),
        ("validator", "backend", "verified_fail"),
        ("user", "planner", None),
    ],
)
def test_non_implementer_producers_are_not_restricted(
    producer: str, next_agent: str, status: str | None
) -> None:
    fields = {"next_agent": next_agent, "producer": producer}
    if status is not None:
        fields["status"] = status

    assert _producer_rule_errors(_frontmatter_errors(**fields)) == []


def test_auditor_epic_close_to_finalizer_is_not_restricted() -> None:
    errors = _frontmatter_errors(
        next_agent="finalizer",
        producer="auditor",
        status="verified_pass",
        close_type="epic",
        scope_sha=SCOPE_SHA,
    )

    assert errors == []


# Post-dispatch validation


_EVIDENCE = f"""
## Verification Evidence

- **Commands run:** `python -m pytest tests -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {SCOPE_SHA}
- **Files changed or reviewed:** llm_handoff/validator.py
- **Unresolved concerns:** none
"""


def _handback(
    *,
    producer: str,
    next_agent: str,
    status: str,
    close_type: str | None = "story",
    body: str = _EVIDENCE,
) -> str:
    lines = [
        "---",
        f"next_agent: {next_agent}",
        'reason: "Implementer handback."',
        f"status: {status}",
        f"scope_sha: {SCOPE_SHA}",
    ]
    if close_type is not None:
        lines.append(f"close_type: {close_type}")
    lines.extend([f"producer: {producer}", "---", ""])
    return "\n".join(lines) + f"\n# {producer} Handback\n{body}"


@pytest.mark.parametrize("producer", IMPLEMENTERS)
def test_post_dispatch_ready_for_review_to_auditor_passes(producer: str) -> None:
    result = validate_handoff_text(
        _handback(producer=producer, next_agent="auditor", status="ready_for_review"),
        producer,
    )

    assert result.errors == []
    assert result.routing_instruction == "auditor"


@pytest.mark.parametrize("producer", IMPLEMENTERS)
@pytest.mark.parametrize(
    ("status", "next_agent"),
    [
        ("blocked_missing_context", "user"),
        ("blocked_implementation_failure", "planner"),
        ("escalate_to_user", "user"),
    ],
)
def test_post_dispatch_blocked_handbacks_pass(
    producer: str, status: str, next_agent: str
) -> None:
    body = f"\nBlocked at {SCOPE_SHA}; see the question below.\n"
    result = validate_handoff_text(
        _handback(
            producer=producer,
            next_agent=next_agent,
            status=status,
            close_type=None,
            body=body,
        ),
        producer,
    )

    assert result.errors == []


@pytest.mark.parametrize("producer", IMPLEMENTERS)
@pytest.mark.parametrize("next_agent", ["backend", "frontend", "tpm", "validator"])
def test_post_dispatch_rejects_implementer_route(
    producer: str, next_agent: str
) -> None:
    result = validate_handoff_text(
        _handback(
            producer=producer,
            next_agent=next_agent,
            status="escalate_to_user",
            close_type=None,
        ),
        producer,
    )

    assert _routing_error(producer, next_agent) in result.errors
    assert result.verdict == "NO"


@pytest.mark.parametrize("producer", IMPLEMENTERS)
@pytest.mark.parametrize("status", ["verified_pass", "verified_fail"])
def test_post_dispatch_rejects_implementer_verified_status(
    producer: str, status: str
) -> None:
    result = validate_handoff_text(
        _handback(producer=producer, next_agent="planner", status=status),
        producer,
    )

    assert _status_error(producer, status) in result.errors
    assert result.verdict == "NO"


@pytest.mark.parametrize("producer", IMPLEMENTERS)
def test_post_dispatch_rejects_implementer_epic_close(producer: str) -> None:
    result = validate_handoff_text(
        _handback(
            producer=producer,
            next_agent="auditor",
            status="ready_for_review",
            close_type="epic",
        ),
        producer,
    )

    assert _epic_close_error(producer) in result.errors
    assert result.verdict == "NO"


# Fixtures and the reference starter


_HANDOFF_FIXTURES = sorted(path.name for path in FIXTURES_DIR.glob("*.md"))


def test_handoff_fixture_directory_is_inspected() -> None:
    assert _HANDOFF_FIXTURES


@pytest.mark.parametrize("fixture_name", _HANDOFF_FIXTURES)
@pytest.mark.parametrize("previous_agent", IMPLEMENTERS)
def test_handoff_fixtures_keep_their_outcomes_under_implementer_rules(
    fixture_name: str, previous_agent: str
) -> None:
    text = (FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")
    try:
        parsed = parse_handoff_frontmatter_text(text.lstrip("﻿"))
    except HandoffFrontmatterError:
        parsed = None

    result = validate_handoff_text(text, previous_agent)

    if parsed is None:
        # Empty, malformed, and frontmatter-free routing inputs keep their
        # legacy outcome: the producer rules never see them.
        assert any(
            error.startswith(("frontmatter_missing:", "frontmatter_parse_error:"))
            for error in result.errors
        )
        assert _producer_rule_errors(result.errors) == []
        return

    # A fixture with frontmatter from an implementer must obey the rules;
    # no fixture name or legacy producer is exempt.
    assert _producer_rule_errors(validate_handoff_frontmatter(parsed).errors) == []


def test_reference_starter_handoff_stays_accepted() -> None:
    text = STARTER_HANDOFF.read_text(encoding="utf-8")
    parsed = parse_handoff_frontmatter_text(text.lstrip("﻿"))

    assert parsed is not None
    assert parsed.producer == "user"
    result = validate_handoff_frontmatter(parsed)
    assert result.errors == []
    assert result.routing_instruction == "planner"
