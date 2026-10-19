"""A leftover Git index lock pauses a real run at startup.

The dispatcher reports the lock and waits for the operator to delete it; it
never deletes, moves, or writes the lock itself.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

import pytest

from llm_handoff import orchestrator
from llm_handoff.config import DispatchConfig
from llm_handoff.stop_request import request_stop, stop_request_path


USER_HANDOFF = """---
next_agent: user
reason: "Human decision required."
status: escalate_to_user
producer: planner
---

## Question

Which story should run next?
"""

LOCK_PAUSE_PREFIX = "GIT INDEX LOCK:"
STOPPED_LINE = (
    "Stopped at the operator's request before the next dispatch. "
    "Exiting with code 0."
)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("/logs/dispatch/\n", encoding="utf-8")
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(USER_HANDOFF, encoding="utf-8")
    (repo / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    return repo


def _write_index_lock(repo: Path) -> Path:
    lock_path = repo / ".git" / "index.lock"
    lock_path.write_text("", encoding="utf-8")
    return lock_path


def _config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "poll_interval_seconds": 0,
        "pause_recovery": "off",
        "interactive_pause": False,
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        "enforce_work_packet": False,
        "work_packet_opt_out_reason": "Tests cover routing without the gate.",
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "invoke_backend_role",
        "invoke_frontend_role",
        "invoke_planner_role",
        "invoke_auditor_role",
        "invoke_finalizer_role",
    ):
        if hasattr(orchestrator, name):
            monkeypatch.setattr(
                orchestrator,
                name,
                lambda *_a, **_k: pytest.fail("no role may be dispatched"),
            )


def _lock_lines(logs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (level, text)
        for level, text in logs
        if "index.lock" in text.lower() or "index lock" in text.lower()
    ]


def _cycle_lines(logs: list[tuple[str, str]]) -> list[str]:
    return [text for _level, text in logs if text.startswith("--- Cycle")]


def test_git_path_result_resolves_relative_to_the_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, list[str]]] = []

    def fake_git(repo_root: Path, command: list[str]) -> str | None:
        calls.append((repo_root, command))
        return ".git/index.lock"

    monkeypatch.setattr(orchestrator, "_run_git_stdout", fake_git)

    lock_path = orchestrator._git_index_lock_path(_config(tmp_path))

    assert calls == [
        (tmp_path, ["git", "rev-parse", "--git-path", "index.lock"])
    ]
    assert lock_path == tmp_path / ".git" / "index.lock"


def test_real_git_resolves_the_repository_lock_path(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)

    lock_path = orchestrator._git_index_lock_path(_config(repo))

    assert lock_path is not None
    assert lock_path.resolve() == (repo / ".git" / "index.lock").resolve()


def test_lock_pauses_startup_until_the_operator_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    lock_path = _write_index_lock(repo)
    _no_dispatch(monkeypatch)
    logs: list[tuple[str, str]] = []
    lock_seen_at_sleep: list[bool] = []
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            # The dispatcher has not touched the lock; the operator deletes it.
            lock_seen_at_sleep.append(lock_path.exists())
            lock_path.unlink()
        elif len(sleeps) == 2:
            # Cycle 1 pauses on the user escalation; stop there.
            request_stop(repo)
        else:
            pytest.fail("the run did not stop")

    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    config = _config(repo, poll_interval_seconds=1)
    checked_path = orchestrator._git_index_lock_path(config)
    assert checked_path is not None
    assert checked_path.resolve() == lock_path.resolve()

    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert lock_seen_at_sleep == [True]
    texts = [text for _level, text in logs]
    pause_index = next(
        i for i, t in enumerate(texts) if t.startswith(LOCK_PAUSE_PREFIX)
    )
    level, pause_text = logs[pause_index]
    assert level == "PAUSE"
    assert pause_text.startswith(f"{LOCK_PAUSE_PREFIX} {checked_path} exists.")
    assert "second Ctrl+C during a dispatcher commit" in pause_text
    assert "no Git process is running in this repository" in pause_text
    assert "delete the file" in pause_text
    assert "resumes when the file is gone" in pause_text
    assert "never deletes the file" in pause_text
    assert "llm-handoff " not in pause_text
    waiting = texts[pause_index + 1]
    assert waiting.startswith("Waiting for ")
    assert waiting.endswith("to be removed; polling every 1s.")
    resumed = texts.index("Git index lock removed. Resuming startup.")
    assert logs[resumed][0] == "PAUSE"
    cycle = texts.index("--- Cycle 1 ---")
    assert pause_index < resumed < cycle


def test_stop_request_during_the_lock_wait_returns_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    lock_path = _write_index_lock(repo)
    _no_dispatch(monkeypatch)
    logs: list[tuple[str, str]] = []
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            request_stop(repo)
        else:
            pytest.fail("the lock wait did not notice the stop request")

    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    exit_code = orchestrator.run_loop(
        _config(repo, poll_interval_seconds=1),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert lock_path.exists()
    assert not stop_request_path(repo).exists()
    assert ("PAUSE", STOPPED_LINE) in logs
    assert _cycle_lines(logs) == []


def test_lock_with_no_polling_returns_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    lock_path = _write_index_lock(repo)
    _no_dispatch(monkeypatch)
    monkeypatch.setattr(
        orchestrator.time, "sleep", lambda _s: pytest.fail("must not poll")
    )
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo, poll_interval_seconds=0),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert lock_path.exists()
    assert [level for level, _text in _lock_lines(logs)] == ["PAUSE"]
    assert _lock_lines(logs)[0][1].startswith(LOCK_PAUSE_PREFIX)
    assert _cycle_lines(logs) == []


def test_dry_run_warns_once_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    lock_path = _write_index_lock(repo)
    monkeypatch.setattr(
        orchestrator.time, "sleep", lambda _s: pytest.fail("must not wait")
    )
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo, dry_run=True),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert lock_path.exists()
    lock_lines = _lock_lines(logs)
    assert len(lock_lines) == 1
    level, text = lock_lines[0]
    assert level == "WARN"
    assert "index.lock" in text
    assert "A real run would pause here" in text
    assert "--- Cycle 1 ---" in _cycle_lines(logs)


def test_no_lock_logs_nothing_about_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    _no_dispatch(monkeypatch)
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert _lock_lines(logs) == []
    assert not (repo / ".git" / "index.lock").exists()
    assert "--- Cycle 1 ---" in _cycle_lines(logs)


def test_lock_check_runs_before_publish_resume_and_interrupted_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    order: list[str] = []
    original_check = orchestrator._wait_for_git_index_lock_removal

    class StopStartup(Exception):
        pass

    def check(*args: Any, **kwargs: Any) -> bool:
        order.append("lock_check")
        return original_check(*args, **kwargs)

    def leave(**_kwargs: Any) -> None:
        order.append("leave_merged_story_branch")

    class Outcome:
        handoff = None
        stop = False
        exit_code = 0

    def resume(**_kwargs: Any) -> Outcome:
        order.append("resume_paused_publish")
        return Outcome()

    def recover(*_args: Any, **_kwargs: Any) -> None:
        order.append("recover_interrupted_dispatch")
        raise StopStartup

    def restore(*_args: Any, **_kwargs: Any) -> None:
        order.append("restore_implementer_leftovers")
        raise StopStartup

    monkeypatch.setattr(orchestrator, "_wait_for_git_index_lock_removal", check)
    monkeypatch.setattr(orchestrator, "leave_merged_story_branch", leave)
    monkeypatch.setattr(orchestrator, "resume_paused_publish", resume)
    monkeypatch.setattr(orchestrator, "_recover_interrupted_dispatch", recover)
    monkeypatch.setattr(orchestrator, "_restore_implementer_leftovers", restore)

    with pytest.raises(StopStartup):
        orchestrator.run_loop(
            _config(
                repo,
                commit_mode="dispatcher",
                enforce_work_packet=True,
                work_packet_opt_out_reason=None,
                publish={"mode": "pr"},
            ),
            log=lambda _level, _text: None,
        )

    assert order == [
        "lock_check",
        "leave_merged_story_branch",
        "resume_paused_publish",
        "recover_interrupted_dispatch",
    ]


def test_lock_path_outside_the_repo_root_is_the_path_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    # A worktree or custom GIT_DIR keeps the lock outside the repo root.
    outside = tmp_path / "elsewhere" / "worktrees" / "repo" / "index.lock"
    outside.parent.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")
    original_git = orchestrator._run_git_stdout

    def fake_git(repo_root: Path, command: list[str]) -> str | None:
        if command == ["git", "rev-parse", "--git-path", "index.lock"]:
            return str(outside)
        return original_git(repo_root, command)

    monkeypatch.setattr(orchestrator, "_run_git_stdout", fake_git)
    _no_dispatch(monkeypatch)
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo, poll_interval_seconds=0),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert outside.exists()
    lock_lines = _lock_lines(logs)
    assert len(lock_lines) == 1
    assert lock_lines[0][1].startswith(f"{LOCK_PAUSE_PREFIX} {outside} exists.")
    assert _cycle_lines(logs) == []


def test_git_failure_skips_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    lock_path = _write_index_lock(repo)
    original_git = orchestrator._run_git_stdout

    def fake_git(repo_root: Path, command: list[str]) -> str | None:
        if command == ["git", "rev-parse", "--git-path", "index.lock"]:
            return None
        return original_git(repo_root, command)

    monkeypatch.setattr(orchestrator, "_run_git_stdout", fake_git)
    _no_dispatch(monkeypatch)
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert lock_path.exists()
    assert _lock_lines(logs) == []
    assert "--- Cycle 1 ---" in _cycle_lines(logs)


def test_lock_path_control_characters_are_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "index‮.lock"
    lock_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        orchestrator, "_git_index_lock_path", lambda _config: lock_path
    )
    logs: list[tuple[str, str]] = []

    keep_going = orchestrator._wait_for_git_index_lock_removal(
        _config(tmp_path),
        lambda level, text: logs.append((level, text)),
        orchestrator._StopRequestWatch(tmp_path),
    )

    assert keep_going is False
    assert lock_path.exists()
    assert "‮" not in logs[0][1]
    assert "\\u202e" in logs[0][1]
