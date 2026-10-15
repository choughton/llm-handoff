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
REREVIEW_COMMENT = "@codex review"
_STATE_MARKER_RE = re.compile(
    r"<!--\s*codex-review-state:v1\s+(\{.*?\})\s*-->", re.DOTALL
)
_CODE_REVIEW_ROW_RE = re.compile(
    r"^\|[^|\n]*\*\*Code Review\*\*[^|\n]*\|([^|\n]*)\|\s*`([0-9a-fA-F]{7,40})`",
    re.MULTILINE,
)
_RUNNING_STATUSES = {"running", "queued", "pending", "in_progress", "started"}
_FAILED_STATUSES = {"failed", "error", "errored", "cancelled", "canceled"}
_BADGE_RE = re.compile(r"!\[(\w+) Badge\]\([^)]*\)")
_FEEDBACK_LINE_RE = re.compile(r"(?m)^Useful\? React with .*$")
_DETAILS_RE = re.compile(r"<details>.*?</details>", re.DOTALL)
_FINDING_BODY_LIMIT = 4000

ReviewOutcome = Literal["clean", "findings", "timeout", "failed"]


@dataclass(frozen=True)
class ReviewGate:
    """Which reviewers to wait for, and for how long."""

    reviewers: tuple[str, ...]
    timeout_seconds: int = 1200
    poll_seconds: int = 30
    max_rounds: int = 3
    codex_bot_login: str = "chatgpt-codex-connector[bot]"


@dataclass(frozen=True)
class ReviewFinding:
    body: str
    path: str | None = None
    line: int | None = None
    url: str | None = None

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


@dataclass(frozen=True)
class ReviewResult:
    outcome: ReviewOutcome
    findings: tuple[ReviewFinding, ...] = field(default_factory=tuple)
    detail: str = ""


def read_codex_review(
    *,
    head_sha: str,
    bot_login: str,
    requested_at: datetime,
    issue_comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
    reactions: list[dict[str, Any]],
) -> CodexReviewState:
    """Interpret the Codex signals on a pull request for ``head_sha``."""

    head = head_sha.lower()

    def by_bot(item: dict[str, Any]) -> bool:
        return ((item.get("user") or {}).get("login") or "") == bot_login

    findings = [
        ReviewFinding(
            body=_clean_body(comment.get("body") or ""),
            path=comment.get("path") or None,
            line=comment.get("line") or comment.get("original_line") or None,
            url=comment.get("html_url") or None,
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
    for comment in issue_comments:
        body = comment.get("body") or ""
        if not by_bot(comment) or SUMMARY_MARKER not in body:
            continue
        summary_status, unresolved = _summary_status(body, head)

    thumbs_up = any(
        by_bot(reaction)
        and reaction.get("content") == "+1"
        and _parse_time(reaction.get("created_at")) >= requested_at
        for reaction in reactions
    )

    if summary_status == "failed":
        return CodexReviewState(
            completed=True,
            failed=True,
            findings=tuple(findings),
            detail="Codex reported the review of this commit as failed",
        )
    if unresolved and not findings:
        findings = [
            ReviewFinding(
                body="Codex reported unresolved findings for this commit; see "
                "the pull request conversation."
            )
        ]
    completed = summary_status == "completed" or bool(head_reviews) or thumbs_up
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
        completed=completed, findings=tuple(findings), detail=detail
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
) -> ReviewResult:
    """Poll the pull request until Codex finishes reviewing ``head_sha``."""

    base = f"repos/{{owner}}/{{repo}}"
    endpoints = {
        "issue_comments": f"{base}/issues/{pr_number}/comments",
        "reviews": f"{base}/pulls/{pr_number}/reviews",
        "review_comments": f"{base}/pulls/{pr_number}/comments",
        "reactions": f"{base}/issues/{pr_number}/reactions",
    }
    deadline = clock() + timeout_seconds
    last_detail = ""
    state = CodexReviewState(completed=False)
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
                **responses,
            )
            if state.detail != last_detail:
                log("INFO", f"PR review: {state.detail}.")
                last_detail = state.detail
            if state.failed:
                return ReviewResult("failed", state.findings, state.detail)
            if state.completed:
                outcome: ReviewOutcome = "findings" if state.findings else "clean"
                return ReviewResult(outcome, state.findings, state.detail)
        if clock() >= deadline:
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
