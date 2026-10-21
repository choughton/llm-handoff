"""Process-wide signal that a chat answer took over the running role.

A chat answer that rewrites ``HANDOFF.md`` requests a takeover. Role and
audit reviewer processes launched through ``_run_command_streaming`` poll the
request and are stopped, process tree included, by raising ``ChatTakeover``,
but only while the loop has armed the window around a role dispatch. The
loop then validates and follows the chat's handoff. The chat answer worker
calls ``request_takeover_if_handoff_changed`` after each answer.

The request records only that the handoff's bytes changed, not who wrote
them. When the role returned during the window and the recorded bytes are
still on disk with a frontmatter ``producer`` naming that role, the loop
clears the request and treats the handoff as the role's own handback
(CHAT-35).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import stat
import threading


class ChatTakeover(BaseException):
    """The running role was stopped because a chat answer rewrote HANDOFF.md.

    It derives from ``BaseException``, so the fail-closed ``except
    Exception`` handlers (the audit fan-out's ``_guarded`` and the evidence
    dispatch in ``llm_handoff/audit_review.py``) let it through, as they do
    for ``KeyboardInterrupt``.
    """


class ChatTakeoverController:
    """Thread-safe record of a pending chat takeover request.

    A request only stops a process while the controller is armed. The loop
    arms it around the shared-branch role dispatch, so a request that lands
    while a validator, the normalizer, a hygiene repair, or the finalizer
    runs stays pending instead of stopping that process.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested = False
        self._handoff_sha: str | None = None
        self._armed = False

    def request(self, handoff_sha: str | None) -> None:
        with self._lock:
            self._requested = True
            self._handoff_sha = handoff_sha

    def requested(self) -> bool:
        with self._lock:
            return self._requested

    def handoff_sha(self) -> str | None:
        """Return the SHA from the latest request, or None when none is set."""
        with self._lock:
            return self._handoff_sha if self._requested else None

    def clear(self) -> None:
        """Drop the pending request. The armed state is left unchanged."""
        with self._lock:
            self._requested = False
            self._handoff_sha = None

    def arm(self) -> None:
        with self._lock:
            self._armed = True

    def disarm(self) -> None:
        with self._lock:
            self._armed = False

    def armed(self) -> bool:
        with self._lock:
            return self._armed

    def stop_requested(self) -> bool:
        """Return True only while a request is pending and the window is armed."""
        with self._lock:
            return self._requested and self._armed


CONTROLLER = ChatTakeoverController()


def request_takeover(handoff_sha: str | None) -> None:
    CONTROLLER.request(handoff_sha)


def takeover_requested() -> bool:
    return CONTROLLER.requested()


def takeover_handoff_sha() -> str | None:
    """Return the handoff SHA the pending request recorded, or None."""
    return CONTROLLER.handoff_sha()


def clear_takeover() -> None:
    CONTROLLER.clear()


def arm_takeover() -> None:
    CONTROLLER.arm()


def disarm_takeover() -> None:
    CONTROLLER.disarm()


def takeover_stop_requested() -> bool:
    return CONTROLLER.stop_requested()


def handoff_content_sha(handoff_path: Path) -> str | None:
    """Return the SHA-256 hex digest of the handoff's raw bytes.

    Returns None when the path is missing, is not a regular file, or cannot
    be read. It never raises.
    """
    try:
        if not stat.S_ISREG(handoff_path.stat().st_mode):
            return None
        data = handoff_path.read_bytes()
    except (OSError, ValueError):
        return None
    return hashlib.sha256(data).hexdigest()


def request_takeover_if_handoff_changed(
    *,
    handoff_path: Path,
    handoff_sha_before: str | None,
    controller: ChatTakeoverController | None = None,
) -> bool:
    """Request a takeover when the handoff's content changed.

    A deleted or unreadable handoff counts as changed. An unchanged handoff
    never requests a takeover, whatever other files changed.
    """
    handoff_sha_after = handoff_content_sha(handoff_path)
    if handoff_sha_after == handoff_sha_before:
        return False
    target = CONTROLLER if controller is None else controller
    target.request(handoff_sha_after)
    return True
