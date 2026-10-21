from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from llm_handoff import ledger, orchestrator
from llm_handoff.agents import DispatchResult, SubagentResult
from llm_handoff.config import DispatchConfig
from llm_handoff.pr_review import ReviewGate, parse_review_responses
from llm_handoff.publish import (
    DISPLACED_HANDOFF_PATH,
    MESSAGE_PATH,
    RESPONSES_PATH,
    STATE_PATH,
    PublishOutcome,
    StateConflictResolver,
    StoryState,
    build_review_bounce_handoff,
    defer_blocked_handback,
    ensure_story_branch,
    keep_review_responses,
    leave_merged_story_branch,
    merge_aware_review_base,
    publish_story,
    recover_orphaned_story,
    resume_paused_publish,
    save_review_responses,
    saved_story_assignment,
    saved_story_base,
    settle_deferred_findings,
    story_branch_name,
    unfinished_story_pause_reason,
)
from llm_handoff.router import route
from llm_handoff.validator import ValidationResult
from llm_handoff.work_claim import (
    carry_over_key,
    extract_work_packet_section,
    work_packet_errors,
)


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
    if state.get("fail_view"):
        sys.exit("HTTP 502: Bad Gateway")
    if "pr" not in state:
        sys.exit("no pull requests found for branch")
    # Like GitHub: the head is the branch as pushed; a merged branch is gone.
    head = state.get("head") or subprocess.run(
        ["git", "--git-dir", os.environ["FAKE_GH_REMOTE"], "rev-parse", "-q",
         "--verify", f"refs/heads/{args[2]}"],
        capture_output=True, text=True,
    ).stdout.strip()
    print(json.dumps({"number": 7, "url": "https://github.example/acme/repo/pull/7",
                      "state": state["pr"], "headRefOid": head}))
elif args[:2] == ["pr", "merge"]:
    branch = args[2]
    git_dir = ["git", "--git-dir", os.environ["FAKE_GH_REMOTE"]]
    # Stands in for GitHub's merge: only a branch that has main merges.
    behind = subprocess.run(
        [*git_dir, "merge-base", "--is-ancestor", "refs/heads/main",
         f"refs/heads/{branch}"],
    ).returncode
    if behind:
        sys.exit("X Pull request acme/repo#7 is not mergeable: the merge "
                 "commit cannot be cleanly created.")
    subprocess.run(
        [*git_dir, "update-ref", "refs/heads/main", f"refs/heads/{branch}"],
        check=True,
    )
    state["pr"] = "MERGED"
    save()
elif args[:2] == ["pr", "checks"]:
    # Lets a test move main on the remote while the pull request is open.
    move = state.pop("move_main_to", None)
    if move:
        subprocess.run(
            ["git", "--git-dir", os.environ["FAKE_GH_REMOTE"], "update-ref",
             "refs/heads/main", move],
            check=True,
        )
        save()
elif args[:3] == ["api", "--method", "POST"]:
    if state.get("fail_comment"):
        sys.exit("HTTP 403: Resource not accessible by integration")
    body = args[args.index("-f") + 1].partition("=")[2]
    state.setdefault("posted", []).append({"endpoint": args[3], "body": body})
    # Lets a test answer each review request with new Codex activity.
    if body == "@codex review" and state.get("after_review_request"):
        state["api"] = state["after_review_request"].pop(0)
    save()
    print(json.dumps({"id": 99, "created_at": "2026-10-19T00:10:00Z"}))
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


# Process launches dominate this file's run time, most of all on Windows. The
# starting repositories are built once per worker and copied into each test.
_TEMPLATE_ROOT: Path | None = None


@pytest.fixture(autouse=True, scope="session")
def _repo_template_root(tmp_path_factory: pytest.TempPathFactory) -> None:
    global _TEMPLATE_ROOT
    _TEMPLATE_ROOT = tmp_path_factory.mktemp("publish-templates")


def _copy_template(name: str, build, dest: Path) -> None:
    assert _TEMPLATE_ROOT is not None
    template = _TEMPLATE_ROOT / name
    if not template.exists():
        build(template)
    shutil.copytree(template, dest, dirs_exist_ok=True)


def _refresh_index(repo: Path) -> None:
    # A copy gets new file creation times, which git's index compares, so
    # plumbing such as `git diff-index` would see every file as changed.
    _git(repo, "update-index", "-q", "--refresh")


def _init(repo: Path) -> None:
    if repo.exists() and any(repo.iterdir()):
        _build_init(repo)
        return
    _copy_template("init", _build_init, repo)
    _refresh_index(repo)


def _build_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "llm-handoff tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")


def _start_story(repo: Path, assignment: str = ASSIGNMENT) -> None:
    error = ensure_story_branch(
        repo_root=repo,
        prefix="llm-handoff/",
        base_branch=None,
        assignment_content=assignment,
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
            )
            is None
        )
    state = json.loads((tmp_path / STATE_PATH).read_text(encoding="utf-8"))
    assert 'print(1)' in state["work_packet"]


def test_saved_story_assignment_continues_only_the_checked_out_story(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    assert saved_story_assignment(tmp_path) is None
    escalation = "---\nnext_agent: user\nstatus: escalate_to_user\n---\n"
    assert ensure_story_branch(
        repo_root=tmp_path,
        prefix="llm-handoff/",
        base_branch=None,
        assignment_content=escalation,
        continue_saved_story=True,
    ) == (
        "publish needs a story_id in the assignment's frontmatter to name the "
        "story branch, and the checkout is not on a saved story's branch to "
        "continue"
    )
    _start_story(tmp_path)

    rebuilt = saved_story_assignment(tmp_path)

    assert rebuilt is not None
    assert carry_over_key(rebuilt) == "story:W9.9"
    assert extract_work_packet_section(rebuilt) == extract_work_packet_section(
        ASSIGNMENT
    )
    assert route(rebuilt).route == "backend"
    assert (
        ensure_story_branch(
            repo_root=tmp_path,
            prefix="llm-handoff/",
            base_branch=None,
            assignment_content=escalation,
            continue_saved_story=True,
        )
        is None
    )
    # Without the operator's continuation, a handoff with no story_id stops.
    assert ensure_story_branch(
        repo_root=tmp_path,
        prefix="llm-handoff/",
        base_branch=None,
        assignment_content=escalation,
    ) == (
        "publish needs a story_id in the assignment's frontmatter to name the "
        "story branch"
    )

    _git(tmp_path, "switch", "-q", "main")
    assert saved_story_assignment(tmp_path) is None


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
    )

    assert error is not None and "llm-handoff/other-story" in error


def test_a_story_branch_is_not_cut_from_an_unpushed_handoff_commit(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    handoff = Path("docs/handoff/HANDOFF.md")
    (tmp_path / "docs" / "handoff").mkdir(parents=True)
    (tmp_path / handoff).write_text("live state\n", encoding="utf-8")
    _git(tmp_path, "add", handoff.as_posix())
    _git(tmp_path, "commit", "-q", "-m", "finalizer committed the live handoff")
    live = _git(tmp_path, "rev-parse", "--short", "HEAD")

    def start() -> str | None:
        return ensure_story_branch(
            repo_root=tmp_path,
            prefix="llm-handoff/",
            base_branch="main",
            assignment_content=ASSIGNMENT,
            remote="origin",
            handoff_path=handoff,
        )

    error = start()

    assert error is not None
    assert live in error and "origin/main" in error
    assert handoff.as_posix() in error
    assert _git(tmp_path, "branch", "--show-current") == "main"
    assert not (tmp_path / STATE_PATH).exists()

    # Unpushed commits that leave the handoff alone are not the problem.
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    (tmp_path / "README.md").write_text("changed\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "local readme change")

    assert start() is None
    assert _git(tmp_path, "branch", "--show-current") == BRANCH


def test_saved_story_base_needs_the_same_story_on_its_branch(tmp_path: Path) -> None:
    _init(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    _start_story(tmp_path)

    assert saved_story_base(tmp_path, "W9.9") == base
    assert saved_story_base(tmp_path, "W1.1") is None
    assert saved_story_base(tmp_path, None) is None
    _git(tmp_path, "switch", "-q", "main")
    assert saved_story_base(tmp_path, "W9.9") is None


def _advance_remote_main(repo: Path) -> str:
    """Commit an upstream file on main and point origin/main at it.

    Returns the upstream commit; the story branch is checked out again.
    """

    _git(repo, "switch", "-q", "main")
    _commit_all(repo, {"upstream.txt": "upstream\n"}, "Upstream change")
    upstream = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", upstream)
    _git(repo, "switch", "-q", BRANCH)
    return upstream


def _review_base(repo: Path, base: str, *, story_id: str = "W9.9", head: str = "HEAD") -> str:
    return merge_aware_review_base(
        repo, remote="origin", story_id=story_id, base_sha=base, head=head
    )


def test_merge_aware_review_base_skips_files_a_base_branch_merge_brought_in(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    _start_story(tmp_path)
    _advance_remote_main(tmp_path)
    _git(tmp_path, "merge", "-q", "--no-edit", "refs/remotes/origin/main")

    result = _review_base(tmp_path, base)

    assert result == _git(tmp_path, "merge-base", "HEAD", "refs/remotes/origin/main")
    assert result != base
    changed = _git(tmp_path, "diff", "--name-only", result, "HEAD").splitlines()
    assert "src/widget.py" in changed
    assert "upstream.txt" not in changed
    # The saved base range is what the fix avoids.
    assert "upstream.txt" in _git(tmp_path, "diff", "--name-only", base, "HEAD")


def test_merge_aware_review_base_keeps_the_saved_base_without_a_merge(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    _start_story(tmp_path)
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", base)

    assert _review_base(tmp_path, base) == base

    # The remote base branch moved on, but the story branch did not merge it.
    _advance_remote_main(tmp_path)
    assert _review_base(tmp_path, base) == base


def test_merge_aware_review_base_keeps_the_saved_base_without_a_tracking_ref(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    _start_story(tmp_path)
    _advance_remote_main(tmp_path)
    _git(tmp_path, "merge", "-q", "--no-edit", "main")
    _git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")

    assert _review_base(tmp_path, base) == base


def test_merge_aware_review_base_needs_the_same_story_on_its_branch(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    _start_story(tmp_path)
    _advance_remote_main(tmp_path)
    _git(tmp_path, "merge", "-q", "--no-edit", "refs/remotes/origin/main")
    story_head = _git(tmp_path, "rev-parse", "HEAD")

    assert _review_base(tmp_path, base, story_id="W1.1") == base
    assert _review_base(tmp_path, base, story_id="  ") == base
    _git(tmp_path, "switch", "-q", "main")
    assert _review_base(tmp_path, base, head=story_head) == base


def test_merge_aware_review_base_needs_a_merge_base_after_the_saved_base(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    story_commit = _git(tmp_path, "rev-parse", "HEAD")
    _advance_remote_main(tmp_path)
    _git(tmp_path, "merge", "-q", "--no-edit", "refs/remotes/origin/main")

    # The merge-base (the upstream commit) does not descend from this base.
    assert _review_base(tmp_path, story_commit) == story_commit


def test_merge_aware_review_base_never_returns_an_empty_range(tmp_path: Path) -> None:
    _init(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    _start_story(tmp_path)
    upstream = _advance_remote_main(tmp_path)

    # A head on the remote base branch is its own merge-base with it.
    assert _review_base(tmp_path, base, head=upstream) == base
    assert _review_base(tmp_path, base, head="no-such-revision") == base


def test_merge_commit_mode_merges_with_a_story_report(tmp_path: Path) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
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
    assert "The widget does what the packet asked." in message
    for removed in ("## Test plan", "## Work Packet", "## Token usage"):
        assert removed not in message
    assert _git(tmp_path, "log", "-1", "--format=%P").count(" ") == 1
    assert BRANCH not in _git(tmp_path, "branch", "--list")
    assert not (tmp_path / STATE_PATH).exists()


def _build_with_remote(root: Path) -> None:
    repo = root / "repo"
    remote = root / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True
    )
    _build_init(repo)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")


def _with_remote(tmp_path: Path) -> tuple[Path, Path, list[str], dict[str, str]]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    assert not repo.exists() and not remote.exists()
    _copy_template("with-remote", _build_with_remote, tmp_path)
    _git(repo, "remote", "set-url", "origin", str(remote))
    _refresh_index(repo)
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


def test_pr_mode_stops_when_the_base_branch_cannot_be_fetched(tmp_path: Path) -> None:
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
    assert any(level == "ERROR" and "git fetch" in msg for level, msg in entries)


def test_pr_mode_stops_when_the_push_fails(tmp_path: Path) -> None:
    repo, _remote, _gh, _env = _with_remote(tmp_path)
    _git(repo, "config", "remote.origin.pushurl", str(tmp_path / "missing.git"))
    _start_story(repo)
    entries, log = _logs()

    outcome = publish_story(
        repo_root=repo,
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


CHANGELOG = "# Changelog\n\n## Unreleased\n\n- Started the project.\n"


def _commit_all(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _commit_on_remote(
    tmp_path: Path, remote: Path, ref: str, files: dict[str, str], message: str
) -> None:
    """Commit on top of the remote's main from another clone; push it to ``ref``."""

    clone = tmp_path / f"clone-{ref.replace('/', '-')}"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    _git(clone, "config", "user.name", "llm-handoff tests")
    _git(clone, "config", "user.email", "tests@example.invalid")
    _git(clone, "config", "core.autocrlf", "false")
    _commit_all(clone, files, message)
    _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{ref}")


def _publish_pr(repo: Path, gh: list[str], log, **kwargs: object) -> PublishOutcome:
    options: dict[str, object] = {
        "repo_root": repo,
        "mode": "pr",
        "remote": "origin",
        "auto_merge": True,
        "wait_for_checks": False,
        "handoff_content": VERIFIED,
        "log": log,
        "gh_command": gh,
    }
    options.update(kwargs)
    return publish_story(**options)


def _resume(repo: Path, gh: list[str], log, **kwargs: object) -> PublishOutcome:
    options: dict[str, object] = {
        "repo_root": repo,
        "remote": "origin",
        "auto_merge": True,
        "wait_for_checks": False,
        "handoff_content": VERIFIED,
        "log": log,
        "gh_command": gh,
    }
    options.update(kwargs)
    return resume_paused_publish(**options)


def _resolve_readme_conflict(repo: Path) -> None:
    """What the pause message asks the operator to do."""

    merged = subprocess.run(
        ["git", "-C", str(repo), "merge", "origin/main"],
        capture_output=True,
        text=True,
    )
    assert merged.returncode != 0
    (repo / "README.md").write_text("resolved readme\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "--no-edit")


def _saved_state(repo: Path) -> dict:
    return json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))


def test_pr_mode_merges_a_moved_base_branch_before_pushing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _commit_all(
        repo,
        {".gitattributes": "CHANGELOG.md merge=union\n", "CHANGELOG.md": CHANGELOG},
        "Add a changelog",
    )
    _git(repo, "push", "-q", "origin", "main")
    _start_story(repo)
    unreleased = "## Unreleased\n\n"
    _commit_all(
        repo,
        {"CHANGELOG.md": CHANGELOG.replace(unreleased, unreleased + "- Added the widget.\n\n")},
        "W9.9 changelog",
    )
    # Another pull request merges while the story is in progress, with its
    # own bullet in the same place.
    _commit_on_remote(
        tmp_path,
        remote,
        "main",
        {
            "CHANGELOG.md": CHANGELOG.replace(
                unreleased, unreleased + "- Fixed the gadget.\n\n"
            ),
            "docs/gadget.md": "gadget\n",
        },
        "Fix the gadget",
    )
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log)

    assert outcome == PublishOutcome()
    merges = [call for call in _gh_calls(tmp_path) if call[:2] == ["pr", "merge"]]
    assert len(merges) == 1
    assert _git(repo, "branch", "--show-current") == "main"
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- Added the widget." in changelog and "- Fixed the gadget." in changelog
    assert (repo / "docs" / "gadget.md").exists()
    assert (repo / "src" / "widget.py").exists()
    assert any(
        level == "AGENT" and "Merged origin/main into" in message
        for level, message in entries
    )


def test_a_conflicting_base_branch_pauses_before_the_push_and_keeps_the_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _commit_all(repo, {"README.md": "story readme\n"}, "W9.9 readme")
    _commit_on_remote(tmp_path, remote, "main", {"README.md": "base readme\n"}, "Readme")
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert "`README.md`" in outcome.message
    assert "git merge origin/main" in outcome.message
    assert any(level == "PAUSE" for level, _message in entries)
    assert not any(level == "ERROR" for level, _message in entries)
    assert _saved_state(repo)["paused_publish"] == "push"
    assert _git(repo, "branch", "--show-current") == BRANCH
    assert _git(repo, "status", "--porcelain") == ""
    assert _gh_calls(tmp_path) == []
    remote_branches = _git(repo, "ls-remote", "--heads", "origin")
    assert BRANCH not in remote_branches

    _resolve_readme_conflict(repo)
    outcome = _resume(repo, gh, log)

    assert outcome == PublishOutcome()
    assert _git(repo, "branch", "--show-current") == "main"
    assert (repo / "README.md").read_text(encoding="utf-8") == "resolved readme\n"
    assert not (repo / STATE_PATH).exists()


def test_a_pull_request_that_is_not_mergeable_merges_after_a_base_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _commit_on_remote(
        tmp_path, remote, "next-main", {"docs/gadget.md": "gadget\n"}, "Gadget"
    )
    # main moves on the remote after the push, while the checks run.
    _set_gh(tmp_path, move_main_to="refs/heads/next-main")
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, wait_for_checks=True)

    assert outcome == PublishOutcome()
    merges = [call for call in _gh_calls(tmp_path) if call[:2] == ["pr", "merge"]]
    assert len(merges) == 2
    assert any(
        level == "WARN" and "not mergeable" in message for level, message in entries
    )
    assert _git(repo, "branch", "--show-current") == "main"
    assert (repo / "docs" / "gadget.md").exists()
    assert (repo / "src" / "widget.py").exists()


@pytest.mark.parametrize("with_review", [False, True])
def test_a_conflict_at_the_merge_pauses_and_resumes_after_the_operator_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_review: bool
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _commit_all(repo, {"README.md": "story readme\n"}, "W9.9 readme")
    _commit_on_remote(
        tmp_path, remote, "next-main", {"README.md": "base readme\n"}, "Readme"
    )
    _set_gh(tmp_path, move_main_to="refs/heads/next-main")
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, wait_for_checks=True)

    assert outcome.stop is True and outcome.exit_code == 0
    assert "`README.md`" in outcome.message and "left open" in outcome.message
    assert _saved_state(repo)["paused_publish"] == "merge"
    assert _git(repo, "branch", "--show-current") == BRANCH
    assert not any(level == "ERROR" for level, _message in entries)

    _resolve_readme_conflict(repo)
    resolution = _git(repo, "rev-parse", "HEAD")
    before = len(_gh_calls(tmp_path))
    outcome = _resume(
        repo, gh, log, wait_for_checks=True,
        review=GATE if with_review else None,
        sleep=lambda seconds: None
    )

    resumed = _gh_calls(tmp_path)[before:]
    pushed = _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not with_review:
        assert outcome == PublishOutcome()
        assert [call[:2] for call in resumed if call[0] == "pr"][-1] == ["pr", "merge"]
        assert _git(repo, "branch", "--show-current") == "main"
        assert (repo / "README.md").read_text(encoding="utf-8") == "resolved readme\n"
        return
    # Codex reviews only new pull requests and the dispatcher never asks for
    # a review, so the operator's merge is pushed and left for a person.
    assert outcome.stop is True and outcome.exit_code == 0
    assert "left open" in outcome.message and "merge it yourself" in outcome.message
    assert pushed and pushed[0] == resolution
    assert not any(call[:2] == ["pr", "merge"] for call in resumed)
    assert not any(call[0] == "api" for call in resumed)
    assert not (repo / STATE_PATH).exists()


def test_a_second_conflict_after_a_merge_step_resume_keeps_the_merge_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _commit_all(repo, {"README.md": "story readme\n"}, "W9.9 readme")
    _commit_on_remote(
        tmp_path, remote, "next-main", {"README.md": "base readme\n"}, "Readme"
    )
    _set_gh(tmp_path, move_main_to="refs/heads/next-main")
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, wait_for_checks=True)

    assert outcome.stop is True and outcome.exit_code == 0
    assert _saved_state(repo)["paused_publish"] == "merge"
    _resolve_readme_conflict(repo)
    # main moves again on the remote with another README change before the
    # operator starts the dispatcher.
    _commit_on_remote(
        tmp_path, remote, "main", {"README.md": "second base readme\n"}, "Readme 2"
    )
    before = len(_gh_calls(tmp_path))

    outcome = _resume(
        repo, gh, log, wait_for_checks=True, review=GATE, sleep=lambda seconds: None
    )

    resumed = _gh_calls(tmp_path)[before:]
    assert outcome.stop is True and outcome.exit_code == 0
    assert "`README.md`" in outcome.message and "left open" in outcome.message
    assert _saved_state(repo)["paused_publish"] == "merge"
    assert _git(repo, "branch", "--show-current") == BRANCH
    assert not any(call[:2] == ["pr", "merge"] for call in resumed)
    assert not any(level == "ERROR" for level, _message in entries)

    _resolve_readme_conflict(repo)
    resolution = _git(repo, "rev-parse", "HEAD")
    before = len(_gh_calls(tmp_path))
    outcome = _resume(
        repo, gh, log, wait_for_checks=True, review=GATE, sleep=lambda seconds: None
    )

    # The reviewed-merge guard still fires: the operator's merge is pushed
    # and left for a person instead of waiting for a review that never comes.
    resumed = _gh_calls(tmp_path)[before:]
    pushed = _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    assert outcome.stop is True and outcome.exit_code == 0
    assert "left open" in outcome.message and "merge it yourself" in outcome.message
    assert pushed and pushed[0] == resolution
    assert not any(call[:2] == ["pr", "merge"] for call in resumed)
    assert not any(call[0] == "api" for call in resumed)
    assert not (repo / STATE_PATH).exists()


def test_a_dirty_file_the_base_branch_changes_pauses_before_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _commit_on_remote(tmp_path, remote, "main", {"README.md": "base readme\n"}, "Readme")
    # Uncommitted edits outside the story: one the base branch also changes,
    # and an untracked note it does not.
    (repo / "README.md").write_text("local readme\n", encoding="utf-8")
    (repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert "would overwrite uncommitted changes to `README.md`" in outcome.message
    assert "`notes.txt`" not in outcome.message
    assert "git stash push --include-untracked -- README.md" in outcome.message
    assert "resolve the conflicts" not in outcome.message
    assert any(level == "PAUSE" for level, _message in entries)
    assert _saved_state(repo)["paused_publish"] == "push"
    assert _git(repo, "branch", "--show-current") == BRANCH
    # The operator's files are left exactly as they were.
    assert (repo / "README.md").read_text(encoding="utf-8") == "local readme\n"
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "scratch\n"
    assert _git(repo, "stash", "list") == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _gh_calls(tmp_path) == []
    assert BRANCH not in _git(repo, "ls-remote", "--heads", "origin")

    _git(repo, "stash", "push", "-q", "--include-untracked", "--", "README.md")
    outcome = _resume(repo, gh, log)

    assert outcome == PublishOutcome()
    assert _git(repo, "branch", "--show-current") == "main"
    assert (repo / "README.md").read_text(encoding="utf-8") == "base readme\n"
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "scratch\n"
    assert not (repo / STATE_PATH).exists()


def test_a_merge_git_refuses_names_the_blocking_files_not_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _commit_on_remote(tmp_path, remote, "main", {"README.md": "base readme\n"}, "Readme")
    # A staged change blocks any real merge, even in a file the base branch
    # leaves alone.
    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "add", "src/widget.py")
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert (
        "failed with `error: Your local changes to the following files would "
        "be overwritten by merge`: `src/widget.py`"
    ) in outcome.message
    assert "git stash push --include-untracked -- src/widget.py" in outcome.message
    assert "resolve the conflicts" not in outcome.message
    assert _saved_state(repo)["paused_publish"] == "push"
    assert _git(repo, "diff", "--cached", "--name-only") == "src/widget.py"
    assert _gh_calls(tmp_path) == []


def test_a_dirty_path_a_shell_would_expand_is_left_out_of_the_stash_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    risky = "notes $(id).md"
    _commit_all(repo, {risky: "notes\n"}, "Add notes")
    _git(repo, "push", "-q", "origin", "main")
    _start_story(repo)
    _commit_on_remote(tmp_path, remote, "main", {risky: "base notes\n"}, "Notes")
    (repo / risky).write_text("local notes\n", encoding="utf-8")
    _entries, log = _logs()

    outcome = _publish_pr(repo, gh, log)

    assert outcome.stop is True
    assert f"`{risky}`" in outcome.message
    assert (
        "`git stash push --include-untracked -- <the files named above>`"
        in outcome.message
    )
    assert f"-- {risky}" not in outcome.message


def test_a_failed_merge_reports_the_git_error_after_warnings() -> None:
    from llm_handoff.publish import _blocking_paths, _first_line

    merged = subprocess.CompletedProcess(
        ["git", "merge"],
        2,
        "",
        "warning: in the working copy of 'docs/handoff/HANDOFF.md', CRLF will "
        "be replaced by LF the next time Git touches it\n"
        "error: Your local changes to the following files would be "
        "overwritten by merge:\n"
        "\tREADME.md\n"
        "\tdocs/guide.md\n"
        "Please commit your changes or stash them before you merge.\n"
        "Aborting\n"
        "Merge with strategy ort failed.\n",
    )

    assert _first_line(merged) == (
        "error: Your local changes to the following files would be "
        "overwritten by merge:"
    )
    assert _blocking_paths(merged) == ["README.md", "docs/guide.md"]
    warning_only = subprocess.CompletedProcess(
        ["git"], 1, "", "warning: something\nhint: try this\nAborting\n"
    )
    assert _first_line(warning_only) == "Aborting"
    assert _blocking_paths(warning_only) == []


STATE_FILE = "PROJECT_STATE.md"
STATE_BASE = (
    "# Project State\n\n## Current Status\n\nIdle.\n\n"
    "## Epic E1\n\n- W9.9 planned\n\n## Epic E2\n\n- X1 planned\n"
)
# The story's own status edits.
STATE_STORY = STATE_BASE.replace("Idle.", "W9.9 in review.").replace(
    "- W9.9 planned", "- W9.9 done"
)
# The operator's backlog edits on the base branch while the story ran.
STATE_OPERATOR = STATE_BASE.replace("Idle.", "Regroomed E2.").replace(
    "- X1 planned", "- X1 planned\n- X2 added"
)
STATE_RESOLVED = STATE_BASE.replace("Idle.", "W9.9 in review; regroomed E2.").replace(
    "- W9.9 planned", "- W9.9 done"
).replace("- X1 planned", "- X1 planned\n- X2 added")


def _state_conflict_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_files: dict[str, str] | None = None,
    *,
    state_file: str = STATE_FILE,
    initial: dict[str, str] | None = None,
) -> tuple[Path, Path, list[str]]:
    """A story whose status edit to the state file conflicts with ``main``."""

    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _commit_all(
        repo, {state_file: STATE_BASE, **(initial or {})}, "Add the project state"
    )
    _git(repo, "push", "-q", "origin", "main")
    _start_story(repo)
    _commit_all(repo, {state_file: STATE_STORY}, "W9.9 status")
    _commit_on_remote(
        tmp_path,
        remote,
        "main",
        {state_file: STATE_OPERATOR, **(base_files or {})},
        "Regroom the backlog",
    )
    return repo, remote, gh


def _planner(
    repo: Path,
    *,
    text: str | None = STATE_RESOLVED,
    also: dict[str, str] | None = None,
    finished: bool = True,
    raises: Exception | None = None,
    path: str = STATE_FILE,
) -> tuple[StateConflictResolver, list[str]]:
    """A fake planner dispatch that writes ``text`` into the state file."""

    prompts: list[str] = []

    def run(prompt: str) -> bool:
        prompts.append(prompt)
        if text is not None:
            (repo / path).write_text(text, encoding="utf-8")
        for name, content in (also or {}).items():
            (repo / name).write_text(content, encoding="utf-8")
        if raises is not None:
            raise raises
        return finished

    return StateConflictResolver(path=path, run=run), prompts


def _merge_in_progress(repo: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            capture_output=True,
        ).returncode
        == 0
    )


def test_the_planner_resolves_a_project_state_conflict_and_publishing_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _state_conflict_story(tmp_path, monkeypatch)
    resolver, prompts = _planner(repo)
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert outcome == PublishOutcome()
    assert len(prompts) == 1
    prompt = prompts[0]
    assert f"`{STATE_FILE}`" in prompt and "`origin/main`" in prompt
    assert f"`{BRANCH}`" in prompt and "Do not run `git add`" in prompt
    assert "epic E1" in prompt
    # The pull request merged the planner's resolution.
    assert _git(repo, "branch", "--show-current") == "main"
    assert (repo / STATE_FILE).read_text(encoding="utf-8") == STATE_RESOLVED
    merge = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(merge) == 3
    # The merge commit holds only the conflict resolution: it differs from
    # the automatic merge of its parents only in the conflicted file.
    automatic = subprocess.run(
        ["git", "-C", str(repo), "merge-tree", "--write-tree", "--no-messages",
         merge[1], merge[2]],
        capture_output=True, text=True,
    ).stdout.split()[0]
    assert _git(repo, "diff", "--name-only", automatic, merge[0]) == STATE_FILE
    assert _git(repo, "log", "-1", "--format=%B", merge[0]) == (
        f"Merge remote-tracking branch 'origin/main' into {BRANCH}"
    )
    assert any(
        level == "AGENT"
        and f"Planner resolved a {STATE_FILE} merge conflict" in message
        and merge[0][:12] in message
        for level, message in entries
    )
    report = (repo / MESSAGE_PATH).read_text(encoding="utf-8")
    assert "### Merge conflict resolution" in report
    assert f"git show {merge[0][:12]} -- {STATE_FILE}" in report
    assert report.rstrip().endswith("llm-handoff)")
    creates = [call for call in _gh_calls(tmp_path) if call[:2] == ["pr", "create"]]
    assert len(creates) == 1


@pytest.mark.parametrize(
    ("planner", "problem"),
    [
        (
            {"text": STATE_RESOLVED.replace("## Epic E2", "<<<<<<< HEAD\n## Epic E2")},
            f"`{STATE_FILE}` still has conflict markers",
        ),
        ({"text": "\n\n"}, f"`{STATE_FILE}` is empty"),
        (
            {"also": {"README.md": "tidied\n"}},
            "it also changed `README.md`",
        ),
        ({"finished": False}, "the planner dispatch failed"),
        (
            {"text": None, "raises": RuntimeError("provider crashed")},
            "the planner dispatch raised RuntimeError: provider crashed",
        ),
    ],
    ids=["markers", "empty", "extra-file", "failed", "raised"],
)
def test_a_planner_resolution_that_fails_a_check_pauses_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planner: dict[str, object],
    problem: str,
) -> None:
    repo, _remote, gh = _state_conflict_story(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    resolver, prompts = _planner(repo, **planner)
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert len(prompts) == 1
    assert outcome.stop is True and outcome.exit_code == 0
    assert f"conflicts in `{STATE_FILE}`, which the planner did not resolve" in (
        outcome.message
    )
    assert problem in outcome.message
    assert "The merge was aborted" in outcome.message
    assert "git merge origin/main" in outcome.message
    assert any(
        level == "WARN" and "The planner did not resolve" in message
        for level, message in entries
    )
    assert not any(
        "Planner resolved" in message for _level, message in entries
    )
    assert not _merge_in_progress(repo)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _saved_state(repo)["paused_publish"] == "push"
    assert _gh_calls(tmp_path) == []
    assert (repo / STATE_FILE).read_text(encoding="utf-8") == STATE_STORY
    if "also" in planner:
        # The dispatcher never discards a file: the planner's stray edit stays.
        assert _git(repo, "status", "--porcelain") == "M README.md"
    else:
        assert _git(repo, "status", "--porcelain") == ""


def test_a_conflict_beyond_the_project_state_file_pauses_without_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _state_conflict_story(
        tmp_path, monkeypatch, base_files={"README.md": "base readme\n"}
    )
    _commit_all(repo, {"README.md": "story readme\n"}, "W9.9 readme")
    resolver, prompts = _planner(repo)
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert prompts == []
    assert outcome.stop is True and outcome.exit_code == 0
    assert f"`{STATE_FILE}`, `README.md`" in outcome.message
    assert "planner" not in outcome.message
    assert not _merge_in_progress(repo)
    assert _saved_state(repo)["paused_publish"] == "push"


def test_without_a_resolver_a_project_state_conflict_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _state_conflict_story(tmp_path, monkeypatch)
    _entries, log = _logs()

    outcome = _publish_pr(repo, gh, log)

    assert outcome.stop is True
    assert f"conflicts in `{STATE_FILE}`, so" in outcome.message
    assert _saved_state(repo)["paused_publish"] == "push"


def test_a_project_state_conflict_at_the_merge_is_resolved_and_commented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _commit_all(repo, {STATE_FILE: STATE_BASE}, "Add the project state")
    _git(repo, "push", "-q", "origin", "main")
    _start_story(repo)
    _commit_all(repo, {STATE_FILE: STATE_STORY}, "W9.9 status")
    # main moves after the push, while the checks run.
    _commit_on_remote(
        tmp_path, remote, "next-main", {STATE_FILE: STATE_OPERATOR}, "Regroom"
    )
    _set_gh(tmp_path, move_main_to="refs/heads/next-main")
    resolver, prompts = _planner(repo)
    entries, log = _logs()

    outcome = _publish_pr(
        repo, gh, log, wait_for_checks=True, state_resolver=resolver
    )

    assert outcome == PublishOutcome()
    assert len(prompts) == 1
    merges = [call for call in _gh_calls(tmp_path) if call[:2] == ["pr", "merge"]]
    assert len(merges) == 2
    assert (repo / STATE_FILE).read_text(encoding="utf-8") == STATE_RESOLVED
    # The pull request body was already written, so a comment records it.
    comments = [post["body"] for post in _gh_state(tmp_path)["posted"]]
    assert len(comments) == 1
    assert "### Merge conflict resolution" in comments[0]
    assert any(
        f"Planner resolved a {STATE_FILE} merge conflict" in message
        for _level, message in entries
    )


def test_a_second_project_state_conflict_in_one_publish_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh = _state_conflict_story(tmp_path, monkeypatch)
    # main moves again during the checks and conflicts with the resolution.
    _commit_on_remote(
        tmp_path,
        remote,
        "next-main",
        {STATE_FILE: STATE_OPERATOR.replace("Regroomed E2.", "Regroomed again.")},
        "Regroom again",
    )
    _set_gh(tmp_path, move_main_to="refs/heads/next-main")
    resolver, prompts = _planner(repo)
    _entries, log = _logs()

    outcome = _publish_pr(
        repo, gh, log, wait_for_checks=True, state_resolver=resolver
    )

    assert len(prompts) == 1
    assert outcome.stop is True and outcome.exit_code == 0
    assert f"conflicts in `{STATE_FILE}`, so" in outcome.message
    assert "left open" in outcome.message
    assert _saved_state(repo)["paused_publish"] == "merge"
    assert not _merge_in_progress(repo)


def test_the_conflict_resolver_dispatches_the_planner_with_its_own_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_bytes(VERIFIED.encode("utf-8"))
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    rewrite: list[bool] = []

    def fake_support(*args: object, **kwargs: object) -> SubagentResult:
        calls.append((args, kwargs))
        if rewrite:
            handoff.write_text("rewritten\n", encoding="utf-8")
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.0)

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    entries, log = _logs()
    config = _pr_loop_config(tmp_path)

    resolver = orchestrator._state_conflict_resolver(config, log)

    assert resolver is not None and resolver.path == STATE_FILE
    assert resolver.run("resolve it") is True
    (name, prompt), kwargs = calls[0]
    assert name == "planner" and prompt == "resolve it"
    assert kwargs["role"] == "planner"
    assert kwargs["agent_config"] == config.agents["planner"]
    # A planner that rewrites the handoff fails, and the handoff is restored.
    rewrite.append(True)
    assert resolver.run("resolve it") is False
    assert handoff.read_bytes() == VERIFIED.encode("utf-8")
    assert any(level == "WARN" and "restored it" in message for level, message in entries)
    off = _pr_loop_config(
        tmp_path, publish={"mode": "pr", "resolve_project_state_conflicts": "off"}
    )
    assert orchestrator._state_conflict_resolver(off, log) is None
    dry = _pr_loop_config(tmp_path, dry_run=True)
    assert orchestrator._state_conflict_resolver(dry, log) is None


NOTES_FILE = "docs/notes.md"


def _index_tampering_planner(
    repo: Path, tamper: str
) -> tuple[StateConflictResolver, list[str]]:
    """A planner that resolves the state file and also edits the index."""

    prompts: list[str] = []

    def run(prompt: str) -> bool:
        prompts.append(prompt)
        (repo / STATE_FILE).write_text(STATE_RESOLVED, encoding="utf-8")
        if tamper == "blob":
            # Swap the staged blob, keep the working-tree bytes, and hide the
            # difference from `git status`.
            blob = subprocess.run(
                ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                input="planted\n",
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            _git(repo, "update-index", "--cacheinfo", f"100644,{blob},{NOTES_FILE}")
            _git(repo, "update-index", "--assume-unchanged", NOTES_FILE)
        elif tamper == "mode":
            _git(repo, "update-index", "--chmod=+x", NOTES_FILE)
            _git(repo, "update-index", "--assume-unchanged", NOTES_FILE)
        else:
            (repo / "extra.txt").write_text("extra\n", encoding="utf-8")
            _git(repo, "add", "--", "extra.txt")
        return True

    return StateConflictResolver(path=STATE_FILE, run=run), prompts


@pytest.mark.parametrize(
    ("tamper", "changed"),
    [("blob", NOTES_FILE), ("mode", NOTES_FILE), ("extra", "extra.txt")],
)
def test_a_planner_that_changes_another_index_entry_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str, changed: str
) -> None:
    repo, _remote, gh = _state_conflict_story(
        tmp_path, monkeypatch, base_files={NOTES_FILE: "base notes\n"}
    )
    head = _git(repo, "rev-parse", "HEAD")
    resolver, prompts = _index_tampering_planner(repo, tamper)
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert len(prompts) == 1
    assert outcome.stop is True and outcome.exit_code == 0
    assert f"conflicts in `{STATE_FILE}`, which the planner did not resolve" in (
        outcome.message
    )
    assert f"`{changed}`" in outcome.message
    assert not any("Planner resolved" in message for _level, message in entries)
    # Nothing was committed: HEAD is still the story head.
    assert _git(repo, "rev-parse", "HEAD") == head
    if tamper == "blob":
        # `git merge --abort` refuses to drop a staged blob that differs from
        # both the file and HEAD, so the pause says to finish it by hand.
        assert "`git merge --abort` failed" in outcome.message
    else:
        assert "The merge was aborted" in outcome.message
        assert not _merge_in_progress(repo)
    assert _saved_state(repo)["paused_publish"] == "push"
    assert _gh_calls(tmp_path) == []


def _hidden_edit_planner(
    repo: Path, flag: str, *, edit: bool = True
) -> tuple[StateConflictResolver, list[str]]:
    """A planner that resolves the state file and hides an edit behind a flag."""

    prompts: list[str] = []

    def run(prompt: str) -> bool:
        prompts.append(prompt)
        (repo / STATE_FILE).write_text(STATE_RESOLVED, encoding="utf-8")
        _git(repo, "update-index", flag, NOTES_FILE)
        if edit:
            (repo / NOTES_FILE).write_text("planted\n", encoding="utf-8")
        return True

    return StateConflictResolver(path=STATE_FILE, run=run), prompts


@pytest.mark.parametrize(
    ("flag", "edit", "problem"),
    [
        ("--assume-unchanged", True, "it also changed `docs/notes.md`"),
        ("--skip-worktree", True, "it also changed `docs/notes.md`"),
        ("--assume-unchanged", False, "index flags of `docs/notes.md`"),
    ],
    ids=["assume-unchanged-edit", "skip-worktree-edit", "flag-only"],
)
def test_a_planner_edit_hidden_behind_an_index_flag_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    edit: bool,
    problem: str,
) -> None:
    repo, _remote, gh = _state_conflict_story(
        tmp_path, monkeypatch, initial={NOTES_FILE: "base notes\n"}
    )
    head = _git(repo, "rev-parse", "HEAD")
    resolver, prompts = _hidden_edit_planner(repo, flag, edit=edit)
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert len(prompts) == 1
    assert outcome.stop is True and outcome.exit_code == 0
    assert f"conflicts in `{STATE_FILE}`, which the planner did not resolve" in (
        outcome.message
    )
    assert f"`{NOTES_FILE}`" in outcome.message
    assert problem in outcome.message
    assert "index flags" in outcome.message
    assert not any("Planner resolved" in message for _level, message in entries)
    # Nothing was committed: HEAD is still the story head.
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _saved_state(repo)["paused_publish"] == "push"
    assert _gh_calls(tmp_path) == []


def test_a_pre_existing_index_flag_does_not_block_the_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _state_conflict_story(
        tmp_path, monkeypatch, initial={NOTES_FILE: "base notes\n"}
    )
    _git(repo, "update-index", "--assume-unchanged", NOTES_FILE)
    resolver, prompts = _planner(repo)
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert outcome == PublishOutcome()
    assert len(prompts) == 1
    assert (repo / STATE_FILE).read_text(encoding="utf-8") == STATE_RESOLVED
    assert any(
        f"Planner resolved a {STATE_FILE} merge conflict" in message
        for _level, message in entries
    )


def test_the_worktree_snapshot_sees_hidden_edits_and_missing_files(
    tmp_path: Path,
) -> None:
    from llm_handoff import publish

    repo = tmp_path / "repo"
    _init(repo)
    _commit_all(
        repo, {NOTES_FILE: "notes\n", "docs/gone.md": "gone\n"}, "Add the docs"
    )
    _git(repo, "update-index", "--skip-worktree", NOTES_FILE)
    before = publish._worktree_snapshot(repo)
    assert before is not None
    assert before[NOTES_FILE] == (
        "", "file", hashlib.sha256((repo / NOTES_FILE).read_bytes()).hexdigest()
    )
    assert before["README.md"][:2] == ("", "file")

    (repo / NOTES_FILE).write_text("planted\n", encoding="utf-8")
    (repo / "docs" / "gone.md").unlink()
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    after = publish._worktree_snapshot(repo)

    assert after is not None
    # `git status` hides the skip-worktree edit, but the digest changes.
    assert after[NOTES_FILE][0] == ""
    assert after[NOTES_FILE][2] == (
        hashlib.sha256((repo / NOTES_FILE).read_bytes()).hexdigest()
    )
    assert after[NOTES_FILE][2] != before[NOTES_FILE][2]
    assert after["docs/gone.md"] == (" D", "missing", "")
    assert after["new.txt"][:2] == ("??", "file")
    assert publish._worktree_snapshot(tmp_path / "not-a-repo") is None

    flags = publish._index_flags(repo)
    assert flags is not None
    assert flags[NOTES_FILE] == (False, True)
    assert flags["README.md"] == (False, False)
    _git(repo, "update-index", "--assume-unchanged", "README.md")
    changed = publish._index_flags(repo)
    assert changed is not None
    assert publish._index_flags_changed(flags, changed) == ["README.md"]


def test_the_worktree_snapshot_sees_a_symlink_type_change(tmp_path: Path) -> None:
    from llm_handoff import publish

    repo = tmp_path / "repo"
    _init(repo)
    before = publish._worktree_snapshot(repo)
    assert before is not None
    (repo / "README.md").unlink()
    try:
        os.symlink("elsewhere", repo / "README.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    after = publish._worktree_snapshot(repo)

    assert after is not None
    assert after["README.md"][1:] == (
        "symlink", hashlib.sha256(b"elsewhere").hexdigest()
    )
    assert before["README.md"] != after["README.md"]


def test_the_worktree_snapshot_sees_hidden_submodule_changes(tmp_path: Path) -> None:
    from llm_handoff import publish

    repo = tmp_path / "repo"
    _init(repo)
    sub = repo / "vendor" / "lib"
    _build_init(sub)
    # Adding an embedded repository tracks it as a gitlink entry.
    _git(repo, "add", "vendor/lib")
    _git(repo, "commit", "-q", "-m", "Add the submodule")
    assert _git(repo, "ls-files", "--stage", "vendor/lib").startswith("160000 ")
    # Hide every submodule change from `git status`, and start it dirty.
    _git(repo, "config", "diff.ignoreSubmodules", "all")
    (sub / "README.md").write_text("dirty\n", encoding="utf-8")
    before = publish._worktree_snapshot(repo)
    assert before is not None
    assert before["vendor/lib"][1] == "submodule"

    # The same status, but different bytes inside the submodule.
    (sub / "README.md").write_text("planted\n", encoding="utf-8")
    edited = publish._worktree_snapshot(repo)
    assert edited is not None
    assert edited["vendor/lib"][0] == before["vendor/lib"][0]
    assert edited["vendor/lib"] != before["vendor/lib"]

    # A new commit in the submodule moves its HEAD.
    (sub / "README.md").write_text("dirty\n", encoding="utf-8")
    assert publish._worktree_snapshot(repo) == before
    _git(sub, "commit", "-q", "--allow-empty", "-m", "moved")
    moved = publish._worktree_snapshot(repo)
    assert moved is not None
    assert moved["vendor/lib"] != before["vendor/lib"]

    # A checkout that is not a repository cannot be compared: fail closed.
    (sub / ".git").rename(tmp_path / "moved-git")
    assert publish._worktree_snapshot(repo) is None
    # An empty directory is a submodule that is not checked out.
    for child in sub.iterdir():
        child.unlink()
    empty = publish._worktree_snapshot(repo)
    assert empty is not None
    assert empty["vendor/lib"][1:] == ("directory", "")


def _deny_reads(monkeypatch: pytest.MonkeyPatch, target: Path, call: str) -> None:
    """Make one read call in the publish module fail for ``target`` only."""

    from llm_handoff import publish

    def denied(path: object) -> bool:
        return Path(os.fspath(path)) == target  # type: ignore[arg-type]

    if call == "read_bytes":
        original_read = Path.read_bytes

        def read_bytes(self: Path) -> bytes:
            if denied(self):
                raise PermissionError(13, "Permission denied", str(self))
            return original_read(self)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        return
    original = getattr(publish.os, call)

    def fail(path: object, *args: object, **kwargs: object) -> object:
        if denied(path):
            raise PermissionError(13, "Permission denied", os.fspath(path))  # type: ignore[arg-type]
        return original(path, *args, **kwargs)

    monkeypatch.setattr(publish.os, call, fail)


@pytest.mark.parametrize("call", ["read_bytes", "lstat"])
def test_the_worktree_snapshot_fails_closed_on_an_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    from llm_handoff import publish

    repo = tmp_path / "repo"
    _init(repo)
    _commit_all(
        repo, {NOTES_FILE: "notes\n", "docs/gone.md": "gone\n"}, "Add the docs"
    )
    # A deleted tracked file is still a readable fact, not a failure.
    (repo / "docs" / "gone.md").unlink()
    readable = publish._worktree_snapshot(repo)
    assert readable is not None
    assert readable["docs/gone.md"] == (" D", "missing", "")

    _deny_reads(monkeypatch, repo / NOTES_FILE, call)

    assert publish._path_fingerprint(repo / NOTES_FILE) is None
    assert publish._worktree_snapshot(repo) is None
    assert publish._path_fingerprint(repo / "docs" / "gone.md") == ("missing", "")


def test_the_worktree_snapshot_fails_closed_on_an_unreadable_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_handoff import publish

    repo = tmp_path / "repo"
    _init(repo)
    (repo / "README.md").unlink()
    try:
        os.symlink("elsewhere", repo / "README.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    assert publish._worktree_snapshot(repo) is not None

    _deny_reads(monkeypatch, repo / "README.md", "readlink")

    assert publish._path_fingerprint(repo / "README.md") is None
    assert publish._worktree_snapshot(repo) is None


def test_a_file_unreadable_after_the_planner_pauses_the_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _state_conflict_story(
        tmp_path, monkeypatch, initial={NOTES_FILE: "base notes\n"}
    )
    head = _git(repo, "rev-parse", "HEAD")
    prompts: list[str] = []

    def run(prompt: str) -> bool:
        prompts.append(prompt)
        (repo / STATE_FILE).write_text(STATE_RESOLVED, encoding="utf-8")
        # The file cannot be read once the planner is done, so the
        # dispatcher cannot tell whether the planner changed it.
        _deny_reads(monkeypatch, repo / NOTES_FILE, "read_bytes")
        return True

    resolver = StateConflictResolver(path=STATE_FILE, run=run)
    entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert len(prompts) == 1
    assert outcome.stop is True and outcome.exit_code == 0
    assert f"conflicts in `{STATE_FILE}`, which the planner did not resolve" in (
        outcome.message
    )
    assert "the checkout could not be read" in outcome.message
    assert "The merge was aborted" in outcome.message
    assert not any("Planner resolved" in message for _level, message in entries)
    assert not _merge_in_progress(repo)
    # No merge commit: HEAD is still the story head.
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _saved_state(repo)["paused_publish"] == "push"
    assert _gh_calls(tmp_path) == []


def test_a_file_unreadable_before_the_planner_pauses_without_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _state_conflict_story(
        tmp_path, monkeypatch, initial={NOTES_FILE: "base notes\n"}
    )
    head = _git(repo, "rev-parse", "HEAD")
    _deny_reads(monkeypatch, repo / NOTES_FILE, "read_bytes")
    resolver, prompts = _planner(repo)
    _entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert prompts == []
    assert outcome.stop is True and outcome.exit_code == 0
    assert "the merge state could not be read" in outcome.message
    assert not _merge_in_progress(repo)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _saved_state(repo)["paused_publish"] == "push"
    assert _gh_calls(tmp_path) == []


def test_the_index_flag_snapshot_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_handoff import publish

    def fake_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["git", *args], 0, "H\0", "")

    monkeypatch.setattr(publish, "_git", fake_git)
    assert publish._index_flags(tmp_path) is None


def test_a_staged_resolution_with_another_entry_is_not_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_handoff import publish

    before = {
        (STATE_FILE, "1"): ("100644", "a" * 40),
        (STATE_FILE, "2"): ("100644", "b" * 40),
        (STATE_FILE, "3"): ("100644", "c" * 40),
        (NOTES_FILE, "0"): ("100644", "d" * 40),
    }
    resolved = {
        (STATE_FILE, "0"): ("100644", "e" * 40),
        (NOTES_FILE, "0"): before[(NOTES_FILE, "0")],
    }
    check = publish._staged_resolution_problem

    assert check(before, resolved, STATE_FILE) == ""
    assert "could not be read" in check(before, None, STATE_FILE)
    assert f"`{NOTES_FILE}`" in check(
        before, {**resolved, (NOTES_FILE, "0"): ("100644", "f" * 40)}, STATE_FILE
    )
    assert "`extra.txt`" in check(
        before, {**resolved, ("extra.txt", "0"): ("100644", "f" * 40)}, STATE_FILE
    )
    assert "not staged as one resolved file" in check(
        before, {**resolved, (STATE_FILE, "2"): before[(STATE_FILE, "2")]}, STATE_FILE
    )
    assert "not staged as one resolved file" in check(
        before, {**resolved, (STATE_FILE, "0"): ("120000", "e" * 40)}, STATE_FILE
    )
    # A failed `git ls-files` fails closed.
    monkeypatch.setattr(
        publish,
        "_git",
        lambda repo_root, args: subprocess.CompletedProcess(args, 128, "", "fatal"),
    )
    assert publish._index_entries(tmp_path) is None


def _handoff_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planner: object,
) -> tuple[StateConflictResolver, list[tuple[str, str]]]:
    def fake_support(*args: object, **kwargs: object) -> SubagentResult:
        planner()  # type: ignore[operator]
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.0)

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    entries, log = _logs()
    resolver = orchestrator._state_conflict_resolver(_pr_loop_config(tmp_path), log)
    assert resolver is not None
    return resolver, entries


def test_the_conflict_resolver_restores_the_handoff_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff.parent.mkdir(parents=True)
    saved = VERIFIED.replace("\n", "\r\n").encode("utf-8") + b"caf\xc3\xa9\r\n"
    handoff.write_bytes(saved)
    resolver, entries = _handoff_resolver(
        tmp_path,
        monkeypatch,
        lambda: handoff.write_text("rewritten\n", encoding="utf-8"),
    )

    assert resolver.run("resolve it") is False

    assert handoff.read_bytes() == saved
    assert any(level == "WARN" and "restored it" in message for level, message in entries)


def _link_directory(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform != "win32":
        pytest.skip("cannot create a directory symlink here")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _assert_not_restored(entries: list[tuple[str, str]]) -> None:
    warnings = [
        message
        for level, message in entries
        if level == "WARN" and "docs/handoff/HANDOFF.md" in message
    ]
    assert len(warnings) == 1
    assert "was not restored because a symlink or junction" in warnings[0]


def test_the_conflict_resolver_does_not_write_through_a_handoff_file_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    handoff = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_bytes(VERIFIED.encode("utf-8"))
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside bytes\n")
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a file symlink here")
    probe.unlink()

    def plant_link() -> None:
        handoff.unlink()
        handoff.symlink_to(outside)

    resolver, entries = _handoff_resolver(repo, monkeypatch, plant_link)

    assert resolver.run("resolve it") is False

    assert outside.read_bytes() == b"outside bytes\n"
    assert handoff.is_symlink()
    _assert_not_restored(entries)


def test_the_conflict_resolver_does_not_write_through_a_handoff_directory_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    handoff = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_bytes(VERIFIED.encode("utf-8"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "HANDOFF.md").write_bytes(b"outside bytes\n")
    # Skips here, not inside the dispatch, when no directory link can be made.
    probe = tmp_path / "probe"
    _link_directory(probe, outside)
    if sys.platform == "win32":
        os.rmdir(probe)
    else:
        probe.unlink()

    def plant_link() -> None:
        shutil.rmtree(repo / "docs" / "handoff")
        _link_directory(repo / "docs" / "handoff", outside)

    resolver, entries = _handoff_resolver(repo, monkeypatch, plant_link)

    assert resolver.run("resolve it") is False

    assert (outside / "HANDOFF.md").read_bytes() == b"outside bytes\n"
    assert sorted(item.name for item in outside.iterdir()) == ["HANDOFF.md"]
    _assert_not_restored(entries)


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("a\n<<<<<<< HEAD\nb\n", True),
        ("a\r\n=======\r\nb\r\n", True),
        ("a\n>>>>>>> origin/main\n", True),
        ("a\n|||||||\n", True),
        ("a <<<<<<< b\n", False),
        ("Title\n========\n", False),
        ("- item\n", False),
    ],
)
def test_conflict_markers_are_found_at_line_starts(text: str, found: bool) -> None:
    from llm_handoff.publish import _conflict_marker_re

    assert bool(_conflict_marker_re(7).search(text)) is found
    wide = _conflict_marker_re(12)
    assert wide.search("a\n<<<<<<<<<<<< HEAD\n")
    assert wide.search("a\n============\n")
    assert not wide.search("a\n=======\n")


def test_a_marker_of_the_configured_size_left_behind_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _state_conflict_story(
        tmp_path,
        monkeypatch,
        initial={".gitattributes": f"{STATE_FILE} conflict-marker-size=12\n"},
    )
    resolver, prompts = _planner(
        repo,
        text=STATE_RESOLVED.replace(
            "## Epic E2", ">>>>>>>>>>>> origin/main\n## Epic E2"
        ),
    )
    _entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert len(prompts) == 1
    assert outcome.stop is True
    assert f"`{STATE_FILE}` still has conflict markers" in outcome.message
    assert not _merge_in_progress(repo)


def test_the_planner_resolves_a_conflict_in_a_non_ascii_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Git quotes this name in its default output (core.quotePath).
    state_file = "ÉTAT.md"
    repo, _remote, gh = _state_conflict_story(
        tmp_path, monkeypatch, state_file=state_file
    )
    resolver, prompts = _planner(repo, path=state_file)
    _entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, state_resolver=resolver)

    assert outcome == PublishOutcome()
    assert len(prompts) == 1
    assert (repo / state_file).read_text(encoding="utf-8") == STATE_RESOLVED


def test_resolve_project_state_conflicts_config(tmp_path: Path) -> None:
    assert DispatchConfig(repo_root=tmp_path).publish.resolve_project_state_conflicts == (
        "planner"
    )
    with pytest.raises(ValueError, match="with quotes"):
        DispatchConfig(
            repo_root=tmp_path,
            publish={"mode": "pr", "resolve_project_state_conflicts": False},
        )
    with pytest.raises(ValueError):
        DispatchConfig(
            repo_root=tmp_path,
            publish={"mode": "pr", "resolve_project_state_conflicts": "always"},
        )


def test_resume_does_nothing_without_a_paused_publish(tmp_path: Path) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    entries, log = _logs()

    outcome = _resume(tmp_path, ["gh-should-not-run"], log)

    assert outcome == PublishOutcome()
    assert entries == []


def test_resume_skips_a_paused_publish_off_its_branch(tmp_path: Path) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    state = _saved_state(tmp_path)
    state["paused_publish"] = "push"
    (tmp_path / STATE_PATH).write_text(json.dumps(state), encoding="utf-8")
    _git(tmp_path, "switch", "-q", "main")
    entries, log = _logs()

    outcome = _resume(tmp_path, ["gh-should-not-run"], log)

    assert outcome == PublishOutcome()
    assert [level for level, _message in entries] == ["WARN"]
    assert _saved_state(tmp_path)["paused_publish"] == ""


def _pause_on_readme_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> tuple[Path, list[str], str]:
    """Pause a ``pr`` publish at ``step``; return the repo, gh, and the head."""

    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _commit_all(repo, {"README.md": "story readme\n"}, "W9.9 readme")
    if step == "merge":
        _commit_on_remote(
            tmp_path, remote, "next-main", {"README.md": "base readme\n"}, "Readme"
        )
        _set_gh(tmp_path, move_main_to="refs/heads/next-main")
    else:
        _commit_on_remote(
            tmp_path, remote, "main", {"README.md": "base readme\n"}, "Readme"
        )
    head = _git(repo, "rev-parse", "HEAD")
    _entries, log = _logs()

    outcome = _publish_pr(repo, gh, log, wait_for_checks=step == "merge")

    assert outcome.stop is True and outcome.exit_code == 0
    assert _saved_state(repo)["paused_publish"] == step
    return repo, gh, head


@pytest.mark.parametrize("step", ["push", "merge"])
def test_a_publish_pause_records_the_story_branch_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    repo, _gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, step)

    assert _saved_state(repo)["paused_head"] == head
    assert _git(repo, "rev-parse", "HEAD") == head
    if step == "merge":
        pushed = _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}").split()
        assert pushed and pushed[0] == head


def test_a_resume_with_a_non_merge_commit_after_a_push_pause_pushes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, "push")
    _resolve_readme_conflict(repo)
    _commit_all(repo, {"src/extra.py": "EXTRA = 1\n"}, "Unreviewed change")
    extra = _git(repo, "rev-parse", "--short", "HEAD")
    entries, log = _logs()

    outcome = _resume(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert f"`{extra} Unreviewed change`" in outcome.message
    assert f"`{BRANCH}` was not pushed" in outcome.message
    assert "git switch main" in outcome.message
    # The base branch commit that the operator's merge brought in is allowed.
    assert "1 commit(s) other than merges" in outcome.message
    assert [level for level, _message in entries] == ["PAUSE"]
    state = _saved_state(repo)
    assert state["paused_publish"] == "push" and state["paused_head"] == head
    assert BRANCH not in _git(repo, "ls-remote", "--heads", "origin")
    assert _gh_calls(tmp_path) == []


def test_a_resume_with_a_non_merge_commit_after_a_merge_pause_does_not_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, "merge")
    _resolve_readme_conflict(repo)
    _commit_all(repo, {"src/extra.py": "EXTRA = 1\n"}, "Unreviewed change")
    extra = _git(repo, "rev-parse", "--short", "HEAD")
    remote_head = _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}")
    before = len(_gh_calls(tmp_path))
    entries, log = _logs()

    outcome = _resume(repo, gh, log, wait_for_checks=True)

    assert outcome.stop is True and outcome.exit_code == 0
    assert extra in outcome.message
    assert [level for level, _message in entries] == ["PAUSE"]
    assert _gh_calls(tmp_path)[before:] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}") == remote_head
    state = _saved_state(repo)
    assert state["paused_publish"] == "merge" and state["paused_head"] == head


_MISSING = object()


@pytest.mark.parametrize("paused_head", [_MISSING, 1234567, ["abc1234"]])
def test_a_resume_from_a_state_without_a_paused_head_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paused_head: object
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    # A state saved by an older version, before paused_head existed, or a
    # hand-edited one whose paused_head is not a string.
    state = _saved_state(repo)
    state["paused_publish"] = "push"
    state.pop("paused_head", None)
    if paused_head is not _MISSING:
        state["paused_head"] = paused_head
    (repo / STATE_PATH).write_text(json.dumps(state), encoding="utf-8")
    entries, log = _logs()

    outcome = _resume(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert f"`{BRANCH}` was not pushed" in outcome.message
    assert "older version" in outcome.message
    assert [level for level, _message in entries] == ["PAUSE"]
    assert _saved_state(repo) == state
    assert _gh_calls(tmp_path) == []
    assert BRANCH not in _git(repo, "ls-remote", "--heads", "origin")


def test_a_resume_after_the_story_branch_was_reset_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, "push")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    _commit_all(repo, {"README.md": "rewritten readme\n"}, "Rewritten readme")
    entries, log = _logs()

    outcome = _resume(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert head[:12] in outcome.message and "reset or rewritten" in outcome.message
    assert [level for level, _message in entries] == ["PAUSE"]
    state = _saved_state(repo)
    assert state["paused_publish"] == "push" and state["paused_head"] == head
    assert BRANCH not in _git(repo, "ls-remote", "--heads", "origin")
    assert _gh_calls(tmp_path) == []


def _assert_resume_paused(
    tmp_path: Path,
    repo: Path,
    step: str,
    head: str,
    outcome: PublishOutcome,
    entries: list[tuple[str, str]],
    gh_before: int,
    remote_head: str,
) -> None:
    assert outcome.stop is True and outcome.exit_code == 0
    assert f"`{BRANCH}` was not pushed" in outcome.message
    assert [level for level, _message in entries] == ["PAUSE"]
    state = _saved_state(repo)
    assert state["paused_publish"] == step and state["paused_head"] == head
    assert _gh_calls(tmp_path)[gh_before:] == []
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}") == remote_head
    if step == "push":
        assert BRANCH not in _git(repo, "ls-remote", "--heads", "origin")


@pytest.mark.parametrize("step", ["push", "merge"])
def test_a_resume_whose_merge_adds_an_unrelated_file_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, step)
    merged = subprocess.run(
        ["git", "-C", str(repo), "merge", "origin/main"],
        capture_output=True,
        text=True,
    )
    assert merged.returncode != 0
    (repo / "README.md").write_text("resolved readme\n", encoding="utf-8")
    (repo / "src" / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")
    _git(repo, "add", "README.md", "src/extra.py")
    _git(repo, "commit", "-q", "--no-edit")
    merge = _git(repo, "show", "-s", "--format=%h %s", "HEAD")
    remote_head = _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}")
    before = len(_gh_calls(tmp_path))
    entries, log = _logs()

    outcome = _resume(repo, gh, log, wait_for_checks=step == "merge")

    _assert_resume_paused(
        tmp_path, repo, step, head, outcome, entries, before, remote_head
    )
    assert f"`{merge}`" in outcome.message
    beyond, _sep, conflicted = outcome.message.partition(
        "It also changes the conflicted"
    )
    assert conflicted, outcome.message
    assert "changes `src/extra.py` beyond the conflict resolution" in beyond
    assert "`README.md`" not in beyond
    assert conflicted.startswith(" path `README.md`;")
    assert "does not inspect edits inside conflicted files" in conflicted
    assert "review it" in conflicted
    assert "`src/extra.py`" not in conflicted
    assert "only the conflict resolution" in outcome.message
    assert "git switch main" in outcome.message


def _merge_keeping_the_automatic_readme(repo: Path, files: dict[str, str]) -> str:
    """Merge origin/main, keep merge-tree's README.md as is, and add ``files``.

    The merge changes no conflicted path relative to the automatic merge of
    its parents; only ``files`` differ from it. Return ``%h %s`` of the merge.
    """

    # The resume check passes full object IDs, which merge-tree also uses as
    # the conflict marker labels, so pass the same here.
    automatic = subprocess.run(
        [
            "git", "-C", str(repo), "merge-tree", "--write-tree", "--no-messages",
            _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "origin/main"),
        ],
        capture_output=True,
        text=True,
    )
    assert automatic.returncode == 1, automatic.stderr
    tree = automatic.stdout.splitlines()[0].strip()
    merged = subprocess.run(
        ["git", "-C", str(repo), "merge", "origin/main"],
        capture_output=True,
        text=True,
    )
    assert merged.returncode != 0
    _git(repo, "checkout", tree, "--", "README.md")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "--no-edit")
    return _git(repo, "show", "-s", "--format=%h %s", "HEAD")


def test_a_resume_whose_merge_adds_a_file_but_keeps_conflicted_files_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, "push")
    merge = _merge_keeping_the_automatic_readme(
        repo, {"src/extra.py": "EXTRA = 1\n"}
    )
    entries, log = _logs()

    outcome = _resume(repo, gh, log)

    _assert_resume_paused(tmp_path, repo, "push", head, outcome, entries, 0, "")
    assert f"`{merge}`" in outcome.message
    assert "changes `src/extra.py` beyond the conflict resolution" in outcome.message
    assert "`README.md`" not in outcome.message
    assert "It also changes the conflicted" not in outcome.message
    assert "does not inspect edits" not in outcome.message


def test_a_resume_lists_paths_up_to_the_path_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_handoff.publish as publish_module

    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, "push")
    extra = {f"src/extra{index}.py": f"EXTRA = {index}\n" for index in range(4)}
    _merge_keeping_the_automatic_readme(repo, extra)
    monkeypatch.setattr(publish_module, "_RESUME_LISTED_PATHS", 2)
    assert publish_module._RESUME_LISTED_COMMITS == 5
    entries, log = _logs()

    outcome = _resume(repo, gh, log)

    _assert_resume_paused(tmp_path, repo, "push", head, outcome, entries, 0, "")
    assert (
        "changes `src/extra0.py`, `src/extra1.py`, and 2 more beyond the "
        "conflict resolution: the automatic merge of its parents did not leave "
        "those paths conflicted"
    ) in outcome.message
    assert "`src/extra2.py`" not in outcome.message
    assert "`src/extra3.py`" not in outcome.message


@pytest.mark.parametrize("step", ["push", "merge"])
def test_a_resume_whose_merge_edits_only_the_conflicted_file_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    repo, gh, _head = _pause_on_readme_conflict(tmp_path, monkeypatch, step)
    _resolve_readme_conflict(repo)
    entries, log = _logs()

    outcome = _resume(repo, gh, log, wait_for_checks=step == "merge")

    assert outcome == PublishOutcome()
    assert not any(level in {"PAUSE", "ERROR"} for level, _message in entries)
    assert _git(repo, "branch", "--show-current") == "main"
    assert (repo / "README.md").read_text(encoding="utf-8") == "resolved readme\n"
    assert not (repo / STATE_PATH).exists()


@pytest.mark.parametrize("step", ["push", "merge"])
def test_a_resume_whose_merge_does_not_bring_in_the_base_branch_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, step)
    # A merge of a side branch whose commit is already on the story branch:
    # no commit other than the merge is added, and the base is not merged.
    side = _git(repo, "rev-parse", "HEAD~1")
    merge = _git(
        repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-p", side, "-m",
        "Merge a side branch",
    )
    _git(repo, "merge", "-q", "--ff-only", merge)
    remote_head = _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}")
    before = len(_gh_calls(tmp_path))
    entries, log = _logs()

    outcome = _resume(repo, gh, log, wait_for_checks=step == "merge")

    _assert_resume_paused(
        tmp_path, repo, step, head, outcome, entries, before, remote_head
    )
    assert "Merge a side branch" in outcome.message
    assert "does not merge origin/main" in outcome.message
    assert side[:12] in outcome.message
    assert "Parent order matters" in outcome.message
    assert "must be the merge's second parent" in outcome.message
    assert "`git merge origin/main`" in outcome.message
    assert "base branch as its first parent, is refused" in outcome.message


@pytest.mark.parametrize("step", ["push", "merge"])
def test_a_resume_whose_merge_has_three_parents_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, step)
    _resolve_readme_conflict(repo)
    upstream = _git(repo, "rev-parse", "origin/main")
    octopus = _git(
        repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD^1", "-p", upstream,
        "-p", "HEAD~2", "-m", "Octopus merge",
    )
    _git(repo, "reset", "-q", "--hard", octopus)
    remote_head = _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}")
    before = len(_gh_calls(tmp_path))
    entries, log = _logs()

    outcome = _resume(repo, gh, log, wait_for_checks=step == "merge")

    _assert_resume_paused(
        tmp_path, repo, step, head, outcome, entries, before, remote_head
    )
    assert "Octopus merge" in outcome.message
    assert "has 3 parents" in outcome.message


@pytest.mark.parametrize("step", ["push", "merge"])
def test_a_resume_pauses_when_git_merge_tree_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    import llm_handoff.publish as publish_module

    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, step)
    _resolve_readme_conflict(repo)
    remote_head = _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}")
    before = len(_gh_calls(tmp_path))
    real_git = publish_module._git

    def failing_merge_tree(repo_root: Path, args: list[str]):
        if args and args[0] == "merge-tree":
            return subprocess.CompletedProcess(
                ["git", *args], 129, "", "error: unknown option `write-tree'"
            )
        return real_git(repo_root, args)

    monkeypatch.setattr(publish_module, "_git", failing_merge_tree)
    entries, log = _logs()

    outcome = _resume(repo, gh, log, wait_for_checks=step == "merge")

    _assert_resume_paused(
        tmp_path, repo, step, head, outcome, entries, before, remote_head
    )
    assert "git merge-tree failed" in outcome.message
    assert "unknown option" in outcome.message


@pytest.mark.parametrize(
    ("returncode", "stdout"), [(1, ""), (0, "not a tree\n")]
)
def test_a_resume_pauses_when_git_merge_tree_prints_no_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    import llm_handoff.publish as publish_module

    repo, gh, head = _pause_on_readme_conflict(tmp_path, monkeypatch, "push")
    _resolve_readme_conflict(repo)
    real_git = publish_module._git

    def odd_merge_tree(repo_root: Path, args: list[str]):
        if args and args[0] == "merge-tree":
            return subprocess.CompletedProcess(["git", *args], returncode, stdout, "")
        return real_git(repo_root, args)

    monkeypatch.setattr(publish_module, "_git", odd_merge_tree)
    entries, log = _logs()

    outcome = _resume(repo, gh, log)

    _assert_resume_paused(tmp_path, repo, "push", head, outcome, entries, 0, "")
    assert "git merge-tree printed no tree" in outcome.message


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


def _api(**pages: list) -> dict:
    return {
        "issues/7/comments": pages.get("comments", []),
        "pulls/7/reviews": pages.get("reviews", []),
        "pulls/7/comments": pages.get("review_comments", []),
        "issues/7/reactions": pages.get("reactions", []),
    }


def _set_api(tmp_path: Path, **pages: list) -> None:
    path = tmp_path / "gh_state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state["api"] = _api(**pages)
    path.write_text(json.dumps(state), encoding="utf-8")


def _gh_state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "gh_state.json").read_text(encoding="utf-8"))


def _set_threads(tmp_path: Path, *threads: dict) -> None:
    path = tmp_path / "gh_state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state["threads"] = list(threads)
    path.write_text(json.dumps(state), encoding="utf-8")


def _publish_with_review(
    repo: Path, gh: list[str], log, gate: ReviewGate = GATE, review_prompt=None
):
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
        review_prompt=review_prompt,
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
        {
            "number": 1,
            "comment_id": 4279958834,
            "location": "src/widget.py:1",
            "body": "**P1 Handle a missing widget**\n\nWIDGET can be unset.",
        }
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


# The fake `gh` stamps each posted comment 2026-10-19T00:10:00Z.
BEFORE_REQUEST = "2026-10-19T00:05:00Z"
AFTER_REQUEST = "2026-10-19T00:20:00Z"
REVIEW_REQUEST = "@codex review"


def _codex_failed_summary(sha: str, updated_at: str = BEFORE_REQUEST) -> dict:
    return {
        "user": {"login": BOT},
        "updated_at": updated_at,
        "body": "<!-- codex-pull-request-review-summary -->\n\n"
        "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
        f"| **Code Review** | **Failed** | `{sha[:7]}` | PR opened |\n",
    }


def _after_review_requests(tmp_path: Path, *apis: dict) -> None:
    path = tmp_path / "gh_state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state["after_review_request"] = list(apis)
    path.write_text(json.dumps(state), encoding="utf-8")


def _posted(tmp_path: Path) -> list[str]:
    path = tmp_path / "gh_state.json"
    if not path.exists():
        return []
    posted = json.loads(path.read_text(encoding="utf-8")).get("posted", [])
    assert all(item["endpoint"] == "repos/{owner}/{repo}/issues/7/comments" for item in posted)
    return [item["body"] for item in posted]


class _Prompt:
    """Stands in for the operator prompt: answers in order, records reasons."""

    def __init__(self, repo: Path, *answers: str) -> None:
        self.repo = repo
        self.answers = list(answers)
        self.reasons: list[str] = []
        self.states: list[dict] = []

    def __call__(self, reason: str) -> str:
        self.reasons.append(reason)
        self.states.append(_saved_state(self.repo))
        return self.answers.pop(0)


def _failed_review_story(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _set_api(tmp_path, comments=[_codex_failed_summary(head)])
    return repo, gh, head


def test_a_failed_codex_review_is_requested_once_more_and_merges_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    clean = {**_codex_summary(head), "updated_at": AFTER_REQUEST}
    _after_review_requests(tmp_path, _api(comments=[clean]))
    entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log)

    assert outcome == PublishOutcome()
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    kinds = [call[:2] for call in _gh_calls(tmp_path)]
    assert ["pr", "comment"] not in kinds
    assert kinds[-1] == ["pr", "merge"]
    assert REVIEW_REQUEST not in (repo / "logs/dispatch/publish/message.md").read_text(
        encoding="utf-8"
    )
    assert any("asked Codex to review" in message for _level, message in entries)


def test_a_retried_review_that_fails_without_a_terminal_pauses_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_failed_summary(head, AFTER_REQUEST)])
    )
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert "failed" in outcome.message
    assert "Review it and merge it yourself" in outcome.message
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert not (repo / STATE_PATH).exists()


def test_a_retried_review_that_fails_asks_the_operator_and_quit_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_failed_summary(head, AFTER_REQUEST)])
    )
    prompt = _Prompt(repo, "quit")
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    [reason] = prompt.reasons
    assert "failed" in reason and "`merge`" in reason and "`retry`" in reason
    # The retry is on record before the prompt, so a restart does not repeat it.
    assert prompt.states[0]["review_retry_head"] == head
    assert prompt.states[0]["review_retry_at"] == "2026-10-19T00:10:00+00:00"
    assert outcome.stop is True and outcome.exit_code == 0
    assert "Review it and merge it yourself" in outcome.message
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_merge_at_the_review_prompt_merges_without_a_review_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_failed_summary(head, AFTER_REQUEST)])
    )
    prompt = _Prompt(repo, "merge")
    entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    assert outcome == PublishOutcome()
    request, note = _posted(tmp_path)
    assert request == REVIEW_REQUEST
    # Posted before the merge, so it states the decision, not a done merge.
    assert "Merged" not in note
    assert f"without a Codex review of `{head[:12]}`" in note
    assert "operator chose `merge`" in note
    assert "@" not in note.replace("⁠", "")
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]
    assert _git(repo, "branch", "--show-current") == "main"
    assert any(
        level == "WARN" and "WITHOUT a Codex review" in message
        for level, message in entries
    )


def test_retry_at_the_review_prompt_asks_codex_again_and_merges_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    clean = {**_codex_summary(head), "updated_at": "2026-10-19T00:30:00Z"}
    _after_review_requests(
        tmp_path,
        _api(comments=[_codex_failed_summary(head, AFTER_REQUEST)]),
        _api(comments=[clean]),
    )
    prompt = _Prompt(repo, "retry")
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    assert outcome == PublishOutcome()
    assert len(prompt.reasons) == 1
    assert _posted(tmp_path) == [REVIEW_REQUEST, REVIEW_REQUEST]
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]


def test_a_review_timeout_is_requested_once_more_then_asks_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _set_api(tmp_path)
    prompt = _Prompt(repo, "quit")
    entries, log = _logs()

    outcome = _publish_with_review(
        repo,
        gh,
        log,
        gate=ReviewGate(reviewers=("codex",), timeout_seconds=0),
        review_prompt=prompt,
    )

    assert any(
        level == "AGENT"
        and message.startswith("no completed Codex review")
        and "asked Codex to review" in message
        for level, message in entries
    )
    [reason] = prompt.reasons
    assert "no completed Codex review" in reason
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert outcome.stop is True and "left open" in outcome.message


# Stalls on the first poll that sees the review running.
STALL_GATE = ReviewGate(reviewers=("codex",), timeout_seconds=600, stall_seconds=0)
STALL_MERGE_GATE = ReviewGate(
    reviewers=("codex",), timeout_seconds=600, stall_seconds=0, stall_action="merge"
)


def _codex_running_summary(sha: str, updated_at: str = BEFORE_REQUEST) -> dict:
    """A summary that shows the review of ``sha`` running, as on a stall."""

    return {
        "user": {"login": BOT},
        "updated_at": updated_at,
        "body": "<!-- codex-pull-request-review-summary -->\n"
        '<!-- codex-review-state:v1 {"code_review":{"commit_sha":"'
        + sha
        + '","has_any_findings":null,"has_unresolved_findings":null,'
        '"status":"running"},"security_review":null} -->\n\n'
        "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
        f"| **Code Review** | **Running** | `{sha[:7]}` | PR opened |\n",
    }


def _stalled_review_story(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _set_api(tmp_path, comments=[_codex_running_summary(head)])
    return repo, gh, head


def _audited(head: str) -> str:
    """The audit's verified_pass for ``head``, as the auditor writes it."""

    return VERIFIED.replace("close_type: story\n", f"close_type: story\nscope_sha: {head}\n")


def _publish_stalled(
    repo: Path,
    gh: list[str],
    log,
    gate: ReviewGate,
    handoff: str,
    review_prompt=None,
) -> PublishOutcome:
    return _publish_pr(
        repo,
        gh,
        log,
        handoff_content=handoff,
        review=gate,
        sleep=lambda seconds: None,
        review_prompt=review_prompt,
    )


def test_a_stalled_review_is_requested_once_more_and_merges_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _stalled_review_story(tmp_path, monkeypatch)
    clean = {**_codex_summary(head), "updated_at": AFTER_REQUEST}
    _after_review_requests(tmp_path, _api(comments=[clean]))
    prompt = _Prompt(repo)
    entries, log = _logs()

    outcome = _publish_stalled(repo, gh, log, STALL_GATE, VERIFIED, prompt)

    assert outcome == PublishOutcome()
    assert prompt.reasons == []
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]
    assert any(
        level == "AGENT"
        and message.startswith(
            f"the Codex review of {head[:12]} has shown as running for at least 0s"
        )
        and "asked Codex to review" in message
        for level, message in entries
    )


def test_a_stall_after_the_retry_asks_the_operator_with_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _stalled_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_running_summary(head, AFTER_REQUEST)])
    )
    prompt = _Prompt(repo, "quit")
    _entries, log = _logs()

    outcome = _publish_stalled(repo, gh, log, STALL_GATE, _audited(head), prompt)

    [reason] = prompt.reasons
    assert "has shown as running" in reason
    assert "`merge`" in reason and "`retry`" in reason
    assert prompt.states[0]["review_retry_head"] == head
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert outcome.stop is True and "left open" in outcome.message
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_a_stall_after_the_retry_pauses_without_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _stalled_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_running_summary(head, AFTER_REQUEST)])
    )
    entries, log = _logs()

    outcome = _publish_stalled(repo, gh, log, STALL_GATE, _audited(head))

    assert outcome.stop is True and outcome.exit_code == 0
    assert "has shown as running" in outcome.message
    assert "Review it and merge it yourself" in outcome.message
    assert any(level == "PAUSE" for level, _message in entries)
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_a_stall_after_the_retry_merges_with_the_merge_stall_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _stalled_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_running_summary(head, AFTER_REQUEST)])
    )
    prompt = _Prompt(repo)
    entries, log = _logs()

    outcome = _publish_stalled(repo, gh, log, STALL_MERGE_GATE, _audited(head), prompt)

    assert outcome == PublishOutcome()
    assert prompt.reasons == []
    request, note = _posted(tmp_path)
    assert request == REVIEW_REQUEST
    assert note.startswith(
        f"Merging without a Codex review: the Codex review of {head[:12]} has "
        "shown as running"
    )
    assert "`publish.review_stall_action` is `merge`" in note
    assert "@" not in note.replace("\u2060", "")
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]
    assert _git(repo, "branch", "--show-current") == "main"
    assert any(
        level == "WARN"
        and f"WITHOUT a Codex review of {head[:12]}" in message
        and "review_stall_action is `merge`" in message
        for level, message in entries
    )


def test_a_failed_retry_merges_with_the_merge_stall_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_failed_summary(head, AFTER_REQUEST)])
    )
    _entries, log = _logs()

    outcome = _publish_stalled(repo, gh, log, STALL_MERGE_GATE, _audited(head))

    assert outcome == PublishOutcome()
    request, note = _posted(tmp_path)
    assert request == REVIEW_REQUEST
    assert note.startswith("Merging without a Codex review: Codex reported")
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]


def test_the_merge_stall_action_is_refused_while_a_thread_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _stalled_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_running_summary(head, AFTER_REQUEST)])
    )
    _set_threads(tmp_path, {"id": "T1", "isResolved": False, "comment_id": 1})
    prompt = _Prompt(repo, "quit")
    entries, log = _logs()

    outcome = _publish_stalled(repo, gh, log, STALL_MERGE_GATE, _audited(head), prompt)

    [reason] = prompt.reasons
    assert "did not merge it without a Codex review: 1 review thread(s) are unresolved" in reason
    assert any(
        level == "WARN" and "was not merged without a Codex review" in message
        for level, message in entries
    )
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert outcome.stop is True
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_the_merge_stall_action_needs_a_passed_audit_of_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _stalled_review_story(tmp_path, monkeypatch)
    _after_review_requests(
        tmp_path, _api(comments=[_codex_running_summary(head, AFTER_REQUEST)])
    )
    _entries, log = _logs()

    # The handoff names no scope_sha, so nothing ties the audit to the head.
    outcome = _publish_stalled(repo, gh, log, STALL_MERGE_GATE, VERIFIED)

    assert outcome.stop is True
    assert "HANDOFF.md does not record a passed audit" in outcome.message
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_the_merge_stall_action_never_has_the_planner_resolve_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _commit_all(repo, {STATE_FILE: STATE_BASE}, "Add the project state")
    _git(repo, "push", "-q", "origin", "main")
    _start_story(repo)
    _commit_all(repo, {STATE_FILE: STATE_STORY}, "W9.9 status")
    head = _git(repo, "rev-parse", "HEAD")
    # main moves after the push, while the checks run, and conflicts with the
    # story only in the project state file.
    _commit_on_remote(
        tmp_path, remote, "next-main", {STATE_FILE: STATE_OPERATOR}, "Regroom"
    )
    _set_gh(tmp_path, move_main_to="refs/heads/next-main")
    _set_api(tmp_path, comments=[_codex_running_summary(head)])
    _after_review_requests(
        tmp_path, _api(comments=[_codex_running_summary(head, AFTER_REQUEST)])
    )
    resolver, prompts = _planner(repo)
    _entries, log = _logs()

    outcome = _publish_pr(
        repo,
        gh,
        log,
        handoff_content=_audited(head),
        review=STALL_MERGE_GATE,
        sleep=lambda seconds: None,
        wait_for_checks=True,
        state_resolver=resolver,
    )

    # The planner's resolution would be neither the audited commit nor a
    # clean base-branch merge, so the merge pauses instead.
    assert prompts == []
    assert outcome.stop is True
    assert f"conflicts in `{STATE_FILE}`" in outcome.message
    merges = [call for call in _gh_calls(tmp_path) if call[:2] == ["pr", "merge"]]
    assert len(merges) == 1
    assert _gh_state(tmp_path)["pr"] == "OPEN"
    assert not _merge_in_progress(repo)


def test_the_merge_stall_action_keeps_the_usage_limit_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    usage_limit = {
        "user": {"login": BOT},
        "created_at": "2099-01-01T00:00:00Z",
        "body": "You have reached your Codex usage limits for code reviews.",
    }
    _set_api(tmp_path, comments=[_codex_failed_summary(head), usage_limit])
    prompt = _Prompt(repo, "quit")
    _entries, log = _logs()

    outcome = _publish_stalled(repo, gh, log, STALL_MERGE_GATE, _audited(head), prompt)

    [reason] = prompt.reasons
    assert "usage limit" in reason
    assert _posted(tmp_path) == []
    assert outcome.stop is True
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_findings_after_a_stall_retry_take_the_correction_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _stalled_review_story(tmp_path, monkeypatch)
    completed = {**_codex_summary(head), "updated_at": AFTER_REQUEST}
    _after_review_requests(
        tmp_path,
        _api(comments=[completed], review_comments=[_codex_finding(head)]),
    )
    _entries, log = _logs()

    outcome = _publish_stalled(repo, gh, log, STALL_MERGE_GATE, _audited(head))

    assert outcome.stop is False and outcome.handoff is not None
    assert "status: verified_fail" in outcome.handoff
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert _saved_state(repo)["review_rounds"] == 1


@pytest.mark.parametrize("action", ["prompt", "merge"])
def test_a_restart_after_a_stall_retry_does_not_request_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    repo, gh, head = _stalled_review_story(tmp_path, monkeypatch)
    _record_review_retry(repo, head)
    # The retried review still showed running when the dispatcher restarted.
    _set_api(tmp_path, comments=[_codex_running_summary(head, AFTER_REQUEST)])
    prompt = _Prompt(repo, "quit")
    _entries, log = _logs()
    gate = STALL_MERGE_GATE if action == "merge" else STALL_GATE

    outcome = _publish_stalled(repo, gh, log, gate, _audited(head), prompt)

    assert REVIEW_REQUEST not in _posted(tmp_path)
    if action == "merge":
        assert outcome == PublishOutcome()
        assert prompt.reasons == []
        [note] = _posted(tmp_path)
        assert note.startswith("Merging without a Codex review:")
        assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]
    else:
        assert len(prompt.reasons) == 1
        assert _posted(tmp_path) == []
        assert outcome.stop is True


def _codex_unattributed_summary(updated_at: str) -> dict:
    """A Completed summary whose marker and Commit cell name no commit."""

    return {
        "user": {"login": BOT},
        "updated_at": updated_at,
        "body": "<!-- codex-pull-request-review-summary -->\n"
        '<!-- codex-review-state:v1 {"code_review":{"commit_sha":null,'
        '"has_any_findings":null,"has_unresolved_findings":null,'
        '"status":"completed"},"security_review":null} -->\n\n'
        "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
        "| **Code Review** | **Completed** | — | PR opened |\n",
    }


def test_a_review_that_names_no_commit_is_retried_once_then_asks_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _set_api(tmp_path, comments=[_codex_unattributed_summary("2099-01-01T00:00:00Z")])
    _after_review_requests(
        tmp_path, _api(comments=[_codex_unattributed_summary(AFTER_REQUEST)])
    )
    prompt = _Prompt(repo, "quit")
    entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    cause = (
        "Codex marked a review completed without naming a commit, so the "
        f"dispatcher cannot tell it reviewed {head[:12]}"
    )
    assert any(
        level == "AGENT" and message.startswith(f"{cause}; asked Codex to review")
        for level, message in entries
    )
    assert not any("as failed" in message for _level, message in entries)
    [reason] = prompt.reasons
    assert f"left open because {cause}." in reason
    assert "`merge`" in reason and "`retry`" in reason
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert outcome.stop is True and cause in outcome.message
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_a_review_that_names_no_commit_merges_when_the_retry_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    head = _git(repo, "rev-parse", "HEAD")
    _set_api(tmp_path, comments=[_codex_unattributed_summary("2099-01-01T00:00:00Z")])
    clean = {**_codex_summary(head), "updated_at": AFTER_REQUEST}
    _after_review_requests(tmp_path, _api(comments=[clean]))
    prompt = _Prompt(repo)
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    assert outcome == PublishOutcome()
    assert prompt.reasons == []
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]


def test_a_usage_limit_failure_goes_straight_to_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    usage_limit = {
        "user": {"login": BOT},
        "created_at": "2099-01-01T00:00:00Z",
        "body": "You have reached your Codex usage limits for code reviews.",
    }
    _set_api(tmp_path, comments=[_codex_failed_summary(head), usage_limit])
    prompt = _Prompt(repo, "quit")
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    [reason] = prompt.reasons
    assert "usage limit" in reason
    assert _posted(tmp_path) == []
    assert outcome.stop is True


def test_a_restart_after_a_review_retry_does_not_request_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    state = _saved_state(repo)
    (repo / STATE_PATH).write_text(
        json.dumps(
            {
                **state,
                "review_retry_head": head,
                "review_retry_at": "2026-10-19T00:10:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    # The retried review failed while the dispatcher was not running.
    _set_api(tmp_path, comments=[_codex_failed_summary(head, AFTER_REQUEST)])
    prompt = _Prompt(repo, "quit")
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    assert len(prompt.reasons) == 1
    assert _posted(tmp_path) == []
    assert outcome.stop is True


def _record_review_retry(repo: Path, head: str) -> None:
    (repo / STATE_PATH).write_text(
        json.dumps(
            {
                **_saved_state(repo),
                "review_retry_head": head,
                "review_retry_at": "2026-10-19T00:10:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def test_startup_resumes_the_wait_for_a_retried_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dispatcher stopped while it waited for the review it asked for; the
    # handoff is still the audit's verified_pass.
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    _git(repo, "push", "-q", "-u", "origin", BRANCH)
    _record_review_retry(repo, head)
    clean = {**_codex_summary(head), "updated_at": AFTER_REQUEST}
    _set_api(tmp_path, comments=[clean])
    state = _gh_state(tmp_path)
    (tmp_path / "gh_state.json").write_text(
        json.dumps({**state, "pr": "OPEN"}), encoding="utf-8"
    )
    entries, log = _logs()

    outcome = _resume(repo, gh, log, review=GATE, sleep=lambda seconds: None)

    assert outcome == PublishOutcome()
    assert _posted(tmp_path) == []
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]
    assert _git(repo, "branch", "--show-current") == "main"
    assert any("Resuming the wait" in message for _level, message in entries)


@pytest.mark.parametrize("moved", ["handoff", "head"])
def test_startup_does_not_resume_a_retry_the_story_moved_past(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moved: str
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    _record_review_retry(repo, head)
    handoff = VERIFIED
    if moved == "handoff":
        handoff = VERIFIED.replace("status: verified_pass", "status: verified_fail")
    else:
        (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "W9.9 Another change")
    _entries, log = _logs()

    outcome = _resume(repo, gh, log, review=GATE, handoff_content=handoff)

    assert outcome == PublishOutcome()
    assert _gh_calls(tmp_path) == []


def test_a_restart_waits_for_the_retried_review_not_the_old_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    state = _saved_state(repo)
    (repo / STATE_PATH).write_text(
        json.dumps(
            {
                **state,
                "review_retry_head": head,
                "review_retry_at": "2026-10-19T00:10:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    # Codex has not touched the summary since the retry request yet.
    prompt = _Prompt(repo, "quit")
    _entries, log = _logs()

    outcome = _publish_with_review(
        repo,
        gh,
        log,
        gate=ReviewGate(reviewers=("codex",), timeout_seconds=0),
        review_prompt=prompt,
    )

    [reason] = prompt.reasons
    assert "no completed Codex review" in reason
    assert _posted(tmp_path) == []
    assert outcome.stop is True


def test_findings_from_a_retried_review_bounce_without_another_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, head = _failed_review_story(tmp_path, monkeypatch)
    completed = {**_codex_summary(head), "updated_at": AFTER_REQUEST}
    _after_review_requests(
        tmp_path,
        _api(comments=[completed], review_comments=[_codex_finding(head)]),
    )
    prompt = _Prompt(repo)
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    assert outcome.stop is False and outcome.handoff is not None
    assert "status: verified_fail" in outcome.handoff
    assert prompt.reasons == []
    assert _posted(tmp_path) == [REVIEW_REQUEST]
    assert _saved_state(repo)["review_rounds"] == 1


def test_a_review_request_that_cannot_be_posted_asks_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, gh, _head = _failed_review_story(tmp_path, monkeypatch)
    state = _gh_state(tmp_path)
    (tmp_path / "gh_state.json").write_text(
        json.dumps({**state, "fail_comment": True}), encoding="utf-8"
    )
    prompt = _Prompt(repo, "quit")
    _entries, log = _logs()

    outcome = _publish_with_review(repo, gh, log, review_prompt=prompt)

    [reason] = prompt.reasons
    assert "Asking Codex to review it again failed" in reason
    assert outcome.stop is True


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
        {"mode": "pr", "wait_for_reviewers": ["codex"], "review_stall_seconds": 0},
        {"mode": "pr", "wait_for_reviewers": ["codex"], "review_stall_action": "quit"},
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
    assert config.publish.review_stall_seconds == 600
    assert config.publish.review_stall_action == "prompt"


def test_the_review_stall_keys_reach_the_review_gate(tmp_path: Path) -> None:
    config = DispatchConfig(
        repo_root=tmp_path,
        enforce_work_packet=True,
        commit_mode="dispatcher",
        publish={
            "mode": "pr",
            "wait_for_reviewers": ["codex"],
            "review_stall_seconds": 300,
            "review_stall_action": "merge",
        },
    )

    gate = orchestrator._review_gate(config)

    assert gate is not None
    assert (gate.stall_seconds, gate.stall_action) == (300, "merge")


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


EPIC_CLOSE = VERIFIED.replace("close_type: story", "close_type: epic")


def test_pr_epic_close_with_everything_pushed_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    entries, log = _logs()

    outcome = _publish_pr(
        repo, gh, log, handoff_content=EPIC_CLOSE, branch_prefix="llm-handoff/"
    )

    assert outcome.stop is False
    assert _gh_calls(tmp_path) == []
    assert _git(repo, "branch", "--show-current") == "main"
    assert any(
        level == "INFO" and "nothing to publish" in message
        for level, message in entries
    )


def test_pr_epic_close_with_an_unpushed_commit_still_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _commit_all(repo, {"src/widget.py": "WIDGET = 1\n"}, "unpublished work")
    _entries, log = _logs()

    outcome = _publish_pr(
        repo, gh, log, handoff_content=EPIC_CLOSE, branch_prefix="llm-handoff/"
    )

    assert outcome.stop is True and outcome.exit_code == 1
    assert "no story state" in outcome.message
    assert _gh_calls(tmp_path) == []


def test_merge_commit_epic_close_without_story_state_publishes_nothing(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    _entries, log = _logs()

    outcome = publish_story(
        repo_root=tmp_path,
        mode="merge-commit",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=EPIC_CLOSE,
        log=log,
        base_branch="main",
    )

    assert outcome.stop is False
    assert _git(tmp_path, "rev-parse", "HEAD") == head


def test_pr_epic_close_on_a_pushed_story_branch_without_state_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _start_story(repo)
    _git(repo, "push", "-q", "-u", "origin", BRANCH)
    (repo / STATE_PATH).unlink()
    _entries, log = _logs()

    outcome = _publish_pr(
        repo, gh, log, handoff_content=EPIC_CLOSE, branch_prefix="llm-handoff/"
    )

    assert outcome.stop is True and outcome.exit_code == 1
    assert "no story state" in outcome.message
    assert _gh_calls(tmp_path) == []


HANDOFF_PATH = Path("docs/handoff/HANDOFF.md")


def _operator_story_on_main(repo: Path) -> tuple[str, str]:
    """Commit an operator story on main, unpushed, with the handoff uncommitted.

    Returns the remote main and the operator commit.
    """

    upstream = _git(repo, "rev-parse", "HEAD")
    _commit_all(
        repo, {".claude/agents/widget.md": "operator edit\n"}, "W9.9 Add a widget"
    )
    head = _git(repo, "rev-parse", "HEAD")
    (repo / HANDOFF_PATH).parent.mkdir(parents=True, exist_ok=True)
    (repo / HANDOFF_PATH).write_text("live handoff\n", encoding="utf-8")
    return upstream, head


def _operator_audit(scope_sha: str) -> str:
    return VERIFIED.replace(
        "close_type: story", f"scope_sha: {scope_sha}\nclose_type: story"
    )


def _publish_operator_story(
    repo: Path, gh: list[str], log, scope_sha: str, **kwargs: object
) -> PublishOutcome:
    options: dict[str, object] = {
        "handoff_content": _operator_audit(scope_sha),
        "base_branch": "main",
        "branch_prefix": "llm-handoff/",
        "handoff_path": HANDOFF_PATH,
    }
    options.update(kwargs)
    return _publish_pr(repo, gh, log, **options)


def test_a_story_the_operator_committed_on_main_moves_to_a_story_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    upstream, head = _operator_story_on_main(repo)
    entries, log = _logs()

    outcome = _publish_operator_story(repo, gh, log, head[:12], auto_merge=False)

    assert outcome.stop is True and outcome.exit_code == 0
    assert _git(repo, "branch", "--show-current") == BRANCH
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "rev-parse", "main") == upstream
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}") == head
    assert _git(remote, "rev-parse", "refs/heads/main") == upstream
    assert (repo / HANDOFF_PATH).read_text(encoding="utf-8") == "live handoff\n"
    calls = _gh_calls(tmp_path)
    assert [call[:2] for call in calls] == [["pr", "view"], ["pr", "create"]]
    create = calls[1]
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == BRANCH
    body = Path(create[create.index("--body-file") + 1]).read_text(encoding="utf-8")
    assert "the operator (a story routed to `user`)" in body
    assert any(
        level == "INFO" and f"Created `{BRANCH}`" in message
        for level, message in entries
    )


def test_an_adopted_operator_story_merges_and_returns_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    upstream, head = _operator_story_on_main(repo)
    _entries, log = _logs()

    outcome = _publish_operator_story(repo, gh, log, head)

    assert outcome == PublishOutcome()
    assert [call[:2] for call in _gh_calls(tmp_path) if call[0] == "pr"] == [
        ["pr", "view"],
        ["pr", "create"],
        ["pr", "view"],
        ["pr", "merge"],
    ]
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "HEAD") == head != upstream
    assert _git(remote, "rev-parse", "refs/heads/main") == head
    assert _git(repo, "branch", "--list", BRANCH) == ""
    assert not (repo / STATE_PATH).exists()
    assert (repo / HANDOFF_PATH).read_text(encoding="utf-8") == "live handoff\n"


def _merge_commit_on_main(repo: Path) -> None:
    _git(repo, "switch", "-q", "-c", "side")
    _commit_all(repo, {"side.md": "side\n"}, "side work")
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")


def _handoff_in_the_operator_commit(repo: Path) -> None:
    _git(repo, "add", HANDOFF_PATH.as_posix())
    _git(repo, "commit", "-q", "--amend", "--no-edit")


@pytest.mark.parametrize(
    ("arrange", "scope", "options", "warning"),
    [
        (lambda repo: None, "upstream", {}, "is not the handoff's scope_sha"),
        (
            lambda repo: (repo / "README.md").write_text("edit\n", encoding="utf-8"),
            "head",
            {},
            "uncommitted files other than",
        ),
        (_merge_commit_on_main, "head", {}, "include a merge commit"),
        (_handoff_in_the_operator_commit, "head", {}, "would put live handoff state"),
        (
            lambda repo: _commit_all(repo, {"more.md": "more\n"}, "unaudited"),
            "head",
            {},
            "commit(s) ahead of origin/main, not one",
        ),
        (
            lambda repo: _git(repo, "branch", BRANCH, "HEAD~1"),
            "head",
            {},
            "already exists",
        ),
        (lambda repo: None, "head", {"handoff_path": None}, None),
        (
            lambda repo: None,
            "head",
            {"handoff_content": VERIFIED.replace(
                "close_type: story", "scope_sha: {head}\nclose_type: epic"
            )},
            None,
        ),
    ],
    ids=[
        "scope-is-not-head",
        "other-dirty-file",
        "merge-commit",
        "commit-changes-handoff",
        "two-commits",
        "branch-exists",
        "no-handoff-path",
        "epic-close",
    ],
)
def test_an_operator_story_on_main_that_does_not_fit_still_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrange,
    scope: str,
    options: dict[str, object],
    warning: str | None,
) -> None:
    repo, remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    upstream, _head = _operator_story_on_main(repo)
    arrange(repo)
    head = _git(repo, "rev-parse", "HEAD")
    if "handoff_content" in options:
        options = {
            **options,
            "handoff_content": str(options["handoff_content"]).format(head=head),
        }
    entries, log = _logs()

    outcome = _publish_operator_story(
        repo, gh, log, head if scope == "head" else upstream, **options
    )

    assert outcome.stop is True and outcome.exit_code == 1
    assert "no story state" in outcome.message
    assert _gh_calls(tmp_path) == []
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "main") == head
    assert _git(remote, "rev-parse", "refs/heads/main") == upstream
    assert not (repo / STATE_PATH).exists()
    warnings = [message for level, message in entries if level == "WARN"]
    if warning is None:
        assert warnings == []
    else:
        assert len(warnings) == 1 and warning in warnings[0]


def _start_empty_story(repo: Path) -> None:
    """Cut the story branch and its state with no implementer commit."""

    error = ensure_story_branch(
        repo_root=repo,
        prefix="llm-handoff/",
        base_branch=None,
        assignment_content=ASSIGNMENT,
    )
    assert error is None


def test_pr_epic_close_on_an_empty_story_branch_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed epic-close audit sent the close to backend, which cut the story
    # branch and had nothing to change; main then moved on the remote.
    repo, remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _start_empty_story(repo)
    _commit_on_remote(tmp_path, remote, "main", {"NEWS.md": "news\n"}, "moved")
    (repo / "README.md").write_text("uncommitted edit\n", encoding="utf-8")
    entries, log = _logs()

    outcome = _publish_pr(
        repo, gh, log, handoff_content=EPIC_CLOSE, branch_prefix="llm-handoff/"
    )

    assert outcome == PublishOutcome()
    assert _gh_calls(tmp_path) == []
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "main") == _git(repo, "rev-parse", "origin/main")
    assert _git(repo, "branch", "--list", BRANCH) == ""
    assert _git(remote, "branch", "--list", BRANCH) == ""
    assert not (repo / STATE_PATH).exists()
    assert (repo / "README.md").read_text(encoding="utf-8") == "uncommitted edit\n"
    assert any(
        level == "INFO" and "nothing to publish" in message
        for level, message in entries
    )


def test_pr_epic_close_with_story_commits_still_opens_a_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _start_story(repo)
    _entries, log = _logs()

    outcome = _publish_pr(
        repo,
        gh,
        log,
        handoff_content=EPIC_CLOSE,
        auto_merge=False,
        branch_prefix="llm-handoff/",
    )

    assert outcome.stop is True and outcome.exit_code == 0
    assert [call[:2] for call in _gh_calls(tmp_path)] == [
        ["pr", "view"],
        ["pr", "create"],
    ]


def test_pr_epic_close_on_an_empty_story_branch_stops_for_unpushed_base_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _start_empty_story(repo)
    _git(repo, "switch", "-q", "main")
    _commit_all(repo, {"local.md": "local\n"}, "unpushed on main")
    local_main = _git(repo, "rev-parse", "main")
    _git(repo, "switch", "-q", BRANCH)
    _entries, log = _logs()

    outcome = _publish_pr(
        repo, gh, log, handoff_content=EPIC_CLOSE, branch_prefix="llm-handoff/"
    )

    assert outcome.stop is True and outcome.exit_code == 1
    assert "has commits that are not on origin/main" in outcome.message
    assert _gh_calls(tmp_path) == []
    assert _git(repo, "branch", "--show-current") == BRANCH
    assert _git(repo, "rev-parse", "main") == local_main
    assert (repo / STATE_PATH).exists()


@pytest.mark.parametrize(
    ("checkout", "options"),
    [
        ("side", {"base_branch": "main", "branch_prefix": "llm-handoff/"}),
        ("main", {}),
    ],
    ids=["not-the-base-branch", "base-branch-unknown"],
)
def test_merge_commit_epic_close_off_the_base_branch_stops(
    tmp_path: Path, checkout: str, options: dict[str, str]
) -> None:
    _init(tmp_path)
    if checkout != "main":
        _git(tmp_path, "switch", "-q", "-c", checkout)
    _entries, log = _logs()

    outcome = publish_story(
        repo_root=tmp_path,
        mode="merge-commit",
        remote="origin",
        auto_merge=True,
        wait_for_checks=False,
        handoff_content=EPIC_CLOSE,
        log=log,
        **options,
    )

    assert outcome.stop is True and outcome.exit_code == 1
    assert "no story state" in outcome.message


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


def _fix_without_a_response(
    tmp_path: Path, repo: Path, gh: list[str], log, *threads: dict
) -> PublishOutcome:
    """Bounce one Codex finding, then publish a fix with no saved response."""

    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(
        tmp_path,
        {"id": "T1", "isResolved": False, "comment_id": 4279958834},
        *threads,
    )
    assert _publish_with_review(repo, gh, log).handoff is not None

    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    _set_api(tmp_path, comments=[_codex_summary(_git(repo, "rev-parse", "HEAD"))])
    assert not (repo / RESPONSES_PATH).exists()
    return _publish_with_review(repo, gh, log)


def test_a_finding_without_a_response_goes_to_the_planner_with_its_thread_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    entries, log = _logs()

    outcome = _fix_without_a_response(tmp_path, repo, gh, log)

    assert outcome.stop is False and outcome.handoff is not None
    assert _gh_state(tmp_path)["threads"][0]["isResolved"] is False
    assert "replies" not in _gh_state(tmp_path)
    assert _resolve_calls(tmp_path) == []
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert _git(repo, "branch", "--show-current") == BRANCH
    assert any("No review response for finding 1" in m for _l, m in entries)
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    assert state["deferral_planner_pass"] is True
    assert state["deferred_findings"] == []
    assert [finding["number"] for finding in state["open_findings"]] == [1]

    handoff = outcome.handoff
    decision = route(handoff)
    assert decision.route == "planner" and decision.warnings == []
    assert "story_id: W9.9" in handoff
    assert "### Finding 1: `src/widget.py:1`" in handoff
    assert "WIDGET can be unset." in handoff
    assert "No response from `backend` was saved for this finding" in handoff
    assert "Response from `backend`:" not in handoff
    assert "## Deferred Finding Decisions" in handoff


def test_a_finding_without_a_response_next_to_a_persons_comment_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    entries, log = _logs()

    # T2 is a person's comment, not a Codex finding.
    outcome = _fix_without_a_response(
        tmp_path, repo, gh, log, {"id": "T2", "isResolved": False, "comment_id": 555}
    )

    assert outcome.stop is True and outcome.exit_code == 0
    assert outcome.handoff is None
    assert "2 review thread(s)" in outcome.message
    assert "replies" not in _gh_state(tmp_path)
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert any(level == "PAUSE" for level, _ in entries)


DEFERRED_RESPONSE = "**Deferred:** out of scope for this story; needs its own packet."


def _defer_the_finding(
    tmp_path: Path, repo: Path, gh: list[str], log
) -> PublishOutcome:
    """Bounce one Codex finding, then publish a fix that defers it."""

    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834})
    assert _publish_with_review(repo, gh, log).handoff is not None

    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Tidy the widget")
    _set_api(tmp_path, comments=[_codex_summary(_git(repo, "rev-parse", "HEAD"))])
    save_review_responses(
        repo,
        f"## Review Responses\n\n### Finding 1: `src/widget.py:1`\n{DEFERRED_RESPONSE}\n",
    )
    return _publish_with_review(repo, gh, log)


def _resolve_calls(tmp_path: Path) -> list[list[str]]:
    return [
        call
        for call in _gh_calls(tmp_path)
        if call[-1] == "graphql" and any("resolveReviewThread" in a for a in call)
    ]


def test_a_deferred_finding_gets_a_reply_and_goes_to_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    entries, log = _logs()

    outcome = _defer_the_finding(tmp_path, repo, gh, log)

    assert outcome.stop is False and outcome.handoff is not None
    gh_state = _gh_state(tmp_path)
    [reply] = gh_state["replies"]
    assert reply["body"].startswith("**Deferred:** out of scope")
    assert gh_state["threads"][0]["isResolved"] is False
    assert _resolve_calls(tmp_path) == []
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert _git(repo, "branch", "--show-current") == BRANCH
    assert any(
        level == "WARN" and "Finding 1 (src/widget.py:1) was deferred" in message
        for level, message in entries
    )
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    assert state["deferral_planner_pass"] is True
    assert state["deferred_findings"] == []
    assert [finding["number"] for finding in state["open_findings"]] == [1]
    assert not any("replied" in finding for finding in state["open_findings"])

    handoff = outcome.handoff
    decision = route(handoff)
    assert decision.route == "planner" and decision.warnings == []
    assert "story_id: W9.9" in handoff
    assert "### Finding 1: `src/widget.py:1`" in handoff
    assert "WIDGET can be unset." in handoff
    assert f"> {DEFERRED_RESPONSE}" in handoff
    assert "## Deferred Finding Decisions" in handoff
    assert "`Follow-up:`" in handoff
    assert "Files in scope" in handoff and "`src/widget.py`" in handoff


def test_a_retry_keeps_a_replied_deferred_finding_open_for_the_planner(
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

    assert outcome.stop is False and route(outcome.handoff).route == "planner"
    assert "replies" not in _gh_state(tmp_path)
    assert _gh_state(tmp_path)["threads"][0]["isResolved"] is False
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert any("was deferred" in message for _level, message in entries)


PLANNER_ACCEPTS = """---
next_agent: backend
reason: "W9.9 closes; its deferred finding becomes W9.10."
epic_id: E1
story_id: W9.10
story_title: Guard the widget
producer: planner
---

# Handoff: Guard the widget

## Deferred Finding Decisions

### Finding 1
Follow-up: W9.10 Guard the widget fixes this with its own Work Packet.

## Work Packet

- **Objective:** Guard the widget.
- **Files in scope:** `src/guard.py`
- **Files out of bounds:** none
- **Context:** none
- **Verification command:** `python -c "print(1)"`
- **Expected next route:** auditor
"""

PLANNER_WIDENS = """---
next_agent: backend
reason: "Widen W9.9 so the deferred finding is fixed before the merge."
epic_id: E1
story_id: W9.9
story_title: Add a widget
producer: planner
---

# Handoff: Add a widget

Fix the finding below and answer it under `## Review Responses`.

### Finding 1: `src/widget.py:1`

WIDGET can be unset.

## Work Packet

- **Objective:** Add the widget module and guard it.
- **Files in scope:** `src/widget.py`, `src/guard.py`
- **Files out of bounds:** none
- **Context:** none
- **Verification command:** `python -c "print(1)"`
- **Expected next route:** auditor
"""


def _settle(repo: Path, gh: list[str], log, handoff: str) -> PublishOutcome:
    return settle_deferred_findings(
        repo_root=repo,
        remote="origin",
        handoff_content=handoff,
        log=log,
        gh_command=gh,
    )


def test_a_deferral_survives_the_next_review_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    two_rounds = ReviewGate(reviewers=("codex",), timeout_seconds=600, max_rounds=2)
    first = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(first)],
        review_comments=[_codex_finding(first)],
    )
    _set_threads(tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834})
    _entries, log = _logs()
    assert _publish_with_review(repo, gh, log, gate=two_rounds).handoff is not None

    # Round 1: the implementer defers finding 1, and Codex's review of the
    # new commit reports a different finding.
    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Tidy the widget")
    second = _git(repo, "rev-parse", "HEAD")
    new_finding = {
        **_codex_finding(second),
        "id": 5000,
        "body": "**P2 Name the widget**\n\nWIDGET needs a docstring.",
    }
    _set_api(
        tmp_path, comments=[_codex_summary(second)], review_comments=[new_finding]
    )
    _set_threads(
        tmp_path,
        {"id": "T1", "isResolved": False, "comment_id": 4279958834},
        {"id": "T2", "isResolved": False, "comment_id": 5000},
    )
    save_review_responses(
        repo,
        f"## Review Responses\n\n### Finding 1: `src/widget.py:1`\n{DEFERRED_RESPONSE}\n",
    )

    outcome = _publish_with_review(repo, gh, log, gate=two_rounds)

    assert outcome.handoff is not None and "WIDGET needs a docstring." in outcome.handoff
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    assert state["review_rounds"] == 2
    assert [f["comment_id"] for f in state["open_findings"]] == [5000]
    assert [f["comment_id"] for f in state["deferred_findings"]] == [4279958834]

    # Round 2: the implementer fixes the new finding. Only the deferred
    # thread is left, so the planner gets it instead of a pause.
    (repo / "src" / "widget.py").write_text('"""Widget."""\nWIDGET = 2\n', encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Name the widget")
    save_review_responses(
        repo,
        "## Review Responses\n\n### Finding 1: `src/widget.py:1`\nAdded a docstring.\n",
    )

    outcome = _publish_with_review(repo, gh, log, gate=two_rounds)

    assert outcome.stop is False and route(outcome.handoff).route == "planner"
    assert "WIDGET can be unset." in outcome.handoff
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_a_planner_follow_up_resolves_the_thread_and_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    entries, log = _logs()
    assert _defer_the_finding(tmp_path, repo, gh, log).handoff is not None
    story_head = _git(repo, "rev-parse", "HEAD")

    outcome = _settle(repo, gh, log, PLANNER_ACCEPTS)

    assert outcome == PublishOutcome(), outcome.message
    gh_state = _gh_state(tmp_path)
    assert gh_state["threads"][0]["isResolved"] is True
    deferred_reply, decision_reply = gh_state["replies"]
    assert deferred_reply["body"].startswith("**Deferred:**")
    assert decision_reply["body"].startswith("Follow-up: W9.10 Guard the widget")
    assert "`planner` role" in decision_reply["body"]
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "HEAD") == story_head
    assert not (repo / STATE_PATH).exists()
    assert any("accepted finding 1" in message for _level, message in entries)


def test_a_planner_can_widen_the_scope_and_send_the_finding_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _entries, log = _logs()
    assert _defer_the_finding(tmp_path, repo, gh, log).handoff is not None

    # The copied `### Finding 1` heading is not a decision: it is outside the
    # `## Deferred Finding Decisions` section.
    outcome = _settle(repo, gh, log, PLANNER_WIDENS)

    assert outcome == PublishOutcome()
    assert _resolve_calls(tmp_path) == []
    assert len(_gh_state(tmp_path)["replies"]) == 1
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert _git(repo, "branch", "--show-current") == BRANCH
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    assert [finding["number"] for finding in state["open_findings"]] == [1]
    assert state["deferral_planner_pass"] is True

    # The implementer fixes it on the same branch; the dispatcher answers,
    # resolves, and merges without another review.
    (repo / "src" / "guard.py").write_text("GUARD = True\n", encoding="utf-8")
    _git(repo, "add", "src/guard.py")
    _git(repo, "commit", "-q", "-m", "W9.9 Guard the widget")
    fixed = _git(repo, "rev-parse", "HEAD")
    save_review_responses(
        repo,
        "## Review Responses\n\n### Finding 1: `src/widget.py:1`\n"
        "Guarded WIDGET in src/guard.py.\n",
    )

    outcome = _publish_with_review(repo, gh, log)

    assert outcome == PublishOutcome()
    gh_state = _gh_state(tmp_path)
    assert gh_state["threads"][0]["isResolved"] is True
    assert gh_state["replies"][-1]["body"].startswith("Guarded WIDGET")
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]
    assert _git(repo, "rev-parse", "HEAD") == fixed


def test_a_second_deferral_after_the_planner_pass_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _entries, log = _logs()
    assert _defer_the_finding(tmp_path, repo, gh, log).handoff is not None
    assert _settle(repo, gh, log, PLANNER_WIDENS) == PublishOutcome()

    (repo / "src" / "widget.py").write_text("WIDGET = 3\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Another try")
    save_review_responses(
        repo,
        f"## Review Responses\n\n### Finding 1: `src/widget.py:1`\n{DEFERRED_RESPONSE}\n",
    )

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is True and outcome.exit_code == 0
    assert outcome.handoff is None
    assert "1 review thread(s)" in outcome.message
    assert _gh_state(tmp_path)["threads"][0]["isResolved"] is False
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


def test_a_planner_that_decides_nothing_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    entries, log = _logs()
    assert _defer_the_finding(tmp_path, repo, gh, log).handoff is not None
    moved_on = PLANNER_ACCEPTS.replace(
        "Follow-up: W9.10", "Left for later: W9.10"
    )

    outcome = _settle(repo, gh, log, moved_on)

    assert outcome.stop is True and outcome.exit_code == 0
    assert "neither accepted nor sent back deferred finding(s) 1" in outcome.message
    assert _gh_state(tmp_path)["threads"][0]["isResolved"] is False
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    assert any(level == "PAUSE" for level, _message in entries)


def test_a_planner_asking_the_user_leaves_the_pull_request_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _entries, log = _logs()
    assert _defer_the_finding(tmp_path, repo, gh, log).handoff is not None
    asks_user = PLANNER_WIDENS.replace("next_agent: backend", "next_agent: user")

    outcome = _settle(repo, gh, log, asks_user)

    assert outcome == PublishOutcome()
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    assert [finding["number"] for finding in state["open_findings"]] == [1]


def test_settle_does_nothing_without_a_planner_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    _entries, log = _logs()

    assert _settle(repo, gh, log, PLANNER_ACCEPTS) == PublishOutcome()
    assert _settle(tmp_path / "nowhere", gh, log, PLANNER_ACCEPTS) == PublishOutcome()
    assert _gh_calls(tmp_path) == []


def test_a_deferred_finding_next_to_another_open_thread_pauses(
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
    # T2 is a person's comment, not a Codex finding the implementer answered.
    _set_threads(
        tmp_path,
        {"id": "T1", "isResolved": False, "comment_id": 4279958834},
        {"id": "T2", "isResolved": False, "comment_id": 555},
    )
    _entries, log = _logs()
    assert _publish_with_review(repo, gh, log).handoff is not None
    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Tidy the widget")
    save_review_responses(
        repo,
        f"## Review Responses\n\n### Finding 1: `src/widget.py:1`\n{DEFERRED_RESPONSE}\n",
    )

    outcome = _publish_with_review(repo, gh, log)

    assert outcome.stop is True and outcome.handoff is None
    assert "2 review thread(s)" in outcome.message
    assert ["pr", "merge"] not in [call[:2] for call in _gh_calls(tmp_path)]


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


def test_save_review_responses_merges_claims_by_finding_number(
    tmp_path: Path,
) -> None:
    save_review_responses(
        tmp_path,
        "## Review Responses\n\n### Finding 1: `a.py:1`\nFixed.\n\n"
        "### Finding 2\nNot a bug.\n",
    )
    assert (tmp_path / RESPONSES_PATH).is_file()

    # A re-claim after an audit bounce answers nothing new.
    save_review_responses(tmp_path, "# Handback\n\nNo responses here.\n")
    saved = (tmp_path / RESPONSES_PATH).read_text(encoding="utf-8")
    assert parse_review_responses(saved) == {1: "Fixed.", 2: "Not a bug."}

    # A claim's response replaces the saved one for the same finding.
    save_review_responses(
        tmp_path, "## Review Responses\n\n### Finding 2\nFixed in the retry.\n"
    )
    saved = (tmp_path / RESPONSES_PATH).read_text(encoding="utf-8")
    assert parse_review_responses(saved) == {1: "Fixed.", 2: "Fixed in the retry."}


def test_an_audit_bounce_keeps_the_correction_rounds_responses(
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

    # The implementer fixes the finding and answers it; the gate saves that.
    (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Handle a missing widget")
    save_review_responses(
        repo,
        "## Review Responses\n\n### Finding 1: `src/widget.py:1`\n"
        "Defaulted WIDGET when it is unset.\n",
    )
    # The audit bounces; the next claim in the round carries no responses.
    (repo / "src" / "widget.py").write_text("WIDGET = 3\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "W9.9 Address the audit")
    save_review_responses(repo, "# Handback\n\nFixed the audit finding.\n")
    _set_api(tmp_path, comments=[_codex_summary(_git(repo, "rev-parse", "HEAD"))])

    outcome = _publish_with_review(repo, gh, log)

    assert outcome == PublishOutcome()
    gh_state = _gh_state(tmp_path)
    [reply] = gh_state["replies"]
    assert reply["body"].startswith("Defaulted WIDGET when it is unset.")
    assert gh_state["threads"][0]["isResolved"] is True
    assert [call[:2] for call in _gh_calls(tmp_path)][-1] == ["pr", "merge"]
    assert not any("No review response" in m for _l, m in entries)


def test_a_new_correction_round_drops_the_saved_responses(
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
    # Responses saved with no open findings answer nothing yet.
    save_review_responses(
        repo, "## Review Responses\n\n### Finding 1\nAn answer to nothing.\n"
    )
    _entries, log = _logs()

    assert _publish_with_review(repo, gh, log).handoff is not None

    assert not (repo / RESPONSES_PATH).exists()


def test_keep_review_responses_never_drops_saved_responses(tmp_path: Path) -> None:
    assert keep_review_responses(
        tmp_path, "## Review Responses\n\n### Finding 1\nFixed.\n"
    )
    assert not keep_review_responses(tmp_path, "# Audit Report\n\nApproved.\n")
    assert parse_review_responses(
        (tmp_path / RESPONSES_PATH).read_text(encoding="utf-8")
    ) == {1: "Fixed."}


def _correction_claim(scope_sha: str | None) -> str:
    """A correction-round handback; without ``scope_sha`` its evidence is bad."""

    scope_lines = f"scope_sha: {scope_sha}\nclose_type: story\n" if scope_sha else ""
    return f"""---
next_agent: auditor
reason: "Fixed the review finding; audit requested."
epic_id: E1
story_id: W9.9
story_title: Add a widget
status: ready_for_review
{scope_lines}producer: backend
---

# Backend Handback

## Completed Work

- Defaulted WIDGET in src/widget.py.

## Verification Evidence

- **Commands run:** `python -c "print(1)"`
- **Output summary:** exit 0
- **Commit SHA verified:** {scope_sha or 'not-a-sha'}
- **Files changed or reviewed:** src/widget.py
- **Unresolved concerns:** none

## Review Responses

### Finding 1: `src/widget.py:1`
Defaulted WIDGET when it is unset.
"""


@pytest.mark.parametrize("recovery", ["restart", "resume"])
def test_review_responses_survive_a_failed_validation_before_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    """A correction-round handback that fails post-dispatch validation never
    passes the work-claim gate. After the handoff is fixed, the auditor runs
    directly, and the responses still reach the review thread."""

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
    bounce = _publish_with_review(repo, gh, log).handoff
    assert bounce is not None

    config = DispatchConfig(
        repo_root=repo,
        poll_interval_seconds=0,
        pause_recovery="off",
        enforce_work_packet=True,
        commit_mode="dispatcher",
        publish={"mode": "pr", "wait_for_reviewers": ["codex"]},
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
    )
    config.handoff_full_path.parent.mkdir(parents=True, exist_ok=True)
    config.handoff_full_path.write_text(bounce, encoding="utf-8")
    dispatches: list[str] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
        path.write_text(_correction_claim(None), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object):
        dispatches.append(subagent_name)
        assert subagent_name == "auditor"
        config.handoff_full_path.write_text(VERIFIED, encoding="utf-8")
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def validate(path: Path, previous_agent: str, **_kwargs: object):
        if previous_agent == "backend":
            return ValidationResult(
                verdict="NO",
                warnings=[],
                errors=["evidence_malformed: the Commit SHA verified is not a SHA."],
                routing_instruction="auditor",
            )
        return ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        )

    def fix_handoff() -> None:
        # The operator points the claim at the dispatcher's commit.
        head = _git(repo, "rev-parse", "HEAD")
        config.handoff_full_path.write_text(_correction_claim(head), encoding="utf-8")
        _set_api(tmp_path, comments=[_codex_summary(head)])

    def fix_and_resume(_config: DispatchConfig, **_kwargs: object) -> bool:
        fix_handoff()
        return True

    def publish(**kwargs: object) -> PublishOutcome:
        return publish_story(**kwargs, gh_command=gh, sleep=lambda seconds: None)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    monkeypatch.setattr(orchestrator, "publish_story", publish)
    if recovery == "resume":
        monkeypatch.setattr(
            orchestrator, "_pause_until_handoff_changes", fix_and_resume
        )
    ledger.reset_bounce("W9.9")
    try:
        if recovery == "restart":
            # The pause stops the run; the operator fixes the handoff and
            # starts the dispatcher again, which routes to the auditor.
            assert orchestrator.run_loop(config, max_cycles=1, log=log) == 1
            assert not (repo / RESPONSES_PATH).exists()
            fix_handoff()
            assert orchestrator.run_loop(config, max_cycles=1, log=log) == 0
        else:
            assert orchestrator.run_loop(config, max_cycles=2, log=log) == 0
    finally:
        ledger.reset_bounce("W9.9")

    assert dispatches == ["backend", "auditor"]
    assert not any("No review response" in message for _level, message in entries)
    gh_state = _gh_state(tmp_path)
    [reply] = gh_state["replies"]
    assert reply["endpoint"].endswith("/pulls/7/comments/4279958834/replies")
    assert reply["body"].startswith("Defaulted WIDGET when it is unset.")
    assert gh_state["threads"][0]["isResolved"] is True
    assert gh_state["pr"] == "MERGED"
    assert _git(repo, "branch", "--show-current") == "main"
    assert not (repo / RESPONSES_PATH).exists()


def test_the_review_bounce_says_a_deferral_is_still_ready_for_review() -> None:
    handoff = build_review_bounce_handoff(
        state=_plain_state(),
        findings=(),
        url="https://github.example/acme/repo/pull/7",
        head_sha="0" * 40,
        review_round=1,
        max_rounds=1,
    )
    text = " ".join(handoff.split())

    assert (
        "Deferring every finding is still a `ready_for_review` handback to "
        "`auditor`: change no file, answer each finding with `Deferred:`"
    ) in text
    assert "`pending-dispatcher-commit`" in text
    assert "so no new commit is needed" in text
    assert "Do not hand back a `blocked_*` status to defer a finding" in text


def test_a_review_bounce_of_an_operator_story_pauses_for_the_user() -> None:
    handoff = build_review_bounce_handoff(
        state=replace(_plain_state(), implementer="user"),
        findings=(),
        url="https://github.example/acme/repo/pull/7",
        head_sha="0" * 40,
        review_round=1,
        max_rounds=1,
    )

    decision = route(handoff)
    assert decision.route == "user" and not decision.warnings
    assert "paused for the operator" in handoff
    assert "status: verified_fail" in build_review_bounce_handoff(
        state=_plain_state(),
        findings=(),
        url="",
        head_sha="0" * 40,
        review_round=1,
        max_rounds=1,
    )


def _deferral_claim() -> str:
    """A correction-round handback that defers the finding and changes nothing."""

    return """---
next_agent: auditor
reason: "Finding 1 is real but outside this story's scope; deferred."
epic_id: E1
story_id: W9.9
story_title: Add a widget
status: ready_for_review
producer: backend
---

# Backend Handback

## Completed Work

- No code change: the only finding is deferred to a follow-up story.

## Verification Evidence

- **Commands run:** `python -c "print(1)"`
- **Output summary:** exit 0
- **Commit SHA verified:** pending-dispatcher-commit
- **Files changed or reviewed:** src/widget.py
- **Unresolved concerns:** Finding 1 is deferred.

## Review Responses

### Finding 1: `src/widget.py:1`
Deferred: the fix needs src/guard.py, which is outside Files in scope.
"""


def _pr_loop_config(repo: Path, **overrides: object) -> DispatchConfig:
    options: dict[str, object] = {
        "repo_root": repo,
        "poll_interval_seconds": 0,
        "pause_recovery": "off",
        "enforce_work_packet": True,
        "commit_mode": "dispatcher",
        "publish": {"mode": "pr", "wait_for_reviewers": ["codex"]},
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
    }
    options.update(overrides)
    return DispatchConfig(**options)


def _bounce_after_a_base_merge(
    tmp_path: Path, repo: Path, remote: Path, gh: list[str], log
) -> str:
    """Bounce one Codex finding on a story branch whose head merges the base.

    The pull request's head is a merge of the moved base branch, as when the
    dispatcher syncs the story branch before pushing it.
    """

    _commit_on_remote(tmp_path, remote, "main", {"NOTES.md": "notes\n"}, "Notes")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
    head = _git(repo, "rev-parse", "HEAD")
    _set_api(
        tmp_path,
        comments=[_codex_summary(head)],
        review_comments=[_codex_finding(head)],
    )
    _set_threads(tmp_path, {"id": "T1", "isResolved": False, "comment_id": 4279958834})
    bounce = _publish_with_review(repo, gh, log).handoff
    assert bounce is not None
    return bounce


def test_a_deferral_only_handback_reaches_the_planner_and_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferring every finding without a code change passes the work-claim
    gate on the story's earlier commits, then flows through the audit, the
    deferred reply, and the planner's follow-up decision to the merge."""

    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    entries, log = _logs()
    bounce = _bounce_after_a_base_merge(tmp_path, repo, remote, gh, log)
    story_head = _git(repo, "rev-parse", "HEAD")
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3

    config = _pr_loop_config(repo)
    config.handoff_full_path.parent.mkdir(parents=True, exist_ok=True)
    config.handoff_full_path.write_text(bounce, encoding="utf-8")
    dispatches: list[str] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        path.write_text(_deferral_claim(), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object):
        dispatches.append(subagent_name)
        assert subagent_name == "auditor"
        config.handoff_full_path.write_text(VERIFIED, encoding="utf-8")
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("planner")
        assert "## Deferred Review Findings" in path.read_text(encoding="utf-8")
        path.write_text(PLANNER_ACCEPTS, encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def validate(path: Path, previous_agent: str, **_kwargs: object):
        return ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="auditor"
        )

    def publish(**kwargs: object) -> PublishOutcome:
        return publish_story(**kwargs, gh_command=gh, sleep=lambda seconds: None)

    def settle(**kwargs: object) -> PublishOutcome:
        return settle_deferred_findings(**kwargs, gh_command=gh)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    monkeypatch.setattr(orchestrator, "publish_story", publish)
    monkeypatch.setattr(orchestrator, "settle_deferred_findings", settle)
    ledger.reset_bounce("W9.9")
    try:
        assert orchestrator.run_loop(config, max_cycles=3, log=log) == 0
    finally:
        ledger.reset_bounce("W9.9")

    assert dispatches == ["backend", "auditor", "planner"]
    assert any(
        "nothing new to commit; recorded the story's earlier commit" in message
        for _level, message in entries
    )
    assert ("AGENT", "Work-claim gate PASSED for backend.") in entries
    gh_state = _gh_state(tmp_path)
    deferred_reply, decision_reply = gh_state["replies"]
    assert deferred_reply["body"].startswith("Deferred: the fix needs src/guard.py")
    assert decision_reply["body"].startswith("Follow-up: W9.10")
    assert gh_state["threads"][0]["isResolved"] is True
    assert gh_state["pr"] == "MERGED"
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "HEAD") == story_head


SECOND_FINDING_ID = 5000


def _second_finding(sha: str) -> dict:
    return {
        **_codex_finding(sha),
        "id": SECOND_FINDING_ID,
        "line": 2,
        "body": "**P2 Name the widget**\n\nWIDGET needs a docstring.",
    }


def _bounce_findings(
    tmp_path: Path, repo: Path, gh: list[str], log, *, two: bool = False
) -> str:
    """Publish the story and bounce one (or two) Codex findings on its head."""

    head = _git(repo, "rev-parse", "HEAD")
    findings = [_codex_finding(head)] + ([_second_finding(head)] if two else [])
    _set_api(tmp_path, comments=[_codex_summary(head)], review_comments=findings)
    _set_threads(
        tmp_path,
        {"id": "T1", "isResolved": False, "comment_id": 4279958834},
        *(
            [{"id": "T2", "isResolved": False, "comment_id": SECOND_FINDING_ID}]
            if two
            else []
        ),
    )
    bounce = _publish_with_review(repo, gh, log).handoff
    assert bounce is not None
    return bounce


def _remote_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    return repo, remote, gh


def test_complete_planner_decisions_without_a_pass_merge_the_reviewed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked correction handback never arms the planner's pass. When the
    planner still decides every open finding and the pull request head is the
    reviewed commit, the decisions are acted on instead of ignored."""

    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    entries, log = _logs()
    _bounce_findings(tmp_path, repo, gh, log)
    story_head = _git(repo, "rev-parse", "HEAD")
    assert _saved_state(repo)["reviewed_head"] == story_head

    outcome = _settle(repo, gh, log, PLANNER_ACCEPTS)

    assert outcome == PublishOutcome(), outcome.message
    gh_state = _gh_state(tmp_path)
    [decision_reply] = gh_state["replies"]
    assert decision_reply["body"].startswith("Follow-up: W9.10 Guard the widget")
    assert gh_state["threads"][0]["isResolved"] is True
    assert gh_state["pr"] == "MERGED"
    assert _git(repo, "branch", "--show-current") == "main"
    assert any("decided every open finding" in message for _l, message in entries)


def test_partial_planner_decisions_without_a_pass_pause_and_merge_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    entries, log = _logs()
    _bounce_findings(tmp_path, repo, gh, log, two=True)
    calls_before = len(_gh_calls(tmp_path))
    asks_user = PLANNER_ACCEPTS.replace("next_agent: backend", "next_agent: user")

    outcome = _settle(repo, gh, log, asks_user)

    assert outcome.stop is True and outcome.exit_code == 0
    assert "cannot act on them safely: finding(s) 2 have no decision" in (
        outcome.message
    )
    assert "2 review finding(s) on https://github.example/acme/repo/pull/7" in (
        outcome.message
    )
    assert "route HANDOFF.md back to `backend` with `story_id: W9.9`" in (
        outcome.message
    )
    assert ("PAUSE", outcome.message) in entries
    assert len(_gh_calls(tmp_path)) == calls_before, "no reply, resolve, or merge"
    assert "replies" not in _gh_state(tmp_path)
    assert _git(repo, "branch", "--show-current") == BRANCH
    state = _saved_state(repo)
    assert state["deferral_planner_pass"] is False
    assert [finding["number"] for finding in state["open_findings"]] == [1, 2]
    assert state["pull_request_url"] == "https://github.example/acme/repo/pull/7"


def test_planner_decisions_without_a_pass_on_an_unreviewed_head_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    _entries, log = _logs()
    _bounce_findings(tmp_path, repo, gh, log)
    (repo / "src" / "widget.py").write_text("WIDGET = 9\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "Unaudited change")

    outcome = _settle(repo, gh, log, PLANNER_ACCEPTS)

    assert outcome.stop is True
    assert "is not the pull request head" in outcome.message
    assert "replies" not in _gh_state(tmp_path)
    assert _gh_state(tmp_path)["pr"] == "OPEN"


def test_a_stale_decisions_section_without_open_findings_is_ignored(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    entries, log = _logs()

    assert _settle(tmp_path, ["gh-should-not-run"], log, PLANNER_ACCEPTS) == (
        PublishOutcome()
    )
    assert entries == []


def test_another_story_on_an_open_story_branch_names_the_pull_request(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _start_story(tmp_path)
    state = _saved_state(tmp_path)
    state["pull_request_url"] = "https://github.example/acme/repo/pull/7"
    state["open_findings"] = [{"number": 1, "comment_id": 9, "location": "x:1"}]
    (tmp_path / STATE_PATH).write_text(json.dumps(state), encoding="utf-8")

    def reason(assignment: str) -> str | None:
        return unfinished_story_pause_reason(
            repo_root=tmp_path,
            prefix="llm-handoff/",
            assignment_content=assignment,
            route="backend",
        )

    message = reason(PLANNER_ACCEPTS)
    assert message is not None
    assert message.startswith("UNFINISHED STORY: HANDOFF.md routes `backend` to `W9.10`")
    assert f"still on `{BRANCH}` for `W9.9`" in message
    assert (
        "its pull request https://github.example/acme/repo/pull/7 is still open "
        "with 1 unresolved review finding(s)"
    ) in message
    assert "route HANDOFF.md back to `backend` with `story_id: W9.9`" in message
    assert "run `git switch main` and `git pull`" in message
    # The same story, or a checkout already off the story branch, is fine.
    assert reason(ASSIGNMENT) is None
    _git(tmp_path, "switch", "-q", "main")
    assert reason(PLANNER_ACCEPTS) is None


# The planner moved on to W9.10 without deciding W9.9's open finding, as the
# planner did with GEM-0c while GEM-0b's pull request was still open.
NEXT_STORY = PLANNER_ACCEPTS.replace(
    "## Deferred Finding Decisions\n\n### Finding 1\n"
    "Follow-up: W9.10 Guard the widget fixes this with its own Work Packet.\n\n",
    "",
)


def _sweep(repo: Path, gh: list[str], log, handoff: str = NEXT_STORY, **kwargs):
    options: dict[str, object] = {
        "repo_root": repo,
        "mode": "pr",
        "remote": "origin",
        "handoff_content": handoff,
        "handoff_path": Path("docs/handoff/HANDOFF.md"),
        "log": log,
        "gh_command": gh,
    }
    options.update(kwargs)
    return recover_orphaned_story(**options)


def test_the_sweep_does_nothing_without_a_story_or_outside_pr_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    _entries, log = _logs()
    bounce = _bounce_findings(tmp_path, repo, gh, log)
    calls_before = len(_gh_calls(tmp_path))

    assert _sweep(repo, gh, log, mode="local") == PublishOutcome()
    assert _sweep(repo, gh, log, mode="merge-commit") == PublishOutcome()
    # The story's own handoffs do not move on from it.
    assert _sweep(repo, gh, log, handoff=bounce) == PublishOutcome()
    assert _sweep(repo, gh, log, handoff=ASSIGNMENT) == PublishOutcome()
    assert _sweep(tmp_path / "nowhere", gh, log) == PublishOutcome()
    assert len(_gh_calls(tmp_path)) == calls_before
    assert _saved_state(repo)["open_findings"]


def test_the_sweep_cleans_up_a_merged_story_and_continues_on_the_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    entries, log = _logs()
    _bounce_findings(tmp_path, repo, gh, log)
    story_head = _git(repo, "rev-parse", "HEAD")
    # A person resolved the thread and merged the pull request.
    subprocess.run([*gh, "pr", "merge", BRANCH, "--merge"], check=True)
    _set_threads(tmp_path, {"id": "T1", "isResolved": True, "comment_id": 4279958834})

    outcome = _sweep(repo, gh, log)

    assert outcome == PublishOutcome()
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "HEAD") == story_head
    assert BRANCH not in _git(repo, "branch", "--list")
    assert not (repo / STATE_PATH).exists()
    assert any("is merged; cleared its saved story state" in m for _l, m in entries)


@pytest.mark.parametrize(
    "case, expected",
    [
        ("merged_with_open_thread", "merged with 1 unresolved review thread(s)"),
        ("open_all_resolved", "is open and was not merged"),
        ("gh_error", "`gh pr view llm-handoff/w9.9-add-a-widget` failed (HTTP 502"),
        ("head_moved", "is not the pull request head"),
        ("person_comment", "not one of the story's saved findings"),
        ("uncommitted", "story files are uncommitted (`src/widget.py`)"),
        ("planner_had_its_pass", "already had its one pass"),
    ],
)
def test_the_sweep_pauses_when_the_story_cannot_be_recovered_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected: str
) -> None:
    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    _entries, log = _logs()
    _bounce_findings(tmp_path, repo, gh, log)
    unresolved = {"id": "T1", "isResolved": False, "comment_id": 4279958834}
    if case == "merged_with_open_thread":
        subprocess.run([*gh, "pr", "merge", BRANCH, "--merge"], check=True)
    elif case == "open_all_resolved":
        _set_threads(tmp_path, {**unresolved, "isResolved": True})
    elif case == "gh_error":
        _set_gh(tmp_path, fail_view=True)
    elif case == "head_moved":
        (repo / "src" / "widget.py").write_text("WIDGET = 9\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "Unaudited change")
    elif case == "person_comment":
        _set_threads(tmp_path, unresolved, {"id": "T9", "isResolved": False, "comment_id": 9})
    elif case == "uncommitted":
        (repo / "src" / "widget.py").write_text("WIDGET = 9\n", encoding="utf-8")
    elif case == "planner_had_its_pass":
        state = _saved_state(repo)
        state["deferral_planner_pass"] = True
        (repo / STATE_PATH).write_text(json.dumps(state), encoding="utf-8")
    calls_before = len(_gh_calls(tmp_path))
    before = _saved_state(repo)

    outcome = _sweep(repo, gh, log)

    assert outcome.pause is True and outcome.stop is False and outcome.handoff is None
    assert outcome.message.startswith(
        "PULL REQUEST NOT MERGED: HANDOFF.md routes `backend` to `W9.10`, but "
        f"story `W9.9` on `{BRANCH}` is not finished"
    )
    assert expected in outcome.message
    assert "Unresolved review findings: 1 (`src/widget.py:1`)" in outcome.message
    assert "route HANDOFF.md back to `backend` with `story_id: W9.9`" in outcome.message
    assert "merge it by hand" in outcome.message
    assert _saved_state(repo) == before
    assert not (repo / DISPLACED_HANDOFF_PATH).exists()
    new_calls = [call[:2] for call in _gh_calls(tmp_path)[calls_before:]]
    assert ["pr", "merge"] not in new_calls
    assert not any("/replies" in call[-1] for call in _gh_calls(tmp_path))


def test_the_sweep_gives_the_planner_its_pass_on_a_safe_open_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    _entries, log = _logs()
    _bounce_findings(tmp_path, repo, gh, log)

    outcome = _sweep(repo, gh, log)

    assert outcome.stop is False and outcome.pause is False
    handoff = outcome.handoff
    assert handoff is not None
    assert route(handoff).route == "planner"
    assert "## Displaced Handoff" in handoff
    assert DISPLACED_HANDOFF_PATH.as_posix() in handoff
    assert "WIDGET can be unset." in handoff
    assert (repo / DISPLACED_HANDOFF_PATH).read_text(encoding="utf-8") == NEXT_STORY
    state = _saved_state(repo)
    assert state["deferral_planner_pass"] is True
    assert [finding["number"] for finding in state["open_findings"]] == [1]
    assert "replies" not in _gh_state(tmp_path)

    # The planner decides the finding and restores the displaced route.
    assert _settle(repo, gh, log, PLANNER_ACCEPTS) == PublishOutcome()
    assert _gh_state(tmp_path)["pr"] == "MERGED"
    assert _git(repo, "branch", "--show-current") == "main"


def _blocked_claim(response: str) -> str:
    return f"""---
next_agent: planner
reason: "Finding 1 is real but outside this story's scope."
epic_id: E1
story_id: W9.9
story_title: Add a widget
status: blocked_implementation_failure
producer: backend
---

# Backend Handback

## Completed Work

- No code change.

## Review Responses

### Finding 1: `src/widget.py:1`
{response}
"""


@pytest.mark.parametrize("variant", ["deferred", "not_deferred", "changed"])
def test_a_blocked_correction_handback_that_only_defers_counts_as_a_deferral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    entries, log = _logs()
    bounce = _bounce_findings(tmp_path, repo, gh, log)
    config = _pr_loop_config(repo)
    config.handoff_full_path.parent.mkdir(parents=True, exist_ok=True)
    config.handoff_full_path.write_text(bounce, encoding="utf-8")
    response = (
        "Deferred: the fix needs src/guard.py, which is outside Files in scope."
        if variant != "not_deferred"
        else "This finding does not hold up: WIDGET is always set."
    )
    claim = _blocked_claim(response)

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        if variant == "changed":
            (repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")
        path.write_text(claim, encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def validate(path: Path, previous_agent: str, **_kwargs: object):
        return ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        )

    def defer(**kwargs: object):
        return defer_blocked_handback(**kwargs, gh_command=gh)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    monkeypatch.setattr(orchestrator, "defer_blocked_handback", defer)
    ledger.reset_bounce("W9.9")
    try:
        assert orchestrator.run_loop(config, max_cycles=1, log=log) == 0
    finally:
        ledger.reset_bounce("W9.9")

    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    state = _saved_state(repo)
    if variant == "deferred":
        [reply] = _gh_state(tmp_path)["replies"]
        assert reply["body"].startswith("Deferred: the fix needs src/guard.py")
        assert _gh_state(tmp_path)["threads"][0]["isResolved"] is False
        assert "## Deferred Review Findings" in handoff
        assert route(handoff).route == "planner"
        assert state["deferral_planner_pass"] is True
        assert any("treating it as a deferral" in m for _l, m in entries)
    else:
        assert "replies" not in _gh_state(tmp_path)
        assert handoff == claim
        assert state["deferral_planner_pass"] is False
        assert not any("treating it as a deferral" in m for _l, m in entries)


def test_a_fresh_start_recovers_a_story_left_open_like_gem_0b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real stall: story.json (saved before reviewed heads were recorded)
    has finding 1 open and no planner pass, the pull request head is the base
    merge Codex reviewed, the checkout is on it, and HANDOFF.md already routes
    the follow-up story. One planner pass merges the pull request, and the
    follow-up story starts from the updated base branch."""

    repo, remote, gh, env = _with_remote(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _start_story(repo)
    entries, log = _logs()
    _bounce_after_a_base_merge(tmp_path, repo, remote, gh, log)
    reviewed = _git(repo, "rev-parse", "HEAD")
    state = _saved_state(repo)
    del state["reviewed_head"], state["pull_request_url"]
    assert state["open_findings"][0]["number"] == 1
    assert state["review_rounds"] == 1 and state["deferral_planner_pass"] is False
    (repo / STATE_PATH).write_text(json.dumps(state), encoding="utf-8")

    config = _pr_loop_config(repo)
    config.handoff_full_path.parent.mkdir(parents=True, exist_ok=True)
    config.handoff_full_path.write_text(NEXT_STORY, encoding="utf-8")
    dispatches: list[tuple[str, str]] = []

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append(("planner", _git(repo, "branch", "--show-current")))
        handoff = path.read_text(encoding="utf-8")
        assert "## Displaced Handoff" in handoff
        displaced = (repo / DISPLACED_HANDOFF_PATH).read_text(encoding="utf-8")
        restored = displaced.replace(
            "## Work Packet",
            "## Deferred Finding Decisions\n\n### Finding 1\n"
            "Follow-up: W9.10 Guard the widget fixes this.\n\n## Work Packet",
        )
        path.write_text(restored, encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append(("backend", _git(repo, "branch", "--show-current")))
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def validate(path: Path, previous_agent: str, **_kwargs: object):
        return ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="backend"
        )

    def sweep(**kwargs: object):
        return recover_orphaned_story(**kwargs, gh_command=gh)

    def settle(**kwargs: object) -> PublishOutcome:
        return settle_deferred_findings(**kwargs, gh_command=gh)

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    monkeypatch.setattr(orchestrator, "recover_orphaned_story", sweep)
    monkeypatch.setattr(orchestrator, "settle_deferred_findings", settle)

    assert orchestrator.run_loop(config, max_cycles=3, log=log) == 0

    assert dispatches == [
        ("planner", BRANCH),
        ("backend", "llm-handoff/w9.10-guard-the-widget"),
    ]
    gh_state = _gh_state(tmp_path)
    [decision_reply] = gh_state["replies"]
    assert decision_reply["body"].startswith("Follow-up: W9.10")
    assert gh_state["threads"][0]["isResolved"] is True
    assert gh_state["pr"] == "MERGED"
    main = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert main == reviewed
    assert not any(level == "ERROR" for level, _message in entries)
    assert _saved_state(repo)["story_id"] == "W9.10"


@pytest.mark.parametrize("dry_run", [False, True])
def test_the_loop_pauses_instead_of_stopping_when_another_story_starts_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    """The handoff routes a follow-up story while the current story's pull
    request is open on a head nobody reviewed, so nothing can recover it."""

    repo, _remote, gh = _remote_pr(tmp_path, monkeypatch)
    entries, log = _logs()
    _bounce_findings(tmp_path, repo, gh, log)
    (repo / "src" / "widget.py").write_text("WIDGET = 9\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "Unaudited change")

    config = _pr_loop_config(repo, dry_run=dry_run)
    config.handoff_full_path.parent.mkdir(parents=True, exist_ok=True)
    config.handoff_full_path.write_text(NEXT_STORY, encoding="utf-8")
    dispatches: list[str] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def sweep(**kwargs: object):
        return recover_orphaned_story(**kwargs, gh_command=gh)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "recover_orphaned_story", sweep)

    assert orchestrator.run_loop(config, max_cycles=1, log=log) == 0

    assert dispatches == []
    assert not any(level == "ERROR" for level, _message in entries)
    if dry_run:
        # The dry run makes no gh calls; it previews the local check.
        [pause] = [m for level, m in entries if level == "WARN" and "UNFINISHED" in m]
        assert pause.startswith("[DRY RUN] Would pause: UNFINISHED STORY")
        assert "https://github.example/acme/repo/pull/7 is still open" in pause
    else:
        [pause] = [m for level, m in entries if level == "PAUSE" and "NOT MERGED" in m]
        assert "https://github.example/acme/repo/pull/7" in pause
        assert "is not the pull request head" in pause
    assert _git(repo, "branch", "--show-current") == BRANCH
    assert "llm-handoff/w9.10-guard-the-widget" not in _git(repo, "branch", "--list")
    assert [finding["number"] for finding in _saved_state(repo)["open_findings"]] == [1]


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


def test_story_report_has_a_summary_and_reviewer_notes(tmp_path: Path) -> None:
    from llm_handoff.publish import _load_state, build_report

    _init(tmp_path)
    _start_story(tmp_path)

    title, body = build_report(_load_state(tmp_path), VERIFIED)

    assert title == "W9.9: Add a widget"
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == ["## Summary", "## Reviewer notes"]
    # The pull request's Files and Commits tabs already list the changes.
    assert "src/widget.py" not in body.split("## Reviewer notes")[0]
    assert "<summary>Work Packet</summary>" not in body
    assert "Reported cost" not in body
    assert body.split("## Reviewer notes")[0].count("Add the widget module.") == 1
    notes = body.split("## Reviewer notes")[1]
    assert "### Story\n\n- **Story:** `W9.9` in epic `E1`\n" in notes
    assert "- **Implemented by:** the `backend` role" in notes
    assert "### Acceptance checks" not in notes
    assert "### Audit\n\n- **Audit verdict:** APPROVED" in notes
    assert "<summary>Quality review</summary>" in body
    assert "The widget does what the packet asked." in body
    assert body.rstrip().endswith(
        "Generated with [llm-handoff](https://github.com/choughton/llm-handoff)"
    )


LONG_OBJECTIVE_ASSIGNMENT = ASSIGNMENT.replace(
    "- **Objective:** Add the widget module.",
    """- **Objective:** Add the widget module so callers can build widgets. It
  replaces the inline widget code in three places. Callers keep their imports.
  Nothing else in the public API changes. The old helper stays for one
  release. Acceptance checks:
  1. `src/widget.py` defines `WIDGET`, and the three callers
     import it instead of defining their own.
  2. The old helper still works.

  Out of scope: renaming the helper.""",
)


def test_story_report_summary_is_three_sentences_and_keeps_the_lists(
    tmp_path: Path,
) -> None:
    from llm_handoff.publish import _load_state, build_report

    _init(tmp_path)
    _start_story(tmp_path, assignment=LONG_OBJECTIVE_ASSIGNMENT)

    _title, body = build_report(_load_state(tmp_path), VERIFIED)

    summary = body.split("## Summary\n\n")[1].split("\n\n## Reviewer notes")[0]
    assert summary == (
        "Add the widget module so callers can build widgets. It replaces the "
        "inline widget code in three places. Callers keep their imports."
    )
    notes = body.split("## Reviewer notes")[1]
    checks = notes.split("### Acceptance checks\n\n")[1].split("### Audit")[0]
    assert checks.startswith(
        "Nothing else in the public API changes. The old helper stays for one "
        "release.\n\n1. `src/widget.py` defines `WIDGET`, and the three callers\n"
        "   import it instead of defining their own.\n2. The old helper still works.\n"
    )
    assert "Acceptance checks:" not in checks
    assert "Out of scope: renaming the helper." in checks
    assert notes.index("### Story") < notes.index("### Acceptance checks")
    assert notes.index("### Acceptance checks") < notes.index("### Audit")


@pytest.mark.parametrize(
    ("objective", "summary", "details"),
    [
        ("", "Add a widget", ""),
        ("One sentence only.", "One sentence only.", ""),
        (
            "Do this: \n- first\n- second",
            "Add a widget",
            "Do this:\n\n- first\n- second",
        ),
        (
            "Fix it, e.g. the parser. Then test it.",
            "Fix it, e.g. the parser. Then test it.",
            "",
        ),
    ],
)
def test_summary_and_details_split(objective: str, summary: str, details: str) -> None:
    from llm_handoff.publish import _summary_and_details

    assert _summary_and_details(objective, "Add a widget") == (summary, details)


def test_a_very_long_opening_sentence_is_shortened() -> None:
    from llm_handoff.publish import _summary_and_details

    summary, details = _summary_and_details(
        "Make " + "the widget work and " * 60 + "ship it.", "Add a widget"
    )

    assert len(summary) <= 500 and summary.endswith("...")
    assert summary.startswith("Make the widget work and")
    assert details == ""


def test_a_story_state_saved_by_an_older_version_still_loads(tmp_path: Path) -> None:
    from llm_handoff.publish import _load_state

    _init(tmp_path)
    _start_story(tmp_path)
    path = tmp_path / STATE_PATH
    state = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps({**state, "log_path": "logs/run.log", "log_offset": 12}),
        encoding="utf-8",
    )

    loaded = _load_state(tmp_path)

    assert loaded is not None and loaded.story_id == "W9.9"
    assert saved_story_base(tmp_path, "W9.9") == state["base_sha"]


def test_a_story_state_without_tracker_fields_loads_with_empty_values(
    tmp_path: Path,
) -> None:
    from llm_handoff.publish import _load_state

    _init(tmp_path)
    _start_story(tmp_path)
    path = tmp_path / STATE_PATH
    state = json.loads(path.read_text(encoding="utf-8"))
    for name in ("tracker_issue", "tracker_url", "selection_basis"):
        state.pop(name)
    path.write_text(json.dumps(state), encoding="utf-8")

    loaded = _load_state(tmp_path)

    assert loaded is not None and loaded.story_id == "W9.9"
    assert (loaded.tracker_issue, loaded.tracker_url, loaded.selection_basis) == (
        "",
        "",
        "",
    )


LINEAR_URL = "https://linear.app/acme/issue/LIN-9/add-a-widget"
LINEAR_ASSIGNMENT = ASSIGNMENT.replace("story_id: W9.9\n", "story_id: LIN-9\n").replace(
    "producer: planner\n",
    f"tracker_issue: ' LIN-9 '\ntracker_url: {LINEAR_URL}\n"
    "selection_basis: priority\nproducer: planner\n",
)
LINEAR_BRANCH = "llm-handoff/lin-9-add-a-widget"


def _tracker_state(repo: Path) -> dict:
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    return {
        name: state[name] for name in ("tracker_issue", "tracker_url", "selection_basis")
    }


def _ensure(repo: Path, content: str) -> None:
    assert (
        ensure_story_branch(
            repo_root=repo,
            prefix="llm-handoff/",
            base_branch=None,
            assignment_content=content,
        )
        is None
    )


def test_ensure_story_branch_saves_the_tracker_fields(tmp_path: Path) -> None:
    _init(tmp_path)

    _ensure(tmp_path, LINEAR_ASSIGNMENT)

    assert _git(tmp_path, "branch", "--show-current") == LINEAR_BRANCH
    assert _tracker_state(tmp_path) == {
        "tracker_issue": "LIN-9",
        "tracker_url": LINEAR_URL,
        "selection_basis": "priority",
    }


def test_ensure_story_branch_saves_empty_tracker_fields_without_them(
    tmp_path: Path,
) -> None:
    _init(tmp_path)

    _ensure(tmp_path, ASSIGNMENT)

    assert _tracker_state(tmp_path) == {
        "tracker_issue": "",
        "tracker_url": "",
        "selection_basis": "",
    }


def test_the_tracker_fields_refresh_and_keep_saved_values(tmp_path: Path) -> None:
    _init(tmp_path)
    _ensure(tmp_path, LINEAR_ASSIGNMENT)
    path = tmp_path / STATE_PATH

    # An assignment without the fields keeps the saved ones and saves nothing.
    without = LINEAR_ASSIGNMENT.replace(f"tracker_url: {LINEAR_URL}\n", "").replace(
        "selection_basis: priority\n", "selection_basis: ''\n"
    )
    before = path.read_text(encoding="utf-8")
    _ensure(tmp_path, without)
    assert path.read_text(encoding="utf-8") == before

    # A changed non-empty value replaces the saved one; on the existing
    # branch after a switch too.
    moved = LINEAR_URL.replace("add-a-widget", "add-the-widget")
    revised = LINEAR_ASSIGNMENT.replace(LINEAR_URL, moved).replace(
        "selection_basis: priority", "selection_basis: manual_order"
    )
    _git(tmp_path, "switch", "-q", "main")
    _ensure(tmp_path, revised)

    assert _git(tmp_path, "branch", "--show-current") == LINEAR_BRANCH
    assert _tracker_state(tmp_path) == {
        "tracker_issue": "LIN-9",
        "tracker_url": moved,
        "selection_basis": "manual_order",
    }
    assert "Add the widget module." in json.loads(path.read_text(encoding="utf-8"))[
        "work_packet"
    ]


def _bounce(state: StoryState) -> str:
    return build_review_bounce_handoff(
        state=state,
        findings=[],
        url="https://example.invalid/pull/7",
        head_sha="0" * 40,
        review_round=1,
        max_rounds=1,
    )


def _deferred(state: StoryState) -> str:
    from llm_handoff.publish import build_deferred_findings_handoff

    return build_deferred_findings_handoff(
        state=state,
        findings=[{"number": 1, "location": "src/widget.py:1", "body": "Edge."}],
        url="https://example.invalid/pull/7",
    )


def _plain_state(**changes: object) -> StoryState:
    base = StoryState(
        branch=BRANCH,
        base_branch="main",
        base_sha="0" * 40,
        story_id="W9.9",
        story_title="Add a widget",
        epic_id="E1",
        work_packet="## Work Packet\n\n- **Objective:** Add the widget module.",
    )
    return replace(base, **changes)


def _frontmatter_keys(handoff: str) -> list[str]:
    return list(yaml.safe_load(handoff.split("---\n")[1]))


def test_generated_handoffs_put_the_tracker_fields_after_story_id() -> None:
    state = _plain_state(
        story_id="LIN-9",
        tracker_issue="LIN-9",
        tracker_url=LINEAR_URL,
        selection_basis="priority",
    )

    assert _frontmatter_keys(_bounce(state)) == [
        "next_agent",
        "reason",
        "epic_id",
        "story_id",
        "tracker_issue",
        "tracker_url",
        "selection_basis",
        "story_title",
        "status",
        "producer",
    ]
    assert _frontmatter_keys(_deferred(state)) == [
        "next_agent",
        "reason",
        "epic_id",
        "story_id",
        "tracker_issue",
        "tracker_url",
        "selection_basis",
        "story_title",
        "status",
        "producer",
    ]
    # Only the non-empty fields are written.
    keys = _frontmatter_keys(_bounce(replace(state, tracker_url="")))
    assert "tracker_url" not in keys and "selection_basis" in keys


def test_generated_handoffs_without_tracker_fields_are_unchanged() -> None:
    state = _plain_state()

    bounce = _bounce(state)
    deferred = _deferred(state)

    assert bounce.startswith(
        "---\n"
        "next_agent: backend\n"
        "reason: Codex review of 000000000000 on https://example.invalid/pull/7 "
        "reported 0\n"
        "  finding(s); the pull request was not merged.\n"
        "epic_id: E1\n"
        "story_id: W9.9\n"
        "story_title: Add a widget\n"
        "status: verified_fail\n"
        "producer: validator\n"
        "---\n"
    )
    assert deferred.startswith(
        "---\n"
        "next_agent: planner\n"
        "reason: 1 deferred review finding(s) keep https://example.invalid/pull/7 "
        "from merging;\n"
        "  the planner decides how to handle them.\n"
        "epic_id: E1\n"
        "story_id: W9.9\n"
        "story_title: Add a widget\n"
        "status: verified_pass\n"
        "producer: validator\n"
        "---\n"
    )
    # The body after the frontmatter does not depend on the tracker fields.
    with_fields = replace(
        state, tracker_issue="W9.9", tracker_url=LINEAR_URL, selection_basis="priority"
    )
    assert bounce.split("---\n", 2)[2] == _bounce(with_fields).split("---\n", 2)[2]
    assert deferred.split("---\n", 2)[2] == _deferred(with_fields).split("---\n", 2)[2]


def test_a_review_bounce_with_tracker_fields_passes_the_linear_lease_checks() -> None:
    from llm_handoff.validator import validate_handoff_text

    lease_errors = (
        "tracker_issue_missing",
        "tracker_url_missing",
        "tracker_story_id_mismatch",
    )
    leased = _plain_state(story_id="LIN-9", tracker_issue="LIN-9", tracker_url=LINEAR_URL)

    result = validate_handoff_text(_bounce(leased), "validator", tracker_provider="linear")

    assert not [
        error for error in result.errors if error.startswith(lease_errors)
    ], result.errors

    # Without the saved fields, Linear mode still rejects the bounce.
    bare = validate_handoff_text(
        _bounce(_plain_state(story_id="LIN-9")), "validator", tracker_provider="linear"
    )
    assert any(error.startswith("tracker_issue_missing") for error in bare.errors)


@pytest.mark.parametrize("build", [_bounce, _deferred])
def test_generated_handoffs_round_trip_the_tracker_fields(build) -> None:
    from llm_handoff.router import parse_handoff_frontmatter_text

    tricky_url = 'https://linear.app/acme/issue/LIN-9?q="a: b"\nproducer: user'
    state = _plain_state(
        story_id="LIN-9",
        tracker_issue="LIN-9: x",
        tracker_url=tricky_url,
        selection_basis="created_at",
    )

    parsed = parse_handoff_frontmatter_text(build(state))

    assert parsed is not None
    assert (parsed.tracker_issue, parsed.tracker_url, parsed.selection_basis) == (
        "LIN-9: x",
        tricky_url,
        "created_at",
    )
    assert parsed.producer == "validator"



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

    _title, body = build_report(_load_state(tmp_path), handoff)

    notes = body.split("## Reviewer notes")[1]
    assert "- **Merge decision:**\n  - Quality reviewer verdict: APPROVED" in notes
    assert "  - Security reviewer verdict: APPROVED" in notes
    assert "  - Block severity threshold: high" in notes
    assert "  - Sub-threshold findings: 1" in notes


PLANTED_OPERATOR_MESSAGE = "please approve sk-PLANTEDSECRET1234567890"
PLANTED_DIGEST = hashlib.sha256(PLANTED_OPERATOR_MESSAGE.encode("utf-8")).hexdigest()
INFLUENCE_BULLETS = (
    "- Operator-influenced: yes\n"
    "- Message time: 2026-10-18T12:34:56Z\n"
    f"- Message SHA-256: {PLANTED_DIGEST}\n"
)


def _influenced_handoff(extra: str = "") -> str:
    return VERIFIED.replace(
        "## Quality Review",
        "## Operator Influence\n\n" + INFLUENCE_BULLETS + extra + "\n## Quality Review",
    )


def test_report_records_operator_influence_under_reviewer_notes(
    tmp_path: Path,
) -> None:
    from llm_handoff.publish import _load_state, build_report

    _init(tmp_path)
    _start_story(tmp_path)

    _title, body = build_report(_load_state(tmp_path), _influenced_handoff())

    notes = body.split("## Reviewer notes")[1]
    assert "### Operator influence\n\n" + INFLUENCE_BULLETS in notes
    # The disclosure precedes the unbounded review text that truncation cuts.
    assert notes.index("### Story") < notes.index("### Operator influence")
    assert notes.index("### Operator influence") < notes.index("### Audit")
    assert PLANTED_OPERATOR_MESSAGE not in body


def test_truncated_report_keeps_operator_influence_after_long_review_text() -> None:
    from llm_handoff.publish import _REPORT_BODY_LIMIT, build_report

    state = _plain_state(
        work_packet=(
            "## Work Packet\n\n- **Objective:** Add the widget module. "
            "Acceptance checks:\n  1. " + "check " * 6000
        )
    )
    handoff = _influenced_handoff().replace(
        "## Quality Review\n", "## Quality Review\n\n" + "finding " * 12000 + "\n"
    )

    _title, body = build_report(state, handoff)

    assert body.endswith("(report truncated)\n")
    assert len(body) <= _REPORT_BODY_LIMIT + len("\n\n(report truncated)\n")
    assert "### Operator influence\n\n" + INFLUENCE_BULLETS in body
    assert PLANTED_OPERATOR_MESSAGE not in body


def test_report_keeps_only_the_time_and_digest_from_the_influence_section(
    tmp_path: Path,
) -> None:
    from llm_handoff.publish import _load_state, build_report

    _init(tmp_path)
    _start_story(tmp_path)
    handoff = _influenced_handoff(extra=f"- Note: {PLANTED_OPERATOR_MESSAGE}\n")

    _title, body = build_report(_load_state(tmp_path), handoff)

    assert "### Operator influence\n\n" + INFLUENCE_BULLETS in body
    assert PLANTED_OPERATOR_MESSAGE not in body
    assert "sk-PLANTEDSECRET" not in body


def test_report_without_operator_influence_is_unchanged(tmp_path: Path) -> None:
    from llm_handoff.publish import _load_state, build_report

    _init(tmp_path)
    _start_story(tmp_path)
    state = _load_state(tmp_path)

    _title, plain = build_report(state, VERIFIED)
    _title, influenced = build_report(state, _influenced_handoff())

    assert "Operator influence" not in plain
    assert "Operator-influenced" not in plain
    assert (
        influenced.replace("### Operator influence\n\n" + INFLUENCE_BULLETS + "\n", "")
        == plain
    )


def test_report_from_a_rendered_fan_out_handoff_records_influence(
    tmp_path: Path,
) -> None:
    from llm_handoff.audit_review import (
        OPERATOR_INFLUENCE_HEADING,
        OperatorInfluence,
        operator_influence_bullets,
    )
    from llm_handoff.publish import _load_state, build_report

    _init(tmp_path)
    _start_story(tmp_path)
    influence = OperatorInfluence(
        at="2026-10-18T12:34:56Z", message_sha256=PLANTED_DIGEST
    )
    section = "\n".join(
        [OPERATOR_INFLUENCE_HEADING, "", *operator_influence_bullets(influence), ""]
    )
    handoff = VERIFIED.replace("## Quality Review", section + "\n## Quality Review")

    _title, body = build_report(_load_state(tmp_path), handoff)

    assert "### Operator influence\n\n" + INFLUENCE_BULLETS in body


HANDOFF = Path("docs/handoff/HANDOFF.md")


def _merged_story_branch(
    tmp_path: Path, story_files: dict[str, str] | None = None
) -> tuple[Path, Path, str]:
    """A story whose pull request was merged on the remote while the checkout
    stayed on the story branch, after a publish pause cleared its state."""

    repo, remote, _gh, _env = _with_remote(tmp_path)
    _start_story(repo)
    if story_files:
        _commit_all(repo, story_files, "More story work")
    _git(repo, "push", "-q", "origin", BRANCH)
    git_dir = [
        "git", "-c", "user.name=llm-handoff tests",
        "-c", "user.email=tests@example.invalid", "--git-dir", str(remote),
    ]
    merge = subprocess.run(
        [*git_dir, "commit-tree", f"refs/heads/{BRANCH}^{{tree}}",
         "-p", "refs/heads/main", "-p", f"refs/heads/{BRANCH}",
         "-m", "Merge pull request #7"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run([*git_dir, "update-ref", "refs/heads/main", merge], check=True)
    (repo / STATE_PATH).unlink()
    (repo / HANDOFF).parent.mkdir(parents=True, exist_ok=True)
    (repo / HANDOFF).write_text("live handoff\n", encoding="utf-8")
    return repo, remote, merge


def _leave(repo: Path, log, **kwargs: object) -> None:
    options: dict[str, object] = {
        "repo_root": repo,
        "prefix": "llm-handoff/",
        "base_branch": "main",
        "remote": "origin",
        "handoff_path": HANDOFF,
        "log": log,
    }
    options.update(kwargs)
    leave_merged_story_branch(**options)


def test_a_merged_story_branch_without_state_returns_to_the_base_branch(
    tmp_path: Path,
) -> None:
    repo, _remote, merge = _merged_story_branch(tmp_path)
    entries, log = _logs()

    _leave(repo, log)

    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "HEAD") == merge
    assert BRANCH not in _git(repo, "branch", "--list")
    assert (repo / HANDOFF).read_text(encoding="utf-8") == "live handoff\n"
    [(level, message)] = entries
    assert level == "INFO"
    assert BRANCH in message and "origin/main" in message
    assert "Merge pull request #7" in message and "nothing was pushed" in message


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unmerged", "commits that are not on origin/main"),
        ("dirty", "uncommitted files other than docs/handoff/HANDOFF.md: `notes.txt`"),
        ("local_base_ahead", "`main` has commits that are not on origin/main"),
        ("fetch_fails", "git fetch origin main failed"),
    ],
)
def test_a_story_branch_that_is_not_safely_merged_stays_checked_out(
    tmp_path: Path, case: str, reason: str
) -> None:
    repo, _remote, _merge = _merged_story_branch(tmp_path)
    if case == "unmerged":
        _commit_all(repo, {"src/extra.py": "EXTRA = 1\n"}, "Unmerged work")
    elif case == "dirty":
        (repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
    elif case == "local_base_ahead":
        _git(repo, "switch", "-q", "main")
        _commit_all(repo, {"LOCAL.md": "local only\n"}, "Local-only commit")
        _git(repo, "switch", "-q", BRANCH)
    else:
        _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    head = _git(repo, "rev-parse", "HEAD")
    entries, log = _logs()

    _leave(repo, log)

    assert _git(repo, "branch", "--show-current") == BRANCH
    assert _git(repo, "rev-parse", "HEAD") == head
    [(level, message)] = entries
    assert level == "WARN" and BRANCH in message and reason in message


def test_leaving_a_story_branch_does_not_overwrite_the_live_handoff(
    tmp_path: Path,
) -> None:
    """The base branch changed the tracked handoff after the merge, so the
    switch would overwrite the uncommitted live handoff; git refuses it."""

    repo, remote, _merge = _merged_story_branch(
        tmp_path, {HANDOFF.as_posix(): "template\n"}
    )
    _commit_on_remote(
        tmp_path, remote, "main", {HANDOFF.as_posix(): "template v2\n"}, "Template"
    )
    entries, log = _logs()

    _leave(repo, log)

    assert _git(repo, "branch", "--show-current") == BRANCH
    assert (repo / HANDOFF).read_text(encoding="utf-8") == "live handoff\n"
    [(level, message)] = entries
    assert level == "WARN" and "`git switch main` failed" in message


@pytest.mark.parametrize("case", ["saved_state", "base_branch_unset", "not_a_story"])
def test_leaving_a_story_branch_needs_a_stateless_story_branch_and_a_base(
    tmp_path: Path, case: str
) -> None:
    repo, _remote, _merge = _merged_story_branch(tmp_path)
    kwargs: dict[str, object] = {}
    if case == "saved_state":
        from llm_handoff.publish import _save_state

        _save_state(repo, StoryState(
            branch=BRANCH, base_branch="main", base_sha="0" * 40, story_id="W9.9",
            story_title="Add a widget", epic_id="E1", work_packet="",
        ))
    elif case == "base_branch_unset":
        kwargs["base_branch"] = None
    else:
        kwargs["prefix"] = "story/"
    entries, log = _logs()

    _leave(repo, log, **kwargs)

    assert _git(repo, "branch", "--show-current") == BRANCH
    assert entries == []


def test_run_loop_leaves_a_merged_story_branch_before_the_planner_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator merged the paused story's pull request but stayed on its
    branch; the planner's next assignment must not stop on that branch."""

    repo, _remote, _merge = _merged_story_branch(tmp_path)
    config = _pr_loop_config(
        repo,
        publish={"mode": "pr", "base_branch": "main", "wait_for_reviewers": ["codex"]},
    )
    config.handoff_full_path.write_text(VERIFIED, encoding="utf-8")
    dispatches: list[str] = []

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append(_git(repo, "branch", "--show-current"))
        path.write_text(NEXT_STORY, encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def validate(path: Path, previous_agent: str, **_kwargs: object):
        return ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="backend"
        )

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    entries, log = _logs()

    orchestrator.run_loop(config, max_cycles=1, log=log)

    assert dispatches == ["main"]
    assert any(
        level == "INFO" and f"Story branch `{BRANCH}`" in message
        for level, message in entries
    )
    assert not any("Story branch not ready" in message for _level, message in entries)
