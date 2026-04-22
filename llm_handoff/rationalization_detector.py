from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml

from llm_handoff.agent_types import COMPLETION_STATUSES, HandoffStatus


@dataclass(frozen=True)
class Match:
    role: str
    phrase: str
    line_number: int
    pattern: str
    failure_mode: str


_PATTERN_PATH = Path(__file__).with_name("rationalization_patterns.yaml")


def detect(role: str, body: str, status: HandoffStatus) -> list[Match]:
    if status not in COMPLETION_STATUSES:
        return []

    role_key = _role_key(role)
    patterns = _PATTERNS.get(role_key, ())
    if not patterns:
        return []

    matches: list[Match] = []
    for line_number, line in enumerate(body.splitlines(), start=1):
        for entry in patterns:
            match = entry["compiled"].search(line)
            if match is None:
                continue
            matches.append(
                Match(
                    role=role_key,
                    phrase=match.group(0),
                    line_number=line_number,
                    pattern=entry["pattern"],
                    failure_mode=entry["failure_mode"],
                )
            )
    return matches


def _role_key(role: str) -> str:
    normalized = role.strip().lower()
    if "frontend" in normalized:
        return "frontend"
    if "backend" in normalized:
        return "backend"
    if "planner" in normalized:
        return "planner"
    if "auditor" in normalized or "validator" in normalized:
        return "auditor"
    return normalized


def _load_patterns() -> dict[str, tuple[dict[str, object], ...]]:
    raw = yaml.safe_load(_PATTERN_PATH.read_text(encoding="utf-8")) or {}
    loaded: dict[str, tuple[dict[str, object], ...]] = {}
    for role, entries in raw.items():
        role_entries: list[dict[str, object]] = []
        for entry in entries or []:
            pattern = str(entry["pattern"])
            role_entries.append(
                {
                    "pattern": pattern,
                    "failure_mode": str(entry.get("failure_mode", "")),
                    "compiled": re.compile(pattern, re.IGNORECASE),
                }
            )
        loaded[str(role)] = tuple(role_entries)
    return loaded


_PATTERNS = _load_patterns()
