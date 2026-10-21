"""CHAT-31: the loop waits for a running chat answer at its commit points."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Callable, Iterator

import pytest

from llm_handoff import chat_session, chat_takeover, console_interrupt, ledger
from llm_handoff import orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.chat_answers import ChatContext
from llm_handoff.config import DispatchConfig
from llm_handoff.publish import PublishOutcome
from llm_handoff.validator import ValidationResult


HANDOFF = Path("docs/handoff/HANDOFF.md")
STORY_ID = "CHAT-W1"
WAIT_LINE = ("INFO", "Waiting for the chat answer to finish.")


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


def _backend_assignment() -> str:
    return f"""---
next_agent: backend
reason: "Set the value."
epic_id: CHAT-W
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


def _claim(*, status: str, producer: str, next_agent: str) -> str:
    sha = "a" * 40
    return f"""---
next_agent: {next_agent}
reason: "Backend work complete; audit requested."
epic_id: CHAT-W
story_id: {STORY_ID}
story_title: Change the value
status: {status}
scope_sha: {sha}
close_type: story
producer: {producer}
---

# Handback

## Verification Evidence

- **Commands run:** `python -m pytest tests/test_app.py -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {sha}
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""


def _planner_request() -> str:
    return f"""---
next_agent: planner
reason: "The packet needs a re-scope."
epic_id: CHAT-W
story_id: {STORY_ID}
story_title: Change the value
status: blocked_implementation_failure
producer: backend
---

# Backend Handback

The Work Packet cannot be completed as written.
"""


def _chat_handoff() -> str:
    return f"""---
next_agent: backend
reason: "The operator redirected the story from chat."
epic_id: CHAT-W
story_id: {STORY_ID}
story_title: Change the value
producer: user
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


def _config(repo: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo,
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


def _validation(route: str):
    return lambda *_args, **_kwargs: ValidationResult(
        verdict="YES", warnings=[], errors=[], routing_instruction=route
    )


class _FakeWorker:
    """A chat answer worker whose answers run when the loop waits on it.

    ``start(name, answer)`` marks an answer as running. The loop's first
    ``wait_idle`` call runs every running answer and reports idle.
    """

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self._running: list[tuple[str, Callable[[], None]]] = []

    def start(self, name: str, answer: Callable[[], None] = lambda: None) -> None:
        self._running.append((name, answer))

    def busy(self) -> bool:
        return bool(self._running)

    def wait_idle(self, timeout: float | None = None) -> bool:
        assert timeout == 0.5
        while self._running:
            name, answer = self._running.pop(0)
            answer()
            self.order.append(f"answered:{name}")
        return True


class _FakeChatLine:
    def set_role(self, role: str | None) -> None:
        pass


class _FakeChatInput:
    def __init__(self) -> None:
        self.running = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


class _FileLog:
    """A logger with the ``log_file_path`` the dispatch logger carries."""

    def __init__(self, log_file_path: Path) -> None:
        self.log_file_path = log_file_path
        self.messages: list[tuple[str, str]] = []

    def __call__(self, level: str, message: str) -> None:
        self.messages.append((level, message))


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    ledger.reset_bounce(STORY_ID)
    chat_takeover.CONTROLLER.clear()
    chat_takeover.CONTROLLER.disarm()
    console_interrupt.CONTROLLER.reset()
    chat_session.detach()
    yield
    chat_session.attach_worker(None)
    chat_session.detach()
    console_interrupt.CONTROLLER.reset()
    chat_takeover.CONTROLLER.clear()
    chat_takeover.CONTROLLER.disarm()
    ledger.reset_bounce(STORY_ID)


def _wrap(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    before: Callable[[], None],
    *,
    after: Callable[[], None] | None = None,
    replacement: Callable[..., Any] | None = None,
) -> None:
    real = replacement if replacement is not None else getattr(orchestrator, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        before()
        result = real(*args, **kwargs)
        if after is not None:
            after()
        return result

    monkeypatch.setattr(orchestrator, name, wrapped)


def test_implementer_cycle_waits_before_each_commit_and_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, _backend_assignment())
    order: list[str] = []
    worker = _FakeWorker(order)
    chat_session.attach_worker(worker)

    def backend(path: Path, **_kwargs: object) -> DispatchResult:
        order.append("backend")
        _write(repo / "src" / "app.py", "VALUE = 2\n")
        path.write_text(
            _claim(status="ready_for_review", producer="backend", next_agent="auditor"),
            encoding="utf-8",
        )
        # An answer still running when the role returns.
        worker.start("during-role")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def busy_mark(name: str) -> Callable[[], None]:
        return lambda: order.append(f"{name}:busy={worker.busy()}")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", backend)
    monkeypatch.setattr(orchestrator, "validate_handoff", _validation("auditor"))
    # Each step starts a new answer after it runs, so the next step only
    # sees an idle worker when the loop waited in between.
    _wrap(
        monkeypatch,
        "_commit_chat_changes_after_role",
        busy_mark("commit_chat_changes"),
        after=lambda: worker.start("after-chat-commit"),
    )
    _wrap(
        monkeypatch,
        "_commit_for_implementer",
        busy_mark("commit_for_implementer"),
        after=lambda: worker.start("after-commit"),
        replacement=lambda **_kwargs: None,
    )
    _wrap(
        monkeypatch,
        "_run_work_claim_gate",
        busy_mark("work_claim_gate"),
        replacement=lambda **_kwargs: True,
    )
    log = _FileLog(tmp_path / "dispatch.log")

    exit_code = orchestrator.run_loop(_config(repo), max_cycles=1, log=log)

    assert exit_code == 0
    assert order == [
        "backend",
        "answered:during-role",
        "commit_chat_changes:busy=False",
        "answered:after-chat-commit",
        "commit_for_implementer:busy=False",
        "answered:after-commit",
        "work_claim_gate:busy=False",
    ]
    assert log.messages.count(WAIT_LINE) == 3


def test_answer_that_finishes_after_the_role_returned_is_followed_as_a_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    started_from = _planner_request()
    base = _init_repo(repo, started_from)
    chat_handoff = _chat_handoff()
    order: list[str] = []
    worker = _FakeWorker(order)
    chat_session.attach_worker(worker)

    def chat_answer() -> None:
        path = repo / HANDOFF
        path.write_text(chat_handoff, encoding="utf-8")
        chat_takeover.request_takeover(chat_takeover.handoff_content_sha(path))

    def planner(path: Path, **_kwargs: object) -> DispatchResult:
        order.append("planner")
        path.write_text(_backend_assignment(), encoding="utf-8")
        # The answer is still running when the planner exits 0.
        worker.start("rewrite", chat_answer)
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    validated: list[str] = []

    def validate(path: Path, previous_agent: str, **_kwargs: Any) -> ValidationResult:
        order.append("validate")
        validated.append(path.read_text(encoding="utf-8"))
        return ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="backend"
        )

    def refuse(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("must not run after a chat takeover")

    monkeypatch.setattr(orchestrator, "invoke_planner_role", planner)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    for name in (
        "_commit_chat_changes_after_role",
        "_commit_for_implementer",
        "_run_work_claim_gate",
    ):
        monkeypatch.setattr(orchestrator, name, refuse)
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(repo),
        max_cycles=1,
        log=lambda level, message: messages.append((level, message)),
    )

    assert exit_code == 0
    assert order == ["planner", "answered:rewrite", "validate"]
    # Only the chat's handoff was validated, never the planner's own.
    assert validated == [chat_handoff]
    assert (repo / HANDOFF).read_text(encoding="utf-8") == chat_handoff
    assert _git(repo, "rev-parse", "HEAD") == base
    assert any(
        level == "AGENT" and "A chat answer rewrote HANDOFF.md" in message
        for level, message in messages
    )
    assert messages.count(WAIT_LINE) == 1
    assert chat_takeover.CONTROLLER.requested() is False
    assert chat_takeover.CONTROLLER.armed() is False


def _refuse(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("must not run after a chat takeover")


def _rewriting_answer(repo: Path, chat_handoff: str) -> Callable[[], None]:
    def answer() -> None:
        path = repo / HANDOFF
        path.write_text(chat_handoff, encoding="utf-8")
        chat_takeover.request_takeover(chat_takeover.handoff_content_sha(path))

    return answer


def _recording_validate(
    order: list[str], validated: list[str], route: str
) -> Callable[..., ValidationResult]:
    def validate(path: Path, previous_agent: str, **_kwargs: Any) -> ValidationResult:
        order.append(f"validate:{previous_agent}")
        validated.append(path.read_text(encoding="utf-8"))
        return ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction=route
        )

    return validate


LATE_TAKEOVER_LINE = (
    "AGENT",
    "A chat answer rewrote HANDOFF.md after the backend returned; the "
    "dispatcher drops the rest of the backend's cycle and follows the chat's "
    "handoff once it validates.",
)


def test_answer_started_after_the_role_returned_is_followed_before_the_chat_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, _backend_assignment())
    chat_handoff = _chat_handoff()
    order: list[str] = []
    worker = _FakeWorker(order)
    chat_session.attach_worker(worker)

    def backend(path: Path, **_kwargs: object) -> DispatchResult:
        order.append("backend")
        path.write_text(
            _claim(status="ready_for_review", producer="backend", next_agent="auditor"),
            encoding="utf-8",
        )
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    validated: list[str] = []
    monkeypatch.setattr(orchestrator, "invoke_backend_role", backend)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        _recording_validate(order, validated, "backend"),
    )

    # The answer starts while the post-dispatch normalization runs, after the
    # takeover check that follows the role's return.
    def start_after_the_role() -> None:
        if "backend" in order:
            worker.start("late", _rewriting_answer(repo, chat_handoff))

    _wrap(
        monkeypatch,
        "_normalize_handoff_next_agent_file",
        lambda: order.append("normalize"),
        after=start_after_the_role,
    )
    for name in (
        "_commit_chat_changes_after_role",
        "_commit_for_implementer",
        "_run_work_claim_gate",
    ):
        monkeypatch.setattr(orchestrator, name, _refuse)
    log = _FileLog(tmp_path / "dispatch.log")

    exit_code = orchestrator.run_loop(_config(repo), max_cycles=1, log=log)

    assert exit_code == 0
    # Only the chat's handoff was validated, as the planner's part.
    assert order == [
        "normalize",
        "backend",
        "normalize",
        "answered:late",
        "validate:planner",
    ]
    assert validated == [chat_handoff]
    assert (repo / HANDOFF).read_text(encoding="utf-8") == chat_handoff
    assert _git(repo, "rev-parse", "HEAD") == base
    assert LATE_TAKEOVER_LINE in log.messages
    assert log.messages.count(WAIT_LINE) == 1
    assert chat_takeover.CONTROLLER.requested() is False
    assert not orchestrator._in_flight_state_path(_config(repo)).exists()


def test_answer_started_after_the_implementer_commit_is_followed_before_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, _backend_assignment())
    chat_handoff = _chat_handoff()
    order: list[str] = []
    worker = _FakeWorker(order)
    chat_session.attach_worker(worker)

    def backend(path: Path, **_kwargs: object) -> DispatchResult:
        order.append("backend")
        path.write_text(
            _claim(status="ready_for_review", producer="backend", next_agent="auditor"),
            encoding="utf-8",
        )
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    validated: list[str] = []
    monkeypatch.setattr(orchestrator, "invoke_backend_role", backend)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        _recording_validate(order, validated, "auditor"),
    )
    _wrap(
        monkeypatch,
        "_commit_for_implementer",
        lambda: order.append("commit_for_implementer"),
        after=lambda: worker.start("late", _rewriting_answer(repo, chat_handoff)),
        replacement=lambda **_kwargs: None,
    )
    monkeypatch.setattr(orchestrator, "_run_work_claim_gate", _refuse)
    log = _FileLog(tmp_path / "dispatch.log")

    exit_code = orchestrator.run_loop(_config(repo), max_cycles=1, log=log)

    assert exit_code == 0
    assert order == [
        "backend",
        "commit_for_implementer",
        "validate:backend",
        "answered:late",
        "validate:planner",
    ]
    assert validated[-1] == chat_handoff
    assert (repo / HANDOFF).read_text(encoding="utf-8") == chat_handoff
    assert LATE_TAKEOVER_LINE in log.messages
    assert chat_takeover.CONTROLLER.requested() is False


def test_answer_finished_at_the_publish_wait_is_followed_instead_of_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        _claim(status="ready_for_review", producer="backend", next_agent="auditor"),
    )
    config = _config(repo, publish={"mode": "pr"})
    chat_handoff = _chat_handoff()
    order: list[str] = []
    worker = _FakeWorker(order)
    chat_session.attach_worker(worker)

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object):
        order.append(subagent_name)
        config.handoff_full_path.write_text(
            _claim(status="verified_pass", producer="auditor", next_agent="planner"),
            encoding="utf-8",
        )
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def gate(**_kwargs: Any) -> bool:
        order.append("work_claim_gate")
        worker.start("late", _rewriting_answer(repo, chat_handoff))
        return False

    validated: list[str] = []
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        _recording_validate(order, validated, "backend"),
    )
    monkeypatch.setattr(orchestrator, "_run_work_claim_gate", gate)
    monkeypatch.setattr(orchestrator, "publish_story", _refuse)
    monkeypatch.setattr(
        orchestrator, "leave_merged_story_branch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "resume_paused_publish", lambda **_kwargs: PublishOutcome()
    )
    monkeypatch.setattr(
        orchestrator, "resume_interrupted_publish", lambda **_kwargs: PublishOutcome()
    )
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: messages.append((level, message)),
    )

    assert exit_code == 0
    assert order == [
        "auditor",
        "validate:auditor (audit)",
        "work_claim_gate",
        "answered:late",
        "validate:planner",
    ]
    assert validated[-1] == chat_handoff
    assert (repo / HANDOFF).read_text(encoding="utf-8") == chat_handoff
    assert chat_takeover.CONTROLLER.requested() is False


def test_auditor_publish_waits_for_the_answer_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        _claim(status="ready_for_review", producer="backend", next_agent="auditor"),
    )
    config = _config(repo, publish={"mode": "pr"})
    order: list[str] = []
    worker = _FakeWorker(order)
    chat_session.attach_worker(worker)
    chat_input = _FakeChatInput()
    chat_session.attach(_FakeChatLine(), chat_input)  # type: ignore[arg-type]

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object):
        order.append(subagent_name)
        config.handoff_full_path.write_text(
            _claim(status="verified_pass", producer="auditor", next_agent="planner"),
            encoding="utf-8",
        )
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    real_gate = orchestrator._run_work_claim_gate

    def gate(**kwargs: Any) -> bool:
        order.append(f"work_claim_gate:busy={worker.busy()}")
        result = real_gate(**kwargs)
        # An answer that starts once the gate passed.
        worker.start("after-gate")
        return result

    def fake_publish(**_kwargs: object) -> PublishOutcome:
        order.append(
            f"publish_story:busy={worker.busy()}:role={chat_session.SESSION.role}"
        )
        return PublishOutcome()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(orchestrator, "validate_handoff", _validation("planner"))
    monkeypatch.setattr(orchestrator, "_run_work_claim_gate", gate)
    monkeypatch.setattr(orchestrator, "publish_story", fake_publish)
    monkeypatch.setattr(
        orchestrator, "leave_merged_story_branch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "resume_paused_publish", lambda **_kwargs: PublishOutcome()
    )
    monkeypatch.setattr(
        orchestrator, "resume_interrupted_publish", lambda **_kwargs: PublishOutcome()
    )
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: messages.append((level, message)),
    )

    assert exit_code == 0
    assert order == [
        "auditor",
        "work_claim_gate:busy=False",
        "answered:after-gate",
        "publish_story:busy=False:role=None",
    ]
    assert messages.count(WAIT_LINE) == 1


def test_role_start_passes_the_chat_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, _backend_assignment())
    chat_session.attach(_FakeChatLine(), _FakeChatInput())  # type: ignore[arg-type]
    seen: list[ChatContext | None] = []

    def backend(path: Path, **_kwargs: object) -> DispatchResult:
        seen.append(chat_session.SESSION.context)
        path.write_text(_planner_request(), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", backend)
    monkeypatch.setattr(orchestrator, "validate_handoff", _validation("planner"))
    monkeypatch.setattr(orchestrator, "_run_work_claim_gate", lambda **_kw: True)
    config = _config(repo)
    log = _FileLog(tmp_path / "dispatch.log")

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=log)

    assert exit_code == 0
    assert seen == [
        ChatContext(
            role="backend",
            provider=config.agents["backend"].provider,
            story_id=STORY_ID,
            log_path=tmp_path / "dispatch.log",
            agent_config=config.agents["backend"],
        )
    ]


def test_chat_context_for_a_route_without_agent_config(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents.pop("frontend", None)

    context = orchestrator._chat_context_for(
        config, "frontend", "not a handoff", lambda _level, _message: None
    )

    assert context == ChatContext(
        role="frontend",
        provider="",
        story_id=None,
        log_path=None,
        agent_config=None,
    )
