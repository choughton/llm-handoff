"""Saved queue of operator chat messages under ``logs/dispatch/``.

Not wired in yet: nothing in the dispatcher reads or writes this queue besides
tests. CHAT-2 holds the loop on it, so a message sent while a role runs
survives a dispatcher stop and still holds the loop at the next start.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_handoff.config import CHAT_QUEUE_STATE_PATH

__all__ = [
    "ChatMessage",
    "append_chat_messages",
    "chat_queue_path",
    "clear_chat_messages",
    "load_chat_messages",
    "queued_message_notice",
]

_QUEUE_VERSION = 1


@dataclass(frozen=True)
class ChatMessage:
    """One operator chat message waiting for the loop to hold."""

    text: str
    role: str | None
    received_at: str


def chat_queue_path(repo_root: Path) -> Path:
    return repo_root / CHAT_QUEUE_STATE_PATH


def load_chat_messages(path: Path) -> list[ChatMessage]:
    """Return the saved messages in saved order; [] when the file is unusable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    version = payload.get("version")
    if type(version) is not int or version != _QUEUE_VERSION:
        return []
    entries = payload.get("messages")
    if not isinstance(entries, list):
        return []
    messages: list[ChatMessage] = []
    for entry in entries:
        message = _parse_entry(entry)
        if message is not None:
            messages.append(message)
    return messages


def _parse_entry(entry: Any) -> ChatMessage | None:
    if not isinstance(entry, dict):
        return None
    text = entry.get("text")
    role = entry.get("role")
    received_at = entry.get("received_at")
    if not isinstance(text, str) or not text.strip():
        return None
    if role is not None and not isinstance(role, str):
        return None
    if not isinstance(received_at, str):
        return None
    return ChatMessage(text=text, role=role, received_at=received_at)


def append_chat_messages(path: Path, messages: Sequence[ChatMessage]) -> None:
    """Append messages after the saved ones; an empty sequence writes nothing."""
    if not messages:
        return
    combined = [*load_chat_messages(path), *messages]
    payload = {
        "version": _QUEUE_VERSION,
        "messages": [
            {
                "text": message.text,
                "role": message.role,
                "received_at": message.received_at,
            }
            for message in combined
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def clear_chat_messages(path: Path) -> None:
    """Delete the saved queue; a missing file is not an error."""
    try:
        path.unlink()
    except FileNotFoundError:
        return


def queued_message_notice(role: str) -> str:
    return f"Question queued until after the current {role} run completes."
