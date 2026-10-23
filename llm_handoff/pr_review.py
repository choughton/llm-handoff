"""Wait for a GitHub pull request reviewer to finish with the head commit.

``publish.wait_for_reviewers: [codex]`` makes the dispatcher wait, before it
merges a story's pull request, for the Codex code review (the ChatGPT Codex
connector app) of the exact commit it pushed. Codex reports progress through:

- a summary comment (``<!-- codex-pull-request-review-summary -->``) whose
  ``**Code Review**`` table row shows the status and short SHA of the commit
  it reviewed, and, while a review runs, a ``codex-review-state:v1`` JSON
  marker with the full SHA and status;
- a pull request review on the reviewed commit, with one inline comment per
  finding, when it has suggestions;
- a +1 reaction on the pull request when every review finished without
  findings (and an eyes reaction while one runs).

Only activity from the configured bot login counts, so a comment written by
anyone else cannot mark a review complete. Signals are matched to the head
commit's SHA, so a review of an earlier push never satisfies the gate.

After a review is re-requested, ``stale_before`` ignores a summary comment
Codex has not touched since the request, so the failed status of the earlier
review does not end the new wait at once.

Codex can also mark a review completed without naming a commit: the state
marker's ``commit_sha`` is null, the table's Commit cell is a dash, and there
is no review or reaction. Nothing ties that review to the head commit, so it
is not a pass. When a summary updated after the push or request shows it on
two polls in a row (so a +1 reaction posted just after the summary still
counts), the wait ends as ``failed`` with ``unattributed`` set.

A review can also stay ``running`` with no result: Codex adds its eyes
reaction and never finishes. With ``stall_seconds``, a wait that has seen the
summary show the head commit's review running for that long ends as
``stalled``, without waiting out the full timeout.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import time
from typing import Any, Literal


LogFn = Callable[[str, str], None]
FetchFn = Callable[[str], "list[dict[str, Any]] | None"]

SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"
_STATE_MARKER_RE = re.compile(
    r"<!--\s*codex-review-state:v1\s+(\{.*?\})\s*-->", re.DOTALL
)
_CODE_REVIEW_ROW_RE = re.compile(
    r"^\|[^|\n]*\*\*Code Review\*\*[^|\n]*\|([^|\n]*)\|\s*`([0-9a-fA-F]{7,40})`",
    re.MULTILINE,
)
# The same row with any Commit cell, such as the dash Codex writes when a
# completed review names no commit.
_CODE_REVIEW_ANY_ROW_RE = re.compile(
    r"^\|[^|\n]*\*\*Code Review\*\*[^|\n]*\|([^|\n]*)\|([^|\n]*)\|",
    re.MULTILINE,
)
_SHA_CELL_RE = re.compile(r"`[0-9a-fA-F]{7,40}`")
_RUNNING_STATUSES = {"running", "queued", "pending", "in_progress", "started"}
_FAILED_STATUSES = {"failed", "error", "errored", "cancelled", "canceled"}
_BADGE_RE = re.compile(r"!\[(\w+) Badge\]\([^)]*\)")
_FEEDBACK_LINE_RE = re.compile(r"(?m)^Useful\? React with .*$")
_DETAILS_RE = re.compile(r"<details>.*?</details>", re.DOTALL)
_FINDING_BODY_LIMIT = 4000
# Posted by the Codex bot, next to a failed summary, when the account is out
# of code review usage.
_USAGE_LIMIT_RE = re.compile(r"usage limits? for code reviews", re.IGNORECASE)
_RESPONSE_RE = re.compile(
    r"(?ms)^### Finding (\d+)\b[^\n]*\n(.*?)(?=^### |\Z)"
)
# "Deferred:" or "**Deferred:**" at the start of a response.
_DEFERRED_RE = re.compile(r"(?:\*\*|__)?deferred\s*:", re.IGNORECASE)
# "Follow-up:" or "**Follow-up:**" at the start of a planner decision.
_FOLLOW_UP_RE = re.compile(r"(?:\*\*|__)?follow[- ]?up\s*:", re.IGNORECASE)

ReviewOutcome = Literal["clean", "findings", "timeout", "failed", "stalled"]


@dataclass(frozen=True)
class ReviewGate:
    """Which reviewers to wait for, and for how long."""

    reviewers: tuple[str, ...]
    timeout_seconds: int = 1200
    poll_seconds: int = 30
    max_rounds: int = 1
    codex_bot_login: str = "chatgpt-codex-connector[bot]"
    # A review shown running this long with no result counts as stalled.
    stall_seconds: int = 600
    # "prompt" or "merge": what happens when the retried review does not
    # finish either (``publish.review_stall_action``).
    stall_action: str = "prompt"
    # The Codex CLI for the ``codex-local`` reviewer; empty means the
    # configured Codex binary.
    codex_command: tuple[str, ...] = ()

    @property
    def local(self) -> bool:
        """Whether the dispatcher runs the Codex review itself."""

        return "codex-local" in self.reviewers


@dataclass(frozen=True)
class ReviewFinding:
    body: str
    path: str | None = None
    line: int | None = None
    url: str | None = None
    comment_id: int | None = None

    def location(self) -> str:
        if self.path and self.line:
            return f"{self.path}:{self.line}"
        return self.path or "pull request"


@dataclass(frozen=True)
class CodexReviewState:
    """What the pull request currently says about the head commit's review."""

    completed: bool
    failed: bool = False
    findings: tuple[ReviewFinding, ...] = ()
    detail: str = "no Codex activity yet"
    usage_limited: bool = False
    # The summary marks a review completed but names no commit.
    unattributed: bool = False
    # The summary shows a review of the head commit running.
    running: bool = False


@dataclass(frozen=True)
class ReviewResult:
    outcome: ReviewOutcome
    findings: tuple[ReviewFinding, ...] = field(default_factory=tuple)
    detail: str = ""
    # A failed review because the Codex account is out of code review usage.
    usage_limited: bool = False
    # A failed review because Codex marked one completed without naming the
    # commit it reviewed.
    unattributed: bool = False


def read_codex_review(
    *,
    head_sha: str,
    bot_login: str,
    requested_at: datetime,
    issue_comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
    reactions: list[dict[str, Any]],
    stale_before: datetime | None = None,
) -> CodexReviewState:
    """Interpret the Codex signals on a pull request for ``head_sha``.

    With ``stale_before``, a summary comment last updated before it, or in
    the same second (GitHub timestamps have one-second resolution), is
    ignored.
    """

    head = head_sha.lower()

    def by_bot(item: dict[str, Any]) -> bool:
        return ((item.get("user") or {}).get("login") or "") == bot_login

    findings = [
        ReviewFinding(
            body=_clean_body(comment.get("body") or ""),
            path=comment.get("path") or None,
            line=comment.get("line") or comment.get("original_line") or None,
            url=comment.get("html_url") or None,
            comment_id=comment.get("id") if isinstance(comment.get("id"), int) else None,
        )
        for comment in review_comments
        if by_bot(comment)
        and not comment.get("in_reply_to_id")
        and (comment.get("original_commit_id") or comment.get("commit_id") or "")
        .lower()
        == head
    ]
    head_reviews = [
        review
        for review in reviews
        if by_bot(review) and (review.get("commit_id") or "").lower() == head
    ]
    if head_reviews and not findings:
        findings = [
            ReviewFinding(body=_clean_body(review.get("body") or ""), url=review.get("html_url"))
            for review in head_reviews
            if _clean_body(review.get("body") or "")
        ]

    summary_status: str | None = None
    unresolved = False
    unattributed = False
    for comment in issue_comments:
        body = comment.get("body") or ""
        if not by_bot(comment) or SUMMARY_MARKER not in body:
            continue
        updated = _parse_time(comment.get("updated_at") or comment.get("created_at"))
        if stale_before is not None and updated <= stale_before:
            continue
        summary_status, unresolved = _summary_status(body, head)
        # Only a summary Codex updated after the push or request counts, so
        # an earlier review that named no commit does not end a new wait.
        unattributed = updated > requested_at and _completed_without_commit(body)

    thumbs_up = any(
        by_bot(reaction)
        and reaction.get("content") == "+1"
        and _parse_time(reaction.get("created_at")) >= requested_at
        for reaction in reactions
    )

    # The usage-limit comment names no commit, so it only counts while the
    # summary shows a review of this head running or failed. Codex can post
    # it a few seconds before the summary flips to Failed.
    usage_limited = summary_status in {"running", "failed"} and any(
        by_bot(comment)
        and _USAGE_LIMIT_RE.search(comment.get("body") or "")
        and _parse_time(comment.get("created_at")) >= requested_at
        for comment in issue_comments
    )
    if summary_status == "failed" or usage_limited:
        return CodexReviewState(
            completed=True,
            failed=True,
            findings=tuple(findings),
            detail=(
                "Codex reached its usage limit for code reviews, so the review "
                f"of {head[:12]} failed (add credits or wait for the limit to "
                "reset, then review the pull request yourself)"
                if usage_limited
                else "Codex reported the review of this commit as failed"
            ),
            usage_limited=usage_limited,
        )
    if unresolved and not findings:
        findings = [
            ReviewFinding(
                body="Codex reported unresolved findings for this commit; see "
                "the pull request conversation."
            )
        ]
    completed = summary_status == "completed" or bool(head_reviews) or thumbs_up
    if summary_status is None and unattributed and not completed and not findings:
        return CodexReviewState(
            completed=False,
            unattributed=True,
            detail=(
                "Codex marked a review completed without naming a commit, so "
                f"the dispatcher cannot tell it reviewed {head[:12]}"
            ),
        )
    if completed:
        detail = (
            f"Codex finished reviewing {head[:12]} with {len(findings)} finding(s)"
            if findings
            else f"Codex finished reviewing {head[:12]} with no findings"
        )
    elif summary_status == "running":
        detail = f"Codex is reviewing {head[:12]}"
    else:
        detail = f"waiting for Codex to start reviewing {head[:12]}"
    return CodexReviewState(
        completed=completed,
        findings=tuple(findings),
        detail=detail,
        running=not completed and summary_status == "running",
    )


def wait_for_codex_review(
    *,
    fetch: FetchFn,
    pr_number: int,
    head_sha: str,
    bot_login: str,
    requested_at: datetime,
    timeout_seconds: int,
    poll_seconds: int,
    log: LogFn,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    stale_before: datetime | None = None,
    stall_seconds: int | None = None,
) -> ReviewResult:
    """Poll the pull request until Codex finishes reviewing ``head_sha``.

    With ``stall_seconds``, the wait ends as ``stalled`` once the summary has
    shown the review running for that long, counted from the first poll of
    this wait that saw it running.
    """

    base = "repos/{owner}/{repo}"
    endpoints = {
        "issue_comments": f"{base}/issues/{pr_number}/comments",
        "reviews": f"{base}/pulls/{pr_number}/reviews",
        "review_comments": f"{base}/pulls/{pr_number}/comments",
        "reactions": f"{base}/issues/{pr_number}/reactions",
    }
    deadline = clock() + timeout_seconds
    last_detail = ""
    state = CodexReviewState(completed=False)
    unattributed_polls = 0
    running_since: float | None = None
    while True:
        responses: dict[str, list[dict[str, Any]]] = {}
        for key, endpoint in endpoints.items():
            items = fetch(endpoint)
            if items is None:
                break
            responses[key] = items
        if len(responses) == len(endpoints):
            state = read_codex_review(
                head_sha=head_sha,
                bot_login=bot_login,
                requested_at=requested_at,
                stale_before=stale_before,
                **responses,
            )
            if state.detail != last_detail:
                log("INFO", f"PR review: {state.detail}.")
                last_detail = state.detail
            if state.failed:
                return ReviewResult(
                    "failed", state.findings, state.detail, state.usage_limited
                )
            if state.completed:
                outcome: ReviewOutcome = "findings" if state.findings else "clean"
                return ReviewResult(outcome, state.findings, state.detail)
            # Two polls in a row, so a +1 reaction Codex posts a few seconds
            # after the summary still makes the review clean.
            unattributed_polls = unattributed_polls + 1 if state.unattributed else 0
            if unattributed_polls >= 2:
                return ReviewResult("failed", detail=state.detail, unattributed=True)
            if not state.running:
                running_since = None
            elif stall_seconds is not None:
                now = clock()
                if running_since is None:
                    running_since = now
                if now - running_since >= stall_seconds:
                    return ReviewResult(
                        "stalled",
                        detail=(
                            f"the Codex review of {head_sha[:12]} has shown as "
                            f"running for at least {stall_seconds}s with no result"
                        ),
                    )
        if clock() >= deadline:
            if state.unattributed:
                return ReviewResult("failed", detail=state.detail, unattributed=True)
            if state.findings:
                return ReviewResult("findings", state.findings, state.detail)
            return ReviewResult(
                "timeout",
                detail=(
                    f"no completed Codex review of {head_sha[:12]} after "
                    f"{timeout_seconds}s ({state.detail})"
                ),
            )
        sleep(poll_seconds)


def parse_review_responses(
    text: str, heading: str = "Review Responses"
) -> dict[int, str]:
    """Read ``### Finding N`` entries from a ``## Review Responses`` section.

    ``heading`` names a different ``## `` section with the same layout.
    """

    normalized = text.replace("\r\n", "\n")
    section = re.search(
        rf"(?ms)^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", normalized
    )
    if section is None:
        return {}
    responses: dict[int, str] = {}
    for match in _RESPONSE_RE.finditer(section.group(1)):
        body = match.group(2).strip()
        if body:
            responses[int(match.group(1))] = body
    return responses


def is_deferred_response(body: str) -> bool:
    """Whether a review response starts with the ``Deferred:`` marker.

    A deferred finding is real but left unfixed in this story, so its review
    thread gets the reply but stays unresolved for a person to decide.
    """

    return _DEFERRED_RE.match(body) is not None


def is_follow_up_response(body: str) -> bool:
    """Whether a planner decision starts with the ``Follow-up:`` marker.

    The planner accepts a deferred finding by naming the follow-up story that
    will fix it, so the dispatcher can post the decision and resolve the
    finding's review thread.
    """

    return _FOLLOW_UP_RE.match(body) is not None


def parse_gh_json_pages(text: str) -> list[dict[str, Any]] | None:
    """Parse ``gh api --paginate`` output: one or more JSON arrays."""

    decoder = json.JSONDecoder()
    items: list[dict[str, Any]] = []
    position = 0
    text = text.strip()
    while position < len(text):
        try:
            value, end = decoder.raw_decode(text, position)
        except ValueError:
            return None
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            items.append(value)
        position = end
        while position < len(text) and text[position].isspace():
            position += 1
    return items


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _summary_status(body: str, head: str) -> tuple[str | None, bool]:
    """Return (``running``/``completed``/``failed``/None, unresolved)."""

    marker = _STATE_MARKER_RE.search(body)
    if marker is not None:
        try:
            code_review = json.loads(marker.group(1)).get("code_review") or {}
        except (ValueError, AttributeError):
            code_review = {}
        if (code_review.get("commit_sha") or "").lower() == head:
            status = str(code_review.get("status") or "").lower()
            unresolved = code_review.get("has_unresolved_findings") is True
            if status in _RUNNING_STATUSES:
                return "running", unresolved
            if status in _FAILED_STATUSES:
                return "failed", unresolved
            if status:
                return "completed", unresolved
    row = _CODE_REVIEW_ROW_RE.search(body)
    if row is None or not head.startswith(row.group(2).lower()):
        return None, False
    cell = row.group(1).lower()
    if "completed" in cell:
        return "completed", False
    if "fail" in cell or "error" in cell:
        return "failed", False
    return "running", False


def _completed_without_commit(body: str) -> bool:
    """Whether the summary marks a code review completed but names no commit."""

    marker = _STATE_MARKER_RE.search(body)
    if marker is not None:
        try:
            code_review = json.loads(marker.group(1)).get("code_review")
        except (ValueError, AttributeError):
            code_review = None
        if isinstance(code_review, dict):
            return (
                not code_review.get("commit_sha")
                and str(code_review.get("status") or "").lower() == "completed"
            )
    row = _CODE_REVIEW_ANY_ROW_RE.search(body)
    return (
        row is not None
        and "completed" in row.group(1).lower()
        and _SHA_CELL_RE.search(row.group(2)) is None
    )


def _clean_body(body: str) -> str:
    text = _DETAILS_RE.sub("", body)
    text = _FEEDBACK_LINE_RE.sub("", text)
    text = _BADGE_RE.sub(r"\1", text)
    text = text.replace("<sub>", "").replace("</sub>", "").strip()
    if len(text) > _FINDING_BODY_LIMIT:
        text = text[:_FINDING_BODY_LIMIT].rstrip() + "\n\n(finding truncated)"
    return text


def _parse_time(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
