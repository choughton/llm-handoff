from __future__ import annotations

from datetime import datetime, timezone

from llm_handoff.pr_review import (
    is_deferred_response,
    is_follow_up_response,
    parse_gh_json_pages,
    parse_review_responses,
    read_codex_review,
    wait_for_codex_review,
)


BOT = "chatgpt-codex-connector[bot]"
HEAD = "99a76b7cf94a3d85ae983e1f5cb0441ce94e91bb"
OLD = "77bceebafc3d3f510f05f92cdb47266d3673f489"
REQUESTED = datetime(2026, 10, 15, 6, 48, tzinfo=timezone.utc)


def summary(
    short_sha: str, status: str = "\u2705 **Completed**", marker: str = ""
) -> dict:
    """The summary comment as Codex in GitHub writes it."""

    return {
        "user": {"login": BOT},
        "body": (
            "<!-- codex-pull-request-review-summary -->\n"
            + marker
            + "\n## Codex Review Summary\n\n"
            "| Review | Status | Commit | Review trigger |\n"
            "| --- | --- | --- | --- |\n"
            f"| \U0001f4dd **Code Review** | {status} "
            '<relative-time datetime="2026-10-15T06:53:08Z">x</relative-time> '
            f"| `{short_sha}` | PR opened |\n"
        ),
    }


def state_marker(sha: str, status: str, unresolved: object = None) -> str:
    return (
        '<!-- codex-review-state:v1 {"code_review":{"commit_sha":"'
        + sha
        + '","has_any_findings":null,"has_unresolved_findings":'
        + ("true" if unresolved is True else "null")
        + ',"status":"'
        + status
        + '"},"security_review":null} -->\n'
    )


FINDINGS_REVIEW = {
    "user": {"login": BOT},
    "state": "COMMENTED",
    "commit_id": HEAD,
    "body": "\n### \U0001f4a1 Codex Review\n\nHere are some automated review "
    "suggestions for this pull request.\n\n**Reviewed commit:** `99a76b7cf9`\n"
    "<details> <summary>About Codex in GitHub</summary>\nboilerplate\n</details>",
    "html_url": "https://github.example/acme/repo/pull/8#pullrequestreview-1",
}

INLINE_FINDING = {
    "user": {"login": BOT},
    "commit_id": HEAD,
    "original_commit_id": HEAD,
    "in_reply_to_id": None,
    "path": "llm_handoff/publish.py",
    "line": 175,
    "body": "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)"
    "</sub></sub>  Stop when verified work lacks publish state**\n\nIt only warns."
    "\n\nUseful? React with \U0001f44d / \U0001f44e.",
    "html_url": "https://github.example/acme/repo/pull/8#discussion_r1",
}


def read(**overrides: list) -> object:
    signals = {
        "issue_comments": [],
        "reviews": [],
        "review_comments": [],
        "reactions": [],
    }
    signals.update(overrides)
    return read_codex_review(
        head_sha=HEAD, bot_login=BOT, requested_at=REQUESTED, **signals
    )


def test_completed_summary_for_the_head_commit_is_a_clean_review() -> None:
    state = read(issue_comments=[summary(HEAD[:7])])

    assert state.completed is True
    assert state.findings == ()


def test_a_completed_review_of_an_earlier_commit_does_not_count() -> None:
    state = read(issue_comments=[summary(OLD[:7])])

    assert state.completed is False


def test_running_marker_for_the_head_commit_is_still_running() -> None:
    state = read(
        issue_comments=[
            summary(OLD[:7], marker=state_marker(HEAD, "running")),
        ]
    )

    assert state.completed is False
    assert "reviewing" in state.detail


def test_inline_comments_on_the_head_commit_are_findings() -> None:
    state = read(
        issue_comments=[summary(HEAD[:7])],
        reviews=[FINDINGS_REVIEW],
        review_comments=[
            INLINE_FINDING,
            {**INLINE_FINDING, "in_reply_to_id": 1, "body": "a reply"},
            {**INLINE_FINDING, "original_commit_id": OLD, "body": "older finding"},
        ],
    )

    assert state.completed is True
    assert len(state.findings) == 1
    finding = state.findings[0]
    assert finding.location() == "llm_handoff/publish.py:175"
    assert finding.body.startswith("**P1  Stop when verified work lacks publish state**")
    assert "Useful?" not in finding.body
    assert finding.url == INLINE_FINDING["html_url"]


def test_a_bot_review_on_the_head_commit_alone_completes_with_its_body() -> None:
    state = read(reviews=[FINDINGS_REVIEW])

    assert state.completed is True
    assert len(state.findings) == 1
    assert "Reviewed commit" in state.findings[0].body
    assert "boilerplate" not in state.findings[0].body


def test_signals_from_other_accounts_are_ignored() -> None:
    impostor = {"login": "someone"}
    state = read(
        issue_comments=[{**summary(HEAD[:7]), "user": impostor}],
        reviews=[{**FINDINGS_REVIEW, "user": impostor}],
        reactions=[
            {"user": impostor, "content": "+1", "created_at": "2026-10-15T07:00:00Z"}
        ],
    )

    assert state.completed is False
    assert state.findings == ()


def test_thumbs_up_counts_only_after_the_review_was_requested() -> None:
    before = {"user": {"login": BOT}, "content": "+1", "created_at": "2026-10-15T06:00:00Z"}
    after = {**before, "created_at": "2026-10-15T07:00:00Z"}

    assert read(reactions=[before]).completed is False
    assert read(reactions=[after]).completed is True


def test_unresolved_findings_without_comments_still_block() -> None:
    state = read(
        issue_comments=[
            summary(OLD[:7], marker=state_marker(HEAD, "completed", unresolved=True))
        ]
    )

    assert state.completed is True
    assert len(state.findings) == 1
    assert "unresolved findings" in state.findings[0].body


def test_a_failed_review_is_reported() -> None:
    state = read(issue_comments=[summary(HEAD[:7], status="**Failed**")])

    assert state.failed is True
    assert state.usage_limited is False


def test_a_summary_untouched_since_a_review_request_is_ignored() -> None:
    # After the dispatcher asks Codex to review again, the failed summary of
    # the earlier review must not end the new wait.
    asked = datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc)
    failed = {
        **summary(HEAD[:7], status="**Failed**"),
        "updated_at": "2026-10-15T06:55:00Z",
    }

    stale = read_codex_review(
        head_sha=HEAD, bot_login=BOT, requested_at=asked, stale_before=asked,
        issue_comments=[failed], reviews=[], review_comments=[], reactions=[],
    )
    fresh = read_codex_review(
        head_sha=HEAD, bot_login=BOT, requested_at=asked, stale_before=asked,
        issue_comments=[
            {**summary(HEAD[:7]), "updated_at": "2026-10-15T07:04:00Z"}
        ],
        reviews=[], review_comments=[], reactions=[],
    )

    # GitHub timestamps have one-second resolution, so a summary updated in
    # the second of the request may still be the old one.
    same_second = read_codex_review(
        head_sha=HEAD, bot_login=BOT, requested_at=asked, stale_before=asked,
        issue_comments=[{**failed, "updated_at": "2026-10-15T07:00:00Z"}],
        reviews=[], review_comments=[], reactions=[],
    )

    assert stale.failed is False and stale.completed is False
    assert same_second.failed is False and same_second.completed is False
    assert fresh.completed is True and fresh.failed is False


def test_parse_gh_json_pages_joins_paginated_arrays() -> None:
    assert parse_gh_json_pages('[{"a": 1}]\n[{"a": 2}, 3]') == [{"a": 1}, {"a": 2}]
    assert parse_gh_json_pages("") == []
    assert parse_gh_json_pages("not json") is None


def test_wait_returns_findings_or_times_out() -> None:
    now = [0.0]
    pages = {"issue_comments": [], "reviews": [], "review_comments": [], "reactions": []}

    def fetch(endpoint: str) -> list:
        for key, path in (
            ("issue_comments", "issues/8/comments"),
            ("reviews", "pulls/8/reviews"),
            ("review_comments", "pulls/8/comments"),
            ("reactions", "issues/8/reactions"),
        ):
            if endpoint.endswith(path):
                return pages[key]
        raise AssertionError(endpoint)

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] >= 60:
            pages["reviews"] = [FINDINGS_REVIEW]
            pages["review_comments"] = [INLINE_FINDING]

    logs: list[tuple[str, str]] = []
    common = dict(
        fetch=fetch,
        pr_number=8,
        head_sha=HEAD,
        bot_login=BOT,
        requested_at=REQUESTED,
        poll_seconds=30,
        log=lambda level, message: logs.append((level, message)),
        sleep=sleep,
        clock=lambda: now[0],
    )

    result = wait_for_codex_review(timeout_seconds=600, **common)
    assert result.outcome == "findings"
    assert len(result.findings) == 1
    assert now[0] == 60

    now[0] = 0.0
    pages.update(reviews=[], review_comments=[])
    sleep_never_reviews = lambda seconds: now.__setitem__(0, now[0] + seconds)  # noqa: E731
    result = wait_for_codex_review(
        timeout_seconds=45, **{**common, "sleep": sleep_never_reviews}
    )
    assert result.outcome == "timeout"
    assert "45s" in result.detail


def test_findings_keep_their_comment_id_for_thread_replies() -> None:
    state = read(review_comments=[{**INLINE_FINDING, "id": 4279958834}])

    assert state.findings[0].comment_id == 4279958834


def test_parse_review_responses_reads_numbered_findings() -> None:
    text = (
        "# Handback\n\n## Review Responses\n\n"
        "### Finding 1: `llm_handoff/publish.py:175`\nNow stops with an error.\n\n"
        "### Finding 2\n\n### Finding 3\nLeft as is: out of scope.\n\n"
        "## Verification Evidence\n\n### Finding 9\nnot a response\n"
    )

    assert parse_review_responses(text) == {
        1: "Now stops with an error.",
        3: "Left as is: out of scope.",
    }
    assert parse_review_responses("no section") == {}


def test_is_deferred_response_needs_the_marker_at_the_start() -> None:
    assert is_deferred_response("Deferred: out of scope for this story.")
    assert is_deferred_response("**Deferred:** needs its own Work Packet.")
    assert is_deferred_response("deferred : later")
    assert not is_deferred_response("Left as is: out of scope.")
    assert not is_deferred_response("Fixed; nothing deferred: see the test.")


def test_is_follow_up_response_needs_the_marker_at_the_start() -> None:
    assert is_follow_up_response("Follow-up: W9.10 fixes this.")
    assert is_follow_up_response("**Follow-up:** W9.10 fixes this.")
    assert is_follow_up_response("followup : W9.10")
    assert not is_follow_up_response("Deferred: out of scope.")
    assert not is_follow_up_response("Fixed here; no follow-up: needed.")


USAGE_LIMIT_COMMENT = {
    "user": {"login": BOT},
    "created_at": "2026-10-15T09:04:12Z",
    "body": "You have reached your Codex usage limits for code reviews. You can "
    "see your limits in the [Codex usage dashboard](https://chatgpt.com/codex/"
    "cloud/settings/usage).",
}


def test_a_failed_review_after_a_usage_limit_says_so() -> None:
    # The shapes Codex posted when it ran out of review usage: a failed
    # marker that still claims unresolved findings, a Failed table row, and a
    # separate usage-limit comment.
    state = read(
        issue_comments=[
            summary(
                HEAD[:7],
                status="\u26a0\ufe0f **Failed**",
                marker=state_marker(HEAD, "failed", unresolved=True),
            ),
            USAGE_LIMIT_COMMENT,
        ]
    )

    assert state.failed is True
    assert state.usage_limited is True
    assert state.findings == ()
    assert "usage limit" in state.detail


def test_a_usage_limit_comment_ends_the_wait_before_the_summary_fails() -> None:
    state = read(
        issue_comments=[
            summary(HEAD[:7], status="\U0001f504 **Running**", marker=state_marker(HEAD, "running")),
            USAGE_LIMIT_COMMENT,
        ]
    )

    assert state.failed is True
    assert "usage limit" in state.detail


def test_an_old_usage_limit_comment_does_not_fail_a_new_review() -> None:
    old = {**USAGE_LIMIT_COMMENT, "created_at": "2026-10-15T05:00:00Z"}
    impostor = {**USAGE_LIMIT_COMMENT, "user": {"login": "someone"}}

    state = read(issue_comments=[summary(HEAD[:7]), old, impostor])

    assert state.failed is False
    assert state.completed is True


def test_a_usage_limit_comment_without_a_review_of_this_head_is_ignored() -> None:
    # A late usage-limit comment from a review of an earlier commit must not
    # fail the new head, which Codex may not have started reviewing yet.
    for summaries in ([], [summary(OLD[:7], status="**Failed**")]):
        state = read(issue_comments=[*summaries, USAGE_LIMIT_COMMENT])

        assert state.failed is False
        assert state.completed is False


# The summary Codex left on a pull request on 2026-10-20: the review is
# Completed, but the state marker's commit_sha is null and the Commit cell
# is an em dash, with no review, inline comment, or reaction.
def unattributed_summary(updated_at: str = "2026-10-15T06:51:22Z") -> dict:
    return {
        "user": {"login": BOT},
        "created_at": "2026-10-15T06:48:09Z",
        "updated_at": updated_at,
        "body": (
            "<!-- codex-pull-request-review-summary -->\n"
            '<!-- codex-review-state:v1 {"code_review":{"commit_sha":null,'
            '"has_any_findings":null,"has_unresolved_findings":null,'
            '"status":"completed"},"security_review":{"commit_sha":null,'
            '"has_any_findings":null,"has_unresolved_findings":null,'
            '"status":null}} -->\n'
            "\n## Codex Review Summary\n\n"
            "| Review | Status | Commit | Review trigger |\n"
            "| --- | --- | --- | --- |\n"
            "| \U0001f4dd **Code Review** | ✅ **Completed** "
            '<relative-time datetime="2026-10-15T06:51:22.040937Z">x'
            "</relative-time> | — | PR opened |\n"
        ),
    }


# The same summary when Codex names the commit, as on the pull request
# before it, followed by the +1 reaction two seconds later.
def attributed_summary(sha: str) -> dict:
    return {
        **unattributed_summary(),
        "body": summary(
            sha[:7],
            status="✅ **Completed**",
            marker=(
                '<!-- codex-review-state:v1 {"code_review":{"commit_sha":"'
                + sha
                + '","has_any_findings":false,"has_unresolved_findings":false,'
                '"status":"completed"},"security_review":{"commit_sha":null,'
                '"has_any_findings":null,"has_unresolved_findings":null,'
                '"status":null}} -->\n'
            ),
        )["body"],
    }


THUMBS_UP = {
    "user": {"login": BOT},
    "content": "+1",
    "created_at": "2026-10-15T06:51:24Z",
}
UNATTRIBUTED_DETAIL = (
    "Codex marked a review completed without naming a commit, so the "
    f"dispatcher cannot tell it reviewed {HEAD[:12]}"
)


def test_a_completed_review_that_names_no_commit_is_unattributed_not_clean() -> None:
    state = read(issue_comments=[unattributed_summary()])

    assert state.completed is False
    assert state.failed is False
    assert state.unattributed is True
    assert state.detail == UNATTRIBUTED_DETAIL


def test_a_completed_review_that_names_the_head_commit_is_still_clean() -> None:
    state = read(issue_comments=[attributed_summary(HEAD)], reactions=[THUMBS_UP])

    assert state.completed is True
    assert state.unattributed is False
    assert state.findings == ()


def test_a_head_review_or_thumbs_up_outranks_a_summary_that_names_no_commit() -> None:
    for signals in (
        {"reactions": [THUMBS_UP]},
        {"reviews": [FINDINGS_REVIEW], "review_comments": [INLINE_FINDING]},
    ):
        state = read(issue_comments=[unattributed_summary()], **signals)

        assert state.completed is True
        assert state.unattributed is False


def test_a_summary_that_names_no_commit_counts_only_after_the_request() -> None:
    old = read(issue_comments=[unattributed_summary("2026-10-15T06:40:00Z")])
    impostor = read(
        issue_comments=[{**unattributed_summary(), "user": {"login": "someone"}}]
    )
    running = read(
        issue_comments=[
            {
                **unattributed_summary(),
                "body": unattributed_summary()["body"].replace(
                    '"status":"completed"', '"status":"running"'
                ),
            }
        ]
    )

    for state in (old, impostor, running):
        assert state.unattributed is False
        assert state.completed is False


def test_a_table_row_without_a_commit_and_no_marker_is_unattributed() -> None:
    body = unattributed_summary()["body"]
    without_marker = "\n".join(
        line for line in body.split("\n") if "codex-review-state" not in line
    )

    state = read(issue_comments=[{**unattributed_summary(), "body": without_marker}])

    assert state.unattributed is True


def _waiter(pages: dict, now: list, on_sleep=None):
    def fetch(endpoint: str) -> list:
        for key, path in (
            ("issue_comments", "issues/8/comments"),
            ("reviews", "pulls/8/reviews"),
            ("review_comments", "pulls/8/comments"),
            ("reactions", "issues/8/reactions"),
        ):
            if endpoint.endswith(path):
                return pages[key]
        raise AssertionError(endpoint)

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if on_sleep is not None:
            on_sleep()

    logs: list[tuple[str, str]] = []
    common = dict(
        fetch=fetch,
        pr_number=8,
        head_sha=HEAD,
        bot_login=BOT,
        requested_at=REQUESTED,
        poll_seconds=30,
        log=lambda level, message: logs.append((level, message)),
        sleep=sleep,
        clock=lambda: now[0],
    )
    return common, logs


def test_wait_ends_early_on_a_completed_review_that_names_no_commit() -> None:
    now = [0.0]
    pages = {
        "issue_comments": [unattributed_summary()],
        "reviews": [],
        "review_comments": [],
        "reactions": [],
    }
    common, logs = _waiter(pages, now)

    result = wait_for_codex_review(timeout_seconds=1200, **common)

    assert result.outcome == "failed"
    assert result.unattributed is True
    assert result.usage_limited is False
    assert result.detail == UNATTRIBUTED_DETAIL
    # One more poll to confirm it, not the whole timeout.
    assert now[0] == 30
    assert logs == [("INFO", f"PR review: {UNATTRIBUTED_DETAIL}.")]


def test_wait_stays_clean_when_the_thumbs_up_follows_the_summary() -> None:
    now = [0.0]
    pages = {
        "issue_comments": [unattributed_summary()],
        "reviews": [],
        "review_comments": [],
        "reactions": [],
    }
    common, _logs = _waiter(
        pages, now, on_sleep=lambda: pages.__setitem__("reactions", [THUMBS_UP])
    )

    result = wait_for_codex_review(timeout_seconds=1200, **common)

    assert result.outcome == "clean"
    assert result.unattributed is False


def test_wait_reports_a_review_that_names_no_commit_at_the_deadline() -> None:
    now = [0.0]
    pages = {
        "issue_comments": [unattributed_summary()],
        "reviews": [],
        "review_comments": [],
        "reactions": [],
    }
    common, _logs = _waiter(pages, now)

    result = wait_for_codex_review(timeout_seconds=0, **common)

    assert result.outcome == "failed"
    assert result.unattributed is True
    assert now[0] == 0
