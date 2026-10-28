"""Dispatcher commit of the project state file after a proposal approval."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from llm_handoff.dispatcher_commit import (
    ProjectStateCommitResult,
    commit_approved_project_state,
)


HANDOFF = Path("docs/handoff/HANDOFF.md")
STATE = Path("PROJECT_STATE.md")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _repo(tmp_path: Path, state: Path = STATE) -> Path:
    (tmp_path / HANDOFF).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / HANDOFF).write_text("# Handoff template\n", encoding="utf-8")
    (tmp_path / state).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / state).write_text("# Project State\n", encoding="utf-8")
    (tmp_path / "COMPLETED_WORK.md").write_text("# Completed\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("logs/\n", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "llm-handoff tests")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _commit(repo: Path, state: Path = STATE) -> ProjectStateCommitResult:
    return commit_approved_project_state(
        repo_root=repo, handoff_path=HANDOFF, project_state_path=state
    )


def _approve(repo: Path, state: Path = STATE) -> None:
    (repo / HANDOFF).write_text("---\nnext_agent: tpm\n---\n", encoding="utf-8")
    (repo / state).write_text(
        "# Project State\n\n### 20. NEW-EPIC\n", encoding="utf-8"
    )


def _head_files(repo: Path) -> list[str]:
    return _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()


def test_commits_only_the_project_state_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    _approve(repo)
    # A staged handoff must stay out of the commit too.
    _git(repo, "add", HANDOFF.as_posix())

    result = _commit(repo)

    assert result.sha == _git(repo, "rev-parse", "HEAD")
    assert result.sha != head_before
    assert _head_files(repo) == ["PROJECT_STATE.md"]
    assert _git(repo, "rev-parse", "HEAD~1") == head_before
    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject == "Record the approved backlog in PROJECT_STATE.md"
    status = _git(repo, "status", "--porcelain")
    assert status.splitlines() == ["M  docs/handoff/HANDOFF.md"]


def test_supports_a_nondefault_project_state_path(tmp_path: Path) -> None:
    state = Path("docs/state/STATE.md")
    repo = _repo(tmp_path, state)
    _approve(repo, state)

    result = _commit(repo, state)

    assert result.sha is not None
    assert _head_files(repo) == ["docs/state/STATE.md"]


def test_unchanged_state_makes_no_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    (repo / HANDOFF).write_text("---\nnext_agent: tpm\n---\n", encoding="utf-8")

    result = _commit(repo)

    assert result == ProjectStateCommitResult(unchanged=True)
    assert _git(repo, "rev-parse", "HEAD") == head_before


@pytest.mark.parametrize(
    "variant", ["staged", "unstaged", "untracked", "completed_work"]
)
def test_any_other_change_refuses_without_touching_it(
    tmp_path: Path, variant: str
) -> None:
    repo = _repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    _approve(repo)
    if variant == "staged":
        (repo / "src.py").write_text("x = 2\n", encoding="utf-8")
        _git(repo, "add", "src.py")
        expected = "src.py"
    elif variant == "unstaged":
        (repo / "src.py").write_text("x = 2\n", encoding="utf-8")
        expected = "src.py"
    elif variant == "untracked":
        (repo / "notes" / "scratch.txt").parent.mkdir()
        (repo / "notes" / "scratch.txt").write_text("tmp\n", encoding="utf-8")
        expected = "notes/scratch.txt"
    else:
        (repo / "COMPLETED_WORK.md").write_text("# Completed\n- x\n", encoding="utf-8")
        expected = "COMPLETED_WORK.md"
    status_before = _git(repo, "status", "--porcelain", "--untracked-files=all")

    result = _commit(repo)

    assert result.sha is None
    assert result.other_changes == (expected,)
    assert _git(repo, "rev-parse", "HEAD") == head_before
    # Nothing was staged, discarded, or absorbed.
    assert (
        _git(repo, "status", "--porcelain", "--untracked-files=all") == status_before
    )


def test_a_staged_rename_into_the_state_path_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "rm", "-q", STATE.as_posix())
    _git(repo, "mv", "COMPLETED_WORK.md", STATE.as_posix())

    result = _commit(repo)

    assert result.sha is None
    assert "COMPLETED_WORK.md" in result.other_changes


def test_a_deleted_state_file_is_not_committed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    (repo / STATE).unlink()

    result = _commit(repo)

    assert result.sha is None
    assert result.error is not None and "deleted" in result.error
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_a_git_failure_is_reported_without_a_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    _approve(repo)
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    result = _commit(repo)

    assert result.sha is None
    assert result.error is not None and result.error.startswith("git add failed")
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_status_failure_is_reported(tmp_path: Path) -> None:
    # Not a repository: git status fails.
    result = _commit(tmp_path)

    assert result.sha is None
    assert result.error == "git status failed; the working tree could not be listed"
