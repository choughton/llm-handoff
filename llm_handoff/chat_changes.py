"""Saved record of the files chat side-sessions changed, under ``logs/dispatch/``.

Each entry names the role and story active when the operator's message was
answered, when the answer was recorded, the SHA-256 of the message (the
message text itself is never written), and each file the side session's edit
events named with that file's working-tree fingerprint at record time.

``chat_owned_paths`` returns the recorded files that still match their most
recent recorded fingerprint, so a file a role changed again after the chat
recorded it stays the role's and goes through the normal scope check. A
fingerprint covers what Git tracks: the file type, the executable bit, and the
content bytes or, for a symlink, the link target.

The record file is writable by the active role, so it is not trusted. Each
saved entry carries an HMAC-SHA256 tag keyed by a secret that lives only in
the dispatcher process's memory and chained to the previous accepted entry's
tag. ``load_chat_changes`` skips an entry whose tag does not verify, so an
entry a role forged or edited, or one saved by an earlier dispatcher process,
is never returned and its paths stay the role's. Dropping or reordering
entries can only hide chat changes or bring back an earlier state the chat
itself recorded.

``HANDOFF.md`` changes are not recorded here; CHAT-19 owns them.

Nothing in the loop calls this module yet.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_handoff.config import CHAT_CHANGES_STATE_PATH
from llm_handoff.work_claim import normalize_repo_path, working_tree_fingerprint

if TYPE_CHECKING:
    from llm_handoff.chat_side_session import ChatSessionResult

__all__ = [
    "ChatChange",
    "ChatChangeEntry",
    "chat_changes_path",
    "chat_owned_paths",
    "clear_chat_changes",
    "load_chat_changes",
    "record_chat_changes",
]

_RECORD_VERSION = 1
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# Generated per dispatcher process and never written anywhere, so a role
# process cannot compute a valid tag for an entry it writes itself.
_PROCESS_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class ChatChange:
    """One file a chat side session changed, fingerprinted at record time."""

    path: str
    fingerprint: str


@dataclass(frozen=True)
class ChatChangeEntry:
    """The files one chat answer changed."""

    role: str
    story_id: str | None
    recorded_at: str
    message_sha256: str
    changes: tuple[ChatChange, ...]


def chat_changes_path(repo_root: Path) -> Path:
    return repo_root / CHAT_CHANGES_STATE_PATH


def _entry_mac(entry: ChatChangeEntry, previous_mac: str) -> str:
    """HMAC tag over one entry's fields, chained to the previous entry's tag."""
    canonical = json.dumps(
        {
            "role": entry.role,
            "story_id": entry.story_id,
            "recorded_at": entry.recorded_at,
            "message_sha256": entry.message_sha256,
            "changes": [[change.path, change.fingerprint] for change in entry.changes],
            "previous": previous_mac,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hmac.new(
        _PROCESS_KEY, canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def load_chat_changes(path: Path) -> list[ChatChangeEntry]:
    """Return the saved entries in saved order; [] when the file is unusable.

    Malformed entries and entries whose HMAC tag does not verify are skipped.
    """
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
    entries: list[ChatChangeEntry] = []
    previous_mac = ""
    for raw in raw_entries:
        entry = _parse_entry(raw)
        if entry is None:
            continue
        mac = raw.get("mac")
        # Check the format first: a JSON string can hold an unpaired
        # surrogate, which cannot be encoded, so only a validated ASCII hex
        # tag reaches the comparison.
        if not isinstance(mac, str) or not _SHA256_HEX.fullmatch(mac):
            continue
        if not hmac.compare_digest(
            mac.encode("ascii"), _entry_mac(entry, previous_mac).encode("ascii")
        ):
            continue
        entries.append(entry)
        previous_mac = mac
    return entries


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _parse_change(raw: Any) -> ChatChange | None:
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    fingerprint = raw.get("fingerprint")
    if not (_non_empty_str(path) and _non_empty_str(fingerprint)):
        return None
    return ChatChange(path=path, fingerprint=fingerprint)


def _parse_entry(raw: Any) -> ChatChangeEntry | None:
    if not isinstance(raw, dict):
        return None
    role = raw.get("role")
    story_id = raw.get("story_id")
    recorded_at = raw.get("recorded_at")
    message_sha256 = raw.get("message_sha256")
    raw_changes = raw.get("changes")
    if not isinstance(role, str) or not role.strip():
        return None
    if story_id is not None and not isinstance(story_id, str):
        return None
    if not isinstance(recorded_at, str):
        return None
    if not isinstance(message_sha256, str) or not _SHA256_HEX.fullmatch(message_sha256):
        return None
    if not isinstance(raw_changes, list):
        return None
    changes: list[ChatChange] = []
    for raw_change in raw_changes:
        change = _parse_change(raw_change)
        if change is None:
            return None
        changes.append(change)
    return ChatChangeEntry(
        role=role,
        story_id=story_id,
        recorded_at=recorded_at,
        message_sha256=message_sha256,
        changes=tuple(changes),
    )


def _write_entries(path: Path, entries: list[ChatChangeEntry]) -> None:
    saved: list[dict[str, Any]] = []
    previous_mac = ""
    for item in entries:
        mac = _entry_mac(item, previous_mac)
        saved.append(
            {
                "role": item.role,
                "story_id": item.story_id,
                "recorded_at": item.recorded_at,
                "message_sha256": item.message_sha256,
                "changes": [
                    {"path": change.path, "fingerprint": change.fingerprint}
                    for change in item.changes
                ],
                "mac": mac,
            }
        )
        previous_mac = mac
    payload = {"version": _RECORD_VERSION, "entries": saved}
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


def clear_chat_changes(path: Path) -> None:
    """Delete the saved record; a missing file is not an error."""
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _repo_relative_handoff(repo_root: Path, handoff_path: Path | str) -> str:
    candidate = Path(handoff_path)
    if candidate.is_absolute():
        with contextlib.suppress(ValueError):
            candidate = candidate.relative_to(repo_root)
    return normalize_repo_path(candidate.as_posix())


def _fingerprint(repo_root: Path, path: str) -> str:
    """Fingerprint what Git tracks for a path: type, executable bit, content.

    A regular file is ``file:<mode>:<work_claim.working_tree_fingerprint>``
    with Git's ``100644`` or ``100755`` mode, and a symlink is
    ``symlink:<SHA-256 of its target>`` (the link is not followed). Absent,
    directory, and unreadable paths keep the ``work_claim`` markers.
    """

    full_path = repo_root / path
    try:
        info = os.lstat(full_path)
    except FileNotFoundError:
        return "<absent>"
    except OSError:
        return "<unreadable>"
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(full_path)
        except OSError:
            return "<unreadable>"
        return "symlink:" + hashlib.sha256(os.fsencode(target)).hexdigest()
    if stat.S_ISDIR(info.st_mode):
        return "<directory>"
    if not stat.S_ISREG(info.st_mode):
        return "<special>"
    content = working_tree_fingerprint(repo_root, path)
    if not _SHA256_HEX.fullmatch(content):
        return content
    mode = "100755" if info.st_mode & 0o111 else "100644"
    return f"file:{mode}:{content}"


def record_chat_changes(
    path: Path,
    *,
    repo_root: Path,
    result: ChatSessionResult,
    message: str,
    role: str,
    story_id: str | None,
    handoff_path: Path | str,
    now: Callable[[], datetime] | None = None,
) -> ChatChangeEntry | None:
    """Append the files one chat answer changed and return the saved entry.

    The handoff path and duplicates are dropped; None is returned and nothing
    is written when no path is left. A failed ``result`` is recorded the same
    way, since its edits are on disk. ``OSError`` from the write propagates.
    """

    handoff = _repo_relative_handoff(repo_root, handoff_path)
    paths: list[str] = []
    for raw_path in result.edited_paths:
        normalized = normalize_repo_path(raw_path)
        if not normalized or normalized == handoff or normalized in paths:
            continue
        paths.append(normalized)
    if not paths:
        return None

    clock = now if now is not None else _utc_now
    recorded_at = clock().astimezone(timezone.utc).isoformat(timespec="seconds")
    entry = ChatChangeEntry(
        role=role,
        story_id=story_id,
        recorded_at=recorded_at,
        message_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        changes=tuple(
            ChatChange(path=item, fingerprint=_fingerprint(repo_root, item))
            for item in paths
        ),
    )
    _write_entries(path, [*load_chat_changes(path), entry])
    return entry


def chat_owned_paths(
    repo_root: Path, entries: Iterable[ChatChangeEntry]
) -> tuple[str, ...]:
    """Return recorded paths whose file still matches the latest recorded state.

    Paths keep first-recorded order. A later entry for the same path replaces
    the earlier fingerprint. Pass entries from ``load_chat_changes`` so only
    authenticated entries are considered.
    """

    latest: dict[str, str] = {}
    for entry in entries:
        for change in entry.changes:
            latest[change.path] = change.fingerprint
    return tuple(
        item
        for item, fingerprint in latest.items()
        if _fingerprint(repo_root, item) == fingerprint
    )
