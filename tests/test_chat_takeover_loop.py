"""CHAT-28: the loop follows a chat rewrite of HANDOFF.md."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess
from typing import Any, Iterator

import pytest

from llm_handoff import chat_takeover
from llm_handoff import orchestrator
import llm_handoff.ledger as ledger
from llm_handoff.agents import DispatchResult
from llm_handoff.chat_changes import chat_changes_path, record_chat_changes
from llm_handoff.chat_side_session import ChatSessionResult
from llm_handoff.config import IN_FLIGHT_STATE_PATH, DispatchConfig
from llm_handoff.validator import ValidationResult


HANDOFF = Path("docs/handoff/HANDOFF.md")
STORY_ID = "EPIC-1-S1"


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


def _sha(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _backend_assignment() -> str:
    return f"""---
next_agent: backend
reason: "Set the value."
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


def _planner_request() -> str:
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


def _chat_handoff(next_agent: str) -> str:
    return f"""---
next_agent: {next_agent}
reason: "The operator redirected the story from chat."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
producer: planner
---

# Operator Redirect

The chat answer rewrote this handoff.
"""


def _init_repo(repo: Path, handoff: str) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "llm-handoff tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    _write(repo / ".gitignore", "logs/\n")
    _write(repo / "src" / "app.py", "VALUE = 1\n")
    _write(repo / "src" / "api.py", "API = 1\n")
    _write(repo / HANDOFF, handoff)
    _write(repo / "PROJECT_STATE.md", "# PROJECT STATE\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD")


def _record_chat_file(repo: Path, rel_path: str, content: str) -> None:
    _write(repo / rel_path, content)
    entry = record_chat_changes(
        chat_changes_path(repo),
        repo_root=repo,
        result=ChatSessionResult(
            text="done",
            session_id="session-1",
            exit_code=0,
            error=None,
            edited_paths=(rel_path,),
        ),
        message="add a note",
        role="backend",
        story_id=STORY_ID,
        handoff_path=HANDOFF,
    )
    assert entry is not None


def _config(repo: Path) -> DispatchConfig:
    return DispatchConfig(
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


def _collecting_log(messages: list[tuple[str, str]]):
    return lambda level, message: messages.append((level, message))


def _refuse(name: str):
    def refuse(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError(f"{name} must not run after a chat takeover")

    return refuse


class _TakeoverWindow:
    """Refuses wrapped calls from a chat takeover until the next dispatch."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.active = False

    def refuse(self, name: str) -> None:
        real = getattr(orchestrator, name)

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if self.active:
                raise AssertionError(f"{name} must not run after a chat takeover")
            return real(*args, **kwargs)

        self.monkeypatch.setattr(orchestrator, name, guarded)


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    ledger.reset_bounce(STORY_ID)
    chat_takeover.CONTROLLER.clear()
    chat_takeover.CONTROLLER.disarm()
    yield
    chat_takeover.CONTROLLER.clear()
    chat_takeover.CONTROLLER.disarm()
    ledger.reset_bounce(STORY_ID)


class _ValidateRecorder:
    """Records validate_handoff calls and the loop state at each call."""

    def __init__(self, repo: Path, routing_instruction: str) -> None:
        self.repo = repo
        self.routing_instruction = routing_instruction
        self.calls: list[dict[str, Any]] = []

    def __call__(self, path: Path, previous_agent: str, **kwargs: Any):
        self.calls.append(
            {
                "previous_agent": previous_agent,
                "kwargs": kwargs,
                "content": path.read_text(encoding="utf-8"),
                "armed": chat_takeover.CONTROLLER.armed(),
                "requested": chat_takeover.CONTROLLER.requested(),
                "in_flight_exists": (self.repo / IN_FLIGHT_STATE_PATH).exists(),
            }
        )
        return ValidationResult(
            verdict="YES",
            warnings=[],
            errors=[],
            routing_instruction=self.routing_instruction,
        )


def test_backend_stopped_by_chat_follows_the_chat_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    assignment = _backend_assignment()
    base = _init_repo(repo, assignment)
    chat_handoff = _chat_handoff("planner")
    armed_during_dispatch: list[bool] = []
    window = _TakeoverWindow(monkeypatch)

    def stopped_backend(path: Path, **_kwargs: object) -> DispatchResult:
        armed_during_dispatch.append(chat_takeover.CONTROLLER.armed())
        # The backend's own partial edit, then a chat answer that changes a
        # file and rewrites the handoff while the backend still runs.
        _write(repo / "src" / "app.py", "VALUE = 2\n")
        _record_chat_file(repo, "docs/notes.md", "chat note\n")
        path.write_text(chat_handoff, encoding="utf-8")
        chat_takeover.request_takeover(chat_takeover.handoff_content_sha(path))
        window.active = True
        raise chat_takeover.ChatTakeover

    planner_calls: list[str] = []

    def next_planner(path: Path, **_kwargs: object) -> DispatchResult:
        window.active = False
        planner_calls.append(path.read_text(encoding="utf-8"))
        path.write_text(_backend_assignment(), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    validate = _ValidateRecorder(repo, "planner")
    persisted: list[Any] = []
    real_persist = orchestrator._persist_implementer_leftovers

    def recording_persist(config: DispatchConfig, leftovers: Any, log: Any) -> None:
        persisted.append(leftovers)
        real_persist(config, leftovers, log)

    role_records: list[str] = []
    real_record_role = orchestrator._record_role_dispatch

    def recording_role_record(config: DispatchConfig, **kwargs: Any) -> None:
        role_records.append(kwargs["route"])
        real_record_role(config, **kwargs)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", stopped_backend)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", next_planner)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    monkeypatch.setattr(
        orchestrator, "_persist_implementer_leftovers", recording_persist
    )
    monkeypatch.setattr(orchestrator, "_record_role_dispatch", recording_role_record)
    for name in (
        "_record_in_flight_handback",
        "_repair_handoff_frontmatter_file",
        "_normalize_handoff_next_agent_file",
        "_log_dispatch_completion",
        "_stop_on_auth_failure",
        "_record_failure",
        "_commit_chat_changes_after_role",
        "defer_blocked_handback",
        "_commit_for_implementer",
        "_run_work_claim_gate",
    ):
        window.refuse(name)
    messages: list[tuple[str, str]] = []

    def log(level: str, message: str) -> None:
        messages.append((level, message))
        if message.startswith("--- End of cycle"):
            # The takeover cycle ended; the next cycle runs normally.
            window.active = False

    exit_code = orchestrator.run_loop(_config(repo), max_cycles=2, log=log)

    assert exit_code == 0
    assert armed_during_dispatch == [True]
    # The next cycle routed on the chat's handoff.
    assert planner_calls == [chat_handoff]
    # The chat's handoff was validated as a planner handoff against the one
    # the backend dispatch started from, after the window was disarmed and
    # the backend's in-flight record was deleted.
    chat_check = validate.calls[0]
    assert chat_check["content"] == chat_handoff
    assert chat_check["previous_agent"] == "planner"
    assert chat_check["kwargs"]["prior_handoff_sha"] == _sha(assignment)
    assert chat_check["kwargs"]["prior_handoff_content"] == assignment
    assert chat_check["kwargs"]["require_work_packet"] is True
    assert chat_check["kwargs"]["dispatcher_commit_mode"] is True
    assert chat_check["armed"] is False
    assert chat_check["requested"] is False
    assert chat_check["in_flight_exists"] is False
    # The backend's partial file is carry-over work; the chat's file is not.
    takeover_leftovers = persisted[-1]
    assert takeover_leftovers is not None
    paths = [path for path, _fingerprint in takeover_leftovers.fingerprints]
    assert paths == ["src/app.py"]
    assert takeover_leftovers.story_key == f"story:{STORY_ID}"
    # The stopped dispatch returned no result to record; the planner did.
    assert role_records == ["planner"]
    # No dispatcher commit was made.
    assert _git(repo, "rev-parse", "HEAD") == base
    assert "src/app.py" in _git(repo, "status", "--porcelain")
    takeover_lines = [
        message
        for level, message in messages
        if level == "AGENT" and "A chat answer rewrote HANDOFF.md" in message
    ]
    assert len(takeover_lines) == 1
    assert "backend" in takeover_lines[0]
    assert any(
        level == "AGENT" and "The chat's handoff is valid" in message
        for level, message in messages
    )
    assert chat_takeover.CONTROLLER.armed() is False


def test_planner_returning_with_a_takeover_requested_follows_the_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    started_from = _planner_request()
    base = _init_repo(repo, started_from)
    chat_handoff = _chat_handoff("backend")
    window = _TakeoverWindow(monkeypatch)

    def planner(path: Path, **_kwargs: object) -> DispatchResult:
        # The planner writes its handoff, then a chat answer rewrites it and
        # the planner exits before the poll saw the request.
        path.write_text(_backend_assignment(), encoding="utf-8")
        path.write_text(chat_handoff, encoding="utf-8")
        chat_takeover.request_takeover(chat_takeover.handoff_content_sha(path))
        window.active = True
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    validate = _ValidateRecorder(repo, "backend")
    role_records: list[str] = []
    real_record_role = orchestrator._record_role_dispatch

    def recording_role_record(config: DispatchConfig, **kwargs: Any) -> None:
        role_records.append(kwargs["route"])
        real_record_role(config, **kwargs)

    monkeypatch.setattr(orchestrator, "invoke_planner_role", planner)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    monkeypatch.setattr(orchestrator, "_record_role_dispatch", recording_role_record)
    for name in (
        "_repair_handoff_frontmatter_file",
        "_normalize_handoff_next_agent_file",
        "_log_dispatch_completion",
        "_stop_on_auth_failure",
        "_record_failure",
        "_commit_chat_changes_after_role",
        "_commit_for_implementer",
        "_run_work_claim_gate",
    ):
        window.refuse(name)
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo), max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    # Only the chat's handoff was validated, never the planner's own.
    assert len(validate.calls) == 1
    assert validate.calls[0]["content"] == chat_handoff
    assert validate.calls[0]["previous_agent"] == "planner"
    assert validate.calls[0]["kwargs"]["prior_handoff_sha"] == _sha(started_from)
    assert validate.calls[0]["armed"] is False
    assert validate.calls[0]["in_flight_exists"] is False
    # The planner returned a result, so its dispatch is recorded.
    assert role_records == ["planner"]
    assert (repo / HANDOFF).read_text(encoding="utf-8") == chat_handoff
    assert _git(repo, "rev-parse", "HEAD") == base
    assert not any(
        message.startswith("Running post-dispatch validation")
        for _level, message in messages
    )
    assert chat_takeover.CONTROLLER.armed() is False
    assert chat_takeover.CONTROLLER.requested() is False


@pytest.mark.parametrize("chat_writes", ["not a handoff\n", "", None])
def test_invalid_chat_handoff_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_writes: str | None
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, _backend_assignment())

    def stopped_backend(path: Path, **_kwargs: object) -> DispatchResult:
        if chat_writes is None:
            path.unlink()
        else:
            path.write_text(chat_writes, encoding="utf-8")
        chat_takeover.request_takeover(chat_takeover.handoff_content_sha(path))
        raise chat_takeover.ChatTakeover

    pauses: list[str] = []

    def recording_pause(_config: DispatchConfig, **kwargs: Any):
        pauses.append(kwargs["reason"])
        return orchestrator.PauseOutcome(kind="stop")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", stopped_backend)
    monkeypatch.setattr(orchestrator, "_pause_at_dispatcher_pause", recording_pause)
    monkeypatch.setattr(
        orchestrator, "_schedule_pause_recovery", _refuse("pause recovery")
    )
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo), max_cycles=3, log=_collecting_log(messages)
    )

    assert exit_code == 1
    assert len(pauses) == 1
    assert pauses[0].startswith(
        "A chat answer rewrote HANDOFF.md and the result is not a valid handoff:"
    )
    errors = [
        message
        for level, message in messages
        if level == "ERROR" and "The chat's handoff failed validation" in message
    ]
    assert len(errors) == 1
    if chat_writes == "not a handoff\n":
        assert "frontmatter_missing" in errors[0]
    else:
        assert "missing or empty" in errors[0]
    assert chat_takeover.CONTROLLER.armed() is False


@pytest.mark.parametrize("route", ["backend", "planner"])
def test_role_write_after_the_chat_rewrite_pauses_instead_of_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    repo = tmp_path / "repo"
    started_from = _backend_assignment() if route == "backend" else _planner_request()
    _init_repo(repo, started_from)
    chat_handoff = _chat_handoff("planner")
    role_handoff = _planner_request() if route == "backend" else _backend_assignment()

    def role(path: Path, **_kwargs: object) -> DispatchResult:
        # The chat answer rewrites the handoff and requests a takeover, then
        # the role writes HANDOFF.md again before the poll stops it.
        path.write_text(chat_handoff, encoding="utf-8")
        chat_takeover.request_takeover(chat_takeover.handoff_content_sha(path))
        path.write_text(role_handoff, encoding="utf-8")
        if route == "backend":
            raise chat_takeover.ChatTakeover
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    pauses: list[str] = []

    def recording_pause(_config: DispatchConfig, **kwargs: Any):
        pauses.append(kwargs["reason"])
        return orchestrator.PauseOutcome(kind="stop")

    monkeypatch.setattr(orchestrator, f"invoke_{route}_role", role)
    monkeypatch.setattr(
        orchestrator, "validate_handoff", _refuse("validating the role's handoff")
    )
    monkeypatch.setattr(orchestrator, "_pause_at_dispatcher_pause", recording_pause)
    monkeypatch.setattr(
        orchestrator, "_schedule_pause_recovery", _refuse("pause recovery")
    )
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo), max_cycles=3, log=_collecting_log(messages)
    )

    assert exit_code == 1
    assert len(pauses) == 1
    assert pauses[0].startswith(orchestrator.CHAT_TAKEOVER_MISMATCH_PAUSE_PREFIX)
    errors = [
        message
        for level, message in messages
        if level == "ERROR" and "The chat's handoff was not followed" in message
    ]
    assert len(errors) == 1
    assert route in errors[0]
    # The role's later write stays on disk for the operator; nothing followed it.
    assert (repo / HANDOFF).read_text(encoding="utf-8") == role_handoff
    assert chat_takeover.CONTROLLER.requested() is False
    assert chat_takeover.CONTROLLER.armed() is False


def test_stale_request_is_cleared_and_a_file_only_chat_change_does_not_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, _planner_request())
    # A request left from before the dispatch, such as one that landed while
    # the finalizer ran.
    chat_takeover.request_takeover("stale")
    seen: list[tuple[bool, bool, bool]] = []

    def planner(path: Path, **_kwargs: object) -> DispatchResult:
        before = chat_takeover.handoff_content_sha(path)
        # A chat answer that changes only another file never requests one.
        _write(repo / "docs" / "notes.md", "chat note\n")
        changed = chat_takeover.request_takeover_if_handoff_changed(
            handoff_path=path, handoff_sha_before=before
        )
        seen.append(
            (
                chat_takeover.CONTROLLER.armed(),
                chat_takeover.CONTROLLER.requested(),
                changed,
            )
        )
        path.write_text(_backend_assignment(), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    validate = _ValidateRecorder(repo, "backend")
    monkeypatch.setattr(orchestrator, "invoke_planner_role", planner)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo), max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    # Armed, with the stale request cleared, and nothing new requested.
    assert seen == [(True, False, False)]
    # The planner's own handoff went through the normal post-dispatch path.
    assert len(validate.calls) == 1
    assert validate.calls[0]["previous_agent"] == "planner"
    assert validate.calls[0]["armed"] is False
    assert any(
        level == "AGENT" and message == "Running post-dispatch validation for planner..."
        for level, message in messages
    )
    assert not any(
        "A chat answer rewrote HANDOFF.md" in message for _level, message in messages
    )
    assert chat_takeover.CONTROLLER.armed() is False


def test_dispatch_that_raises_another_error_still_disarms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, _planner_request())

    def interrupted_planner(path: Path, **_kwargs: object) -> DispatchResult:
        assert chat_takeover.CONTROLLER.armed() is True
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "invoke_planner_role", interrupted_planner)

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_loop(_config(repo), max_cycles=1, log=lambda *_args: None)

    assert chat_takeover.CONTROLLER.armed() is False
