"""A start after an interrupted ``pr`` publish finishes it safely.

Each test builds a real repository with a bare remote and a fake ``gh`` and
records the ``git`` and ``gh`` calls the resume makes.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from llm_handoff import orchestrator, publish
from llm_handoff.config import DispatchConfig
from llm_handoff.pr_review import ReviewGate
from llm_handoff.publish import (
    STATE_PATH,
    PublishOutcome,
    StoryState,
    ensure_story_branch,
    resume_interrupted_publish,
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
story_id: {story_id}
story_title: Add a widget
status: {status}
scope_sha: {scope_sha}
close_type: story
producer: auditor
---

# Audit Report - W9.9

## Verdict

APPROVED
"""

BRANCH = "llm-handoff/w9.9-add-a-widget"
HANDOFF_PATH = Path("docs/handoff/HANDOFF.md")
PR_URL = "https://github.example/acme/repo/pull/7"
BOT = "chatgpt-codex-connector[bot]"
PAUSE_PREFIX = "INTERRUPTED PUBLISH:"

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
git_dir = ["git", "--git-dir", os.environ["FAKE_GH_REMOTE"]]
if args[:2] == ["pr", "create"]:
    state["pr"] = "OPEN"
    save()
    print("https://github.example/acme/repo/pull/7")
elif args[:2] == ["pr", "view"]:
    if state.get("fail_view"):
        sys.exit("HTTP 502: Bad Gateway")
    if "pr" not in state:
        sys.exit("no pull requests found for branch \"x\"")
    # A merged pull request keeps reporting the head it merged after its
    # branch is deleted.
    head = state.get("merged_head") or subprocess.run(
        [*git_dir, "rev-parse", "-q", "--verify", f"refs/heads/{args[2]}"],
        capture_output=True, text=True,
    ).stdout.strip()
    print(json.dumps({"number": 7, "url": "https://github.example/acme/repo/pull/7",
                      "state": state["pr"], "headRefOid": head}))
elif args[:2] == ["pr", "edit"]:
    pass
elif args[:2] == ["pr", "merge"]:
    branch = args[2]
    subprocess.run(
        [*git_dir, "update-ref", "refs/heads/main", f"refs/heads/{branch}"],
        check=True,
    )
    state["merged_head"] = subprocess.run(
        [*git_dir, "rev-parse", f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    state["pr"] = "MERGED"
    save()
elif args[0] == "api":
    endpoint = args[-1]
    if endpoint == "graphql":
        nodes = [
            {"id": t["id"], "isResolved": t["isResolved"],
             "comments": {"nodes": [{"databaseId": None,
                                     "fullDatabaseId": str(t["comment_id"])}]}}
            for t in state.get("threads", [])
        ]
        print(json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": "c0"},
                              "nodes": nodes}}}}}))
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


def _remote_git(remote: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(remote), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


class Story:
    """A story branch whose publish was interrupted, with its fake ``gh``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.repo = tmp_path / "repo"
        self.remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
            check=True,
        )
        repo = self.repo
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.name", "llm-handoff tests")
        _git(repo, "config", "user.email", "tests@example.invalid")
        _git(repo, "config", "core.autocrlf", "false")
        (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
        (repo / "README.md").write_text("repo\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "initial")
        _git(repo, "remote", "add", "origin", str(self.remote))
        _git(repo, "push", "-q", "-u", "origin", "main")
        script = tmp_path / "fake_gh.py"
        script.write_text(FAKE_GH, encoding="utf-8")
        self.gh = [sys.executable, str(script)]
        monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.jsonl"))
        monkeypatch.setenv("FAKE_GH_REMOTE", str(self.remote))
        monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "gh_state.json"))
        assert (
            ensure_story_branch(
                repo_root=repo,
                prefix="llm-handoff/",
                base_branch=None,
                assignment_content=ASSIGNMENT,
            )
            is None
        )
        self.commit({"src/widget.py": "WIDGET = 1\n"}, "W9.9 Add a widget")
        self.audited = self.head()
        # The live handoff is uncommitted, as in a real run.
        self.handoff = self.verified()
        (repo / HANDOFF_PATH).parent.mkdir(parents=True)
        (repo / HANDOFF_PATH).write_text(self.handoff, encoding="utf-8")
        self.git_calls: list[list[str]] = []
        original = publish._git

        def recording_git(repo_root: Path, args: list[str]):
            self.git_calls.append(list(args))
            return original(repo_root, args)

        monkeypatch.setattr(publish, "_git", recording_git)
        self.entries: list[tuple[str, str]] = []
        self.questions: list[str] = []

    def log(self, level: str, message: str) -> None:
        self.entries.append((level, message))

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD")

    def commit(self, files: dict[str, str], message: str) -> None:
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", message)

    def verified(self, **values: str) -> str:
        fields = {"story_id": "W9.9", "status": "verified_pass", "scope_sha": self.audited}
        fields.update(values)
        return VERIFIED.format(**fields)

    def set_gh(self, **values: object) -> None:
        path = self.tmp_path / "gh_state.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        state.update(values)
        path.write_text(json.dumps(state), encoding="utf-8")

    def gh_calls(self) -> list[list[str]]:
        path = self.tmp_path / "gh.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def gh_kinds(self) -> list[list[str]]:
        return [call[:2] for call in self.gh_calls()]

    def pushes(self) -> list[list[str]]:
        return [args for args in self.git_calls if args[:1] == ["push"]]

    def state(self) -> StoryState:
        loaded = publish._load_state(self.repo)
        assert loaded is not None
        return loaded

    def save_state(self, **changes: object) -> None:
        path = self.repo / STATE_PATH
        path.write_text(
            json.dumps(asdict(replace(self.state(), **changes)), indent=2),
            encoding="utf-8",
        )

    def push_branch(self) -> None:
        _git(self.repo, "push", "-q", "-u", "origin", BRANCH)

    def merge_on_github(self) -> None:
        """What a `gh pr merge --merge --delete-branch` that went through did."""

        self.push_branch()
        _remote_git(self.remote, "update-ref", "refs/heads/main", self.head())
        _remote_git(self.remote, "update-ref", "-d", f"refs/heads/{BRANCH}")
        self.set_gh(pr="MERGED", merged_head=self.head())

    def confirm_yes(self, question: str) -> bool:
        self.questions.append(question)
        return True

    def confirm_no(self, question: str) -> bool:
        self.questions.append(question)
        return False

    def resume(self, *, confirm: Any = "yes", **kwargs: Any) -> PublishOutcome:
        if confirm == "yes":
            confirm = self.confirm_yes
        options: dict[str, Any] = {
            "repo_root": self.repo,
            "remote": "origin",
            "auto_merge": True,
            "wait_for_checks": False,
            "handoff_content": self.handoff,
            "log": self.log,
            "handoff_path": HANDOFF_PATH,
            "confirm": confirm,
            "gh_command": self.gh,
        }
        options.update(kwargs)
        return resume_interrupted_publish(**options)

    def assert_paused(self, outcome: PublishOutcome, *needles: str) -> None:
        assert outcome.stop is True and outcome.exit_code == 0
        assert outcome.message.startswith(PAUSE_PREFIX)
        assert f"`{BRANCH}`" in outcome.message
        assert "git switch main" in outcome.message
        assert "start the dispatcher again" in outcome.message
        for needle in needles:
            assert needle in outcome.message
        assert (self.repo / STATE_PATH).exists()
        assert self.pushes() == []
        assert self.questions == []
        kinds = self.gh_kinds()
        for write in (["pr", "create"], ["pr", "edit"], ["pr", "merge"]):
            assert write not in kinds


@pytest.fixture
def story(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Story:
    return Story(tmp_path, monkeypatch)


def _fail_confirm(question: str) -> bool:
    raise AssertionError(f"confirm must not be asked: {question}")


def test_states_that_are_not_an_interrupted_publish_are_left_alone(
    story: Story,
) -> None:
    nothing = PublishOutcome()
    original = story.state()

    story.save_state(paused_publish="push")
    assert story.resume(confirm=_fail_confirm) == nothing
    (story.repo / STATE_PATH).write_text(
        json.dumps(asdict(original), indent=2), encoding="utf-8"
    )

    for handoff in (
        story.verified(status="ready_for_review"),
        story.verified(story_id="W1.1"),
        story.verified(scope_sha="0" * 40),
        story.handoff.replace("close_type: story\n", ""),
        "not a handoff",
    ):
        assert story.resume(confirm=_fail_confirm, handoff_content=handoff) == nothing

    _git(story.repo, "switch", "-q", "main")
    assert story.resume(confirm=_fail_confirm) == nothing
    _git(story.repo, "switch", "-q", BRANCH)

    (story.repo / STATE_PATH).unlink()
    assert story.resume(confirm=_fail_confirm) == nothing

    assert story.gh_calls() == []
    assert story.pushes() == []
    assert story.entries == []


def test_a_pending_review_retry_is_left_to_the_paused_publish_resume(
    story: Story,
) -> None:
    story.save_state(
        review_retry_head=story.head(), review_retry_at="2026-10-19T00:10:00+00:00"
    )

    assert story.resume(confirm=_fail_confirm) == PublishOutcome()
    assert story.gh_calls() == []


def test_a_dirty_story_file_pauses_and_keeps_the_story(story: Story) -> None:
    (story.repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")

    outcome = story.resume()

    story.assert_paused(outcome, "src/widget.py", "uncommitted")
    assert story.gh_calls() == []


def test_a_merge_in_progress_pauses(story: Story) -> None:
    clone = story.tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(story.remote), str(clone)], check=True)
    _git(clone, "config", "user.name", "llm-handoff tests")
    _git(clone, "config", "user.email", "tests@example.invalid")
    (clone / "NOTES.md").write_text("notes\n", encoding="utf-8")
    _git(clone, "add", "NOTES.md")
    _git(clone, "commit", "-q", "-m", "Notes on main")
    _git(clone, "push", "-q", "origin", "main")
    _git(story.repo, "fetch", "-q", "origin")
    _git(story.repo, "merge", "--no-commit", "--no-ff", "origin/main")

    outcome = story.resume()

    story.assert_paused(outcome, "a merge is in progress")
    assert story.gh_calls() == []


def test_an_unaudited_commit_after_the_scope_sha_pauses(story: Story) -> None:
    story.commit({"src/extra.py": "EXTRA = 1\n"}, "Unaudited change")

    outcome = story.resume()

    story.assert_paused(outcome, "is not a clean merge", story.audited[:12])
    assert story.gh_calls() == []


def test_a_failing_pull_request_view_pauses(story: Story) -> None:
    story.set_gh(fail_view=True)

    outcome = story.resume()

    story.assert_paused(outcome, "gh pr view", "HTTP 502")
    assert story.gh_kinds() == [["pr", "view"]]


def test_no_pull_request_yet_pushes_and_opens_one_after_a_clean_base_merge(
    story: Story,
) -> None:
    # The interrupted publish had merged the moved base branch already.
    clone = story.tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(story.remote), str(clone)], check=True)
    _git(clone, "config", "user.name", "llm-handoff tests")
    _git(clone, "config", "user.email", "tests@example.invalid")
    (clone / "NOTES.md").write_text("notes\n", encoding="utf-8")
    _git(clone, "add", "NOTES.md")
    _git(clone, "commit", "-q", "-m", "Notes on main")
    _git(clone, "push", "-q", "origin", "main")
    _git(story.repo, "fetch", "-q", "origin")
    _git(story.repo, "merge", "-q", "--no-edit", "origin/main")
    merged_head = story.head()

    outcome = story.resume()

    assert outcome == PublishOutcome()
    assert story.questions == [f"Resume publishing W9.9 on {BRANCH}? [y/N]"]
    info = [m for level, m in story.entries if level == "INFO" and "interrupted" in m]
    assert len(info) == 1
    assert BRANCH in info[0] and merged_head[:12] in info[0]
    assert "no pull request yet" in info[0]
    assert "push and open a pull request" in info[0]
    kinds = story.gh_kinds()
    assert kinds.count(["pr", "create"]) == 1
    assert ["pr", "edit"] not in kinds
    assert kinds[-1] == ["pr", "merge"]
    assert len(story.pushes()) == 1
    assert _git(story.repo, "branch", "--show-current") == "main"
    assert not (story.repo / STATE_PATH).exists()


def test_an_open_pull_request_is_pushed_to_and_reused(story: Story) -> None:
    story.push_branch()
    story.set_gh(pr="OPEN")

    outcome = story.resume()

    assert outcome == PublishOutcome()
    info = [m for level, m in story.entries if level == "INFO" and "interrupted" in m]
    assert len(info) == 1
    assert PR_URL in info[0] and "push and reuse the open pull request" in info[0]
    kinds = story.gh_kinds()
    assert ["pr", "create"] not in kinds
    assert kinds.count(["pr", "edit"]) == 1
    assert kinds[-1] == ["pr", "merge"]
    assert len(story.pushes()) == 1
    assert _git(story.repo, "branch", "--show-current") == "main"


def test_a_merged_pull_request_is_cleaned_up_without_a_push_or_merge(
    story: Story,
) -> None:
    story.merge_on_github()
    story_head = story.head()

    outcome = story.resume(confirm=_fail_confirm)

    assert outcome == PublishOutcome()
    assert _git(story.repo, "branch", "--show-current") == "main"
    assert story.head() == story_head
    assert BRANCH not in _git(story.repo, "branch", "--list")
    assert not (story.repo / STATE_PATH).exists()
    assert story.pushes() == []
    kinds = story.gh_kinds()
    for write in (["pr", "create"], ["pr", "edit"], ["pr", "merge"]):
        assert write not in kinds
    assert [args[:2] for args in story.git_calls if args[0] in {"switch", "pull"}] == [
        ["switch", "main"],
        ["pull", "--ff-only"],
    ]


def test_a_pull_request_merged_before_an_audited_correction_was_pushed_pauses(
    story: Story,
) -> None:
    # The pull request was merged at the first head while the dispatcher was
    # down; the audited correction after it was never pushed.
    story.merge_on_github()
    merged = story.head()
    story.commit({"src/widget.py": "WIDGET = 2\n"}, "W9.9 Fix the review finding")
    story.audited = story.head()
    story.handoff = story.verified()

    outcome = story.resume(confirm=_fail_confirm)

    story.assert_paused(outcome, f"merged {merged[:12]}", story.audited[:12])
    assert _git(story.repo, "branch", "--show-current") == BRANCH
    assert story.head() == story.audited
    assert story.gh_kinds() == [["pr", "view"]]
    assert [args[:1] for args in story.git_calls if args[0] in {"switch", "pull"}] == []


def test_a_merged_pull_request_with_an_unresolved_thread_pauses(
    story: Story,
) -> None:
    story.merge_on_github()
    story.set_gh(threads=[{"id": "T1", "isResolved": False, "comment_id": 11}])

    outcome = story.resume()

    story.assert_paused(outcome, "merged with 1 unresolved review thread")
    assert _git(story.repo, "branch", "--show-current") == BRANCH


def test_a_merged_pull_request_with_an_unresolved_saved_finding_pauses(
    story: Story,
) -> None:
    story.merge_on_github()
    story.save_state(
        open_findings=({"number": 1, "comment_id": 11, "location": "x", "body": "b"},)
    )

    outcome = story.resume()

    story.assert_paused(outcome, "unresolved review thread")


def test_a_closed_pull_request_pauses_and_is_not_reopened(story: Story) -> None:
    story.push_branch()
    story.set_gh(pr="CLOSED")

    outcome = story.resume()

    story.assert_paused(outcome, "is closed")
    assert story.gh_kinds() == [["pr", "view"]]


@pytest.mark.parametrize("answer", ["decline", "none"])
def test_a_declined_or_unasked_resume_keeps_the_story_and_pushes_nothing(
    story: Story, answer: str
) -> None:
    story.push_branch()
    story.set_gh(pr="OPEN")

    outcome = story.resume(
        confirm=story.confirm_no if answer == "decline" else None
    )

    assert outcome == PublishOutcome()
    assert (story.repo / STATE_PATH).exists()
    assert story.pushes() == []
    assert story.gh_kinds() == [["pr", "view"]]
    warnings = [m for level, m in story.entries if level == "WARN"]
    assert len(warnings) == 1
    assert "not resumed" in warnings[0] and "kept" in warnings[0]
    assert story.questions == (
        [f"Resume publishing W9.9 on {BRANCH}? [y/N]"] if answer == "decline" else []
    )
    assert _git(story.repo, "branch", "--show-current") == BRANCH


def test_a_lost_review_bounce_pauses(story: Story) -> None:
    story.push_branch()
    story.set_gh(
        pr="OPEN", threads=[{"id": "T1", "isResolved": False, "comment_id": 11}]
    )
    story.save_state(
        reviewed_head=story.head(),
        review_rounds=1,
        open_findings=({"number": 1, "comment_id": 11, "location": "x", "body": "b"},),
    )

    outcome = story.resume()

    story.assert_paused(outcome, "review-bounce handoff", "lost")
    assert story.state().review_rounds == 1


def _record_review_waits(
    monkeypatch: pytest.MonkeyPatch, story: Story
) -> list[tuple[datetime, dict]]:
    waits: list[tuple[datetime, dict]] = []
    original = publish.wait_for_codex_review

    def wait(**kwargs: Any):
        waits.append(
            (
                kwargs["requested_at"],
                json.loads((story.repo / STATE_PATH).read_text(encoding="utf-8")),
            )
        )
        return original(**kwargs)

    monkeypatch.setattr(publish, "wait_for_codex_review", wait)
    return waits


def _thumbs_up(created_at: str) -> dict:
    return {"user": {"login": BOT}, "content": "+1", "created_at": created_at}


GATE = ReviewGate(reviewers=("codex",), timeout_seconds=0, poll_seconds=0)
SAVED_AT = "2026-10-19T00:00:00+00:00"


def test_a_resumed_publish_of_the_same_head_reuses_the_saved_request_time(
    story: Story, monkeypatch: pytest.MonkeyPatch
) -> None:
    story.push_branch()
    story.save_state(
        publish_requested_head=story.head(), publish_requested_at=SAVED_AT
    )
    # Codex gave its thumbs-up while the dispatcher was down.
    story.set_gh(
        pr="OPEN",
        api={"issues/7/reactions": [_thumbs_up("2026-10-19T00:05:00Z")]},
    )
    waits = _record_review_waits(monkeypatch, story)

    outcome = story.resume(review=GATE, sleep=lambda _seconds: None)

    assert outcome == PublishOutcome()
    [(requested_at, _saved)] = waits
    assert requested_at == datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert story.gh_kinds()[-1] == ["pr", "merge"]
    assert any("no findings" in m for _level, m in story.entries)


def test_a_new_head_saves_a_new_request_time(
    story: Story, monkeypatch: pytest.MonkeyPatch
) -> None:
    story.push_branch()
    story.save_state(publish_requested_head="a" * 40, publish_requested_at=SAVED_AT)
    story.set_gh(
        pr="OPEN",
        api={"issues/7/reactions": [_thumbs_up("2026-10-19T00:05:00Z")]},
    )
    waits = _record_review_waits(monkeypatch, story)
    before = datetime.now(timezone.utc).replace(microsecond=0)

    outcome = story.resume(review=GATE, sleep=lambda _seconds: None)

    [(requested_at, saved)] = waits
    assert requested_at >= before
    assert saved["publish_requested_head"] == story.head()
    assert datetime.fromisoformat(saved["publish_requested_at"]) == requested_at
    # The old thumbs-up is from before the new push, so no review counts.
    assert outcome.stop is True and "left open" in outcome.message
    assert ["pr", "merge"] not in story.gh_kinds()


def test_a_state_file_without_the_request_fields_still_loads(story: Story) -> None:
    path = story.repo / STATE_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("publish_requested_head")
    data.pop("publish_requested_at")
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = story.state()

    assert loaded.publish_requested_head == ""
    assert loaded.publish_requested_at == ""


# Orchestrator startup


def _startup_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("/logs/dispatch/\n", encoding="utf-8")
    handoff = repo / HANDOFF_PATH
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "---\nnext_agent: user\nreason: \"Wait.\"\nstatus: escalate_to_user\n"
        "producer: planner\n---\n",
        encoding="utf-8",
    )
    (repo / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    return repo


def _startup_config(repo: Path, mode: str, dry_run: bool) -> DispatchConfig:
    return DispatchConfig(
        repo_root=repo,
        poll_interval_seconds=0,
        pause_recovery="off",
        interactive_pause=False,
        dry_run=dry_run,
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        commit_mode="dispatcher",
        enforce_work_packet=True,
        work_packet_opt_out_reason=None,
        publish={"mode": mode},
    )


@pytest.mark.parametrize(
    ("mode", "dry_run", "paused", "expected", "exit_code"),
    [
        ("pr", False, PublishOutcome(), ["paused", "interrupted", "recover"], None),
        ("pr", True, PublishOutcome(), ["recover"], None),
        ("local", False, PublishOutcome(), ["recover"], None),
        (
            "pr",
            False,
            PublishOutcome(handoff="---\nnext_agent: backend\n---\n"),
            ["paused", "recover"],
            None,
        ),
        (
            "pr",
            False,
            PublishOutcome(stop=True, exit_code=0, message="paused"),
            ["paused"],
            0,
        ),
    ],
)
def test_startup_resumes_an_interrupted_publish_after_a_paused_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    dry_run: bool,
    paused: PublishOutcome,
    expected: list[str],
    exit_code: int | None,
) -> None:
    repo = _startup_repo(tmp_path)
    config = _startup_config(repo, mode, dry_run)
    order: list[str] = []
    interrupted_calls: list[dict[str, Any]] = []

    class StopStartup(Exception):
        pass

    def resume_paused(**_kwargs: Any) -> PublishOutcome:
        order.append("paused")
        return paused

    def resume_interrupted(**kwargs: Any) -> PublishOutcome:
        order.append("interrupted")
        interrupted_calls.append(kwargs)
        return PublishOutcome()

    def recover(*_args: Any, **_kwargs: Any) -> None:
        order.append("recover")
        raise StopStartup

    monkeypatch.setattr(orchestrator, "leave_merged_story_branch", lambda **_k: None)
    monkeypatch.setattr(orchestrator, "resume_paused_publish", resume_paused)
    monkeypatch.setattr(orchestrator, "resume_interrupted_publish", resume_interrupted)
    monkeypatch.setattr(orchestrator, "_recover_interrupted_dispatch", recover)

    if exit_code is None:
        with pytest.raises(StopStartup):
            orchestrator.run_loop(config, log=lambda _level, _text: None)
    else:
        assert orchestrator.run_loop(config, log=lambda _level, _text: None) == exit_code

    assert order == expected
    if "interrupted" in expected:
        [call] = interrupted_calls
        assert call["handoff_path"] == config.handoff_path
        assert call["remote"] == config.publish.remote
        # Without an interactive terminal the resume is never confirmed.
        assert call["confirm"]("Resume publishing W9.9 on x? [y/N]") is False


def test_startup_stops_when_the_interrupted_publish_resume_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _startup_repo(tmp_path)
    config = _startup_config(repo, "pr", False)
    monkeypatch.setattr(orchestrator, "leave_merged_story_branch", lambda **_k: None)
    monkeypatch.setattr(
        orchestrator, "resume_paused_publish", lambda **_k: PublishOutcome()
    )
    monkeypatch.setattr(
        orchestrator,
        "resume_interrupted_publish",
        lambda **_k: PublishOutcome(
            stop=True, exit_code=0, message=f"{PAUSE_PREFIX} paused"
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_recover_interrupted_dispatch",
        lambda *_a, **_k: pytest.fail("startup must stop at the publish pause"),
    )

    assert orchestrator.run_loop(config, log=lambda _level, _text: None) == 0
