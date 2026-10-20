"""RECOVERY-9: the operator-confirmed fallback for an implementer with no record."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
import unicodedata

import pytest

from llm_handoff.agents import DispatchResult
from llm_handoff.config import CARRY_OVER_STATE_PATH, IN_FLIGHT_STATE_PATH, DispatchConfig
from llm_handoff.dispatcher_commit import PENDING_COMMIT_SHA
import llm_handoff.ledger as ledger
import llm_handoff.no_record_recovery as no_record_recovery
from llm_handoff.work_claim import ImplementerLeftovers, carry_over_key


HANDOFF_RELATIVE_PATH = Path("docs/handoff/HANDOFF.md")
STORY_ID = "EPIC-1-S1"
PYTHON = sys.executable
PASS_COMMAND = f'"{PYTHON}" -c "import sys; sys.exit(0)"'
QUESTION = f"Treat these as backend's interrupted {STORY_ID} work? [y/N]"


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


def _assignment(
    *,
    next_agent: str = "backend",
    story_id: str | None = STORY_ID,
    files_in_scope: str = "src/app.py, tests/test_app.py",
    work_packet: bool = True,
) -> str:
    story_line = f"story_id: {story_id}\n" if story_id else ""
    packet = (
        f"""
## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** {files_in_scope}
- **Files out of bounds:** src/api.py
- **Context:** Keep the module import-free.
- **Verification command:** {PASS_COMMAND}
- **Expected next route:** auditor
"""
        if work_packet
        else "\n## Next Step\n\nSet VALUE to 2.\n"
    )
    return f"""---
next_agent: {next_agent}
reason: "Implement the value change."
epic_id: EPIC-1
{story_line}story_title: Change the value
producer: planner
---

## Task Assignment
{packet}"""


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


def _init_repo(repo_root: Path, *, handoff: str | None = None) -> None:
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
    _write(repo_root / ".gitignore", "logs/\n")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "initial")


def _commit(repo_root: Path, subject: str) -> str:
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", subject)
    return _git(repo_root, "rev-parse", "HEAD")


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


def _no_input_stream() -> io.StringIO:
    raise AssertionError("the fallback must not read operator input here")


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


def _recover(
    orchestrator: Any, config: DispatchConfig, messages: list[tuple[str, str]]
) -> ImplementerLeftovers | None:
    return orchestrator._recover_unrecorded_implementer(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def reset_story_bounce():
    ledger.reset_bounce(STORY_ID)
    yield
    ledger.reset_bounce(STORY_ID)


# ---------------------------------------------------------------------------
# The question and its answers
# ---------------------------------------------------------------------------


def test_a_confirmed_in_scope_file_is_returned_with_its_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    leftovers = _recover(orchestrator, _config(tmp_path), messages)

    assert leftovers == ImplementerLeftovers(
        story_key=f"story:{STORY_ID}",
        fingerprints=(("src/app.py", _sha256(tmp_path / "src" / "app.py")),),
    )
    assert [level for level, _message in messages] == ["INFO", "INFO", "INFO"]
    assert "src/app.py" in messages[0][1]
    assert messages[1] == ("INFO", QUESTION)
    assert "operator confirmed" in messages[2][1]
    assert "carry over" in messages[2][1]
    assert not (tmp_path / CARRY_OVER_STATE_PATH).exists()


def test_a_confirmed_file_is_carried_over_and_committed_by_the_next_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    calls: list[Path] = []

    def finishing_backend(path: Path, **_kwargs: object) -> DispatchResult:
        calls.append(path)
        path.write_text(_pending_claim(), encoding="utf-8")
        return _result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", finishing_backend)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(_config(tmp_path), max_cycles=1, log=_collecting_log(messages))

    assert ("INFO", QUESTION) in messages
    assert (
        "INFO",
        f"Carrying over uncommitted work from the previous {STORY_ID} "
        "dispatch: src/app.py.",
    ) in messages
    assert len(calls) == 1
    assert "src/app.py" in _git(tmp_path, "show", "--name-only", "--format=", "HEAD")


@pytest.mark.parametrize(
    ("interactive", "operator_input", "interactive_pause"),
    [
        pytest.param(False, None, True, id="no_tty"),
        pytest.param(True, "n\n", True, id="tty_declines"),
        pytest.param(True, "", True, id="tty_eof"),
        pytest.param(True, None, False, id="interactive_pause_off"),
    ],
)
def test_an_unconfirmed_fallback_leaves_operator_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
    operator_input: str | None,
    interactive_pause: bool,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    status_before = _git(tmp_path, "status", "--porcelain")
    orchestrator = _orchestrator()
    _set_terminal(
        orchestrator, monkeypatch, interactive=interactive, operator_input=operator_input
    )
    messages: list[tuple[str, str]] = []

    leftovers = _recover(
        orchestrator, _config(tmp_path, interactive_pause=interactive_pause), messages
    )

    assert leftovers is None
    asked = ("INFO", QUESTION) in messages
    assert asked == (interactive and interactive_pause)
    warnings = [message for level, message in messages if level == "WARN"]
    assert len(warnings) == 1
    assert "src/app.py" in warnings[0]
    assert "stay operator edits" in warnings[0]
    assert _git(tmp_path, "status", "--porcelain") == status_before
    assert not (tmp_path / CARRY_OVER_STATE_PATH).exists()
    assert not (tmp_path / IN_FLIGHT_STATE_PATH).exists()


def test_a_dry_run_logs_the_files_and_asks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    leftovers = _recover(orchestrator, _config(tmp_path, dry_run=True), messages)

    assert leftovers is None
    assert len(messages) == 1
    level, message = messages[0]
    assert level == "INFO"
    assert "backend" in message
    assert STORY_ID in message
    assert "src/app.py" in message
    assert message.endswith("Not applied in dry run.")


def test_a_control_character_in_a_path_is_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path, handoff=_assignment(files_in_scope="src/"))
    orchestrator = _orchestrator()
    # Filesystems reject such paths, so Git's listing is stubbed.
    evil = "src/evil\x1b]0;x\x07\n.py"
    monkeypatch.setattr(no_record_recovery, "git_dirty_files", lambda _root: (evil,))
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    leftovers = _recover(orchestrator, _config(tmp_path), messages)

    assert leftovers is None
    # Found, question, decline.
    assert len(messages) == 3
    for _level, message in messages:
        assert not any(unicodedata.category(char) == "Cc" for char in message)
    assert "src/evil\\x1b]0;x\\x07\\x0a.py" in messages[0][1]
    assert messages[1] == ("INFO", QUESTION)
    assert "src/evil\\x1b]0;x\\x07\\x0a.py" in messages[2][1]


# ---------------------------------------------------------------------------
# Which files are offered
# ---------------------------------------------------------------------------


def test_an_out_of_scope_file_is_not_offered_and_is_listed_as_an_operator_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / "src" / "api.py", "API = 'operator'\n")
    _write(tmp_path / "notes.txt", "operator notes\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    leftovers = _recover(orchestrator, _config(tmp_path), messages)

    assert leftovers is not None
    assert [path for path, _fingerprint in leftovers.fingerprints] == ["src/app.py"]
    level, found = messages[0]
    assert level == "INFO"
    offered, _separator, left_out = found.partition("Not offered")
    assert "src/app.py" in offered
    assert "src/api.py" not in offered
    assert "notes.txt" not in offered
    assert "src/api.py" in left_out
    assert "notes.txt" in left_out
    assert "stay operator edits" in left_out


def test_only_out_of_scope_files_ask_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "api.py", "API = 'operator'\n")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _assignment() + "\nOperator note.\n")
    _write(tmp_path / "logs" / "dispatch" / "dispatch.log", "log line\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    assert _recover(orchestrator, _config(tmp_path), messages) is None
    assert messages == []


def test_a_protected_in_scope_file_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    pass\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    leftovers = _recover(
        orchestrator, _config(tmp_path, protected_paths=("src/app.py",)), messages
    )

    assert leftovers is not None
    assert [path for path, _fingerprint in leftovers.fingerprints] == [
        "tests/test_app.py"
    ]
    offered, _separator, left_out = messages[0][1].partition("Not offered")
    assert "src/app.py" not in offered
    assert "src/app.py" in left_out


def test_a_protected_only_candidate_asks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    leftovers = _recover(
        orchestrator, _config(tmp_path, protected_paths=("src/**",)), messages
    )

    assert leftovers is None
    assert messages == []


def test_a_file_changed_after_the_story_commit_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    # A commit before the story's last commit does not matter.
    _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    assert 1\n")
    _commit(tmp_path, "unrelated earlier work")
    _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    assert 2\n")
    story_commit = _commit(tmp_path, f"{STORY_ID} Change the value")
    _write(tmp_path / "src" / "app.py", "VALUE = 3\n")
    _commit(tmp_path, "Later operator commit")
    _write(tmp_path / "src" / "app.py", "VALUE = 4\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    assert 3\n")

    assert no_record_recovery.story_last_commit(tmp_path, STORY_ID) == story_commit
    candidates = no_record_recovery.find_no_record_candidates(
        tmp_path,
        _assignment(),
        HANDOFF_RELATIVE_PATH,
        (),
        protected_match=_orchestrator()._protected_path_matches,
    )

    assert candidates is not None
    assert candidates.offered == ("tests/test_app.py",)
    assert candidates.left_out == ("src/app.py",)


def test_the_story_commit_subject_must_match_the_story_id_exactly(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    exact = _commit_empty(tmp_path, STORY_ID)
    _commit_empty(tmp_path, f"{STORY_ID}0 Another story")
    _commit_empty(tmp_path, f"Revert {STORY_ID}")

    assert no_record_recovery.story_last_commit(tmp_path, STORY_ID) == exact
    assert no_record_recovery.story_last_commit(tmp_path, "EPIC-9-S9") == ""


def _commit_empty(repo_root: Path, subject: str) -> str:
    _git(repo_root, "commit", "-q", "--allow-empty", "-m", subject)
    return _git(repo_root, "rev-parse", "HEAD")


def test_without_a_story_id_head_is_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = _assignment(story_id=None)
    _init_repo(tmp_path, handoff=handoff)
    _write(tmp_path / "src" / "app.py", "VALUE = 3\n")
    _commit(tmp_path, "Later operator commit")
    _write(tmp_path / "src" / "app.py", "VALUE = 4\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    leftovers = _recover(orchestrator, _config(tmp_path), messages)

    assert leftovers is not None
    assert leftovers.story_key == carry_over_key(handoff)
    assert leftovers.story_key.startswith("work-packet:")
    assert [path for path, _fingerprint in leftovers.fingerprints] == ["src/app.py"]
    assert any(
        level == "INFO" and "the Work Packet" in message and "[y/N]" in message
        for level, message in messages
    )


def test_a_failing_git_call_offers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    monkeypatch.setattr(no_record_recovery, "git_dirty_files", lambda _root: None)
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    assert _recover(orchestrator, _config(tmp_path), messages) is None
    assert messages == []

    monkeypatch.undo()
    monkeypatch.setattr(no_record_recovery, "_git_nul_entries", lambda *_args: None)
    assert (
        no_record_recovery.find_no_record_candidates(
            tmp_path,
            _assignment(),
            HANDOFF_RELATIVE_PATH,
            (),
            protected_match=orchestrator._protected_path_matches,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Which handoffs qualify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("next_agent", ["planner", "auditor"])
def test_a_handoff_not_routed_to_an_implementer_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, next_agent: str
) -> None:
    _init_repo(tmp_path, handoff=_assignment(next_agent=next_agent))
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    assert _recover(orchestrator, _config(tmp_path), messages) is None
    assert messages == []


def test_a_frontend_handoff_qualifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path, handoff=_assignment(next_agent="frontend"))
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    assert _recover(orchestrator, _config(tmp_path), messages) is None
    assert (
        "INFO",
        f"Treat these as frontend's interrupted {STORY_ID} work? [y/N]",
    ) in messages


def test_a_handoff_without_a_work_packet_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path, handoff=_assignment(work_packet=False))
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input=None)
    messages: list[tuple[str, str]] = []

    assert _recover(orchestrator, _config(tmp_path), messages) is None
    assert messages == []


# ---------------------------------------------------------------------------
# An existing record skips the fallback
# ---------------------------------------------------------------------------


def _run_until_dispatch(
    tmp_path: Path,
    orchestrator: Any,
    monkeypatch: pytest.MonkeyPatch,
    messages: list[tuple[str, str]],
) -> None:
    def interrupted_backend(_path: Path, **_kwargs: object) -> DispatchResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "invoke_backend_role", interrupted_backend)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_loop(
            _config(tmp_path), max_cycles=1, log=_collecting_log(messages)
        )


def test_an_existing_in_flight_record_skips_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / IN_FLIGHT_STATE_PATH, "{not json")
    orchestrator = _orchestrator()
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="y\n")
    messages: list[tuple[str, str]] = []

    _run_until_dispatch(tmp_path, orchestrator, monkeypatch, messages)

    assert any(
        level == "WARN" and "not valid" in message for level, message in messages
    )
    assert not any("[y/N]" in message for _level, message in messages)
    assert not any("Treat these" in message for _level, message in messages)
    assert not any(
        "Carrying over uncommitted work" in message for _level, message in messages
    )


def test_an_existing_carry_over_record_skips_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(
        tmp_path / CARRY_OVER_STATE_PATH,
        json.dumps(
            {
                "version": 1,
                "story_key": f"story:{STORY_ID}",
                "fingerprints": {
                    "src/app.py": _sha256(tmp_path / "src" / "app.py")
                },
            }
        ),
    )
    orchestrator = _orchestrator()
    # The operator declines the carry-over record; no second question follows.
    _set_terminal(orchestrator, monkeypatch, interactive=True, operator_input="n\n")
    messages: list[tuple[str, str]] = []

    _run_until_dispatch(tmp_path, orchestrator, monkeypatch, messages)

    questions = [message for _level, message in messages if "[y/N]" in message]
    assert questions == [f"Carry these over as {STORY_ID}'s work? [y/N]"]
    assert any(
        level == "WARN" and "Discarded the carry-over record" in message
        for level, message in messages
    )
    assert not any("Treat these" in message for _level, message in messages)
    assert not any("Did not treat" in message for _level, message in messages)
