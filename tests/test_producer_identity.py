"""TRANS-4: the handoff producer must match the dispatched role.

The dispatcher knows which role it ran. When `validate_handoff_text()` checks
agent output (`dispatcher_authored=False`), a `producer` that does not
normalize to the dispatched role fails with `frontmatter_producer_mismatch`,
so no role can borrow another role's rules or authority by claiming its name.
Handoffs the dispatcher wrote itself (`dispatcher_authored=True`) skip only
this identity check. An unknown previous role and a missing producer keep
their existing diagnostics and invent no identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff.roles import CANONICAL_NEXT_AGENT_ROLES
from llm_handoff.validator import validate_handoff, validate_handoff_text


SCOPE_SHA = "0a040ac"
MISMATCH = "frontmatter_producer_mismatch:"
ROLES = tuple(CANONICAL_NEXT_AGENT_ROLES)
ROLE_PAIRS = tuple(
    (dispatched, claimed)
    for dispatched in ROLES
    for claimed in ROLES
    if claimed != dispatched
)


def _mismatch_error(dispatched: str, claimed: str) -> str:
    return (
        f"frontmatter_producer_mismatch: the dispatched role is `{dispatched}`, "
        f"but the handoff names producer `{claimed}`; a {dispatched} handoff "
        f"sets `producer: {dispatched}`."
    )


def _mismatches(errors: list[str]) -> list[str]:
    return [error for error in errors if error.startswith(MISMATCH)]


def _plain_handoff(*, producer: str | None, next_agent: str) -> str:
    lines = ["---", f"next_agent: {next_agent}", 'reason: "Identity check."']
    if producer is not None:
        lines.append(f'producer: "{producer}"')
    lines.append("---")
    return "\n".join(lines) + "\n\n# Handoff\n\nNo work claimed.\n"


def _other_route(role: str) -> str:
    return "planner" if role == "user" else "user"


_EVIDENCE = f"""
## Verification Evidence

- **Commands run:** `python -m pytest tests -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {SCOPE_SHA}
- **Files changed or reviewed:** llm_handoff/validator.py
- **Unresolved concerns:** none
"""
_NO_EVIDENCE = f"\nDispatcher verification checked {SCOPE_SHA}.\n"


def _verdict_handoff(
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
        'reason: "Identity check."',
        f"status: {status}",
        f"scope_sha: {SCOPE_SHA}",
    ]
    if close_type is not None:
        lines.append(f"close_type: {close_type}")
    lines.extend([f"producer: {producer}", "---", ""])
    return "\n".join(lines) + f"\n# {producer} Report\n\n**Agent:** {producer}\n{body}"


# Every known role


@pytest.mark.parametrize("role", ROLES)
def test_matching_producer_introduces_no_mismatch(role: str) -> None:
    content = _plain_handoff(producer=role, next_agent=_other_route(role))

    result = validate_handoff_text(content, role)

    assert _mismatches(result.errors) == []


@pytest.mark.parametrize(("dispatched", "claimed"), ROLE_PAIRS)
def test_different_producer_fails_without_prior_handoff(
    dispatched: str, claimed: str
) -> None:
    content = _plain_handoff(producer=claimed, next_agent=_other_route(dispatched))

    result = validate_handoff_text(content, dispatched)

    assert _mismatches(result.errors) == [_mismatch_error(dispatched, claimed)]
    assert result.verdict == "NO"


# Claimed authority cannot bypass the dispatched role


_WORK_PACKET = """
## Work Packet

- **Objective:** Close the epic.
- **Files in scope:** `PROJECT_STATE.md`
- **Files out of bounds:** `llm_handoff/`
- **Context:** The final story is approved.
- **Verification command:**
  - python -m pytest tests -q
- **Expected next route:** finalizer
"""


def _auditor_epic_close(*, body: str = _EVIDENCE) -> str:
    return _verdict_handoff(
        producer="auditor",
        next_agent="finalizer",
        status="verified_pass",
        close_type="epic",
        body=body,
    )


@pytest.mark.parametrize("body", [_EVIDENCE, _EVIDENCE + _WORK_PACKET])
def test_auditor_epic_close_fixture_is_valid_for_an_auditor_dispatch(
    body: str,
) -> None:
    result = validate_handoff_text(_auditor_epic_close(body=body), "auditor")

    assert result.errors == []
    assert result.routing_instruction == "finalizer"


def test_planner_claiming_auditor_cannot_route_to_finalizer() -> None:
    # The planner handoff carries a Work Packet, so the producer mismatch is
    # the only thing that rejects it.
    content = _auditor_epic_close(body=_EVIDENCE + _WORK_PACKET)

    result = validate_handoff_text(content, "planner")

    assert result.errors == [_mismatch_error("planner", "auditor")]
    assert result.verdict == "NO"


def test_backend_claiming_auditor_cannot_route_to_finalizer() -> None:
    result = validate_handoff_text(_auditor_epic_close(), "backend")

    assert _mismatch_error("backend", "auditor") in result.errors
    assert result.verdict == "NO"


def test_backend_claiming_auditor_cannot_approve_a_story() -> None:
    content = _verdict_handoff(
        producer="auditor",
        next_agent="planner",
        status="verified_pass",
        close_type="story",
    )

    assert validate_handoff_text(content, "auditor").errors == []
    result = validate_handoff_text(content, "backend")

    assert _mismatch_error("backend", "auditor") in result.errors
    assert result.verdict == "NO"


# Normalization, absent producers, unknown previous roles


@pytest.mark.parametrize(
    ("previous_agent", "producer"),
    [
        ("backend", " Backend "),
        ("auditor", "AUDITOR"),
        ("tpm", "`tpm`"),
        ("Planner", "planner"),
        ("validator", "Validator"),
        ("reviewer", "auditor"),
        ("ledger-updater", "finalizer"),
    ],
)
def test_normalized_labels_match(previous_agent: str, producer: str) -> None:
    content = _plain_handoff(producer=producer, next_agent="user")

    result = validate_handoff_text(content, previous_agent)

    assert _mismatches(result.errors) == []


def test_producer_alias_is_not_the_canonical_role() -> None:
    # Producers normalize like other frontmatter role values: an alias the
    # canonical normalization does not accept names no role.
    content = _plain_handoff(producer="reviewer", next_agent="planner")

    result = validate_handoff_text(content, "auditor")

    assert _mismatches(result.errors) == [_mismatch_error("auditor", "reviewer")]


@pytest.mark.parametrize("role", ROLES)
def test_missing_producer_reports_only_the_missing_producer_error(role: str) -> None:
    content = _plain_handoff(producer=None, next_agent=_other_route(role))

    result = validate_handoff_text(content, role)

    assert _mismatches(result.errors) == []
    assert any(
        error.startswith("frontmatter_producer_missing:") for error in result.errors
    )


@pytest.mark.parametrize("producer", ["planner", "auditor", "mystery-bot"])
def test_unknown_previous_agent_sets_no_identity(producer: str) -> None:
    content = _plain_handoff(producer=producer, next_agent="user")

    result = validate_handoff_text(content, "mystery-bot")

    assert _mismatches(result.errors) == []
    assert any(
        warning.startswith("unknown_previous_agent:") for warning in result.warnings
    )


def test_file_based_wrapper_rejects_a_mismatch(tmp_path: Path) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(_auditor_epic_close(), encoding="utf-8")

    rejected = validate_handoff(handoff_path, "planner")
    accepted = validate_handoff(handoff_path, "auditor")

    assert _mismatch_error("planner", "auditor") in rejected.errors
    assert rejected.verdict == "NO"
    assert _mismatches(accepted.errors) == []


# Dispatcher-authored handoffs


@pytest.mark.parametrize("previous_agent", ["auditor", "backend", "planner"])
def test_dispatcher_authored_validator_handoff_skips_identity(
    previous_agent: str,
) -> None:
    content = _verdict_handoff(
        producer="validator", next_agent="backend", status="verified_fail"
    )

    trusted = validate_handoff_text(
        content, previous_agent, dispatcher_authored=True
    )
    untrusted = validate_handoff_text(content, previous_agent)

    assert _mismatches(trusted.errors) == []
    assert _mismatch_error(previous_agent, "validator") in untrusted.errors


def test_dispatcher_authored_validator_ready_for_review_is_still_rejected() -> None:
    content = _verdict_handoff(
        producer="validator", next_agent="auditor", status="ready_for_review"
    )

    errors = validate_handoff_text(content, "backend", dispatcher_authored=True).errors

    assert _mismatches(errors) == []
    assert (
        "frontmatter_status_producer_rule: producer `validator` cannot emit status "
        "`ready_for_review`; only an implementer claims work for review."
    ) in errors


def test_dispatcher_authored_validator_finalizer_epic_close_is_still_rejected() -> None:
    content = _verdict_handoff(
        producer="validator",
        next_agent="finalizer",
        status="verified_pass",
        close_type="epic",
    )

    errors = validate_handoff_text(content, "auditor", dispatcher_authored=True).errors

    assert _mismatches(errors) == []
    assert (
        "frontmatter_routing_rule: producer `validator` cannot route to next_agent "
        "`finalizer`; only the auditor approves an epic close."
    ) in errors
    assert (
        "frontmatter_routing_rule: producer `validator` emitted close_type `epic`; "
        "only the auditor approves an epic close."
    ) in errors


def test_dispatcher_authored_validator_verdict_still_needs_evidence() -> None:
    content = _verdict_handoff(
        producer="validator",
        next_agent="planner",
        status="verified_pass",
        body=_NO_EVIDENCE,
    )

    errors = validate_handoff_text(content, "auditor", dispatcher_authored=True).errors

    assert _mismatches(errors) == []
    assert any(error.startswith("EVIDENCE_MISSING:") for error in errors)
