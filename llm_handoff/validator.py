from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from typing import Literal

from llm_handoff.router import (
    HandoffFrontmatterError,
    HandoffRouting,
    parse_handoff_frontmatter_text,
    route as route_handoff,
)
from llm_handoff.roles import (
    legacy_next_agent_warning,
    normalize_agent_label,
    normalize_next_agent_value,
)
from llm_handoff.text_io import read_dispatch_text


ValidationVerdict = Literal["YES", "NO", "WARNINGS-ONLY"]

_VALID_RE = re.compile(r"(?im)^VALID:\s*(YES|NO|WARNINGS-ONLY)\s*$")
_CHECK_LINE_RE = re.compile(
    r"(?im)^\s{2}([A-Z-]+):\s*(PASS|WARN|FAIL)\s*(?:-|—)\s*(.+)$"
)
_TASK_ASSIGNMENT_RE = re.compile(r"(?im)^#{1,6}\s+Task Assignment\b")
_OBJECTIVE_RE = re.compile(r"(?im)^#{1,6}\s+Objective\b")
_ACCEPTANCE_CRITERIA_RE = re.compile(r"(?im)^#{1,6}\s+Acceptance Criteria\b")
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


def validate_handoff_frontmatter(parsed: HandoffRouting) -> ValidationResult:
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
    elif alias_warning := legacy_next_agent_warning(parsed.next_agent):
        warnings.append(f"frontmatter_next_agent_alias: {alias_warning}")

    if not parsed.reason:
        errors.append(
            f"frontmatter_reason_missing: producer {producer} omitted required reason."
        )
    elif len(parsed.reason) >= 200:
        warnings.append(
            f"frontmatter_reason_too_long: producer {producer} emitted a reason >= 200 characters."
        )

    if parsed.close_type is not None and parsed.close_type not in _ALLOWED_CLOSE_TYPES:
        errors.append(
            "frontmatter_close_type_invalid: "
            f"producer {producer} used unsupported close_type `{parsed.close_type}`."
        )

    if parsed.close_type is not None:
        if not parsed.scope_sha:
            errors.append(
                "frontmatter_scope_sha_missing: "
                f"producer {producer} omitted scope_sha while close_type is set."
            )
        elif _SHA_VALUE_RE.fullmatch(parsed.scope_sha) is None:
            errors.append(
                "frontmatter_scope_sha_invalid: "
                f"producer {producer} emitted scope_sha `{parsed.scope_sha}`; expected 7-40 hex chars."
            )

    if parsed.scope_sha and _SHA_VALUE_RE.fullmatch(parsed.scope_sha) is None:
        errors.append(
            "frontmatter_scope_sha_invalid: "
            f"producer {producer} emitted scope_sha `{parsed.scope_sha}`; expected 7-40 hex chars."
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
) -> ValidationResult:
    handoff_content = read_dispatch_text(handoff_path)
    return validate_handoff_text(
        handoff_content,
        previous_agent,
        prior_handoff_sha=prior_handoff_sha,
    )


def validate_handoff_text(
    handoff_content: str,
    previous_agent: str,
    *,
    prior_handoff_sha: str | None = None,
) -> ValidationResult:
    normalized_content = handoff_content.lstrip("\ufeff")
    current_handoff_sha = sha256(normalized_content.encode("utf-8")).hexdigest()
    warnings: list[str] = []
    errors: list[str] = []
    routing_instruction: str | None = None
    frontmatter_valid = False
    frontmatter_missing = False
    frontmatter_producer: str | None = None

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
            frontmatter_result = validate_handoff_frontmatter(frontmatter)
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
            errors.append("sha_missing: Handoff is missing a git commit SHA.")
    elif not commit_shas:
        warnings.append(
            "sha_missing: Planner handoff does not yet include a git commit SHA."
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


def _requires_commit_sha(previous_route: str | None) -> bool:
    return previous_route in {
        "backend",
        "frontend",
        "auditor",
        "validator",
    }


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
        if not has_task_assignment:
            return [
                "scope_claim_mismatch: planner handoffs must use a Task Assignment block."
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
    if route_name in {"auditor", "validator"}:
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

