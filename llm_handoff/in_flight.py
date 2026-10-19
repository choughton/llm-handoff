"""The in-flight record for an implementer dispatch.

The dispatcher writes this record right before it launches an implementer
(``backend`` or ``frontend``) under the work-claim gate. Once the dispatch
returns, it rewrites the record as a ``handback`` record holding the handoff
the implementer left and HEAD at that moment, and deletes it when the cycle
reaches the loop top. A ``dispatch`` record found at the next start therefore
means that implementer was interrupted mid-dispatch: a second Ctrl+C, a closed
window, or a crash. A ``handback`` record means the implementer returned but
the cycle was interrupted before it finished its commit, validation, and gates.
The record keeps what the dispatcher knew before the dispatch, so the start can
tell the implementer's partial edits from operator edits.

This module only builds and parses the record. It does no file I/O; the
orchestrator reads, writes, and deletes the file through its race-safe
helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

from llm_handoff.work_claim import carry_over_key

IN_FLIGHT_RECORD_VERSION = 1
InFlightRole = Literal["backend", "frontend"]
InFlightPhase = Literal["dispatch", "handback"]
_ROLES = frozenset({"backend", "frontend"})
_PHASES = frozenset({"dispatch", "handback"})


@dataclass(frozen=True)
class InFlightRecord:
    """What the dispatcher knew right before an implementer dispatch.

    ``handoff_content`` is the pre-dispatch ``HANDOFF.md`` text.
    ``assignment_content`` is set only when the dispatch worked from another
    assignment: an operator ``@backend`` or ``@frontend`` dispatch that
    continued the saved story from a handoff without a ``story_id``.
    ``story_key`` is the ``carry_over_key`` of ``assignment_content`` when it
    is set, else of ``handoff_content``. ``dirty_fingerprints`` are the
    operator-owned dirty files and their content fingerprints, and
    ``carried_over`` the files carried over from an earlier dispatch of the
    same story, both as ``PreDispatchState`` recorded them.

    A ``handback`` record also holds ``handback_content``, the ``HANDOFF.md``
    text when the dispatch returned, and ``handback_head_sha``, HEAD at that
    moment. A ``dispatch`` record has neither.
    """

    cycle: int
    role: InFlightRole
    story_key: str
    phase: InFlightPhase
    head_sha: str | None
    handoff_content: str
    dirty_fingerprints: tuple[tuple[str, str], ...]
    carried_over: tuple[str, ...]
    assignment_content: str | None = None
    handback_content: str | None = None
    handback_head_sha: str | None = None
    version: int = IN_FLIGHT_RECORD_VERSION

    @property
    def continues_saved_story(self) -> bool:
        """Whether the dispatch continued the saved story's rebuilt assignment."""

        return self.assignment_content is not None


def in_flight_record_payload(record: InFlightRecord) -> str:
    """The record as JSON with sorted keys."""

    payload = {
        "version": record.version,
        "cycle": record.cycle,
        "role": record.role,
        "story_key": record.story_key,
        "phase": record.phase,
        "head_sha": record.head_sha,
        "handoff_content": record.handoff_content,
        "assignment_content": record.assignment_content,
        "dirty_fingerprints": [list(pair) for pair in record.dirty_fingerprints],
        "carried_over": list(record.carried_over),
        "handback_content": record.handback_content,
        "handback_head_sha": record.handback_head_sha,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_in_flight_record(text: str) -> InFlightRecord | None:
    """Parse a record; None when it is malformed or does not hold together.

    Rejects invalid JSON, another ``version``, a ``role`` other than
    ``backend`` or ``frontend``, a ``phase`` other than ``dispatch`` or
    ``handback``, a field of the wrong type, and a ``story_key`` that is not
    the ``carry_over_key`` of ``assignment_content`` (when set) or
    ``handoff_content``. A ``handback`` record needs a string
    ``handback_content``; a ``dispatch`` record must not have one. A missing
    ``assignment_content``, ``handback_content``, or ``handback_head_sha``
    reads as None, so a record written before the ``handback`` phase existed
    still loads as a ``dispatch`` record. JSON nested too deeply to parse
    (``RecursionError``) is rejected like any other invalid JSON.
    """

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    if type(version) is not int or version != IN_FLIGHT_RECORD_VERSION:
        return None
    cycle = payload.get("cycle")
    if type(cycle) is not int:
        return None
    role = payload.get("role")
    if not isinstance(role, str) or role not in _ROLES:
        return None
    phase = payload.get("phase")
    if not isinstance(phase, str) or phase not in _PHASES:
        return None
    head_sha = payload.get("head_sha")
    if head_sha is not None and not isinstance(head_sha, str):
        return None
    handoff_content = payload.get("handoff_content")
    if not isinstance(handoff_content, str):
        return None
    assignment_content = payload.get("assignment_content")
    if assignment_content is not None and not isinstance(assignment_content, str):
        return None
    handback_content = payload.get("handback_content")
    if phase == "handback":
        if not isinstance(handback_content, str):
            return None
    elif handback_content is not None:
        return None
    handback_head_sha = payload.get("handback_head_sha")
    if handback_head_sha is not None and not isinstance(handback_head_sha, str):
        return None
    story_key = payload.get("story_key")
    if not isinstance(story_key, str) or not story_key:
        return None
    dirty_fingerprints = _string_pairs(payload.get("dirty_fingerprints"))
    if dirty_fingerprints is None:
        return None
    carried_over = _strings(payload.get("carried_over"))
    if carried_over is None:
        return None
    keyed_content = (
        assignment_content if assignment_content is not None else handoff_content
    )
    if story_key != carry_over_key(keyed_content):
        return None
    return InFlightRecord(
        version=version,
        cycle=cycle,
        role=role,  # type: ignore[arg-type]
        story_key=story_key,
        phase=phase,  # type: ignore[arg-type]
        head_sha=head_sha,
        handoff_content=handoff_content,
        dirty_fingerprints=dirty_fingerprints,
        carried_over=carried_over,
        assignment_content=assignment_content,
        handback_content=handback_content,
        handback_head_sha=handback_head_sha,
    )


def _string_pairs(value: object) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(value, list):
        return None
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            return None
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def _strings(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)
