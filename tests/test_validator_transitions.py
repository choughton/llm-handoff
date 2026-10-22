"""TRANS-3: route and status legality for validator producers.

A `producer: validator` handoff may route anywhere except `finalizer`, never
sets `close_type: epic`, and never emits `ready_for_review`. It may carry
`verified_pass` or `verified_fail` only when the caller validates content the
dispatcher wrote itself and passes `dispatcher_authored=True`. The flag is
never inferred from the handoff, and it waives no other check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff.roles import CANONICAL_NEXT_AGENT_ROLES
from llm_handoff.router import HandoffRouting
from llm_handoff.validator import (
    validate_handoff,
    validate_handoff_frontmatter,
    validate_handoff_text,
)


SCOPE_SHA = "0a040ac"
STATUS_RULE = "frontmatter_status_producer_rule:"
ROUTING_RULE = "frontmatter_routing_rule:"
VERDICTS = ("verified_pass", "verified_fail")
# Each verdict paired with a route its status allows.
VERDICT_ROUTES = (("verified_fail", "backend"), ("verified_pass", "planner"))
VALIDATOR_ROUTES = tuple(
    role for role in CANONICAL_NEXT_AGENT_ROLES if role != "finalizer"
)

VALIDATOR_FINALIZER_ERROR = (
    "frontmatter_routing_rule: producer `validator` cannot route to next_agent "
    "`finalizer`; only the auditor approves an epic close."
)
VALIDATOR_EPIC_ERROR = (
    "frontmatter_routing_rule: producer `validator` emitted close_type `epic`; "
    "only the auditor approves an epic close."
)
VALIDATOR_READY_ERROR = (
    "frontmatter_status_producer_rule: producer `validator` cannot emit status "
    "`ready_for_review`; only an implementer claims work for review."
)


def _verdict_error(status: str) -> str:
    return (
        "frontmatter_status_producer_rule: producer `validator` cannot emit "
        f"status `{status}`; only the auditor approves or fails work, and only "
        "dispatcher-written handoffs carry a validator verdict."
    )


def _routing(**fields: str) -> HandoffRouting:
    return HandoffRouting(**{"reason": "Validator handoff.", **fields})


def _frontmatter_errors(**fields: object) -> list[str]:
    flag = fields.pop("dispatcher_authored", None)
    parsed = _routing(**fields)
    if flag is None:
        return validate_handoff_frontmatter(parsed).errors
    return validate_handoff_frontmatter(parsed, dispatcher_authored=flag).errors


def _rule_errors(errors: list[str]) -> list[str]:
    return [error for error in errors if error.startswith((STATUS_RULE, ROUTING_RULE))]


# Verdict permission


@pytest.mark.parametrize(("status", "next_agent"), VERDICT_ROUTES)
@pytest.mark.parametrize("flag", [None, False], ids=["default", "explicit-false"])
def test_validator_verdict_is_rejected_without_the_flag(
    status: str, next_agent: str, flag: bool | None
) -> None:
    errors = _frontmatter_errors(
        next_agent=next_agent,
        producer="validator",
        status=status,
        dispatcher_authored=flag,
    )

    assert errors == [_verdict_error(status)]


@pytest.mark.parametrize(("status", "next_agent"), VERDICT_ROUTES)
def test_validator_verdict_is_allowed_with_the_flag(
    status: str, next_agent: str
) -> None:
    errors = _frontmatter_errors(
        next_agent=next_agent,
        producer="validator",
        status=status,
        dispatcher_authored=True,
    )

    assert errors == []


def test_validator_producer_label_is_normalized() -> None:
    errors = _frontmatter_errors(
        next_agent="backend", producer=" Validator ", status="verified_fail"
    )

    assert errors == [_verdict_error("verified_fail")]


# Rules the flag never lifts


@pytest.mark.parametrize("flag", [False, True])
def test_validator_ready_for_review_is_always_rejected(flag: bool) -> None:
    errors = _frontmatter_errors(
        next_agent="auditor",
        producer="validator",
        status="ready_for_review",
        dispatcher_authored=flag,
    )

    assert VALIDATOR_READY_ERROR in errors
    assert _rule_errors(errors) == [VALIDATOR_READY_ERROR]


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("status", [None, *VERDICTS])
def test_validator_route_to_finalizer_is_always_rejected(
    flag: bool, status: str | None
) -> None:
    fields: dict[str, object] = {
        "next_agent": "finalizer",
        "producer": "validator",
        "close_type": "epic",
        "scope_sha": SCOPE_SHA,
        "dispatcher_authored": flag,
    }
    if status is not None:
        fields["status"] = status

    errors = _frontmatter_errors(**fields)

    assert VALIDATOR_FINALIZER_ERROR in errors
    assert VALIDATOR_EPIC_ERROR in errors
    # The generic finalizer rule still names the auditor.
    assert (
        "frontmatter_routing_rule: next_agent `finalizer` requires producer "
        "`auditor`." in errors
    )


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("status", [None, "verified_pass"])
def test_validator_epic_close_is_always_rejected(
    flag: bool, status: str | None
) -> None:
    fields: dict[str, object] = {
        "next_agent": "auditor",
        "producer": "validator",
        "close_type": "epic",
        "scope_sha": SCOPE_SHA,
        "dispatcher_authored": flag,
    }
    if status is not None:
        fields["status"] = status

    errors = _frontmatter_errors(**fields)

    assert VALIDATOR_EPIC_ERROR in errors


@pytest.mark.parametrize("flag", [False, True])
def test_validator_verdict_keeps_status_route_constraints(flag: bool) -> None:
    # verified_pass never routes to an implementer, flag or not.
    errors = _frontmatter_errors(
        next_agent="backend",
        producer="validator",
        status="verified_pass",
        dispatcher_authored=flag,
    )

    assert any(error.startswith("frontmatter_status_route_conflict:") for error in errors)


# Routes


@pytest.mark.parametrize("next_agent", VALIDATOR_ROUTES)
def test_validator_routes_anywhere_but_finalizer(next_agent: str) -> None:
    assert _frontmatter_errors(next_agent=next_agent, producer="validator") == []


@pytest.mark.parametrize(
    ("status", "next_agent"),
    [
        ("blocked_missing_context", "user"),
        ("escalate_to_user", "user"),
        ("blocked_implementation_failure", "planner"),
    ],
)
@pytest.mark.parametrize("flag", [False, True])
def test_validator_pause_statuses_need_no_flag(
    status: str, next_agent: str, flag: bool
) -> None:
    errors = _frontmatter_errors(
        next_agent=next_agent,
        producer="validator",
        status=status,
        dispatcher_authored=flag,
    )

    assert errors == []


# Other producers are unaffected by the flag


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize(
    ("producer", "next_agent", "status", "close_type"),
    [
        ("planner", "planner", "verified_pass", None),
        ("user", "planner", "verified_fail", None),
        ("reviewer-bot", "planner", "verified_pass", None),
        ("backend", "planner", "verified_pass", None),
        ("frontend", "backend", "verified_fail", None),
        ("tpm", "planner", "verified_pass", None),
        ("auditor", "planner", "ready_for_review", None),
        ("auditor", "auditor", None, None),
        ("finalizer", "planner", "verified_fail", None),
        ("finalizer", "planner", None, "story"),
        ("backend", "auditor", "ready_for_review", "epic"),
    ],
)
def test_flag_does_not_change_other_producer_rules(
    flag: bool,
    producer: str,
    next_agent: str,
    status: str | None,
    close_type: str | None,
) -> None:
    fields: dict[str, object] = {"next_agent": next_agent, "producer": producer}
    if status is not None:
        fields["status"] = status
    if close_type is not None:
        fields["close_type"] = close_type
        fields["scope_sha"] = SCOPE_SHA

    default = _frontmatter_errors(**fields)
    flagged = _frontmatter_errors(**fields, dispatcher_authored=flag)

    assert _rule_errors(default), default
    assert flagged == default


# Post-dispatch entry points


_EVIDENCE = f"""
## Verification Evidence

- **Commands run:** `python -m pytest tests -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {SCOPE_SHA}
- **Files changed or reviewed:** llm_handoff/validator.py
- **Unresolved concerns:** none
"""
_NO_EVIDENCE = f"\nDispatcher verification checked {SCOPE_SHA}.\n"


def _handoff(
    *,
    next_agent: str,
    status: str,
    producer: str = "validator",
    body: str = _EVIDENCE,
    extra: tuple[str, ...] = (),
) -> str:
    lines = [
        "---",
        f"next_agent: {next_agent}",
        'reason: "Validator handoff."',
        f"status: {status}",
        f"scope_sha: {SCOPE_SHA}",
        *extra,
        f"producer: {producer}",
        "---",
        "",
    ]
    return "\n".join(lines) + f"\n# Dispatcher Verification\n\n**Agent:** {producer}\n{body}"


def _text_errors(content: str, **kwargs: object) -> list[str]:
    return validate_handoff_text(content, "validator", **kwargs).errors


@pytest.mark.parametrize(("status", "next_agent"), VERDICT_ROUTES)
def test_text_entry_point_rejects_verdict_despite_complete_evidence(
    status: str, next_agent: str
) -> None:
    content = _handoff(next_agent=next_agent, status=status)

    assert _verdict_error(status) in _text_errors(content)
    assert _verdict_error(status) in _text_errors(content, dispatcher_authored=False)
    assert _text_errors(content, dispatcher_authored=True) == []


@pytest.mark.parametrize(("status", "next_agent"), VERDICT_ROUTES)
def test_file_entry_point_propagates_the_flag(
    tmp_path: Path, status: str, next_agent: str
) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(
        _handoff(next_agent=next_agent, status=status), encoding="utf-8"
    )

    default = validate_handoff(handoff_path, "validator")
    explicit_false = validate_handoff(
        handoff_path, "validator", dispatcher_authored=False
    )
    trusted = validate_handoff(handoff_path, "validator", dispatcher_authored=True)

    assert _verdict_error(status) in default.errors
    assert _verdict_error(status) in explicit_false.errors
    assert trusted.errors == []
    assert trusted.routing_instruction == next_agent


@pytest.mark.parametrize(("status", "next_agent"), VERDICT_ROUTES)
def test_flag_does_not_waive_evidence(status: str, next_agent: str) -> None:
    content = _handoff(next_agent=next_agent, status=status, body=_NO_EVIDENCE)

    errors = _text_errors(content, dispatcher_authored=True)

    assert any(error.startswith("EVIDENCE_MISSING:") for error in errors)
    assert not any(error.startswith(STATUS_RULE) for error in errors)


def test_flag_does_not_waive_lease_checks() -> None:
    content = _handoff(
        next_agent="backend", status="verified_fail", extra=("story_id: LIN-9",)
    )

    errors = _text_errors(content, dispatcher_authored=True, tracker_provider="linear")

    assert any(error.startswith("tracker_issue_missing") for error in errors)
    assert any(error.startswith("tracker_url_missing") for error in errors)


def test_flag_does_not_waive_self_loop_check() -> None:
    content = _handoff(next_agent="validator", status="escalate_to_user")

    errors = _text_errors(content, dispatcher_authored=True)

    assert any(error.startswith("agent_self_loop:") for error in errors)


@pytest.mark.parametrize("flag", [False, True])
def test_text_entry_point_rejects_ready_for_review_with_evidence(flag: bool) -> None:
    content = _handoff(next_agent="auditor", status="ready_for_review")

    errors = _text_errors(content, dispatcher_authored=flag)

    assert VALIDATOR_READY_ERROR in errors


@pytest.mark.parametrize("flag", [False, True])
def test_text_entry_point_rejects_finalizer_epic_close_with_evidence(
    flag: bool,
) -> None:
    content = _handoff(
        next_agent="finalizer", status="verified_pass", extra=("close_type: epic",)
    )

    errors = _text_errors(content, dispatcher_authored=flag)

    assert VALIDATOR_FINALIZER_ERROR in errors
    assert VALIDATOR_EPIC_ERROR in errors


@pytest.mark.parametrize("previous_agent", ["auditor", "backend", "planner"])
def test_previous_role_grants_no_verdict_permission(previous_agent: str) -> None:
    # The flag comes from the caller only, never from the dispatched role.
    content = _handoff(next_agent="planner", status="verified_pass")

    errors = validate_handoff_text(content, previous_agent).errors

    assert _verdict_error("verified_pass") in errors


# TRANS-4: chat takeover producer identity
#
# `_follow_chat_takeover` validates an operator-directed chat rewrite as a
# planner dispatch with `chat_takeover=True`. The rewrite may name
# `producer: user` or `producer: planner`; any other producer still fails
# `frontmatter_producer_mismatch`, and every other planner check still runs.

MISMATCH = "frontmatter_producer_mismatch:"


def _planner_shaped(producer: str, *, work_packet: bool = True) -> str:
    packet = (
        """
## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** `src/app.py`
- **Files out of bounds:** `src/api.py`
- **Context:** The operator redirected the story from chat.
- **Verification command:**
  - python -m pytest tests -q
- **Expected next route:** auditor
"""
        if work_packet
        else "\nNo packet.\n"
    )
    return (
        "---\n"
        "next_agent: backend\n"
        'reason: "The operator redirected the story from chat."\n'
        "epic_id: EPIC-1\n"
        "story_id: EPIC-1-S1\n"
        "story_title: Change the value\n"
        f"producer: {producer}\n"
        "---\n\n"
        "# Operator Redirect\n" + packet
    )


def _mismatches(errors: list[str]) -> list[str]:
    return [error for error in errors if error.startswith(MISMATCH)]


@pytest.mark.parametrize("producer", ["user", "planner", " User "])
def test_chat_takeover_accepts_user_or_planner_producer(producer: str) -> None:
    result = validate_handoff_text(
        _planner_shaped(producer), "planner", chat_takeover=True
    )

    assert result.errors == []
    assert result.routing_instruction == "backend"


@pytest.mark.parametrize(
    "producer", ["auditor", "backend", "frontend", "tpm", "finalizer", "validator"]
)
def test_chat_takeover_rejects_any_other_producer(producer: str) -> None:
    result = validate_handoff_text(
        _planner_shaped(producer), "planner", chat_takeover=True
    )

    assert _mismatches(result.errors) == [
        f"frontmatter_producer_mismatch: a chat takeover rewrite names producer "
        f"`{producer}`; a chat rewrite sets `producer: user` or `producer: planner`."
    ]
    assert result.verdict == "NO"


@pytest.mark.parametrize("flag", [None, False], ids=["default", "explicit-false"])
def test_user_producer_after_a_planner_dispatch_fails_without_chat_takeover(
    flag: bool | None,
) -> None:
    content = _planner_shaped("user")
    if flag is None:
        result = validate_handoff_text(content, "planner")
    else:
        result = validate_handoff_text(content, "planner", chat_takeover=flag)

    assert _mismatches(result.errors) == [
        "frontmatter_producer_mismatch: the dispatched role is `planner`, but the "
        "handoff names producer `user`; a planner handoff sets `producer: planner`."
    ]


def test_chat_takeover_waives_no_planner_check() -> None:
    result = validate_handoff_text(
        _planner_shaped("user", work_packet=False),
        "planner",
        chat_takeover=True,
        require_work_packet=True,
    )

    assert _mismatches(result.errors) == []
    assert result.verdict == "NO"
    assert any("Work Packet" in error for error in result.errors), result.errors


def _linear_takeover(producer: str, *, selection_basis: str | None) -> str:
    basis = f"selection_basis: {selection_basis}\n" if selection_basis else ""
    return _planner_shaped(producer).replace(
        "story_id: EPIC-1-S1\n",
        "story_id: ENG-42\n"
        "tracker_issue: ENG-42\n"
        "tracker_url: https://linear.app/example/issue/ENG-42/change-the-value\n"
        + basis,
    )


SELECTION_MISSING = "selection_basis_missing:"


@pytest.mark.parametrize("producer", ["user", "planner"])
def test_linear_chat_takeover_requires_selection_basis(producer: str) -> None:
    result = validate_handoff_text(
        _linear_takeover(producer, selection_basis=None),
        "planner",
        chat_takeover=True,
        tracker_provider="linear",
    )

    missing = [e for e in result.errors if e.startswith(SELECTION_MISSING)]
    assert len(missing) == 1, result.errors
    assert result.verdict == "NO"


@pytest.mark.parametrize("producer", ["user", "planner"])
def test_linear_chat_takeover_with_selection_basis_passes(producer: str) -> None:
    result = validate_handoff_text(
        _linear_takeover(producer, selection_basis="priority"),
        "planner",
        chat_takeover=True,
        tracker_provider="linear",
    )

    assert result.errors == []


def test_user_selection_basis_rule_applies_only_on_the_takeover_path() -> None:
    content = _linear_takeover("user", selection_basis=None)

    vanilla = validate_handoff_text(
        content, "planner", chat_takeover=True, tracker_provider="none"
    )
    not_takeover = validate_handoff_text(
        content, "planner", tracker_provider="linear"
    )

    assert not any(e.startswith(SELECTION_MISSING) for e in vanilla.errors)
    assert not any(e.startswith(SELECTION_MISSING) for e in not_takeover.errors)


def test_file_entry_point_propagates_chat_takeover(tmp_path: Path) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(_planner_shaped("user"), encoding="utf-8")

    followed = validate_handoff(handoff_path, "planner", chat_takeover=True)
    default = validate_handoff(handoff_path, "planner")

    assert followed.errors == []
    assert len(_mismatches(default.errors)) == 1
