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


def _install_hooks(repo: Path) -> Path:
    """Hooks that change and stage src.py and leave a marker when run."""

    marker = repo / ".git" / "hook-ran"
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    scripts = {
        "prepare-commit-msg": (
            "#!/bin/sh\n"
            f"echo prepare-commit-msg >> '{marker.as_posix()}'\n"
            "echo 'x = 99' > src.py\n"
            "git add src.py\n"
        ),
        "post-commit": (
            "#!/bin/sh\n"
            f"echo post-commit >> '{marker.as_posix()}'\n"
            "echo 'x = 99' > src.py\n"
            "git add src.py\n"
        ),
    }
    for name, text in scripts.items():
        hook = hooks / name
        hook.write_bytes(text.encode("utf-8"))
        hook.chmod(0o755)
    # Pin the hooks directory in the repository config, so a global
    # core.hooksPath cannot hide these hooks from the control test.
    _git(repo, "config", "core.hooksPath", hooks.as_posix())
    return marker


def test_no_verify_alone_still_runs_the_hooks(tmp_path: Path) -> None:
    # Control for the next test: --no-verify does not stop prepare-commit-msg
    # or post-commit, and both stage src.py. Whether the --only commit tree
    # picks up the prepare-commit-msg change varies by Git version, so only
    # the hook runs and their effect on the checkout are asserted.
    repo = _repo(tmp_path)
    marker = _install_hooks(repo)
    _approve(repo)

    _git(repo, "commit", "-q", "--no-verify", "--only", "-m", "x", "--", "PROJECT_STATE.md")

    ran = marker.read_text(encoding="utf-8").split()
    assert "prepare-commit-msg" in ran
    assert "post-commit" in ran
    assert (repo / "src.py").read_text(encoding="utf-8").strip() == "x = 99"


def test_repository_hooks_do_not_run_and_cannot_widen_the_commit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    marker = _install_hooks(repo)
    head_before = _git(repo, "rev-parse", "HEAD")
    _approve(repo)

    result = _commit(repo)

    assert result.error is None
    assert result.sha == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-parse", "HEAD~1") == head_before
    assert _head_files(repo) == ["PROJECT_STATE.md"]
    assert not marker.exists()
    assert (repo / "src.py").read_text(encoding="utf-8") == "x = 1\n"
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    # `_git` strips the output, so the leading index-status space is gone.
    assert status.splitlines() == ["M docs/handoff/HANDOFF.md"]


def test_a_commit_holding_another_path_is_reported_not_undone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate anything that widens the commit after the scope check.
    import llm_handoff.dispatcher_commit as dispatcher_commit

    repo = _repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    _approve(repo)
    real_git = dispatcher_commit._git

    def widening_git(repo_root: Path, args: list[str]):
        if "commit" in args:
            (repo / "src.py").write_text("x = 2\n", encoding="utf-8")
            only = args.index("--")
            args = [*args[:only], "--", *args[only + 1 :], "src.py"]
        return real_git(repo_root, args)

    monkeypatch.setattr(dispatcher_commit, "_git", widening_git)

    result = _commit(repo)

    head = _git(repo, "rev-parse", "HEAD")
    assert result.sha == head
    assert result.error is not None
    assert "also changes src.py" in result.error
    # The commit stays for the operator to inspect.
    assert _git(repo, "rev-parse", "HEAD~1") == head_before
    assert sorted(_head_files(repo)) == ["PROJECT_STATE.md", "src.py"]


def test_orchestrator_pauses_on_a_widened_commit_without_undoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from llm_handoff import orchestrator

    monkeypatch.setattr(
        orchestrator,
        "commit_approved_project_state",
        lambda **_: ProjectStateCommitResult(
            sha="a" * 40,
            error="the new commit aaaaaaaaaaaa is not a PROJECT_STATE.md-only "
            "commit: it also changes src.py; inspect it before continuing",
        ),
    )
    config = SimpleNamespace(
        repo_root=tmp_path, handoff_path=HANDOFF, project_state_path=STATE
    )
    logged: list[str] = []

    reason = orchestrator._commit_approved_project_state(
        config, lambda *args, **kwargs: logged.append(" ".join(map(str, args)))
    )

    assert reason is not None
    assert reason.startswith("PROPOSAL APPROVAL COMMIT NEEDS REVIEW")
    assert "as aaaaaaaaaaaa" in reason
    assert "also changes src.py" in reason
    assert "not undone" in reason


def test_status_failure_is_reported(tmp_path: Path) -> None:
    # Not a repository: git status fails.
    result = _commit(tmp_path)

    assert result.sha is None
    assert result.error == "git status failed; the working tree could not be listed"
