from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
import subprocess
from typing import Literal

from llm_handoff.agent_types import (
    BLOCKED_STATUSES,
    COMPLETION_STATUSES,
    HandoffStatus,
)
from llm_handoff.dispatcher_commit import PENDING_COMMIT_SHA
from llm_handoff.rationalization_detector import detect as detect_rationalizations
from llm_handoff.router import (
    HandoffFrontmatterError,
    HandoffRouting,
    parse_handoff_frontmatter_text,
    route as route_handoff,
)
from llm_handoff.roles import (
    normalize_agent_label,
    normalize_next_agent_value,
)
from llm_handoff.text_io import read_dispatch_text
from llm_handoff.work_claim import work_packet_errors


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
    r"(?im)^-\s+\*\*(Commands run|Output summary|Commit SHA verified|Files changed or reviewed|Unresolved concerns):\*\*\s*(.*)$"
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
) -> ValidationResult:
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

    routing_instruction = normalized_next_agent
    return ValidationResult(
        verdict=_derive_verdict(errors, warnings),
        warnings=_dedupe(warnings),
        errors=_dedupe(errors),
        routing_instruction=routing_instruction,
    )


def validate_handoff(
    handoff_path: Path,
    previous_agent: str,
    *,
    prior_handoff_sha: str | None = None,
    require_work_packet: bool = False,
    dispatcher_commit_mode: bool = False,
) -> ValidationResult:
    handoff_content = read_dispatch_text(handoff_path)
    return validate_handoff_text(
        handoff_content,
        previous_agent,
        prior_handoff_sha=prior_handoff_sha,
        git_cwd=handoff_path.parent,
        require_work_packet=require_work_packet,
        dispatcher_commit_mode=dispatcher_commit_mode,
    )


def validate_handoff_text(
    handoff_content: str,
    previous_agent: str,
    *,
    prior_handoff_sha: str | None = None,
    git_cwd: Path | None = None,
    require_work_packet: bool = False,
    dispatcher_commit_mode: bool = False,
) -> ValidationResult:
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
            )
            errors.extend(frontmatter_result.errors)
            warnings.extend(frontmatter_result.warnings)
            routing_instruction = frontmatter_result.routing_instruction
            frontmatter_valid = not frontmatter_result.errors
            frontmatter_producer = frontmatter.producer

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

    commit_shas = _COMMIT_SHA_RE.findall(normalized_content)
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
    errors.extend(_evidence_contract_errors(normalized_content, frontmatter))
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

    return ValidationResult(
        verdict=_derive_verdict(errors, warnings),
        warnings=_dedupe(warnings),
        errors=_dedupe(errors),
        routing_instruction=routing_instruction,
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


def _evidence_contract_errors(
    handoff_content: str,
    frontmatter: HandoffRouting | None,
) -> list[str]:
    status = _frontmatter_status(frontmatter)
    if status not in COMPLETION_STATUSES:
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

    commit_value = fields["Commit SHA verified"].strip().strip("`")
    if commit_value.upper() == "HEAD":
        return [
            "EVIDENCE_MISSING: Verification Evidence cannot use HEAD as Commit SHA verified."
        ]
    return []


def _work_packet_contract_warnings(
    handoff_content: str,
    *,
    previous_route: str | None,
    routing_instruction: str | None,
) -> list[str]:
    if previous_route != "planner" or routing_instruction not in {
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
        "work_packet_missing: planner handoffs to backend or frontend require a ## Work Packet section."
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
    _line_number, content = block
    return {
        match.group(1): match.group(2).strip()
        for match in _EVIDENCE_FIELD_RE.finditer(content)
    }


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

    _line_number, content = block
    matches = list(_WORK_PACKET_FIELD_RE.finditer(content))
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


def _git_commit_exists(sha: str, cwd: Path | None) -> bool | None:
    if cwd is None:
        return None

    rev_parse = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if rev_parse.returncode != 0:
        return None

    completed = subprocess.run(
        ["git", "-C", str(cwd), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


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

    if previous_route == "planner":
        if routing_instruction == "user":
            return []
        has_work_packet = _WORK_PACKET_RE.search(handoff_content) is not None
        if not has_work_packet and not has_task_assignment:
            return [
                "scope_claim_mismatch: planner handoffs must include a ## Work Packet section."
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

    if previous_route == "planner":
        if routing_instruction == "user":
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
