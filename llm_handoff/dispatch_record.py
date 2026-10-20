"""Saved record of the run's role dispatches under ``logs/dispatch/``.

Each entry names the role, the story it worked, the provider that ran it, and
the session id the provider reported (CHAT-14), so a later chat answer can
reach the session that did the work. Nothing in the loop writes the record yet;
CHAT-15 wires the orchestrator to it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm_handoff.config import DISPATCH_RECORD_STATE_PATH

__all__ = [
    "DispatchEntry",
    "append_dispatch_entry",
    "clear_dispatch_record",
    "dispatch_record_path",
    "load_dispatch_record",
    "record_dispatch",
]

_RECORD_VERSION = 1


@dataclass(frozen=True)
class DispatchEntry:
    """One role dispatch in the current run."""

    role: str
    story_id: str | None
    provider: str | None
    session_id: str | None
    recorded_at: str


def dispatch_record_path(repo_root: Path) -> Path:
    return repo_root / DISPATCH_RECORD_STATE_PATH


def load_dispatch_record(path: Path) -> list[DispatchEntry]:
    """Return the saved entries in saved order; [] when the file is unusable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    version = payload.get("version")
    if type(version) is not int or version != _RECORD_VERSION:
        return []
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return []
    entries: list[DispatchEntry] = []
    for raw in raw_entries:
        entry = _parse_entry(raw)
        if entry is not None:
            entries.append(entry)
    return entries


def _optional_str(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _parse_entry(raw: Any) -> DispatchEntry | None:
    if not isinstance(raw, dict):
        return None
    role = raw.get("role")
    story_id = raw.get("story_id")
    provider = raw.get("provider")
    session_id = raw.get("session_id")
    recorded_at = raw.get("recorded_at")
    if not isinstance(role, str) or not role.strip():
        return None
    if not (
        _optional_str(story_id) and _optional_str(provider) and _optional_str(session_id)
    ):
        return None
    if not isinstance(recorded_at, str):
        return None
    return DispatchEntry(
        role=role,
        story_id=story_id,
        provider=provider,
        session_id=session_id,
        recorded_at=recorded_at,
    )


def append_dispatch_entry(path: Path, entry: DispatchEntry) -> None:
    """Append one entry after the saved ones. ``OSError`` propagates."""
    combined = [*load_dispatch_record(path), entry]
    payload = {
        "version": _RECORD_VERSION,
        "entries": [
            {
                "role": item.role,
                "story_id": item.story_id,
                "provider": item.provider,
                "session_id": item.session_id,
                "recorded_at": item.recorded_at,
            }
            for item in combined
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates a new randomly named file with O_CREAT | O_EXCL, so a
    # link planted at a predictable temporary name is never opened or followed.
    file_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
        os.replace(temporary_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary_path.unlink()
        raise


def clear_dispatch_record(path: Path) -> None:
    """Delete the saved record; a missing file is not an error."""
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def record_dispatch(
    path: Path,
    *,
    role: str,
    story_id: str | None,
    provider: str | None,
    session_id: str | None,
    now: Callable[[], datetime] | None = None,
) -> DispatchEntry:
    """Append one dispatch recorded now and return the saved entry.

    A blank ``session_id`` is stored as None. ``OSError`` from the write
    propagates.
    """

    clock = now if now is not None else _utc_now
    recorded_at = clock().astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(session_id, str) and not session_id.strip():
        session_id = None
    entry = DispatchEntry(
        role=role,
        story_id=story_id,
        provider=provider,
        session_id=session_id,
        recorded_at=recorded_at,
    )
    append_dispatch_entry(path, entry)
    return entry
