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
    resume_paused_publish,
    save_review_responses,
    saved_story_base,
    settle_deferred_findings,
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
    assert "`src/extra.py`" in outcome.message
    assert "`README.md`" not in outcome.message
    assert "only the conflict resolution" in outcome.message
    assert "git switch main" in outcome.message


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
