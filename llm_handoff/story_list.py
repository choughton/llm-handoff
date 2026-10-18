"""Checks that a handoff rewrite keeps the epic's planned stories.

`remaining_stories` in HANDOFF.md frontmatter is the live story list for the
epic. A role that rewrites the handoff can drop entries without saying so, and
in dispatcher commit mode a story that exists only in the handoff is then
gone. These checks compare a dispatch's rewrite with the handoff it started
from. Stories are matched by their leading ID token, so `S2 Add login` and
`S2: Add login form` are the same story.
"""

from __future__ import annotations

import re

from llm_handoff.router import (
    HandoffFrontmatterError,
    HandoffRouting,
    parse_handoff_frontmatter_text,
)


STORIES_DROPPED_ERROR = "remaining_stories_dropped"
STORY_NOT_IN_PROJECT_STATE_WARNING = "remaining_story_not_in_project_state"

_DROPPED_STORIES_HEADING_RE = re.compile(r"(?im)^(#{2,6})\s+Dropped Stories\s*$")
_HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
# Wrapping and separator characters around an ID token, as in `**S2**:`.
_TOKEN_STRIP = "`*_\"'[](),:;.!?"
_REASON_LEAD_STRIP = " \t`*_:;,.-\u2013\u2014"


def story_id_token(entry: str | None) -> str | None:
    """The leading ID token of a story entry, such as `S2` in `S2 Add login`."""

    if entry is None:
        return None
    for raw in entry.split():
        token = raw.strip(_TOKEN_STRIP)
        if token:
            return token
    return None


def _story_key(entry: str | None) -> str | None:
    token = story_id_token(entry)
    return token.casefold() if token is not None else None


def dropped_story_reasons(handoff_content: str) -> dict[str, str]:
    """Map each story key in `## Dropped Stories` to its reason text.

    Each entry is a bullet that starts with the story ID, for example
    `- S2: Superseded by S5.` An entry without text after the ID maps to an
    empty reason.
    """

    match = _DROPPED_STORIES_HEADING_RE.search(handoff_content)
    if match is None:
        return {}
    level = len(match.group(1))
    start = match.end()
    end = len(handoff_content)
    for heading in _HEADING_RE.finditer(handoff_content, start):
        if len(heading.group(1)) <= level:
            end = heading.start()
            break
    reasons: dict[str, str] = {}
    for line in handoff_content[start:end].splitlines():
        bullet = _BULLET_RE.match(line)
        if bullet is None:
            continue
        text = bullet.group(1).strip()
        token = story_id_token(text)
        if token is None:
            continue
        key = token.casefold()
        _before, _sep, after = text.partition(token)
        reason = after.lstrip(_REASON_LEAD_STRIP).strip()
        if reason or key not in reasons:
            reasons[key] = reason
    return reasons


def _parse(content: str | None) -> HandoffRouting | None:
    if not content:
        return None
    try:
        return parse_handoff_frontmatter_text(content.lstrip("\ufeff"))
    except HandoffFrontmatterError:
        return None


def _same_epic(prior: HandoffRouting, current: HandoffRouting) -> bool:
    # Only two named, different epics end the comparison. A rewrite that
    # leaves `epic_id` out does not start a new epic.
    if not prior.epic_id or not current.epic_id:
        return True
    return prior.epic_id.casefold() == current.epic_id.casefold()


def story_list_errors(prior_content: str | None, current_content: str) -> list[str]:
    """Errors for `remaining_stories` entries the rewrite dropped silently.

    An entry of the prior handoff may leave the list only when it is the
    prior or the current `story_id` (the story just closed or now assigned),
    or when the rewrite lists it under `## Dropped Stories` with a reason.
    """

    prior = _parse(prior_content)
    current = _parse(current_content)
    if prior is None or current is None or not prior.remaining_stories:
        return []
    if not _same_epic(prior, current):
        return []
    kept = {
        key
        for key in (
            *(_story_key(entry) for entry in current.remaining_stories),
            _story_key(prior.story_id),
            _story_key(current.story_id),
        )
        if key is not None
    }
    reasons = dropped_story_reasons(current_content.lstrip("\ufeff"))
    unexplained: list[str] = []
    missing_reason: list[str] = []
    for entry in prior.remaining_stories:
        key = _story_key(entry)
        if key is None or key in kept:
            continue
        if key not in reasons:
            unexplained.append(entry)
        elif not reasons[key]:
            missing_reason.append(entry)
    if not unexplained and not missing_reason:
        return []
    parts: list[str] = []
    if unexplained:
        parts.append(
            "the rewrite removed "
            + "; ".join(f"`{entry}`" for entry in unexplained)
            + " from remaining_stories"
        )
    if missing_reason:
        parts.append(
            "## Dropped Stories lists "
            + "; ".join(f"`{entry}`" for entry in missing_reason)
            + " without a reason"
        )
    return [
        f"{STORIES_DROPPED_ERROR}: "
        + ", and ".join(parts)
        + ". Keep every story that is not the one just closed in "
        "remaining_stories, or list it under `## Dropped Stories` as "
        "`- <story id>: <reason>`."
    ]


def project_state_story_warnings(
    current_content: str,
    project_state_content: str | None,
) -> list[str]:
    """Warnings for `remaining_stories` IDs the project state file never names."""

    if project_state_content is None:
        return []
    current = _parse(current_content)
    if current is None:
        return []
    missing: list[str] = []
    for entry in current.remaining_stories:
        token = story_id_token(entry)
        if token is None:
            continue
        pattern = rf"(?<![\w-]){re.escape(token)}(?![\w-])"
        if re.search(pattern, project_state_content, re.IGNORECASE) is None:
            missing.append(entry)
    if not missing:
        return []
    return [
        f"{STORY_NOT_IN_PROJECT_STATE_WARNING}: remaining_stories names "
        + "; ".join(f"`{entry}`" for entry in missing)
        + ", which the project state file does not list. A story kept only "
        "in the handoff is lost if a later rewrite drops it; record it in "
        "the project state file through a committed Work Packet or the "
        "finalizer."
    ]


__all__ = [
    "STORIES_DROPPED_ERROR",
    "STORY_NOT_IN_PROJECT_STATE_WARNING",
    "dropped_story_reasons",
    "project_state_story_warnings",
    "story_id_token",
    "story_list_errors",
]
