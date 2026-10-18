"""Checks that a handoff rewrite keeps the epic's planned stories.

`remaining_stories` in HANDOFF.md frontmatter is the live story list for the
epic. A role that rewrites the handoff can drop entries without saying so, and
in dispatcher commit mode a story that exists only in the handoff is then
gone. These checks compare a dispatch's rewrite with the handoff it started
from. Stories are matched by their leading ID token, so `S2 Add login` and
`S2: Add login form` are the same story. A leading token counts as an ID only
when it contains a digit; an entry without one, such as `Add login`, matches
by its full text instead, so two titles that share a first word stay distinct.
"""

from __future__ import annotations

from collections import Counter
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
    """The leading ID token of a story entry, such as `S2` in `S2 Add login`.

    `None` when the first word has no digit, as in the title-only `Add login`.
    """

    if entry is None:
        return None
    for raw in entry.split():
        token = raw.strip(_TOKEN_STRIP)
        if token:
            return token if any(char.isdigit() for char in token) else None
    return None


def _normalized_title(text: str) -> str:
    words = (word.strip(_TOKEN_STRIP) for word in text.casefold().split())
    return " ".join(word for word in words if word)


def _story_key(entry: str | None) -> str | None:
    if entry is None:
        return None
    token = story_id_token(entry)
    if token is not None:
        return token.casefold()
    # Title-only entries match by their whole text; the prefix keeps a title
    # from colliding with an ID.
    title = _normalized_title(entry)
    return f"title:{title}" if title else None


def _dropped_story_bullets(handoff_content: str) -> list[str]:
    match = _DROPPED_STORIES_HEADING_RE.search(handoff_content)
    if match is None:
        return []
    level = len(match.group(1))
    start = match.end()
    end = len(handoff_content)
    for heading in _HEADING_RE.finditer(handoff_content, start):
        if len(heading.group(1)) <= level:
            end = heading.start()
            break
    bullets: list[str] = []
    for line in handoff_content[start:end].splitlines():
        bullet = _BULLET_RE.match(line)
        if bullet is not None and bullet.group(1).strip():
            bullets.append(bullet.group(1).strip())
    return bullets


def _title_drop_reason(title_key: str, bullets: list[str]) -> str | None:
    """The reason a `## Dropped Stories` bullet gives for a title-only story.

    The bullet starts with the story's full title, then the reason, as in
    `- Add billing: Moved to the next epic.`
    """

    title = title_key.removeprefix("title:")
    found: str | None = None
    for text in bullets:
        normalized = _normalized_title(text)
        if normalized != title and not normalized.startswith(f"{title} "):
            continue
        reason = normalized[len(title) :].strip()
        if reason or found is None:
            found = reason
    return found


def dropped_story_reasons(handoff_content: str) -> dict[str, str]:
    """Map each story ID in `## Dropped Stories` to its reason text.

    Each entry is a bullet that starts with the story ID, for example
    `- S2: Superseded by S5.` An entry without text after the ID maps to an
    empty reason. Bullets for title-only stories are matched separately.
    """

    reasons: dict[str, str] = {}
    for text in _dropped_story_bullets(handoff_content):
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
    exempt = {
        key
        for key in (_story_key(prior.story_id), _story_key(current.story_id))
        if key is not None
    }
    # Counted, so a rewrite that keeps one of two entries with the same key
    # still drops the other.
    kept = Counter(
        key
        for key in (_story_key(entry) for entry in current.remaining_stories)
        if key is not None
    )
    body = current_content.lstrip("\ufeff")
    reasons = dropped_story_reasons(body)
    bullets = _dropped_story_bullets(body)
    unexplained: list[str] = []
    missing_reason: list[str] = []
    for entry in prior.remaining_stories:
        key = _story_key(entry)
        if key is None or key in exempt:
            continue
        if kept[key] > 0:
            kept[key] -= 1
            continue
        reason = (
            _title_drop_reason(key, bullets)
            if key.startswith("title:")
            else reasons.get(key)
        )
        if reason is None:
            unexplained.append(entry)
        elif not reason:
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
    """Warnings for `remaining_stories` entries the project state file never names.

    An entry with an ID must have that ID in the file; a title-only entry must
    have its full title.
    """

    if project_state_content is None:
        return []
    current = _parse(current_content)
    if current is None:
        return []
    state_text = f" {_normalized_title(project_state_content)} "
    missing: list[str] = []
    for entry in current.remaining_stories:
        token = story_id_token(entry)
        if token is None:
            title = _normalized_title(entry)
            if title and f" {title} " not in state_text:
                missing.append(entry)
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
