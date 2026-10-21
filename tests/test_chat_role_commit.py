"""CHAT-23: the dispatcher commits chat changes after a non-implementer role."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess

import pytest

from llm_handoff import chat_changes
from llm_handoff import orchestrator
from llm_handoff import publish
import llm_handoff.ledger as ledger
from llm_handoff.agents import DispatchResult, SubagentResult
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
from llm_handoff.dispatcher_commit import DispatcherCommitResult, commit_chat_changes
from llm_handoff.validator import ValidationResult
from llm_handoff.work_claim import capture_pre_dispatch_state


HANDOFF = Path("docs/handoff/HANDOFF.md")
STORY_ID = "EPIC-1-S1"
BRANCH = "llm-handoff/epic-1-s1"
MESSAGE = "please also add a note"
MESSAGE_SHA = hashlib.sha256(MESSAGE.encode("utf-8")).hexdigest()
MARKER = OperatorInfluence(at="2026-10-20T12:30:05Z", message_sha256=MESSAGE_SHA)


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


def _blocked_handoff() -> str:
    return f"""---
next_agent: planner
reason: "The packet needs a re-scope."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
status: blocked_implementation_failure
producer: backend
---

# Backend Handback

The Work Packet cannot be completed as written.
"""


def _planner_assignment() -> str:
    return f"""---
next_agent: backend
reason: "Re-scoped the value change."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
producer: planner
---

## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** src/app.py
- **Files out of bounds:** src/api.py
- **Context:** Keep the module import-free.
- **Verification command:** python -c "import sys; sys.exit(0)"
- **Expected next route:** auditor
"""


def _approved_handoff() -> str:
    return f"""---
next_agent: finalizer
reason: "Story approved."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
status: verified_pass
producer: auditor
---

# Audit Report

Approved.
"""


def _backend_claim() -> str:
    return f"""---
next_agent: auditor
reason: "Backend work complete; audit requested."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
status: ready_for_review
scope_sha:
producer: backend
---

# Backend Handback

## Verification Evidence

- **Commands run:** python -c "import sys; sys.exit(0)"
- **Output summary:** exit 0
- **Commit SHA verified:** pending-dispatcher-commit
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
    _write(repo / HANDOFF, _blocked_handoff())
    _write(repo / "PROJECT_STATE.md", "# PROJECT STATE\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD")


def _check_out_saved_story(repo: Path, base: str) -> None:
    _git(repo, "checkout", "-q", "-b", BRANCH)
    publish._save_state(
        repo,
        publish.StoryState(
            branch=BRANCH,
            base_branch="main",
            base_sha=base,
            story_id=STORY_ID,
            story_title="Change the value",
            epic_id="EPIC-1",
            work_packet="## Work Packet\n\n- **Objective:** Set VALUE to 2.\n",
        ),
    )
    assert publish.saved_story_assignment(repo) is not None


def _clock():
    return lambda: datetime(
        2026, 10, 20, 8, 30, 5, tzinfo=timezone(timedelta(hours=-4))
    )


def _record(record_path: Path, repo: Path, *paths: str, role: str = "planner"):
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
        message=MESSAGE,
        role=role,
        story_id=STORY_ID,
        handoff_path=HANDOFF,
        now=_clock(),
    )
    assert entry is not None
    return entry


def _commit_files(repo: Path, sha: str) -> list[str]:
    return _git(repo, "show", "--name-only", "--format=", sha).splitlines()


def _body(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%B")


def _collecting_log(messages: list[tuple[str, str]]):
    return lambda level, message: messages.append((level, message))


def _config(repo: Path, **overrides) -> DispatchConfig:
    kwargs = {"repo_root": repo, "commit_mode": "dispatcher"}
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


# ---------------------------------------------------------------------------
# commit_chat_changes
# ---------------------------------------------------------------------------


def test_commit_chat_changes_commits_owned_paths_with_the_marker(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _write(repo / "docs" / "notes.md", "chat note\n")
    _write(repo / "src" / "api.py", "API = 2\n")
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    # src/api.py is recorded, then changed again by the role; src/app.py is
    # dirty but was never recorded.
    entry = _record(tmp_path / "record.json", repo, "docs/notes.md", "src/api.py")
    _write(repo / "src" / "api.py", "API = 3\n")
    _write(repo / HANDOFF, _planner_assignment())
    handoff = HANDOFF.as_posix()
    handoff_entry = ChatChangeEntry(
        role="planner",
        story_id=STORY_ID,
        recorded_at="2026-10-20T12:30:05+00:00",
        message_sha256=MESSAGE_SHA,
        changes=(ChatChange(handoff, chat_changes._fingerprint(repo, handoff)),),
    )

    result = commit_chat_changes(
        repo_root=repo,
        handoff_path=HANDOFF,
        role="planner",
        story_id=STORY_ID,
        chat_entries=(entry, handoff_entry),
    )

    assert result.error is None
    head = _git(repo, "rev-parse", "HEAD")
    assert result.sha == head
    assert _git(repo, "rev-parse", "HEAD~1") == base
    assert result.committed == ("docs/notes.md",)
    assert result.chat_committed == ("docs/notes.md",)
    assert result.chat_influences == (MARKER,)
    assert result.nothing_to_commit is False
    assert _commit_files(repo, head) == ["docs/notes.md"]
    assert _body(repo) == (
        f"{STORY_ID} Chat changes after the planner role\n\n"
        "Committed by the llm-handoff dispatcher after the planner role's "
        "cycle.\n\nFiles:\n- docs/notes.md\n\n"
        "Chat changes (operator-influenced):\n- docs/notes.md\n"
        "- Message time: 2026-10-20T12:30:05Z\n"
        f"- Message SHA-256: {MESSAGE_SHA}"
    )
    status = _git(repo, "status", "--porcelain")
    assert "src/api.py" in status
    assert "src/app.py" in status
    assert handoff in status


def test_commit_chat_changes_subject_without_a_story(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo / "docs" / "notes.md", "chat note\n")
    entry = _record(tmp_path / "record.json", repo, "docs/notes.md")

    result = commit_chat_changes(
        repo_root=repo,
        handoff_path=HANDOFF,
        role="finalizer",
        story_id="  ",
        chat_entries=(entry,),
    )

    assert result.sha is not None
    assert _git(repo, "log", "-1", "--format=%s") == (
        "Chat changes after the finalizer role"
    )


def test_commit_chat_changes_with_nothing_owned_commits_nothing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _write(repo / "docs" / "notes.md", "chat note\n")
    entry = _record(tmp_path / "record.json", repo, "docs/notes.md")
    _write(repo / "docs" / "notes.md", "role rewrote it\n")
    _write(repo / "src" / "app.py", "VALUE = 2\n")

    result = commit_chat_changes(
        repo_root=repo,
        handoff_path=HANDOFF,
        role="planner",
        story_id=STORY_ID,
        chat_entries=(entry,),
    )

    assert result.sha is None
    assert result.nothing_to_commit is True
    assert result.skipped_reason
    assert result.error is None
    assert result.committed == ()
    assert _git(repo, "rev-parse", "HEAD") == base


def test_commit_chat_changes_treats_recorded_names_as_literal_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    # A recorded name that is also a glob must not select the files it
    # would match as a pattern.
    _write(repo / "docs" / "[ab].md", "chat note\n")
    entry = _record(tmp_path / "record.json", repo, "docs/[ab].md")
    _write(repo / "docs" / "a.md", "not from chat\n")
    _write(repo / "docs" / "b.md", "not from chat\n")

    result = commit_chat_changes(
        repo_root=repo,
        handoff_path=HANDOFF,
        role="planner",
        story_id=STORY_ID,
        chat_entries=(entry,),
    )

    assert result.error is None and result.sha is not None
    assert result.committed == ("docs/[ab].md",)
    assert _commit_files(repo, result.sha) == ["docs/[ab].md"]
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    assert "docs/a.md" in status
    assert "docs/b.md" in status


# ---------------------------------------------------------------------------
# _commit_chat_changes_after_role
# ---------------------------------------------------------------------------


def _helper_setup(tmp_path: Path, *, saved_story: bool = True):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    if saved_story:
        _check_out_saved_story(repo, base)
    _write(repo / "docs" / "notes.md", "chat note\n")
    record = chat_changes_path(repo)
    _record(record, repo, "docs/notes.md")
    assert load_chat_changes(record)
    return repo, base, record


def test_helper_commits_after_the_planner_on_a_saved_story_branch(
    tmp_path: Path,
) -> None:
    repo, base, record = _helper_setup(tmp_path)
    messages: list[tuple[str, str]] = []

    result = orchestrator._commit_chat_changes_after_role(
        config=_config(repo),
        route="planner",
        handoff_content=_blocked_handoff(),
        log=_collecting_log(messages),
    )

    assert result is not None and result.sha is not None
    assert result.sha == _git(repo, "rev-parse", "HEAD") != base
    assert result.chat_committed == ("docs/notes.md",)
    assert not record.exists()
    agent_lines = [message for level, message in messages if level == "AGENT"]
    assert len(agent_lines) == 1
    assert result.sha[:12] in agent_lines[0]
    assert "planner" in agent_lines[0]
    assert "operator-influenced" in agent_lines[0]
    assert "docs/notes.md" in agent_lines[0]
    assert orchestrator._chat_marker_after_role(result, _blocked_handoff()) == (
        STORY_ID,
        MARKER,
    )


@pytest.mark.parametrize(
    ("route", "commit_mode"),
    [("backend", "dispatcher"), ("frontend", "dispatcher"), ("planner", "agent")],
)
def test_helper_does_nothing_for_implementers_or_agent_commit_mode(
    tmp_path: Path, route: str, commit_mode: str
) -> None:
    repo, base, record = _helper_setup(tmp_path)
    messages: list[tuple[str, str]] = []
    config = (
        _config(repo)
        if commit_mode == "dispatcher"
        else DispatchConfig(repo_root=repo, commit_mode="agent")
    )

    result = orchestrator._commit_chat_changes_after_role(
        config=config,
        route=route,
        handoff_content=_blocked_handoff(),
        log=_collecting_log(messages),
    )

    assert result is None
    assert _git(repo, "rev-parse", "HEAD") == base
    assert record.exists()
    assert messages == []


def test_helper_does_nothing_with_an_empty_record(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _check_out_saved_story(repo, base)
    _write(repo / "docs" / "notes.md", "not from chat\n")
    messages: list[tuple[str, str]] = []

    result = orchestrator._commit_chat_changes_after_role(
        config=_config(repo),
        route="planner",
        handoff_content=_blocked_handoff(),
        log=_collecting_log(messages),
    )

    assert result is None
    assert _git(repo, "rev-parse", "HEAD") == base
    assert messages == []


def test_helper_without_a_saved_story_branch_keeps_the_record(
    tmp_path: Path,
) -> None:
    repo, base, record = _helper_setup(tmp_path, saved_story=False)
    messages: list[tuple[str, str]] = []

    result = orchestrator._commit_chat_changes_after_role(
        config=_config(repo),
        route="auditor",
        handoff_content=_blocked_handoff(),
        log=_collecting_log(messages),
    )

    assert result is None
    assert _git(repo, "rev-parse", "HEAD") == base
    assert record.exists()
    assert "docs/notes.md" in _git(repo, "status", "--porcelain")
    info = [message for level, message in messages if level == "INFO"]
    assert len(info) == 1
    assert "docs/notes.md" in info[0]
    assert "ride the next implementer commit" in info[0]
    assert len(messages) == 1


def test_helper_keeps_the_record_when_the_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, record = _helper_setup(tmp_path)
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        orchestrator,
        "commit_chat_changes",
        lambda **_kwargs: DispatcherCommitResult(
            sha=None, error="git commit failed: index locked"
        ),
    )

    result = orchestrator._commit_chat_changes_after_role(
        config=_config(repo),
        route="tpm",
        handoff_content=_blocked_handoff(),
        log=_collecting_log(messages),
    )

    assert result is not None and result.sha is None
    assert record.exists()
    errors = [message for level, message in messages if level == "ERROR"]
    assert len(errors) == 1 and "index locked" in errors[0]
    assert orchestrator._chat_marker_after_role(result, _blocked_handoff()) is None


def test_helper_warns_when_the_clear_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, record = _helper_setup(tmp_path)
    messages: list[tuple[str, str]] = []

    def failing_clear(_path: Path) -> None:
        raise OSError("record is locked")

    monkeypatch.setattr(orchestrator, "clear_chat_changes", failing_clear)

    result = orchestrator._commit_chat_changes_after_role(
        config=_config(repo),
        route="validator",
        handoff_content=_blocked_handoff(),
        log=_collecting_log(messages),
    )

    assert result is not None and result.sha is not None
    warns = [message for level, message in messages if level == "WARN"]
    assert len(warns) == 1 and "record is locked" in warns[0]
    assert record.exists()


@pytest.mark.parametrize("approved_on", ["started_from", "written"])
def test_helper_commits_nothing_while_the_story_stands_approved(
    tmp_path: Path, approved_on: str
) -> None:
    # A commit after an approving audit would be published under an approval
    # that never reviewed it.
    repo, base, record = _helper_setup(tmp_path)
    if approved_on == "written":
        _write(repo / HANDOFF, _approved_handoff())
        started_from = _backend_claim()
    else:
        started_from = _approved_handoff()
    messages: list[tuple[str, str]] = []

    result = orchestrator._commit_chat_changes_after_role(
        config=_config(repo),
        route="auditor" if approved_on == "written" else "finalizer",
        handoff_content=started_from,
        log=_collecting_log(messages),
    )

    assert result is None
    assert _git(repo, "rev-parse", "HEAD") == base
    assert record.exists()
    assert "docs/notes.md" in _git(repo, "status", "--porcelain")
    assert len(messages) == 1
    level, message = messages[0]
    assert level == "INFO"
    assert "verified_pass" in message and "docs/notes.md" in message
    assert "ride the next implementer commit" in message


# ---------------------------------------------------------------------------
# The chat marker reaches the story's later audit
# ---------------------------------------------------------------------------


def _audit_influence(handoff: str, marker) -> OperatorInfluence | None:
    return orchestrator._audit_operator_influence(
        route="auditor",
        recovery_kind=None,
        active_operator_influence=None,
        handoff_content=handoff,
        chat_commit_influence=orchestrator._chat_commit_influence_for(
            marker, handoff
        ),
    )


def test_carry_marker_writes_the_section_for_the_same_story(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo / HANDOFF, _backend_claim())
    marker = (STORY_ID, MARKER)

    orchestrator._carry_chat_marker_into_handoff(
        _config(repo), marker, lambda *_args: None
    )

    handoff = (repo / HANDOFF).read_text(encoding="utf-8")
    assert handoff.count("## Operator Influence") == 1
    assert _audit_influence(handoff, marker) == MARKER


def test_carry_marker_leaves_another_story_alone(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    other = _backend_claim().replace(STORY_ID, "EPIC-1-S2")
    _write(repo / HANDOFF, other)

    orchestrator._carry_chat_marker_into_handoff(
        _config(repo), (STORY_ID, MARKER), lambda *_args: None
    )

    assert (repo / HANDOFF).read_text(encoding="utf-8") == other


def test_implementer_commit_carries_a_held_marker_to_the_audit(
    tmp_path: Path,
) -> None:
    # A planner-cycle chat commit, then a backend handback without new chat
    # entries that dropped the section: the story's audit still sees it.
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _write(repo / HANDOFF, _backend_claim())
    marker = (STORY_ID, MARKER)

    result = orchestrator._commit_for_implementer(
        config=_config(repo),
        route="backend",
        assignment_content=_planner_assignment(),
        pre_dispatch_state=state,
        log=lambda *_args: None,
        chat_commit_marker=marker,
    )

    assert result is not None and result.sha is not None
    assert result.chat_committed == ()
    handoff = (repo / HANDOFF).read_text(encoding="utf-8")
    assert f"scope_sha: {result.sha}" in handoff
    assert handoff.count("## Operator Influence") == 1
    assert _audit_influence(handoff, marker) == MARKER


def test_implementer_commit_ignores_a_marker_for_another_story(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = capture_pre_dispatch_state(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _write(repo / HANDOFF, _backend_claim())

    result = orchestrator._commit_for_implementer(
        config=_config(repo),
        route="backend",
        assignment_content=_planner_assignment(),
        pre_dispatch_state=state,
        log=lambda *_args: None,
        chat_commit_marker=("EPIC-1-S9", MARKER),
    )

    assert result is not None and result.sha is not None
    assert "## Operator Influence" not in (repo / HANDOFF).read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# _run_loop
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_story_bounce():
    ledger.reset_bounce(STORY_ID)
    yield
    ledger.reset_bounce(STORY_ID)


def test_run_loop_commits_chat_changes_after_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_story_bounce: None
) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _check_out_saved_story(repo, base)
    record = chat_changes_path(repo)
    messages: list[tuple[str, str]] = []

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        # A chat side session changed a file mid-dispatch.
        _write(repo / "docs" / "notes.md", "chat note\n")
        _record(record, repo, "docs/notes.md")
        path.write_text(_planner_assignment(), encoding="utf-8")
        # The planner commits the handoff despite dispatcher commit mode; the
        # dispatcher undoes that before the chat commit.
        _git(repo, "add", HANDOFF.as_posix())
        _git(repo, "commit", "-q", "-m", "planner handoff commit")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda *_args, **_kwargs: SubagentResult(
            stdout="", stderr="", exit_code=0, elapsed_seconds=0.01
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *_args, **_kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="backend"
        ),
    )
    config = DispatchConfig(
        repo_root=repo,
        poll_interval_seconds=0,
        pause_recovery="off",
        max_consecutive_failures=3,
        enforce_work_packet=True,
        work_packet_verification_timeout_seconds=60,
        commit_mode="dispatcher",
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
    )

    exit_code = orchestrator.run_loop(
        config, max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    head = _git(repo, "rev-parse", "HEAD")
    assert head != base
    assert _git(repo, "rev-parse", "HEAD~1") == base
    assert _commit_files(repo, head) == ["docs/notes.md"]
    assert _git(repo, "log", "-1", "--format=%s") == (
        f"{STORY_ID} Chat changes after the planner role"
    )
    assert f"- Message SHA-256: {MESSAGE_SHA}" in _body(repo)
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == BRANCH
    assert _git(repo, "status", "--porcelain") == f"M {HANDOFF.as_posix()}"
    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    assert "next_agent: backend" in handoff
    # The planner's assignment stays on the story, so it carries the marker
    # a later audit of the story matches against the dispatcher-held one.
    assert handoff.count("## Operator Influence") == 1
    assert _audit_influence(handoff, (STORY_ID, MARKER)) == MARKER
    assert not record.exists()
    assert any(
        level == "WARN" and "undid it with `git reset --soft`" in message
        for level, message in messages
    )
    assert any(
        level == "AGENT" and "chat change(s) after the planner role" in message
        for level, message in messages
    )
