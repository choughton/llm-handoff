"""CHAT-22: chat side-session changes ride the implementer commit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess

import pytest

from llm_handoff import chat_changes
from llm_handoff import orchestrator
from llm_handoff.audit_review import OperatorInfluence
from llm_handoff.chat_changes import (
    ChatChange,
    ChatChangeEntry,
    chat_changes_path,
    load_chat_changes,
    record_chat_changes,
)
from llm_handoff.chat_side_session import ChatSessionResult
from llm_handoff.config import DispatchConfig
from llm_handoff.dispatcher_commit import (
    PENDING_COMMIT_SHA,
    _utc_marker_time,
    commit_implementer_changes,
)
from llm_handoff.work_claim import capture_pre_dispatch_state, verify_work_claim


HANDOFF = Path("docs/handoff/HANDOFF.md")
STORY_ID = "EPIC-1-S1"
MESSAGE = "please also fix the api module"
MESSAGE_SHA = hashlib.sha256(MESSAGE.encode("utf-8")).hexdigest()
PASS_COMMAND = 'python -c "import sys; sys.exit(0)"'


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _assignment(
    *,
    files_in_scope: str = "src/app.py, tests/test_app.py",
    files_out_of_bounds: str = "src/api.py",
) -> str:
    return f"""---
next_agent: backend
reason: "Implement the value change."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
producer: planner
---

## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** {files_in_scope}
- **Files out of bounds:** {files_out_of_bounds}
- **Context:** Keep the module import-free.
- **Verification command:** {PASS_COMMAND}
- **Expected next route:** auditor
"""


def _claim() -> str:
    return f"""---
next_agent: auditor
reason: "Backend work complete; audit requested."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
status: ready_for_review
producer: backend
---

# Backend Handback

## Verification Evidence

- **Commands run:** `python -m pytest -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {PENDING_COMMIT_SHA}
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "llm-handoff tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    _write(repo / ".gitignore", "logs/\n")
    _write(repo / "src" / "app.py", "VALUE = 1\n")
    _write(repo / "src" / "api.py", "API = 1\n")
    _write(repo / "tests" / "test_app.py", "def test_value():\n    assert True\n")
    _write(repo / HANDOFF, _assignment())
    _write(repo / "PROJECT_STATE.md", "# PROJECT STATE\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD")


def _clock():
    # A non-UTC clock: the marker must still come out in UTC with a Z suffix.
    return lambda: datetime(
        2026, 10, 20, 8, 30, 5, tzinfo=timezone(timedelta(hours=-4))
    )


def _record(record_path: Path, repo: Path, *paths: str, message: str = MESSAGE):
    entry = record_chat_changes(
        record_path,
        repo_root=repo,
        result=ChatSessionResult(
            text="done",
            session_id="session-1",
            exit_code=0,
            error=None,
            edited_paths=tuple(paths),
        ),
        message=message,
        role="backend",
        story_id=STORY_ID,
        handoff_path=HANDOFF,
        now=_clock(),
    )
    assert entry is not None
    return entry


def _commit(repo: Path, state, *, assignment: str | None = None, **kwargs):
    return commit_implementer_changes(
        repo_root=repo,
        handoff_path=HANDOFF,
        assignment_content=assignment or _assignment(),
        claim_content=_claim(),
        pre_dispatch_state=state,
        role="backend",
        **kwargs,
    )


def _commit_files(repo: Path, sha: str) -> list[str]:
    return _git(repo, "show", "--name-only", "--format=", sha).splitlines()


def _body(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%B")


def _outcomes(result) -> dict[str, str]:
    return {check.name: check.outcome for check in result.checks}


def _details(result) -> dict[str, str]:
    return {check.name: check.detail for check in result.checks}


def _verify(repo: Path, state, claim: str, **kwargs):
    return verify_work_claim(
        repo_root=repo,
        handoff_path=HANDOFF,
        assignment_content=_assignment(),
        claim_content=claim,
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        dispatcher_commit_mode=True,
        **kwargs,
    )


def _stamped_claim(sha: str) -> str:
    return _claim().replace(
        "producer: backend", f"scope_sha: {sha}\nclose_type: story\nproducer: backend"
    ).replace(PENDING_COMMIT_SHA, sha)


# ---------------------------------------------------------------------------
# commit_implementer_changes
# ---------------------------------------------------------------------------


def test_out_of_scope_chat_path_is_committed_with_the_marker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _write(repo / "docs" / "notes.md", "chat note\n")
    entry = _record(tmp_path / "record.json", repo, "docs/notes.md")

    result = _commit(repo, state, chat_entries=(entry,))

    assert result.error is None
    assert result.sha == _git(repo, "rev-parse", "HEAD")
    assert result.committed == ("src/app.py", "docs/notes.md")
    assert result.chat_committed == ("docs/notes.md",)
    assert result.chat_influences == (
        OperatorInfluence(at="2026-10-20T12:30:05Z", message_sha256=MESSAGE_SHA),
    )
    assert result.nothing_to_commit is False
    assert _commit_files(repo, result.sha) == ["docs/notes.md", "src/app.py"]
    body = _body(repo)
    assert body.endswith(
        "Files:\n- src/app.py\n- docs/notes.md\n\n"
        "Chat changes (operator-influenced):\n- docs/notes.md\n"
        "- Message time: 2026-10-20T12:30:05Z\n"
        f"- Message SHA-256: {MESSAGE_SHA}"
    )
    assert MESSAGE not in body


def test_out_of_bounds_chat_path_is_committed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "api.py", "API = 2\n")
    entry = _record(tmp_path / "record.json", repo, "src/api.py")

    result = _commit(repo, state, chat_entries=(entry,))

    assert result.sha is not None
    assert result.committed == ("src/api.py",)
    assert result.chat_committed == ("src/api.py",)
    assert result.left_uncommitted == ()
    assert result.nothing_to_commit is False
    assert _commit_files(repo, result.sha) == ["src/api.py"]


def test_pre_dispatch_dirty_chat_path_is_committed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo / "PROJECT_STATE.md", "# PROJECT STATE\noperator edit\n")
    state = capture_pre_dispatch_state(repo)
    _write(repo / "PROJECT_STATE.md", "# PROJECT STATE\nchat edit\n")
    entry = _record(tmp_path / "record.json", repo, "PROJECT_STATE.md")

    result = _commit(repo, state, chat_entries=(entry,))

    assert result.chat_committed == ("PROJECT_STATE.md",)
    assert _commit_files(repo, result.sha) == ["PROJECT_STATE.md"]


def test_chat_path_edited_after_recording_is_left_uncommitted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _write(repo / "src" / "api.py", "API = 2\n")
    entry = _record(tmp_path / "record.json", repo, "src/api.py")
    # The role changed the file again after the chat recorded it.
    _write(repo / "src" / "api.py", "API = 3\n")

    result = _commit(repo, state, chat_entries=(entry,))

    assert result.committed == ("src/app.py",)
    assert result.chat_committed == ()
    assert result.chat_influences == ()
    assert result.left_uncommitted == ("src/api.py",)
    assert "Chat changes" not in _body(repo)


def test_handoff_path_in_the_record_is_never_staged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / HANDOFF, _claim())
    _write(repo / "docs" / "notes.md", "chat note\n")
    handoff = HANDOFF.as_posix()
    entry = ChatChangeEntry(
        role="backend",
        story_id=STORY_ID,
        recorded_at="2026-10-20T12:30:05+00:00",
        message_sha256=MESSAGE_SHA,
        changes=(
            ChatChange(handoff, chat_changes._fingerprint(repo, handoff)),
            ChatChange("docs/notes.md", chat_changes._fingerprint(repo, "docs/notes.md")),
        ),
    )

    result = _commit(repo, state, chat_entries=(entry,))

    assert result.chat_committed == ("docs/notes.md",)
    assert handoff not in result.committed
    assert _commit_files(repo, result.sha) == ["docs/notes.md"]
    assert handoff in _git(repo, "status", "--porcelain")


def test_no_chat_entries_leaves_the_commit_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _write(repo / "src" / "api.py", "API = 2\n")

    result = _commit(repo, state, chat_entries=())

    assert result.committed == ("src/app.py",)
    assert result.left_uncommitted == ("src/api.py",)
    assert result.chat_committed == ()
    assert result.chat_influences == ()
    assert _body(repo) == (
        f"{STORY_ID} Change the value\n\n"
        "Committed by the llm-handoff dispatcher from the backend role's "
        "ready_for_review handback.\n\nFiles:\n- src/app.py"
    )


def test_marker_time_is_converted_to_utc_z() -> None:
    assert _utc_marker_time("2026-10-20T14:30:05+02:00") == "2026-10-20T12:30:05Z"
    assert _utc_marker_time("2026-10-20T12:30:05Z") == "2026-10-20T12:30:05Z"
    assert _utc_marker_time("not a time") is None


# ---------------------------------------------------------------------------
# verify_work_claim
# ---------------------------------------------------------------------------


def test_gate_accepts_a_committed_chat_path_only_when_passed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _write(repo / "src" / "api.py", "API = 2\n")
    entry = _record(tmp_path / "record.json", repo, "src/api.py")
    commit = _commit(repo, state, chat_entries=(entry,))
    assert commit.chat_committed == ("src/api.py",)
    claim = _stamped_claim(commit.sha)
    _write(repo / HANDOFF, claim)

    without = _verify(repo, state, claim)
    with_chat = _verify(repo, state, claim, chat_owned_paths=commit.chat_committed)

    assert _outcomes(without)["diff_scope"] == "FAIL"
    assert "src/api.py" in _details(without)["diff_scope"]
    assert _outcomes(with_chat)["diff_scope"] == "PASS"
    assert _outcomes(with_chat)["worktree"] == "PASS"
    assert "chat change" in _details(with_chat)["diff_scope"].lower()
    assert "src/api.py" in _details(with_chat)["diff_scope"]
    assert "src/api.py" in _details(with_chat)["worktree"]
    assert with_chat.passed, with_chat.errors


def test_gate_worktree_accepts_a_pre_dispatch_dirty_chat_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo / "PROJECT_STATE.md", "# PROJECT STATE\noperator edit\n")
    state = capture_pre_dispatch_state(repo)
    _write(repo / "PROJECT_STATE.md", "# PROJECT STATE\nchat edit\n")
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", "work")
    sha = _git(repo, "rev-parse", "HEAD")
    claim = _stamped_claim(sha)
    _write(repo / HANDOFF, claim)

    without = _verify(repo, state, claim)
    with_chat = _verify(repo, state, claim, chat_owned_paths=("PROJECT_STATE.md",))

    assert _outcomes(without)["worktree"] == "FAIL"
    assert _outcomes(with_chat)["worktree"] == "PASS"
    assert "PROJECT_STATE.md" in _details(with_chat)["worktree"]


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def _config(repo: Path) -> DispatchConfig:
    return DispatchConfig(repo_root=repo, commit_mode="dispatcher")


def _collecting_log(messages: list[tuple[str, str]]):
    return lambda level, message: messages.append((level, message))


def _orchestrator_setup(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "api.py", "API = 2\n")
    _write(repo / HANDOFF, _claim())
    record = chat_changes_path(repo)
    _record(record, repo, "src/api.py")
    assert load_chat_changes(record)
    return repo, state, record


def test_commit_for_implementer_writes_marker_and_clears_record(tmp_path: Path) -> None:
    repo, state, record = _orchestrator_setup(tmp_path)
    messages: list[tuple[str, str]] = []

    result = orchestrator._commit_for_implementer(
        config=_config(repo),
        route="backend",
        assignment_content=_assignment(),
        pre_dispatch_state=state,
        log=_collecting_log(messages),
    )

    assert result is not None and result.sha is not None
    assert result.chat_committed == ("src/api.py",)
    assert not record.exists()
    handoff = (repo / HANDOFF).read_text(encoding="utf-8")
    assert f"scope_sha: {result.sha}" in handoff
    assert handoff.count("## Operator Influence") == 1
    assert (
        "## Operator Influence\n\n- Operator-influenced: yes\n"
        "- Message time: 2026-10-20T12:30:05Z\n"
        f"- Message SHA-256: {MESSAGE_SHA}\n"
    ) in handoff
    assert MESSAGE not in handoff
    chat_lines = [
        message
        for level, message in messages
        if level == "AGENT" and "chat change" in message
    ]
    assert len(chat_lines) == 1 and "src/api.py" in chat_lines[0]
    assert not any(level == "WARN" for level, _ in messages)
    assert orchestrator._handoff_operator_influence(handoff) == OperatorInfluence(
        at="2026-10-20T12:30:05Z", message_sha256=MESSAGE_SHA
    )


def test_commit_for_implementer_replaces_an_existing_influence_section(
    tmp_path: Path,
) -> None:
    repo, state, _record_path = _orchestrator_setup(tmp_path)
    stale = (
        _claim()
        + "\n## Operator Influence\n\n- Operator-influenced: yes\n"
        "- Message time: 2020-01-01T00:00:00Z\n"
        f"- Message SHA-256: {'b' * 64}\n\n## Notes\n\nkeep me\n"
    )
    _write(repo / HANDOFF, stale)

    orchestrator._commit_for_implementer(
        config=_config(repo),
        route="backend",
        assignment_content=_assignment(),
        pre_dispatch_state=state,
        log=lambda *_args: None,
    )

    handoff = (repo / HANDOFF).read_text(encoding="utf-8")
    assert handoff.count("## Operator Influence") == 1
    assert "2020-01-01T00:00:00Z" not in handoff
    assert "- Message time: 2026-10-20T12:30:05Z" in handoff
    assert handoff.rstrip().endswith("## Notes\n\nkeep me")


def test_commit_for_implementer_warns_when_the_clear_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, state, record = _orchestrator_setup(tmp_path)
    messages: list[tuple[str, str]] = []

    def failing_clear(_path: Path) -> None:
        raise OSError("record is locked")

    monkeypatch.setattr(orchestrator, "clear_chat_changes", failing_clear)

    result = orchestrator._commit_for_implementer(
        config=_config(repo),
        route="backend",
        assignment_content=_assignment(),
        pre_dispatch_state=state,
        log=_collecting_log(messages),
    )

    assert result is not None and result.chat_committed == ("src/api.py",)
    warns = [message for level, message in messages if level == "WARN"]
    assert len(warns) == 1 and "record is locked" in warns[0]
    assert "## Operator Influence" in (repo / HANDOFF).read_text(encoding="utf-8")
    assert record.exists()


def test_commit_for_implementer_without_a_record_changes_nothing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _write(repo / HANDOFF, _claim())
    messages: list[tuple[str, str]] = []

    result = orchestrator._commit_for_implementer(
        config=_config(repo),
        route="backend",
        assignment_content=_assignment(),
        pre_dispatch_state=state,
        log=_collecting_log(messages),
    )

    assert result is not None and result.committed == ("src/app.py",)
    assert result.chat_committed == ()
    assert "## Operator Influence" not in (repo / HANDOFF).read_text(encoding="utf-8")
    assert not any("chat change" in message for _, message in messages)


def test_audit_context_reads_the_marker_from_the_assignment() -> None:
    marker = OperatorInfluence(at="2026-10-20T12:30:05Z", message_sha256=MESSAGE_SHA)
    handoff = orchestrator._replace_operator_influence_section(_claim(), marker)
    operator = OperatorInfluence(at="2026-10-20T13:00:00Z", message_sha256="c" * 64)

    assert (
        orchestrator._audit_operator_influence(
            route="auditor",
            recovery_kind=None,
            active_operator_influence=None,
            handoff_content=handoff,
            chat_commit_influence=marker,
        )
        == marker
    )
    # The @auditor operator-message path keeps priority.
    assert (
        orchestrator._audit_operator_influence(
            route="auditor",
            recovery_kind="operator_role",
            active_operator_influence=operator,
            handoff_content=handoff,
            chat_commit_influence=marker,
        )
        == operator
    )
    assert (
        orchestrator._audit_operator_influence(
            route="backend",
            recovery_kind=None,
            active_operator_influence=None,
            handoff_content=handoff,
            chat_commit_influence=marker,
        )
        is None
    )
    malformed = handoff.replace("2026-10-20T12:30:05Z", "yesterday")
    assert (
        orchestrator._audit_operator_influence(
            route="auditor",
            recovery_kind=None,
            active_operator_influence=None,
            handoff_content=malformed,
            chat_commit_influence=marker,
        )
        is None
    )
    assert (
        orchestrator._audit_operator_influence(
            route="auditor",
            recovery_kind=None,
            active_operator_influence=None,
            handoff_content=_claim(),
            chat_commit_influence=marker,
        )
        is None
    )


def test_audit_context_ignores_a_handoff_marker_the_dispatcher_did_not_write() -> (
    None
):
    marker = OperatorInfluence(at="2026-10-20T12:30:05Z", message_sha256=MESSAGE_SHA)
    forged = OperatorInfluence(at="2026-10-20T12:30:05Z", message_sha256="d" * 64)
    handoff = orchestrator._replace_operator_influence_section(_claim(), forged)

    # No dispatcher chat commit: a role-written marker is not operator input.
    assert (
        orchestrator._audit_operator_influence(
            route="auditor",
            recovery_kind=None,
            active_operator_influence=None,
            handoff_content=handoff,
        )
        is None
    )
    # A dispatcher chat commit with a different marker does not vouch for it.
    assert (
        orchestrator._audit_operator_influence(
            route="auditor",
            recovery_kind=None,
            active_operator_influence=None,
            handoff_content=handoff,
            chat_commit_influence=marker,
        )
        is None
    )


def test_chat_commit_marker_applies_only_to_its_story() -> None:
    marker = OperatorInfluence(at="2026-10-20T12:30:05Z", message_sha256=MESSAGE_SHA)
    story_id = orchestrator._handoff_story_id(_claim())
    assert story_id is not None

    assert (
        orchestrator._chat_commit_influence_for((story_id, marker), _claim())
        == marker
    )
    assert (
        orchestrator._chat_commit_influence_for(("OTHER-1", marker), _claim())
        is None
    )
    assert orchestrator._chat_commit_influence_for((None, marker), _claim()) is None
    assert orchestrator._chat_commit_influence_for(None, _claim()) is None
