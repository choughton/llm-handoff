"""CHAT-24: chat changes after an approved audit go back to the audit fan-out.

Outside ``local`` publish mode, chat changes recorded during an approving
audit cycle are committed on the story branch and the story is audited again
instead of publishing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess
from typing import Any

import pytest

from llm_handoff import orchestrator
from llm_handoff import publish
import llm_handoff.ledger as ledger
from llm_handoff.agents import DispatchResult
from llm_handoff.audit_review import OperatorInfluence
from llm_handoff.chat_changes import (
    chat_changes_path,
    load_chat_changes,
    record_chat_changes,
)
from llm_handoff.chat_side_session import ChatSessionResult
from llm_handoff.config import DispatchConfig
from llm_handoff.dispatcher_commit import DispatcherCommitResult
from llm_handoff.publish import PublishOutcome
from llm_handoff.validator import ValidationResult


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


def _audit_assignment(scope_sha: str) -> str:
    return f"""---
next_agent: auditor
reason: "Backend work complete; audit requested."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
status: ready_for_review
scope_sha: {scope_sha}
close_type: story
producer: backend
---

# Backend Handback

## Verification Evidence

- **Commands run:** python -c "import sys; sys.exit(0)"
- **Output summary:** exit 0
- **Commit SHA verified:** {scope_sha}
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""


def _approval(scope_sha: str) -> str:
    return f"""---
next_agent: planner
reason: "Story approved."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
status: verified_pass
scope_sha: {scope_sha}
close_type: story
producer: auditor
---

# Audit Report

Approved.

## Verification Evidence

- **Commands run:** git diff
- **Output summary:** clean
- **Commit SHA verified:** {scope_sha}
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
    _write(repo / HANDOFF, "# placeholder\n")
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


def _implementer_commit(repo: Path) -> str:
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", f"{STORY_ID} Change the value")
    return _git(repo, "rev-parse", "HEAD")


def _clock():
    return lambda: datetime(
        2026, 10, 20, 8, 30, 5, tzinfo=timezone(timedelta(hours=-4))
    )


def _record(record_path: Path, repo: Path, *paths: str):
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
        role="auditor",
        story_id=STORY_ID,
        handoff_path=HANDOFF,
        now=_clock(),
    )
    assert entry is not None
    return entry


def _collecting_log(messages: list[tuple[str, str]]):
    return lambda level, message: messages.append((level, message))


def _config(repo: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo,
        "commit_mode": "dispatcher",
        "publish": {"mode": "pr"},
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _story_repo(tmp_path: Path, *, saved_story: bool = True):
    """A story branch with one implementer commit and a chat change recorded."""

    repo = tmp_path / "repo"
    base = _init_repo(repo)
    if saved_story:
        _check_out_saved_story(repo, base)
    implemented = _implementer_commit(repo)
    assignment = _audit_assignment(implemented)
    _write(repo / HANDOFF, _approval(implemented))
    _write(repo / "docs" / "notes.md", "chat note\n")
    record = chat_changes_path(repo)
    _record(record, repo, "docs/notes.md")
    assert load_chat_changes(record)
    return repo, base, implemented, assignment, record


# ---------------------------------------------------------------------------
# _send_chat_changes_back_to_audit
# ---------------------------------------------------------------------------


def test_helper_does_nothing_in_agent_commit_mode(tmp_path: Path) -> None:
    repo, _base, implemented, assignment, record = _story_repo(tmp_path)
    messages: list[tuple[str, str]] = []

    result = orchestrator._send_chat_changes_back_to_audit(
        _config(repo, commit_mode="agent", publish={"mode": "local"}),
        assignment,
        _collecting_log(messages),
    )

    assert result is None
    assert _git(repo, "rev-parse", "HEAD") == implemented
    assert record.exists()
    assert messages == []


def test_helper_does_nothing_with_an_empty_record(tmp_path: Path) -> None:
    repo, _base, implemented, assignment, record = _story_repo(tmp_path)
    record.unlink()
    messages: list[tuple[str, str]] = []

    result = orchestrator._send_chat_changes_back_to_audit(
        _config(repo), assignment, _collecting_log(messages)
    )

    assert result is None
    assert _git(repo, "rev-parse", "HEAD") == implemented
    assert messages == []


def test_helper_does_nothing_without_a_saved_story_branch(tmp_path: Path) -> None:
    repo, _base, implemented, assignment, record = _story_repo(
        tmp_path, saved_story=False
    )
    messages: list[tuple[str, str]] = []

    result = orchestrator._send_chat_changes_back_to_audit(
        _config(repo), assignment, _collecting_log(messages)
    )

    assert result is None
    assert _git(repo, "rev-parse", "HEAD") == implemented
    assert record.exists()
    assert messages == []


def test_helper_commits_and_rewrites_the_handoff_for_a_re_audit(
    tmp_path: Path,
) -> None:
    repo, _base, implemented, assignment, record = _story_repo(tmp_path)
    messages: list[tuple[str, str]] = []
    config = _config(repo)

    result = orchestrator._send_chat_changes_back_to_audit(
        config, assignment, _collecting_log(messages)
    )

    head = _git(repo, "rev-parse", "HEAD")
    assert head != implemented
    assert _git(repo, "rev-parse", "HEAD~1") == implemented
    assert result == (STORY_ID, MARKER)
    assert _git(repo, "log", "-1", "--format=%s") == (
        f"{STORY_ID} Chat changes after the auditor role"
    )
    assert _git(repo, "show", "--name-only", "--format=", head).splitlines() == [
        "docs/notes.md"
    ]
    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    assert "next_agent: auditor\n" in handoff
    assert "status: ready_for_review\n" in handoff
    assert f"scope_sha: {head}\n" in handoff
    assert f"- **Commit SHA verified:** {head}" in handoff
    assert implemented not in handoff
    assert handoff.count("## Operator Influence") == 1
    assert orchestrator._handoff_operator_influence(handoff) == MARKER
    # The re-audit's fan-out accepts the section against the returned marker.
    assert (
        orchestrator._audit_operator_influence(
            route="auditor",
            recovery_kind=None,
            active_operator_influence=None,
            handoff_content=handoff,
            chat_commit_influence=orchestrator._chat_commit_influence_for(
                result, handoff
            ),
        )
        == MARKER
    )
    assert not record.exists()
    agent_lines = [message for level, message in messages if level == "AGENT"]
    assert len(agent_lines) == 1
    assert head[:12] in agent_lines[0]
    assert "docs/notes.md" in agent_lines[0]
    assert "back to the audit fan-out instead of publishing" in agent_lines[0]
    assert [level for level, _message in messages] == ["AGENT"]


def test_helper_warns_when_the_clear_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, _implemented, assignment, record = _story_repo(tmp_path)
    messages: list[tuple[str, str]] = []

    def failing_clear(_path: Path) -> None:
        raise OSError("record is locked")

    monkeypatch.setattr(orchestrator, "clear_chat_changes", failing_clear)

    result = orchestrator._send_chat_changes_back_to_audit(
        _config(repo), assignment, _collecting_log(messages)
    )

    assert result == (STORY_ID, MARKER)
    warns = [message for level, message in messages if level == "WARN"]
    assert len(warns) == 1 and "record is locked" in warns[0]
    assert record.exists()


def test_helper_clears_the_record_only_after_the_handoff_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, _implemented, assignment, record = _story_repo(tmp_path)
    config = _config(repo)
    handoffs_at_clear: list[str] = []
    real_clear = orchestrator.clear_chat_changes

    def observing_clear(path: Path) -> None:
        handoffs_at_clear.append(
            config.handoff_full_path.read_text(encoding="utf-8")
        )
        real_clear(path)

    monkeypatch.setattr(orchestrator, "clear_chat_changes", observing_clear)

    result = orchestrator._send_chat_changes_back_to_audit(
        config, assignment, lambda *_args: None
    )

    head = _git(repo, "rev-parse", "HEAD")
    assert result == (STORY_ID, MARKER)
    assert len(handoffs_at_clear) == 1
    assert f"scope_sha: {head}\n" in handoffs_at_clear[0]
    assert "next_agent: auditor\n" in handoffs_at_clear[0]
    assert not record.exists()


def test_helper_keeps_the_record_when_the_handoff_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, implemented, assignment, record = _story_repo(tmp_path)
    approval = (repo / HANDOFF).read_text(encoding="utf-8")
    messages: list[tuple[str, str]] = []

    def failing_write(_path: Path, _content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "_write_handoff_durably", failing_write)

    result = orchestrator._send_chat_changes_back_to_audit(
        _config(repo), assignment, _collecting_log(messages)
    )

    assert result == orchestrator.CHAT_REAUDIT_FAILED
    assert record.exists()
    assert load_chat_changes(record)
    assert (repo / HANDOFF).read_text(encoding="utf-8") == approval
    assert _git(repo, "rev-parse", "HEAD~1") == implemented
    errors = [message for level, message in messages if level == "ERROR"]
    assert len(errors) == 1
    assert "disk full" in errors[0] and "record is kept" in errors[0]
    assert [level for level, _message in messages] == ["ERROR"]


def test_helper_keeps_the_record_when_the_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, implemented, assignment, record = _story_repo(tmp_path)
    approval = (repo / HANDOFF).read_text(encoding="utf-8")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        orchestrator,
        "commit_chat_changes",
        lambda **_kwargs: DispatcherCommitResult(
            sha=None, error="git commit failed: index locked"
        ),
    )

    result = orchestrator._send_chat_changes_back_to_audit(
        _config(repo), assignment, _collecting_log(messages)
    )

    assert result == orchestrator.CHAT_REAUDIT_FAILED
    assert record.exists()
    assert (repo / HANDOFF).read_text(encoding="utf-8") == approval
    assert _git(repo, "rev-parse", "HEAD") == implemented
    errors = [message for level, message in messages if level == "ERROR"]
    assert len(errors) == 1
    assert "index locked" in errors[0] and "docs/notes.md" in errors[0]


def test_helper_clears_a_record_with_nothing_left_to_commit(
    tmp_path: Path,
) -> None:
    repo, _base, implemented, assignment, record = _story_repo(tmp_path)
    _write(repo / "docs" / "notes.md", "the role rewrote it\n")
    approval = (repo / HANDOFF).read_text(encoding="utf-8")

    result = orchestrator._send_chat_changes_back_to_audit(
        _config(repo), assignment, lambda *_args: None
    )

    assert result is None
    assert not record.exists()
    assert _git(repo, "rev-parse", "HEAD") == implemented
    assert (repo / HANDOFF).read_text(encoding="utf-8") == approval


# ---------------------------------------------------------------------------
# The CHAT-23 approved-story line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "mode", "sent_back"),
    [
        ("auditor", "pr", True),
        ("auditor", "merge-commit", True),
        ("auditor", "local", False),
        ("finalizer", "pr", False),
    ],
)
def test_approved_story_line_names_the_re_audit_only_before_a_publish(
    tmp_path: Path, route: str, mode: str, sent_back: bool
) -> None:
    repo, _base, implemented, _assignment, record = _story_repo(tmp_path)
    messages: list[tuple[str, str]] = []

    result = orchestrator._commit_chat_changes_after_role(
        config=_config(repo, publish={"mode": mode}),
        route=route,
        handoff_content=_approval(implemented),
        log=_collecting_log(messages),
    )

    assert result is None
    assert _git(repo, "rev-parse", "HEAD") == implemented
    assert record.exists()
    assert len(messages) == 1
    level, message = messages[0]
    assert level == "INFO"
    assert "verified_pass" in message and "docs/notes.md" in message
    if sent_back:
        assert "sent back to the audit fan-out before the story publishes" in message
        assert "ride the next implementer commit" not in message
    else:
        assert "ride the next implementer commit" in message
        assert "audit fan-out" not in message


# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_story_bounce():
    ledger.reset_bounce(STORY_ID)
    yield
    ledger.reset_bounce(STORY_ID)


def _loop_config(repo: Path, mode: str = "pr") -> DispatchConfig:
    return DispatchConfig(
        repo_root=repo,
        poll_interval_seconds=0,
        pause_recovery="off",
        commit_mode="dispatcher",
        enforce_work_packet=True,
        publish={"mode": mode},
        audit={
            "reviewers": ["quality", "security"],
            "security_provider": "claude",
        },
    )


def _quiet_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator, "leave_merged_story_branch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "resume_paused_publish", lambda **_kwargs: PublishOutcome()
    )
    monkeypatch.setattr(
        orchestrator, "resume_interrupted_publish", lambda **_kwargs: PublishOutcome()
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *_args, **_kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda *_args, **_kwargs: pytest.fail("the audit fan-out is faked"),
    )


def _loop_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _check_out_saved_story(repo, base)
    implemented = _implementer_commit(repo)
    _write(repo / HANDOFF, _audit_assignment(implemented))
    return repo, base, implemented


def test_run_loop_re_audits_chat_changes_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_story_bounce: None
) -> None:
    repo, base, implemented = _loop_repo(tmp_path)
    config = _loop_config(repo)
    record = chat_changes_path(repo)
    audits: list[tuple[str | None, OperatorInfluence | None, str | None]] = []
    order: list[str] = []

    def fake_fan_out(_config: DispatchConfig, _log: Any, **kwargs: Any):
        context = kwargs["audit_context"]
        frontmatter = orchestrator._parse_frontmatter_or_none(context.handoff_content)
        scope_sha = frontmatter.scope_sha if frontmatter is not None else None
        audits.append(
            (scope_sha, context.operator_influence, context.implementer_base_sha)
        )
        order.append("audit")
        if len(audits) == 1:
            # A chat side session changed a file during the approving audit.
            _write(repo / "docs" / "notes.md", "chat note\n")
            _record(record, repo, "docs/notes.md")
        config.handoff_full_path.write_text(
            _approval(str(scope_sha)), encoding="utf-8"
        )
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def fake_publish(**kwargs: Any) -> PublishOutcome:
        order.append("publish_story")
        assert f"scope_sha: {_git(repo, 'rev-parse', 'HEAD')}" in kwargs[
            "handoff_content"
        ]
        return PublishOutcome(stop=True, exit_code=0, message="published")

    _quiet_startup(monkeypatch)
    monkeypatch.setattr(orchestrator, "_dispatch_audit_fan_out", fake_fan_out)
    monkeypatch.setattr(orchestrator, "publish_story", fake_publish)
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config, max_cycles=5, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert order == ["audit", "audit", "publish_story"]
    chat_sha = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-parse", "HEAD~1") == implemented
    assert _git(repo, "log", "-1", "--format=%s") == (
        f"{STORY_ID} Chat changes after the auditor role"
    )
    assert audits[0] == (implemented, None, base)
    # The re-audit covers the whole story branch, including the chat commit,
    # with the chat commit's marker.
    assert audits[1] == (chat_sha, MARKER, base)
    assert not record.exists()
    assert any(
        level == "AGENT"
        and "back to the audit fan-out instead of publishing" in message
        for level, message in messages
    )


def test_run_loop_stops_without_publishing_when_the_chat_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_story_bounce: None
) -> None:
    repo, _base, implemented = _loop_repo(tmp_path)
    config = _loop_config(repo)
    record = chat_changes_path(repo)

    def fake_fan_out(_config: DispatchConfig, _log: Any, **_kwargs: Any):
        _write(repo / "docs" / "notes.md", "chat note\n")
        _record(record, repo, "docs/notes.md")
        config.handoff_full_path.write_text(_approval(implemented), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    _quiet_startup(monkeypatch)
    monkeypatch.setattr(orchestrator, "_dispatch_audit_fan_out", fake_fan_out)
    monkeypatch.setattr(
        orchestrator,
        "publish_story",
        lambda **_kwargs: pytest.fail("publish_story must not run"),
    )
    monkeypatch.setattr(
        orchestrator,
        "commit_chat_changes",
        lambda **_kwargs: DispatcherCommitResult(
            sha=None, error="git commit failed: index locked"
        ),
    )
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config, max_cycles=5, log=_collecting_log(messages)
    )

    assert exit_code == 1
    assert record.exists()
    assert _git(repo, "rev-parse", "HEAD") == implemented
    errors = [message for level, message in messages if level == "ERROR"]
    assert len(errors) == 1 and "index locked" in errors[0]


def test_run_loop_publishes_when_nothing_is_left_to_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_story_bounce: None
) -> None:
    repo, _base, implemented = _loop_repo(tmp_path)
    config = _loop_config(repo)
    record = chat_changes_path(repo)
    published: list[str] = []

    def fake_fan_out(_config: DispatchConfig, _log: Any, **_kwargs: Any):
        _write(repo / "docs" / "notes.md", "chat note\n")
        _record(record, repo, "docs/notes.md")
        # The chat change is gone again before the publish.
        (repo / "docs" / "notes.md").unlink()
        config.handoff_full_path.write_text(_approval(implemented), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def fake_publish(**_kwargs: Any) -> PublishOutcome:
        published.append("publish_story")
        return PublishOutcome()

    _quiet_startup(monkeypatch)
    monkeypatch.setattr(orchestrator, "_dispatch_audit_fan_out", fake_fan_out)
    monkeypatch.setattr(orchestrator, "publish_story", fake_publish)

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=lambda *_args: None)

    assert exit_code == 0
    assert published == ["publish_story"]
    assert not record.exists()
    assert _git(repo, "rev-parse", "HEAD") == implemented


def test_run_loop_in_local_mode_keeps_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_story_bounce: None
) -> None:
    repo, _base, implemented = _loop_repo(tmp_path)
    config = _loop_config(repo, mode="local")
    record = chat_changes_path(repo)

    def fake_fan_out(_config: DispatchConfig, _log: Any, **_kwargs: Any):
        _write(repo / "docs" / "notes.md", "chat note\n")
        _record(record, repo, "docs/notes.md")
        config.handoff_full_path.write_text(_approval(implemented), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    _quiet_startup(monkeypatch)
    monkeypatch.setattr(orchestrator, "_dispatch_audit_fan_out", fake_fan_out)
    monkeypatch.setattr(
        orchestrator,
        "publish_story",
        lambda **_kwargs: pytest.fail("local mode never publishes"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_send_chat_changes_back_to_audit",
        lambda *_args, **_kwargs: pytest.fail("local mode keeps CHAT-23 behavior"),
    )

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=lambda *_args: None)

    assert exit_code == 0
    assert record.exists()
    assert _git(repo, "rev-parse", "HEAD") == implemented
    assert "docs/notes.md" in _git(repo, "status", "--porcelain", "--untracked-files=all")
    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    assert "status: verified_pass" in handoff
    assert "## Operator Influence" not in handoff
