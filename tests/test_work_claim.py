from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import unicodedata

import pytest

from llm_handoff.agents import DispatchResult, SubagentResult
from llm_handoff.antigravity_permissions import antigravity_command_grant
from llm_handoff.config import DispatchConfig, ScannerConfig, load_dispatch_config
from llm_handoff.dispatcher_commit import (
    PENDING_COMMIT_SHA,
    commit_implementer_changes,
    stamp_commit_sha,
)
import llm_handoff.ledger as ledger
from llm_handoff.router import parse_handoff_frontmatter_text, route
from llm_handoff.stop_request import request_stop
from llm_handoff.validator import (
    ValidationResult,
    validate_handoff_frontmatter,
    validate_handoff_text,
)
from llm_handoff.work_claim import (
    CommandRun,
    ImplementerLeftovers,
    PreDispatchState,
    ScannerContext,
    WorkPacket,
    WorkClaimResult,
    WorkPacketError,
    bounce_route_for,
    build_bounce_handoff,
    build_work_claim_env,
    capture_implementer_leftovers,
    capture_pre_dispatch_state,
    carry_over_key,
    evidence_commit_value,
    extract_work_packet_section,
    find_scanner_executable,
    git_dirty_files,
    git_head,
    git_head_and_dirty_files,
    load_implementer_leftovers,
    parse_work_packet,
    path_is_in_scope,
    path_is_out_of_bounds,
    path_matches_entry,
    prune_implementer_leftovers,
    render_scanner_command,
    run_scanner,
    run_shell_command,
    save_implementer_leftovers,
    shell_quote,
    verification_command_allowed,
    verify_work_claim,
    work_packet_errors,
    working_tree_fingerprint,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
HANDOFF_RELATIVE_PATH = Path("docs/handoff/HANDOFF.md")
STORY_ID = "EPIC-1-S1"
PYTHON = sys.executable
PASS_COMMAND = f'"{PYTHON}" -c "import sys; sys.exit(0)"'
FAIL_COMMAND = f'"{PYTHON}" -c "import sys; print(\'verification boom\'); sys.exit(3)"'
SLEEP_COMMAND = f'"{PYTHON}" -c "import time; time.sleep(30)"'


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


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
    verification_command: str = PASS_COMMAND,
    *,
    files_in_scope: str = "src/app.py, tests/test_app.py",
    files_out_of_bounds: str = "src/api.py",
    producer: str = "planner",
) -> str:
    return f"""---
next_agent: backend
reason: "Implement the value change."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
producer: {producer}
---

## Task Assignment

**Agent:** backend

## Work Packet

- **Objective:** Set VALUE to 2.
- **Files in scope:** {files_in_scope}
- **Files out of bounds:** {files_out_of_bounds}
- **Context:** Keep the module import-free.
- **Verification command:** {verification_command}
- **Expected next route:** auditor
"""


def _assignment_without_packet() -> str:
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

Set VALUE to 2 in src/app.py.
"""


def _claim(
    scope_sha: str | None,
    *,
    status: str = "ready_for_review",
    producer: str = "backend",
    next_agent: str = "auditor",
) -> str:
    scope_lines = ""
    if scope_sha is not None:
        scope_lines = f"scope_sha: {scope_sha}\nclose_type: story\n"
    evidence_sha = scope_sha or "0123456789abcdef0123456789abcdef01234567"
    return f"""---
next_agent: {next_agent}
reason: "Backend work complete; audit requested."
epic_id: EPIC-1
story_id: {STORY_ID}
story_title: Change the value
status: {status}
{scope_lines}producer: {producer}
---

# Backend Handback

**Agent:** backend

## Completed Work

- Set VALUE to 2 in src/app.py.

## Verification Evidence

- **Commands run:** `python -m pytest tests/test_app.py -q`
- **Output summary:** exit 0
- **Commit SHA verified:** {evidence_sha}
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""


# Process launches dominate this file's run time, most of all on Windows. Each
# starting repository is built once per worker and copied into each test.
_TEMPLATE_ROOT: Path | None = None
_TEMPLATE_HEADS: dict[str, str] = {}


@pytest.fixture(autouse=True, scope="session")
def _repo_template_root(tmp_path_factory: pytest.TempPathFactory) -> None:
    global _TEMPLATE_ROOT
    _TEMPLATE_ROOT = tmp_path_factory.mktemp("work-claim-templates")


def _init_repo(repo_root: Path, handoff_content: str | None = None) -> str:
    if repo_root.exists() and any(repo_root.iterdir()):
        return _build_repo(repo_root, handoff_content)
    assert _TEMPLATE_ROOT is not None
    key = hashlib.sha256(repr(handoff_content).encode("utf-8")).hexdigest()[:16]
    template = _TEMPLATE_ROOT / key
    if key not in _TEMPLATE_HEADS:
        _TEMPLATE_HEADS[key] = _build_repo(template, handoff_content)
    shutil.copytree(template, repo_root, dirs_exist_ok=True)
    # A copy gets new file creation times, which git's index compares, so
    # plumbing such as `git diff-index` would see every file as changed.
    _git(repo_root, "update-index", "-q", "--refresh")
    return _TEMPLATE_HEADS[key]


def _build_repo(repo_root: Path, handoff_content: str | None) -> str:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.name", "llm-handoff tests")
    _git(repo_root, "config", "user.email", "tests@example.invalid")
    _git(repo_root, "config", "core.autocrlf", "false")
    _write(repo_root / "src" / "app.py", "VALUE = 1\n")
    _write(repo_root / "src" / "api.py", "API = 1\n")
    _write(repo_root / "tests" / "test_app.py", "def test_value():\n    assert True\n")
    _write(
        repo_root / HANDOFF_RELATIVE_PATH,
        handoff_content if handoff_content is not None else _assignment(),
    )
    _write(repo_root / "PROJECT_STATE.md", "# PROJECT STATE\n")
    _write(repo_root / "AGENTS.md", "dispatch test fixture\n")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "initial")
    return _git(repo_root, "rev-parse", "HEAD")


def _commit_all(repo_root: Path, message: str) -> str:
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", message)
    return _git(repo_root, "rev-parse", "HEAD")


def _commit_paths(repo_root: Path, message: str, *paths: str) -> str:
    _git(repo_root, "add", "--", *paths)
    _git(repo_root, "commit", "-q", "-m", message)
    return _git(repo_root, "rev-parse", "HEAD")


def _verify(
    repo_root: Path,
    *,
    assignment: str,
    claim: str,
    state: PreDispatchState,
    timeout_seconds: int = 60,
):
    return verify_work_claim(
        repo_root=repo_root,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=assignment,
        claim_content=claim,
        pre_dispatch_state=state,
        verification_timeout_seconds=timeout_seconds,
    )


def _outcomes(result) -> dict[str, str]:
    return {check.name: check.outcome for check in result.checks}


def _dispatch_config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "poll_interval_seconds": 0,
        # Pauses exit here; pause recovery is covered in test_pause_recovery.py.
        "pause_recovery": "off",
        "max_consecutive_failures": 3,
        "enforce_work_packet": True,
        "work_packet_verification_timeout_seconds": 60,
        # These tests stub the single auditor; the security reviewer fan-out
        # is covered in tests/test_audit_review.py.
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _dispatch_result() -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


@pytest.fixture
def reset_story_bounce():
    ledger.reset_bounce(STORY_ID)
    yield
    ledger.reset_bounce(STORY_ID)


# ---------------------------------------------------------------------------
# Work Packet parsing
# ---------------------------------------------------------------------------


def test_parse_work_packet_extracts_scope_and_command() -> None:
    packet = parse_work_packet(
        _assignment(
            "python -m pytest tests/test_app.py -q",
            files_in_scope="`src/app.py`, tests/",
            files_out_of_bounds="src/api.py, frontend/",
        )
    )

    assert packet.files_in_scope == ("src/app.py", "tests/")
    assert packet.files_out_of_bounds == ("src/api.py", "frontend/")
    assert packet.verification_commands == ("python -m pytest tests/test_app.py -q",)
    assert packet.section_text.startswith("## Work Packet\n")
    assert "- **Expected next route:** auditor" in packet.section_text


def test_parse_work_packet_treats_none_as_empty_out_of_bounds() -> None:
    packet = parse_work_packet(_assignment(files_out_of_bounds="none"))

    assert packet.files_out_of_bounds == ()


def test_parse_work_packet_supports_nested_lists_and_multiple_commands() -> None:
    packet = parse_work_packet(
        """---
next_agent: backend
reason: "Nested packet."
producer: planner
---

## Work Packet

- **Objective:** Nested lists.
- **Files in scope:**
  - src/app.py
  - `tests/test_app.py`
- **Files out of bounds**: none
- **Context:** none
- **Verification command:**
  - `python -m pytest tests -q`
  - python -m ruff check src
- **Expected next route:** auditor

## Notes

Not part of the packet.
"""
    )

    assert packet.files_in_scope == ("src/app.py", "tests/test_app.py")
    assert packet.files_out_of_bounds == ()
    assert packet.verification_commands == (
        "python -m pytest tests -q",
        "python -m ruff check src",
    )
    assert "## Notes" not in packet.section_text


def test_parse_work_packet_ignores_heading_inside_code_fence() -> None:
    content = """---
next_agent: backend
reason: "Fenced example first."
producer: planner
---

Template reminder:

```markdown
## Work Packet

- **Files in scope:** TEMPLATE
```

## Work Packet

- **Objective:** Real packet.
- **Files in scope:** src/app.py
- **Files out of bounds:** none
- **Context:** none
- **Verification command:** python -c "pass"
- **Expected next route:** auditor
"""

    packet = parse_work_packet(content)

    assert packet.files_in_scope == ("src/app.py",)


def test_parse_work_packet_raises_missing_by_name() -> None:
    with pytest.raises(WorkPacketError) as exc_info:
        parse_work_packet(_assignment_without_packet())

    assert exc_info.value.prefix == "work_packet_missing"
    assert str(exc_info.value).startswith("work_packet_missing:")


def test_parse_work_packet_raises_unparseable_when_fields_are_unusable() -> None:
    content = """---
next_agent: backend
reason: "Half a packet."
producer: planner
---

## Work Packet

- **Objective:** Half a packet.
- **Files out of bounds:** none
- **Verification command:** none
"""

    with pytest.raises(WorkPacketError) as exc_info:
        parse_work_packet(content)

    assert exc_info.value.prefix == "work_packet_unparseable"
    assert "missing Files in scope" in exc_info.value.detail
    assert "empty Verification command" in exc_info.value.detail


@pytest.mark.parametrize(
    ("path", "entry", "expected"),
    [
        ("src/app.py", "src/app.py", True),
        ("src/app.py", "src/", True),
        ("src/app.py", "src", True),
        ("srcx/app.py", "src", False),
        ("src/app.py", "tests/", False),
        ("tests/test_app.py", "tests/test_*.py", True),
        ("tests/helpers/x.py", "tests/test_*.py", False),
    ],
)
def test_path_matches_entry(path: str, entry: str, expected: bool) -> None:
    assert path_matches_entry(path, entry) is expected


# ---------------------------------------------------------------------------
# verify_work_claim against a real git repository
# ---------------------------------------------------------------------------


def test_verify_work_claim_passes_for_in_scope_commit_at_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _claim(head))

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=state
    )

    assert result.errors == ()
    assert result.passed
    assert result.changed_files == ("src/app.py",)
    assert _outcomes(result) == {
        "work_packet": "PASS",
        "scope_sha": "PASS",
        "diff_scope": "PASS",
        "worktree": "PASS",
        "verification": "PASS",
    }
    assert result.verification_runs[0].exit_code == 0


def test_verify_work_claim_rejects_pre_existing_commit_as_scope_sha(
    tmp_path: Path,
) -> None:
    initial = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(initial), state=state
    )

    assert result.failed_prefixes == ("scope_sha_not_head",)
    assert any(error.startswith("scope_sha_not_head:") for error in result.errors)
    assert _outcomes(result)["scope_sha"] == "FAIL"


def test_verify_work_claim_rejects_short_scope_sha_that_is_not_head(
    tmp_path: Path,
) -> None:
    initial = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim(initial[:7]),
        state=state,
    )

    assert result.failed_prefixes == ("scope_sha_not_head",)


def test_verify_work_claim_accepts_short_scope_sha_at_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head[:7]), state=state
    )

    assert result.passed


def test_verify_work_claim_rejects_missing_scope_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(None), state=state
    )

    assert result.failed_prefixes == ("scope_sha_not_head",)
    assert "omitted scope_sha" in result.errors[0]


def test_verify_work_claim_tolerates_handoff_only_commit_above_scope_sha(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    work_sha = _commit_all(tmp_path, "set value")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _claim(work_sha))
    head = _commit_all(tmp_path, "handoff")
    assert head != work_sha

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(work_sha), state=state
    )

    assert result.passed
    scope_check = next(check for check in result.checks if check.name == "scope_sha")
    assert "HEAD~1" in scope_check.detail
    assert result.changed_files == ("docs/handoff/HANDOFF.md", "src/app.py")


def _git_launches(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    import llm_handoff.work_claim as work_claim_module

    launches: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(args, *run_args, **run_kwargs):
        if args and args[0] == "git":
            launches.append(list(args[3:]))
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(work_claim_module.subprocess, "run", recording_run)
    return launches


def test_verify_work_claim_runs_each_git_lookup_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _claim(head))
    launches = _git_launches(monkeypatch)

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=state
    )

    assert result.passed
    # scope_sha and the evidence both name HEAD, which `git status` reported,
    # so neither needs a rev-parse and HEAD's files are listed once.
    assert [args[0] for args in launches] == ["status", "diff", "diff-tree"]


def test_verify_work_claim_walks_handoff_only_commits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    work_sha = _commit_all(tmp_path, "set value")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _claim(work_sha))
    _commit_all(tmp_path, "handoff")
    launches = _git_launches(monkeypatch)

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(work_sha), state=state
    )

    assert result.passed
    assert [args[0] for args in launches] == [
        "status",
        "diff",
        "rev-parse",
        "diff-tree",
        "rev-parse",
        "diff-tree",
    ]


def test_verify_work_claim_skips_git_diff_when_head_did_not_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    launches = _git_launches(monkeypatch)

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(None), state=state
    )

    assert result.changed_files == ()
    assert "diff" not in [args[0] for args in launches]
    assert _outcomes(result)["diff_scope"] == "PASS"


def test_verify_work_claim_rejects_handoff_plus_source_commit_above_scope_sha(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    work_sha = _commit_all(tmp_path, "set value")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _claim(work_sha))
    _write(tmp_path / "src" / "app.py", "VALUE = 3\n")
    _commit_all(tmp_path, "handoff plus more work")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(work_sha), state=state
    )

    assert result.failed_prefixes == ("scope_sha_not_head",)


def test_verify_work_claim_rejects_out_of_bounds_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    head = _commit_all(tmp_path, "touch api")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=state
    )

    assert result.failed_prefixes == ("scope_violation",)
    assert result.errors == (
        "scope_violation: changed files listed under Files out of bounds: src/api.py.",
    )


def test_verify_work_claim_rejects_change_outside_files_in_scope(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "other.py", "OTHER = 1\n")
    head = _commit_all(tmp_path, "add other")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=state
    )

    assert result.failed_prefixes == ("scope_violation",)
    assert result.errors == (
        "scope_violation: changed files outside Files in scope: src/other.py.",
    )


def test_verify_work_claim_allows_handoff_file_in_diff(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, "placeholder\n")
    head = _commit_all(tmp_path, "work plus handoff")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=state
    )

    assert result.passed
    assert result.changed_files == ("docs/handoff/HANDOFF.md", "src/app.py")


def test_verify_work_claim_rejects_failing_verification_command(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path, _assignment(FAIL_COMMAND))
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path,
        assignment=_assignment(FAIL_COMMAND),
        claim=_claim(head),
        state=state,
    )

    assert result.failed_prefixes == ("verification_failed",)
    assert "exited 3" in result.errors[0]
    assert len(result.verification_runs) == 1
    assert result.verification_runs[0].exit_code == 3
    assert "verification boom" in result.verification_runs[0].output_tail


def test_verify_work_claim_times_out_verification_command(tmp_path: Path) -> None:
    _init_repo(tmp_path, _assignment(SLEEP_COMMAND))
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    started = time.monotonic()
    result = _verify(
        tmp_path,
        assignment=_assignment(SLEEP_COMMAND),
        claim=_claim(head),
        state=state,
        timeout_seconds=1,
    )

    assert time.monotonic() - started < 20
    assert result.failed_prefixes == ("verification_failed",)
    assert "timed out after 1s" in result.errors[0]
    assert result.verification_runs[0].timed_out is True
    assert result.verification_runs[0].exit_code is None


def test_verify_work_claim_stops_at_first_failing_command(tmp_path: Path) -> None:
    assignment = _assignment(FAIL_COMMAND).replace(
        f"- **Verification command:** {FAIL_COMMAND}",
        f"- **Verification command:**\n  - {FAIL_COMMAND}\n  - {PASS_COMMAND}",
    )
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)

    assert result.failed_prefixes == ("verification_failed",)
    assert len(result.verification_runs) == 1


def test_verify_work_claim_rejects_missing_work_packet(tmp_path: Path) -> None:
    _init_repo(tmp_path, _assignment_without_packet())
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path,
        assignment=_assignment_without_packet(),
        claim=_claim(head),
        state=state,
    )

    assert result.failed_prefixes == ("work_packet_missing",)
    assert _outcomes(result)["diff_scope"] == "SKIP"
    assert _outcomes(result)["verification"] == "SKIP"
    assert result.verification_runs == ()
    assert bounce_route_for(result, implementer="backend").next_agent == "planner"


def test_verify_work_claim_rejects_unparseable_work_packet(tmp_path: Path) -> None:
    assignment = _assignment(files_in_scope="none")
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)

    assert result.failed_prefixes == ("work_packet_unparseable",)
    assert "empty Files in scope" in result.errors[0]


def test_verify_work_claim_rejects_new_uncommitted_changes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "api.py", "API = 'dirty before dispatch'\n")
    state = capture_pre_dispatch_state(tmp_path)
    assert state.dirty_files == ("src/api.py",)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_paths(tmp_path, "set value", "src/app.py")
    _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    assert 1\n")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _claim(head))

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=state
    )

    assert result.failed_prefixes == ("uncommitted_changes",)
    assert "outside docs/handoff/HANDOFF.md: tests/test_app.py;" in result.errors[0]
    assert "src/api.py" not in result.errors[0]


def test_verify_work_claim_reports_all_failed_checks_in_one_pass(
    tmp_path: Path,
) -> None:
    initial = _init_repo(tmp_path, _assignment(FAIL_COMMAND))
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    _commit_all(tmp_path, "touch api")

    result = _verify(
        tmp_path,
        assignment=_assignment(FAIL_COMMAND),
        claim=_claim(initial),
        state=state,
    )

    assert result.failed_prefixes == (
        "scope_sha_not_head",
        "scope_violation",
        "verification_failed",
    )


def test_verify_work_claim_escalates_when_git_state_is_unavailable(
    tmp_path: Path,
) -> None:
    state = PreDispatchState(head_sha=None, dirty_files=())

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim("a" * 40),
        state=state,
    )

    assert result.failed_prefixes == ("git_state_unavailable",)
    assert result.verification_runs == ()
    assert bounce_route_for(result, implementer="backend").next_agent == "user"
    assert bounce_route_for(result, implementer="backend").status == "escalate_to_user"

    # The escalation needs no verdict permission, so it validates either way.
    bounce = build_bounce_handoff(
        claim_content=_claim("a" * 40),
        result=result,
        implementer="backend",
        route=bounce_route_for(result, implementer="backend"),
        bounce_count=None,
    )
    frontmatter = parse_handoff_frontmatter_text(bounce)
    assert frontmatter is not None
    assert frontmatter.producer == "validator"
    for dispatcher_authored in (True, False):
        assert (
            validate_handoff_frontmatter(
                frontmatter, dispatcher_authored=dispatcher_authored
            ).errors
            == []
        )


# ---------------------------------------------------------------------------
# Bounce handoff
# ---------------------------------------------------------------------------


def test_build_bounce_handoff_routes_back_to_implementer_with_evidence(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path, _assignment(FAIL_COMMAND))
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    head = _commit_all(tmp_path, "touch api")
    claim = _claim(head)
    result = _verify(
        tmp_path,
        assignment=_assignment(FAIL_COMMAND),
        claim=claim,
        state=state,
    )
    bounce_route = bounce_route_for(result, implementer="backend")

    bounce = build_bounce_handoff(
        claim_content=claim,
        result=result,
        implementer="backend",
        route=bounce_route,
        bounce_count=1,
    )

    frontmatter = parse_handoff_frontmatter_text(bounce)
    assert frontmatter is not None
    assert frontmatter.next_agent == "backend"
    assert frontmatter.status == "verified_fail"
    assert frontmatter.producer == "validator"
    assert frontmatter.bounce_count == 1
    assert frontmatter.scope_sha == head
    assert frontmatter.story_id == STORY_ID
    assert frontmatter.close_type == "story"
    assert (
        'reason: "Dispatcher verification failed: scope_violation, '
        'verification_failed."' in bounce
    )
    decision = route(bounce)
    assert decision.route == "backend"
    assert decision.confidence == "HIGH"

    body = bounce.split("---\n", 2)[2]
    assert body.startswith("\n## Dispatcher Verification\n")
    assert "- **Failed checks:** scope_violation, verification_failed" in body
    assert f"- **Current HEAD:** `{head}`" in body
    assert "- **Changed files since dispatch:** `src/api.py`" in body
    assert "FAIL `diff_scope`" in body
    assert "FAIL `verification`" in body
    assert "verification boom" in body
    assert body.index("## Dispatcher Verification") < body.index("## Work Packet")
    assert "- **Files out of bounds:** src/api.py" in body
    assert "## Prior Handback" in body
    assert "### Verification Evidence" in body
    assert "\n## Verification Evidence" not in body
    carried = parse_work_packet(bounce)
    assert carried.verification_commands == (FAIL_COMMAND,)

    # The dispatcher wrote this verified_fail, so it validates only when the
    # caller says so; the same frontmatter from an agent is rejected.
    trusted = validate_handoff_frontmatter(
        frontmatter, git_cwd=tmp_path, dispatcher_authored=True
    )
    assert trusted.errors == []
    untrusted = validate_handoff_frontmatter(frontmatter, git_cwd=tmp_path)
    assert [
        error
        for error in untrusted.errors
        if error.startswith("frontmatter_status_producer_rule:")
    ] == [
        "frontmatter_status_producer_rule: producer `validator` cannot emit "
        "status `verified_fail`; only the auditor approves or fails work, and "
        "only dispatcher-written handoffs carry a validator verdict."
    ]


def test_build_bounce_handoff_routes_missing_packet_to_planner(tmp_path: Path) -> None:
    _init_repo(tmp_path, _assignment_without_packet())
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    claim = _claim(head)
    result = _verify(
        tmp_path,
        assignment=_assignment_without_packet(),
        claim=claim,
        state=state,
    )
    bounce_route = bounce_route_for(result, implementer="backend")

    bounce = build_bounce_handoff(
        claim_content=claim,
        result=result,
        implementer="backend",
        route=bounce_route,
        bounce_count=1,
    )

    frontmatter = parse_handoff_frontmatter_text(bounce)
    assert frontmatter is not None
    assert frontmatter.next_agent == "planner"
    assert frontmatter.status == "verified_fail"
    assert route(bounce).route == "planner"
    assert "did not carry a usable `## Work Packet`" in bounce
    assert extract_work_packet_section(bounce) is None
    assert "## Prior Handback" in bounce


# ---------------------------------------------------------------------------
# Validator: packet required for implementer routes when enforced
# ---------------------------------------------------------------------------


def test_validate_handoff_text_requires_work_packet_only_when_enforced() -> None:
    content = _assignment_without_packet()

    relaxed = validate_handoff_text(content, "planner")
    enforced = validate_handoff_text(content, "planner", require_work_packet=True)

    assert not any(error.startswith("work_packet") for error in relaxed.errors)
    assert any(
        warning.startswith("work_packet_missing:") for warning in relaxed.warnings
    )
    assert enforced.verdict == "NO"
    assert any(error.startswith("work_packet_missing:") for error in enforced.errors)


def test_validate_handoff_text_enforced_accepts_parseable_packet() -> None:
    result = validate_handoff_text(_assignment(), "planner", require_work_packet=True)

    assert not any(error.startswith("work_packet") for error in result.errors)


def test_validate_handoff_text_enforced_requires_packet_on_audit_fail_route() -> None:
    content = """---
next_agent: backend
reason: "Defect found; fix the value."
story_id: EPIC-1-S1
status: verified_fail
scope_sha: 0123456789abcdef0123456789abcdef01234567
producer: auditor
---

# Audit Report

**Agent:** auditor

## Verification Evidence

- **Commands run:** `python -m pytest -q`
- **Output summary:** exit 1
- **Commit SHA verified:** 0123456789abcdef0123456789abcdef01234567
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""

    result = validate_handoff_text(content, "auditor (audit)", require_work_packet=True)

    assert any(error.startswith("work_packet_missing:") for error in result.errors)


def test_validate_handoff_text_enforced_ignores_packet_on_non_implementer_status() -> (
    None
):
    content = """---
next_agent: backend
reason: "Story approved; backend owns the next story."
story_id: EPIC-1-S1
status: verified_pass
scope_sha: 0123456789abcdef0123456789abcdef01234567
close_type: story
producer: auditor
---

# Audit Report

**Agent:** auditor

## Verification Evidence

- **Commands run:** `python -m pytest -q`
- **Output summary:** exit 0
- **Commit SHA verified:** 0123456789abcdef0123456789abcdef01234567
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""

    result = validate_handoff_text(content, "auditor (audit)", require_work_packet=True)

    assert not any(error.startswith("work_packet") for error in result.errors)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


GATE_OPT_OUT_REASON = "Tests cover the dispatch loop without the gate."


def test_dispatch_config_work_claim_gate_is_on_by_default(tmp_path: Path) -> None:
    config = DispatchConfig(repo_root=tmp_path)

    assert config.enforce_work_packet is True
    assert config.work_packet_opt_out_reason is None
    assert config.work_packet_verification_timeout_seconds == 600


def test_load_dispatch_config_without_gate_key_turns_gate_on(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("auto_push: false\n", encoding="utf-8")

    config = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert config.enforce_work_packet is True
    assert config.work_packet_opt_out_reason is None


def test_dispatch_config_rejects_gate_opt_out_without_reason(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        DispatchConfig(repo_root=tmp_path, enforce_work_packet=False)

    message = str(exc_info.value)
    assert "enforce_work_packet" in message
    assert "work_packet_opt_out_reason" in message


def test_dispatch_config_rejects_whitespace_only_gate_opt_out_reason(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        DispatchConfig(
            repo_root=tmp_path,
            enforce_work_packet=False,
            work_packet_opt_out_reason="   \t ",
        )

    message = str(exc_info.value)
    assert "enforce_work_packet" in message
    assert "work_packet_opt_out_reason" in message
    assert "non-empty reason" in message


def test_dispatch_config_rejects_gate_opt_out_reason_while_gate_is_on(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="work_packet_opt_out_reason only applies when the work-claim gate is off",
    ):
        DispatchConfig(
            repo_root=tmp_path,
            work_packet_opt_out_reason=GATE_OPT_OUT_REASON,
        )


def test_load_dispatch_config_reads_valid_gate_opt_out(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "enforce_work_packet: false\n"
        'work_packet_opt_out_reason: "  Planner prompts do not emit packets yet.  "\n',
        encoding="utf-8",
    )

    config = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert config.enforce_work_packet is False
    assert config.work_packet_opt_out_reason == (
        "Planner prompts do not emit packets yet."
    )


def test_dispatcher_commit_mode_error_wins_over_gate_opt_out(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        DispatchConfig(
            repo_root=tmp_path,
            commit_mode="dispatcher",
            enforce_work_packet=False,
            work_packet_opt_out_reason=GATE_OPT_OUT_REASON,
        )

    assert (
        "commit_mode: dispatcher requires enforce_work_packet: true; the "
        "dispatcher only commits files the Work Packet puts in scope, "
        "and the work-claim gate checks what it committed."
    ) in str(exc_info.value)


def test_load_dispatch_config_reads_work_claim_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "enforce_work_packet: true\nwork_packet_verification_timeout_seconds: 30\n",
        encoding="utf-8",
    )

    config = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert config.enforce_work_packet is True
    assert config.work_packet_verification_timeout_seconds == 30


def test_dispatch_config_rejects_non_positive_verification_timeout(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="work_packet_verification_timeout_seconds"):
        DispatchConfig(repo_root=tmp_path, work_packet_verification_timeout_seconds=0)


@pytest.mark.parametrize(
    "config_path",
    [
        REPO_ROOT / "dispatch_config.example.yaml",
        REPO_ROOT / "examples" / "reference-workflow" / "dispatch_config.yaml",
    ],
)
def test_shipped_configs_document_gate_on(config_path: Path) -> None:
    content = config_path.read_text(encoding="utf-8")
    config = load_dispatch_config(repo_root=config_path.parent, config_path=config_path)

    assert "\nenforce_work_packet: true\n" in content
    assert "#   enforce_work_packet: false" in content
    assert '#   work_packet_opt_out_reason: "<why>"' in content
    assert config.enforce_work_packet is True
    assert config.work_packet_opt_out_reason is None


def test_docs_describe_work_claim_gate() -> None:
    configuration = (REPO_ROOT / "CONFIGURATION.md").read_text(encoding="utf-8")
    architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "enforce_work_packet" in configuration
    assert "work_packet_verification_timeout_seconds" in configuration
    assert "## Work-Claim Gate" in architecture
    assert "llm_handoff.work_claim" in architecture
    for prefix in (
        "scope_sha_not_head",
        "scope_violation",
        "verification_failed",
        "uncommitted_changes",
        "work_packet_missing",
    ):
        assert prefix in architecture
    assert "agent-written" in architecture
    assert "enforce_work_packet" in security


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def _load_orchestrator():
    return importlib.import_module("llm_handoff.orchestrator")


def _collecting_log(messages: list[tuple[str, str]]):
    return lambda level, message: messages.append((level, message))


def test_run_loop_bounces_failed_work_claim_instead_of_dispatching_auditor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path)
    handoff_path = config.handoff_full_path
    dispatches: list[str] = []
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        _write(tmp_path / "src" / "api.py", "API = 2\n")
        head = _commit_all(tmp_path, "touch api")
        path.write_text(_claim(head), encoding="utf-8")
        return _dispatch_result()

    def fake_support(
        subagent_name: str, prompt: str, **_kwargs: object
    ) -> SubagentResult:
        dispatches.append(subagent_name)
        raise AssertionError("auditor must not be dispatched for a failed claim")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)

    exit_code = orchestrator.run_loop(
        config, max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert dispatches == ["backend"]
    bounced = parse_handoff_frontmatter_text(handoff_path.read_text(encoding="utf-8"))
    assert bounced is not None
    assert bounced.next_agent == "backend"
    assert bounced.status == "verified_fail"
    assert bounced.producer == "validator"
    assert bounced.bounce_count == 1
    assert ledger.bounce_count(STORY_ID) == 1
    assert "## Dispatcher Verification" in handoff_path.read_text(encoding="utf-8")
    assert any(
        level == "ERROR" and message.startswith("scope_violation:")
        for level, message in messages
    )
    assert any(
        message.startswith("Work-claim gate FAILED for backend")
        for _level, message in messages
    )
    assert ("INFO", f"Bounce count for story_id={STORY_ID} is now 1.") in messages


def test_run_loop_passes_work_claim_and_dispatches_auditor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path)
    dispatches: list[str] = []
    messages: list[tuple[str, str]] = []
    real_validate = orchestrator.validate_handoff

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        head = _commit_all(tmp_path, "set value")
        path.write_text(_claim(head), encoding="utf-8")
        return _dispatch_result()

    def fake_support(
        subagent_name: str, prompt: str, **_kwargs: object
    ) -> SubagentResult:
        dispatches.append(subagent_name)
        config.handoff_full_path.write_text(
            """---
next_agent: planner
reason: "Audit approved; scope next story."
producer: auditor
---

# Audit Report

**Agent:** auditor

Audit complete with verification.
""",
            encoding="utf-8",
        )
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def validate(path: Path, previous_agent: str, **kwargs: object) -> ValidationResult:
        if previous_agent == "backend":
            return real_validate(path, previous_agent, **kwargs)
        return ValidationResult(
            verdict="YES",
            warnings=[],
            errors=[],
            routing_instruction="planner",
        )

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)

    exit_code = orchestrator.run_loop(
        config, max_cycles=2, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert dispatches == ["backend", "auditor"]
    assert ledger.bounce_count(STORY_ID) == 0
    assert ("AGENT", "Work-claim gate PASSED for backend.") in messages
    assert any(
        message.startswith("work-claim: verification command") and "exited 0" in message
        for _level, message in messages
    )
    assert any("Work-claim gate:    ON" in message for _level, message in messages)


def test_run_loop_leaves_claims_alone_when_gate_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path,
        enforce_work_packet=False,
        work_packet_opt_out_reason=GATE_OPT_OUT_REASON,
    )
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "api.py", "API = 2\n")
        head = _commit_all(tmp_path, "touch api")
        path.write_text(_claim(head), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    exit_code = orchestrator.run_loop(
        config, max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    unchanged = parse_handoff_frontmatter_text(
        config.handoff_full_path.read_text(encoding="utf-8")
    )
    assert unchanged is not None
    assert unchanged.next_agent == "auditor"
    assert unchanged.status == "ready_for_review"
    assert ledger.bounce_count(STORY_ID) == 0
    assert not any("work-claim" in message.lower() for _level, message in messages[8:])
    assert any("Work-claim gate:    OFF" in message for _level, message in messages)


def test_run_loop_bounces_missing_work_packet_to_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path, _assignment_without_packet())
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path)
    dispatches: list[str] = []
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        head = _commit_all(tmp_path, "set value")
        path.write_text(_claim(head), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    exit_code = orchestrator.run_loop(
        config, max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert dispatches == ["backend"]
    bounced = parse_handoff_frontmatter_text(
        config.handoff_full_path.read_text(encoding="utf-8")
    )
    assert bounced is not None
    assert bounced.next_agent == "planner"
    assert bounced.status == "verified_fail"
    assert bounced.producer == "validator"
    assert any(
        level == "ERROR" and message.startswith("work_packet_missing:")
        for level, message in messages
    )
    assert (
        validate_handoff_frontmatter(
            bounced, git_cwd=tmp_path, dispatcher_authored=True
        ).errors
        == []
    )


def test_run_loop_gate_bounces_feed_the_three_fix_circuit_breaker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    ledger.increment_bounce(STORY_ID)
    ledger.increment_bounce(STORY_ID)
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path)
    dispatches: list[str] = []
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        _write(tmp_path / "src" / "api.py", "API = 2\n")
        head = _commit_all(tmp_path, "touch api")
        path.write_text(_claim(head), encoding="utf-8")
        return _dispatch_result()

    def fake_planner(path: Path, **kwargs: object) -> DispatchResult:
        dispatches.append("planner")
        assert "three-fix" in str(kwargs.get("additional_instruction", "")).lower() or (
            "bounced" in str(kwargs.get("additional_instruction", ""))
        )
        path.write_text(
            """---
next_agent: user
reason: "Human review required."
producer: planner
---

## user

Re-scoping needed.
""",
            encoding="utf-8",
        )
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)

    exit_code = orchestrator.run_loop(
        config, max_cycles=2, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert dispatches == ["backend", "planner"]
    assert ledger.bounce_count(STORY_ID) == 3
    assert any("THREE_FIX_CIRCUIT_BREAKER" in message for _level, message in messages)


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------


FINDING_TOOL_BODY = (
    "import json, sys\nprint('finding: ' + json.dumps(sys.argv[1:]))\nsys.exit(1)\n"
)
CLEAN_TOOL_BODY = (
    "import json, sys\nprint('clean: ' + json.dumps(sys.argv[1:]))\nsys.exit(0)\n"
)
HANG_TOOL_BODY = "import time\ntime.sleep(30)\n"


def _order_tool_body(log_path: Path, label: str, exit_code: int) -> str:
    return (
        "import sys\n"
        f"with open({str(log_path)!r}, 'a', encoding='utf-8') as handle:\n"
        f"    handle.write({label!r} + '\\n')\n"
        f"sys.exit({exit_code})\n"
    )


def _install_fake_tool(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write(bin_dir / f"{name}.py", body)
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(
            f'@"{PYTHON}" "%~dp0{name}.py" %*\r\n',
            encoding="utf-8",
            newline="",
        )
    else:
        launcher = bin_dir / name
        _write(
            launcher,
            f'#!/bin/sh\nexec "{PYTHON}" "$(dirname "$0")/{name}.py" "$@"\n',
        )
        launcher.chmod(0o755)


@pytest.fixture
def fake_bin(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    bin_dir = tmp_path_factory.mktemp("fake-bin")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return bin_dir


def _scanner(name: str, command: str, **overrides: Any) -> ScannerConfig:
    return ScannerConfig(name=name, command=command, **overrides)


def _passing_claim_setup(
    tmp_path: Path,
    assignment: str | None = None,
) -> tuple[PreDispatchState, str]:
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    return state, head


def _verify_with_scanners(
    tmp_path: Path,
    state: PreDispatchState,
    head: str,
    scanners: Any,
    *,
    assignment: str | None = None,
):
    return verify_work_claim(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=assignment or _assignment(),
        claim_content=_claim(head),
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        scanners=scanners,
    )


def test_scanner_config_defaults() -> None:
    scanner = ScannerConfig(name="gitleaks", command="gitleaks git {repo_root}")

    assert scanner.timeout_ms == 300000
    assert scanner.required is True


@pytest.mark.parametrize(
    "command",
    [
        "semgrep {changed_file}",
        "tool {diff_range!r}",
        "tool {changed_files:>10}",
        "tool {",
    ],
)
def test_scanner_config_rejects_unfillable_placeholders(command: str) -> None:
    with pytest.raises(ValueError, match="scanner_config_invalid:semgrep"):
        ScannerConfig(name="semgrep", command=command)


def test_scanner_config_accepts_doubled_literal_braces() -> None:
    scanner = ScannerConfig(name="jq", command="jq '{{a: 1}}' {changed_files}")

    assert scanner.command == "jq '{{a: 1}}' {changed_files}"


def test_scanner_config_rejects_bad_names_and_duplicates(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scanner name"):
        ScannerConfig(name="bad name", command="tool")
    with pytest.raises(ValueError, match="unique"):
        DispatchConfig(
            repo_root=tmp_path,
            scanners=(
                ScannerConfig(name="dup", command="tool"),
                ScannerConfig(name="dup", command="tool"),
            ),
        )


def test_load_dispatch_config_reads_scanners(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """enforce_work_packet: true
scanners:
  - name: gitleaks
    command: gitleaks git --log-opts="{diff_range}" {repo_root}
  - name: bandit
    command: bandit -q {changed_py_files}
    timeout_ms: 1000
    required: false
""",
        encoding="utf-8",
    )

    config = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert [scanner.name for scanner in config.scanners] == ["gitleaks", "bandit"]
    assert config.scanners[0].timeout_ms == 300000
    assert config.scanners[1].timeout_ms == 1000
    assert config.scanners[1].required is False


def test_load_dispatch_config_rejects_unknown_scanner_placeholder(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "scanners:\n  - name: semgrep\n    command: semgrep {files}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scanner_config_invalid:semgrep"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_dispatch_config_scanners_default_to_empty(tmp_path: Path) -> None:
    assert DispatchConfig(repo_root=tmp_path).scanners == ()


def test_render_scanner_command_substitutes_and_quotes_paths(tmp_path: Path) -> None:
    context = ScannerContext(
        repo_root=tmp_path,
        diff_range="aaa..bbb",
        changed_files=("src/my file.py", "docs/notes.md"),
        changed_py_files=("src/my file.py",),
    )

    rendered = render_scanner_command(
        "tool --range {diff_range} --root {repo_root} -- {changed_files} "
        ":: {changed_py_files}",
        context,
    )

    quoted = shell_quote("src/my file.py")
    assert quoted != "src/my file.py"
    assert rendered.command == (
        f"tool --range aaa..bbb --root {shell_quote(str(tmp_path))} -- "
        f"{quoted} {shell_quote('docs/notes.md')} :: {quoted}"
    )
    assert rendered.empty_placeholder is None


def test_render_scanner_command_skips_when_list_placeholder_is_empty(
    tmp_path: Path,
) -> None:
    context = ScannerContext(
        repo_root=tmp_path,
        diff_range="aaa..bbb",
        changed_files=("docs/notes.md",),
        changed_py_files=(),
    )

    rendered = render_scanner_command("bandit -q {changed_py_files}", context)

    assert rendered.command is None
    assert rendered.empty_placeholder == "changed_py_files"


def test_scanner_receives_quoted_paths_with_spaces(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    _install_fake_tool(fake_bin, "fakescan", FINDING_TOOL_BODY)
    assignment = _assignment(files_in_scope="src/")
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "my file.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "add spaced file")

    result = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [_scanner("fakescan", "fakescan {changed_files}")],
        assignment=assignment,
    )

    assert result.failed_prefixes == ("scanner_failed:fakescan",)
    scanner_run = result.scanner_runs[0]
    assert scanner_run.run is not None
    assert 'finding: ["src/my file.py"]' in scanner_run.run.output_tail


def test_scanner_diff_range_spans_pre_dispatch_head_to_scope_sha(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    _install_fake_tool(fake_bin, "rangescan", FINDING_TOOL_BODY)
    initial = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [_scanner("range", "rangescan {diff_range}")],
    )

    assert result.scanner_runs[0].run is not None
    assert f'["{initial}..{head}"]' in result.scanner_runs[0].run.output_tail


def test_scanners_run_in_config_order_before_verification_and_report_all(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    order_log = fake_bin / "order.log"
    _install_fake_tool(fake_bin, "scan-a", _order_tool_body(order_log, "a", 1))
    _install_fake_tool(fake_bin, "scan-b", _order_tool_body(order_log, "b", 0))
    _install_fake_tool(fake_bin, "scan-c", _order_tool_body(order_log, "c", 1))
    _install_fake_tool(fake_bin, "verify-cmd", _order_tool_body(order_log, "verify", 0))
    assignment = _assignment("verify-cmd")
    state, head = _passing_claim_setup(tmp_path, assignment)

    result = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [
            _scanner("a", "scan-a {diff_range}"),
            _scanner("b", "scan-b"),
            _scanner("c", "scan-c {repo_root}"),
        ],
        assignment=assignment,
    )

    assert order_log.read_text(encoding="utf-8").split() == ["a", "b", "c", "verify"]
    assert result.failed_prefixes == ("scanner_failed:a", "scanner_failed:c")
    assert [run.outcome for run in result.scanner_runs] == ["FAIL", "PASS", "FAIL"]
    assert [check.name for check in result.checks][-4:] == [
        "scanner:a",
        "scanner:b",
        "scanner:c",
        "verification",
    ]
    assert result.verification_runs[0].exit_code == 0


def test_scanner_skips_when_changed_py_files_is_empty(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    _install_fake_tool(fake_bin, "pyscan", FINDING_TOOL_BODY)
    assignment = _assignment(files_in_scope="docs/")
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "docs" / "notes.md", "notes\n")
    head = _commit_all(tmp_path, "add notes")

    result = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [_scanner("pyscan", "pyscan {changed_py_files}")],
        assignment=assignment,
    )

    assert result.passed
    assert result.scanner_runs[0].outcome == "SKIP"
    assert result.scanner_runs[0].skipped is True
    assert "{changed_py_files} is empty" in result.scanner_runs[0].detail


def test_required_missing_scanner_fails_and_optional_is_skipped(
    tmp_path: Path,
) -> None:
    state, head = _passing_claim_setup(tmp_path)

    required = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [_scanner("ghost", "ghost-scanner-not-installed {repo_root}")],
    )
    optional = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [
            _scanner(
                "ghost",
                "ghost-scanner-not-installed {repo_root}",
                required=False,
            )
        ],
    )

    assert required.failed_prefixes == ("scanner_missing:ghost",)
    assert "ghost-scanner-not-installed" in required.errors[0]
    assert required.scanner_runs[0].missing is True
    assert optional.passed
    assert optional.scanner_runs[0].outcome == "SKIP"
    assert optional.scanner_runs[0].missing is True


def test_scanner_timeout_kills_the_tool_and_reports_it(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    _install_fake_tool(fake_bin, "hangscan", HANG_TOOL_BODY)
    state, head = _passing_claim_setup(tmp_path)

    started = time.monotonic()
    result = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [_scanner("hang", "hangscan", timeout_ms=1000)],
    )

    assert time.monotonic() - started < 20
    assert result.failed_prefixes == ("scanner_timeout:hang",)
    assert "timed out after 1s" in result.errors[0]
    assert result.scanner_runs[0].run is not None
    assert result.scanner_runs[0].run.timed_out is True


def test_scanners_do_not_run_when_scope_checks_fail(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    order_log = fake_bin / "order.log"
    _install_fake_tool(fake_bin, "scan-a", _order_tool_body(order_log, "a", 0))
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    head = _commit_all(tmp_path, "touch api")

    result = _verify_with_scanners(tmp_path, state, head, [_scanner("a", "scan-a")])

    assert not order_log.exists()
    assert result.failed_prefixes == ("scope_violation",)
    assert result.scanner_runs[0].outcome == "SKIP"
    assert "scope checks did not pass" in result.scanner_runs[0].detail


def test_empty_scanner_list_leaves_gate_unchanged(tmp_path: Path) -> None:
    state, head = _passing_claim_setup(tmp_path)

    result = _verify_with_scanners(tmp_path, state, head, [])

    assert result.passed
    assert result.scanner_runs == ()
    assert not any(check.name.startswith("scanner:") for check in result.checks)


# ---------------------------------------------------------------------------
# Allowlisted environment for gate shell commands
# ---------------------------------------------------------------------------


TEST_SECRET_ENV = "LLM_HANDOFF_TEST_SECRET"
SECRET_ABSENT_COMMAND = (
    f'"{PYTHON}" -c "import os; import sys; '
    f"sys.exit(1 if '{TEST_SECRET_ENV}' in os.environ else 0)\""
)
SECRET_PRESENT_COMMAND = (
    f'"{PYTHON}" -c "import os; import sys; '
    f"sys.exit(0 if os.environ.get('{TEST_SECRET_ENV}') == 'shh' else 1)\""
)
SECRET_ABSENT_TOOL_BODY = (
    "import os, sys\n"
    f"sys.exit(1 if {TEST_SECRET_ENV!r} in os.environ else 0)\n"
)


def test_build_work_claim_env_keeps_only_allowlisted_and_passthrough_names() -> None:
    environ = {
        "PATH": "/usr/bin",
        "SystemRoot": "C:\\Windows",
        TEST_SECRET_ENV: "shh",
        "OPENAI_API_KEY": "sk-test",
        "CLAUDE_CODE_OAUTH_TOKEN": "token",
        "Codex_Setting": "value",
        "GEMINI_API_KEY": "gemini-key",
        "GOOGLE_API_KEY": "google-key",
        "Extra_Tool_Home": "/opt/tool",
        "UNLISTED_VALUE": "nope",
    }

    env = build_work_claim_env(
        (
            "extra_tool_home",
            "OPENAI_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "codex_setting",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        ),
        environ=environ,
    )

    assert env == {
        "PATH": "/usr/bin",
        "SystemRoot": "C:\\Windows",
        "Extra_Tool_Home": "/opt/tool",
    }


def test_build_work_claim_env_drops_secret_without_passthrough() -> None:
    env = build_work_claim_env(environ={"PATH": "/bin", TEST_SECRET_ENV: "shh"})

    assert env == {"PATH": "/bin"}


def test_run_shell_command_does_not_inherit_unlisted_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "shh")

    run = run_shell_command(SECRET_ABSENT_COMMAND, cwd=tmp_path, timeout_seconds=60)

    assert run.exit_code == 0, run.output_tail


def test_run_shell_command_passes_listed_passthrough_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "shh")

    run = run_shell_command(
        SECRET_PRESENT_COMMAND,
        cwd=tmp_path,
        timeout_seconds=60,
        env_passthrough=(TEST_SECRET_ENV,),
    )

    assert run.exit_code == 0, run.output_tail


def test_run_shell_command_kills_tree_and_reraises_on_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import llm_handoff.work_claim as work_claim_module

    class _InterruptedProcess:
        pid = 1
        returncode = None

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            raise KeyboardInterrupt

    process = _InterruptedProcess()
    killed: list[object] = []
    monkeypatch.setattr(
        work_claim_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(work_claim_module, "_kill_process_tree", killed.append)

    # The command runs in its own process group, so a second Ctrl+C during
    # the gate must not leave the verification command running.
    with pytest.raises(KeyboardInterrupt):
        run_shell_command("echo hi", cwd=tmp_path, timeout_seconds=60)

    assert killed == [process]


def test_run_scanner_hands_env_passthrough_to_the_runner(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    _install_fake_tool(fake_bin, "fakescan", CLEAN_TOOL_BODY)
    calls: list[dict[str, Any]] = []

    def recording_runner(command: str, **kwargs: Any) -> CommandRun:
        calls.append({"command": command, **kwargs})
        return CommandRun(
            command=command,
            exit_code=0,
            elapsed_seconds=0.0,
            output_tail="",
            timeout_seconds=kwargs["timeout_seconds"],
        )

    context = ScannerContext(
        repo_root=tmp_path,
        diff_range="aaa..bbb",
        changed_files=("src/app.py",),
        changed_py_files=("src/app.py",),
    )

    scanner_run, error = run_scanner(
        _scanner("fakescan", "fakescan {changed_files}"),
        context,
        runner=recording_runner,
        env_passthrough=("EXTRA_TOOL_HOME",),
    )

    assert error is None
    assert scanner_run.outcome == "PASS"
    assert len(calls) == 1
    assert calls[0]["env_passthrough"] == ("EXTRA_TOOL_HOME",)
    assert calls[0]["cwd"] == tmp_path


def test_verify_work_claim_runs_scanners_and_verification_without_unlisted_variables(
    tmp_path: Path,
    fake_bin: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "shh")
    _install_fake_tool(fake_bin, "envscan", SECRET_ABSENT_TOOL_BODY)
    assignment = _assignment(SECRET_ABSENT_COMMAND)
    state, head = _passing_claim_setup(tmp_path, assignment)

    result = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [_scanner("envscan", "envscan {changed_files}")],
        assignment=assignment,
    )

    assert result.passed, result.errors
    assert _outcomes(result)["scanner:envscan"] == "PASS"
    assert _outcomes(result)["verification"] == "PASS"


def test_verify_work_claim_passes_env_passthrough_to_scanners_and_verification(
    tmp_path: Path,
    fake_bin: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "shh")
    _install_fake_tool(fake_bin, "envscan", SECRET_ABSENT_TOOL_BODY)
    assignment = _assignment(SECRET_PRESENT_COMMAND)
    state, head = _passing_claim_setup(tmp_path, assignment)

    result = verify_work_claim(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=assignment,
        claim_content=_claim(head),
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        scanners=[_scanner("envscan", "envscan {changed_files}")],
        env_passthrough=(TEST_SECRET_ENV,),
    )

    # The scanner sees the passed-through variable, so the absent-check fails.
    assert result.failed_prefixes == ("scanner_failed:envscan",)
    assert _outcomes(result)["verification"] == "PASS"


def test_build_bounce_handoff_lists_every_failed_scanner(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    _install_fake_tool(fake_bin, "scan-a", FINDING_TOOL_BODY)
    _install_fake_tool(fake_bin, "scan-b", CLEAN_TOOL_BODY)
    _install_fake_tool(fake_bin, "scan-c", FINDING_TOOL_BODY)
    state, head = _passing_claim_setup(tmp_path)
    claim = _claim(head)
    result = _verify_with_scanners(
        tmp_path,
        state,
        head,
        [
            _scanner("a", "scan-a {changed_py_files}"),
            _scanner("b", "scan-b"),
            _scanner("c", "scan-c {diff_range}"),
        ],
    )
    bounce_route = bounce_route_for(result, implementer="backend")

    bounce = build_bounce_handoff(
        claim_content=claim,
        result=result,
        implementer="backend",
        route=bounce_route,
        bounce_count=1,
    )

    assert bounce_route.next_agent == "backend"
    assert (
        'reason: "Dispatcher verification failed: scanner_failed:a, '
        'scanner_failed:c."' in bounce
    )
    assert "### Scanner Output" in bounce
    assert "Scanner `a`:" in bounce
    assert "Scanner `c`:" in bounce
    assert "Scanner `b`:" not in bounce
    assert "FAIL `scanner:a`" in bounce
    assert "PASS `scanner:b`" in bounce
    assert "FAIL `scanner:c`" in bounce
    assert 'finding: ["src/app.py"]' in bounce
    assert route(bounce).route == "backend"


def test_scanner_config_invalid_at_runtime_escalates_to_user(tmp_path: Path) -> None:
    class RawScanner:
        name = "raw"
        command = "tool {unknown}"
        timeout_ms = 1000
        required = True

    state, head = _passing_claim_setup(tmp_path)

    result = _verify_with_scanners(tmp_path, state, head, [RawScanner()])

    assert result.failed_prefixes == ("scanner_config_invalid:raw",)
    assert bounce_route_for(result, implementer="backend").next_agent == "user"


def test_run_loop_bounces_scanner_findings_and_warns_for_optional_missing_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    fake_bin: Path,
) -> None:
    _install_fake_tool(fake_bin, "gitleaks", FINDING_TOOL_BODY)
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path,
        scanners=(
            ScannerConfig(name="gitleaks", command="gitleaks {diff_range}"),
            ScannerConfig(
                name="pip-audit",
                command="pip-audit-not-installed",
                required=False,
            ),
        ),
    )
    dispatches: list[str] = []
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        head = _commit_all(tmp_path, "set value")
        path.write_text(_claim(head), encoding="utf-8")
        return _dispatch_result()

    def fake_support(
        subagent_name: str, prompt: str, **_kwargs: object
    ) -> SubagentResult:
        raise AssertionError("auditor must not be dispatched when a scanner fails")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)

    exit_code = orchestrator.run_loop(
        config, max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    assert dispatches == ["backend"]
    bounced = parse_handoff_frontmatter_text(
        config.handoff_full_path.read_text(encoding="utf-8")
    )
    assert bounced is not None
    assert bounced.next_agent == "backend"
    assert bounced.status == "verified_fail"
    assert ledger.bounce_count(STORY_ID) == 1
    assert any(
        level == "ERROR" and message.startswith("scanner_failed:gitleaks:")
        for level, message in messages
    )
    assert any(
        level == "WARN" and "optional scanner pip-audit skipped" in message
        for level, message in messages
    )
    assert any(
        "Scanners:           gitleaks, pip-audit" in message
        for _level, message in messages
    )


def test_startup_banner_warns_when_scanners_configured_but_gate_is_off(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path,
        enforce_work_packet=False,
        work_packet_opt_out_reason=GATE_OPT_OUT_REASON,
        dry_run=True,
        scanners=(ScannerConfig(name="gitleaks", command="gitleaks"),),
    )
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert any(
        level == "WARN" and "Scanners configured (gitleaks)" in message
        for level, message in messages
    )


def test_startup_banner_warns_with_reason_when_gate_is_off(tmp_path: Path) -> None:
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path,
        enforce_work_packet=False,
        work_packet_opt_out_reason=GATE_OPT_OUT_REASON,
    )
    messages: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(config, _collecting_log(messages))

    gate_lines = [
        (level, message)
        for level, message in messages
        if message.startswith("Work-claim gate:")
    ]
    assert gate_lines == [
        (
            "WARN",
            "Work-claim gate:    OFF (enforce_work_packet: false; "
            f"work_packet_opt_out_reason: {GATE_OPT_OUT_REASON})",
        )
    ]


def test_startup_banner_logs_info_when_gate_is_on(tmp_path: Path) -> None:
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path)
    messages: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(config, _collecting_log(messages))

    gate_lines = [
        (level, message)
        for level, message in messages
        if message.startswith("Work-claim gate:")
    ]
    assert gate_lines == [
        (
            "INFO",
            "Work-claim gate:    ON (runs the Work Packet verification command)",
        )
    ]


def _allowlist_lines(messages: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (level, message)
        for level, message in messages
        if message.startswith("Verification allowlist:")
    ]


def test_startup_banner_warns_when_verification_allowlist_is_empty(
    tmp_path: Path,
) -> None:
    orchestrator = _load_orchestrator()
    messages: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(
        _dispatch_config(tmp_path), _collecting_log(messages)
    )

    lines = _allowlist_lines(messages)
    assert len(lines) == 1
    level, message = lines[0]
    assert level == "WARN"
    assert message.startswith("Verification allowlist: OFF")
    assert "verification_allowed_prefixes" in message


def test_startup_banner_lists_verification_allowlist_when_set(tmp_path: Path) -> None:
    orchestrator = _load_orchestrator()
    messages: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(
        _dispatch_config(
            tmp_path,
            verification_allowed_prefixes=("python -m pytest", "npm test"),
        ),
        _collecting_log(messages),
    )

    assert _allowlist_lines(messages) == [
        ("INFO", "Verification allowlist: python -m pytest, npm test")
    ]


def test_startup_banner_omits_verification_allowlist_when_gate_is_off(
    tmp_path: Path,
) -> None:
    orchestrator = _load_orchestrator()
    messages: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(
        _dispatch_config(
            tmp_path,
            enforce_work_packet=False,
            work_packet_opt_out_reason=GATE_OPT_OUT_REASON,
        ),
        _collecting_log(messages),
    )

    assert _allowlist_lines(messages) == []


# ---------------------------------------------------------------------------
# Verification command allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest",
        "python -m pytest tests -q",
        "  python   -m pytest\ttests  ",
        "anything at all & whoami",
    ],
)
def test_verification_command_allowed_with_an_empty_list_allows_everything(
    command: str,
) -> None:
    assert verification_command_allowed(command, ()) is None


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest",
        "python -m pytest tests -q",
        "  python -m pytest tests -q\t",
        "\tpython -m pytest  ",
    ],
)
def test_verification_command_allowed_accepts_a_token_prefix_match(
    command: str,
) -> None:
    assert verification_command_allowed(command, ("python -m pytest",)) is None


def test_verification_command_allowed_accepts_any_listed_prefix() -> None:
    prefixes = ("npm test", "python -m pytest")
    assert verification_command_allowed("python -m pytest -q", prefixes) is None
    assert verification_command_allowed("npm test -- --ci", prefixes) is None


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ("python -m pytestx", "does not start with any"),
        ('python -c "x"', "does not start with any"),
        ("Python -m pytest", "does not start with any"),
        ("python -m", "does not start with any"),
        ("python -m pytest tests -q & whoami", "'&'"),
        ("python -m pytest | tee out", "'|'"),
        ("python -m pytest; whoami", "';'"),
        ("python -m pytest < in", "'<'"),
        ("python -m pytest > out", "'>'"),
        ("python -m pytest $HOME", "'$'"),
        ("python -m pytest `whoami`", "'`'"),
        ("python -m pytest (x)", "'('"),
        ("python -m pytest x)", "')'"),
        ("python -m pytest %CD%", "'%'"),
        ("python -m pytest !CD!", "'!'"),
        ("python -m pytest\rwhoami", "'\\r'"),
        ("python -m pytest\nwhoami", "'\\n'"),
        ("python  -m pytest", "whitespace"),
        ("python -m pytest\ttests", "whitespace"),
        ("python -m pytest  -q", "whitespace"),
        ("  python   -m pytest\ttests", "whitespace"),
        ("python -m pytest\x0btests", "whitespace"),
        ("python -m pytest\x0ctests", "whitespace"),
        ("python -m pytest", "whitespace"),
    ],
)
def test_verification_command_allowed_rejects_with_a_one_line_reason(
    command: str,
    fragment: str,
) -> None:
    reason = verification_command_allowed(command, ("python -m pytest",))

    assert reason is not None
    assert fragment in reason
    assert "\n" not in reason and "\r" not in reason


def _grant_regex(prefix: str) -> re.Pattern[str]:
    grant = antigravity_command_grant(prefix)
    assert grant.startswith("command(regex:") and grant.endswith(")")
    return re.compile(grant[len("command(regex:") : -1])


@pytest.mark.parametrize("prefix", ["python -m pytest", "git status", "npm test"])
def test_verification_command_allowed_agrees_with_the_agy_grant(prefix: str) -> None:
    words = prefix.split()
    doubled = "  ".join(words)
    candidates = [
        prefix,
        f"{prefix} -q",
        f"{prefix} tests -q --tb=short",
        f"{prefix}x",
        f"{prefix}\t-q",
        f"{prefix}  -q",
        f"  {prefix} -q  ",
        f"\t{prefix} -q",
        doubled,
        f"{doubled} -q",
        "\t".join(words),
        prefix.upper(),
        words[0],
    ]
    grant = _grant_regex(prefix)
    allowed = [
        command
        for command in candidates
        if verification_command_allowed(command, (prefix,)) is None
    ]

    assert prefix in allowed and f"{prefix} -q" in allowed
    for command in allowed:
        # The gate strips surrounding whitespace; the agent runs the stripped line.
        assert grant.fullmatch(command.strip(" \t")), command
    for command in (doubled, f"{doubled} -q"):
        assert verification_command_allowed(command, (prefix,)) is not None
        assert grant.fullmatch(command) is None


def _assignment_with_commands(*commands: str) -> str:
    listed = "\n".join(f"  - {command}" for command in commands)
    return _assignment("").replace(
        "- **Verification command:** \n",
        f"- **Verification command:**\n{listed}\n",
    )


def test_verify_work_claim_rejects_a_disallowed_command_without_running_any(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    order_log = fake_bin / "order.log"
    _install_fake_tool(fake_bin, "verify-cmd", _order_tool_body(order_log, "verify", 0))
    _install_fake_tool(fake_bin, "other-cmd", _order_tool_body(order_log, "other", 0))
    assignment = _assignment_with_commands("verify-cmd", "other-cmd --all")
    assert parse_work_packet(assignment).verification_commands == (
        "verify-cmd",
        "other-cmd --all",
    )
    state, head = _passing_claim_setup(tmp_path, assignment)
    claim = _claim(head)
    _write(tmp_path / HANDOFF_RELATIVE_PATH, claim)
    logged: list[tuple[str, str]] = []

    result = verify_work_claim(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=assignment,
        claim_content=claim,
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        verification_allowed_prefixes=("verify-cmd",),
        log=_collecting_log(logged),
    )

    assert not order_log.exists()
    assert result.verification_runs == ()
    assert result.failed_prefixes == ("verification_not_allowed",)
    assert _outcomes(result)["verification"] == "FAIL"
    assert "`other-cmd --all`" in result.errors[0]
    assert result.rejected_verification_command == "other-cmd --all"
    assert not any("running verification command" in m for _l, m in logged)

    bounce_route = bounce_route_for(result, implementer="backend")
    assert bounce_route.next_agent == "planner"
    assert bounce_route.status == "verified_fail"
    bounce = build_bounce_handoff(
        claim_content=claim,
        result=result,
        implementer="backend",
        route=bounce_route,
        bounce_count=1,
    )
    assert route(bounce).route == "planner"
    assert "rejected the Verification command `other-cmd --all`" in bounce
    assert "`verification_allowed_prefixes`" in bounce
    assert "shell control character" in bounce
    assert "Re-issue the Work Packet" in bounce
    assert "did not carry a usable `## Work Packet`" not in bounce


def test_verification_not_allowed_yields_to_a_user_escalation(tmp_path: Path) -> None:
    result = WorkClaimResult(
        checks=(),
        errors=(
            "verification_not_allowed: rejected.",
            "git_state_unavailable: git status failed.",
        ),
        pre_dispatch_head=None,
        head_sha=None,
        scope_sha=None,
        changed_files=(),
        verification_runs=(),
        work_packet=None,
        work_packet_text=None,
    )

    bounce_route = bounce_route_for(result, implementer="backend")

    assert bounce_route.next_agent == "user"
    assert bounce_route.status == "escalate_to_user"


def test_verification_not_allowed_outranks_implementer_failures(
    tmp_path: Path,
) -> None:
    result = WorkClaimResult(
        checks=(),
        errors=(
            "scope_violation: changed files outside Files in scope: x.py.",
            "verification_not_allowed: rejected.",
        ),
        pre_dispatch_head=None,
        head_sha=None,
        scope_sha=None,
        changed_files=(),
        verification_runs=(),
        work_packet=None,
        work_packet_text=None,
    )

    assert bounce_route_for(result, implementer="backend").next_agent == "planner"


def test_verify_work_claim_runs_an_allowed_command(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    order_log = fake_bin / "order.log"
    _install_fake_tool(fake_bin, "verify-cmd", _order_tool_body(order_log, "verify", 0))
    assignment = _assignment("verify-cmd --quiet")
    state, head = _passing_claim_setup(tmp_path, assignment)
    claim = _claim(head)
    _write(tmp_path / HANDOFF_RELATIVE_PATH, claim)

    result = verify_work_claim(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=assignment,
        claim_content=claim,
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        verification_allowed_prefixes=("npm test", "verify-cmd"),
    )

    assert result.passed, result.errors
    assert order_log.read_text(encoding="utf-8").split() == ["verify"]
    assert _outcomes(result)["verification"] == "PASS"
    assert result.rejected_verification_command is None


def test_verify_work_claim_logs_scanner_and_verification_commands_before_running(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    order_log = fake_bin / "order.log"
    _install_fake_tool(fake_bin, "scan-a", _order_tool_body(order_log, "scan", 0))
    _install_fake_tool(fake_bin, "verify-cmd", _order_tool_body(order_log, "verify", 0))
    assignment = _assignment("verify-cmd --quiet")
    state, head = _passing_claim_setup(tmp_path, assignment)
    claim = _claim(head)
    _write(tmp_path / HANDOFF_RELATIVE_PATH, claim)
    logged: list[tuple[str, str]] = []

    def log(level: str, message: str) -> None:
        logged.append((level, message))
        with order_log.open("a", encoding="utf-8") as handle:
            handle.write("log\n")

    result = verify_work_claim(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=assignment,
        claim_content=claim,
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        scanners=[_scanner("a", "scan-a {diff_range}")],
        log=log,
    )

    assert result.passed, result.errors
    assert order_log.read_text(encoding="utf-8").split() == [
        "log",
        "scan",
        "log",
        "verify",
    ]
    # The scanner runs, and is logged, by the absolute path the lookup found.
    scan_a = find_scanner_executable("scan-a")
    assert scan_a is not None
    assert logged == [
        (
            "INFO",
            "work-claim: running scanner a: "
            f"{shell_quote(scan_a)} {state.head_sha}..{head}",
        ),
        ("INFO", "work-claim: running verification command: verify-cmd --quiet"),
    ]


def test_verify_work_claim_without_log_behaves_as_before(
    tmp_path: Path,
    fake_bin: Path,
) -> None:
    order_log = fake_bin / "order.log"
    _install_fake_tool(fake_bin, "scan-a", _order_tool_body(order_log, "scan", 0))
    _install_fake_tool(fake_bin, "verify-cmd", _order_tool_body(order_log, "verify", 0))
    assignment = _assignment("verify-cmd")
    state, head = _passing_claim_setup(tmp_path, assignment)

    result = _verify_with_scanners(
        tmp_path, state, head, [_scanner("a", "scan-a")], assignment=assignment
    )

    assert result.passed, result.errors
    assert order_log.read_text(encoding="utf-8").split() == ["scan", "verify"]


def test_docs_describe_scanners() -> None:
    configuration = (REPO_ROOT / "CONFIGURATION.md").read_text(encoding="utf-8")
    architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    example = (REPO_ROOT / "dispatch_config.example.yaml").read_text(encoding="utf-8")

    for tool in ("gitleaks", "semgrep", "bandit", "pip-audit"):
        assert tool in configuration
        assert tool in example
    for placeholder in (
        "{diff_range}",
        "{changed_files}",
        "{changed_py_files}",
        "{repo_root}",
    ):
        assert placeholder in configuration
    for prefix in (
        "scanner_failed",
        "scanner_missing",
        "scanner_timeout",
        "scanner_config_invalid",
    ):
        assert prefix in configuration
        assert prefix in architecture
    assert "operator-authored" in architecture
    assert "operator" in security
    assert "sandbox" in security.lower()
    assert "requirements-dev" not in configuration


def test_example_config_has_no_active_semgrep_config_auto_entry() -> None:
    config_path = REPO_ROOT / "dispatch_config.example.yaml"
    content = config_path.read_text(encoding="utf-8")
    active_lines = [
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    ]

    assert not any("--config auto" in line for line in active_lines)
    assert not any("semgrep" in line for line in active_lines)
    assert "#   command: semgrep scan --config .semgrep/rules.yml" in content
    config = load_dispatch_config(repo_root=config_path.parent, config_path=config_path)
    assert [scanner.name for scanner in config.scanners] == [
        "gitleaks",
        "bandit",
        "pip-audit",
    ]
    assert all("--config auto" not in scanner.command for scanner in config.scanners)


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------


def test_parse_work_packet_keeps_quoted_paths_with_spaces_whole() -> None:
    packet = parse_work_packet(
        _assignment(
            files_in_scope='`src/my file.py`, "docs/user guide.md" tests/test_app.py',
            files_out_of_bounds="'vendor/third party/'; src/api.py",
        )
    )

    assert packet.files_in_scope == (
        "src/my file.py",
        "docs/user guide.md",
        "tests/test_app.py",
    )
    assert packet.files_out_of_bounds == ("vendor/third party/", "src/api.py")


def test_verify_work_claim_accepts_change_to_quoted_path_with_space(
    tmp_path: Path,
) -> None:
    assignment = _assignment(files_in_scope="`src/my file.py`")
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "my file.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "add spaced file")

    result = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)

    assert result.passed, result.errors


def test_verify_work_claim_rejects_further_edits_to_pre_existing_dirty_file(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "api.py", "API = 'dirty before dispatch'\n")
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_paths(tmp_path, "set value", "src/app.py")
    _write(tmp_path / "src" / "api.py", "API = 'edited again during dispatch'\n")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=state
    )

    assert result.failed_prefixes == ("uncommitted_changes",)
    assert "already uncommitted before dispatch" in result.errors[0]
    assert "src/api.py" in result.errors[0]


def test_verify_work_claim_ignores_untouched_pre_existing_dirty_file(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "api.py", "API = 'dirty before dispatch'\n")
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_paths(tmp_path, "set value", "src/app.py")

    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=state
    )

    assert result.passed, result.errors


# ---------------------------------------------------------------------------
# Dispatcher commit mode
# ---------------------------------------------------------------------------


def _pending_claim() -> str:
    """A ready_for_review handback written without committing anything."""

    return _claim(None).replace(
        "0123456789abcdef0123456789abcdef01234567",
        PENDING_COMMIT_SHA,
    )


def _commit_files(repo_root: Path, sha: str) -> list[str]:
    return _git(repo_root, "show", "--name-only", "--format=", sha).splitlines()


def test_dispatcher_commit_stages_only_in_scope_changes(tmp_path: Path) -> None:
    base = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    _write(tmp_path / "notes.txt", "scratch\n")

    result = commit_implementer_changes(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(),
        claim_content=_pending_claim(),
        pre_dispatch_state=state,
        role="backend",
    )

    assert result.error is None
    assert result.sha == _git(tmp_path, "rev-parse", "HEAD")
    assert result.sha != base
    assert result.committed == ("src/app.py",)
    assert set(result.left_uncommitted) == {"src/api.py", "notes.txt"}
    assert _commit_files(tmp_path, result.sha) == ["src/app.py"]
    subject = _git(tmp_path, "log", "-1", "--format=%s")
    assert subject == f"{STORY_ID} Change the value"


def test_dispatcher_commit_includes_new_files_under_an_in_scope_directory(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "tests" / "test_extra.py", "def test_extra():\n    pass\n")

    result = commit_implementer_changes(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(files_in_scope="src/app.py, tests/"),
        claim_content=_pending_claim(),
        pre_dispatch_state=state,
        role="backend",
    )

    assert result.committed == ("tests/test_extra.py",)


def test_dispatcher_commit_never_stages_files_that_were_dirty_before_dispatch(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 'operator edit'\n")
    state = capture_pre_dispatch_state(tmp_path)

    result = commit_implementer_changes(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(),
        claim_content=_pending_claim(),
        pre_dispatch_state=state,
        role="backend",
    )

    assert result.sha is None
    assert result.skipped_reason == "no in-scope files changed since dispatch"
    assert "src/app.py" in _git(tmp_path, "status", "--porcelain")


def test_dispatcher_commit_leaves_other_staged_changes_alone(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "PROJECT_STATE.md", "# PROJECT STATE\nstaged by the operator\n")
    _git(tmp_path, "add", "PROJECT_STATE.md")
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")

    result = commit_implementer_changes(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(),
        claim_content=_pending_claim(),
        pre_dispatch_state=state,
        role="backend",
    )

    assert result.sha is not None
    assert _commit_files(tmp_path, result.sha) == ["src/app.py"]
    assert "M  PROJECT_STATE.md" in _git(tmp_path, "status", "--porcelain")


def test_dispatcher_commit_skips_handbacks_that_are_not_ready_for_review(
    tmp_path: Path,
) -> None:
    base = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")

    result = commit_implementer_changes(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(),
        claim_content=_claim(None, status="blocked_implementation_failure"),
        pre_dispatch_state=state,
        role="backend",
    )

    assert result.sha is None
    assert "blocked_implementation_failure" in (result.skipped_reason or "")
    assert _git(tmp_path, "rev-parse", "HEAD") == base


# A planner-written boundary in prose, wrapped across lines the way Markdown
# editors and agents wrap long bullets.
PROSE_OUT_OF_BOUNDS = (
    "`docs/handoff/`, `AGENTS.md`, every file in `src/` other than\n"
    "  `src/app.py`, every file in `tests/` other than `tests/test_app.py`"
)


def test_parse_work_packet_ignores_prose_words_and_exceptions_in_out_of_bounds() -> (
    None
):
    packet = parse_work_packet(
        _assignment(
            files_in_scope="`src/app.py`, `tests/test_app.py`",
            files_out_of_bounds=PROSE_OUT_OF_BOUNDS,
        )
    )

    assert packet.files_out_of_bounds == (
        "docs/handoff/",
        "AGENTS.md",
        "src/",
        "tests/",
    )


def test_parse_work_packet_keeps_bare_paths_next_to_backticked_prose() -> None:
    packet = parse_work_packet(
        _assignment(files_out_of_bounds="`frontend/` and also vendor/lib.py, Makefile")
    )

    # Prose mode keeps bare words only when they look like paths.
    assert packet.files_out_of_bounds == ("frontend/", "vendor/lib.py")


def test_path_is_out_of_bounds_lets_the_narrowest_entry_decide() -> None:
    packet = parse_work_packet(
        _assignment(
            files_in_scope="src/app.py, src/pkg/",
            files_out_of_bounds="src/, src/pkg/private.py",
        )
    )

    assert path_is_in_scope("src/app.py", packet)
    assert path_is_out_of_bounds("src/api.py", packet)
    assert path_is_in_scope("src/pkg/mod.py", packet)
    assert path_is_out_of_bounds("src/pkg/private.py", packet)


def test_path_is_out_of_bounds_wins_a_tie() -> None:
    # parse_work_packet rejects an entry listed in both fields, so build the
    # packet directly to pin the tie rule itself.
    packet = WorkPacket(
        files_in_scope=("src/app.py",),
        files_out_of_bounds=("src/app.py",),
        verification_commands=(PASS_COMMAND,),
        section_text="",
    )

    assert path_is_out_of_bounds("src/app.py", packet)
    assert not path_is_in_scope("src/app.py", packet)


def test_parse_work_packet_rejects_a_path_in_both_lists() -> None:
    # Replays the W1.6 self-host loop: the planner limited edits within a file
    # by listing it under both fields, so the file resolved to out of bounds.
    assignment = _assignment(
        files_in_scope="`src/app.py` (only the VALUE constant), `tests/test_app.py`",
        files_out_of_bounds="`src/api.py`, `src/app.py` beyond the VALUE constant",
    )

    with pytest.raises(WorkPacketError) as exc_info:
        parse_work_packet(assignment)

    assert exc_info.value.prefix == "work_packet_unparseable"
    assert (
        "listed under both Files in scope and Files out of bounds: `src/app.py`"
        in exc_info.value.detail
    )
    assert work_packet_errors(assignment)


def test_parse_work_packet_treats_a_directory_with_and_without_slash_as_one() -> None:
    with pytest.raises(WorkPacketError) as exc_info:
        parse_work_packet(_assignment(files_in_scope="src", files_out_of_bounds="src/"))

    assert "Files out of bounds: `src`" in exc_info.value.detail


def test_verify_work_claim_routes_a_path_in_both_lists_to_the_planner(
    tmp_path: Path,
) -> None:
    assignment = _assignment(
        files_in_scope="src/app.py", files_out_of_bounds="src/app.py"
    )
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)

    assert result.failed_prefixes == ("work_packet_unparseable",)
    assert bounce_route_for(result, implementer="backend").next_agent == "planner"


@pytest.mark.parametrize(
    ("command_lines", "prose"),
    [
        # Replays the W1.7d self-host loop: a bare `and` between two commands.
        (
            (
                "\n  `python -m pytest tests/test_app.py -q`\n  and\n"
                "  `python -m pytest tests -q`"
            ),
            "`and`",
        ),
        ("\n  - python -m pytest -q\n  - Then,\n  - ruff check .", "`Then,`"),
        (
            "Run both of these:\n  - python -m pytest -q\n  - ruff check .",
            "`Run both of these:`",
        ),
    ],
)
def test_parse_work_packet_rejects_prose_lines_in_verification_command(
    command_lines: str, prose: str
) -> None:
    assignment = _assignment(command_lines)

    with pytest.raises(WorkPacketError) as exc_info:
        parse_work_packet(assignment)

    assert exc_info.value.prefix == "work_packet_unparseable"
    assert (
        f"Verification command has lines that are not commands: {prose}"
        in exc_info.value.detail
    )
    # The planner's post-dispatch gate reads the same parser.
    validation = validate_handoff_text(assignment, "planner", require_work_packet=True)
    assert validation.verdict == "NO"
    assert any(
        error.startswith("work_packet_unparseable:") and prose in error
        for error in validation.errors
    )


def test_parse_work_packet_keeps_commands_that_contain_connectives() -> None:
    packet = parse_work_packet(
        _assignment(
            '\n  - python -m pytest -k "parse and not slow" -q\n'
            "  - git diff --check && echo or"
        )
    )

    assert packet.verification_commands == (
        'python -m pytest -k "parse and not slow" -q',
        "git diff --check && echo or",
    )


@pytest.mark.parametrize(
    "command_lines",
    [
        # Replays the PR2 self-host loop: two commands as prose wrapped over
        # two lines, so the gate ran "`cmd1`, then" and pytest exited 4.
        "`python -m pytest tests/test_a.py -q`, then\n  `python -m pytest tests -q`",
        "`python -m pytest tests/test_a.py -q` and `python -m pytest tests -q`",
        "`python -m pytest tests/test_a.py -q`; `python -m pytest tests -q`.",
        "`python -m pytest tests/test_a.py -q` and then `python -m pytest tests -q`",
        (
            "\n  - `python -m pytest tests/test_a.py -q`, then\n"
            "  - `python -m pytest tests -q`"
        ),
    ],
)
def test_parse_work_packet_extracts_commands_from_backticked_prose(
    command_lines: str,
) -> None:
    packet = parse_work_packet(_assignment(command_lines))

    assert packet.verification_commands == (
        "python -m pytest tests/test_a.py -q",
        "python -m pytest tests -q",
    )


def test_parse_work_packet_keeps_backticks_inside_a_double_backtick_span() -> None:
    packet = parse_work_packet(_assignment("``echo `date` ``"))

    assert packet.verification_commands == ("echo `date`",)


@pytest.mark.parametrize(
    "line",
    [
        # "or" names alternatives, so running both spans would not match.
        "`python -m pytest -q` or `python -m pytest tests -q`",
        "Run `python -m pytest -q` from the repo root",
        "`python -m pytest` on `tests/`",
        "`python -m pytest -q`, then `ruff check .",
    ],
)
def test_parse_work_packet_rejects_ambiguous_backticked_prose(line: str) -> None:
    assignment = _assignment(line)

    with pytest.raises(WorkPacketError) as exc_info:
        parse_work_packet(assignment)

    assert exc_info.value.prefix == "work_packet_unparseable"
    assert (
        f"Verification command has lines that are not commands: `{line}`"
        in exc_info.value.detail
    )


def test_verify_work_claim_runs_each_command_from_backticked_prose(
    tmp_path: Path,
) -> None:
    assignment = _assignment(f"`{PASS_COMMAND}`, then\n  `{PASS_COMMAND} second`")
    state, head = _passing_claim_setup(tmp_path, assignment)
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _claim(head))

    result = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)

    assert result.errors == ()
    assert result.passed


def _bounce_for(tmp_path: Path, claim: str, result) -> str:
    bounce = build_bounce_handoff(
        claim_content=claim,
        result=result,
        implementer="backend",
        route=bounce_route_for(result, implementer="backend"),
        bounce_count=1,
    )
    _write(tmp_path / HANDOFF_RELATIVE_PATH, bounce)
    return bounce


def test_verify_work_claim_routes_a_repeated_verification_failure_to_the_planner(
    tmp_path: Path,
) -> None:
    # Replays the W1.7d self-host loop: the implementer could only rewrite its
    # copy of the packet in the handoff, so the same command failed again.
    assignment = _assignment(FAIL_COMMAND)
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    first = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)
    assert first.repeated_verification_failure is False
    assert bounce_route_for(first, implementer="backend").next_agent == "backend"
    bounce = _bounce_for(tmp_path, _claim(head), first)

    retry_state = capture_pre_dispatch_state(tmp_path)
    claim = _claim(head)
    _write(tmp_path / HANDOFF_RELATIVE_PATH, claim)
    second = _verify(tmp_path, assignment=bounce, claim=claim, state=retry_state)

    assert second.failed_prefixes == ("verification_failed",)
    assert second.repeated_verification_failure is True
    bounce_route = bounce_route_for(second, implementer="backend")
    assert bounce_route.next_agent == "planner"
    assert bounce_route.status == "verified_fail"
    second_bounce = build_bounce_handoff(
        claim_content=claim,
        result=second,
        implementer="backend",
        route=bounce_route,
        bounce_count=2,
    )
    assert route(second_bounce).route == "planner"
    assert f"The Verification command `{FAIL_COMMAND}` failed again" in second_bounce
    assert "did not carry a usable `## Work Packet`" not in second_bounce
    assert parse_work_packet(second_bounce).verification_commands == (FAIL_COMMAND,)


def test_verify_work_claim_returns_a_repeated_failure_with_new_work_to_the_implementer(
    tmp_path: Path,
) -> None:
    assignment = _assignment(FAIL_COMMAND)
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    first = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)
    bounce = _bounce_for(tmp_path, _claim(head), first)

    retry_state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 3\n")
    retry_head = _commit_paths(tmp_path, "try again", "src/app.py")
    second = _verify(
        tmp_path, assignment=bounce, claim=_claim(retry_head), state=retry_state
    )

    assert second.failed_prefixes == ("verification_failed",)
    assert second.repeated_verification_failure is False
    assert bounce_route_for(second, implementer="backend").next_agent == "backend"


def test_verify_work_claim_returns_a_repeated_failure_with_uncommitted_work_to_the_implementer(
    tmp_path: Path,
) -> None:
    assignment = _assignment(FAIL_COMMAND)
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    first = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)
    bounce = _bounce_for(tmp_path, _claim(head), first)

    retry_state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 3\n")
    claim = _claim(head)
    _write(tmp_path / HANDOFF_RELATIVE_PATH, claim)
    second = _verify(tmp_path, assignment=bounce, claim=claim, state=retry_state)

    assert second.failed_prefixes == ("uncommitted_changes", "verification_failed")
    assert second.repeated_verification_failure is False
    assert bounce_route_for(second, implementer="backend").next_agent == "backend"


def test_verify_work_claim_returns_a_different_failing_command_to_the_implementer(
    tmp_path: Path,
) -> None:
    other_fail = FAIL_COMMAND.replace("sys.exit(3)", "sys.exit(4)")
    assignment = _assignment(FAIL_COMMAND)
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    first = _verify(tmp_path, assignment=assignment, claim=_claim(head), state=state)
    bounce = _bounce_for(tmp_path, _claim(head), first).replace(
        f"- **Verification command:** {FAIL_COMMAND}",
        f"- **Verification command:** {other_fail}",
    )
    assert parse_work_packet(bounce).verification_commands == (other_fail,)

    retry_state = capture_pre_dispatch_state(tmp_path)
    claim = _claim(head)
    _write(tmp_path / HANDOFF_RELATIVE_PATH, claim)
    second = _verify(tmp_path, assignment=bounce, claim=claim, state=retry_state)

    assert second.failed_prefixes == ("verification_failed",)
    assert second.repeated_verification_failure is False
    assert bounce_route_for(second, implementer="backend").next_agent == "backend"


def test_dispatcher_commit_includes_in_scope_files_inside_out_of_bounds_directories(
    tmp_path: Path,
) -> None:
    assignment = _assignment(
        files_in_scope="`src/app.py`, `tests/test_app.py`",
        files_out_of_bounds=PROSE_OUT_OF_BOUNDS,
    )
    _init_repo(tmp_path, assignment)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    pass\n")
    _write(tmp_path / "src" / "api.py", "API = 2\n")

    result = commit_implementer_changes(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=assignment,
        claim_content=_pending_claim(),
        pre_dispatch_state=state,
        role="backend",
    )

    assert result.committed == ("src/app.py", "tests/test_app.py")
    assert result.left_uncommitted == ("src/api.py",)
    verify = _verify(
        tmp_path,
        assignment=assignment,
        claim=_claim(result.sha),
        state=state,
    )
    assert verify.failed_prefixes == ("scope_violation",)
    assert "src/api.py (listed under Files out of bounds)" in verify.errors[0]


def test_dispatcher_commit_commits_work_the_previous_story_dispatch_left_behind(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    first_state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    leftovers = capture_implementer_leftovers(
        tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        pre_dispatch_state=first_state,
        story_key=STORY_ID,
    )

    second_state = capture_pre_dispatch_state(
        tmp_path, leftovers=leftovers, story_key=STORY_ID
    )
    result = commit_implementer_changes(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(),
        claim_content=_pending_claim(),
        pre_dispatch_state=second_state,
        role="backend",
    )

    assert second_state.carried_over == ("src/app.py",)
    assert second_state.dirty_files == ()
    assert result.committed == ("src/app.py",)


def test_carried_over_work_left_uncommitted_fails_the_gate(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    first_state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    leftovers = capture_implementer_leftovers(
        tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        pre_dispatch_state=first_state,
        story_key=STORY_ID,
    )
    second_state = capture_pre_dispatch_state(
        tmp_path, leftovers=leftovers, story_key=STORY_ID
    )

    # Before the carry-over, an untouched pre-dispatch file was ignored and a
    # claim whose scope_sha lacked the story's code passed the gate.
    result = _verify(
        tmp_path, assignment=_assignment(), claim=_claim(head), state=second_state
    )

    assert "uncommitted_changes" in result.failed_prefixes
    assert "src/app.py" in " ".join(result.errors)


def test_pre_dispatch_state_does_not_carry_over_edited_or_other_story_files(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    first_state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    leftovers = capture_implementer_leftovers(
        tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        pre_dispatch_state=first_state,
        story_key=STORY_ID,
    )
    _write(tmp_path / "src" / "api.py", "API = 'operator edit'\n")

    same_story = capture_pre_dispatch_state(
        tmp_path, leftovers=leftovers, story_key=STORY_ID
    )
    other_story = capture_pre_dispatch_state(
        tmp_path, leftovers=leftovers, story_key="EPIC-1-S2"
    )

    assert same_story.carried_over == ("src/app.py",)
    assert same_story.dirty_files == ("src/api.py",)
    assert other_story.carried_over == ()
    assert other_story.dirty_files == ("src/api.py", "src/app.py")


def test_carry_over_key_falls_back_to_the_work_packet_without_story_id() -> None:
    assignment = _assignment()
    without_story = assignment.replace(f"story_id: {STORY_ID}\n", "")
    other_packet = without_story.replace("Set VALUE to 2.", "Set VALUE to 3.")

    assert carry_over_key(assignment) == f"story:{STORY_ID}"
    packet_key = carry_over_key(without_story)
    assert packet_key is not None and packet_key.startswith("work-packet:")
    # A bounce carries the Work Packet verbatim under a new handoff body.
    bounced = without_story.replace("## Task Assignment", "## Dispatcher Verification")
    assert carry_over_key(bounced) == packet_key
    assert carry_over_key(other_packet) != packet_key
    no_packet = _assignment_without_packet().replace(f"story_id: {STORY_ID}\n", "")
    assert carry_over_key(no_packet) is None


def test_implementer_leftovers_exclude_operator_edits_and_the_handoff(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "PROJECT_STATE.md", "# PROJECT STATE\noperator note\n")
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _pending_claim())

    leftovers = capture_implementer_leftovers(
        tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        pre_dispatch_state=state,
        story_key=STORY_ID,
    )

    assert [path for path, _ in leftovers.fingerprints] == ["src/app.py"]


def test_stamp_commit_sha_fills_scope_sha_and_evidence() -> None:
    sha = "a" * 40

    stamped = stamp_commit_sha(_pending_claim(), sha)

    frontmatter = parse_handoff_frontmatter_text(stamped)
    assert frontmatter is not None
    assert frontmatter.scope_sha == sha
    assert f"- **Commit SHA verified:** {sha}" in stamped
    assert PENDING_COMMIT_SHA not in stamped


@pytest.mark.parametrize(
    "marker", [PENDING_COMMIT_SHA, f"`{PENDING_COMMIT_SHA}`"]
)
def test_stamp_commit_sha_keeps_the_text_after_the_marker(marker: str) -> None:
    sha = "a" * 40
    claim = _pending_claim().replace(
        f"- **Commit SHA verified:** {PENDING_COMMIT_SHA}",
        f"- **Commit SHA verified:** {marker} (plus operator commit 1234567,\n"
        "  which only touched docs)",
    )

    stamped = stamp_commit_sha(claim, sha)

    assert (
        f"- **Commit SHA verified:** {sha} (plus operator commit 1234567,\n"
        "  which only touched docs)"
    ) in stamped
    assert PENDING_COMMIT_SHA not in stamped


def test_stamp_commit_sha_accepts_a_parenthetical_label_note() -> None:
    sha = "a" * 40
    claim = _pending_claim().replace(
        "- **Commit SHA verified:**", "- **Commit SHA verified (this turn):**"
    )

    stamped = stamp_commit_sha(claim, sha)

    assert f"- **Commit SHA verified (this turn):** {sha}" in stamped
    assert evidence_commit_value(stamped) == sha


def test_stamp_commit_sha_replaces_an_existing_scope_sha() -> None:
    stamped = stamp_commit_sha(_claim("b" * 40), "c" * 40)

    frontmatter = parse_handoff_frontmatter_text(stamped)
    assert frontmatter is not None
    assert frontmatter.scope_sha == "c" * 40
    assert stamped.count("scope_sha:") == 1


def test_work_claim_require_new_commit_rejects_an_empty_claim(tmp_path: Path) -> None:
    base = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)

    result = verify_work_claim(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(),
        claim_content=_claim(base),
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        require_new_commit=True,
    )

    assert not result.passed
    assert "no_changes" in result.failed_prefixes
    assert bounce_route_for(result, implementer="backend").next_agent == "backend"


def test_dispatcher_commit_mode_requires_the_work_claim_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires enforce_work_packet"):
        DispatchConfig(
            repo_root=tmp_path,
            enforce_work_packet=False,
            commit_mode="dispatcher",
        )


def test_run_loop_commits_for_the_implementer_in_dispatcher_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    base = _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    instructions: list[object] = []
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **kwargs: object) -> DispatchResult:
        instructions.append(kwargs.get("additional_instruction"))
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    exit_code = orchestrator.run_loop(
        config, max_cycles=1, log=_collecting_log(messages)
    )

    assert exit_code == 0
    head = _git(tmp_path, "rev-parse", "HEAD")
    assert head != base
    assert _commit_files(tmp_path, head) == ["src/app.py"]
    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    frontmatter = parse_handoff_frontmatter_text(handoff)
    assert frontmatter is not None
    assert frontmatter.scope_sha == head
    assert f"- **Commit SHA verified:** {head}" in handoff
    assert instructions and "Dispatcher commit mode is on" in str(instructions[0])
    assert ("AGENT", "Work-claim gate PASSED for backend.") in messages


def test_run_loop_bounces_out_of_scope_leftovers_in_dispatcher_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        _write(tmp_path / "src" / "api.py", "API = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    bounced = parse_handoff_frontmatter_text(
        config.handoff_full_path.read_text(encoding="utf-8")
    )
    assert bounced is not None
    assert bounced.next_agent == "backend"
    assert bounced.status == "verified_fail"
    assert any(
        level == "WARN" and "src/api.py" in message for level, message in messages
    )
    assert any(
        level == "ERROR"
        and message.startswith("scope_violation:")
        and "src/api.py (listed under Files out of bounds)" in message
        for level, message in messages
    )
    assert not any("commit them" in message for _level, message in messages)


def _empty_scope_sha_claim() -> str:
    """A dispatcher-mode handback whose scope_sha the dispatcher never filled."""

    return _pending_claim().replace(
        "producer: backend", "scope_sha:\nclose_type: story\nproducer: backend"
    )


def test_verify_work_claim_reports_an_uncommitted_out_of_bounds_file_as_scope(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_paths(tmp_path, "set value", "src/app.py")
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    _write(tmp_path / "PROJECT_STATE.md", "# edited\n")

    result = _verify(tmp_path, assignment=_assignment(), claim=_claim(head), state=state)

    assert result.failed_prefixes == ("scope_violation",)
    assert _outcomes(result)["worktree"] == "FAIL"
    assert "PROJECT_STATE.md (outside Files in scope)" in result.errors[0]
    assert "src/api.py (listed under Files out of bounds)" in result.errors[0]
    assert "revert them" in result.errors[0]
    assert "commit them" not in result.errors[0]


def test_work_claim_in_dispatcher_mode_never_asks_for_a_commit(
    tmp_path: Path,
) -> None:
    # The W1.6 retries: the only change was out of bounds, so the dispatcher
    # committed nothing and left scope_sha empty.
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    claim = _empty_scope_sha_claim()

    result = verify_work_claim(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(),
        claim_content=claim,
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        require_new_commit=True,
        dispatcher_commit_mode=True,
    )

    assert result.failed_prefixes == ("no_changes", "scope_violation")
    assert _outcomes(result)["scope_sha"] == "SKIP"
    bounce = build_bounce_handoff(
        claim_content=claim,
        result=result,
        implementer="backend",
        route=bounce_route_for(result, implementer="backend"),
        bounce_count=1,
    )
    assert "leave the fix uncommitted with `scope_sha` empty" in bounce
    assert "commit the fix" not in bounce
    assert "commit them" not in bounce


def test_work_claim_in_dispatcher_mode_still_requires_scope_sha_for_agent_commits(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _commit_all(tmp_path, "agent committed despite dispatcher mode")

    result = verify_work_claim(
        repo_root=tmp_path,
        handoff_path=HANDOFF_RELATIVE_PATH,
        assignment_content=_assignment(),
        claim_content=_empty_scope_sha_claim(),
        pre_dispatch_state=state,
        verification_timeout_seconds=60,
        require_new_commit=True,
        dispatcher_commit_mode=True,
    )

    assert _outcomes(result)["scope_sha"] == "FAIL"
    assert "scope_sha_not_head" in result.failed_prefixes


def test_work_claim_counts_earlier_story_commits_for_a_revert_only_retry(
    tmp_path: Path,
) -> None:
    story_base = _init_repo(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_paths(tmp_path, "first dispatch", "src/app.py")
    state = capture_pre_dispatch_state(tmp_path)

    def verify(base: str | None):
        return verify_work_claim(
            repo_root=tmp_path,
            handoff_path=HANDOFF_RELATIVE_PATH,
            assignment_content=_assignment(),
            claim_content=_claim(head),
            pre_dispatch_state=state,
            verification_timeout_seconds=60,
            require_new_commit=True,
            dispatcher_commit_mode=True,
            story_base_sha=base,
        )

    assert verify(story_base).passed
    assert verify(None).failed_prefixes == ("no_changes",)


def test_run_loop_passes_a_retry_that_only_reverts_a_forbidden_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    dispatches: list[int] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append(1)
        if len(dispatches) == 1:
            _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
            _write(tmp_path / "src" / "api.py", "API = 2\n")
        else:
            _write(tmp_path / "src" / "api.py", "API = 1\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=2, log=_collecting_log(messages))

    assert len(dispatches) == 2
    head = _git(tmp_path, "rev-parse", "HEAD")
    assert _commit_files(tmp_path, head) == ["src/app.py"]
    assert any(
        level == "ERROR" and message.startswith("scope_violation:")
        for level, message in messages
    )
    assert any(
        "nothing new to commit; recorded the story's earlier commit" in message
        for _level, message in messages
    )
    assert ("AGENT", "Work-claim gate PASSED for backend.") in messages
    frontmatter = parse_handoff_frontmatter_text(
        config.handoff_full_path.read_text(encoding="utf-8")
    )
    assert frontmatter is not None
    assert frontmatter.scope_sha == head


def test_run_loop_skips_handoff_repair_when_the_dispatcher_committed_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    base = _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    dispatches: list[str] = []

    def fake_backend(path: Path, **kwargs: object) -> DispatchResult:
        dispatches.append(str(kwargs.get("additional_instruction") or ""))
        _write(tmp_path / "src" / "api.py", "API = 2\n")
        path.write_text(_empty_scope_sha_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert len(dispatches) == 1, "the handoff repair should not be dispatched"
    assert _git(tmp_path, "rev-parse", "HEAD") == base
    assert any(
        level == "AGENT" and "skipping the handoff repair" in message
        for level, message in messages
    )
    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    bounced = parse_handoff_frontmatter_text(handoff)
    assert bounced is not None
    assert bounced.next_agent == "backend"
    assert bounced.status == "verified_fail"
    assert "- **Failed checks:** no_changes, scope_violation" in handoff


REASON_LINE = 'reason: "Backend work complete; audit requested."\n'


def test_run_loop_handoff_repair_keeps_the_handoff_uncommitted_in_dispatcher_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    repair_prompts: list[str] = []

    def fake_backend(path: Path, **kwargs: object) -> DispatchResult:
        instruction = str(kwargs.get("additional_instruction") or "")
        if "Repair only" not in instruction:
            _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
            path.write_text(_pending_claim().replace(REASON_LINE, ""), encoding="utf-8")
            return _dispatch_result()
        repair_prompts.append(instruction)
        stamped = path.read_text(encoding="utf-8")
        path.write_text(
            stamped.replace("epic_id:", REASON_LINE + "epic_id:", 1),
            encoding="utf-8",
        )
        # The repair ignores the prompt and commits the handoff anyway.
        _git(tmp_path, "add", "--", HANDOFF_RELATIVE_PATH.as_posix())
        _git(tmp_path, "commit", "-q", "-m", "repair handoff")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert len(repair_prompts) == 1, " | ".join(
        f"{level} {message}" for level, message in messages[22:]
    )
    assert "leave docs/handoff/HANDOFF.md uncommitted" in repair_prompts[0]
    assert "Commit exactly" not in repair_prompts[0]
    assert _commit_files(tmp_path, "HEAD") == ["src/app.py"]
    assert _git(tmp_path, "diff", "--name-only") == HANDOFF_RELATIVE_PATH.as_posix()
    assert _git(tmp_path, "diff", "--cached", "--name-only") == ""
    assert any(
        level == "WARN" and "handoff repair committed the handoff" in message
        for level, message in messages
    )
    assert ("AGENT", "Producer handoff hygiene repair passed post-checks.") in messages


def test_git_dirty_files_keeps_the_first_porcelain_path_whole(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    # An unstaged edit lists as " M path"; stripping the whole porcelain output
    # used to eat that leading space and the path's first character.
    _write(tmp_path / "AGENTS.md", "edited\n")
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")

    assert orchestrator._git_dirty_files(tmp_path) == ("AGENTS.md", "src/app.py")


def test_git_head_and_dirty_files_matches_the_separate_lookups(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    _git(tmp_path, "mv", "src/api.py", "src/api moved.py")
    _git(tmp_path, "rm", "-q", "--cached", "AGENTS.md")
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / "new dir" / "notes with spaces.md", "untracked\n")

    expected = (
        "AGENTS.md",
        "new dir/notes with spaces.md",
        "src/api moved.py",
        "src/app.py",
    )
    assert git_dirty_files(tmp_path) == expected
    assert git_head_and_dirty_files(tmp_path) == (head, expected)
    assert git_head(tmp_path) == head


def test_git_head_and_dirty_files_reports_no_head_on_an_unborn_branch(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "README.md", "untracked\n")

    assert git_head(tmp_path) is None
    assert git_head_and_dirty_files(tmp_path) == (None, ("README.md",))


def test_git_head_and_dirty_files_outside_a_repository(tmp_path: Path) -> None:
    assert git_head_and_dirty_files(tmp_path / "missing") == (None, None)


STORY_ID_LINE = f"story_id: {STORY_ID}\n"


@pytest.mark.parametrize("with_story_id", [True, False])
def test_run_loop_commits_work_a_bounced_dispatch_left_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    with_story_id: bool,
) -> None:
    # Replays the W1.5 self-host stall: the first commit dropped an in-scope
    # file, the gate bounced to backend, and the retry could never commit it
    # because it was already dirty before the retry's dispatch. story_id is
    # optional, so carry-over must also work without one.
    def strip(content: str) -> str:
        return content if with_story_id else content.replace(STORY_ID_LINE, "")

    _init_repo(tmp_path, strip(_assignment()))
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    dispatches: list[int] = []
    real_commit = orchestrator.commit_implementer_changes

    def dropping_commit(**kwargs: Any):
        if len(dispatches) == 1:
            kwargs["assignment_content"] = _assignment(
                files_in_scope="tests/test_app.py"
            )
        return real_commit(**kwargs)

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append(1)
        if len(dispatches) == 1:
            _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
            _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    pass\n")
        path.write_text(strip(_pending_claim()), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "commit_implementer_changes", dropping_commit)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=2, log=_collecting_log(messages))

    assert len(dispatches) == 2
    assert _commit_files(tmp_path, "HEAD") == ["src/app.py"]
    assert "src/app.py" not in _git(tmp_path, "status", "--porcelain")
    label = STORY_ID if with_story_id else "same Work Packet"
    carried = (
        f"Carrying over uncommitted work from the previous {label} "
        "dispatch: src/app.py."
    )
    assert ("INFO", carried) in messages
    assert ("AGENT", "Work-claim gate PASSED for backend.") in messages


def test_run_loop_carries_over_leftovers_across_a_dispatcher_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    # The same stall as above, but the dispatcher stops between the two
    # dispatches. The leftovers must come back from logs/dispatch/.
    _init_repo(tmp_path)
    _write(tmp_path / ".gitignore", "logs/\n")
    _commit_all(tmp_path, "ignore runtime state")
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    state_path = tmp_path / "logs" / "dispatch" / "carry-over.json"
    messages: list[tuple[str, str]] = []
    dispatches: list[int] = []
    real_commit = orchestrator.commit_implementer_changes

    def dropping_commit(**kwargs: Any):
        if len(dispatches) == 1:
            kwargs["assignment_content"] = _assignment(
                files_in_scope="tests/test_app.py"
            )
        return real_commit(**kwargs)

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append(1)
        if len(dispatches) == 1:
            _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
            _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    pass\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "commit_implementer_changes", dropping_commit)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    # The committed file left the record; the dropped one stays.
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["story_key"] == f"story:{STORY_ID}"
    assert list(saved["fingerprints"]) == ["src/app.py"]

    # A record read from disk applies only after the operator confirms it.
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(
        orchestrator, "_operator_input_stream", lambda: io.StringIO("y\n")
    )
    messages.clear()
    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert len(dispatches) == 2
    assert any(
        level == "INFO"
        and message.startswith("Restored carry-over record from")
        and "src/app.py" in message
        for level, message in messages
    )
    assert ("INFO", f"Carry these over as {STORY_ID}'s work? [y/N]") in messages
    assert (
        "INFO",
        f"Carrying over uncommitted work from the previous {STORY_ID} "
        "dispatch: src/app.py.",
    ) in messages
    assert _commit_files(tmp_path, "HEAD") == ["src/app.py"]
    assert ("AGENT", "Work-claim gate PASSED for backend.") in messages
    # Everything was committed, so nothing is left to carry over.
    assert not state_path.exists()


def test_run_loop_keeps_carry_over_in_memory_when_git_does_not_ignore_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    # An untracked record Git does not ignore would show up in the next gate.
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "api.py", "API = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert not (tmp_path / "logs" / "dispatch" / "carry-over.json").exists()
    assert any(
        level == "WARN"
        and "Git does not ignore logs/dispatch/carry-over.json" in message
        for level, message in messages
    )


def _save_app_carry_over_record(repo_root: Path) -> Path:
    """Leave src/app.py dirty and record it as the story's leftover on disk."""

    _write(repo_root / ".gitignore", "logs/\n")
    _commit_all(repo_root, "ignore runtime state")
    _write(repo_root / "src" / "app.py", "VALUE = 2\n")
    state_path = repo_root / "logs" / "dispatch" / "carry-over.json"
    save_implementer_leftovers(
        state_path,
        ImplementerLeftovers(
            story_key=f"story:{STORY_ID}",
            fingerprints=(
                ("src/app.py", working_tree_fingerprint(repo_root, "src/app.py")),
            ),
        ),
    )
    return state_path


def _no_input_stream() -> io.StringIO:
    raise AssertionError("the carry-over record must not read operator input")


@pytest.mark.parametrize(
    ("interactive", "operator_input"),
    [
        pytest.param(False, None, id="no_tty"),
        pytest.param(True, "n\n", id="tty_declines"),
        pytest.param(True, "", id="tty_eof"),
    ],
)
def test_run_loop_discards_an_unconfirmed_carry_over_record_from_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    interactive: bool,
    operator_input: str | None,
) -> None:
    # Roles can write under logs/, so a record read from disk could be forged
    # to claim operator edits as implementer work.
    _init_repo(tmp_path)
    state_path = _save_app_carry_over_record(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    seen_states: list[Any] = []
    real_capture = orchestrator.capture_pre_dispatch_state

    def recording_capture(*args: Any, **kwargs: Any):
        state = real_capture(*args, **kwargs)
        seen_states.append(state)
        return state

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "tests" / "test_app.py", "def test_value():\n    pass\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

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
    monkeypatch.setattr(orchestrator, "capture_pre_dispatch_state", recording_capture)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    asked = ("INFO", f"Carry these over as {STORY_ID}'s work? [y/N]") in messages
    assert asked == interactive
    assert any(
        level == "WARN"
        and "carry-over record read from" in message
        and STORY_ID in message
        and "src/app.py" in message
        and "not confirmed at a terminal" in message
        and "stay operator edits" in message
        for level, message in messages
    )
    assert not state_path.exists()
    assert seen_states[0].carried_over == ()
    assert "src/app.py" in seen_states[0].dirty_files
    assert "src/app.py" not in _commit_files(tmp_path, "HEAD")
    assert "src/app.py" in _git(tmp_path, "status", "--porcelain")


def test_carry_over_record_is_discarded_without_input_when_interactive_pause_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    state_path = _save_app_carry_over_record(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path, commit_mode="dispatcher", interactive_pause=False
    )
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", _no_input_stream)

    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert restored is None
    assert not state_path.exists()
    assert not any("[y/N]" in message for _level, message in messages)
    assert any(
        level == "WARN" and "not confirmed at a terminal" in message
        for level, message in messages
    )


def test_carry_over_record_is_kept_and_not_applied_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    state_path = _save_app_carry_over_record(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher", dry_run=True)
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", _no_input_stream)

    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert restored is None
    assert state_path.exists()
    assert len(messages) == 1
    level, message = messages[0]
    assert level == "INFO"
    assert str(state_path) in message
    assert STORY_ID in message
    assert "src/app.py" in message
    assert "not applied in dry run" in message.lower()


def test_stale_carry_over_record_is_deleted_without_asking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    state_path = _save_app_carry_over_record(tmp_path)
    # The file changed after the record was written, so it no longer matches.
    _write(tmp_path / "src" / "app.py", "VALUE = 'operator edit'\n")
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", _no_input_stream)

    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert restored is None
    assert not state_path.exists()
    assert messages == []


def test_carry_over_prompt_escapes_control_characters_from_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    state_path = tmp_path / "logs" / "dispatch" / "carry-over.json"
    # Filesystems reject such paths, so the forged record skips pruning.
    forged = ImplementerLeftovers(
        story_key="story:EVIL\x1b[2J\nWARN fake line",
        fingerprints=(("src/evil\x1b]0;x\x07\n.py", "0" * 64),),
    )
    save_implementer_leftovers(state_path, forged)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        orchestrator, "prune_implementer_leftovers", lambda _root, record: record
    )
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(
        orchestrator, "_operator_input_stream", lambda: io.StringIO("n\n")
    )

    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert restored is None
    assert len(messages) == 3
    for _level, message in messages:
        assert not any(unicodedata.category(char) == "Cc" for char in message)
    assert (
        "INFO",
        "Carry these over as EVIL\\x1b[2J\\x0aWARN fake line's work? [y/N]",
    ) in messages
    assert "src/evil\\x1b]0;x\\x07\\x0a.py" in messages[0][1]
    assert "src/evil\\x1b]0;x\\x07\\x0a.py" in messages[2][1]


def test_carry_over_story_display_names_the_work_packet_or_the_raw_key() -> None:
    orchestrator = _load_orchestrator()
    packet = ImplementerLeftovers(
        story_key="work-packet:" + "abcdef0123456789" * 4,
        fingerprints=(("src/app.py", "x"),),
    )
    other = ImplementerLeftovers(story_key="custom-key", fingerprints=packet.fingerprints)

    assert orchestrator._carry_over_display(packet) == (
        "the Work Packet abcdef012345",
        "src/app.py",
    )
    assert orchestrator._carry_over_display(other) == ("custom-key", "src/app.py")


def _link_directory(link: Path, target: Path) -> None:
    """Point ``link`` at ``target`` with a symlink, or a junction on Windows."""

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


def _external_carry_over_record(directory: Path, *, matching: bool, repo: Path) -> Path:
    record_path = directory / "carry-over.json"
    fingerprint = (
        working_tree_fingerprint(repo, "src/app.py") if matching else "0" * 64
    )
    save_implementer_leftovers(
        record_path,
        ImplementerLeftovers(
            story_key=f"story:{STORY_ID}", fingerprints=(("src/app.py", fingerprint),)
        ),
    )
    return record_path


@pytest.mark.parametrize(
    "matching",
    [pytest.param(False, id="stale_record"), pytest.param(True, id="live_record")],
)
def test_carry_over_record_behind_a_directory_link_is_not_read_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matching: bool,
) -> None:
    # A role that can write under logs/ could link logs/dispatch at a file
    # outside the repository; startup must not delete that target.
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    external = _external_carry_over_record(outside, matching=matching, repo=repo)
    _link_directory(repo / "logs" / "dispatch", outside)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(orchestrator, "_operator_input_stream", _no_input_stream)

    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert restored is None
    assert external.exists()
    assert len(messages) == 1
    level, message = messages[0]
    assert level == "WARN"
    assert "symlink or junction" in message
    assert "stay operator edits" in message


def test_carry_over_record_that_is_a_file_symlink_is_not_read_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    external = _external_carry_over_record(outside, matching=False, repo=repo)
    link = repo / "logs" / "dispatch" / "carry-over.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(external, link)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a file symlink here")
    orchestrator = _load_orchestrator()
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: False)

    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert restored is None
    assert external.exists()
    assert link.is_symlink()
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


@pytest.mark.parametrize("error", [RuntimeError, OSError])
def test_carry_over_path_that_cannot_be_resolved_is_treated_as_a_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    # Python 3.11/3.12 raise RuntimeError on a symlink loop; startup must warn
    # and carry on instead of crashing.
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    orchestrator = _load_orchestrator()
    config = _dispatch_config(repo, commit_mode="dispatcher")
    record_path = repo / "logs" / "dispatch" / "carry-over.json"
    save_implementer_leftovers(
        record_path,
        ImplementerLeftovers(
            story_key=f"story:{STORY_ID}",
            fingerprints=(("src/app.py", working_tree_fingerprint(repo, "src/app.py")),),
        ),
    )
    original_resolve = Path.resolve

    def failing_resolve(self: Path, strict: bool = False) -> Path:
        if self.name == "carry-over.json":
            raise error("cannot resolve")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", failing_resolve)
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: False)
    messages: list[tuple[str, str]] = []

    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )
    orchestrator._persist_implementer_leftovers(config, None, _collecting_log(messages))

    assert restored is None
    assert record_path.exists()
    assert [level for level, _message in messages] == ["WARN", "WARN"]
    assert all("symlink or junction" in message for _level, message in messages)


def test_carry_over_record_in_a_symlink_loop_does_not_crash_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    dispatch_dir = repo / "logs" / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    link = dispatch_dir / "carry-over.json"
    try:
        os.symlink(dispatch_dir / "loop.json", link)
        os.symlink(link, dispatch_dir / "loop.json")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a file symlink here")
    orchestrator = _load_orchestrator()
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: False)

    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert restored is None
    assert link.is_symlink()
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


def test_persisting_carry_over_does_not_write_or_delete_through_a_link(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _write(repo / ".gitignore", "logs/\n")
    _commit_all(repo, "ignore runtime state")
    _write(repo / "src" / "app.py", "VALUE = 2\n")
    external = _external_carry_over_record(outside, matching=True, repo=repo)
    original = external.read_text(encoding="utf-8")
    _link_directory(repo / "logs" / "dispatch", outside)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    other = ImplementerLeftovers(
        story_key="story:EPIC-1-S2",
        fingerprints=(("src/app.py", working_tree_fingerprint(repo, "src/app.py")),),
    )

    orchestrator._persist_implementer_leftovers(config, other, _collecting_log(messages))
    assert external.read_text(encoding="utf-8") == original
    orchestrator._persist_implementer_leftovers(config, None, _collecting_log(messages))
    assert external.read_text(encoding="utf-8") == original
    assert not (outside / "carry-over.json.tmp").exists()
    assert [level for level, _message in messages] == ["WARN", "WARN"]
    assert all("symlink or junction" in message for _level, message in messages)


_CARRY_OVER_WRITERS = [
    pytest.param(False, id="checked_paths"),
    pytest.param(
        True,
        id="directory_fds",
        marks=pytest.mark.skipif(
            sys.platform == "win32", reason="Windows has no dir_fd support"
        ),
    ),
]


def _ignored_logs_repo(repo: Path) -> None:
    _init_repo(repo)
    _write(repo / ".gitignore", "logs/\n")
    _commit_all(repo, "ignore runtime state")
    _write(repo / "src" / "app.py", "VALUE = 2\n")


def _app_leftovers(repo: Path) -> ImplementerLeftovers:
    return ImplementerLeftovers(
        story_key=f"story:{STORY_ID}",
        fingerprints=(("src/app.py", working_tree_fingerprint(repo, "src/app.py")),),
    )


def _select_carry_over_writer(
    orchestrator: Any, monkeypatch: pytest.MonkeyPatch, *, use_dir_fd: bool
) -> None:
    """Select the descriptor writer or the pinned checked-path writer.

    The checked-path writer runs only inside the Windows directory pin. On
    Windows the real pin is used; elsewhere a stub pin that creates and
    yields ``logs/dispatch`` stands in, so the writer's own checks still run.
    """

    monkeypatch.setattr(orchestrator, "_CARRY_OVER_DIR_FD_SUPPORTED", use_dir_fd)
    if use_dir_fd or sys.platform == "win32":
        return

    @contextlib.contextmanager
    def stub_pin(repo_root: Path, *, create: bool):
        directory = repo_root / "logs" / "dispatch"
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        elif not directory.is_dir():
            yield None
            return
        yield orchestrator._PinnedCarryOverDirectory(directory, None)

    monkeypatch.setattr(orchestrator, "_CARRY_OVER_WINDOWS_PIN_SUPPORTED", True)
    monkeypatch.setattr(orchestrator, "_pinned_carry_over_directories", stub_pin)


@pytest.mark.parametrize("use_dir_fd", _CARRY_OVER_WRITERS)
def test_persisting_carry_over_never_uses_the_fixed_temporary_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_dir_fd: bool,
) -> None:
    # A role could plant carry-over.json.tmp; the writer must not open it.
    repo = tmp_path / "repo"
    _ignored_logs_repo(repo)
    planted = repo / "logs" / "dispatch" / "carry-over.json.tmp"
    _write(planted, "planted\n")
    orchestrator = _load_orchestrator()
    _select_carry_over_writer(orchestrator, monkeypatch, use_dir_fd=use_dir_fd)
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    leftovers = _app_leftovers(repo)

    orchestrator._persist_implementer_leftovers(
        config, leftovers, _collecting_log(messages)
    )

    assert messages == []
    assert planted.read_text(encoding="utf-8") == "planted\n"
    assert load_implementer_leftovers(planted.parent / "carry-over.json") == leftovers
    leftover_names = sorted(path.name for path in planted.parent.iterdir())
    assert leftover_names == ["carry-over.json", "carry-over.json.tmp"]


@pytest.mark.parametrize("use_dir_fd", _CARRY_OVER_WRITERS)
def test_persisting_carry_over_does_not_write_through_a_temporary_file_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_dir_fd: bool,
) -> None:
    repo = tmp_path / "repo"
    _ignored_logs_repo(repo)
    external = tmp_path / "outside" / "victim.txt"
    _write(external, "do not overwrite\n")
    planted = repo / "logs" / "dispatch" / "carry-over.json.tmp"
    planted.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(external, planted)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a file symlink here")
    orchestrator = _load_orchestrator()
    _select_carry_over_writer(orchestrator, monkeypatch, use_dir_fd=use_dir_fd)
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    leftovers = _app_leftovers(repo)

    orchestrator._persist_implementer_leftovers(
        config, leftovers, _collecting_log(messages)
    )

    assert messages == []
    assert external.read_text(encoding="utf-8") == "do not overwrite\n"
    assert planted.is_symlink()
    assert load_implementer_leftovers(planted.parent / "carry-over.json") == leftovers


def test_checked_carry_over_writer_refuses_a_link_that_appears_mid_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The path is clean when _persist_implementer_leftovers checks it, then a
    # link appears; the writer must not rename onto the record.
    repo = tmp_path / "repo"
    _ignored_logs_repo(repo)
    orchestrator = _load_orchestrator()
    _select_carry_over_writer(orchestrator, monkeypatch, use_dir_fd=False)
    checks = {"count": 0}

    def link_after_first_check(_path: Path) -> bool:
        checks["count"] += 1
        return checks["count"] > 1

    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", link_after_first_check
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, _app_leftovers(repo), _collecting_log(messages)
    )

    dispatch_dir = repo / "logs" / "dispatch"
    assert not (dispatch_dir / "carry-over.json").exists()
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


def test_checked_carry_over_writer_removes_its_temporary_file_on_a_late_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A link reported only right before the rename leaves no temporary file.
    repo = tmp_path / "repo"
    _ignored_logs_repo(repo)
    orchestrator = _load_orchestrator()
    _select_carry_over_writer(orchestrator, monkeypatch, use_dir_fd=False)
    original = orchestrator._carry_over_path_traverses_link
    state = {"reported": False}

    def link_before_rename(path: Path) -> bool:
        if path.name == "carry-over.json" and (path.parent / "marker").exists():
            state["reported"] = True
            return True
        return original(path)

    original_handle_check = orchestrator._carry_over_handle_is_at

    def handle_check_then_mark(path: Path, file_fd: int) -> bool:
        result = original_handle_check(path, file_fd)
        (path.parent / "marker").write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", link_before_rename
    )
    monkeypatch.setattr(orchestrator, "_carry_over_handle_is_at", handle_check_then_mark)
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, _app_leftovers(repo), _collecting_log(messages)
    )

    dispatch_dir = repo / "logs" / "dispatch"
    assert state["reported"]
    assert sorted(path.name for path in dispatch_dir.iterdir()) == ["marker"]
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no dir_fd support")
def test_directory_fd_carry_over_writer_refuses_a_directory_link_that_appears_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even when the path check misses a linked logs/dispatch, the writer opens
    # each directory with O_NOFOLLOW and never writes outside the repository.
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    _ignored_logs_repo(repo)
    _link_directory(repo / "logs" / "dispatch", outside)
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(orchestrator, "_CARRY_OVER_DIR_FD_SUPPORTED", True)
    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", lambda _path: False
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, _app_leftovers(repo), _collecting_log(messages)
    )

    assert list(outside.iterdir()) == []
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


def test_carry_over_dir_fd_detection_checks_rename_not_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CPython lists renameat support under os.rename only; os.replace is never
    # in os.supports_dir_fd even where it accepts src_dir_fd/dst_dir_fd.
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0x20000), raising=False)
    monkeypatch.setattr(os, "O_DIRECTORY", getattr(os, "O_DIRECTORY", 0x10000), raising=False)
    monkeypatch.setattr(
        os, "supports_dir_fd", {os.open, os.mkdir, os.rename, os.unlink}
    )

    assert orchestrator._detect_carry_over_dir_fd_support() is True

    monkeypatch.setattr(os, "supports_dir_fd", {os.open, os.mkdir, os.unlink})

    assert orchestrator._detect_carry_over_dir_fd_support() is False


def test_carry_over_dir_fd_detection_matches_the_platform() -> None:
    # Without any monkeypatching, POSIX hosts must select the descriptor
    # writer and Windows must fall back to checked paths.
    orchestrator = _load_orchestrator()

    expected = sys.platform != "win32"
    assert orchestrator._detect_carry_over_dir_fd_support() is expected
    assert orchestrator._CARRY_OVER_DIR_FD_SUPPORTED is expected


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no dir_fd support")
def test_persisting_carry_over_selects_the_directory_fd_writer_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The capability flag is left as the module computed it.
    repo = tmp_path / "repo"
    _ignored_logs_repo(repo)
    orchestrator = _load_orchestrator()
    calls: list[str] = []
    original_dir_fd_writer = orchestrator._write_carry_over_record_with_dir_fd

    def spy_dir_fd_writer(repo_root: Path, payload: str) -> None:
        calls.append("dir_fd")
        original_dir_fd_writer(repo_root, payload)

    def refuse_checked_writer(_state_path: Path, _payload: str) -> None:
        raise AssertionError("checked-path writer selected on POSIX")

    monkeypatch.setattr(
        orchestrator, "_write_carry_over_record_with_dir_fd", spy_dir_fd_writer
    )
    monkeypatch.setattr(
        orchestrator, "_write_carry_over_record_checked", refuse_checked_writer
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    leftovers = _app_leftovers(repo)

    orchestrator._persist_implementer_leftovers(
        config, leftovers, _collecting_log(messages)
    )

    assert messages == []
    assert calls == ["dir_fd"]
    state_path = repo / "logs" / "dispatch" / "carry-over.json"
    assert load_implementer_leftovers(state_path) == leftovers


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no dir_fd support")
def test_directory_fd_carry_over_delete_refuses_a_directory_link_that_appears_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even when the path check misses a linked logs/dispatch, deletion opens
    # each directory with O_NOFOLLOW and never removes a file outside the repo.
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _ignored_logs_repo(repo)
    external_record = outside / "carry-over.json"
    _write(external_record, "{}\n")
    _link_directory(repo / "logs" / "dispatch", outside)
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", lambda _path: False
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, None, _collecting_log(messages)
    )
    orchestrator._delete_carry_over_record(config, _collecting_log(messages))

    assert external_record.read_text(encoding="utf-8") == "{}\n"
    assert [level for level, _message in messages] == ["WARN", "WARN"]
    assert all("symlink or junction" in message for _level, message in messages)


def test_carry_over_delete_without_a_dispatch_directory_is_quiet(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _ignored_logs_repo(repo)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, None, _collecting_log(messages)
    )
    orchestrator._delete_carry_over_record(config, _collecting_log(messages))

    assert messages == []
    assert not (repo / "logs").exists()


def test_carry_over_windows_pin_detection_matches_the_platform() -> None:
    orchestrator = _load_orchestrator()

    expected = sys.platform == "win32"
    assert orchestrator._detect_carry_over_windows_pin_support() is expected
    assert orchestrator._CARRY_OVER_WINDOWS_PIN_SUPPORTED is expected


_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="the directory pin uses CreateFileW"
)


@_WINDOWS_ONLY
def test_carry_over_pin_blocks_renaming_the_carry_over_directories(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    dispatch_dir = repo / "logs" / "dispatch"
    orchestrator = _load_orchestrator()

    with orchestrator._pinned_carry_over_directories(
        repo, create=True
    ) as directory:
        assert directory.path == dispatch_dir
        assert directory.handle is not None
        assert dispatch_dir.is_dir()
        with pytest.raises(OSError):
            os.rename(dispatch_dir, repo / "logs" / "moved")
        with pytest.raises(OSError):
            os.rename(repo / "logs", repo / "moved-logs")
        # Files inside the pinned directory can still be created and removed.
        _write(dispatch_dir / "probe.txt", "x\n")
        os.unlink(dispatch_dir / "probe.txt")

    # The handles are closed on exit, so the directories can move again.
    os.rename(dispatch_dir, repo / "logs" / "moved")
    assert (repo / "logs" / "moved").is_dir()


@_WINDOWS_ONLY
def test_carry_over_pin_without_create_yields_none_for_a_missing_directory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestrator = _load_orchestrator()

    with orchestrator._pinned_carry_over_directories(repo, create=False) as directory:
        assert directory is None
    (repo / "logs").mkdir()
    with orchestrator._pinned_carry_over_directories(repo, create=False) as directory:
        assert directory is None
    assert not (repo / "logs" / "dispatch").exists()


@_WINDOWS_ONLY
@pytest.mark.parametrize("linked", ["logs", "logs/dispatch"])
@pytest.mark.parametrize("create", [True, False])
def test_carry_over_pin_rejects_a_junction(
    tmp_path: Path,
    linked: str,
    create: bool,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (outside / "dispatch").mkdir(parents=True)
    repo.mkdir()
    _link_directory(repo / linked, outside)
    orchestrator = _load_orchestrator()

    with pytest.raises(orchestrator._CarryOverLinkError):
        with orchestrator._pinned_carry_over_directories(repo, create=create):
            raise AssertionError("the pin must not be entered through a junction")

    assert sorted(path.name for path in outside.iterdir()) == ["dispatch"]
    assert list((outside / "dispatch").iterdir()) == []


def _try_junction_swap(repo: Path, outside: Path, outcomes: list[str]) -> None:
    """Try to move ``logs/dispatch`` away and junction it to ``outside``."""

    dispatch_dir = repo / "logs" / "dispatch"
    try:
        os.rename(dispatch_dir, repo / "logs" / "moved")
    except OSError:
        outcomes.append("rename blocked")
        return
    _link_directory(dispatch_dir, outside)
    outcomes.append("swapped")


@_WINDOWS_ONLY
def test_carry_over_delete_survives_a_junction_swap_inside_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _ignored_logs_repo(repo)
    external_record = outside / "carry-over.json"
    _write(external_record, "{}\n")
    record = repo / "logs" / "dispatch" / "carry-over.json"
    save_implementer_leftovers(record, _app_leftovers(repo))
    orchestrator = _load_orchestrator()
    outcomes: list[str] = []
    real_unlink = orchestrator._unlink_carry_over_file_checked

    def swap_then_unlink(state_path: Path, directory_handle: int | None) -> None:
        _try_junction_swap(repo, outside, outcomes)
        real_unlink(state_path, directory_handle)

    monkeypatch.setattr(
        orchestrator, "_unlink_carry_over_file_checked", swap_then_unlink
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, None, _collecting_log(messages)
    )

    assert outcomes == ["rename blocked"]
    assert messages == []
    assert external_record.read_text(encoding="utf-8") == "{}\n"
    assert not record.exists()
    assert (repo / "logs" / "dispatch").is_dir()


@_WINDOWS_ONLY
def test_carry_over_write_survives_a_junction_swap_inside_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _ignored_logs_repo(repo)
    (repo / "logs" / "dispatch").mkdir(parents=True)
    external_record = outside / "carry-over.json"
    _write(external_record, "{}\n")
    orchestrator = _load_orchestrator()
    outcomes: list[str] = []
    real_checked_writer = orchestrator._write_carry_over_record_checked

    def swap_then_write(
        state_path: Path, payload: str, directory_handle: int | None
    ) -> None:
        _try_junction_swap(repo, outside, outcomes)
        real_checked_writer(state_path, payload, directory_handle)

    monkeypatch.setattr(
        orchestrator, "_write_carry_over_record_checked", swap_then_write
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    leftovers = _app_leftovers(repo)

    orchestrator._persist_implementer_leftovers(
        config, leftovers, _collecting_log(messages)
    )

    assert outcomes == ["rename blocked"]
    assert messages == []
    assert external_record.read_text(encoding="utf-8") == "{}\n"
    assert sorted(path.name for path in outside.iterdir()) == ["carry-over.json"]
    record = repo / "logs" / "dispatch" / "carry-over.json"
    assert load_implementer_leftovers(record) == leftovers


def _mount_directory_in_place(directory: Path, target: Path) -> None:
    """Turn the existing empty ``directory`` into a mount point to ``target``.

    Unlike ``_link_directory`` this does not rename or recreate the directory:
    it sets the reparse point on the directory itself with
    ``FSCTL_SET_REPARSE_POINT``, which needs only write-attributes access and
    so is not blocked by the carry-over directory pin.
    """

    import ctypes
    import struct
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    file_write_attributes = 0x0100
    share_all = 0x7
    open_existing = 3
    flags = 0x02000000 | 0x00200000  # backup semantics | open reparse point
    handle = kernel32.CreateFileW(
        str(directory), file_write_attributes, share_all, None, open_existing, flags, None
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        substitute = ("\\??\\" + str(target)).encode("utf-16-le")
        printed = str(target).encode("utf-16-le")
        data = (
            struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed))
            + substitute
            + b"\0\0"
            + printed
            + b"\0\0"
        )
        mount_point_tag = 0xA0000003
        raw = struct.pack("<IHH", mount_point_tag, len(data), 0) + data
        buffer = ctypes.create_string_buffer(raw)
        returned = wintypes.DWORD()
        fsctl_set_reparse_point = 0x000900A4
        if not kernel32.DeviceIoControl(
            handle,
            fsctl_set_reparse_point,
            buffer,
            len(raw),
            None,
            0,
            ctypes.byref(returned),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


@_WINDOWS_ONLY
def test_carry_over_delete_refuses_a_mount_point_set_in_place_inside_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The in-repo record is absent, so logs/dispatch is empty and can be made a
    # mount point without renaming it; the pin cannot stop that. The delete
    # must still not remove the external record the mount point exposes.
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _ignored_logs_repo(repo)
    dispatch_dir = repo / "logs" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    external_record = outside / "carry-over.json"
    _write(external_record, "{}\n")
    orchestrator = _load_orchestrator()
    outcomes: list[str] = []
    real_unlink = orchestrator._unlink_carry_over_file_checked

    def mount_then_unlink(state_path: Path, directory_handle: int | None) -> None:
        _mount_directory_in_place(dispatch_dir, outside)
        outcomes.append("mounted")
        assert state_path.exists()  # the literal path now reaches outside
        real_unlink(state_path, directory_handle)

    monkeypatch.setattr(
        orchestrator, "_unlink_carry_over_file_checked", mount_then_unlink
    )
    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", lambda _path: False
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, None, _collecting_log(messages)
    )

    assert outcomes == ["mounted"]
    assert external_record.read_text(encoding="utf-8") == "{}\n"
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


@_WINDOWS_ONLY
def test_carry_over_write_refuses_a_mount_point_set_in_place_inside_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _ignored_logs_repo(repo)
    dispatch_dir = repo / "logs" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    external_record = outside / "carry-over.json"
    _write(external_record, "{}\n")
    orchestrator = _load_orchestrator()
    outcomes: list[str] = []
    real_checked_writer = orchestrator._write_carry_over_record_checked

    def mount_then_write(
        state_path: Path, payload: str, directory_handle: int | None
    ) -> None:
        _mount_directory_in_place(dispatch_dir, outside)
        outcomes.append("mounted")
        real_checked_writer(state_path, payload, directory_handle)

    monkeypatch.setattr(
        orchestrator, "_write_carry_over_record_checked", mount_then_write
    )
    # Simulate the mount point appearing after every path-based check, so only
    # the handle checks stand between the writer and the external directory.
    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", lambda _path: False
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, _app_leftovers(repo), _collecting_log(messages)
    )

    assert outcomes == ["mounted"]
    assert external_record.read_text(encoding="utf-8") == "{}\n"
    assert sorted(path.name for path in outside.iterdir()) == ["carry-over.json"]
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


@_WINDOWS_ONLY
def test_carry_over_handle_check_rejects_a_file_outside_the_directory(
    tmp_path: Path,
) -> None:
    # The location check compares the handles' final paths, so a file reached
    # through a mount point is caught even when the directory handle itself
    # was opened before the mount point appeared.
    import ctypes

    repo = tmp_path / "repo"
    inside = repo / "logs" / "dispatch"
    outside = tmp_path / "outside"
    _write(inside / "carry-over.json", "{}\n")
    _write(outside / "carry-over.json", "{}\n")
    orchestrator = _load_orchestrator()
    kernel32 = orchestrator._carry_over_kernel32()

    def open_file(path: Path) -> int:
        handle = kernel32.CreateFileW(
            str(path), 0x0080, 0x7, None, 3, 0x02000000 | 0x00200000, None
        )
        assert handle not in (None, ctypes.c_void_p(-1).value)
        return handle

    with orchestrator._pinned_carry_over_directories(repo, create=False) as pinned:
        assert pinned is not None
        inside_handle = open_file(inside / "carry-over.json")
        outside_handle = open_file(outside / "carry-over.json")
        try:
            orchestrator._carry_over_require_handle_in_directory(
                inside_handle, pinned.handle, inside / "carry-over.json"
            )
            with pytest.raises(orchestrator._CarryOverLinkError):
                orchestrator._carry_over_require_handle_in_directory(
                    outside_handle, pinned.handle, inside / "carry-over.json"
                )
        finally:
            kernel32.CloseHandle(inside_handle)
            kernel32.CloseHandle(outside_handle)


@_WINDOWS_ONLY
def test_carry_over_handle_delete_removes_a_file_symlink_not_its_target(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    dispatch_dir = repo / "logs" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    target = tmp_path / "outside" / "victim.txt"
    _write(target, "keep\n")
    link = dispatch_dir / "carry-over.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a file symlink here")
    orchestrator = _load_orchestrator()

    with orchestrator._pinned_carry_over_directories(repo, create=False) as pinned:
        assert pinned is not None
        orchestrator._unlink_carry_over_file_checked(link, pinned.handle)
        # A missing file is not an error.
        orchestrator._unlink_carry_over_file_checked(link, pinned.handle)

    assert not os.path.lexists(link)
    assert target.read_text(encoding="utf-8") == "keep\n"


@_WINDOWS_ONLY
def test_carry_over_handle_helpers_require_the_pinned_directory_handle(
    tmp_path: Path,
) -> None:
    # The Windows helpers never fall back to looking up the directory by path.
    record = tmp_path / "logs" / "dispatch" / "carry-over.json"
    _write(record, "{}\n")
    orchestrator = _load_orchestrator()

    with pytest.raises(ValueError):
        orchestrator._unlink_carry_over_file_checked(record)
    with pytest.raises(ValueError):
        orchestrator._write_carry_over_record_checked(record, "{}\n")
    assert record.read_text(encoding="utf-8") == "{}\n"


def _mount_logs_before_opening_dispatch(
    orchestrator: Any,
    monkeypatch: pytest.MonkeyPatch,
    logs_dir: Path,
    target: Path,
    outcomes: list[str],
) -> None:
    """Turn the pinned, empty ``logs`` into a mount point before its child opens.

    This is the window between pinning ``logs`` and pinning ``logs/dispatch``:
    the pin on ``logs`` cannot stop an in-place mount point, so the
    ``dispatch`` open must not follow it to ``target``.
    """

    real_pin_child = orchestrator._pin_carry_over_child_directory

    def mount_then_pin(
        parent_handle: int, name: str, path: Path, *, create: bool
    ) -> int | None:
        if name == "dispatch":
            _mount_directory_in_place(logs_dir, target)
            outcomes.append("mounted")
            assert (path / "carry-over.json").exists()  # the path reaches outside
        return real_pin_child(parent_handle, name, path, create=create)

    monkeypatch.setattr(
        orchestrator, "_pin_carry_over_child_directory", mount_then_pin
    )
    # Simulate the mount point appearing after every path-based check, so only
    # the handle checks stand between the pin and the external directory.
    monkeypatch.setattr(
        orchestrator, "_carry_over_path_traverses_link", lambda _path: False
    )


@_WINDOWS_ONLY
def test_carry_over_delete_refuses_a_mount_point_set_on_logs_while_pinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _ignored_logs_repo(repo)
    logs_dir = repo / "logs"
    logs_dir.mkdir()
    external_record = outside / "dispatch" / "carry-over.json"
    _write(external_record, "{}\n")
    orchestrator = _load_orchestrator()
    outcomes: list[str] = []
    _mount_logs_before_opening_dispatch(
        orchestrator, monkeypatch, logs_dir, outside, outcomes
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, None, _collecting_log(messages)
    )

    assert outcomes == ["mounted"]
    assert external_record.read_text(encoding="utf-8") == "{}\n"
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


@_WINDOWS_ONLY
def test_carry_over_write_refuses_a_mount_point_set_on_logs_while_pinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _ignored_logs_repo(repo)
    logs_dir = repo / "logs"
    logs_dir.mkdir()
    external_record = outside / "dispatch" / "carry-over.json"
    _write(external_record, "{}\n")
    orchestrator = _load_orchestrator()
    outcomes: list[str] = []
    _mount_logs_before_opening_dispatch(
        orchestrator, monkeypatch, logs_dir, outside, outcomes
    )
    config = _dispatch_config(repo, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    orchestrator._persist_implementer_leftovers(
        config, _app_leftovers(repo), _collecting_log(messages)
    )

    assert outcomes == ["mounted"]
    assert external_record.read_text(encoding="utf-8") == "{}\n"
    assert sorted(path.name for path in (outside / "dispatch").iterdir()) == [
        "carry-over.json"
    ]
    assert [level for level, _message in messages] == ["WARN"]
    assert "symlink or junction" in messages[0][1]


def test_carry_over_record_stays_in_memory_without_a_race_safe_mechanism(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _ignored_logs_repo(repo)
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(orchestrator, "_CARRY_OVER_DIR_FD_SUPPORTED", False)
    monkeypatch.setattr(orchestrator, "_CARRY_OVER_WINDOWS_PIN_SUPPORTED", False)
    monkeypatch.setattr(orchestrator, "_operator_stdin_is_interactive", lambda: True)
    config = _dispatch_config(repo, commit_mode="dispatcher")
    record = repo / "logs" / "dispatch" / "carry-over.json"
    leftovers = _app_leftovers(repo)

    messages: list[tuple[str, str]] = []
    orchestrator._persist_implementer_leftovers(
        config, leftovers, _collecting_log(messages)
    )

    assert not (repo / "logs").exists()
    assert [level for level, _message in messages] == ["WARN"]
    assert "in memory only" in messages[0][1]

    save_implementer_leftovers(record, leftovers)
    original = record.read_text(encoding="utf-8")
    messages = []
    restored = orchestrator._restore_implementer_leftovers(
        config, _collecting_log(messages), orchestrator._OperatorPromptState()
    )

    assert restored is None
    assert record.read_text(encoding="utf-8") == original
    assert [level for level, _message in messages] == ["WARN"]
    assert str(orchestrator._carry_over_state_path(config)) in messages[0][1]
    assert "untouched" in messages[0][1]

    messages = []
    orchestrator._persist_implementer_leftovers(config, None, _collecting_log(messages))

    assert record.read_text(encoding="utf-8") == original
    assert [level for level, _message in messages] == ["WARN"]


def test_restored_leftovers_do_not_carry_over_edited_or_other_story_files(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state_path = tmp_path / "logs" / "dispatch" / "carry-over.json"
    first_state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _write(tmp_path / "src" / "api.py", "API = 2\n")
    save_implementer_leftovers(
        state_path,
        capture_implementer_leftovers(
            tmp_path,
            handoff_path=HANDOFF_RELATIVE_PATH,
            pre_dispatch_state=first_state,
            story_key=f"story:{STORY_ID}",
        ),
    )
    # The operator edits one leftover while the dispatcher is stopped.
    _write(tmp_path / "src" / "api.py", "API = 'operator edit'\n")

    restored = load_implementer_leftovers(state_path)
    same_story = capture_pre_dispatch_state(
        tmp_path, leftovers=restored, story_key=f"story:{STORY_ID}"
    )
    other_story = capture_pre_dispatch_state(
        tmp_path, leftovers=restored, story_key="story:EPIC-1-S2"
    )

    assert same_story.carried_over == ("src/app.py",)
    assert "src/api.py" in same_story.dirty_files
    assert other_story.carried_over == ()


def test_carry_over_record_is_cleared_when_nothing_can_carry_over(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state_path = tmp_path / "logs" / "dispatch" / "carry-over.json"
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    leftovers = ImplementerLeftovers(
        story_key=f"story:{STORY_ID}",
        fingerprints=(("src/app.py", working_tree_fingerprint(tmp_path, "src/app.py")),),
    )
    save_implementer_leftovers(state_path, leftovers)
    assert load_implementer_leftovers(state_path) == leftovers

    _commit_all(tmp_path, "commit the leftover")
    assert prune_implementer_leftovers(tmp_path, leftovers) is None

    save_implementer_leftovers(state_path, None)
    assert not state_path.exists()
    # Without a story key a record never matches, so it is not kept.
    save_implementer_leftovers(
        state_path, ImplementerLeftovers(story_key=None, fingerprints=leftovers.fingerprints)
    )
    assert not state_path.exists()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json", encoding="utf-8")
    assert load_implementer_leftovers(state_path) is None


def test_run_loop_clears_the_carry_over_record_when_the_story_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / ".gitignore", "logs/\n")
    _commit_all(tmp_path, "ignore runtime state")
    state_path = tmp_path / "logs" / "dispatch" / "carry-over.json"
    _write(tmp_path / "src" / "api.py", "API = 'left by another story'\n")
    save_implementer_leftovers(
        state_path,
        ImplementerLeftovers(
            story_key="story:EPIC-9-S9",
            fingerprints=(
                ("src/api.py", working_tree_fingerprint(tmp_path, "src/api.py")),
            ),
        ),
    )
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []
    seen_states: list[Any] = []
    real_capture = orchestrator.capture_pre_dispatch_state

    def recording_capture(*args: Any, **kwargs: Any):
        state = real_capture(*args, **kwargs)
        seen_states.append(state)
        return state

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        assert not state_path.exists(), "the other story's record must be cleared"
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "capture_pre_dispatch_state", recording_capture)
    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert seen_states[0].carried_over == ()
    assert "src/api.py" in seen_states[0].dirty_files
    assert _commit_files(tmp_path, "HEAD") == ["src/app.py"]


def test_stop_request_during_an_implementer_dispatch_exits_after_commit_and_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / ".gitignore", "logs/\n")
    base = _commit_all(tmp_path, "ignore runtime state")
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    stop_path = tmp_path / "logs" / "dispatch" / "stop-request"
    messages: list[tuple[str, str]] = []
    dispatches: list[str] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        # The operator runs `llm-handoff stop` while the agent works.
        request_stop(tmp_path)
        return _dispatch_result()

    def fail_auditor(*_args: object, **_kwargs: object) -> SubagentResult:
        raise AssertionError("the stop request must prevent the next dispatch")

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fail_auditor)

    # No max_cycles: only the stop request ends this run.
    exit_code = orchestrator.run_loop(config, log=_collecting_log(messages))

    assert exit_code == 0
    assert dispatches == ["backend"]
    assert _git(tmp_path, "rev-parse", "HEAD^") == base
    assert _commit_files(tmp_path, "HEAD") == ["src/app.py"]
    assert not stop_path.exists()
    texts = [message for _level, message in messages]

    def index_of(prefix: str) -> int:
        return next(i for i, text in enumerate(texts) if text.startswith(prefix))

    seen = index_of("Stop requested (")
    committed = index_of("Dispatcher committed 1 in-scope file(s) for backend")
    gate = texts.index("Work-claim gate PASSED for backend.")
    stopped = index_of("Stopped at the operator's request before the next dispatch")
    assert seen < committed < gate < stopped
    assert sum(text.startswith("Stop requested (") for text in texts) == 1
    assert not any(text.startswith("--- Cycle 2") for text in texts)


# ---------------------------------------------------------------------------
# Recording a passed gate for the auditor
# ---------------------------------------------------------------------------


def test_passed_gate_is_recorded_in_the_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))

    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    head = _git(tmp_path, "rev-parse", "HEAD")
    assert handoff.count("## Dispatcher Verification") == 1
    assert "- **Verdict:** PASS" in handoff
    assert f"- **Verified commit:** `{head}`" in handoff
    assert "- **Verification command:** " in handoff
    assert route(handoff).route == "auditor"


def test_record_passed_verification_replaces_an_earlier_section() -> None:
    from llm_handoff.work_claim import WorkClaimResult, record_passed_verification

    earlier = _claim("a" * 40) + "\n## Dispatcher Verification\n\nold FAIL text\n"
    result = WorkClaimResult(
        checks=(),
        errors=(),
        pre_dispatch_head="b" * 40,
        head_sha="a" * 40,
        scope_sha="a" * 40,
        changed_files=("src/app.py",),
        verification_runs=(),
        work_packet=None,
        work_packet_text=None,
    )

    updated = record_passed_verification(earlier, result)

    assert updated.count("## Dispatcher Verification") == 1
    assert "old FAIL text" not in updated
    assert "- **Verdict:** PASS" in updated


def test_log_work_claim_result_reports_each_scanner_outcome() -> None:
    from llm_handoff.work_claim import ScannerRun, WorkClaimCheck, WorkClaimResult

    orchestrator = _load_orchestrator()
    result = WorkClaimResult(
        checks=(WorkClaimCheck("diff_scope", "PASS", "every changed file is in scope."),),
        errors=(),
        pre_dispatch_head="b" * 40,
        head_sha="a" * 40,
        scope_sha="a" * 40,
        changed_files=("src/app.py",),
        verification_runs=(),
        work_packet=None,
        work_packet_text=None,
        scanner_runs=(
            ScannerRun(
                name="gitleaks",
                command="gitleaks git .",
                outcome="PASS",
                detail="`gitleaks git .` exited 0 after 1.0s.",
            ),
            ScannerRun(
                name="bandit",
                command="bandit -q {changed_py_files}",
                outcome="SKIP",
                detail="not installed.",
                required=False,
                missing=True,
            ),
        ),
    )
    messages: list[tuple[str, str]] = []

    orchestrator._log_work_claim_result(
        result, lambda level, message: messages.append((level, message))
    )

    assert (
        "INFO",
        "work-claim: PASS diff_scope: every changed file is in scope.",
    ) in messages
    assert (
        "INFO",
        "work-claim: PASS scanner gitleaks: `gitleaks git .` exited 0 after 1.0s.",
    ) in messages
    assert ("INFO", "work-claim: SKIP scanner bandit: not installed.") in messages
    assert (
        "WARN",
        "work-claim: optional scanner bandit skipped: not installed.",
    ) in messages


def test_run_loop_puts_implementer_work_on_a_story_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)
    base_branch = _git(tmp_path, "branch", "--show-current")
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path, commit_mode="dispatcher", publish={"mode": "merge-commit"}
    )
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    branch = _git(tmp_path, "branch", "--show-current")
    assert branch.startswith("llm-handoff/epic-1-s1-")
    assert branch != base_branch
    assert _git(tmp_path, "log", "-1", "--format=%s").startswith(STORY_ID)
    assert ("AGENT", "Work-claim gate PASSED for backend.") in messages


def _restart_mid_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    saved_story_id: str | None = None,
) -> tuple[DispatchConfig, str, str, list[tuple[str, str]]]:
    """Commit the story's work in one run, then re-claim it in a fresh run."""

    base = _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path, commit_mode="dispatcher", publish={"mode": "pr"}
    )
    dispatches: list[int] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append(1)
        if len(dispatches) == 1:
            _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))
    story_head = _git(tmp_path, "rev-parse", "HEAD")
    assert story_head != base

    # A review correction routes the story back to the implementer, whose
    # work is already committed. The dispatcher restarts before it runs.
    config.handoff_full_path.write_text(_assignment(), encoding="utf-8")
    if saved_story_id is not None:
        state_path = tmp_path / "logs" / "dispatch" / "publish" / "story.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["story_id"] = saved_story_id
        state_path.write_text(json.dumps(state), encoding="utf-8")
        # A handoff for another story would need the saved story's pull
        # request merged (tests/test_publish.py covers that gate); this test
        # is only about the saved story base.
        from llm_handoff.publish import PublishOutcome

        monkeypatch.setattr(
            orchestrator,
            "recover_orphaned_story",
            lambda **_kwargs: PublishOutcome(),
        )
    messages: list[tuple[str, str]] = []
    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))
    assert len(dispatches) == 2
    return config, base, story_head, messages


def test_run_loop_story_base_survives_a_restart_mid_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    config, _base, story_head, messages = _restart_mid_story(tmp_path, monkeypatch)

    assert _git(tmp_path, "rev-parse", "HEAD") == story_head
    assert any(
        "nothing new to commit; recorded the story's earlier commit" in message
        for _level, message in messages
    )
    assert ("AGENT", "Work-claim gate PASSED for backend.") in messages
    frontmatter = parse_handoff_frontmatter_text(
        config.handoff_full_path.read_text(encoding="utf-8")
    )
    assert frontmatter is not None
    assert frontmatter.scope_sha == story_head


def test_run_loop_ignores_a_saved_story_base_for_another_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _config, _base, _story_head, messages = _restart_mid_story(
        tmp_path, monkeypatch, saved_story_id="EPIC-9-S9"
    )

    assert not any(
        "recorded the story's earlier commit" in message
        for _level, message in messages
    )
    assert ("AGENT", "Work-claim gate PASSED for backend.") not in messages
    assert any("no_changes" in message for _level, message in messages)


def test_run_loop_writes_the_review_bounce_from_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    from llm_handoff.publish import PublishOutcome

    _init_repo(tmp_path, handoff_content=_claim("a" * 40))
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path,
        commit_mode="dispatcher",
        publish={"mode": "pr", "wait_for_reviewers": ["codex"]},
    )
    bounce = _claim(None, status="verified_fail", producer="validator", next_agent="backend")
    published: list[dict[str, object]] = []

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
        config.handoff_full_path.write_text(
            _claim(None, status="verified_pass", producer="auditor", next_agent="planner"),
            encoding="utf-8",
        )
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def fake_publish(**kwargs: object) -> PublishOutcome:
        published.append(kwargs)
        return PublishOutcome(handoff=bounce)

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *_args, **_kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        ),
    )
    monkeypatch.setattr(orchestrator, "publish_story", fake_publish)

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))

    assert exit_code == 0
    assert len(published) == 1
    review = published[0]["review"]
    assert review.reviewers == ("codex",) and review.max_rounds == 1
    assert published[0]["handoff_path"] == config.handoff_path
    assert config.handoff_full_path.read_text(encoding="utf-8") == bounce


@pytest.mark.parametrize(
    ("mode", "dry_run", "resumes"),
    [("pr", False, True), ("pr", True, False), ("merge-commit", False, False)],
)
def test_run_loop_resumes_a_paused_publish_before_the_first_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    mode: str,
    dry_run: bool,
    resumes: bool,
) -> None:
    from llm_handoff.publish import PublishOutcome

    verified = _claim(
        "a" * 40, status="verified_pass", producer="auditor", next_agent="planner"
    )
    _init_repo(tmp_path, handoff_content=verified)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path,
        commit_mode="dispatcher",
        dry_run=dry_run,
        publish={"mode": mode, "wait_for_reviewers": ["codex"]}
        if mode == "pr"
        else {"mode": mode},
    )
    resumed: list[dict[str, object]] = []

    def fake_resume(**kwargs: object) -> PublishOutcome:
        resumed.append(kwargs)
        return PublishOutcome(stop=True, exit_code=0, message="paused again")

    def fail_planner(*_args: object, **_kwargs: object) -> DispatchResult:
        raise AssertionError("the planner must not run before the resume")

    monkeypatch.setattr(orchestrator, "resume_paused_publish", fake_resume)
    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        fail_planner if resumes else lambda *_args, **_kwargs: _dispatch_result(),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *_args, **_kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        ),
    )

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))

    assert exit_code == 0
    if not resumes:
        assert resumed == []
        return
    [call] = resumed
    assert call["handoff_content"] == verified
    assert call["remote"] == "origin"
    assert call["review"].reviewers == ("codex",)


@pytest.mark.parametrize("mode", ["pr", "local"])
def test_run_loop_settles_deferred_findings_after_a_planner_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    mode: str,
) -> None:
    from llm_handoff.publish import PublishOutcome

    _init_repo(
        tmp_path,
        handoff_content=_claim(
            "a" * 40, status="verified_pass", producer="validator", next_agent="planner"
        ),
    )
    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path, commit_mode="dispatcher", publish={"mode": mode}
    )
    decided = _claim(
        None, status="verified_fail", producer="planner", next_agent="backend"
    )
    settled: list[dict[str, object]] = []

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        path.write_text(decided, encoding="utf-8")
        return _dispatch_result()

    def fake_settle(**kwargs: object) -> PublishOutcome:
        settled.append(kwargs)
        return PublishOutcome(stop=True, exit_code=0, message="paused")

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(
        orchestrator,
        "validate_handoff",
        lambda *_args, **_kwargs: ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="backend"
        ),
    )
    monkeypatch.setattr(orchestrator, "settle_deferred_findings", fake_settle)

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))

    assert exit_code == 0
    if mode == "local":
        assert settled == []
        return
    [call] = settled
    assert call["handoff_content"] == decided
    assert call["remote"] == "origin"
    assert call["repo_root"] == config.repo_root


@pytest.mark.parametrize("commit_mode", ["dispatcher", "agent"])
def test_planner_is_told_to_leave_the_handoff_uncommitted_in_dispatcher_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    commit_mode: str,
) -> None:
    from llm_handoff.dispatcher_commit import PLANNER_COMMIT_MODE_INSTRUCTION

    _init_repo(
        tmp_path,
        handoff_content=_claim(
            "a" * 40, status="verified_pass", producer="auditor", next_agent="planner"
        ),
    )
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode=commit_mode)
    instructions: list[object] = []

    def fake_planner(path: Path, **kwargs: object) -> DispatchResult:
        instructions.append(kwargs.get("additional_instruction"))
        return _dispatch_result()

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
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        ),
    )

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))

    assert len(instructions) == 1
    told = PLANNER_COMMIT_MODE_INSTRUCTION in str(instructions[0] or "")
    assert told is (commit_mode == "dispatcher")


@pytest.mark.parametrize("commit_mode", ["dispatcher", "agent"])
def test_post_dispatch_validation_is_told_the_commit_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    commit_mode: str,
) -> None:
    _init_repo(
        tmp_path,
        handoff_content=_claim(
            "a" * 40, status="verified_pass", producer="auditor", next_agent="planner"
        ),
    )
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode=commit_mode)
    validate_kwargs: list[dict[str, object]] = []

    def fake_validate(*_args: object, **kwargs: object) -> ValidationResult:
        validate_kwargs.append(kwargs)
        return ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="planner"
        )

    monkeypatch.setattr(
        orchestrator, "invoke_planner_role", lambda *_a, **_k: _dispatch_result()
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda *_args, **_kwargs: SubagentResult(
            stdout="", stderr="", exit_code=0, elapsed_seconds=0.01
        ),
    )
    monkeypatch.setattr(orchestrator, "validate_handoff", fake_validate)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))

    assert validate_kwargs
    assert all(
        kwargs.get("dispatcher_commit_mode") is (commit_mode == "dispatcher")
        for kwargs in validate_kwargs
    )


def _run_planner_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planner_work,
    *,
    commit_mode: str = "dispatcher",
) -> tuple[int, list[tuple[str, str]], DispatchConfig]:
    _init_repo(
        tmp_path,
        handoff_content=_claim(
            "a" * 40, status="verified_pass", producer="auditor", next_agent="planner"
        ),
    )
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode=commit_mode)
    messages: list[tuple[str, str]] = []

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        path.write_text(_assignment(), encoding="utf-8")
        planner_work(path)
        return _dispatch_result()

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
    exit_code = orchestrator.run_loop(
        config, max_cycles=1, log=_collecting_log(messages)
    )
    return exit_code, messages, config


def test_a_planner_commit_of_the_handoff_is_undone_in_dispatcher_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    base = None

    def planner_work(path: Path) -> None:
        nonlocal base
        base = _git(tmp_path, "rev-parse", "HEAD")
        _commit_paths(tmp_path, "Dispatch S1 to backend", HANDOFF_RELATIVE_PATH.as_posix())

    exit_code, messages, config = _run_planner_cycle(tmp_path, monkeypatch, planner_work)

    assert exit_code == 0
    assert _git(tmp_path, "rev-parse", "HEAD") == base
    assert _git(tmp_path, "status", "--porcelain") == f"M {HANDOFF_RELATIVE_PATH.as_posix()}"
    assert "next_agent: backend" in config.handoff_full_path.read_text(encoding="utf-8")
    assert any(
        level == "WARN" and "undid it with `git reset --soft`" in message
        for level, message in messages
    )


def test_a_planner_commit_of_other_files_stops_the_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    def planner_work(path: Path) -> None:
        _write(tmp_path / "src" / "app.py", "VALUE = 9\n")
        _commit_paths(tmp_path, "planner wrote code", "src/app.py")

    exit_code, messages, _config = _run_planner_cycle(tmp_path, monkeypatch, planner_work)

    assert exit_code == 1
    assert _git(tmp_path, "log", "-1", "--format=%s") == "planner wrote code"
    assert any(
        level == "ERROR" and "files other than the handoff: src/app.py" in message
        for level, message in messages
    )


def _run_finalizer_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalizer_work,
) -> tuple[int, list[tuple[str, str]], str]:
    audited = _init_repo(
        tmp_path,
        handoff_content=_claim(
            "a" * 40, status="verified_pass", producer="auditor", next_agent="finalizer"
        ).replace("close_type: story", "close_type: epic"),
    )
    from llm_handoff.ledger import EpicCloseResult

    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode="dispatcher")
    messages: list[tuple[str, str]] = []

    def fake_epic_close(**_kwargs: object) -> EpicCloseResult:
        config.handoff_full_path.write_text(
            _claim(audited, status="verified_pass", producer="finalizer", next_agent="planner"),
            encoding="utf-8",
        )
        finalizer_work()
        return EpicCloseResult(subagent_exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator, "_invoke_epic_close", fake_epic_close)
    exit_code = orchestrator.run_loop(
        config, max_cycles=1, log=_collecting_log(messages)
    )
    return exit_code, messages, audited


def test_a_finalizer_commit_of_only_the_handoff_is_undone_in_dispatcher_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    exit_code, messages, audited = _run_finalizer_cycle(
        tmp_path,
        monkeypatch,
        lambda: _commit_paths(
            tmp_path, "Close the epic", HANDOFF_RELATIVE_PATH.as_posix()
        ),
    )

    assert exit_code == 0
    assert _git(tmp_path, "rev-parse", "HEAD") == audited
    assert _git(tmp_path, "status", "--porcelain") == f"M {HANDOFF_RELATIVE_PATH.as_posix()}"
    assert any(
        level == "WARN" and "The finalizer committed the handoff" in message
        for level, message in messages
    )


def test_a_finalizer_project_state_commit_is_kept_in_dispatcher_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    def finalizer_work() -> None:
        _write(tmp_path / "PROJECT_STATE.md", "# PROJECT STATE\n\nEPIC-1 closed.\n")
        _commit_paths(tmp_path, "Close the epic", "PROJECT_STATE.md")

    exit_code, messages, _audited = _run_finalizer_cycle(
        tmp_path, monkeypatch, finalizer_work
    )

    assert exit_code == 0
    assert _git(tmp_path, "log", "-1", "--format=%s") == "Close the epic"
    assert not any(level == "ERROR" for level, _ in messages)


def test_a_finalizer_commit_mixing_the_handoff_stops_the_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    def finalizer_work() -> None:
        _write(tmp_path / "PROJECT_STATE.md", "# PROJECT STATE\n\nEPIC-1 closed.\n")
        _commit_paths(
            tmp_path,
            "Close the epic",
            "PROJECT_STATE.md",
            HANDOFF_RELATIVE_PATH.as_posix(),
        )

    exit_code, messages, _audited = _run_finalizer_cycle(
        tmp_path, monkeypatch, finalizer_work
    )

    assert exit_code == 1
    assert any(
        level == "ERROR"
        and f"the handoff {HANDOFF_RELATIVE_PATH.as_posix()} alongside: PROJECT_STATE.md"
        in message
        for level, message in messages
    )


def test_the_finalizer_is_told_to_leave_the_handoff_uncommitted_in_dispatcher_mode() -> None:
    from llm_handoff.ledger import ledger_updater_prompt

    dispatcher = ledger_updater_prompt(HANDOFF_RELATIVE_PATH, "dispatcher")
    agent = ledger_updater_prompt(HANDOFF_RELATIVE_PATH, "agent")

    assert "Dispatcher commit mode is on" in dispatcher
    assert f"leave {HANDOFF_RELATIVE_PATH.as_posix()} uncommitted" in dispatcher
    assert (
        "Commit PROJECT_STATE.md, COMPLETED_WORK.md, and "
        f"{HANDOFF_RELATIVE_PATH.as_posix()}"
    ) in agent


@pytest.mark.parametrize("commit_mode", ["agent", "dispatcher"])
def test_epic_close_sends_the_finalizer_the_configured_commit_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_mode: str,
) -> None:
    prompts: list[str] = []

    def fake_invoke_support_role(_name: str, prompt: str, **_kwargs: object):
        prompts.append(prompt)
        return SubagentResult(stdout="", stderr="", exit_code=1, elapsed_seconds=0)

    monkeypatch.setattr(ledger, "invoke_support_role", fake_invoke_support_role)

    ledger.run_epic_close(config=_dispatch_config(tmp_path, commit_mode=commit_mode))

    assert prompts == [ledger.ledger_updater_prompt(HANDOFF_RELATIVE_PATH, commit_mode)]
    told = "Dispatcher commit mode is on" in prompts[0]
    assert told is (commit_mode == "dispatcher")


def test_planner_commits_are_left_alone_in_agent_commit_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    def planner_work(path: Path) -> None:
        _commit_paths(tmp_path, "Dispatch S1 to backend", HANDOFF_RELATIVE_PATH.as_posix())

    exit_code, _messages, _config = _run_planner_cycle(
        tmp_path, monkeypatch, planner_work, commit_mode="agent"
    )

    assert exit_code == 0
    assert _git(tmp_path, "log", "-1", "--format=%s") == "Dispatch S1 to backend"


# ---------------------------------------------------------------------------
# Audit fan-out range across bounces
# ---------------------------------------------------------------------------


MARKER_COMMAND = (
    f'"{PYTHON}" -c "import sys; '
    "sys.exit(0 if 'fixed' in open('tests/test_app.py').read() else 4)\""
)


def _audit_range_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend_work,
    planner_handoff: str | None = None,
    audit_warnings: list[list[str]] | None = None,
    publish: dict[str, object] | None = None,
) -> tuple[DispatchConfig, list[str], list[str | None]]:
    """Run real-git cycles with the fan-out replaced by a base-SHA recorder."""

    orchestrator = _load_orchestrator()
    config = _dispatch_config(
        tmp_path,
        commit_mode="dispatcher",
        audit={"reviewers": ["quality", "security"]},
        **({"publish": publish} if publish is not None else {}),
    )
    backend_heads: list[str] = []
    audit_bases: list[str | None] = []
    real_validate = orchestrator.validate_handoff

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        backend_heads.append(_git(tmp_path, "rev-parse", "HEAD"))
        backend_work(len(backend_heads))
        path.write_text(_pending_claim(), encoding="utf-8")
        return _dispatch_result()

    def fake_fan_out(_config: DispatchConfig, *, context, **_kwargs: object):
        audit_bases.append(context.implementer_base_sha)
        config.handoff_full_path.write_text(
            _claim(None, status="verified_pass", producer="auditor", next_agent="planner"),
            encoding="utf-8",
        )
        return _dispatch_result()

    def fake_planner(path: Path, **_kwargs: object) -> DispatchResult:
        assert planner_handoff is not None
        path.write_text(planner_handoff, encoding="utf-8")
        return _dispatch_result()

    def validate(path: Path, previous_agent: str, **kwargs: object) -> ValidationResult:
        if previous_agent == "backend":
            return real_validate(path, previous_agent, **kwargs)
        warnings: list[str] = []
        if previous_agent.startswith("auditor") and audit_warnings:
            warnings = audit_warnings.pop(0)
        return ValidationResult(
            verdict="YES", warnings=warnings, errors=[], routing_instruction="planner"
        )

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, "run_audit_fan_out", fake_fan_out)
    monkeypatch.setattr(orchestrator, "validate_handoff", validate)
    return config, backend_heads, audit_bases


def test_audit_range_spans_every_attempt_after_a_gate_bounce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    base = _init_repo(tmp_path, _assignment(MARKER_COMMAND))

    def backend_work(attempt: int) -> None:
        if attempt == 1:
            # Committed by the dispatcher, then bounced by the failing check.
            _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        else:
            _write(tmp_path / "tests" / "test_app.py", "# fixed\n")

    config, backend_heads, audit_bases = _audit_range_harness(
        tmp_path, monkeypatch, backend_work=backend_work
    )

    _load_orchestrator().run_loop(config, max_cycles=3, log=_collecting_log([]))

    assert len(backend_heads) == 2
    assert backend_heads[0] == base
    assert backend_heads[1] != base, "the first attempt was not committed"
    assert audit_bases == [base]


def test_audit_range_survives_a_restart_mid_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    from llm_handoff.publish import PublishOutcome

    base = _init_repo(tmp_path)

    def backend_work(attempt: int) -> None:
        if attempt == 1:
            _write(tmp_path / "src" / "app.py", "VALUE = 2\n")

    config, backend_heads, audit_bases = _audit_range_harness(
        tmp_path, monkeypatch, backend_work=backend_work, publish={"mode": "pr"}
    )
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator, "publish_story", lambda **_kwargs: PublishOutcome()
    )

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))
    # The dispatcher restarts before the committed story work is audited.
    config.handoff_full_path.write_text(_assignment(), encoding="utf-8")
    orchestrator.run_loop(config, max_cycles=2, log=_collecting_log([]))

    assert len(backend_heads) == 2
    assert backend_heads[1] != base, "the first dispatch was not committed"
    assert audit_bases == [base]


@pytest.mark.parametrize("mode", ["pr", "local"])
def test_a_restart_that_routes_straight_to_the_auditor_audits_the_whole_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    mode: str,
) -> None:
    from llm_handoff.publish import PublishOutcome

    base = _init_repo(tmp_path)

    def backend_work(attempt: int) -> None:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")

    config, backend_heads, audit_bases = _audit_range_harness(
        tmp_path, monkeypatch, backend_work=backend_work, publish={"mode": mode}
    )
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator, "publish_story", lambda **_kwargs: PublishOutcome()
    )

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))
    # The dispatcher restarts with the handback still routing to the auditor,
    # so no implementer dispatch in this run records where the story started.
    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))

    assert backend_heads == [base]
    # Outside `local` mode the saved story base outlives the restart; in
    # `local` mode the review falls back to the scope commit's parent.
    assert audit_bases == [base if mode == "pr" else None]


def test_audit_range_starts_at_the_merge_base_after_a_base_branch_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    from llm_handoff.publish import STATE_PATH, PublishOutcome

    base = _init_repo(tmp_path)

    def backend_work(attempt: int) -> None:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")

    config, backend_heads, audit_bases = _audit_range_harness(
        tmp_path, monkeypatch, backend_work=backend_work, publish={"mode": "pr"}
    )
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator, "publish_story", lambda **_kwargs: PublishOutcome()
    )

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log([]))
    claim = config.handoff_full_path.read_text(encoding="utf-8")
    story_head = _git(tmp_path, "rev-parse", "HEAD")
    assert f"scope_sha: {story_head}" in claim

    # The base branch moves on upstream, and the story branch merges it before
    # the audit, as publishing does before it pushes.
    state = json.loads((tmp_path / STATE_PATH).read_text(encoding="utf-8"))
    assert state["base_sha"] == base
    _git(tmp_path, "switch", "-q", state["base_branch"])
    _write(tmp_path / "docs" / "upstream.md", "upstream\n")
    upstream = _commit_paths(tmp_path, "Upstream change", "docs/upstream.md")
    _git(tmp_path, "update-ref", f"refs/remotes/origin/{state['base_branch']}", upstream)
    _git(tmp_path, "switch", "-q", state["branch"])
    _git(tmp_path, "merge", "-q", "--no-edit", upstream)
    merged_head = _git(tmp_path, "rev-parse", "HEAD")
    config.handoff_full_path.write_text(
        claim.replace(story_head, merged_head), encoding="utf-8"
    )

    messages: list[tuple[str, str]] = []
    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert backend_heads == [base]
    assert audit_bases == [upstream]
    assert upstream == _git(
        tmp_path, "merge-base", "HEAD", f"refs/remotes/origin/{state['base_branch']}"
    )
    assert any(
        level == "INFO"
        and "merged its base branch" in message
        and base[:7] in message
        and upstream[:7] in message
        for level, message in messages
    ), messages


def test_audit_range_restarts_after_the_story_is_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    _init_repo(tmp_path)

    def backend_work(attempt: int) -> None:
        _write(tmp_path / "src" / "app.py", f"VALUE = {attempt + 1}\n")

    config, backend_heads, audit_bases = _audit_range_harness(
        tmp_path,
        monkeypatch,
        backend_work=backend_work,
        planner_handoff=_assignment(),
    )

    _load_orchestrator().run_loop(config, max_cycles=5, log=_collecting_log([]))

    assert len(backend_heads) == 2
    assert audit_bases == backend_heads


def test_a_pull_request_correction_round_audits_the_whole_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    from llm_handoff.publish import PublishOutcome

    base = _init_repo(tmp_path)

    def backend_work(attempt: int) -> None:
        # The correction round defers its finding and changes nothing.
        if attempt == 1:
            _write(tmp_path / "src" / "app.py", "VALUE = 2\n")

    config, backend_heads, audit_bases = _audit_range_harness(
        tmp_path,
        monkeypatch,
        backend_work=backend_work,
        # Stands in for the review bounce: the approved story goes back to
        # backend on its open branch.
        planner_handoff=_assignment(),
        publish={"mode": "pr"},
    )
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator, "publish_story", lambda **_kwargs: PublishOutcome()
    )

    orchestrator.run_loop(config, max_cycles=5, log=_collecting_log([]))

    assert len(backend_heads) == 2
    assert backend_heads[1] != base, "the first dispatch was not committed"
    assert audit_bases == [base, base]


def test_audit_range_survives_a_rejected_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    base = _init_repo(tmp_path)

    def backend_work(attempt: int) -> None:
        _write(tmp_path / "src" / "app.py", f"VALUE = {attempt + 1}\n")

    config, backend_heads, audit_bases = _audit_range_harness(
        tmp_path,
        monkeypatch,
        backend_work=backend_work,
        planner_handoff=_assignment(),
        # The first verified_pass is a rationalized claim, so the dispatcher
        # sends the story back through the planner instead of accepting it.
        audit_warnings=[["RATIONALIZATION_DETECTED: claimed done without proof"]],
    )

    _load_orchestrator().run_loop(config, max_cycles=5, log=_collecting_log([]))

    assert len(backend_heads) == 2
    assert backend_heads[1] != base
    assert audit_bases == [base, base]


# ---------------------------------------------------------------------------
# W1.11: evidence SHA and no-change checks
# ---------------------------------------------------------------------------


def _claim_with_evidence(scope_sha: str, evidence_value: str) -> str:
    claim = _claim(scope_sha)
    old_line = f"- **Commit SHA verified:** {scope_sha}"
    assert old_line in claim
    return claim.replace(old_line, f"- **Commit SHA verified:** {evidence_value}")


def _no_auditor(subagent_name: str, prompt: str, **_kwargs: object) -> SubagentResult:
    raise AssertionError("auditor must not be dispatched for a failed claim")


def test_verify_work_claim_rejects_evidence_naming_a_different_commit(
    tmp_path: Path,
) -> None:
    base = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim_with_evidence(head, base),
        state=state,
    )

    assert result.failed_prefixes == ("evidence_sha_mismatch",)
    assert _outcomes(result)["scope_sha"] == "PASS"
    assert _outcomes(result)["evidence_sha"] == "FAIL"
    assert base[:12] in result.errors[0]
    assert head[:12] in result.errors[0]


def test_verify_work_claim_rejects_evidence_that_is_not_a_commit(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim_with_evidence(head, "f" * 40),
        state=state,
    )

    assert result.failed_prefixes == ("evidence_sha_mismatch",)
    assert "does not resolve to a commit" in result.errors[0]


@pytest.mark.parametrize("evidence_value", ["", "n/a"])
def test_verify_work_claim_rejects_evidence_without_a_sha(
    tmp_path: Path,
    evidence_value: str,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim_with_evidence(head, evidence_value),
        state=state,
    )

    assert result.failed_prefixes == ("evidence_sha_mismatch",)


def test_verify_work_claim_rejects_a_claim_without_an_evidence_block(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    claim = _claim(head).split("## Verification Evidence")[0]

    result = _verify(tmp_path, assignment=_assignment(), claim=claim, state=state)

    assert result.failed_prefixes == ("evidence_sha_mismatch",)
    assert "no Commit SHA verified value" in result.errors[0]


@pytest.mark.parametrize(
    "evidence_template",
    ["`{sha}`", "{short} (implementation commit)"],
)
def test_verify_work_claim_accepts_annotated_evidence_for_scope_sha(
    tmp_path: Path,
    evidence_template: str,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    evidence = evidence_template.format(sha=head, short=head[:7])

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim_with_evidence(head, evidence),
        state=state,
    )

    assert result.passed, result.errors
    assert "evidence_sha" not in _outcomes(result)


def test_verify_work_claim_accepts_evidence_at_a_handoff_only_commit_above_scope_sha(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    work_sha = _commit_all(tmp_path, "set value")
    _write(tmp_path / HANDOFF_RELATIVE_PATH, _claim(work_sha))
    handoff_sha = _commit_all(tmp_path, "handoff")

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim_with_evidence(work_sha, handoff_sha),
        state=state,
    )

    assert result.passed, result.errors


def test_verify_work_claim_rejects_evidence_above_scope_sha_through_source_commits(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    work_sha = _commit_all(tmp_path, "set value")
    _write(tmp_path / "src" / "app.py", "VALUE = 3\n")
    later_sha = _commit_all(tmp_path, "more work")

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim_with_evidence(later_sha, work_sha),
        state=state,
    )

    assert result.failed_prefixes == ("evidence_sha_mismatch",)


def test_verify_work_claim_reads_only_the_top_level_evidence_section(
    tmp_path: Path,
) -> None:
    base = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    claim = _claim(head) + (
        "\n## Prior Handback\n\n### Verification Evidence\n\n"
        f"- **Commit SHA verified:** {base}\n"
    )

    result = _verify(tmp_path, assignment=_assignment(), claim=claim, state=state)

    assert result.passed, result.errors


def test_verify_work_claim_skips_the_evidence_check_without_a_resolvable_scope_sha(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)
    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    _commit_all(tmp_path, "set value")

    result = _verify(
        tmp_path,
        assignment=_assignment(),
        claim=_claim("f" * 40),
        state=state,
    )

    assert result.failed_prefixes == ("scope_sha_not_head",)


@pytest.mark.parametrize("dispatcher_commit_mode", [False, True])
@pytest.mark.parametrize("implementer", ["backend", "frontend"])
def test_new_gate_failures_route_back_to_the_implementer_as_verified_fail(
    tmp_path: Path,
    dispatcher_commit_mode: bool,
    implementer: str,
) -> None:
    base = _init_repo(tmp_path)
    state = capture_pre_dispatch_state(tmp_path)

    def verify(claim: str):
        return verify_work_claim(
            repo_root=tmp_path,
            handoff_path=HANDOFF_RELATIVE_PATH,
            assignment_content=_assignment(),
            claim_content=claim,
            pre_dispatch_state=state,
            verification_timeout_seconds=60,
            require_new_commit=True,
            dispatcher_commit_mode=dispatcher_commit_mode,
        )

    no_changes = verify(_claim(base))
    assert no_changes.failed_prefixes == ("no_changes",)

    _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
    head = _commit_all(tmp_path, "set value")
    mismatch = verify(_claim_with_evidence(head, base))
    assert mismatch.failed_prefixes == ("evidence_sha_mismatch",)

    for result in (no_changes, mismatch):
        route = bounce_route_for(result, implementer=implementer)
        assert (route.next_agent, route.status) == (implementer, "verified_fail")


@pytest.mark.parametrize("commit_mode", ["agent", "dispatcher"])
def test_run_loop_rejects_a_claim_that_changed_nothing_in_both_commit_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
    commit_mode: str,
) -> None:
    base = _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path, commit_mode=commit_mode)
    messages: list[tuple[str, str]] = []
    dispatches: list[str] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        dispatches.append("backend")
        # Verification-only work: nothing changed, so there is nothing to
        # commit. In agent mode the claim names the pre-dispatch HEAD.
        claim = _claim(base) if commit_mode == "agent" else _pending_claim()
        path.write_text(claim, encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", _no_auditor)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert dispatches == ["backend"], "no handoff repair should be dispatched"
    assert _git(tmp_path, "rev-parse", "HEAD") == base
    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    bounced = parse_handoff_frontmatter_text(handoff)
    assert bounced is not None
    assert bounced.next_agent == "backend"
    assert bounced.status == "verified_fail"
    assert "- **Failed checks:** no_changes" in handoff
    assert any(
        level == "ERROR" and message.startswith("no_changes:")
        for level, message in messages
    )


def test_run_loop_bounces_an_evidence_sha_mismatch_to_the_implementer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_story_bounce: None,
) -> None:
    base = _init_repo(tmp_path)
    orchestrator = _load_orchestrator()
    config = _dispatch_config(tmp_path)
    messages: list[tuple[str, str]] = []

    def fake_backend(path: Path, **_kwargs: object) -> DispatchResult:
        _write(tmp_path / "src" / "app.py", "VALUE = 2\n")
        head = _commit_all(tmp_path, "set value")
        path.write_text(_claim_with_evidence(head, base), encoding="utf-8")
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", _no_auditor)

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    handoff = config.handoff_full_path.read_text(encoding="utf-8")
    bounced = parse_handoff_frontmatter_text(handoff)
    assert bounced is not None
    assert bounced.next_agent == "backend"
    assert bounced.status == "verified_fail"
    assert "- **Failed checks:** evidence_sha_mismatch" in handoff
    assert any(
        level == "ERROR" and message.startswith("evidence_sha_mismatch:")
        for level, message in messages
    )


# ---------------------------------------------------------------------------
# helpers for dispatcher verification before an audit
# ---------------------------------------------------------------------------


def test_parse_work_packet_verification_commands_ignores_empty_path_lists() -> None:
    from llm_handoff.work_claim import parse_work_packet_verification_commands

    content = (
        "## Work Packet\n\n"
        "- **Files in scope:** none (read-only audit)\n"
        "- **Verification command:**\n"
        "  - python -m pytest tests -q\n"
    )

    assert parse_work_packet_verification_commands(content) == (
        ("python -m pytest tests -q",),
        (),
    )
    assert parse_work_packet_verification_commands("# No packet\n") is None
    assert (
        parse_work_packet_verification_commands(
            "## Work Packet\n\n- **Files in scope:** a.py\n"
        )
        is None
    )


@pytest.mark.parametrize(
    ("verdict", "commit", "expected"),
    [
        ("PASS", "a" * 40, True),
        ("PASS", "a" * 12, True),
        ("PASS", "b" * 40, False),
        ("FAIL", "a" * 40, False),
    ],
)
def test_has_passing_dispatcher_verification(
    verdict: str, commit: str, expected: bool
) -> None:
    from llm_handoff.work_claim import has_passing_dispatcher_verification

    content = (
        "# Handoff\n\n## Dispatcher Verification\n\n"
        f"- **Verdict:** {verdict}\n"
        f"- **Verified commit:** `{commit}`\n\n"
        "## Next\n\n- **Verdict:** PASS\n"
    )

    assert has_passing_dispatcher_verification(content, "a" * 40) is expected


def test_recorded_verification_commands_reads_the_top_level_section() -> None:
    from llm_handoff.work_claim import recorded_verification_commands

    content = (
        "# Handoff\n\n## Dispatcher Verification\n\n"
        "- **Verdict:** PASS\n"
        "- **Verification command:** `python -m pytest -q` (exited 0 after 2.0s)\n"
        "- **Verification command:** `ruff check .` (exited 0 after 0.5s)\n\n"
        "## Next\n\n"
        "- **Verification command:** `other` (exited 0 after 1.0s)\n"
    )

    assert recorded_verification_commands(content) == (
        "python -m pytest -q",
        "ruff check .",
    )
    assert recorded_verification_commands("# No section\n") == ()


def test_dispatcher_verification_only_counts_the_top_level_section() -> None:
    from llm_handoff.work_claim import has_passing_dispatcher_verification

    content = (
        "# Handoff\n\n## Prior Handback\n\n### Dispatcher Verification\n\n"
        "- **Verdict:** PASS\n"
        f"- **Verified commit:** `{'a' * 40}`\n"
    )

    assert has_passing_dispatcher_verification(content, "a" * 40) is False
