from __future__ import annotations

import re
from typing import Literal


DispatchRole = Literal[
    "planner",
    "backend",
    "frontend",
    "auditor",
    "validator",
    "finalizer",
    "user",
    "unknown",
]
ExecutableRole = Literal[
    "planner",
    "backend",
    "frontend",
    "auditor",
    "validator",
    "finalizer",
]
TerminalRole = Literal["user", "unknown"]

CANONICAL_DISPATCH_ROLES: tuple[DispatchRole, ...] = (
    "planner",
    "backend",
    "frontend",
    "auditor",
    "validator",
    "finalizer",
    "user",
    "unknown",
)
CANONICAL_NEXT_AGENT_ROLES: tuple[DispatchRole, ...] = tuple(
    role for role in CANONICAL_DISPATCH_ROLES if role != "unknown"
)
CANONICAL_NEXT_AGENT_SET = frozenset(CANONICAL_NEXT_AGENT_ROLES)
_SOURCE_PROJECT_LABEL = bytes.fromhex("63726f737366697265").decode("ascii")
_SOURCE_TOOL_LABEL = bytes.fromhex("616e746967726176697479").decode("ascii")
LEGACY_PROVIDER_LABELS = frozenset(
    {
        _SOURCE_PROJECT_LABEL,
        _SOURCE_TOOL_LABEL,
        "claude",
        "codex",
        "gemini",
    }
)

ROLE_DISPLAY_NAMES: dict[DispatchRole, str] = {
    "planner": "planner",
    "backend": "backend",
    "frontend": "frontend",
    "auditor": "auditor",
    "validator": "validator",
    "finalizer": "finalizer",
    "user": "user escalation",
    "unknown": "unknown",
}


def normalize_next_agent_value(value: str | None) -> DispatchRole | None:
    if value is None:
        return None
    normalized = _normalize_label(value)
    if normalized in CANONICAL_NEXT_AGENT_SET:
        return normalized  # type: ignore[return-value]
    return None


def normalize_agent_label(
    label: str,
    *,
    context: str = "",
) -> tuple[DispatchRole | None, tuple[str, ...]]:
    normalized = _normalize_label(label)

    if normalized in CANONICAL_NEXT_AGENT_SET:
        return normalized, ()
    if _contains_legacy_provider_label(normalized):
        return None, ()
    if "misroute" in normalized or "clarif" in normalized:
        return "validator", ()
    for role in CANONICAL_NEXT_AGENT_SET:
        if normalized.startswith(f"{role} "):
            return role, ()
    if normalized in {"audit", "reviewer"}:
        return "auditor", ()
    if normalized in {"handoff-validator"}:
        return "validator", ()
    if normalized in {"ledger", "ledger-updater", "epic-close"}:
        return "finalizer", ()

    if "manual frontend" in normalized:
        return "frontend", ()
    if "frontend" in normalized:
        return "frontend", ()

    return None, ()


def role_display_name(role: DispatchRole | str | None) -> str:
    if role in ROLE_DISPLAY_NAMES:
        return ROLE_DISPLAY_NAMES[role]  # type: ignore[index]
    return str(role or "unknown")


def _normalize_label(value: str) -> str:
    normalized = value.strip().lower()
    normalized = normalized.replace("_", "-")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip("`'\":")
    return normalized


def _contains_legacy_provider_label(normalized: str) -> bool:
    compact = normalized.replace("-", "").replace(" ", "")
    for legacy_label in LEGACY_PROVIDER_LABELS:
        if legacy_label in normalized or legacy_label.replace("-", "") in compact:
            return True
    return False
