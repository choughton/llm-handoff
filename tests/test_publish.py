from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from llm_handoff.config import DispatchConfig
from llm_handoff.pr_review import ReviewGate
from llm_handoff.publish import (
    RESPONSES_PATH,
    STATE_PATH,
    PublishOutcome,
    StoryState,
    build_review_bounce_handoff,
    ensure_story_branch,
    publish_story,
    save_gate_section,
    save_review_responses,
    story_branch_name,
)
from llm_handoff.router import route
from llm_handoff.work_claim import work_packet_errors


ASSIGNMENT = """---
next_agent: backend
reason: "Build the widget."
epic_id: E1
story_id: W9.9
story_title: Add a widget
producer: planner
---

# Handoff: Add a widget

## Work Packet

- **Objective:** Add the widget module.
- **Files in scope:** `src/widget.py`
- **Files out of bounds:** none
- **Context:** none
- **Verification command:** `python -c "print(1)"`
- **Expected next route:** auditor
"""

VERIFIED = """---
next_agent: planner
reason: "W9.9 approved."
epic_id: E1
story_id: W9.9
story_title: Add a widget
status: verified_pass
close_type: story
producer: auditor
---

# Audit Report - W9.9

## Verdict

APPROVED

## Quality Review

The widget does what the packet asked.

## Security Review

No security issues.

## Next Step

- **planner:** pick the next story.
"""

BRANCH = "llm-handoff/w9.9-add-a-widget"

FAKE_GH = r"""
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
state_path = os.environ["FAKE_GH_STATE"]
try:
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
except FileNotFoundError:
    state = {}
def save():
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
if args[:2] == ["pr", "create"]:
    state["pr"] = "OPEN"
    save()
    print("https://github.example/acme/repo/pull/7")
elif args[:2] == ["pr", "view"]:
    if "pr" not in state:
        sys.exit("no pull requests found for branch")
    print(json.dumps({"number": 7, "url": "https://github.example/acme/repo/pull/7",
                      "state": state["pr"]}))
elif args[:2] == ["pr", "merge"]:
    branch = args[2]
    subprocess.run(
        ["git", "--git-dir", os.environ["FAKE_GH_REMOTE"], "update-ref",
         "refs/heads/main", f"refs/heads/{branch}"],
        check=True,
    )
    state["pr"] = "MERGED"
    save()
elif args[0] == "api":
    fields = {}
    for flag, value in zip(args, args[1:]):
        if flag in ("-f", "-F"):
            key, _, field = value.partition("=")
            fields[key] = field
    endpoint = args[-1]
    threads = state.setdefault("threads", [])
    if endpoint == "graphql" and "resolveReviewThread" in fields["query"]:
        if state.get("fail_resolve"):
            sys.exit("resolveReviewThread failed")
        for thread in threads:
            if thread["id"] == fields["id"]:
                thread["isResolved"] = True
        save()
        print(json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}))
    elif endpoint == "graphql":
        # Like `gh api --paginate`: one JSON object per page, back to back.
        # Only fullDatabaseId carries the comment ID, as for 64-bit IDs.
        nodes = [
            {"id": t["id"], "isResolved": t["isResolved"],
             "comments": {"nodes": [{"databaseId": None,
                                     "fullDatabaseId": str(t["comment_id"])}]}}
            for t in threads
        ]
        size = state.get("page_size", 100)
        pages = [nodes[i:i + size] for i in range(0, len(nodes), size)] or [[]]
        for index, page in enumerate(pages):
            print(json.dumps({"data": {"repository": {"pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": index < len(pages) - 1,
                                 "endCursor": f"c{index}"},
                    "nodes": page}}}}}))
    elif endpoint.endswith("/replies"):
        state.setdefault("replies", []).append(
            {"endpoint": endpoint, "body": fields["body"]})
        save()
        print("{}")
    else:
        endpoint = endpoint.split("{repo}/", 1)[1]
        print(json.dumps(state.get("api", {}).get(endpoint, [])))
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "llm-handoff tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")


def _start_story(repo: Path, log_file: Path | None = None) -> None:
    error = ensure_story_branch(
        repo_root=repo,
        prefix="llm-handoff/",
        base_branch=None,
        assignment_content=ASSIGNMENT,
        log_file=log_file,
    )
    assert error is None
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "widget.py").write_text("WIDGET = 1\n", encoding="utf-8")
    _git(repo, "add", "src/widget.py")
    _git(repo, "commit", "-q", "-m", "W9.9 Add a widget")


def _logs() -> tuple[list[tuple[str, str]], object]:
    entries: list[tuple[str, str]] = []
    return entries, lambda level, message: entries.append((level, message))


def test_story_branch_name_is_a_readable_slug() -> None:
    assert story_branch_name("llm-handoff/", "W9.9", "Add a widget") == BRANCH
    assert story_branch_name("x/", "S-1", "Fix: the  thing!!") == "x/s-1-fix-the-thing"


def test_ensure_story_branch_creates_then_reuses_the_branch(tmp_path: Path) -> None:
    _init(tmp_path)

    assert (
        ensure_story_branch(
            repo_root=tmp_path,
            prefix="llm-handoff/",
            base_branch=None,
            assignment_content=ASSIGNMENT,
            log_file=None,
        )
        is None
    )
    assert _git(tmp_path, "branch", "--show-current") == BRANCH
    state = json.loads((tmp_path / STATE_PATH).read_text(encoding="utf-8"))
    assert state["base_branch"] == "main"
    assert "Add the widget module." in state["work_packet"]

    _git(tmp_path, "switch", "-q", "main")
    assert (
        ensure_story_branch(
            repo_root=tmp_path,
            prefix="llm-handoff/",
            base_branch=None,
            assignment_content=ASSIGNMENT,
            log_file=None,
        )
        is None
    )
    assert _git(tmp_path, "branch", "--show-current") == BRANCH


def test_a_revised_work_packet_reaches_the_review_bounce(tmp_path: Path) -> None:
    _init(tmp_path)
    revised = ASSIGNMENT.replace('"print(1)"', '"print(2)"')
    assert revised != ASSIGNMENT
    # The planner revises the packet while the story branch is checked out.
    for content in (ASSIGNMENT, revised):
        assert (
            ensure_story_branch(
                repo_root=tmp_path,
                prefix="llm-handoff/",
                base_branch=None,
                assignment_content=content,
                log_file=None,
            )
            is None
        )
    state = StoryState(**json.loads((tmp_path / STATE_PATH).read_text(encoding="utf-8")))

    handoff = build_review_bounce_handoff(
        state=state,
        findings=[],
        url="https://example.invalid/pull/7",
        head_sha="0" * 40,
        review_round=1,
        max_rounds=1,
    )

    assert "print(2)" in handoff
    assert "print(1)" not in handoff

    # Switching back to the existing branch refreshes it too, and an
    # assignment without a Work Packet keeps the saved one.
    _git(tmp_path, "switch", "-q", "main")
    no_packet = ASSIGNMENT.split("## Work Packet")[0]
    for content in (ASSIGNMENT, no_packet):
        assert (
            ensure_story_branch(
                repo_root=tmp_path,
                prefix="llm-handoff/",
                base_branch=None,
                assignment_content=content,
                log_file=None,
            )
            is None
        )
    state = json.loads((tmp_path / STATE_PATH).read_text(encoding="utf-8"))
    assert 'print(1)' in state["work_packet"]


def test_ensure_story_branch_refuses_to_start_from_another_story(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _git(tmp_path, "switch", "-q", "-c", "llm-handoff/other-story")

    error = ensure_story_branch(
        repo_root=tmp_path,
        prefix="llm-handoff/",
        base_branch=None,
        assignment_content=ASSIGNMENT,
        log_file=None,
    )

    assert error is not None and "llm-handoff/other-story" in error


def test_merge_commit_mode_merges_with_a_story_report(tmp_path: Path) -> None:
    _init(tmp_path)
    log_file = tmp_path / "logs" / "dispatch" / "run.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text(
        "[t] [DEBUG] usage: Earlier provider=claude cost_usd=9.99\n", encoding="utf-8"
    )
    _start_story(tmp_path, log_file)
    save_gate_section(tmp_path, "## Dispatcher Verification\n\n- **Verdict:** PASS\n")
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(
            "[t] [DEBUG] usage: Claude auditor provider=claude cost_usd=0.30\n"
            "[t] [DEBUG] usage: Codex provider=codex input_tokens=10 output_tokens=3\n"
            "[t] [DEBUG] usage: Claude planner provider=claude cost_usd=0.15\n"
        )
    _entries, log = _logs()

    outcome = publish_story(
        repo_root=tmp_path,
        mode="merge-commit",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
    )

    assert outcome.stop is False
    assert _git(tmp_path, "branch", "--show-current") == "main"
    message = _git(tmp_path, "log", "-1", "--format=%B")
    assert message.startswith("Merge W9.9: Add a widget")
    assert "Add the widget module." in message
    assert "## Test plan" in message and "**Verdict:** PASS" in message
    assert "The widget does what the packet asked." in message
    assert "Reported cost: $0.45" in message
    assert "Earlier" not in message
    assert _git(tmp_path, "log", "-1", "--format=%P").count(" ") == 1
    assert BRANCH not in _git(tmp_path, "branch", "--list")
    assert not (tmp_path / STATE_PATH).exists()


def _with_remote(tmp_path: Path) -> tuple[Path, Path, list[str], dict[str, str]]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True
    )
    _init(repo)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    script = tmp_path / "fake_gh.py"
    script.write_text(FAKE_GH, encoding="utf-8")
    gh_log = tmp_path / "gh.jsonl"
    env = {
        "FAKE_GH_LOG": str(gh_log),
        "FAKE_GH_REMOTE": str(remote),
        "FAKE_GH_STATE": str(tmp_path / "gh_state.json"),
    }
    return repo, remote, [sys.executable, str(script)], env


def _gh_calls(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "gh.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_pr_mode_without_auto_merge_opens_a_pr_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    entries, log = _logs()

    outcome = publish_story(
        repo_root=repo,
        mode="pr",
        remote="origin",
        auto_merge=False,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
        gh_command=gh,
    )

    assert outcome.stop is True and outcome.exit_code == 0
    assert "pull/7" in outcome.message
    remote_branches = subprocess.run(
        ["git", "--git-dir", str(remote), "branch", "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert BRANCH in remote_branches
    calls = _gh_calls(tmp_path)
    assert [call[:2] for call in calls] == [["pr", "view"], ["pr", "create"]]
    create = calls[1]
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == BRANCH
    body = Path(create[create.index("--body-file") + 1]).read_text(encoding="utf-8")
    assert "Add the widget module." in body
    assert any(level == "PAUSE" for level, _ in entries)


def test_pr_mode_with_auto_merge_merges_and_returns_to_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    story_head = _git(repo, "rev-parse", "HEAD")
    _entries, log = _logs()

    outcome = publish_story(
        repo_root=repo,
        mode="pr",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
        gh_command=gh,
    )

    assert outcome.stop is False
    calls = [call for call in _gh_calls(tmp_path) if call[0] == "pr"]
    assert [call[:2] for call in calls] == [
        ["pr", "view"],
        ["pr", "create"],
        ["pr", "view"],
        ["pr", "merge"],
    ]
    assert any(call[-1] == "graphql" for call in _gh_calls(tmp_path))
    merge_call = calls[-1]
    assert "--merge" in merge_call and "--delete-branch" in merge_call
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "HEAD") == story_head
    assert BRANCH not in _git(repo, "branch", "--list")


def test_pr_mode_stops_when_the_push_fails(tmp_path: Path) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    entries, log = _logs()

    outcome = publish_story(
        repo_root=tmp_path,
        mode="pr",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
        gh_command=["gh-should-not-run"],
    )

    assert outcome.stop is True and outcome.exit_code == 1
    assert any(level == "ERROR" and "git push" in msg for level, msg in entries)


def test_publish_ignores_handoffs_that_are_not_verified_pass(tmp_path: Path) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    _entries, log = _logs()

    outcome = publish_story(
        repo_root=tmp_path,
        mode="merge-commit",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED.replace("verified_pass", "verified_fail"),
        log=log,
    )

    assert outcome.stop is False
    assert _git(tmp_path, "branch", "--show-current") == BRANCH


def test_publish_modes_require_dispatcher_commits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires commit_mode: dispatcher"):
        DispatchConfig(
            repo_root=tmp_path,
            enforce_work_packet=True,
            publish={"mode": "pr"},
        )


BOT = "chatgpt-codex-connector[bot]"
GATE = ReviewGate(reviewers=("codex",), timeout_seconds=600, poll_seconds=30)


def _codex_summary(sha: str) -> dict:
    return {
        "user": {"login": BOT},
        "body": "<!-- codex-pull-request-review-summary -->\n\n"
        "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
        f"| **Code Review** | **Completed** | `{sha[:7]}` | PR opened |\n",
    }


def _codex_finding(sha: str) -> dict:
    return {
        "id": 4279958834,
        "user": {"login": BOT},
        "commit_id": sha,
        "original_commit_id": sha,
        "in_reply_to_id": None,
        "path": "src/widget.py",
        "line": 1,
        "body": "**P1 Handle a missing widget**\n\nWIDGET can be unset.",
        "html_url": "https://github.example/acme/repo/pull/7#discussion_r1",
    }


def _set_api(tmp_path: Path, **pages: list) -> None:
    path = tmp_path / "gh_state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state["api"] = {
        "issues/7/comments": pages.get("comments", []),
        "pulls/7/reviews": pages.get("reviews", []),
        "pulls/7/comments": pages.get("review_comments", []),
        "issues/7/reactions": pages.get("reactions", []),
    }
    path.write_text(json.dumps(state), encoding="utf-8")


def _gh_state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "gh_state.json").read_text(encoding="utf-8"))


def _set_threads(tmp_path: Path, *threads: dict) -> None:
    path = tmp_path / "gh_state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state["threads"] = list(threads)
    path.write_text(json.dumps(state), encoding="utf-8")


def _publish_with_review(repo: Path, gh: list[str], log, gate: ReviewGate = GATE):
    return publish_story(
        repo_root=repo,
        mode="pr",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
        gh_command=gh,
        review=gate,
        sleep=lambda seconds: None,
    )


def test_codex_gate_merges_after_a_clean_review_of_the_head_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _set_api(tmp_path, comments=[_codex_summary(_git(repo, "rev-parse", "HEAD"))])
    entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log)

    assert outcome == PublishOutcome()
    kinds = [call[:2] for call in _gh_calls(tmp_path)]
    assert ["pr", "comment"] not in kinds
    assert ["api", "--paginate"] in kinds
    assert kinds[-1] == ["pr", "merge"]
    assert _git(repo, "branch", "--show-current") == "main"
    assert any("no findings" in message for _level, message in entries)


def test_codex_findings_get_one_correction_round_then_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834})
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is False and outcome.handoff is not None
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert _git(repo, "branch", "--show-current") == BRANCH
    handoff = outcome.handoff
    assert "status: verified_fail" in handoff
    assert "story_id: W9.9" in handoff
    assert "### Finding 1: `src/widget.py:1`" in handoff
    assert "WIDGET can be unset." in handoff
    assert first in handoff
    assert work_packet_errors(handoff) == []
    decision = route(handoff)
    assert decision.route == "backend" and decision.warnings == []
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    assert state["review_rounds"] == 1
    assert state["open_findings"] == [
        {"number": 1, "comment_id": 4279958834, "location": "src/widget.py:1"}
    ]
    assert "## Review Responses" in handoff
    assert "`Deferred:`" in handoff

    # The implementer fixes the finding on the same branch. The dispatcher
    # answers and resolves the thread and merges without another review.
    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    second = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(second)],
        review_comments=[_codex_finding(first)],
    )
    save_review_responses(
        repo,
        "## Review Responses\n\n### Finding 1: `src/widget.py:1`\n"
        "Defaulted WIDGET when it is unset; asked @codex to recheck.\n",
    )

    calls_before = len(_gh_calls(tmp_path))

    outcome = _publish_with_review(repo, gh, log)

    assert outcome == PublishOutcome()
    gh_state = _gh_state(tmp_path)
    assert gh_state["threads"][0]["isResolved"] is True
    [reply] = gh_state["replies"]
    assert reply["endpoint"].endswith("/pulls/7/comments/4279958834/replies")
    assert reply["body"].startswith("Defaulted WIDGET when it is unset")
    assert "@codex" not in reply["body"]
    assert second[:12] in reply["body"]
    calls = _gh_calls(tmp_path)
    kinds = [call[:2] for call in calls]
    assert kinds.count(["pr", "create"]) == 1
    assert ["pr", "edit"] in kinds
    assert ["pr", "comment"] not in kinds
    second_round = calls[calls_before:]
    assert not any(
        call[:2] == ["api", "--paginate"] and call[-1] != "graphql"
        for call in second_round
    ), "the dispatcher waited for another Codex review"
    assert kinds[-1] == ["pr", "merge"]
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "HEAD") == second
    assert not (repo / STATE_PATH).exists()


def test_codex_gate_pauses_without_merging_when_no_review_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _set_api(tmp_path)
    entries, log = _logs()

    outcome = _publish_with_review(
        repo, gh, log, gate=ReviewGate(reviewers=("codex",), timeout_seconds=0)
    )

    assert outcome.stop is True and outcome.exit_code == 0
    assert "left open" in outcome.message
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert any(level == "PAUSE" for level, _message in entries)
    assert not (repo / STATE_PATH).exists()


def test_a_higher_round_limit_waits_for_codexs_own_review_of_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    head = _git(repo, "rev-parse", "HEAD")
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    (repo / STATE_PATH).write_text(
        json.dumps({**state, "review_rounds": 1}), encoding="utf-8"
    )
    _set_api(tmp_path, comments=[_codex_summary(head)])
    _entries, log = _logs()

    outcome = _publish_with_review(
        repo,
        gh,
        log,
        gate=ReviewGate(reviewers=("codex",), timeout_seconds=600, max_rounds=2),
    )

    assert outcome == PublishOutcome()
    kinds = [call[:2] for call in _gh_calls(tmp_path)]
    assert ["pr", "comment"] not in kinds
    assert ["api", "--paginate"] in kinds
    assert kinds[-1] == ["pr", "merge"]


@pytest.mark.parametrize(
    "publish",
    [
        {"mode": "merge-commit", "wait_for_reviewers": ["codex"]},
        {"mode": "pr", "auto_merge": False, "wait_for_reviewers": ["codex"]},
        {"mode": "pr", "wait_for_reviewers": ["codex"], "review_timeout_seconds": 0},
        {"mode": "pr", "wait_for_reviewers": ["copilot"]},
    ],
)
def test_wait_for_reviewers_config_is_validated(tmp_path: Path, publish: dict) -> None:
    with pytest.raises(ValueError):
        DispatchConfig(
            repo_root=tmp_path,
            enforce_work_packet=True,
            commit_mode="dispatcher",
            publish=publish,
        )


def test_wait_for_reviewers_config_accepts_codex(tmp_path: Path) -> None:
    config = DispatchConfig(
        repo_root=tmp_path,
        enforce_work_packet=True,
        commit_mode="dispatcher",
        publish={"mode": "pr", "wait_for_reviewers": ["codex"]},
    )

    assert config.publish.wait_for_reviewers == ("codex",)
    assert config.publish.codex_bot_login == BOT


def test_pr_report_does_not_mention_github_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _entries, log = _logs()

    publish_story(
        repo_root=repo,
        mode="pr",
        remote="origin",
        auto_merge=False,
        wait_for_checks=False,
        handoff_content=VERIFIED.replace(
            "The widget does what the packet asked.",
            "Then the dispatcher comments `@codex review`; email a@b.c stays.",
        ),
        log=log,
        gh_command=gh,
    )

    create = _gh_calls(tmp_path)[1]
    body = Path(create[create.index("--body-file") + 1]).read_text(encoding="utf-8")
    assert "@codex" not in body
    assert "`@\u2060codex review`" in body
    assert "a@b.c stays" in body


def test_publish_stops_when_the_story_state_is_missing(tmp_path: Path) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    (tmp_path / STATE_PATH).unlink()
    entries, log = _logs()

    outcome = publish_story(
        repo_root=tmp_path,
        mode="merge-commit",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
    )

    assert outcome.stop is True and outcome.exit_code == 1
    assert "no story state" in outcome.message
    assert any(level == "ERROR" for level, _ in entries)
    assert _git(tmp_path, "branch", "--show-current") == BRANCH


def test_publish_stops_when_the_checkout_is_not_the_story_branch(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    _git(tmp_path, "switch", "-q", "main")
    _entries, log = _logs()

    outcome = publish_story(
        repo_root=tmp_path,
        mode="merge-commit",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
    )

    assert outcome.stop is True and outcome.exit_code == 1
    assert f"expected: {BRANCH}" in outcome.message
    assert _git(tmp_path, "log", "-1", "--format=%s") == "initial"


def test_a_finding_without_a_response_keeps_its_thread_and_blocks_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834})
    entries, log = _logs()
    assert _publish_with_review(repo, gh, log).handoff is not None

    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    second = _git(repo, "rev-parse", "HEAD")
    _set_api(tmp_path, comments=[_codex_summary(second)])
    assert not (repo / RESPONSES_PATH).exists()

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert "1 review thread(s)" in outcome.message
    assert _gh_state(tmp_path)["threads"][0]["isResolved"] is False
    assert "replies" not in _gh_state(tmp_path)
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert any("No review response for finding 1" in m for _l, m in entries)


def test_a_deferred_finding_gets_a_reply_but_keeps_its_thread_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834})
    entries, log = _logs()
    assert _publish_with_review(repo, gh, log).handoff is not None

    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    _set_api(tmp_path, comments=[_codex_summary(_git(repo, "rev-parse", "HEAD"))])
    save_review_responses(
        repo,
        "## Review Responses\n\n### Finding 1: `src/widget.py:1`\n"
        "**Deferred:** out of scope for this story; needs its own packet.\n",
    )

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert "1 review thread(s)" in outcome.message
    gh_state = _gh_state(tmp_path)
    [reply] = gh_state["replies"]
    assert reply["body"].startswith("**Deferred:** out of scope")
    assert gh_state["threads"][0]["isResolved"] is False
    assert not any(
        call[-1] == "graphql" and any("resolveReviewThread" in a for a in call)
        for call in _gh_calls(tmp_path)
    )
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert any(
        level == "WARN" and "Finding 1 (src/widget.py:1) was deferred" in message
        for level, message in entries
    )


def test_a_retry_keeps_a_replied_deferred_finding_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834})
    entries, log = _logs()
    assert _publish_with_review(repo, gh, log).handoff is not None

    # An earlier attempt already posted the deferred reply and saved that in
    # the story state; the saved responses are gone (a later gate pass
    # without responses removes them). The finding must still stay open.
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    state["open_findings"][0].update(replied=True, deferred=True)
    (repo / STATE_PATH).write_text(json.dumps(state), encoding="utf-8")
    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    _set_api(tmp_path, comments=[_codex_summary(_git(repo, "rev-parse", "HEAD"))])
    assert not (repo / RESPONSES_PATH).exists()

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is True and "1 review thread(s)" in outcome.message
    assert "replies" not in _gh_state(tmp_path)
    assert _gh_state(tmp_path)["threads"][0]["isResolved"] is False
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert any("was deferred" in message for _level, message in entries)


def test_auto_merge_waits_for_every_review_thread_to_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _set_threads(
        tmp_path,
        {"id": "T9", "isResolved": False, "comment_id": 900},
        {"id": "T8", "isResolved": True, "comment_id": 800},
    )
    entries, log = _logs()

    outcome = publish_story(
        repo_root=repo,
        mode="pr",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
        gh_command=gh,
    )

    assert outcome.stop is True and outcome.exit_code == 0
    assert "1 review thread(s)" in outcome.message
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert any(level == "PAUSE" for level, _ in entries)


def test_save_review_responses_keeps_only_claims_with_responses(
    tmp_path: Path,
) -> None:
    save_review_responses(tmp_path, "## Review Responses\n\n### Finding 1\nFixed.\n")
    assert (tmp_path / RESPONSES_PATH).is_file()

    save_review_responses(tmp_path, "# Handback\n\nNo responses here.\n")
    assert not (tmp_path / RESPONSES_PATH).exists()


def _set_gh(tmp_path: Path, **values: object) -> None:
    path = tmp_path / "gh_state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state.update(values)
    path.write_text(json.dumps(state), encoding="utf-8")


def test_merge_check_reads_every_page_of_review_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    threads = [
        {"id": f"T{i}", "isResolved": i != 120, "comment_id": 5_000_000_000 + i}
        for i in range(150)
    ]
    _set_threads(tmp_path, *threads)
    _set_gh(tmp_path, page_size=100)
    _entries, log = _logs()

    outcome = publish_story(
        repo_root=repo,
        mode="pr",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=VERIFIED,
        log=log,
        gh_command=gh,
    )

    assert outcome.stop is True
    assert "1 review thread(s)" in outcome.message
    graphql = [call for call in _gh_calls(tmp_path) if call[-1] == "graphql"]
    assert all("--paginate" in call for call in graphql)
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_a_retry_after_a_failed_resolve_does_not_reply_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(
        tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834}
    )
    _entries, log = _logs()
    assert _publish_with_review(repo, gh, log).handoff is not None

    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    second = _git(repo, "rev-parse", "HEAD")
    _set_api(tmp_path, comments=[_codex_summary(second)])
    save_review_responses(
        repo, "## Review Responses\n\n### Finding 1\nDefaulted WIDGET.\n"
    )
    _set_gh(tmp_path, fail_resolve=True)

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 1
    assert "could not resolve the review thread for finding 1" in outcome.message
    assert len(_gh_state(tmp_path)["replies"]) == 1
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    assert state["open_findings"][0]["replied"] is True

    _set_gh(tmp_path, fail_resolve=False)
    outcome = _publish_with_review(repo, gh, log)

    assert outcome == PublishOutcome()
    assert len(_gh_state(tmp_path)["replies"]) == 1
    assert _gh_state(tmp_path)["threads"][0]["isResolved"] is True
    assert _gh_calls(tmp_path)[-1][:2] == ["pr", "merge"]


def test_an_already_resolved_thread_gets_no_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(
        tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834}
    )
    _entries, log = _logs()
    assert _publish_with_review(repo, gh, log).handoff is not None

    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    _set_api(tmp_path, comments=[_codex_summary(_git(repo, "rev-parse", "HEAD"))])
    _set_threads(
        tmp_path, {"id": "T1", "isResolved": True, "comment_id": 4279958834}
    )
    save_review_responses(
        repo, "## Review Responses\n\n### Finding 1\nDefaulted WIDGET.\n"
    )

    outcome = _publish_with_review(repo, gh, log)

    assert outcome == PublishOutcome()
    assert "replies" not in _gh_state(tmp_path)


def test_story_report_follows_the_pull_request_template(tmp_path: Path) -> None:
    from llm_handoff.publish import _load_state, build_report

    _init(tmp_path)
    _start_story(tmp_path)
    save_gate_section(
        tmp_path,
        "## Dispatcher Verification\n\n- **Verdict:** PASS\n"
        "- **Verification command:** `python -c \"print(1)\"` (exited 0)\n",
    )

    title, body = build_report(tmp_path, _load_state(tmp_path), VERIFIED)

    assert title == "W9.9: Add a widget"
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Summary",
        "## Changes",
        "## Reviewer notes",
        "## Test plan",
        "## Work Packet",
    ]
    template = Path(__file__).resolve().parents[1] / ".github" / "pull_request_template.md"
    template_headings = [
        line
        for line in template.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    assert headings[: len(template_headings)] == template_headings
    assert body.split("## Changes")[0].count("Add the widget module.") == 1
    assert "- `A` `src/widget.py`" in body
    assert "W9.9 Add a widget" in body
    assert "- **Audit verdict:** APPROVED" in body
    assert "<summary>Quality review</summary>" in body
    assert "The widget does what the packet asked." in body
    assert "(exited 0)" in body.split("## Test plan")[1]
    assert body.rstrip().endswith(
        "Generated with [llm-handoff](https://github.com/choughton/llm-handoff)"
    )



def test_reviewer_notes_keep_the_whole_merge_decision(tmp_path: Path) -> None:
    from llm_handoff.publish import _load_state, build_report

    _init(tmp_path)
    _start_story(tmp_path)
    handoff = VERIFIED.replace(
        "## Quality Review",
        "## Merge Decision\n\n"
        "- Quality reviewer verdict: APPROVED\n"
        "- Security reviewer verdict: APPROVED\n"
        "- Block severity threshold: high\n"
        "- Blocking findings: 0\n"
        "- Sub-threshold findings: 1\n\n"
        "## Quality Review",
    )

    _title, body = build_report(tmp_path, _load_state(tmp_path), handoff)

    notes = body.split("## Reviewer notes")[1].split("## Test plan")[0]
    assert "- **Merge decision:**\n  - Quality reviewer verdict: APPROVED" in notes
    assert "  - Security reviewer verdict: APPROVED" in notes
    assert "  - Block severity threshold: high" in notes
    assert "  - Sub-threshold findings: 1" in notes
