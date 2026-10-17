"""Graceful stop requests (`llm-handoff stop`) and the dispatch lock message.

The stop taking effect only after an implementer dispatch's commit,
validation, and work-claim gate is covered in tests/test_work_claim.py.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from llm_handoff import __main__ as main_module
from llm_handoff import orchestrator
from llm_handoff.config import DispatchConfig
from llm_handoff.operator_prompt import (
    OperatorInputReader,
    StopRequested,
    content_sha,
    wait_for_operator_or_handoff_change,
)
from llm_handoff.stop_request import (
    request_stop,
    stop_request_path,
    stop_requested,
)


USER_HANDOFF = """---
next_agent: user
reason: "Human decision required."
status: escalate_to_user
producer: planner
---

## Question

Which story should run next?
"""
BACKEND_HANDOFF = """## Next Step

- **backend:** Implement Story 1.
"""


def _git_repo(tmp_path: Path, *, ignore_logs: bool = True) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if ignore_logs:
        (tmp_path / ".gitignore").write_text("/logs/dispatch/\n", encoding="utf-8")
    return tmp_path


def _write_lock(repo_root: Path, pid: int) -> Path:
    lock_path = repo_root / ".dispatch.lock"
    lock_path.write_text(
        f"pid: {pid}\ncreated_at: 2026-10-16T09:00:00\nrepo_root: {repo_root}\n",
        encoding="utf-8",
    )
    return lock_path


def _write_handoff(repo_root: Path, content: str) -> None:
    handoff_path = repo_root / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(content, encoding="utf-8")
    (repo_root / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")


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


# --- llm-handoff stop ------------------------------------------------------


def test_stop_writes_a_request_for_a_running_dispatcher(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path)
    _write_lock(repo, os.getpid())

    exit_code = main_module.main(["stop", "--repo-root", str(repo)])

    assert exit_code == 0
    request = stop_request_path(repo)
    assert request.is_file()
    text = request.read_text(encoding="utf-8")
    assert "requested_at:" in text
    assert f"target_pid: {os.getpid()}" in text
    out = capsys.readouterr().out
    assert f"pid {os.getpid()}" in out
    assert "exits with code 0 before the next dispatch" in out


def test_stop_refuses_when_no_dispatcher_holds_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path)

    exit_code = main_module.main(["stop", "--repo-root", str(repo)])

    assert exit_code == 1
    assert not stop_request_path(repo).exists()
    assert "Nothing to stop" in capsys.readouterr().err


def test_stop_refuses_a_lock_whose_pid_is_not_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _git_repo(tmp_path)
    lock_path = _write_lock(repo, 424242)
    monkeypatch.setattr(
        "llm_handoff.stop_request.process_is_running", lambda _pid: False
    )
    monkeypatch.setattr(main_module, "process_is_running", lambda _pid: False)

    exit_code = main_module.main(["stop", "--repo-root", str(repo)])

    assert exit_code == 1
    assert not stop_request_path(repo).exists()
    assert lock_path.exists(), "the stale lock is reported, never removed"
    err = capsys.readouterr().err
    assert "pid 424242" in err
    assert "is not running" in err
    assert "424242" in err and ("Get-Process -Id 424242" in err or "ps -p 424242" in err)


def test_stop_refuses_when_git_does_not_ignore_the_request_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path, ignore_logs=False)
    _write_lock(repo, os.getpid())

    exit_code = main_module.main(["stop", "--repo-root", str(repo)])

    assert exit_code == 1
    assert not stop_request_path(repo).exists()
    assert "Add /logs/dispatch/ to .gitignore" in capsys.readouterr().err


# --- dispatch lock and startup ----------------------------------------------


def test_run_dispatch_lock_refusal_names_the_pid_and_how_to_check_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".git").mkdir()
    lock_path = _write_lock(tmp_path, 424242)
    monkeypatch.setattr(
        "llm_handoff.stop_request.process_is_running", lambda _pid: False
    )
    monkeypatch.setattr(
        main_module, "run_loop", lambda *_a, **_k: pytest.fail("must not run")
    )

    exit_code = main_module._run_dispatch(
        dry_run=True, use_manual_frontend=False, repo_root=tmp_path
    )

    assert exit_code == 1
    assert lock_path.exists()
    err = capsys.readouterr().err
    assert "Another llm-handoff dispatcher appears to be running" in err
    assert "names pid 424242, started 2026-10-16T09:00:00" in err
    assert "may be stale" in err
    assert "Get-Process -Id 424242" in err or "ps -p 424242" in err


def test_run_dispatch_discards_a_stop_request_from_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    request_stop(tmp_path, target_pid=os.getpid() + 1)
    logged: list[tuple[str, str]] = []
    captured: dict[str, bool] = {}

    class FakeDispatchLogger:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __call__(self, level: str, message: str) -> None:
            logged.append((level, message))

    def fake_run_loop(_config: object, *, log: object = None) -> int:
        captured["request_during_run"] = stop_request_path(tmp_path).exists()
        request_stop(tmp_path)
        return 0

    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module._run_dispatch(
        dry_run=True, use_manual_frontend=False, repo_root=tmp_path
    )

    assert exit_code == 0
    assert captured["request_during_run"] is False
    assert any(
        level == "WARN" and "Removed a stop request left from before this run" in text
        for level, text in logged
    )
    # A request the run did not honor does not outlive it.
    assert not stop_request_path(tmp_path).exists()
    assert not (tmp_path / ".dispatch.lock").exists()


def test_stop_refuses_a_lock_that_never_records_a_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _git_repo(tmp_path)
    (repo / ".dispatch.lock").write_text("", encoding="utf-8")
    sleeps: list[float] = []
    monkeypatch.setattr(main_module.time, "sleep", sleeps.append)

    exit_code = main_module.main(["stop", "--repo-root", str(repo)])

    assert exit_code == 1
    assert sleeps, "a starting dispatcher gets time to write its pid"
    assert not stop_request_path(repo).exists()
    assert "does not record a pid" in capsys.readouterr().err


def test_run_dispatch_keeps_a_stop_request_for_its_own_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `llm-handoff stop` can accept the new lock before startup cleans up.
    (tmp_path / ".git").mkdir()
    request_stop(tmp_path, target_pid=os.getpid())
    captured: dict[str, bool] = {}

    class FakeDispatchLogger:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __call__(self, level: str, message: str) -> None:
            pytest.fail(f"unexpected log line: {level} {message}")

    def fake_run_loop(_config: object, *, log: object = None) -> int:
        captured["request_during_run"] = stop_requested(tmp_path, pid=os.getpid())
        return 0

    monkeypatch.setattr(main_module, "DispatchLogger", FakeDispatchLogger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module._run_dispatch(
        dry_run=True, use_manual_frontend=False, repo_root=tmp_path
    )

    assert exit_code == 0
    assert captured["request_during_run"] is True


def test_stop_request_for_another_pid_does_not_apply(tmp_path: Path) -> None:
    request_stop(tmp_path, target_pid=os.getpid() + 1)

    assert stop_requested(tmp_path, pid=os.getpid()) is False
    assert stop_requested(tmp_path, pid=os.getpid() + 1) is True
    request_stop(tmp_path)
    assert stop_requested(tmp_path, pid=os.getpid()) is True


# --- run_loop stop points ---------------------------------------------------


def test_run_loop_stops_before_the_first_dispatch_when_a_request_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_handoff(tmp_path, BACKEND_HANDOFF)
    request_stop(tmp_path)
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda *_a, **_k: pytest.fail("no dispatch may start"),
    )

    exit_code = orchestrator.run_loop(
        _config(tmp_path), log=lambda level, text: logs.append((level, text))
    )

    assert exit_code == 0
    assert not stop_request_path(tmp_path).exists()
    texts = [text for _level, text in logs]
    assert any(text.startswith("Stop requested (") for text in texts)
    assert (
        "PAUSE",
        "Stopped at the operator's request before the next dispatch. "
        "Exiting with code 0.",
    ) in logs
    assert not any(text.startswith("--- Cycle") for text in texts)


def test_run_loop_stops_while_waiting_at_a_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_handoff(tmp_path, USER_HANDOFF)
    logs: list[tuple[str, str]] = []
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        # The operator runs `llm-handoff stop` during the second poll.
        sleeps.append(seconds)
        if len(sleeps) == 2:
            request_stop(tmp_path)
        if len(sleeps) > 5:
            pytest.fail("the pause did not notice the stop request")

    monkeypatch.setattr(orchestrator.time, "sleep", fake_sleep)

    exit_code = orchestrator.run_loop(
        _config(tmp_path, poll_interval_seconds=1),
        log=lambda level, text: logs.append((level, text)),
    )

    assert exit_code == 0
    assert len(sleeps) == 2
    assert not stop_request_path(tmp_path).exists()
    texts = [text for _level, text in logs]
    escalation = next(i for i, t in enumerate(texts) if "ESCALATION DETECTED" in t)
    seen = next(i for i, t in enumerate(texts) if t.startswith("Stop requested ("))
    stopped = texts.index(
        "Stopped at the operator's request before the next dispatch. "
        "Exiting with code 0."
    )
    assert escalation < seen < stopped


def test_operator_prompt_wait_returns_when_a_stop_is_requested(
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_text(USER_HANDOFF, encoding="utf-8")
    checks: list[int] = []

    def stop_requested() -> bool:
        checks.append(1)
        return len(checks) >= 2

    result = wait_for_operator_or_handoff_change(
        OperatorInputReader(io.StringIO("")),
        handoff,
        content_sha(USER_HANDOFF),
        0.01,
        lambda _level, _text: None,
        stop_requested=stop_requested,
    )

    assert isinstance(result, StopRequested)
    assert len(checks) == 2
