from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from llm_handoff.config import DispatchConfig
from llm_handoff.pr_review import ReviewGate
from llm_handoff.publish import (
    STATE_PATH,
    PublishOutcome,
    ensure_story_branch,
    publish_story,
    save_gate_section,
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
    endpoint = args[-1].split("{repo}/", 1)[1]
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
    assert "Dispatcher Verification" in message
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
    assert [call[:2] for call in _gh_calls(tmp_path)] == [
        ["pr", "view"],
        ["pr", "create"],
        ["pr", "merge"],
    ]
    merge_call = _gh_calls(tmp_path)[2]
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


def test_codex_findings_bounce_to_the_implementer_then_rereview_and_merge(
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

    # The implementer fixes the finding on the same branch, and Codex reviews
    # the new head commit cleanly. The old finding stays on the old commit.
    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    second = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(second)],
        review_comments=[_codex_finding(first)],
    )

    outcome = _publish_with_review(repo, gh, log)

    assert outcome == PublishOutcome()
    calls = _gh_calls(tmp_path)
    kinds = [call[:2] for call in calls]
    assert kinds.count(["pr", "create"]) == 1
    assert ["pr", "edit"] in kinds
    comment = calls[kinds.index(["pr", "comment"])]
    assert comment[comment.index("--body") + 1] == "@codex review"
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


def test_codex_gate_pauses_after_the_last_review_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    head = _git(repo, "rev-parse", "HEAD")
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    (repo / STATE_PATH).write_text(
        json.dumps({**state, "review_rounds": 3}), encoding="utf-8"
    )
    _set_api(
        tmp_path,
        comments=[_codex_summary(head)],
        review_comments=[_codex_finding(head)],
    )
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is True and outcome.handoff is None
    assert "after 3 review round(s)" in outcome.message
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


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
