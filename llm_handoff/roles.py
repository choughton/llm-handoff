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

LEGACY_NEXT_AGENT_ALIASES: dict[str, DispatchRole] = {
    "claude-audit": "auditor",
    "claude-ledger": "finalizer",
    "codex": "backend",
    "gemini-pe": "planner",
    "gemini-frontend": "frontend",
    "manual frontend": "frontend",
}

LEGACY_PROVIDER_LABEL_ALIASES: dict[str, DispatchRole] = {
    "claude code": "auditor",
    "claudecode": "auditor",
    "codex": "backend",
    "gemini": "planner",
    "gemini pe": "planner",
    "gemini-pe": "planner",
    "gemini frontend": "frontend",
    "gemini-frontend": "frontend",
    "manual frontend": "frontend",
    "manual frontend gui": "frontend",
}

_FRONTEND_HINT_RE = re.compile(r"(?i)(frontend|ui|react|tailwind)")


def normalize_next_agent_value(value: str | None) -> DispatchRole | None:
    if value is None:
        return None
    normalized = _normalize_label(value)
    if normalized in CANONICAL_NEXT_AGENT_SET:
        return normalized  # type: ignore[return-value]
    return LEGACY_NEXT_AGENT_ALIASES.get(normalized)


def is_legacy_next_agent_alias(value: str | None) -> bool:
    if value is None:
        return False
    return _normalize_label(value) in LEGACY_NEXT_AGENT_ALIASES


def legacy_next_agent_warning(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_label(value)
    role = LEGACY_NEXT_AGENT_ALIASES.get(normalized)
    if role is None:
        return None
    if normalized.startswith("manual frontend"):
        return "Legacy manual frontend reference normalized to frontend."
    return f"Deprecated provider-specific next_agent alias normalized to {role}."


def normalize_agent_label(
    label: str,
    *,
    context: str = "",
) -> tuple[DispatchRole | None, tuple[str, ...]]:
    normalized = _normalize_label(label)
    warnings: list[str] = []

    if "misroute" in normalized or "clarif" in normalized:
        return "validator", ()
    if normalized in CANONICAL_NEXT_AGENT_SET:
        return normalized, ()
    for role in CANONICAL_NEXT_AGENT_SET:
        if normalized.startswith(f"{role} "):
            return role, ()
    if normalized in {"audit", "reviewer"}:
        return "auditor", ()
    if normalized in {"handoff-validator"}:
        return "validator", ()
    if normalized in {"ledger", "ledger-updater", "epic-close"}:
        return "finalizer", ()

    if normalized in LEGACY_PROVIDER_LABEL_ALIASES:
        role = LEGACY_PROVIDER_LABEL_ALIASES[normalized]
        if normalized == "gemini" and _FRONTEND_HINT_RE.search(context):
            role = "frontend"
        if normalized == "manual frontend" or normalized == "manual frontend gui":
            warnings.append("Legacy manual frontend reference normalized to frontend.")
        return role, tuple(warnings)

    if "claude" in normalized:
        return "auditor", ()
    if "codex" in normalized:
        return "backend", ()
    if "manual frontend" in normalized:
        return "frontend", ("Legacy manual frontend reference normalized to frontend.",)
    if "gemini-frontend" in normalized or "gemini frontend" in normalized:
        return "frontend", ()
    if "gemini-pe" in normalized or "gemini pe" in normalized:
        return "planner", ()
    if "gemini" in normalized:
        if _FRONTEND_HINT_RE.search(context):
            return "frontend", ()
        return "planner", ()
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
