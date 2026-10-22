"""HUMAN-1: spot an operator edit of HANDOFF.md and keep the previous handoff."""

from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from llm_handoff import chat_session, orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import DispatchConfig
from llm_handoff.operator_prompt import HandoffChanged, OperatorAction
from llm_handoff.validator import ValidationResult


HANDOFF_RELATIVE_PATH = Path("docs/handoff/HANDOFF.md")
SNAPSHOT_RELATIVE_PATH = Path("logs/dispatch/operator-edit/previous-handoff.md")
EDIT_LOG = ("INFO", "Operator edited HANDOFF.md.")

ASSIGN_BACKEND = """---
next_agent: backend
reason: "Implement story one."
epic_id: EDIT
story_id: EDIT-1
story_title: Story one
producer: planner
---

## Next Step

- **backend:** Implement story one.
"""

BACKEND_ESCALATES = """---
next_agent: user
reason: "Need a decision on story one."
epic_id: EDIT
story_id: EDIT-1
story_title: Story one
status: escalate_to_user
producer: backend
---

## Question

Which API should story one use?
"""

# The same handback with an unquoted colon in `reason`: the dispatcher's
# frontmatter repair rewrites it.
BACKEND_ESCALATES_REPAIRABLE = BACKEND_ESCALATES.replace(
    'reason: "Need a decision on story one."',
    "reason: Need a decision: story one",
)

USER_ESCALATION = """---
next_agent: user
reason: "Human decision required."
status: escalate_to_user
producer: planner
---

## Question

Which story should run next?
"""

OPERATOR_TO_PLANNER = """---
next_agent: planner
reason: "The operator finished the edits."
producer: user
---

## Next Step

- **planner:** Pick up from the operator's edits.
"""

OPERATOR_TO_PLANNER_AGAIN = OPERATOR_TO_PLANNER.replace(
    "finished the edits", "finished the edits again"
)

FREE_FORM = "I made the edits.\n"
FREE_FORM_AGAIN = "I made the edits, and the tests pass.\n"


# --- fixtures -------------------------------------------------------------


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


def _init_repo(repo_root: Path, handoff: str, *, ignore_logs: bool = True) -> Path:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.name", "llm-handoff tests")
    _git(repo_root, "config", "user.email", "tests@example.invalid")
    _git(repo_root, "config", "core.autocrlf", "false")
    _write(repo_root / HANDOFF_RELATIVE_PATH, handoff)
    _write(repo_root / "PROJECT_STATE.md", "# PROJECT STATE\n")
    _write(repo_root / "AGENTS.md", "dispatch test fixture\n")
    _write(repo_root / ".gitignore", "logs/\n" if ignore_logs else "*.tmp\n")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "initial")
    return repo_root


def _config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "poll_interval_seconds": 0,
        "pause_recovery": "off",
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        "enforce_work_packet": False,
        "work_packet_opt_out_reason": "Tests cover routing without the gate.",
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _snapshot(repo_root: Path) -> Path:
    return repo_root / SNAPSHOT_RELATIVE_PATH


def _handoff(repo_root: Path) -> Path:
    return repo_root / HANDOFF_RELATIVE_PATH


class _Logs:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.entries.append((level, text))

    def edits(self) -> int:
        return self.entries.count(EDIT_LOG)

    def warnings(self) -> list[str]:
        return [text for level, text in self.entries if level == "WARN"]


def _no_input_stream() -> io.StringIO:
    raise AssertionError("these runs must not read operator input")


@pytest.fixture(autouse=True)
def _isolated_terminal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", _no_input_stream)
    chat_session.clear_role_dispatches()
    yield
    chat_session.clear_role_dispatches()


def _dispatch_result() -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


class _Roles:
    """Stub roles that write scripted handoffs; validation always passes."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        planner_writes: list[str] | None = None,
        backend_writes: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        planner_queue = list(planner_writes or [])
        backend_queue = list(backend_writes or [])

        def fake_planner(path: Path, **_kwargs: Any) -> DispatchResult:
            self.calls.append("planner")
            if planner_queue:
                path.write_text(planner_queue.pop(0), encoding="utf-8")
            return _dispatch_result()

        def fake_backend(path: Path, **_kwargs: Any) -> DispatchResult:
            self.calls.append("backend")
            if backend_queue:
                path.write_text(backend_queue.pop(0), encoding="utf-8")
            return _dispatch_result()

        def fake_support(name: str, _prompt: str, **_kwargs: Any) -> SubagentResult:
            self.calls.append(name)
            return SubagentResult(
                stdout="VALID: NO\n  ROUTING: FAIL - diagnosis fixture\n",
                stderr="",
                exit_code=0,
                elapsed_seconds=0.01,
            )

        monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
        monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
        monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
        monkeypatch.setattr(
            orchestrator,
            "validate_handoff",
            lambda *_a, **_k: ValidationResult(verdict="YES", warnings=[], errors=[]),
        )


def _watch(repo: Path, **overrides: Any) -> tuple[orchestrator._OperatorEditWatch, DispatchConfig]:
    return orchestrator._OperatorEditWatch(), _config(repo, **overrides)


# --- the watch itself -----------------------------------------------------


def test_first_observation_is_a_baseline_not_an_edit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    watch, config = _watch(repo)
    logs = _Logs()

    assert watch.observe(USER_ESCALATION, config, logs) is None

    assert logs.entries == []
    assert watch.valid_content == USER_ESCALATION
    assert not _snapshot(repo).exists()


def test_unchanged_content_is_never_an_edit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    watch, config = _watch(repo)
    logs = _Logs()

    for _ in range(3):
        assert watch.observe(USER_ESCALATION, config, logs) is None

    assert logs.entries == []
    assert not _snapshot(repo).exists()


def test_an_edit_saves_the_exact_previous_valid_handoff(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    watch, config = _watch(repo)
    logs = _Logs()
    watch.observe(USER_ESCALATION, config, logs)

    edit = watch.observe(FREE_FORM, config, logs)

    assert edit == orchestrator.OperatorEdit(
        content=FREE_FORM, previous_valid=USER_ESCALATION, saved=True
    )
    assert watch.last_edit == edit
    assert logs.edits() == 1
    assert (
        "INFO",
        "Saved the previous valid handoff to "
        "logs/dispatch/operator-edit/previous-handoff.md.",
    ) in logs.entries
    assert _snapshot(repo).read_bytes() == USER_ESCALATION.encode("utf-8")
    # Logged once: rereading the same edited version is not a new edit.
    assert watch.observe(FREE_FORM, config, logs) is None
    assert logs.edits() == 1


def test_successive_edits_keep_the_most_recent_valid_context(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    watch, config = _watch(repo)
    logs = _Logs()
    watch.observe(USER_ESCALATION, config, logs)

    # A valid edit becomes the next edit's predecessor.
    watch.observe(OPERATOR_TO_PLANNER, config, logs)
    assert _snapshot(repo).read_text(encoding="utf-8") == USER_ESCALATION
    watch.observe(FREE_FORM, config, logs)
    assert _snapshot(repo).read_text(encoding="utf-8") == OPERATOR_TO_PLANNER

    # An invalid edit never replaces the last valid handoff.
    edit = watch.observe(FREE_FORM_AGAIN, config, logs)
    assert edit is not None
    assert edit.previous_valid == OPERATOR_TO_PLANNER
    assert _snapshot(repo).read_text(encoding="utf-8") == OPERATOR_TO_PLANNER

    edit = watch.observe(OPERATOR_TO_PLANNER_AGAIN, config, logs)
    assert edit is not None
    assert edit.previous_valid == OPERATOR_TO_PLANNER
    assert watch.valid_content == OPERATOR_TO_PLANNER_AGAIN
    assert logs.edits() == 4


def test_role_and_dispatcher_writes_advance_the_baseline_without_an_edit(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo", ASSIGN_BACKEND)
    watch, config = _watch(repo)
    logs = _Logs()
    watch.observe(ASSIGN_BACKEND, config, logs)

    # Invalid role output advances the observed version, not the valid one.
    watch.accept(FREE_FORM, config)
    assert watch.observe(FREE_FORM, config, logs) is None
    assert watch.valid_content == ASSIGN_BACKEND

    # A valid role or dispatcher write becomes the valid baseline.
    watch.accept(BACKEND_ESCALATES, config)
    assert watch.observe(BACKEND_ESCALATES, config, logs) is None
    assert logs.entries == []
    assert not _snapshot(repo).exists()

    edit = watch.observe(OPERATOR_TO_PLANNER, config, logs)
    assert edit is not None
    assert edit.previous_valid == BACKEND_ESCALATES
    assert _snapshot(repo).read_text(encoding="utf-8") == BACKEND_ESCALATES


def test_an_edit_without_a_valid_predecessor_reports_its_absence(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo", FREE_FORM)
    stale = "stale snapshot from an earlier edit\n"
    _write(_snapshot(repo), stale)
    watch, config = _watch(repo)
    logs = _Logs()
    watch.observe(FREE_FORM, config, logs)

    edit = watch.observe(FREE_FORM_AGAIN, config, logs)

    assert edit == orchestrator.OperatorEdit(
        content=FREE_FORM_AGAIN, previous_valid=None, saved=False
    )
    assert logs.edits() == 1
    assert any(
        "No valid HANDOFF.md was observed before this edit" in text
        and "is from an earlier edit, not this one" in text
        for text in logs.warnings()
    )
    assert not any(text.startswith("Saved the previous") for _l, text in logs.entries)
    # The unrelated file on disk is left alone, not passed off as this edit's.
    assert _snapshot(repo).read_text(encoding="utf-8") == stale


def test_dry_run_neither_creates_nor_replaces_the_snapshot(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    watch, config = _watch(repo, dry_run=True)
    logs = _Logs()
    watch.observe(USER_ESCALATION, config, logs)

    edit = watch.observe(FREE_FORM, config, logs)
    assert edit is not None and edit.saved is False
    assert not _snapshot(repo).exists()

    _write(_snapshot(repo), "earlier\n")
    watch.observe(OPERATOR_TO_PLANNER, config, logs)
    assert _snapshot(repo).read_text(encoding="utf-8") == "earlier\n"
    assert logs.edits() == 2
    assert (
        "INFO",
        "[DRY RUN] Would save the previous valid handoff to "
        "logs/dispatch/operator-edit/previous-handoff.md.",
    ) in logs.entries


def test_a_write_failure_is_reported_not_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    watch, config = _watch(repo)
    logs = _Logs()
    watch.observe(USER_ESCALATION, config, logs)

    def failing_writer(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(orchestrator, "_write_carry_over_payload", failing_writer)

    edit = watch.observe(FREE_FORM, config, logs)

    assert edit is not None and edit.saved is False
    assert logs.edits() == 1
    assert any(
        text.startswith(
            "Could not save the previous valid handoff to "
            "logs/dispatch/operator-edit/previous-handoff.md: "
        )
        and "No space left on device" in text
        for text in logs.warnings()
    )
    assert not any(text.startswith("Saved the previous") for _l, text in logs.entries)
    # The edit still becomes the observed version: it is not logged again.
    assert watch.observe(FREE_FORM, config, logs) is None


def test_a_link_found_during_the_write_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    watch, config = _watch(repo)
    logs = _Logs()
    watch.observe(USER_ESCALATION, config, logs)
    attempts: list[tuple[str, object]] = []

    def linked_writer(_root: Path, _payload: object, name: str, **kwargs: object) -> None:
        attempts.append((name, kwargs.get("parts")))
        raise orchestrator._CarryOverLinkError(0, "link", name)

    monkeypatch.setattr(orchestrator, "_write_carry_over_payload", linked_writer)

    edit = watch.observe(FREE_FORM, config, logs)

    assert edit is not None and edit.saved is False
    assert attempts == [("previous-handoff.md", ("logs", "dispatch", "operator-edit"))]
    assert any("symlink or junction" in text for text in logs.warnings())
    assert not any(text.startswith("Saved the previous") for _l, text in logs.entries)


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


@pytest.mark.parametrize(
    "linked_parts",
    [("logs",), ("logs", "dispatch"), ("logs", "dispatch", "operator-edit")],
    ids=["logs", "dispatch", "operator-edit"],
)
def test_a_linked_ancestor_directory_is_not_written_through(
    tmp_path: Path, linked_parts: tuple[str, ...]
) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo.joinpath(*linked_parts)
    link.parent.mkdir(parents=True, exist_ok=True)
    _link_directory(link, outside)
    try:
        watch, config = _watch(repo)
        logs = _Logs()
        watch.observe(USER_ESCALATION, config, logs)

        edit = watch.observe(FREE_FORM, config, logs)

        assert edit is not None and edit.saved is False
        assert list(outside.rglob("*")) == []
        assert any("symlink or junction" in text for text in logs.warnings())
        assert not any(
            text.startswith("Saved the previous") for _l, text in logs.entries
        )
    finally:
        _unlink_directory(link)


def test_a_linked_destination_file_is_not_written_through(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    outside = tmp_path / "outside.md"
    outside.write_text("outside the repository\n", encoding="utf-8")
    destination = _snapshot(repo)
    destination.parent.mkdir(parents=True)
    try:
        os.symlink(outside, destination)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a file symlink here: {exc}")
    watch, config = _watch(repo)
    logs = _Logs()
    watch.observe(USER_ESCALATION, config, logs)

    edit = watch.observe(FREE_FORM, config, logs)

    assert edit is not None and edit.saved is False
    assert outside.read_text(encoding="utf-8") == "outside the repository\n"
    assert any("symlink or junction" in text for text in logs.warnings())


def test_a_snapshot_git_does_not_ignore_is_not_written(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION, ignore_logs=False)
    watch, config = _watch(repo)
    logs = _Logs()
    watch.observe(USER_ESCALATION, config, logs)

    edit = watch.observe(FREE_FORM, config, logs)

    assert edit is not None and edit.saved is False
    assert not _snapshot(repo).exists()
    assert any("Git does not ignore it" in text for text in logs.warnings())


# --- run_loop: between completed cycles -----------------------------------


def test_an_edit_between_cycles_is_spotted_and_still_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", ASSIGN_BACKEND)
    roles = _Roles(
        monkeypatch,
        backend_writes=[BACKEND_ESCALATES],
        planner_writes=[USER_ESCALATION],
    )
    logs = _Logs()

    def log(level: str, text: str) -> None:
        logs(level, text)
        if text == "--- End of cycle 1 ---":
            # The operator replaces the backend's handback after the cycle.
            _write(_handoff(repo), OPERATOR_TO_PLANNER)

    exit_code = orchestrator.run_loop(_config(repo), max_cycles=2, log=log)

    assert exit_code == 0
    # The valid operator edit routes as today.
    assert roles.calls == ["backend", "planner"]
    assert logs.edits() == 1
    edit_index = logs.entries.index(EDIT_LOG)
    assert edit_index > logs.entries.index(("INFO", "--- Cycle 2 ---"))
    # The backend's handback, the last valid handoff before the edit.
    assert _snapshot(repo).read_text(encoding="utf-8") == BACKEND_ESCALATES


def test_role_output_and_dispatcher_repairs_are_not_operator_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", ASSIGN_BACKEND)
    _Roles(monkeypatch, backend_writes=[BACKEND_ESCALATES_REPAIRABLE])
    logs = _Logs()

    orchestrator.run_loop(_config(repo), max_cycles=2, log=logs)

    assert any("Auto-repaired HANDOFF YAML frontmatter" in text for _l, text in logs.entries)
    assert logs.edits() == 0
    assert not _snapshot(repo).exists()


def test_invalid_role_output_is_not_an_operator_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", ASSIGN_BACKEND)
    _Roles(monkeypatch, backend_writes=[FREE_FORM])
    logs = _Logs()

    orchestrator.run_loop(_config(repo), max_cycles=2, log=logs)

    assert ("INFO", "--- Cycle 2 ---") in logs.entries
    assert logs.edits() == 0
    assert not _snapshot(repo).exists()


def test_a_run_that_starts_on_an_edited_file_takes_it_as_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", FREE_FORM)
    _Roles(monkeypatch)
    logs = _Logs()

    orchestrator.run_loop(_config(repo), max_cycles=1, log=logs)

    assert logs.edits() == 0
    assert not _snapshot(repo).exists()


# --- run_loop: after a pause ----------------------------------------------


def _edit_on_second_sleep(
    monkeypatch: pytest.MonkeyPatch, repo: Path, content: str
) -> list[float]:
    """Polls once with no change, then the operator edits the handoff."""

    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            _write(_handoff(repo), content)

    monkeypatch.setattr(orchestrator.time, "sleep", sleep)
    return sleeps


def test_an_edit_during_a_polling_pause_is_spotted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    roles = _Roles(monkeypatch, planner_writes=[USER_ESCALATION])
    sleeps = _edit_on_second_sleep(monkeypatch, repo, OPERATOR_TO_PLANNER)
    logs = _Logs()

    exit_code = orchestrator.run_loop(
        _config(repo, poll_interval_seconds=1), max_cycles=2, log=logs
    )

    assert exit_code == 0
    assert len(sleeps) == 2
    assert roles.calls == ["planner"]
    # Logged when the pause ends, not again when cycle 2 rereads the file.
    assert logs.edits() == 1
    edit_index = logs.entries.index(EDIT_LOG)
    assert edit_index < logs.entries.index(("INFO", "--- Cycle 2 ---"))
    assert _snapshot(repo).read_bytes() == USER_ESCALATION.encode("utf-8")


def test_an_edit_during_an_interactive_pause_is_spotted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    roles = _Roles(monkeypatch, planner_writes=[USER_ESCALATION])
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", lambda: io.StringIO())
    waits: list[str] = []

    def operator_edits(*_args: Any, **_kwargs: Any) -> HandoffChanged:
        waits.append("wait")
        _write(_handoff(repo), OPERATOR_TO_PLANNER)
        return HandoffChanged()

    monkeypatch.setattr(orchestrator, "wait_for_operator_or_handoff_change", operator_edits)
    logs = _Logs()

    exit_code = orchestrator.run_loop(
        _config(repo, poll_interval_seconds=1), max_cycles=2, log=logs
    )

    assert exit_code == 0
    assert waits == ["wait"]
    assert roles.calls == ["planner"]
    assert logs.edits() == 1
    assert _snapshot(repo).read_text(encoding="utf-8") == USER_ESCALATION


def test_a_prompt_answer_without_an_edit_is_not_an_operator_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    roles = _Roles(monkeypatch, planner_writes=[OPERATOR_TO_PLANNER])
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", lambda: io.StringIO())
    monkeypatch.setattr(
        orchestrator,
        "wait_for_operator_or_handoff_change",
        lambda *_a, **_k: OperatorAction(kind="planner"),
    )
    logs = _Logs()

    orchestrator.run_loop(_config(repo, poll_interval_seconds=1), max_cycles=2, log=logs)

    assert roles.calls == ["planner"]
    assert logs.edits() == 0
    assert not _snapshot(repo).exists()


def test_dry_run_spots_a_pause_edit_without_a_snapshot_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo", USER_ESCALATION)
    roles = _Roles(monkeypatch)
    _edit_on_second_sleep(monkeypatch, repo, OPERATOR_TO_PLANNER)
    logs = _Logs()

    exit_code = orchestrator.run_loop(
        _config(repo, poll_interval_seconds=1, dry_run=True), log=logs
    )

    assert exit_code == 0
    assert roles.calls == []
    assert logs.edits() == 1
    assert not _snapshot(repo).exists()
    assert ("INFO", "[DRY RUN] Single cycle complete. Exiting.") in logs.entries
