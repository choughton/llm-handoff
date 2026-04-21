from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator
import yaml

from llm_handoff.roles import (
    DispatchRole,
    legacy_next_agent_warning,
    normalize_agent_label,
    normalize_next_agent_value,
)
from llm_handoff.text_io import read_dispatch_text


RouteName = DispatchRole
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
NextAgent = Literal[
    "auditor",
    "backend",
    "finalizer",
    "frontend",
    "planner",
    "validator",
    "user",
]
CloseType = Literal["story", "epic"]

_NEXT_STEP_HEADER_RE = re.compile(r"(?i)^(#{1,6})\s+Next\s+Steps?\b(.*)$")
_TASK_ASSIGNMENT_HEADER_RE = re.compile(r"(?i)^(#{1,6})\s+Task Assignment\b")
_HEADING_RE = re.compile(r"^(#{1,6})\s+")
_REPORTING_HEADING_RE = re.compile(
    r"(?i)^#{1,6}\s+.*\b(Handback|Handoff|Completion\s+Report|Complete\b|Closed?\b|CLOSED|Closing|Audit\b|Verdict|Review\b|APPROVED|Status)\b"
)
_CANONICAL_DISPATCH_RE = re.compile(r"(?i)^Next:\s*dispatch\s+(.+?)\s*$")
_PROSE_NEXT_AGENT_RE = re.compile(
    r"(?i)^#{0,6}\s*Next(?:\s+Agent)?\s*[:\-→>]+\s*(.+?)\s*$"
)
_EPIC_CLOSE_RE = re.compile(
    r"(?i)(^|[^a-z])(close\s+(?:the\s+)?epic|close\s+this\s+epic|close\s+out\s+the\s+epic)([^a-z]|$)"
)
_LEDGER_CLOSE_RE = re.compile(
    r"(?i)(ledger\s+close|close\s+(?:the\s+)?ledger|ledger\s+gate|ledger\s+push|ledger\s+update|ledger\s+writeback)"
)
_LEDGER_AMBIGUOUS_RE = re.compile(r"(?i)\b(ledger|push(?:\s+to\s+origin)?)\b")
_MISROUTE_RE = re.compile(r"(?i)(misroute|clarif)")
_SHORT_SHA_RE = re.compile(r"(?i)\b[0-9a-f]{7,40}\b")
_SHA_FULL_RE = re.compile(r"(?i)^[0-9a-f]{7,40}$")
LEGACY_FRONTMATTER_WARNING = (
    "Deprecated HANDOFF routing format: missing YAML routing frontmatter; "
    "falling back to legacy prose routing."
)
_ALLOWED_CLOSE_TYPES = {"story", "epic"}
_FRONTMATTER_KNOWN_KEYS = {
    "next_agent",
    "reason",
    "epic_id",
    "story_id",
    "story_title",
    "remaining_stories",
    "scope_sha",
    "close_type",
    "prior_sha",
    "producer",
}
_FRONTMATTER_REPAIRABLE_SCALAR_KEYS = {
    "reason",
    "story_title",
}
_FRONTMATTER_SCALAR_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")
_FRONTMATTER_LIST_ITEM_RE = re.compile(r"^\s+-\s*(.+?)\s*$")


@dataclass(frozen=True)
class RoutingDecision:
    route: RouteName
    confidence: Confidence
    source: str
    reasoning: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Signal:
    route: RouteName
    confidence: Confidence
    source: str
    reasoning: str
    warnings: tuple[str, ...] = ()
    priority: int = 0


class HandoffRouting(BaseModel):
    model_config = ConfigDict(frozen=True)

    next_agent: str | None = None
    reason: str | None = None
    scope_sha: str | None = None
    close_type: str | None = None
    prior_sha: str | None = None
    producer: str | None = None
    epic_id: str | None = None
    story_id: str | None = None
    story_title: str | None = None
    remaining_stories: tuple[str, ...] = ()

    @field_validator(
        "next_agent",
        "reason",
        "scope_sha",
        "close_type",
        "prior_sha",
        "producer",
        "epic_id",
        "story_id",
        "story_title",
        mode="before",
    )
    @classmethod
    def _coerce_optional_string(cls, value: object) -> str | None:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("remaining_stories", mode="before")
    @classmethod
    def _coerce_remaining_stories(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, list | tuple):
            return tuple(
                item_text for item in value if (item_text := str(item).strip())
            )
        item_text = str(value).strip()
        if not item_text:
            return ()
        return (item_text,)


@dataclass(frozen=True)
class HandoffFrontmatterRepair:
    content: str
    repaired: bool
    warnings: tuple[str, ...] = ()


class HandoffFrontmatterError(ValueError):
    """Raised when the required YAML frontmatter block exists but is invalid YAML."""


def parse_handoff_frontmatter(handoff_path: Path) -> HandoffRouting | None:
    return parse_handoff_frontmatter_text(read_dispatch_text(handoff_path))


def parse_handoff_frontmatter_text(handoff_content: str) -> HandoffRouting | None:
    frontmatter_text = _extract_frontmatter_text(handoff_content)
    if frontmatter_text is None:
        return None

    try:
        loaded = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise HandoffFrontmatterError("YAML frontmatter could not be parsed.") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise HandoffFrontmatterError("YAML frontmatter must be a mapping.")

    return HandoffRouting.model_validate(loaded)


def repair_handoff_frontmatter_text(handoff_content: str) -> HandoffFrontmatterRepair:
    try:
        parse_handoff_frontmatter_text(handoff_content)
    except HandoffFrontmatterError:
        pass
    else:
        return HandoffFrontmatterRepair(content=handoff_content, repaired=False)

    normalized_content = handoff_content.lstrip("\ufeff")
    lines = normalized_content.splitlines()
    if not lines or lines[0].strip() != "---":
        return HandoffFrontmatterRepair(content=handoff_content, repaired=False)

    end_index = _frontmatter_end_index(lines)
    if end_index is None:
        return HandoffFrontmatterRepair(content=handoff_content, repaired=False)

    recovered = _recover_frontmatter_mapping(lines[1:end_index])
    if recovered is None:
        return HandoffFrontmatterRepair(content=handoff_content, repaired=False)

    recovered_mapping, quoted_keys = recovered
    if not recovered_mapping:
        return HandoffFrontmatterRepair(content=handoff_content, repaired=False)

    repaired_frontmatter = yaml.safe_dump(
        recovered_mapping,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip()
    repaired_lines = ["---", *repaired_frontmatter.splitlines(), "---"]
    repaired_lines.extend(lines[end_index + 1 :])
    repaired_content = "\n".join(repaired_lines)
    if normalized_content.endswith("\n"):
        repaired_content += "\n"

    try:
        parse_handoff_frontmatter_text(repaired_content)
    except HandoffFrontmatterError:
        return HandoffFrontmatterRepair(content=handoff_content, repaired=False)

    return HandoffFrontmatterRepair(
        content=repaired_content,
        repaired=True,
        warnings=(_frontmatter_repair_warning(quoted_keys),),
    )


def route(
    handoff_content: str, project_state_content: str | None = None
) -> RoutingDecision:
    normalized_content = handoff_content.lstrip("\ufeff")
    try:
        frontmatter = parse_handoff_frontmatter_text(normalized_content)
    except HandoffFrontmatterError:
        return RoutingDecision(
            route="unknown",
            confidence="LOW",
            source="frontmatter.parse_error",
            reasoning="YAML routing frontmatter is present but could not be parsed.",
            warnings=[
                "Invalid HANDOFF routing frontmatter: YAML frontmatter could not be parsed."
            ],
        )

    if frontmatter is not None:
        return _route_from_frontmatter(frontmatter)

    return _with_legacy_warning(
        _route_legacy(
            normalized_content,
            project_state_content=project_state_content,
        )
    )


def _route_legacy(
    handoff_content: str,
    *,
    project_state_content: str | None = None,
) -> RoutingDecision:
    normalized_content = handoff_content.lstrip("\ufeff")
    if not normalized_content.strip():
        return RoutingDecision(
            route="unknown",
            confidence="LOW",
            source="no_signal",
            reasoning="No recognizable routing signal was found in HANDOFF content.",
            warnings=["HANDOFF content is empty or whitespace."],
        )

    lines = normalized_content.splitlines()
    candidates: list[_Signal] = []

    escalation_signal = _find_escalation(lines)
    if escalation_signal is not None:
        return _to_decision(escalation_signal)

    close_type_signal = _find_close_type(lines)
    if close_type_signal is not None:
        return _to_decision(close_type_signal)

    canonical_signal = _find_canonical_dispatch(lines, project_state_content)
    if canonical_signal is not None:
        candidates.append(canonical_signal)

    next_step_signal = _find_next_step(lines, project_state_content)
    if next_step_signal is not None:
        candidates.append(next_step_signal)

    task_assignment_signal = _find_task_assignment(lines)
    if task_assignment_signal is not None:
        candidates.append(task_assignment_signal)

    prose_signal = _find_prose_next_agent(lines, project_state_content)
    if prose_signal is not None:
        candidates.append(prose_signal)

    if not candidates:
        return RoutingDecision(
            route="unknown",
            confidence="LOW",
            source="no_signal",
            reasoning="No recognizable routing signal was found in HANDOFF content.",
            warnings=["No recognized routing signal found."],
        )

    candidates.sort(key=lambda candidate: candidate.priority, reverse=True)
    selected = candidates[0]
    confidence = selected.confidence
    warnings = list(selected.warnings)
    reasoning = selected.reasoning

    if (
        selected.source.startswith("next_step")
        and any(
            candidate.source == "task_assignment_block" for candidate in candidates[1:]
        )
        and not warnings
    ):
        if selected.source == "next_step_section":
            reasoning = "Next Step section overrides the Task Assignment block under router precedence."
        warnings.append(
            "Multiple routing signals found; applied precedence order Next Step > Task Assignment."
        )
        confidence = "LOW"

    return RoutingDecision(
        route=selected.route,
        confidence=confidence,
        source=selected.source,
        reasoning=reasoning,
        warnings=warnings,
    )


def _extract_frontmatter_text(handoff_content: str) -> str | None:
    normalized_content = handoff_content.lstrip("\ufeff")
    lines = normalized_content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])

    raise HandoffFrontmatterError("YAML frontmatter block is not closed.")


def _frontmatter_end_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return index
    return None


def _recover_frontmatter_mapping(
    frontmatter_lines: list[str],
) -> tuple[dict[str, object], tuple[str, ...]] | None:
    recovered: dict[str, object] = {}
    quoted_keys: list[str] = []
    index = 0
    while index < len(frontmatter_lines):
        line = frontmatter_lines[index].strip()
        if not line or line.startswith("#"):
            index += 1
            continue

        match = _FRONTMATTER_SCALAR_LINE_RE.match(line)
        if match is None:
            return None

        key = match.group(1)
        raw_value = (match.group(2) or "").strip()
        if key not in _FRONTMATTER_KNOWN_KEYS:
            return None

        if key == "remaining_stories" and not raw_value:
            items: list[str] = []
            index += 1
            while index < len(frontmatter_lines):
                item_match = _FRONTMATTER_LIST_ITEM_RE.match(frontmatter_lines[index])
                if item_match is None:
                    break
                items.append(str(_recover_frontmatter_scalar(item_match.group(1))))
                index += 1
            recovered[key] = items
            continue

        if _frontmatter_scalar_needs_quote(key, raw_value):
            quoted_keys.append(key)
            recovered[key] = raw_value
        else:
            recovered[key] = _recover_frontmatter_scalar(raw_value)
        index += 1

    return recovered, tuple(dict.fromkeys(quoted_keys))


def _recover_frontmatter_scalar(raw_value: str) -> str:
    if not raw_value:
        return ""
    try:
        loaded = yaml.safe_load(raw_value)
    except yaml.YAMLError:
        return raw_value
    if loaded is None:
        return ""
    if isinstance(loaded, str):
        return loaded
    return str(loaded)


def _frontmatter_scalar_needs_quote(key: str, raw_value: str) -> bool:
    if key not in _FRONTMATTER_REPAIRABLE_SCALAR_KEYS or not raw_value:
        return False
    if raw_value[0] in {"'", '"', "|", ">", "[", "{"}:
        return False
    return ": " in raw_value or " #" in raw_value


def _frontmatter_repair_warning(quoted_keys: tuple[str, ...]) -> str:
    if quoted_keys:
        return (
            "Auto-repaired HANDOFF YAML frontmatter by quoting "
            f"{', '.join(quoted_keys)}."
        )
    return "Auto-repaired HANDOFF YAML frontmatter with PyYAML canonical serialization."


def _route_from_frontmatter(frontmatter: HandoffRouting) -> RoutingDecision:
    errors = _frontmatter_route_errors(frontmatter)
    if errors:
        return RoutingDecision(
            route="unknown",
            confidence="LOW",
            source="frontmatter.invalid",
            reasoning="YAML routing frontmatter is present but invalid.",
            warnings=[f"Invalid HANDOFF routing frontmatter: {errors[0]}"],
        )

    next_agent = frontmatter.next_agent or ""
    route_name = normalize_next_agent_value(next_agent)
    if route_name is None:
        route_name = "unknown"
    warnings: list[str] = []
    if alias_warning := legacy_next_agent_warning(next_agent):
        warnings.append(alias_warning)
    warnings.extend(_frontmatter_metadata_warnings(frontmatter))

    return RoutingDecision(
        route=route_name,
        confidence="HIGH",
        source="frontmatter.next_agent",
        reasoning=_agent_reasoning(route_name, "frontmatter.next_agent"),
        warnings=warnings,
    )


def _frontmatter_route_errors(frontmatter: HandoffRouting) -> list[str]:
    errors: list[str] = []
    next_agent = frontmatter.next_agent
    close_type = frontmatter.close_type

    if not next_agent:
        errors.append("next_agent is required.")
    elif normalize_next_agent_value(next_agent) is None:
        errors.append(f"next_agent `{next_agent}` is not recognized.")

    if not frontmatter.reason:
        errors.append("reason is required.")

    if close_type is not None and close_type not in _ALLOWED_CLOSE_TYPES:
        errors.append(f"close_type `{close_type}` is not recognized.")

    if close_type is not None:
        if not frontmatter.scope_sha:
            errors.append("scope_sha is required when close_type is set.")
        elif _SHA_FULL_RE.fullmatch(frontmatter.scope_sha) is None:
            errors.append("scope_sha must be a 7-40 character git SHA.")

    normalized_next_agent = normalize_next_agent_value(next_agent)
    normalized_producer = normalize_next_agent_value(frontmatter.producer)
    epic_close_agents = {"auditor", "finalizer"}
    if close_type == "epic" and normalized_next_agent not in epic_close_agents:
        errors.append("close_type `epic` requires next_agent `auditor` or `finalizer`.")

    if normalized_next_agent == "finalizer" and close_type != "epic":
        errors.append(f"next_agent `{next_agent}` requires close_type `epic`.")

    if normalized_next_agent == "finalizer" and normalized_producer != "auditor":
        errors.append(f"next_agent `{next_agent}` requires producer `auditor`.")

    return errors


def _frontmatter_metadata_warnings(frontmatter: HandoffRouting) -> list[str]:
    warnings: list[str] = []

    if frontmatter.prior_sha and _SHA_FULL_RE.fullmatch(frontmatter.prior_sha) is None:
        warnings.append(
            "Invalid HANDOFF routing frontmatter metadata: prior_sha must be a 7-40 character git SHA."
        )

    return warnings


def _with_legacy_warning(decision: RoutingDecision) -> RoutingDecision:
    if LEGACY_FRONTMATTER_WARNING in decision.warnings:
        return decision
    return RoutingDecision(
        route=decision.route,
        confidence=decision.confidence,
        source=decision.source,
        reasoning=decision.reasoning,
        warnings=[*decision.warnings, LEGACY_FRONTMATTER_WARNING],
    )


def _to_decision(signal: _Signal) -> RoutingDecision:
    return RoutingDecision(
        route=signal.route,
        confidence=signal.confidence,
        source=signal.source,
        reasoning=signal.reasoning,
        warnings=list(signal.warnings),
    )


def _find_escalation(lines: list[str]) -> _Signal | None:
    for raw_line in lines:
        if re.match(r"(?i)^#{1,6}\s*(Escalation|User)\s*$", raw_line.strip()):
            return _Signal(
                route="user",
                confidence="HIGH",
                source="escalation_heading",
                reasoning="Escalation heading takes precedence over all other routing signals.",
                priority=100,
            )
    return None


def _find_close_type(lines: list[str]) -> _Signal | None:
    for raw_line in lines:
        line = raw_line.strip().replace("*", "").replace("`", "")
        if re.match(r"(?i)^Close\s+Type\s*:\s*EPIC[-\s]*CLOSE\s*$", line):
            return _Signal(
                route="finalizer",
                confidence="HIGH",
                source="close_type_metadata",
                reasoning="Close Type metadata declares the handoff ready for the epic-close flow.",
                priority=95,
            )
    return None


def _find_canonical_dispatch(
    lines: list[str], project_state_content: str | None
) -> _Signal | None:
    for raw_line in reversed(lines):
        line = raw_line.strip()

        if (
            re.match(r"(?i)^Next:\s*finalizer\s*$", line)
            or re.match(r"(?i)^Next:\s*epic[-\s]+close\s*$", line)
            or re.match(r"(?i)^Next:\s*close\s+epic\s*$", line)
        ):
            return _epic_close_signal("canonical_dispatch_line")

        match = _CANONICAL_DISPATCH_RE.match(line)
        if match is None:
            continue

        dispatch_target = match.group(1).strip()
        route_name, route_warnings = _normalize_agent(dispatch_target, dispatch_target)
        if route_name is None:
            continue

        if route_name == "auditor":
            return _auditor_signal(
                source="canonical_dispatch_line",
                action_text=dispatch_target,
                project_state_content=project_state_content,
                audit_reasoning="Canonical dispatch line routes work to auditor for audit.",
                misroute_reasoning="Canonical dispatch line routes work to auditor for misroute clarification.",
                epic_reasoning="Canonical dispatch line routes auditor work to the epic-close flow.",
                priority=80,
            )

        reasoning = _agent_reasoning(route_name, "canonical_dispatch_line")
        return _Signal(
            route=route_name,
            confidence="HIGH",
            source="canonical_dispatch_line",
            reasoning=reasoning,
            warnings=tuple(route_warnings),
            priority=80,
        )

    return None


def _find_next_step(
    lines: list[str], project_state_content: str | None
) -> _Signal | None:
    in_next_step = False
    next_step_depth = 0
    header_agent: RouteName | None = None
    header_warnings: tuple[str, ...] = ()
    header_suffix = ""
    body_lines: list[str] = []

    for raw_line in lines:
        trimmed = raw_line.strip()
        header_match = _NEXT_STEP_HEADER_RE.match(trimmed)
        if header_match is not None:
            in_next_step = True
            next_step_depth = len(header_match.group(1))
            header_suffix = header_match.group(2)
            header_agent, header_warnings = _extract_header_agent(header_suffix)
            continue

        if in_next_step:
            heading_match = _HEADING_RE.match(trimmed)
            if heading_match is not None:
                heading_depth = len(heading_match.group(1))
                if heading_depth <= next_step_depth:
                    in_next_step = False
                    continue

        if not in_next_step:
            continue

        if trimmed:
            body_lines.append(trimmed)

        heading_match = _HEADING_RE.match(trimmed)
        if heading_match is not None and len(heading_match.group(1)) > next_step_depth:
            heading_text = trimmed[len(heading_match.group(1)) :].strip()
            agent, action, agent_warnings = _extract_agent_and_action(heading_text, "")
            if agent is not None:
                return _build_next_step_signal(
                    source="next_step_subheading",
                    agent=agent,
                    action=action,
                    project_state_content=project_state_content,
                    warnings=agent_warnings,
                )

        bold_match = re.match(r"(?i)^[-*+]?\s*\*\*(.+?)\*\*\s*:?\s*(.*)$", trimmed)
        if bold_match is None:
            continue

        agent, action, agent_warnings = _extract_agent_and_action(
            bold_match.group(1), bold_match.group(2)
        )
        if agent is None:
            continue

        return _build_next_step_signal(
            source="next_step_section",
            agent=agent,
            action=action,
            project_state_content=project_state_content,
            warnings=agent_warnings,
        )

    if header_agent is None:
        return None

    full_context = header_suffix
    if body_lines:
        full_context = full_context + "\n" + "\n".join(body_lines)

    if header_agent == "auditor":
        return _auditor_signal(
            source="next_step_header",
            action_text=full_context,
            project_state_content=project_state_content,
            audit_reasoning="Next Step header names auditor as the target agent.",
            misroute_reasoning="Next Step header routes work to auditor for misroute clarification.",
            epic_reasoning="Next Step header routes auditor work to the epic-close flow.",
            priority=70,
        )

    reasoning = f"Next Step header names {header_agent} as the target agent."
    return _Signal(
        route=header_agent,
        confidence="HIGH",
        source="next_step_header",
        reasoning=reasoning,
        warnings=header_warnings,
        priority=70,
    )


def _find_task_assignment(lines: list[str]) -> _Signal | None:
    in_task_assignment = False
    task_assignment_depth = 0
    for raw_line in lines:
        trimmed = raw_line.strip()
        header_match = _TASK_ASSIGNMENT_HEADER_RE.match(trimmed)
        if header_match is not None:
            in_task_assignment = True
            task_assignment_depth = len(header_match.group(1))
            continue

        if in_task_assignment:
            heading_match = _HEADING_RE.match(trimmed)
            if (
                heading_match is not None
                and len(heading_match.group(1)) <= task_assignment_depth
            ):
                break

        if not in_task_assignment:
            continue

        agent_match = re.match(r"(?i)^\*\*Agent:\*\*\s*(.+)$", trimmed)
        if agent_match is None:
            continue

        route_name, route_warnings = _normalize_agent(
            agent_match.group(1), agent_match.group(1)
        )
        if route_name is None:
            return None

        return _Signal(
            route=route_name,
            confidence="HIGH",
            source="task_assignment_block",
            reasoning=f"Task Assignment block names {route_name} as the target agent.",
            warnings=tuple(route_warnings),
            priority=60,
        )

    return None


def _find_prose_next_agent(
    lines: list[str], project_state_content: str | None
) -> _Signal | None:
    for raw_line in reversed(lines):
        line = raw_line.strip()
        match = _PROSE_NEXT_AGENT_RE.match(line)
        if match is None:
            continue

        target_text = match.group(1).strip()
        if _is_explicit_epic_close(target_text) or _is_ledger_close_transition(
            target_text,
            project_state_content=project_state_content,
        ):
            return _epic_close_signal("next_agent_prose")

        agent, action, agent_warnings = _extract_agent_and_action(target_text, "")
        if agent is None:
            continue

        if agent == "auditor":
            return _auditor_signal(
                source="next_agent_prose",
                action_text=target_text,
                project_state_content=project_state_content,
                audit_reasoning="Prose Next Agent line routes work to auditor for audit.",
                misroute_reasoning="Prose Next Agent line routes work to auditor for misroute clarification.",
                epic_reasoning="Prose Next Agent line routes auditor work to the epic-close flow.",
                priority=50,
            )

        reasoning = _agent_reasoning(agent, "next_agent_prose")
        return _Signal(
            route=agent,
            confidence="HIGH",
            source="next_agent_prose",
            reasoning=reasoning,
            warnings=agent_warnings,
            priority=50,
        )

    return None


def _build_next_step_signal(
    *,
    source: str,
    agent: RouteName,
    action: str,
    project_state_content: str | None,
    warnings: tuple[str, ...],
) -> _Signal:
    if agent == "auditor":
        return _auditor_signal(
            source=source,
            action_text=action,
            project_state_content=project_state_content,
            audit_reasoning=f"{_source_label(source)} routes work to auditor for audit.",
            misroute_reasoning=f"{_source_label(source)} routes work to auditor for misroute clarification.",
            epic_reasoning=f"{_source_label(source)} routes auditor work to the epic-close flow.",
            priority=70 if source == "next_step_header" else 75,
            extra_warnings=warnings,
        )

    reasoning = _agent_reasoning(agent, source)
    return _Signal(
        route=agent,
        confidence="HIGH",
        source=source,
        reasoning=reasoning,
        warnings=warnings,
        priority=70 if source == "next_step_header" else 75,
    )


def _auditor_signal(
    *,
    source: str,
    action_text: str,
    project_state_content: str | None,
    audit_reasoning: str,
    misroute_reasoning: str,
    epic_reasoning: str,
    priority: int,
    extra_warnings: tuple[str, ...] = (),
) -> _Signal:
    if _MISROUTE_RE.search(action_text):
        return _Signal(
            route="validator",
            confidence="HIGH",
            source=source,
            reasoning=misroute_reasoning,
            warnings=extra_warnings,
            priority=priority,
        )

    if _LEDGER_AMBIGUOUS_RE.search(
        action_text
    ) and _project_state_has_remaining_stories(project_state_content):
        warnings = list(extra_warnings)
        warnings.append(
            "Finalizer wording is ambiguous; the project state shows remaining work, so finalizer was not selected."
        )
        return _Signal(
            route="auditor",
            confidence="MEDIUM",
            source=source,
            reasoning="Next Step section keeps auditor on the audit path because the ledger wording is not an explicit epic-close signal."
            if source == "next_step_section"
            else audit_reasoning,
            warnings=tuple(warnings),
            priority=priority,
        )

    if _is_explicit_epic_close(action_text) or _is_ledger_close_transition(
        action_text,
        project_state_content=project_state_content,
    ):
        return _Signal(
            route="finalizer",
            confidence="HIGH",
            source=source,
            reasoning=epic_reasoning,
            warnings=extra_warnings,
            priority=90,
        )

    return _Signal(
        route="auditor",
        confidence="HIGH",
        source=source,
        reasoning=audit_reasoning,
        warnings=extra_warnings,
        priority=priority,
    )


def _agent_reasoning(route_name: RouteName, source: str) -> str:
    label = _source_label(source)
    if route_name == "backend":
        return f"{label} routes work to backend."
    if route_name == "planner":
        return f"{label} routes work to planner."
    if route_name == "frontend":
        return f"{label} routes work to frontend."
    if route_name == "validator":
        return f"{label} routes work to validator."
    if route_name == "finalizer":
        return f"{label} routes work to the epic-close flow."
    return f"{label} names {route_name} as the target agent."


def _source_label(source: str) -> str:
    if source == "next_step_section":
        return "Next Step section"
    if source == "next_step_header":
        return "Next Step header"
    if source == "next_step_subheading":
        return "Next Step sub-heading"
    if source == "next_agent_prose":
        return "Prose Next Agent line"
    if source == "canonical_dispatch_line":
        return "Canonical dispatch line"
    if source == "task_assignment_block":
        return "Task Assignment block"
    if source == "frontmatter.next_agent":
        return "YAML routing frontmatter"
    return "Routing signal"


def _extract_header_agent(suffix: str) -> tuple[RouteName | None, tuple[str, ...]]:
    match = re.search(
        r"(?i)\b(for|to|:|→|->)\s+(.+?)\s*$",
        suffix,
    )
    if match is None:
        return None, ()

    route_name, warnings = _normalize_agent(match.group(2), suffix)
    return route_name, tuple(warnings)


def _extract_agent_and_action(
    inside_label: str, trailing_text: str
) -> tuple[RouteName | None, str, tuple[str, ...]]:
    route_name, warnings = _normalize_agent(
        inside_label,
        inside_label + " " + trailing_text,
    )
    if route_name is None:
        return None, "", ()

    action = (inside_label + " " + trailing_text).strip()
    return route_name, action.strip(), tuple(warnings)


def _normalize_agent(
    agent_text: str, action_text: str
) -> tuple[RouteName | None, list[str]]:
    role, warnings = normalize_agent_label(agent_text, context=action_text)
    return role, list(warnings)


def _is_reporting_document(lines: list[str]) -> bool:
    for raw_line in lines:
        if _REPORTING_HEADING_RE.match(raw_line.strip()):
            return True
        if raw_line.strip():
            break
    return False


def _project_state_has_remaining_stories(project_state_content: str | None) -> bool:
    if not project_state_content:
        return False
    return re.search(r"(?i)remaining stor(?:y|ies)", project_state_content) is not None


def _is_explicit_epic_close(text: str) -> bool:
    return _EPIC_CLOSE_RE.search(text) is not None


def _is_ledger_close_transition(
    text: str,
    *,
    project_state_content: str | None,
) -> bool:
    if _LEDGER_CLOSE_RE.search(text) is None:
        return False
    if re.search(r"(?i)\bepic\b", text) is not None:
        return True
    return not _project_state_has_remaining_stories(project_state_content)


def _epic_close_signal(source: str) -> _Signal:
    return _Signal(
        route="finalizer",
        confidence="HIGH",
        source=source,
        reasoning=_agent_reasoning("finalizer", source),
        priority=90,
    )
