from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Literal

from llm_handoff.config import DISPATCHER_HANDOFF_STATE_PATH
from llm_handoff.agent_types import (
    BLOCKED_STATUSES,
    COMPLETION_STATUSES,
    HandoffStatus,
)
from llm_handoff.dispatcher_commit import PENDING_COMMIT_SHA
from llm_handoff.rationalization_detector import detect as detect_rationalizations
from llm_handoff.story_list import project_state_story_warnings, story_list_errors
from llm_handoff.router import (
    HandoffFrontmatterError,
    HandoffRouting,
    parse_handoff_frontmatter_text,
    route as route_handoff,
    status_next_agent_conflict,
)
from llm_handoff.roles import (
    normalize_agent_label,
    normalize_next_agent_value,
)
from llm_handoff.text_io import read_dispatch_text
from llm_handoff.work_claim import (
    EVIDENCE_LABEL_NOTE,
    evidence_sha_token,
    work_packet_errors,
)


ValidationVerdict = Literal["YES", "NO", "WARNINGS-ONLY"]

_VALID_RE = re.compile(r"(?im)^VALID:\s*(YES|NO|WARNINGS-ONLY)\s*$")
_PENDING_COMMIT_RE = re.compile(
    rf"`?{re.escape(PENDING_COMMIT_SHA)}`?(?:\s|$)"
)
_CHECK_LINE_RE = re.compile(
    r"(?im)^\s{2}([A-Z-]+):\s*(PASS|WARN|FAIL)\s*(?:-|—)\s*(.+)$"
)
_TASK_ASSIGNMENT_RE = re.compile(r"(?im)^#{1,6}\s+Task Assignment\b")
_OBJECTIVE_RE = re.compile(r"(?im)^#{1,6}\s+Objective\b")
_ACCEPTANCE_CRITERIA_RE = re.compile(r"(?im)^#{1,6}\s+Acceptance Criteria\b")
_WORK_PACKET_RE = re.compile(r"(?im)^#{1,6}\s+Work Packet\b")
_WORK_PACKET_HEADING_RE = re.compile(r"(?im)^#{1,6}\s+Work Packet\b[^\n]*$")
_WORK_PACKET_FIELD_RE = re.compile(
    r"(?im)^-[ \t]+\*\*(Objective|Files in scope|Files out of bounds|Context|Verification command|Expected next route):\*\*[ \t]*(.*)$"
)
_VERIFICATION_EVIDENCE_RE = re.compile(r"(?im)^##\s+Verification Evidence\s*$")
_EVIDENCE_FIELD_RE = re.compile(
    r"(?im)^-[ \t]+\*\*(Commands run|Output summary|Commit SHA verified|Files changed or reviewed|Unresolved concerns)"
    rf"{EVIDENCE_LABEL_NOTE}:\*\*[ \t]*(.*)$"
)
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")
_REPORTING_HEADER_RE = re.compile(
    r"(?im)^#{1,6}\s+.*\b(handback|handoff|audit|verdict|status|close)\b"
)
_COMMIT_SHA_RE = re.compile(r"(?i)\b[0-9a-f]{7,40}\b")
_SHA_VALUE_RE = re.compile(r"(?i)^[0-9a-f]{7,40}$")
_AGENT_LINE_RE = re.compile(r"(?im)^\*\*Agent:\*\*\s*(.+?)\s*$")
_SUBSTANCE_RE = re.compile(
    r"(?im)\b(completed work|verification|validation|results|test suites?|execution sha|implementation sha|(?:4-check\s+)?audit gate|audit verdict|checks:)\b"
)
_ALLOWED_CLOSE_TYPES = {"story", "epic"}
_IMPLEMENTER_ROUTES = {"backend", "frontend"}
# Roles that assign Work Packets. A tpm assignment to an implementer is
# checked exactly as a planner assignment is.
_PLANNING_ROUTES = {"planner", "tpm"}
# A tpm picks and assigns stories; it never claims completion, closes an
# epic, or hands to the finalizer or validator.
_TPM_ALLOWED_NEXT_AGENTS = ("backend", "frontend", "auditor", "planner", "user")
_TPM_REJECTED_STATUSES = (
    HandoffStatus.READY_FOR_REVIEW.value,
    HandoffStatus.VERIFIED_PASS.value,
    HandoffStatus.VERIFIED_FAIL.value,
)
# An implementer hands back for review, asks the planner to re-scope, or
# pauses for a person; it never approves, fails, or closes an epic.
_IMPLEMENTER_ALLOWED_NEXT_AGENTS = ("auditor", "planner", "user")
_IMPLEMENTER_REJECTED_STATUSES = (
    HandoffStatus.VERIFIED_PASS.value,
    HandoffStatus.VERIFIED_FAIL.value,
)
# A finalizer records the closed epic and hands to whoever picks the next
# one. It carries the auditor's epic-close verified_pass forward, or pauses;
# it never claims, fails, or closes work itself.
_FINALIZER_ALLOWED_NEXT_AGENTS = ("planner", "tpm", "user")
_FINALIZER_REJECTED_STATUSES = (
    HandoffStatus.READY_FOR_REVIEW.value,
    HandoffStatus.VERIFIED_FAIL.value,
    HandoffStatus.BLOCKED_IMPLEMENTATION_FAILURE.value,
)
# The auditor approves or fails work; it never claims work for review or
# routes back to itself.
_AUDITOR_REJECTED_STATUSES = (HandoffStatus.READY_FOR_REVIEW.value,)
_VERIFIED_STATUSES = (
    HandoffStatus.VERIFIED_PASS.value,
    HandoffStatus.VERIFIED_FAIL.value,
)
# `producer: validator` names the validator role and the dispatcher's own
# handoffs (review bounces, deferred findings, work-claim bounces, and
# protected-file escalations). It never claims work for review, closes an
# epic, or hands to the finalizer. Only a caller that knows it is checking
# dispatcher-written content (`dispatcher_authored=True`) may let it carry a
# verdict status.
_VALIDATOR_ALWAYS_REJECTED_STATUSES = (HandoffStatus.READY_FOR_REVIEW.value,)
_REQUIRED_EVIDENCE_FIELDS = {
    "Commands run",
    "Output summary",
    "Commit SHA verified",
    "Files changed or reviewed",
    "Unresolved concerns",
}
# The planner prompt contract: a ``## Work Packet`` section with exactly
# these six bold bullet fields. ``## Task Assignment`` with ``Objective`` and
# ``Acceptance Criteria`` headings is the pre-extraction shape and is only
# kept as a deprecated alias.
_REQUIRED_WORK_PACKET_FIELDS = (
    "Objective",
    "Files in scope",
    "Files out of bounds",
    "Context",
    "Verification command",
    "Expected next route",
)
_SELECTION_BASIS_VALUES = ("priority", "manual_order", "created_at")
_TRACKER_LEASE_FIELDS = ("tracker_issue", "tracker_url", "selection_basis")
# Routes that hold a Linear issue lease: the implementer working the issue and
# the auditor reviewing it. Planner, user, validator, and finalizer routes are
# exempt because they do not work a single tracker issue.
_TRACKER_LEASE_ROUTES = {"backend", "frontend", "auditor"}
_TASK_ASSIGNMENT_DEPRECATION_WARNING = (
    "task_assignment_deprecated: ## Task Assignment is a legacy alias; use a "
    "## Work Packet section with the six bold fields instead."
)


@dataclass(frozen=True)
class ValidationResult:
    verdict: ValidationVerdict
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    routing_instruction: str | None = None


def parse_validation_output(output: str) -> ValidationResult:
    normalized_output = output.lstrip("\ufeff")
    verdict_match = _VALID_RE.search(normalized_output)
    if verdict_match is None:
        raise ValueError("Missing VALID verdict line.")

    warnings: list[str] = []
    errors: list[str] = []
    routing_instruction: str | None = None

    for check_name, status, detail in _CHECK_LINE_RE.findall(normalized_output):
        message = f"{check_name.lower()}: {detail.strip()}"
        if status == "WARN":
            warnings.append(message)
        elif status == "FAIL":
            errors.append(message)

        if check_name == "ROUTING" and routing_instruction is None:
            routing_instruction = _infer_route_from_text(detail)

    return ValidationResult(
        verdict=verdict_match.group(1),
        warnings=warnings,
        errors=errors,
        routing_instruction=routing_instruction,
    )


def validate_handoff_frontmatter(
    parsed: HandoffRouting,
    *,
    git_cwd: Path | None = None,
    tracker_provider: str = "none",
    dispatcher_authored: bool = False,
) -> ValidationResult:
    """Validate handoff frontmatter on its own.

    Set `dispatcher_authored` only when the caller itself built the handoff
    (a dispatcher bounce or escalation). It lets `producer: validator` carry
    `verified_pass` or `verified_fail`; it is never inferred from the
    handoff's own fields, and it waives no other check.
    """

    producer = parsed.producer or "unknown producer"
    errors: list[str] = []
    warnings: list[str] = []

    normalized_next_agent = normalize_next_agent_value(parsed.next_agent)
    normalized_producer = normalize_next_agent_value(parsed.producer)

    if not parsed.next_agent:
        errors.append(
            f"frontmatter_next_agent_missing: producer {producer} omitted required next_agent."
        )
    elif normalized_next_agent is None:
        errors.append(
            "frontmatter_next_agent_invalid: "
            f"producer {producer} used unsupported next_agent `{parsed.next_agent}`."
        )

    if not parsed.reason:
        errors.append(
            f"frontmatter_reason_missing: producer {producer} omitted required reason."
        )
    elif len(parsed.reason) >= 200:
        warnings.append(
            f"frontmatter_reason_too_long: producer {producer} emitted a reason >= 200 characters."
        )

    if not parsed.producer:
        errors.append(
            "frontmatter_producer_missing: "
            "HANDOFF frontmatter omitted required producer."
        )

    if parsed.close_type is not None and parsed.close_type not in _ALLOWED_CLOSE_TYPES:
        errors.append(
            "frontmatter_close_type_invalid: "
            f"producer {producer} used unsupported close_type `{parsed.close_type}`."
        )

    if parsed.status is not None:
        try:
            HandoffStatus(parsed.status)
        except ValueError:
            errors.append(
                "STATUS_INVALID: "
                f"producer {producer} used unsupported status `{parsed.status}`."
            )

    if parsed.bounce_count is not None and parsed.bounce_count < 0:
        errors.append(
            "frontmatter_bounce_count_invalid: "
            f"producer {producer} emitted negative bounce_count `{parsed.bounce_count}`."
        )

    scope_sha_syntax_valid = bool(
        parsed.scope_sha and _SHA_VALUE_RE.fullmatch(parsed.scope_sha)
    )

    if parsed.close_type is not None:
        if not parsed.scope_sha:
            errors.append(
                "frontmatter_scope_sha_missing: "
                f"producer {producer} omitted scope_sha while close_type is set."
            )
        elif not scope_sha_syntax_valid:
            errors.append(
                "frontmatter_scope_sha_invalid: "
                f"producer {producer} emitted scope_sha `{parsed.scope_sha}`; expected 7-40 hex chars."
            )

    if parsed.scope_sha and not scope_sha_syntax_valid:
        errors.append(
            "frontmatter_scope_sha_invalid: "
            f"producer {producer} emitted scope_sha `{parsed.scope_sha}`; expected 7-40 hex chars."
        )
    elif parsed.scope_sha and _git_commit_exists(parsed.scope_sha, git_cwd) is False:
        errors.append(
            "frontmatter_scope_sha_unknown: "
            f"producer {producer} emitted scope_sha `{parsed.scope_sha}`, but it does not resolve to a commit in this repository."
        )

    if parsed.prior_sha and _SHA_VALUE_RE.fullmatch(parsed.prior_sha) is None:
        errors.append(
            "frontmatter_prior_sha_invalid: "
            f"producer {producer} emitted prior_sha `{parsed.prior_sha}`; expected 7-40 hex chars."
        )

    epic_close_agents = {"auditor", "finalizer"}
    if parsed.close_type == "epic" and normalized_next_agent not in epic_close_agents:
        errors.append(
            "frontmatter_routing_rule: close_type `epic` requires next_agent "
            "`auditor` or `finalizer`."
        )

    if normalized_next_agent == "finalizer" and parsed.close_type != "epic":
        errors.append(
            f"frontmatter_routing_rule: next_agent `{parsed.next_agent}` requires close_type `epic`."
        )

    if normalized_next_agent == "finalizer" and normalized_producer != "auditor":
        errors.append(
            f"frontmatter_routing_rule: next_agent `{parsed.next_agent}` requires producer `auditor`."
        )

    if normalized_producer == "tpm":
        errors.extend(_tpm_producer_errors(parsed, normalized_next_agent))
    elif normalized_producer in _IMPLEMENTER_ROUTES:
        errors.extend(
            _implementer_producer_errors(
                parsed, normalized_producer, normalized_next_agent
            )
        )
    elif normalized_producer == "finalizer":
        errors.extend(_finalizer_producer_errors(parsed, normalized_next_agent))
    elif normalized_producer == "auditor":
        errors.extend(_auditor_producer_errors(parsed, normalized_next_agent))
    elif normalized_producer == "validator":
        errors.extend(
            _validator_producer_errors(
                parsed,
                normalized_next_agent,
                dispatcher_authored=dispatcher_authored,
            )
        )
    elif parsed.producer:
        # A missing producer is already reported as
        # frontmatter_producer_missing.
        errors.extend(_verified_status_producer_errors(parsed))

    status_conflict = status_next_agent_conflict(parsed)
    if status_conflict is not None:
        errors.append(f"frontmatter_status_route_conflict: {status_conflict}")

    tracker_errors, tracker_warnings = _tracker_lease_findings(
        parsed,
        tracker_provider=tracker_provider,
        normalized_next_agent=normalized_next_agent,
        normalized_producer=normalized_producer,
    )
    errors.extend(tracker_errors)
    warnings.extend(tracker_warnings)

    routing_instruction = normalized_next_agent
    return ValidationResult(
        verdict=_derive_verdict(errors, warnings),
        warnings=_dedupe(warnings),
        errors=_dedupe(errors),
        routing_instruction=routing_instruction,
    )


def _tpm_producer_errors(
    parsed: HandoffRouting,
    normalized_next_agent: str | None,
) -> list[str]:
    errors: list[str] = []
    if (
        normalized_next_agent is not None
        and normalized_next_agent not in _TPM_ALLOWED_NEXT_AGENTS
    ):
        errors.append(
            "frontmatter_routing_rule: producer `tpm` cannot route to next_agent "
            f"`{normalized_next_agent}`; a tpm routes only to "
            f"{', '.join(f'`{agent}`' for agent in _TPM_ALLOWED_NEXT_AGENTS)}."
        )
    if parsed.status in _TPM_REJECTED_STATUSES:
        errors.append(
            "frontmatter_status_producer_rule: producer `tpm` cannot emit status "
            f"`{parsed.status}`; a tpm omits status or uses "
            "`blocked_missing_context`, `blocked_implementation_failure`, or "
            "`escalate_to_user`."
        )
    if parsed.close_type == "epic":
        errors.append(
            "frontmatter_routing_rule: producer `tpm` emitted close_type `epic`; "
            "a tpm never closes an epic and routes to `planner` when "
            "remaining_stories is empty."
        )
    return errors


def _implementer_producer_errors(
    parsed: HandoffRouting,
    producer: str,
    normalized_next_agent: str | None,
) -> list[str]:
    """Errors for a `producer: backend` or `producer: frontend` handoff.

    An unknown `next_agent` is already reported as
    `frontmatter_next_agent_invalid`, so only recognized routes are checked.
    """

    errors: list[str] = []
    if (
        normalized_next_agent is not None
        and normalized_next_agent not in _IMPLEMENTER_ALLOWED_NEXT_AGENTS
    ):
        errors.append(
            f"frontmatter_routing_rule: producer `{producer}` cannot route to "
            f"next_agent `{normalized_next_agent}`; an implementer routes only to "
            f"{', '.join(f'`{agent}`' for agent in _IMPLEMENTER_ALLOWED_NEXT_AGENTS)}."
        )
    if parsed.status in _IMPLEMENTER_REJECTED_STATUSES:
        errors.append(
            f"frontmatter_status_producer_rule: producer `{producer}` cannot emit "
            f"status `{parsed.status}`; only the auditor approves or fails work."
        )
    if parsed.close_type == "epic":
        errors.append(
            f"frontmatter_status_producer_rule: producer `{producer}` cannot emit "
            "close_type `epic`; an implementer hands back with close_type "
            "`story` and the auditor approves the epic close."
        )
    return errors


def _finalizer_producer_errors(
    parsed: HandoffRouting,
    normalized_next_agent: str | None,
) -> list[str]:
    """Errors for a `producer: finalizer` handoff.

    An unknown `next_agent` or `close_type` is already reported by the
    generic checks, so only recognized values are checked here.
    """

    errors: list[str] = []
    if (
        normalized_next_agent is not None
        and normalized_next_agent not in _FINALIZER_ALLOWED_NEXT_AGENTS
    ):
        errors.append(
            "frontmatter_routing_rule: producer `finalizer` cannot route to "
            f"next_agent `{normalized_next_agent}`; a finalizer routes only to "
            f"{', '.join(f'`{agent}`' for agent in _FINALIZER_ALLOWED_NEXT_AGENTS)}."
        )
    if parsed.close_type in _ALLOWED_CLOSE_TYPES:
        errors.append(
            "frontmatter_routing_rule: producer `finalizer` emitted close_type "
            f"`{parsed.close_type}`; a finalizer records a closed epic and "
            "omits close_type."
        )
    if parsed.status in _FINALIZER_REJECTED_STATUSES:
        errors.append(
            "frontmatter_status_producer_rule: producer `finalizer` cannot emit "
            f"status `{parsed.status}`; a finalizer omits status or uses "
            "`verified_pass`, `blocked_missing_context`, or `escalate_to_user`."
        )
    return errors


def _auditor_producer_errors(
    parsed: HandoffRouting,
    normalized_next_agent: str | None,
) -> list[str]:
    errors: list[str] = []
    if normalized_next_agent == "auditor":
        errors.append(
            "frontmatter_routing_rule: producer `auditor` cannot route to "
            "next_agent `auditor`; an auditor approves, fails, or escalates "
            "the work it reviewed."
        )
    if parsed.status in _AUDITOR_REJECTED_STATUSES:
        errors.append(
            "frontmatter_status_producer_rule: producer `auditor` cannot emit "
            f"status `{parsed.status}`; an auditor approves or fails work with "
            "`verified_pass` or `verified_fail`."
        )
    return errors


def _validator_producer_errors(
    parsed: HandoffRouting,
    normalized_next_agent: str | None,
    *,
    dispatcher_authored: bool,
) -> list[str]:
    """Errors for a `producer: validator` handoff.

    A validator handoff may route anywhere except `finalizer`, never sets
    `close_type: epic`, and never emits `ready_for_review`. It carries
    `verified_pass` or `verified_fail` only when `dispatcher_authored` is
    set by a caller validating content the dispatcher wrote itself.
    """

    errors: list[str] = []
    if normalized_next_agent == "finalizer":
        errors.append(
            "frontmatter_routing_rule: producer `validator` cannot route to "
            "next_agent `finalizer`; only the auditor approves an epic close."
        )
    if parsed.close_type == "epic":
        errors.append(
            "frontmatter_routing_rule: producer `validator` emitted close_type "
            "`epic`; only the auditor approves an epic close."
        )
    if parsed.status in _VALIDATOR_ALWAYS_REJECTED_STATUSES:
        errors.append(
            "frontmatter_status_producer_rule: producer `validator` cannot emit "
            f"status `{parsed.status}`; only an implementer claims work for "
            "review."
        )
    elif parsed.status in _VERIFIED_STATUSES and not dispatcher_authored:
        errors.append(
            "frontmatter_status_producer_rule: producer `validator` cannot emit "
            f"status `{parsed.status}`; only the auditor approves or fails "
            "work, and only dispatcher-written handoffs carry a validator "
            "verdict."
        )
    return errors


def _verified_status_producer_errors(parsed: HandoffRouting) -> list[str]:
    """Errors for a verdict status from a producer that gives no verdicts.

    Covers planner, user, and unrecognized producers. Implementer, tpm,
    finalizer, auditor, and validator producers have their own rules.
    """

    if parsed.status not in _VERIFIED_STATUSES:
        return []
    producer = normalize_next_agent_value(parsed.producer) or parsed.producer
    return [
        f"frontmatter_status_producer_rule: producer `{producer}` cannot emit "
        f"status `{parsed.status}`; only the auditor approves or fails work, "
        "and only the finalizer carries an epic-close `verified_pass` forward."
    ]


def _finalizer_dispatch_status_errors(
    current: HandoffRouting,
    *,
    previous_route: str | None,
) -> list[str]:
    """Errors for a finalizer verified_pass claim after another role's dispatch.

    The finalizer's `verified_pass` carries the auditor's approval forward
    without evidence, so a non-finalizer turn cannot borrow that claim by
    writing `producer: finalizer`. An unknown previous role sets no
    constraint here; it also grants no evidence exemption.
    """

    if previous_route is None or previous_route == "finalizer":
        return []
    if normalize_next_agent_value(current.producer) != "finalizer":
        return []
    if current.status != HandoffStatus.VERIFIED_PASS.value:
        return []
    return [
        "frontmatter_status_producer_rule: producer `finalizer` emitted status "
        f"`verified_pass`, but the dispatched role is `{previous_route}`; only "
        "a finalizer dispatch carries the epic-close approval forward."
    ]


def _dispatch_producer_errors(
    current: HandoffRouting,
    *,
    previous_route: str | None,
    dispatcher_authored: bool = False,
) -> list[str]:
    """Errors for a dispatch whose handoff claims another role as producer.

    The dispatcher knows which role it ran, so no role can borrow another
    role's rules or authority by writing a different `producer` (a tpm
    writing `producer: planner`, or a planner or backend writing
    `producer: auditor` to route to the finalizer). The producer must
    normalize to the dispatched role under the canonical frontmatter
    normalization (`normalize_next_agent_value`).

    No check runs when the previous role is unknown (already a
    `unknown_previous_agent` warning; no identity is invented for it), when
    the producer is missing (already `frontmatter_producer_missing`), or when
    the caller validates a handoff the dispatcher wrote itself
    (`dispatcher_authored=True`), which always names `producer: validator`.
    """

    if dispatcher_authored or previous_route is None or not current.producer:
        return []
    if normalize_next_agent_value(current.producer) == previous_route:
        return []
    return [
        f"frontmatter_producer_mismatch: the dispatched role is "
        f"`{previous_route}`, but the handoff names producer "
        f"`{current.producer}`; a {previous_route} handoff sets "
        f"`producer: {previous_route}`."
    ]


def _tpm_epic_change_errors(
    current: HandoffRouting,
    prior_handoff_content: str,
    *,
    previous_route: str | None = None,
) -> list[str]:
    """Errors for a tpm rewrite that leaves the epic its dispatch started in.

    A tpm grooms stories inside the open epic; choosing or renaming an epic
    is planner work. Renaming the epic would also skip the story-list check,
    which compares only rewrites of the same epic. The guard applies when the
    dispatcher ran the tpm (`previous_route`) or the handoff names producer
    `tpm`, so a tpm cannot bypass it by claiming another producer. Epic IDs
    compare case-insensitively, as the story-list check does. A prior handoff
    without an epic ID, or one whose frontmatter does not parse, sets no
    constraint.
    """

    producer_is_tpm = normalize_next_agent_value(current.producer) == "tpm"
    if not producer_is_tpm and previous_route != "tpm":
        return []
    actor = "producer `tpm`" if producer_is_tpm else "dispatched role `tpm`"
    try:
        prior = parse_handoff_frontmatter_text(prior_handoff_content.lstrip("﻿"))
    except HandoffFrontmatterError:
        return []
    if prior is None:
        return []
    prior_epic = (prior.epic_id or "").strip()
    if not prior_epic:
        return []
    current_epic = (current.epic_id or "").strip()
    if current_epic.casefold() == prior_epic.casefold():
        return []
    if current_epic:
        change = f"changed epic_id from `{prior_epic}` to `{current_epic}`"
    else:
        change = f"removed epic_id `{prior_epic}`"
    return [
        f"frontmatter_routing_rule: {actor} {change}; a tpm stays "
        "inside the open epic and routes to `planner` when the work belongs "
        "to another epic."
    ]


def _tracker_lease_findings(
    parsed: HandoffRouting,
    *,
    tracker_provider: str,
    normalized_next_agent: str | None,
    normalized_producer: str | None,
) -> tuple[list[str], list[str]]:
    producer = parsed.producer or "unknown producer"
    errors: list[str] = []
    warnings: list[str] = []

    if (
        parsed.selection_basis is not None
        and parsed.selection_basis not in _SELECTION_BASIS_VALUES
    ):
        errors.append(
            "frontmatter_selection_basis_invalid: "
            f"producer {producer} used unsupported selection_basis "
            f"`{parsed.selection_basis}`; expected one of "
            f"{', '.join(_SELECTION_BASIS_VALUES)}."
        )

    if parsed.tracker_url is not None and (
        not parsed.tracker_url or re.search(r"\s", parsed.tracker_url)
    ):
        errors.append(
            "frontmatter_tracker_url_invalid: "
            f"producer {producer} emitted tracker_url `{parsed.tracker_url}`; "
            "expected a non-blank URL with no whitespace."
        )

    if tracker_provider != "linear":
        present = [
            name for name in _TRACKER_LEASE_FIELDS if getattr(parsed, name) is not None
        ]
        if present:
            warnings.append(
                "tracker_fields_ignored: "
                f"producer {producer} set {', '.join(present)}, but tracker.provider "
                "is not `linear`; the fields are ignored."
            )
        return errors, warnings

    if normalized_next_agent in _TRACKER_LEASE_ROUTES:
        if not parsed.tracker_issue:
            errors.append(
                "tracker_issue_missing: "
                f"producer {producer} routed to `{normalized_next_agent}` in Linear "
                "mode without tracker_issue."
            )
        if not parsed.tracker_url:
            errors.append(
                "tracker_url_missing: "
                f"producer {producer} routed to `{normalized_next_agent}` in Linear "
                "mode without tracker_url."
            )
        if parsed.tracker_issue and parsed.story_id != parsed.tracker_issue:
            errors.append(
                "tracker_story_id_mismatch: "
                f"producer {producer} emitted story_id `{parsed.story_id or ''}` "
                f"but tracker_issue `{parsed.tracker_issue}`; they must match."
            )

    if (
        normalized_producer == "planner"
        and normalized_next_agent in _IMPLEMENTER_ROUTES
        and parsed.selection_basis is None
    ):
        errors.append(
            "selection_basis_missing: "
            f"planner routed to `{normalized_next_agent}` in Linear mode without "
            "selection_basis."
        )

    return errors, warnings


def validate_handoff(
    handoff_path: Path,
    previous_agent: str,
    *,
    prior_handoff_sha: str | None = None,
    require_work_packet: bool = False,
    dispatcher_commit_mode: bool = False,
    tracker_provider: str = "none",
    prior_handoff_content: str | None = None,
    project_state_path: Path | None = None,
    dispatcher_authored: bool = False,
) -> ValidationResult:
    handoff_content = read_dispatch_text(handoff_path)
    project_state_content = (
        read_dispatch_text(project_state_path)
        if project_state_path is not None and project_state_path.is_file()
        else None
    )
    return validate_handoff_text(
        handoff_content,
        previous_agent,
        prior_handoff_sha=prior_handoff_sha,
        git_cwd=handoff_path.parent,
        require_work_packet=require_work_packet,
        dispatcher_commit_mode=dispatcher_commit_mode,
        tracker_provider=tracker_provider,
        prior_handoff_content=prior_handoff_content,
        project_state_content=project_state_content,
        dispatcher_authored=dispatcher_authored,
    )


def validate_handoff_text(
    handoff_content: str,
    previous_agent: str,
    *,
    prior_handoff_sha: str | None = None,
    git_cwd: Path | None = None,
    require_work_packet: bool = False,
    dispatcher_commit_mode: bool = False,
    tracker_provider: str = "none",
    prior_handoff_content: str | None = None,
    project_state_content: str | None = None,
    dispatcher_authored: bool = False,
) -> ValidationResult:
    """Validate a post-dispatch handoff.

    With `prior_handoff_content`, the handoff the dispatch started from, a
    rewrite that drops `remaining_stories` entries without saying why fails.
    With `project_state_content`, story IDs the project state file never names
    are warnings; Linear mode skips that check because the tracker holds the
    stories.

    `dispatcher_authored` is for callers checking a handoff the dispatcher
    wrote itself; agent output is always validated with it false. See
    `validate_handoff_frontmatter()`. Agent output must also name the
    dispatched role as `producer` (`frontmatter_producer_mismatch`); the flag
    skips only that identity check.
    """

    normalized_content = handoff_content.lstrip("\ufeff")
    current_handoff_sha = sha256(normalized_content.encode("utf-8")).hexdigest()
    warnings: list[str] = []
    errors: list[str] = []
    routing_instruction: str | None = None
    frontmatter_valid = False
    frontmatter_missing = False
    frontmatter_producer: str | None = None
    frontmatter: HandoffRouting | None = None

    previous_route = _normalize_agent(previous_agent)
    if previous_route is None:
        warnings.append(
            f"unknown_previous_agent: Could not normalize previous agent `{previous_agent}`."
        )

    if prior_handoff_sha is not None and prior_handoff_sha == current_handoff_sha:
        errors.append(
            "no_new_sha: HANDOFF content hash matches the prior dispatch state."
        )

    try:
        frontmatter = parse_handoff_frontmatter_text(normalized_content)
    except HandoffFrontmatterError as exc:
        frontmatter = None
        errors.append(
            "frontmatter_parse_error: producer unknown producer emitted invalid YAML "
            f"routing frontmatter: {exc}"
        )
    else:
        if frontmatter is None:
            frontmatter_missing = True
            errors.append(
                "frontmatter_missing: HANDOFF.md is missing required YAML routing "
                "frontmatter (producer: unknown producer)."
            )
        else:
            frontmatter_result = validate_handoff_frontmatter(
                frontmatter,
                git_cwd=git_cwd,
                tracker_provider=tracker_provider,
                dispatcher_authored=dispatcher_authored,
            )
            errors.extend(frontmatter_result.errors)
            warnings.extend(frontmatter_result.warnings)
            routing_instruction = frontmatter_result.routing_instruction
            frontmatter_valid = not frontmatter_result.errors
            frontmatter_producer = frontmatter.producer
            errors.extend(
                _dispatch_producer_errors(
                    frontmatter,
                    previous_route=previous_route,
                    dispatcher_authored=dispatcher_authored,
                )
            )
            errors.extend(
                _finalizer_dispatch_status_errors(
                    frontmatter, previous_route=previous_route
                )
            )
            if prior_handoff_content is not None:
                errors.extend(
                    _tpm_epic_change_errors(
                        frontmatter,
                        prior_handoff_content,
                        previous_route=previous_route,
                    )
                )
            errors.extend(
                _implementer_scope_sha_errors(
                    frontmatter,
                    previous_route=previous_route,
                )
            )

    routing_decision = route_handoff(normalized_content)
    if frontmatter_missing:
        routing_instruction = (
            None if routing_decision.route == "unknown" else routing_decision.route
        )
    if routing_instruction is None:
        errors.append(
            "routing_instruction_missing: HANDOFF does not provide a dispatchable next step."
        )
    elif frontmatter_missing and routing_decision.confidence == "LOW":
        warnings.append(f"routing_low_confidence: {routing_decision.reasoning}")
    elif frontmatter_valid and routing_decision.confidence == "LOW":
        errors.append(
            "routing_instruction_missing: HANDOFF frontmatter did not produce a dispatchable route."
        )
    if previous_route is not None and routing_instruction == previous_route:
        if previous_route == "planner":
            errors.append(
                "planner_self_loop: planner handoff routes work back to planner, which would immediately re-dispatch the planner. Route to a backend agent, auditor, or explicit pause state instead."
            )
        else:
            errors.append(
                f"agent_self_loop: {previous_route} handoff routes work back to {routing_instruction}, which would immediately re-dispatch the same agent. Route to a different agent or explicit pause state instead."
            )

    commit_shas = _commit_sha_tokens(normalized_content, git_cwd)
    if _requires_commit_sha(previous_route):
        if not commit_shas:
            if dispatcher_commit_mode and _is_blocked_pending_commit_handback(
                normalized_content,
                frontmatter,
                previous_route=previous_route,
            ):
                warnings.append(
                    "sha_pending: Blocked implementer handback has no commit SHA; "
                    "dispatcher commit mode leaves blocked work uncommitted."
                )
            else:
                errors.append("sha_missing: Handoff is missing a git commit SHA.")
    elif not commit_shas:
        warnings.append(
            "sha_missing: Planner handoff does not yet include a git commit SHA."
        )

    errors.extend(
        _status_contract_errors(
            frontmatter,
            previous_route=previous_route,
            routing_instruction=routing_instruction,
        )
    )
    errors.extend(
        _evidence_contract_errors(
            normalized_content,
            frontmatter,
            previous_route=previous_route,
            git_cwd=git_cwd,
            dispatcher_commit_mode=dispatcher_commit_mode,
        )
    )
    if require_work_packet:
        errors.extend(
            _work_packet_contract_errors(
                normalized_content,
                frontmatter=frontmatter,
                previous_route=previous_route,
                routing_instruction=routing_instruction,
            )
        )
    else:
        warnings.extend(
            _work_packet_contract_warnings(
                normalized_content,
                previous_route=previous_route,
                routing_instruction=routing_instruction,
            )
        )
    warnings.extend(
        _rationalization_warnings(
            normalized_content,
            frontmatter=frontmatter,
            previous_route=previous_route,
        )
    )
    errors.extend(
        _scope_claim_errors(
            normalized_content,
            previous_route,
            routing_instruction,
            frontmatter_producer=frontmatter_producer,
        )
    )
    warnings.extend(
        _content_warnings(
            normalized_content,
            previous_route,
            routing_instruction,
        )
    )
    if prior_handoff_content is not None:
        errors.extend(story_list_errors(prior_handoff_content, normalized_content))
    if tracker_provider == "none":
        warnings.extend(
            project_state_story_warnings(normalized_content, project_state_content)
        )

    return ValidationResult(
        verdict=_derive_verdict(errors, warnings),
        warnings=_dedupe(warnings),
        errors=_dedupe(errors),
        routing_instruction=routing_instruction,
    )


# Dispatcher-handoff provenance. The record holds only the SHA-256 of the
# handoff the dispatcher last wrote itself, so any edit to HANDOFF.md drops
# the trust. The dispatcher clears it when an agent dispatch returns, so a
# record an agent planted during its turn is gone before anything reads it.
_DISPATCHER_HANDOFF_RECORD_VERSION = 1
_DISPATCHER_HANDOFF_RECORD_MAX_BYTES = 4096
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def dispatcher_handoff_digest(handoff_content: str) -> str:
    """SHA-256 of a handoff's text, ignoring a leading byte-order mark."""

    return sha256(handoff_content.lstrip("﻿").encode("utf-8")).hexdigest()


def dispatcher_handoff_state_path(repo_root: Path) -> Path:
    return repo_root / DISPATCHER_HANDOFF_STATE_PATH


def _is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    # Windows reports junctions and other reparse points through
    # st_file_attributes on Python 3.11 too; is_junction() is 3.12+.
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _dispatcher_handoff_path_is_unsafe(repo_root: Path) -> bool:
    """Whether `logs`, `logs/dispatch`, or the record is a link or junction.

    Missing components are safe. A directory component that is not a
    directory, or a record that is not a single-link regular file, is unsafe.
    """

    current = repo_root
    parts = DISPATCHER_HANDOFF_STATE_PATH.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if _is_link_or_reparse_point(metadata):
            return True
        if index < len(parts) - 1:
            if not stat.S_ISDIR(metadata.st_mode):
                return True
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            return True
    return False


def record_dispatcher_handoff(repo_root: Path, handoff_content: str) -> bool:
    """Record `handoff_content` as the handoff the dispatcher just wrote.

    Pass the text as read back from disk, so the digest matches what a later
    reader sees. Returns False, writing nothing, when the record path goes
    through a link or junction or the write fails.
    """

    if _dispatcher_handoff_path_is_unsafe(repo_root):
        return False
    path = dispatcher_handoff_state_path(repo_root)
    payload = json.dumps(
        {
            "version": _DISPATCHER_HANDOFF_RECORD_VERSION,
            "sha256": dispatcher_handoff_digest(handoff_content),
        },
        indent=2,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _dispatcher_handoff_path_is_unsafe(repo_root):
            return False
        # mkstemp opens a new randomly named file exclusively, and os.replace
        # swaps the directory entry without following a link at the target.
        file_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError:
        return False
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload + "\n")
        os.replace(temporary_path, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary_path.unlink()
        return False
    return True


def clear_dispatcher_handoff(repo_root: Path) -> bool:
    """Delete the provenance record; True when no record is left.

    A record path that goes through a link or junction is not touched;
    `is_recorded_dispatcher_handoff()` refuses to read it either way.
    """

    path = dispatcher_handoff_state_path(repo_root)
    current = repo_root
    for part in DISPATCHER_HANDOFF_STATE_PATH.parent.parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if _is_link_or_reparse_point(metadata) or not stat.S_ISDIR(
            metadata.st_mode
        ):
            return False
    for attempt in range(2):
        try:
            # unlink removes a file symlink itself, never its target.
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            # A read-only record (as an agent could leave it on Windows).
            if attempt == 0:
                with contextlib.suppress(OSError):
                    if stat.S_ISREG(os.lstat(path).st_mode):
                        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                continue
            return False
        except OSError:
            return False
    return False


def is_recorded_dispatcher_handoff(repo_root: Path, handoff_content: str) -> bool:
    """Whether `handoff_content` is the handoff the dispatcher last recorded.

    False when there is no record, the record is malformed, or its path goes
    through a link or junction. Callers pass the result as
    `dispatcher_authored` to `validate_handoff_text()`.
    """

    if _dispatcher_handoff_path_is_unsafe(repo_root):
        return False
    path = dispatcher_handoff_state_path(repo_root)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        file_fd = os.open(path, flags)
    except OSError:
        return False
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or (
            metadata.st_size > _DISPATCHER_HANDOFF_RECORD_MAX_BYTES
        ):
            return False
        data = os.read(file_fd, _DISPATCHER_HANDOFF_RECORD_MAX_BYTES + 1)
    except OSError:
        return False
    finally:
        os.close(file_fd)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    version = payload.get("version")
    if type(version) is not int or version != _DISPATCHER_HANDOFF_RECORD_VERSION:
        return False
    recorded = payload.get("sha256")
    if not isinstance(recorded, str) or not _SHA256_HEX_RE.fullmatch(recorded):
        return False
    return hmac.compare_digest(
        recorded.encode("ascii"),
        dispatcher_handoff_digest(handoff_content).encode("ascii"),
    )


def _normalize_agent(agent_text: str) -> str | None:
    role, _warnings = normalize_agent_label(agent_text)
    return role


def _status_contract_errors(
    frontmatter: HandoffRouting | None,
    *,
    previous_route: str | None,
    routing_instruction: str | None,
) -> list[str]:
    if frontmatter is None:
        return []
    if not _requires_status(previous_route, routing_instruction, frontmatter):
        return []
    if frontmatter.status:
        return []
    producer = frontmatter.producer or "unknown producer"
    return [
        "STATUS_MISSING: "
        f"producer {producer} omitted required status for completion-class route `{routing_instruction}`."
    ]


def _requires_status(
    previous_route: str | None,
    routing_instruction: str | None,
    frontmatter: HandoffRouting,
) -> bool:
    if frontmatter.status is not None:
        return True
    return frontmatter.evidence_present is not None


def _implementer_scope_sha_errors(
    frontmatter: HandoffRouting,
    *,
    previous_route: str | None,
) -> list[str]:
    # An implementer's completion claim must name the commit it claims. The
    # close_type rule in validate_handoff_frontmatter already reports the
    # same prefix when close_type is set, so skip that case to avoid a
    # duplicate. In dispatcher commit mode the dispatcher fills scope_sha
    # before validation; it stays empty only when nothing was committed, and
    # the orchestrator then lets the work-claim gate report no_changes.
    if _frontmatter_status(frontmatter) != HandoffStatus.READY_FOR_REVIEW:
        return []
    if frontmatter.scope_sha or frontmatter.close_type is not None:
        return []
    producer_route = normalize_next_agent_value(frontmatter.producer)
    if (
        previous_route not in _IMPLEMENTER_ROUTES
        and producer_route not in _IMPLEMENTER_ROUTES
    ):
        return []
    producer = frontmatter.producer or "unknown producer"
    return [
        "frontmatter_scope_sha_missing: "
        f"producer {producer} omitted scope_sha on a ready_for_review handback; "
        "an implementer claim must name the commit that holds the work."
    ]


def _evidence_contract_errors(
    handoff_content: str,
    frontmatter: HandoffRouting | None,
    *,
    previous_route: str | None = None,
    git_cwd: Path | None = None,
    dispatcher_commit_mode: bool = False,
) -> list[str]:
    status = _frontmatter_status(frontmatter)
    if status not in COMPLETION_STATUSES:
        return []
    if (
        status == HandoffStatus.VERIFIED_PASS
        and previous_route == "finalizer"
        and frontmatter is not None
        and normalize_next_agent_value(frontmatter.producer) == "finalizer"
    ):
        # The finalizer's epic-close handoff carries the auditor's approval
        # forward; it verifies no commit itself. The auditor handoff it
        # replaces held the evidence, and the dispatch loop does not run
        # post-dispatch validation on finalizer output. Both the dispatched
        # role and the producer must be the finalizer; doctor and the
        # supervisor preflight pass the producer as the previous role.
        return []

    block = _verification_evidence_block(handoff_content)
    if block is None:
        line_number = _frontmatter_status_line_number(handoff_content) or 1
        return [
            "EVIDENCE_MISSING: "
            f"status {status.value} requires a populated Verification Evidence block (status line {line_number})."
        ]

    fields = _evidence_fields(block)
    missing = sorted(_REQUIRED_EVIDENCE_FIELDS - set(fields))
    empty = sorted(name for name, value in fields.items() if not value.strip())
    if missing or empty:
        line_number = block[0]
        detail = ", ".join(
            [
                *(f"missing {name}" for name in missing),
                *(f"empty {name}" for name in empty),
            ]
        )
        return [
            "EVIDENCE_MISSING: "
            f"status {status.value} requires populated evidence fields at line {line_number}: {detail}."
        ]

    raw_commit_value = fields["Commit SHA verified"].strip()
    commit_value = raw_commit_value.strip("`")
    if commit_value.upper() == "HEAD":
        return [
            "EVIDENCE_MISSING: Verification Evidence cannot use HEAD as Commit SHA verified."
        ]
    if dispatcher_commit_mode and _PENDING_COMMIT_RE.match(raw_commit_value):
        # The dispatcher replaces the marker when it commits; when it commits
        # nothing, the work-claim gate reports no_changes instead.
        return []
    token = evidence_sha_token(commit_value)
    if token is None:
        if not _git_repo_available(git_cwd):
            return []
        return [
            "evidence_sha_unknown: "
            f"Commit SHA verified `{_clip(commit_value)}` names no commit SHA; "
            "write the 7-40 character SHA of the verified commit."
        ]
    if _git_commit_exists(token, git_cwd) is False:
        return [
            "evidence_sha_unknown: "
            f"Commit SHA verified `{token}` does not resolve to a commit in this "
            "repository."
        ]
    return []


def _work_packet_contract_warnings(
    handoff_content: str,
    *,
    previous_route: str | None,
    routing_instruction: str | None,
) -> list[str]:
    if previous_route not in _PLANNING_ROUTES or routing_instruction not in {
        "backend",
        "frontend",
    }:
        return []
    if _WORK_PACKET_RE.search(handoff_content):
        return []
    if (
        _OBJECTIVE_RE.search(handoff_content) is not None
        and _ACCEPTANCE_CRITERIA_RE.search(handoff_content) is not None
    ):
        return []
    return [
        f"work_packet_missing: {previous_route} handoffs to backend or frontend require a ## Work Packet section."
    ]


def _work_packet_contract_errors(
    handoff_content: str,
    *,
    frontmatter: HandoffRouting | None,
    previous_route: str | None,
    routing_instruction: str | None,
) -> list[str]:
    # With enforce_work_packet enabled, every handoff that sends work to an
    # implementer must carry a packet the work-claim gate can enforce later.
    # Implementer-to-implementer routes are already rejected as self-loops.
    # Only a plain assignment or a verified_fail actually dispatches the
    # implementer; every other status routes elsewhere regardless of next_agent.
    if routing_instruction not in _IMPLEMENTER_ROUTES:
        return []
    if previous_route is None or previous_route in _IMPLEMENTER_ROUTES:
        return []
    if frontmatter is not None and frontmatter.status:
        if _frontmatter_status(frontmatter) != HandoffStatus.VERIFIED_FAIL:
            return []
    return [
        f"{error} Handoffs routed to {routing_instruction} must carry a parseable "
        "## Work Packet while enforce_work_packet is enabled."
        for error in work_packet_errors(handoff_content)
    ]


def _rationalization_warnings(
    handoff_content: str,
    *,
    frontmatter: HandoffRouting | None,
    previous_route: str | None,
) -> list[str]:
    status = _frontmatter_status(frontmatter)
    if status is None:
        return []
    role = (
        frontmatter.producer if frontmatter and frontmatter.producer else previous_route
    )
    matches = detect_rationalizations(role or "unknown", handoff_content, status)
    if not matches:
        return []
    details = ", ".join(
        f"line {match.line_number}: `{match.phrase}`" for match in matches[:3]
    )
    return [f"RATIONALIZATION_DETECTED: {details}"]


def _frontmatter_status(frontmatter: HandoffRouting | None) -> HandoffStatus | None:
    if frontmatter is None or frontmatter.status is None:
        return None
    try:
        return HandoffStatus(frontmatter.status)
    except ValueError:
        return None


def _frontmatter_status_line_number(handoff_content: str) -> int | None:
    for line_number, line in enumerate(handoff_content.splitlines(), start=1):
        if line.strip().startswith("status:"):
            return line_number
    return None


def _verification_evidence_block(
    handoff_content: str,
) -> tuple[int, str] | None:
    match = _VERIFICATION_EVIDENCE_RE.search(handoff_content)
    if match is None:
        return None
    start = match.end()
    next_heading = _HEADING_RE.search(handoff_content, start)
    end = next_heading.start() if next_heading is not None else len(handoff_content)
    line_number = handoff_content[: match.start()].count("\n") + 1
    return line_number, handoff_content[start:end]


def _evidence_fields(block: tuple[int, str]) -> dict[str, str]:
    """Map each Verification Evidence field to its value.

    Like a Work Packet field, a value is the text after the label plus any
    indented lines below it. ``Commit SHA verified`` is the exception: its SHA
    must be on the label line, the line the work-claim gate and the
    dispatcher's commit stamp read.
    """

    fields = _bullet_field_values(_EVIDENCE_FIELD_RE, block)
    for match in _EVIDENCE_FIELD_RE.finditer(block[1]):
        if match.group(1) == "Commit SHA verified":
            fields["Commit SHA verified"] = match.group(2).strip()
    return fields


def _work_packet_block(handoff_content: str) -> tuple[int, str] | None:
    match = _WORK_PACKET_HEADING_RE.search(handoff_content)
    if match is None:
        return None
    start = match.end()
    next_heading = _HEADING_RE.search(handoff_content, start)
    end = next_heading.start() if next_heading is not None else len(handoff_content)
    line_number = handoff_content[: match.start()].count("\n") + 1
    return line_number, handoff_content[start:end]


def _work_packet_fields(block: tuple[int, str]) -> dict[str, str]:
    """Map each Work Packet field to its value.

    A value is the text after the label plus any indented lines below it, so a
    field written as nested bullets (``- **Context:**`` followed by indented
    ``- ...`` items) is not empty.
    """

    return _bullet_field_values(_WORK_PACKET_FIELD_RE, block)


def _bullet_field_values(
    field_re: re.Pattern[str], block: tuple[int, str]
) -> dict[str, str]:
    _line_number, content = block
    matches = list(field_re.finditer(content))
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        parts = [match.group(2).strip()]
        for line in content[match.end() : end].splitlines():
            if not line.strip():
                continue
            if not line[0].isspace():
                break
            parts.append(line.strip())
        fields[match.group(1)] = " ".join(part for part in parts if part)
    return fields


def _work_packet_coverage_warnings(block: tuple[int, str]) -> list[str]:
    fields = _work_packet_fields(block)
    warnings: list[str] = []
    for name in _REQUIRED_WORK_PACKET_FIELDS:
        if name not in fields:
            warnings.append(
                f"acceptance_coverage_unclear: Work Packet is missing the {name} field."
            )
        elif not fields[name]:
            warnings.append(
                f"acceptance_coverage_unclear: Work Packet field {name} is empty."
            )
    return warnings


def _git_repo_available(cwd: Path | None) -> bool:
    if cwd is None:
        return False
    rev_parse = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return rev_parse.returncode == 0


def _git_commit_exists(sha: str, cwd: Path | None) -> bool | None:
    if not _git_repo_available(cwd):
        return None

    completed = subprocess.run(
        ["git", "-C", str(cwd), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _commit_sha_tokens(handoff_content: str, cwd: Path | None) -> list[str]:
    """Return the hex tokens in the handoff that name commits.

    Outside a git repository every 7-40 character hex token counts, because
    there is nothing to resolve them against.
    """

    tokens = list(dict.fromkeys(_COMMIT_SHA_RE.findall(handoff_content)))
    if not tokens or not _git_repo_available(cwd):
        return tokens
    completed = subprocess.run(
        ["git", "-C", str(cwd), "cat-file", "--batch-check"],
        input="".join(f"{token}^{{commit}}\n" for token in tokens),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return tokens
    lines = completed.stdout.splitlines()
    if len(lines) != len(tokens):
        return tokens
    # A resolved object prints "<sha> commit <size>"; anything else (missing,
    # ambiguous, or a non-commit) does not name a commit.
    return [
        token
        for token, line in zip(tokens, lines)
        if line.split(" ")[1:2] == ["commit"]
    ]


def _clip(value: str, limit: int = 80) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _requires_commit_sha(previous_route: str | None) -> bool:
    return previous_route in {
        "backend",
        "frontend",
        "auditor",
        "validator",
    }


def _is_blocked_pending_commit_handback(
    handoff_content: str,
    frontmatter: HandoffRouting | None,
    *,
    previous_route: str | None,
) -> bool:
    # With commit_mode: dispatcher the dispatcher commits only after a
    # ready_for_review claim, so a blocked or escalated implementer handback
    # has no commit to cite and carries the pending marker instead.
    if previous_route not in _IMPLEMENTER_ROUTES:
        return False
    if _frontmatter_status(frontmatter) not in BLOCKED_STATUSES:
        return False
    block = _verification_evidence_block(handoff_content)
    if block is None:
        return False
    commit_value = _evidence_fields(block).get("Commit SHA verified", "")
    # Agents often add a note after the marker, such as
    # "pending-dispatcher-commit (nothing to commit)".
    return _PENDING_COMMIT_RE.match(commit_value.strip()) is not None


def _scope_claim_errors(
    handoff_content: str,
    previous_route: str | None,
    routing_instruction: str | None,
    *,
    frontmatter_producer: str | None = None,
) -> list[str]:
    if previous_route is None:
        return []

    has_task_assignment = _TASK_ASSIGNMENT_RE.search(handoff_content) is not None

    if previous_route in _PLANNING_ROUTES:
        if _planning_route_skips_work_packet(previous_route, routing_instruction):
            return []
        has_work_packet = _WORK_PACKET_RE.search(handoff_content) is not None
        if not has_work_packet and not has_task_assignment:
            return [
                f"scope_claim_mismatch: {previous_route} handoffs must include a ## Work Packet section."
            ]
        return []

    if has_task_assignment and _REPORTING_HEADER_RE.search(handoff_content) is None:
        return [
            "scope_claim_mismatch: Implementation or audit handoff still looks like a Task Assignment."
        ]

    agent_match = _ownership_agent_line_match(handoff_content)
    if agent_match is None:
        producer_route = _normalize_agent(frontmatter_producer or "")
        if _author_role(producer_route) == _author_role(previous_route):
            return []
        return ["scope_claim_missing: Handoff is missing an **Agent:** ownership line."]

    claimed_route = _normalize_agent(agent_match.group(1))
    if _author_role(claimed_route) != _author_role(previous_route):
        return [
            "scope_claim_mismatch: Handoff ownership line does not match the agent that just finished."
        ]

    return []


def _planning_route_skips_work_packet(
    previous_route: str, routing_instruction: str | None
) -> bool:
    """Return whether a planner or tpm handoff on this route needs no Work Packet.

    A route to the user pauses for a person. A planner opening an epic for
    the tpm, and a tpm escalating to the planner, assign no implementation
    work either. Routes to an implementer or the auditor still need one.
    """

    if routing_instruction == "user":
        return True
    if previous_route == "planner" and routing_instruction == "tpm":
        return True
    return previous_route == "tpm" and routing_instruction == "planner"


def _ownership_agent_line_match(handoff_content: str) -> re.Match[str] | None:
    task_assignment_match = _TASK_ASSIGNMENT_RE.search(handoff_content)
    task_assignment_start = (
        task_assignment_match.start() if task_assignment_match is not None else None
    )

    for agent_match in _AGENT_LINE_RE.finditer(handoff_content):
        if task_assignment_start is None or agent_match.start() < task_assignment_start:
            return agent_match
    return None


def _content_warnings(
    handoff_content: str,
    previous_route: str | None,
    routing_instruction: str | None,
) -> list[str]:
    warnings: list[str] = []

    if previous_route in _PLANNING_ROUTES:
        if _planning_route_skips_work_packet(previous_route, routing_instruction):
            return warnings
        if _TASK_ASSIGNMENT_RE.search(handoff_content) is not None:
            warnings.append(_TASK_ASSIGNMENT_DEPRECATION_WARNING)
        work_packet = _work_packet_block(handoff_content)
        if work_packet is not None:
            warnings.extend(_work_packet_coverage_warnings(work_packet))
            return warnings
        if _OBJECTIVE_RE.search(handoff_content) is None:
            warnings.append(
                "acceptance_coverage_unclear: Task Assignment is missing an Objective section."
            )
        if _ACCEPTANCE_CRITERIA_RE.search(handoff_content) is None:
            warnings.append(
                "acceptance_coverage_unclear: Task Assignment is missing an Acceptance Criteria section."
            )
        return warnings

    if _SUBSTANCE_RE.search(handoff_content) is None:
        warnings.append(
            "acceptance_coverage_unclear: Handoff does not cite completed work, verification, or audit checks."
        )

    return warnings


def _author_role(route_name: str | None) -> str | None:
    # Ownership validation treats audit and validator remediation as the
    # same author while routing still keeps the two dispatch paths distinct.
    if route_name in {"auditor", "validator", "auditor (audit)"}:
        return "auditor-family"
    return route_name


def _derive_verdict(
    errors: list[str],
    warnings: list[str],
) -> ValidationVerdict:
    if errors:
        return "NO"
    if warnings:
        return "WARNINGS-ONLY"
    return "YES"


def _dedupe(messages: list[str]) -> list[str]:
    return list(dict.fromkeys(messages))


def _infer_route_from_text(detail: str) -> str | None:
    inferred_route = _normalize_agent(detail)
    if inferred_route is not None:
        return inferred_route
    return None
