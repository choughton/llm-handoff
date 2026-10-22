"""CHAT-15: the loop records each role dispatch in the dispatch record."""

from __future__ import annotations

import io
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest

from llm_handoff import __main__ as main_module
from llm_handoff import chat_session, orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.chat_takeover import ChatTakeover
from llm_handoff.config import (
    DISPATCH_RECORD_STATE_PATH,
    AgentConfig,
    DispatchConfig,
)
from llm_handoff.dispatch_record import (
    DispatchEntry,
    dispatch_record_path,
    load_dispatch_record,
    record_dispatch,
)
from llm_handoff.dispatcher_commit import PENDING_COMMIT_SHA
import llm_handoff.ledger as ledger


HANDOFF_RELATIVE_PATH = Path("docs/handoff/HANDOFF.md")
STORY_ID = "REC-1"
PYTHON = sys.executable
PASS_COMMAND = f'"{PYTHON}" -c "import sys; sys.exit(0)"'


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _assignment() -> str:
    return f"""---
next_agent: backend
reason: "Implement the value change."
epic_id: REC
story_id: {STORY_ID}
story_title: Change the value
producer: planner
---

## Task Assignment

**Agent:** backend

## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** src/app.py
- **Files out of bounds:** src/api.py
- **Context:** Keep the module import-free.
- **Verification command:** {PASS_COMMAND}
- **Expected next route:** auditor
"""


def _pending_claim() -> str:
    return f"""---
next_agent: auditor
reason: "Backend work complete; audit requested."
epic_id: REC
story_id: {STORY_ID}
story_title: Change the value
status: ready_for_review
producer: backend
---

# Backend Handback

## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** src/app.py
- **Files out of bounds:** src/api.py
- **Context:** Keep the module import-free.
- **Verification command:** {PASS_COMMAND}
- **Expected next route:** auditor

## Verification Evidence

- **Commands run:** `python -m pytest tests/test_app.py -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {PENDING_COMMIT_SHA}
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""


def _audit_fail() -> str:
    return f"""---
next_agent: backend
reason: "Audit found a gap."
epic_id: REC
story_id: {STORY_ID}
story_title: Change the value
status: verified_fail
producer: auditor
---

# Audit

## Findings

- Value is wrong.
"""


def _init_repo(repo_root: Path) -> Path:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.name", "llm-handoff tests")
    _git(repo_root, "config", "user.email", "tests@example.invalid")
    _git(repo_root, "config", "core.autocrlf", "false")
    _write(repo_root / "src" / "app.py", "VALUE = 1\n")
    _write(repo_root / "src" / "api.py", "API = 1\n")
    _write(repo_root / HANDOFF_RELATIVE_PATH, _assignment())
    _write(repo_root / "PROJECT_STATE.md", "# PROJECT STATE\n")
    _write(repo_root / "AGENTS.md", "dispatch test fixture\n")
    _write(repo_root / ".gitignore", "logs/\n")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "initial")
    return repo_root


def _config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "poll_interval_seconds": 0,
        "pause_recovery": "off",
        "max_consecutive_failures": 3,
        "enforce_work_packet": True,
        "work_packet_verification_timeout_seconds": 60,
        "commit_mode": "dispatcher",
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _no_input_stream() -> io.StringIO:
    raise AssertionError("these runs must not read operator input")


@pytest.fixture(autouse=True)
def _isolated_terminal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", _no_input_stream)
    ledger.reset_bounce(STORY_ID)
    chat_session.clear_role_dispatches()
    yield
    chat_session.clear_role_dispatches()
    ledger.reset_bounce(STORY_ID)


def _finishing_backend(repo_root: Path, session_id: str | None, calls: list[str]):
    def backend(path: Path, **_kwargs: object) -> DispatchResult:
        calls.append("backend")
        _write(repo_root / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return DispatchResult(
            stdout="",
            stderr="",
            exit_code=0,
            elapsed_seconds=0.01,
            session_id=session_id,
        )

    return backend


def _run(config: DispatchConfig, *, max_cycles: int = 1) -> tuple[int, list[tuple[str, str]]]:
    logs: list[tuple[str, str]] = []
    exit_code = orchestrator.run_loop(
        config,
        max_cycles=max_cycles,
        log=lambda level, text: logs.append((level, text)),
    )
    return exit_code, logs


def _record(repo_root: Path) -> list[DispatchEntry]:
    return load_dispatch_record(dispatch_record_path(repo_root))


def _summary(entries: list[DispatchEntry]) -> list[tuple[Any, ...]]:
    return [
        (entry.role, entry.story_id, entry.provider, entry.session_id)
        for entry in entries
    ]


# (a)
def test_a_backend_dispatch_records_role_story_provider_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    calls: list[str] = []
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", calls)
    )
    config = _config(repo)

    exit_code, logs = _run(config)

    assert exit_code == 0
    assert calls == ["backend"]
    entries = _record(repo)
    assert _summary(entries) == [
        ("backend", STORY_ID, config.agents["backend"].provider, "sess-1")
    ]
    assert entries[0].recorded_at
    assert not any("dispatch record" in text for _level, text in logs)


# (b)
def test_a_dispatch_without_a_session_id_records_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, None, [])
    )

    _run(_config(repo))

    entries = _record(repo)
    assert len(entries) == 1
    assert entries[0].role == "backend"
    assert entries[0].session_id is None


# (c)
def test_two_cycles_record_two_entries_in_dispatch_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    config = _config(repo)
    order: list[str] = []
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-b", order)
    )

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
        order.append(subagent_name)
        config.handoff_full_path.write_text(_audit_fail(), encoding="utf-8")
        return SubagentResult(
            stdout="",
            stderr="",
            exit_code=0,
            elapsed_seconds=0.01,
            session_id="sess-a",
        )

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)

    _run(config, max_cycles=2)

    assert order == ["backend", "auditor"]
    assert _summary(_record(repo)) == [
        ("backend", STORY_ID, config.agents["backend"].provider, "sess-b"),
        ("auditor", STORY_ID, config.agents["auditor"].provider, "sess-a"),
    ]


# (d) and the second-run half of check 5
def test_a_stale_record_from_an_earlier_run_is_cleared_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    path = dispatch_record_path(repo)
    record_dispatch(
        path, role="planner", story_id="OLD-1", provider="codex", session_id="old"
    )
    assert len(_record(repo)) == 1
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", [])
    )

    _run(_config(repo))

    assert _summary(_record(repo)) == [
        ("backend", STORY_ID, _config(repo).agents["backend"].provider, "sess-1")
    ]


def test_a_run_with_nothing_to_dispatch_leaves_no_stale_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    path = dispatch_record_path(repo)
    record_dispatch(
        path, role="planner", story_id="OLD-1", provider="codex", session_id="old"
    )
    # Escalated to the user: the run pauses without dispatching any role.
    _write(
        repo / HANDOFF_RELATIVE_PATH,
        "---\nnext_agent: user\nreason: \"Question.\"\n"
        "status: escalate_to_user\nproducer: planner\n---\n\n## Question\n\nWhich?\n",
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda *_a, **_k: pytest.fail("no role may be dispatched"),
    )

    _run(_config(repo))

    assert not path.exists()


# (e)
def test_a_dry_run_leaves_an_existing_record_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    path = dispatch_record_path(repo)
    record_dispatch(
        path, role="planner", story_id="OLD-1", provider="codex", session_id="old"
    )
    before = path.read_bytes()
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda *_a, **_k: pytest.fail("a dry run dispatches nothing"),
    )

    exit_code, _logs = _run(_config(repo, dry_run=True))

    assert exit_code == 0
    assert path.read_bytes() == before


def _normalized(handoff: str) -> str:
    """The handoff with commit SHAs and elapsed times masked."""
    masked = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", handoff)
    return re.sub(r"\d+(\.\d+)?s\b", "<secs>", masked)


# (f)
def test_a_record_write_oserror_warns_once_and_the_cycle_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_repo = _init_repo(tmp_path / "baseline")
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        _finishing_backend(baseline_repo, "sess-1", []),
    )
    baseline_exit, baseline_logs = _run(_config(baseline_repo))

    failing_repo = _init_repo(tmp_path / "failing")
    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        _finishing_backend(failing_repo, "sess-1", []),
    )

    def failing_record(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "record_dispatch", failing_record)
    failing_exit, failing_logs = _run(_config(failing_repo))

    path = dispatch_record_path(failing_repo)
    warnings = [
        text for level, text in failing_logs if level == "WARN" and "dispatch record" in text
    ]
    assert len(warnings) == 1
    assert str(path) in warnings[0]
    assert "disk full" in warnings[0]
    assert failing_exit == baseline_exit == 0
    assert not path.exists()
    # The dispatcher commit and the handoff come out the same as without the
    # failure.
    assert _git(failing_repo, "show", "--name-only", "--format=", "HEAD") == _git(
        baseline_repo, "show", "--name-only", "--format=", "HEAD"
    )
    assert _git(failing_repo, "rev-list", "--count", "HEAD") == _git(
        baseline_repo, "rev-list", "--count", "HEAD"
    )
    failing_handoff = (failing_repo / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8")
    baseline_handoff = (baseline_repo / HANDOFF_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )
    assert _normalized(failing_handoff) == _normalized(baseline_handoff)
    baseline_levels = [level for level, _text in baseline_logs]
    failing_levels = [level for level, _text in failing_logs]
    assert failing_levels.count("WARN") == baseline_levels.count("WARN") + 1


def test_a_non_oserror_from_the_record_write_is_not_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", [])
    )

    def broken_record(*_args: object, **_kwargs: object) -> None:
        raise ValueError("bug")

    monkeypatch.setattr(orchestrator, "record_dispatch", broken_record)

    with pytest.raises(ValueError, match="bug"):
        _run(_config(repo))


# (g)
def test_a_clear_oserror_warns_once_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    calls: list[str] = []
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", calls)
    )

    real_unlink = orchestrator._unlink_carry_over_record

    def failing_clear(repo_root: Path, name: str = "carry-over.json") -> None:
        if name == DISPATCH_RECORD_STATE_PATH.name:
            raise OSError("access denied")
        real_unlink(repo_root, name)

    monkeypatch.setattr(orchestrator, "_unlink_carry_over_record", failing_clear)

    exit_code, logs = _run(_config(repo))

    path = dispatch_record_path(repo)
    warnings = [text for level, text in logs if level == "WARN" and "dispatch record" in text]
    assert len(warnings) == 1
    assert str(path) in warnings[0]
    assert "access denied" in warnings[0]
    assert exit_code == 0
    assert calls == ["backend"]
    assert [entry.session_id for entry in _record(repo)] == ["sess-1"]


def test_a_dry_run_does_not_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    real_unlink = orchestrator._unlink_carry_over_record

    def no_clear(repo_root: Path, name: str = "carry-over.json") -> None:
        if name == DISPATCH_RECORD_STATE_PATH.name:
            pytest.fail("a dry run must not clear the record")
        real_unlink(repo_root, name)

    monkeypatch.setattr(orchestrator, "_unlink_carry_over_record", no_clear)
    monkeypatch.setattr(
        orchestrator,
        "record_dispatch",
        lambda *_a, **_k: pytest.fail("a dry run must not write the record"),
    )

    exit_code, _logs = _run(_config(repo, dry_run=True))

    assert exit_code == 0


# (h)
def test_a_resumed_handback_adds_no_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")

    def interrupted_commit(**_kwargs: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(
            orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", [])
        )
        patch.setattr(orchestrator, "_commit_for_implementer", interrupted_commit)
        with pytest.raises(KeyboardInterrupt):
            _run(_config(repo))
    # The interrupted run's dispatch was recorded before the commit.
    assert [entry.role for entry in _record(repo)] == ["backend"]

    monkeypatch.setattr(
        orchestrator,
        "invoke_backend_role",
        lambda *_a, **_k: pytest.fail("a resumed handback must not dispatch"),
    )
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", lambda: io.StringIO("y\n"))

    exit_code, logs = _run(_config(repo))

    assert exit_code == 0
    assert ("INFO", "Routing source: interrupted_handback_recovery") in logs
    assert _record(repo) == []


def test_a_record_git_does_not_ignore_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo / ".gitignore", "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "stop ignoring logs")
    calls: list[str] = []
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", calls)
    )

    exit_code, logs = _run(_config(repo))

    warnings = [
        text
        for level, text in logs
        if level == "WARN" and "dispatch-record.json" in text
    ]
    assert len(warnings) == 1
    assert "Git does not ignore" in warnings[0]
    assert calls == ["backend"]
    assert exit_code == 0
    assert any(
        level == "AGENT" and text == "Work-claim gate PASSED for backend."
        for level, text in logs
    )
    assert not dispatch_record_path(repo).exists()


# Provider selection
def test_the_manual_frontend_records_no_provider(tmp_path: Path) -> None:
    config = _config(tmp_path, use_manual_frontend=True)
    result = DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.0)

    orchestrator._record_role_dispatch(
        config,
        route="frontend",
        story_id="S1",
        dispatch_result=result,
        log=lambda *_: None,
        state=orchestrator._DispatchRecordState(enabled=True),
    )

    assert _summary(_record(tmp_path)) == [("frontend", "S1", None, None)]


def test_a_configured_frontend_records_its_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = DispatchResult(
        stdout="", stderr="", exit_code=1, elapsed_seconds=0.0, session_id="f-1"
    )

    orchestrator._record_role_dispatch(
        config,
        route="frontend",
        story_id=None,
        dispatch_result=result,
        log=lambda *_: None,
        state=orchestrator._DispatchRecordState(enabled=True),
    )

    assert _summary(_record(tmp_path)) == [
        ("frontend", None, config.agents["frontend"].provider, "f-1")
    ]


def test_a_disabled_record_writes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.0)

    orchestrator._record_role_dispatch(
        config,
        route="backend",
        story_id="S1",
        dispatch_result=result,
        log=lambda *_: None,
        state=orchestrator._DispatchRecordState(enabled=False),
    )

    assert not dispatch_record_path(tmp_path).exists()


# Review finding 2: decide the ignore status before deleting anything.
def test_a_tracked_record_git_does_not_ignore_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo / ".gitignore", "")
    path = dispatch_record_path(repo)
    record_dispatch(
        path, role="planner", story_id="OLD-1", provider="codex", session_id="old"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "track the record")
    before = path.read_bytes()
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", [])
    )

    exit_code, logs = _run(_config(repo))

    assert exit_code == 0
    assert path.read_bytes() == before
    assert _git(repo, "status", "--porcelain", "--", DISPATCH_RECORD_STATE_PATH.as_posix()) == ""
    assert any(
        level == "WARN" and "Git does not ignore" in text for level, text in logs
    )


# Review finding 1: never delete or write through a linked parent directory.
def _link_directory(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def _unlink_directory(link: Path) -> None:
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()


def _link_warnings(logs: list[tuple[str, str]]) -> list[str]:
    return [
        text
        for level, text in logs
        if level == "WARN"
        and text.startswith("The dispatch record path")
        and "symlink or junction" in text
    ]


def test_a_linked_record_directory_at_start_is_neither_cleared_nor_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / DISPATCH_RECORD_STATE_PATH.name
    victim.write_bytes(b"outside the repository\n")
    link = repo / DISPATCH_RECORD_STATE_PATH.parent
    link.parent.mkdir(parents=True)
    _link_directory(link, outside)
    calls: list[str] = []
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", calls)
    )

    try:
        _exit_code, logs = _run(_config(repo))
        assert calls == ["backend"]
        assert victim.read_bytes() == b"outside the repository\n"
        assert len(_link_warnings(logs)) == 1
        # CHAT-34: the in-memory entry does not depend on the record.
        entry = chat_session.latest_role_dispatch("backend")
        assert entry is not None and entry.session_id == "sess-1"
    finally:
        _unlink_directory(link)


def test_a_link_planted_during_a_dispatch_is_not_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / DISPATCH_RECORD_STATE_PATH.name
    victim.write_bytes(b"outside the repository\n")
    link = repo / DISPATCH_RECORD_STATE_PATH.parent
    finish = _finishing_backend(repo, "sess-1", [])

    def planting_backend(path: Path, **kwargs: object) -> DispatchResult:
        result = finish(path, **kwargs)
        if link.exists():
            link.rename(link.parent / "dispatch-moved")
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
        _link_directory(link, outside)
        return result

    monkeypatch.setattr(orchestrator, "invoke_backend_role", planting_backend)

    try:
        _exit_code, logs = _run(_config(repo))
        assert victim.read_bytes() == b"outside the repository\n"
        assert sorted(item.name for item in outside.iterdir()) == [victim.name]
        assert len(_link_warnings(logs)) == 1
    finally:
        _unlink_directory(link)


def test_a_link_found_mid_write_disables_the_record_for_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    result = DispatchResult(
        stdout="", stderr="", exit_code=0, elapsed_seconds=0.0, session_id="s-1"
    )
    state = orchestrator._DispatchRecordState(enabled=True)
    attempts: list[str] = []

    def linked_writer(_root: Path, _payload: object, name: str, **_kwargs: object) -> None:
        attempts.append(name)
        raise orchestrator._CarryOverLinkError(0, "link", name)

    monkeypatch.setattr(orchestrator, "_write_carry_over_payload", linked_writer)
    logs: list[tuple[str, str]] = []
    for _ in range(2):
        orchestrator._record_role_dispatch(
            config,
            route="backend",
            story_id="S1",
            dispatch_result=result,
            log=lambda level, text: logs.append((level, text)),
            state=state,
        )

    assert attempts == [DISPATCH_RECORD_STATE_PATH.name]
    assert state.enabled is False
    assert len(_link_warnings(logs)) == 1
    assert not dispatch_record_path(tmp_path).exists()


def test_the_repository_copy_is_never_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    config = _config(repo)
    order: list[str] = []
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-b", order)
    )

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
        order.append(subagent_name)
        # A role rewrites the record between dispatches; the next write
        # starts from what the dispatcher saved, not from this file.
        record_dispatch(
            dispatch_record_path(repo),
            role="planted",
            story_id=None,
            provider=None,
            session_id="planted",
        )
        config.handoff_full_path.write_text(_audit_fail(), encoding="utf-8")
        return SubagentResult(
            stdout="", stderr="", exit_code=0, elapsed_seconds=0.01, session_id="sess-a"
        )

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)

    _run(config, max_cycles=2)

    assert order == ["backend", "auditor"]
    assert [entry.role for entry in _record(repo)] == ["backend", "auditor"]


# --- CHAT-34: returned dispatches are kept in memory for @role chat -----------


def _latest(role: str) -> tuple[Any, ...] | None:
    entry = chat_session.latest_role_dispatch(role)
    if entry is None:
        return None
    return (entry.role, entry.story_id, entry.provider, entry.session_id)


def test_a_returned_dispatch_is_kept_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", [])
    )
    config = _config(repo)

    _run(config)

    assert _latest("backend") == (
        "backend",
        STORY_ID,
        config.agents["backend"].provider,
        "sess-1",
    )


def test_a_nonzero_exit_is_kept_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")

    def failing_backend(_path: Path, **_kwargs: object) -> DispatchResult:
        return DispatchResult(
            stdout="",
            stderr="boom",
            exit_code=1,
            elapsed_seconds=0.01,
            session_id="sess-fail",
        )

    monkeypatch.setattr(orchestrator, "invoke_backend_role", failing_backend)
    config = _config(repo)

    _run(config)

    assert _latest("backend") == (
        "backend",
        STORY_ID,
        config.agents["backend"].provider,
        "sess-fail",
    )


def test_two_cycles_keep_each_role_latest_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    config = _config(repo)
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-b", [])
    )

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
        config.handoff_full_path.write_text(_audit_fail(), encoding="utf-8")
        return SubagentResult(
            stdout="", stderr="", exit_code=0, elapsed_seconds=0.01, session_id="sess-a"
        )

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)

    _run(config, max_cycles=2)

    assert _latest("backend") == (
        "backend",
        STORY_ID,
        config.agents["backend"].provider,
        "sess-b",
    )
    assert _latest("auditor") == (
        "auditor",
        STORY_ID,
        config.agents["auditor"].provider,
        "sess-a",
    )


@pytest.mark.parametrize("missing", [None, ""])
def test_a_later_returned_dispatch_without_a_session_hides_the_older_one(
    tmp_path: Path, missing: str | None
) -> None:
    config = _config(tmp_path)
    for session_id in ("sess-1", missing):
        orchestrator._remember_role_session(
            config,
            route="backend",
            story_id="S1",
            dispatch_result=DispatchResult(
                stdout="",
                stderr="",
                exit_code=0,
                elapsed_seconds=0.0,
                session_id=session_id,
            ),
        )

    assert _latest("backend") == (
        "backend",
        "S1",
        config.agents["backend"].provider,
        None,
    )
    assert main_module._role_session_lookup(config)("backend") is None


def test_a_dispatch_that_raises_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")

    def broken_backend(_path: Path, **_kwargs: object) -> DispatchResult:
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", broken_backend)

    with pytest.raises(RuntimeError, match="adapter bug"):
        _run(_config(repo))

    assert _latest("backend") is None


def test_a_takeover_with_no_result_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")

    class _Stop(Exception):
        pass

    def stopped_route(*_args: object, **_kwargs: object) -> object:
        raise ChatTakeover()

    def stop_loop(*_args: object, **_kwargs: object) -> object:
        raise _Stop()

    monkeypatch.setattr(orchestrator, "_dispatch_route", stopped_route)
    monkeypatch.setattr(orchestrator, "_follow_chat_takeover", stop_loop)

    with pytest.raises(_Stop):
        _run(_config(repo))

    assert _latest("backend") is None


def test_a_disabled_record_still_keeps_the_session_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    # Git no longer ignores the record, so the run disables it.
    _write(repo / ".gitignore", "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "stop ignoring logs")
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", [])
    )

    _run(_config(repo))

    assert not dispatch_record_path(repo).exists()
    assert _latest("backend") is not None
    assert _latest("backend")[3] == "sess-1"  # type: ignore[index]


def test_a_failed_record_write_still_keeps_the_session_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-1", [])
    )

    def failing_record(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "record_dispatch", failing_record)

    _run(_config(repo))

    assert not dispatch_record_path(repo).exists()
    assert _latest("backend") is not None
    assert _latest("backend")[3] == "sess-1"  # type: ignore[index]


def test_a_forged_repository_record_does_not_change_the_resumed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    config = _config(repo)
    monkeypatch.setattr(
        orchestrator, "invoke_backend_role", _finishing_backend(repo, "sess-b", [])
    )

    def forging_support(subagent_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
        # A role overwrites the repository record with a forged backend entry.
        path = dispatch_record_path(repo)
        path.unlink(missing_ok=True)
        record_dispatch(
            path,
            role="backend",
            story_id="FORGED-1",
            provider="codex",
            session_id="forged",
        )
        config.handoff_full_path.write_text(_audit_fail(), encoding="utf-8")
        return SubagentResult(
            stdout="", stderr="", exit_code=0, elapsed_seconds=0.01, session_id="sess-a"
        )

    monkeypatch.setattr(orchestrator, "invoke_support_role", forging_support)
    _run(config, max_cycles=2)
    # Forge the record again after the run, before the chat reads it.
    path = dispatch_record_path(repo)
    path.unlink(missing_ok=True)
    record_dispatch(
        path, role="backend", story_id="FORGED-2", provider="codex", session_id="forged"
    )

    lookup = main_module._role_session_lookup(config)
    session = lookup("backend")

    assert session is not None
    assert session.session_id == "sess-b"
    assert session.story_id == STORY_ID
    assert session.provider == config.agents["backend"].provider
    assert session.agent_config == config.agents["backend"]


def test_the_manual_frontend_keeps_no_session(tmp_path: Path) -> None:
    config = _config(tmp_path, use_manual_frontend=True)
    chat_session.record_role_dispatch(
        "frontend", story_id="S0", provider="claude", session_id="older"
    )
    result = DispatchResult(
        stdout="", stderr="", exit_code=0, elapsed_seconds=0.0, session_id="manual"
    )

    orchestrator._remember_role_session(
        config, route="frontend", story_id="S1", dispatch_result=result
    )

    assert _latest("frontend") == ("frontend", "S1", None, None)
    assert main_module._role_session_lookup(config)("frontend") is None


def _finalizer_route() -> str:
    return f"""---
next_agent: finalizer
reason: "Close the epic."
epic_id: REC
story_id: {STORY_ID}
story_title: Change the value
producer: auditor
close_type: epic
scope_sha: abc1234
---

# Handoff
"""


_FINALIZER_SUCCESS_OUTPUT = """PROJECT STATE UPDATED: YES
HANDOFF.MD REWRITTEN: YES
SCOPE CLOSED: REC
NEXT ROUTE: planner
AUDIT SHA: abc1234
COMMIT SHA: abc1234
PUSH RESULT: SKIPPED
CHANGES MADE:
  - PROJECT_STATE.md: closed REC
"""


@pytest.mark.parametrize(
    ("stdout", "exit_code"),
    [
        (_FINALIZER_SUCCESS_OUTPUT, 0),
        ("prose instead of structured fields", 0),
        ("", 3),
    ],
    ids=["success", "parse-error", "nonzero-exit"],
)
def test_a_returned_finalizer_dispatch_is_kept_in_memory_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, exit_code: int
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo / HANDOFF_RELATIVE_PATH, _finalizer_route())
    _git(repo, "commit", "-q", "-am", "route to finalizer")
    config = _config(repo)
    calls: list[str] = []

    def fake_finalizer(subagent_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
        calls.append(subagent_name)
        return SubagentResult(
            stdout=stdout,
            stderr="",
            exit_code=exit_code,
            elapsed_seconds=0.01,
            session_id=f"sess-fin-{calls.count('ledger-updater')}",
        )

    def fake_validator(subagent_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
        # A failed close dispatches the validator; it changes nothing here.
        calls.append(subagent_name)
        return SubagentResult(
            stdout="", stderr="", exit_code=0, elapsed_seconds=0.01, session_id="val"
        )

    monkeypatch.setattr(ledger, "invoke_support_role", fake_finalizer)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_validator)

    _run(config)

    assert calls[0] == "ledger-updater"
    # The latest returned finalizer session is the one kept.
    assert _latest("finalizer") == (
        "finalizer",
        STORY_ID,
        config.agents["finalizer"].provider,
        f"sess-fin-{calls.count('ledger-updater')}",
    )
    session = main_module._role_session_lookup(config)("finalizer")
    assert session is not None
    assert session.session_id == _latest("finalizer")[3]  # type: ignore[index]
    # CHAT-15: the epic-close ledger stays out of the persisted record.
    assert "finalizer" not in [entry.role for entry in _record(repo)]


def test_a_finalizer_result_without_a_session_hides_the_older_one(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    chat_session.record_role_dispatch(
        "finalizer", story_id="S0", provider="claude", session_id="older"
    )

    class _Result:
        parse_error = None
        subagent_exit_code = 0

    orchestrator._remember_finalizer_session(
        config, story_id="S1", ledger_result=_Result()
    )

    assert _latest("finalizer") == (
        "finalizer",
        "S1",
        config.agents["finalizer"].provider,
        None,
    )
    assert main_module._role_session_lookup(config)("finalizer") is None


def test_the_in_memory_entry_ignores_the_record_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = DispatchResult(
        stdout="", stderr="", exit_code=2, elapsed_seconds=0.0, session_id="f-1"
    )

    orchestrator._remember_role_session(
        config, route="frontend", story_id=None, dispatch_result=result
    )

    assert _latest("frontend") == (
        "frontend",
        None,
        config.agents["frontend"].provider,
        "f-1",
    )
    assert not dispatch_record_path(tmp_path).exists()


# Failover: chat still reaches the fallback agent's session.
_CLAUDE_FALLBACK = AgentConfig(
    provider="claude", binary="claude", model="claude-opus-5-5"
)


def _config_with_fallback(tmp_path: Path, role: str) -> DispatchConfig:
    agents = dict(_config(tmp_path).agents)
    agents[role] = AgentConfig(
        provider="antigravity", binary="agy", fallback=_CLAUDE_FALLBACK
    )
    return _config(tmp_path, agents=agents)


def _failover_result(session_id: str | None = "claude-sess") -> DispatchResult:
    return DispatchResult(
        stdout="",
        stderr="",
        exit_code=0,
        elapsed_seconds=0.0,
        session_id=None,
        session_invalidated=True,
        fallback_ran=True,
        fallback_session_id=session_id,
    )


@pytest.mark.parametrize("role", ["planner", "backend", "tpm"])
def test_a_failover_dispatch_keeps_the_fallback_session_for_chat(
    tmp_path: Path, role: str
) -> None:
    config = _config_with_fallback(tmp_path, role)

    orchestrator._remember_role_session(
        config, route=role, story_id="S1", dispatch_result=_failover_result()
    )

    assert _latest(role) == (role, "S1", "claude", "claude-sess")
    entry = chat_session.latest_role_dispatch(role)
    assert entry is not None and entry.fallback is True
    session = main_module._role_session_lookup(config)(role)
    assert session is not None
    assert session.provider == "claude"
    assert session.session_id == "claude-sess"
    # Chat resumes with the fallback's CLI, not the primary agy config.
    assert session.agent_config == _CLAUDE_FALLBACK


def test_a_failover_dispatch_records_the_fallback_provider_and_session(
    tmp_path: Path,
) -> None:
    config = _config_with_fallback(tmp_path, "planner")

    orchestrator._record_role_dispatch(
        config,
        route="planner",
        story_id="S1",
        dispatch_result=_failover_result(),
        log=lambda *_: None,
        state=orchestrator._DispatchRecordState(enabled=True),
    )

    assert _summary(_record(tmp_path)) == [("planner", "S1", "claude", "claude-sess")]


def test_a_primary_dispatch_still_records_the_primary(tmp_path: Path) -> None:
    config = _config_with_fallback(tmp_path, "planner")
    result = DispatchResult(
        stdout="", stderr="", exit_code=0, elapsed_seconds=0.0, session_id="agy-conv"
    )

    orchestrator._remember_role_session(
        config, route="planner", story_id="S1", dispatch_result=result
    )

    assert _latest("planner") == ("planner", "S1", "antigravity", "agy-conv")
    session = main_module._role_session_lookup(config)("planner")
    assert session is not None
    assert session.agent_config == config.agents["planner"]


def test_a_failover_finalizer_keeps_the_fallback_session_for_chat(
    tmp_path: Path,
) -> None:
    agents = dict(_config(tmp_path).agents)
    agents["finalizer"] = AgentConfig(
        provider="codex", binary="codex", fallback=_CLAUDE_FALLBACK
    )
    config = _config(tmp_path, agents=agents)

    class _Result:
        parse_error = None
        subagent_exit_code = 0
        session_id = None
        fallback_ran = True
        fallback_session_id = "claude-fin"

    orchestrator._remember_finalizer_session(
        config, story_id="S1", ledger_result=_Result()
    )

    assert _latest("finalizer") == ("finalizer", "S1", "claude", "claude-fin")
    session = main_module._role_session_lookup(config)("finalizer")
    assert session is not None
    assert session.agent_config == _CLAUDE_FALLBACK
