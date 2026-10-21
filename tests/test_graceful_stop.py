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


class _TTYStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _ChatLoggerFake:
    def __init__(self, **_kwargs: object) -> None:
        self.chat_line: object | None = None
        self.attached: list[object | None] = []
        self.logged: list[tuple[str, str]] = []

    def __call__(self, level: str, message: str) -> None:
        self.logged.append((level, message))

    def attach_chat_line(self, chat_line: object | None) -> None:
        self.chat_line = chat_line
        self.attached.append(chat_line)


class _IdleKeyReader:
    def __init__(self) -> None:
        self.open_count = 0
        self.close_count = 0

    def open(self) -> None:
        self.open_count += 1

    def read_chars(self, timeout: float) -> str:
        return ""

    def close(self) -> None:
        self.close_count += 1


def _chat_run_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dry_run: bool,
    stdin_tty: bool = True,
    stdout_tty: bool = True,
    run_loop_error: BaseException | None = None,
    during_run: Any = None,
    seen: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    from llm_handoff import chat_session
    from llm_handoff.chat_input import ChatInput

    (tmp_path / ".git").mkdir()
    # A caller may pass ``seen`` to read it when ``_run_dispatch`` raises.
    seen = {} if seen is None else seen
    seen.update({"factory_calls": 0, "logger": None})
    events: list[str] = seen.setdefault("events", [])
    real_detach = chat_session.detach

    def recording_detach() -> None:
        events.append("detach")
        real_detach()

    monkeypatch.setattr(chat_session, "detach", recording_detach)

    class Logger(_ChatLoggerFake):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            seen["logger"] = self

    def factory(stdin: object, chat_line: Any, **_kwargs: object) -> ChatInput:
        seen["factory_calls"] += 1
        seen["chat_line"] = chat_line
        seen["factory_kwargs"] = dict(_kwargs)
        return ChatInput(chat_line, _IdleKeyReader(), poll_interval=0.01)

    def fake_run_loop(_config: object, *, log: Any = None) -> int:
        seen["chat_line_during_run"] = log.chat_line
        seen["session_attached_during_run"] = chat_session.SESSION.attached
        seen["worker_during_run"] = chat_session.SESSION.worker
        if during_run is not None:
            during_run(seen)
        if run_loop_error is not None:
            raise run_loop_error
        return 0

    monkeypatch.setattr(main_module, "DispatchLogger", Logger)
    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)
    monkeypatch.setattr(main_module, "chat_input_for_console", factory)
    monkeypatch.setattr(
        main_module, "_set_dispatch_console_title", lambda: ("", False)
    )
    monkeypatch.setattr(main_module.sys, "stdin", _TTYStream(stdin_tty))
    monkeypatch.setattr(main_module.sys, "stdout", _TTYStream(stdout_tty))

    try:
        exit_code = main_module._run_dispatch(
            dry_run=dry_run, use_manual_frontend=False, repo_root=tmp_path
        )
    finally:
        seen["session_attached_after_run"] = chat_session.SESSION.attached
        seen["worker_after_run"] = chat_session.SESSION.worker
    return exit_code, seen


class _FakeChatWorker:
    """Stands in for ``ChatAnswerWorker`` and records how it is used."""

    def __init__(self, events: list[str], **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.events = events
        self.submitted: list[str] = []
        self.stop_calls = 0

    def submit(self, text: str) -> str:
        self.submitted.append(text)
        return "sent"

    def busy(self) -> bool:
        return False

    def wait_idle(self, timeout: float | None = None) -> bool:
        return True

    def stop(self) -> None:
        self.stop_calls += 1
        self.events.append("stop")


def _patch_fake_worker(
    monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any]
) -> list[_FakeChatWorker]:
    """Patch ``ChatAnswerWorker`` in ``__main__``; workers share seen's events."""

    instances: list[_FakeChatWorker] = []
    events: list[str] = seen.setdefault("events", [])

    def build(**kwargs: Any) -> _FakeChatWorker:
        worker = _FakeChatWorker(events, **kwargs)
        instances.append(worker)
        return worker

    monkeypatch.setattr(main_module, "ChatAnswerWorker", build)
    return instances


def test_run_dispatch_attaches_the_chat_line_for_an_interactive_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, seen = _chat_run_dispatch(tmp_path, monkeypatch, dry_run=False)

    assert exit_code == 0
    assert seen["factory_calls"] == 1
    assert seen["chat_line_during_run"] is seen["chat_line"]
    assert seen["session_attached_during_run"] is True
    assert seen["session_attached_after_run"] is False
    assert seen["logger"].chat_line is None
    assert seen["logger"].attached == [seen["chat_line"], None]


def test_run_dispatch_sends_chat_lines_to_the_answer_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_handoff import chat_session
    from llm_handoff.chat_changes import chat_changes_path

    seen: dict[str, Any] = {}
    workers = _patch_fake_worker(monkeypatch, seen)

    exit_code, seen = _chat_run_dispatch(
        tmp_path, monkeypatch, dry_run=False, seen=seen
    )

    assert exit_code == 0
    assert len(workers) == 1
    worker = workers[0]
    on_submit = seen["factory_kwargs"].get("on_submit")
    assert on_submit == worker.submit
    repo_root = Path(worker.kwargs["repo_root"])
    assert repo_root.resolve() == tmp_path.resolve()
    assert worker.kwargs["handoff_path"] == (
        repo_root / "docs" / "handoff" / "HANDOFF.md"
    ).resolve()
    assert worker.kwargs["changes_path"] == chat_changes_path(repo_root)

    # The context callable reads the session when it is called.
    context = worker.kwargs["context"]
    first, second = object(), object()
    monkeypatch.setattr(chat_session.SESSION, "_context", first)
    assert context() is first
    monkeypatch.setattr(chat_session.SESSION, "_context", second)
    assert context() is second

    assert on_submit("hello") == "sent"
    assert worker.submitted == ["hello"]
    assert not (tmp_path / "logs/dispatch/chat-queue.json").exists()
    assert not hasattr(main_module, "save_chat_message")
    assert not hasattr(main_module, "chat_queue_path")


def test_run_dispatch_attaches_the_worker_only_during_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    workers = _patch_fake_worker(monkeypatch, seen)

    exit_code, seen = _chat_run_dispatch(
        tmp_path, monkeypatch, dry_run=False, seen=seen
    )

    assert exit_code == 0
    assert len(workers) == 1
    assert seen["worker_during_run"] is workers[0]
    assert seen["worker_after_run"] is None


@pytest.mark.parametrize(
    ("run_loop_error", "expected_exit"),
    [(None, 0), (KeyboardInterrupt(), 130), (RuntimeError("x"), None)],
)
def test_run_dispatch_stops_the_worker_before_the_session_detaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_loop_error: BaseException | None,
    expected_exit: int | None,
) -> None:
    seen: dict[str, Any] = {}
    workers = _patch_fake_worker(monkeypatch, seen)

    if expected_exit is None:
        with pytest.raises(RuntimeError):
            _chat_run_dispatch(
                tmp_path,
                monkeypatch,
                dry_run=False,
                run_loop_error=run_loop_error,
                seen=seen,
            )
    else:
        exit_code, seen = _chat_run_dispatch(
            tmp_path,
            monkeypatch,
            dry_run=False,
            run_loop_error=run_loop_error,
            seen=seen,
        )
        assert exit_code == expected_exit

    assert len(workers) == 1
    assert workers[0].stop_calls == 1
    assert seen["events"] == ["stop", "detach"]
    assert seen["worker_after_run"] is None
    assert seen["session_attached_after_run"] is False
    assert seen["logger"].chat_line is None


def test_run_dispatch_writes_chat_answers_through_the_logger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import functools

    from llm_handoff import chat_session
    from llm_handoff.chat_answers import (
        CHAT_ANSWER_PREFIX,
        ChatAnswerWorker,
        ChatContext,
    )
    from llm_handoff.chat_side_session import ChatSessionResult

    calls: list[tuple[str, str]] = []

    def fake_run_session(provider: str, message: str, **_kwargs: Any) -> Any:
        calls.append((provider, message))
        return ChatSessionResult(
            text="the answer",
            session_id=None,
            exit_code=0,
            error=None,
            edited_paths=(),
        )

    monkeypatch.setattr(
        main_module,
        "ChatAnswerWorker",
        functools.partial(
            ChatAnswerWorker,
            run_session=fake_run_session,
            record_changes=lambda *_a, **_k: None,
            request_takeover=lambda **_k: None,
        ),
    )

    def during_run(seen: dict[str, Any]) -> None:
        monkeypatch.setattr(
            chat_session.SESSION,
            "_context",
            ChatContext(
                role="backend",
                provider="claude",
                story_id="S1",
                log_path=None,
                agent_config=None,
            ),
        )
        seen["notice"] = seen["factory_kwargs"]["on_submit"]("hello")
        seen["idle"] = seen["worker_during_run"].wait_idle(timeout=5)

    exit_code, seen = _chat_run_dispatch(
        tmp_path, monkeypatch, dry_run=False, during_run=during_run
    )

    assert exit_code == 0
    assert seen["idle"] is True
    assert seen["notice"] == "Chat message sent to the chat session."
    assert calls == [("claude", "hello")]
    assert ("AGENT", f"{CHAT_ANSWER_PREFIX}the answer") in seen["logger"].logged
    assert not (tmp_path / "logs/dispatch/chat-queue.json").exists()


def test_run_dispatch_detaches_the_chat_line_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, seen = _chat_run_dispatch(
        tmp_path, monkeypatch, dry_run=False, run_loop_error=KeyboardInterrupt()
    )

    assert exit_code == 130
    assert seen["chat_line_during_run"] is seen["chat_line"]
    assert seen["session_attached_after_run"] is False
    assert seen["logger"].chat_line is None
    assert any(
        "Dispatch interrupted by user" in text for _level, text in seen["logger"].logged
    )


def test_run_dispatch_detaches_the_chat_line_when_run_loop_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeError):
        _chat_run_dispatch(
            tmp_path, monkeypatch, dry_run=False, run_loop_error=RuntimeError("x")
        )

    from llm_handoff import chat_session

    assert chat_session.SESSION.attached is False


@pytest.mark.parametrize(
    ("dry_run", "stdin_tty", "stdout_tty"),
    [(True, True, True), (False, False, True), (False, True, False)],
)
def test_run_dispatch_keeps_the_chat_line_off_without_an_interactive_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    stdin_tty: bool,
    stdout_tty: bool,
) -> None:
    seen: dict[str, Any] = {}
    workers = _patch_fake_worker(monkeypatch, seen)

    exit_code, seen = _chat_run_dispatch(
        tmp_path,
        monkeypatch,
        dry_run=dry_run,
        stdin_tty=stdin_tty,
        stdout_tty=stdout_tty,
        seen=seen,
    )

    assert exit_code == 0
    assert seen["factory_calls"] == 0
    assert seen["chat_line_during_run"] is None
    assert seen["session_attached_during_run"] is False
    assert seen["logger"].attached == []
    assert workers == []
    assert seen["worker_during_run"] is None
    assert seen["events"] == ["detach"]


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
