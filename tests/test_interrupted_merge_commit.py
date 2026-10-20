"""A start after an interrupted ``merge-commit`` publish finishes it safely.

Each test builds a real repository whose story branch passed its audit, cuts
the publish at one of its steps, and records the ``git`` calls the resume
makes.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

import pytest

from llm_handoff import orchestrator, publish
from llm_handoff.config import DispatchConfig
from llm_handoff.publish import (
    STATE_PATH,
    PublishOutcome,
    ensure_story_branch,
    resume_interrupted_merge_commit,
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
PAUSE_PREFIX = "INTERRUPTED PUBLISH:"
QUESTION = "Resume merging W9.9 into main? [y/N]"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


class Story:
    """An audited story branch whose ``merge-commit`` publish was cut."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.repo = repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.name", "llm-handoff tests")
        _git(repo, "config", "user.email", "tests@example.invalid")
        _git(repo, "config", "core.autocrlf", "false")
        (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
        (repo / "README.md").write_text("repo\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "initial")
        self.base = self.head()
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

    def current(self) -> str:
        return _git(self.repo, "branch", "--show-current")

    def branch_exists(self) -> bool:
        return bool(_git(self.repo, "branch", "--list", BRANCH))

    def commit(self, files: dict[str, str], message: str) -> None:
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", message)

    def verified(self, **values: str) -> str:
        fields = {
            "story_id": "W9.9",
            "status": "verified_pass",
            "scope_sha": self.audited,
        }
        fields.update(values)
        return VERIFIED.format(**fields)

    def merges(self) -> list[list[str]]:
        return [args for args in self.git_calls if args[:1] == ["merge"]]

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
            "handoff_content": self.handoff,
            "handoff_path": HANDOFF_PATH,
            "log": self.log,
            "confirm": confirm,
        }
        options.update(kwargs)
        return resume_interrupted_merge_commit(**options)

    def assert_paused(self, outcome: PublishOutcome, *needles: str) -> None:
        assert outcome.stop is True and outcome.exit_code == 0
        assert outcome.message.startswith(PAUSE_PREFIX)
        for needle in (
            f"`{BRANCH}`",
            "`W9.9`",
            "`main`",
            "git switch main",
            f"git merge --no-ff {BRANCH}",
            f"git branch -d {BRANCH}",
            STATE_PATH.as_posix(),
            "start the dispatcher again",
            *needles,
        ):
            assert needle in outcome.message
        assert (self.repo / STATE_PATH).exists()
        assert self.merges() == []
        assert self.questions == []


@pytest.fixture
def story(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Story:
    return Story(tmp_path, monkeypatch)


def _fail_confirm(question: str) -> bool:
    raise AssertionError(f"confirm must not be asked: {question}")


# (a), (b): on the story branch


def test_a_confirmed_resume_on_the_story_branch_merges_with_no_ff(
    story: Story,
) -> None:
    outcome = story.resume()

    assert outcome == PublishOutcome()
    assert story.questions == [QUESTION]
    info = [m for level, m in story.entries if level == "INFO"]
    assert len(info) == 1
    assert BRANCH in info[0] and story.audited[:12] in info[0]
    assert "Resuming will merge it into `main`" in info[0]
    [merge] = story.merges()
    assert "--no-ff" in merge and merge[-1] == BRANCH
    assert story.current() == "main"
    parents = _git(story.repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1:] == [story.base, story.audited]
    assert not story.branch_exists()
    assert not (story.repo / STATE_PATH).exists()


@pytest.mark.parametrize("answer", ["decline", "none"])
def test_a_declined_or_unasked_resume_keeps_the_state_and_merges_nothing(
    story: Story, answer: str
) -> None:
    outcome = story.resume(confirm=story.confirm_no if answer == "decline" else None)

    assert outcome == PublishOutcome()
    assert outcome.stop is False
    assert (story.repo / STATE_PATH).exists()
    assert story.merges() == []
    warnings = [m for level, m in story.entries if level == "WARN"]
    assert len(warnings) == 1
    assert "not resumed" in warnings[0] and "the story state was kept" in warnings[0]
    assert story.questions == ([QUESTION] if answer == "decline" else [])
    assert story.current() == BRANCH
    assert story.head() == story.audited


# (c): story-branch pauses


def test_a_head_past_the_audited_commit_pauses(story: Story) -> None:
    story.commit({"src/extra.py": "EXTRA = 1\n"}, "Unaudited change")

    outcome = story.resume()

    story.assert_paused(outcome, "is not the audited commit", story.audited[:12])
    assert story.current() == BRANCH


def test_an_uncommitted_story_file_pauses(story: Story) -> None:
    (story.repo / "src" / "widget.py").write_text("WIDGET = 2\n", encoding="utf-8")

    outcome = story.resume()

    story.assert_paused(outcome, "src/widget.py", "uncommitted")


def test_an_uncommitted_handoff_and_dispatch_log_do_not_pause(story: Story) -> None:
    log_file = story.repo / "logs" / "dispatch" / "run.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("log\n", encoding="utf-8")
    # Staged past the ignore rule, so `git status` lists it.
    _git(story.repo, "add", "-f", "logs/dispatch/run.log")
    assert "logs/dispatch/run.log" in _git(story.repo, "status", "--porcelain")

    outcome = story.resume(confirm=story.confirm_no)

    assert outcome == PublishOutcome()
    assert story.questions == [QUESTION]


def test_a_merge_in_progress_on_the_story_branch_pauses(story: Story) -> None:
    _git(story.repo, "switch", "-q", "main")
    story.commit({"NOTES.md": "notes\n"}, "Notes on main")
    _git(story.repo, "switch", "-q", BRANCH)
    _git(story.repo, "merge", "--no-commit", "--no-ff", "main")

    outcome = story.resume()

    story.assert_paused(outcome, "a merge is in progress")


# (d)-(g): on the base branch


def test_an_already_merged_branch_is_deleted_without_asking(story: Story) -> None:
    _git(story.repo, "switch", "-q", "main")
    _git(story.repo, "merge", "-q", "--no-ff", "--no-edit", BRANCH)
    merged = story.head()

    outcome = story.resume(confirm=_fail_confirm)

    assert outcome == PublishOutcome()
    assert not story.branch_exists()
    assert not (story.repo / STATE_PATH).exists()
    assert story.head() == merged
    agent = [m for level, m in story.entries if level == "AGENT"]
    assert agent == [
        f"Finished the interrupted merge of {BRANCH} into main: deleted the "
        "merged branch and cleared the story state."
    ]
    assert story.merges() == []


def test_an_unmerged_branch_on_the_base_branch_pauses(story: Story) -> None:
    _git(story.repo, "switch", "-q", "main")

    outcome = story.resume(confirm=_fail_confirm)

    story.assert_paused(outcome, "not merged")
    assert story.branch_exists()
    assert story.head() == story.base
    assert story.current() == "main"


def test_a_branch_tip_that_is_not_the_audited_commit_pauses(story: Story) -> None:
    story.commit({"src/extra.py": "EXTRA = 1\n"}, "Unaudited change")
    tip = story.head()
    _git(story.repo, "switch", "-q", "main")
    _git(story.repo, "merge", "-q", "--no-ff", "--no-edit", BRANCH)

    outcome = story.resume(confirm=_fail_confirm)

    story.assert_paused(outcome, tip[:12], story.audited[:12])
    assert story.branch_exists()


def test_a_gone_and_merged_branch_clears_the_state(story: Story) -> None:
    _git(story.repo, "switch", "-q", "main")
    _git(story.repo, "merge", "-q", "--no-ff", "--no-edit", BRANCH)
    _git(story.repo, "branch", "-d", BRANCH)

    outcome = story.resume(confirm=_fail_confirm)

    assert outcome == PublishOutcome()
    assert not (story.repo / STATE_PATH).exists()
    info = [m for level, m in story.entries if level == "INFO"]
    assert len(info) == 1 and BRANCH in info[0]


def test_a_gone_and_unmerged_branch_pauses(story: Story) -> None:
    _git(story.repo, "switch", "-q", "main")
    _git(story.repo, "branch", "-D", BRANCH)

    outcome = story.resume(confirm=_fail_confirm)

    story.assert_paused(outcome, "no longer exists", story.audited[:12])


def test_a_merge_in_progress_on_the_base_branch_pauses(story: Story) -> None:
    _git(story.repo, "switch", "-q", "main")
    _git(story.repo, "merge", "--no-commit", "--no-ff", BRANCH)

    outcome = story.resume(confirm=_fail_confirm)

    story.assert_paused(outcome, "a merge into `main` is in progress")
    assert story.branch_exists()


# (h): not an interrupted merge-commit publish


def test_states_that_are_not_an_interrupted_merge_are_left_alone(
    story: Story,
) -> None:
    nothing = PublishOutcome()

    for handoff in (
        story.verified(status="ready_for_review"),
        story.verified(story_id="W1.1"),
        story.verified(scope_sha="0" * 40),
        story.handoff.replace("close_type: story\n", ""),
        "not a handoff",
    ):
        assert story.resume(confirm=_fail_confirm, handoff_content=handoff) == nothing

    _git(story.repo, "switch", "-q", "-c", "elsewhere")
    assert story.resume(confirm=_fail_confirm) == nothing
    _git(story.repo, "switch", "-q", "--detach", story.audited)
    assert story.resume(confirm=_fail_confirm) == nothing
    _git(story.repo, "switch", "-q", BRANCH)

    (story.repo / STATE_PATH).unlink()
    assert story.resume(confirm=_fail_confirm) == nothing

    assert story.entries == []
    assert story.merges() == []
    assert story.current() == BRANCH


# (i): orchestrator startup


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
    ("dry_run", "expected"),
    [(False, ["merge-commit", "recover"]), (True, ["recover"])],
)
def test_startup_in_merge_commit_mode_resumes_only_the_merge_commit_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    expected: list[str],
) -> None:
    repo = _startup_repo(tmp_path)
    config = _startup_config(repo, "merge-commit", dry_run)
    order: list[str] = []
    calls: list[dict[str, Any]] = []

    class StopStartup(Exception):
        pass

    def resume_merge_commit(**kwargs: Any) -> PublishOutcome:
        order.append("merge-commit")
        calls.append(kwargs)
        return PublishOutcome()

    def recover(*_args: Any, **_kwargs: Any) -> None:
        order.append("recover")
        raise StopStartup

    def pr_resume(**_kwargs: Any) -> PublishOutcome:
        order.append("pr")
        return PublishOutcome()

    monkeypatch.setattr(
        orchestrator, "resume_interrupted_merge_commit", resume_merge_commit
    )
    monkeypatch.setattr(orchestrator, "resume_interrupted_publish", pr_resume)
    monkeypatch.setattr(orchestrator, "resume_paused_publish", pr_resume)
    monkeypatch.setattr(orchestrator, "_recover_interrupted_dispatch", recover)

    with pytest.raises(StopStartup):
        orchestrator.run_loop(config, log=lambda _level, _text: None)

    assert order == expected
    if not dry_run:
        [call] = calls
        assert call["handoff_path"] == config.handoff_path
        assert call["handoff_content"].startswith("---\nnext_agent: user")
        # Without an interactive terminal the resume is never confirmed.
        assert call["confirm"](QUESTION) is False


def test_startup_returns_the_merge_commit_pause_exit_code_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _startup_repo(tmp_path)
    config = _startup_config(repo, "merge-commit", False)
    monkeypatch.setattr(
        orchestrator,
        "resume_interrupted_merge_commit",
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
