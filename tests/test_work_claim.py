from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import pytest

from llm_handoff.agents import DispatchResult, SubagentResult
from llm_handoff.config import DispatchConfig, ScannerConfig, load_dispatch_config
from llm_handoff.dispatcher_commit import (
    PENDING_COMMIT_SHA,
    commit_implementer_changes,
    stamp_commit_sha,
)
import llm_handoff.ledger as ledger
from llm_handoff.router import parse_handoff_frontmatter_text, route
from llm_handoff.validator import ValidationResult, validate_handoff_text
from llm_handoff.work_claim import (
    PreDispatchState,
    ScannerContext,
    WorkPacketError,
    bounce_route_for,
    build_bounce_handoff,
    capture_pre_dispatch_state,
    extract_work_packet_section,
    parse_work_packet,
    path_matches_entry,
    render_scanner_command,
    shell_quote,
    verify_work_claim,
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


def _init_repo(repo_root: Path, handoff_content: str | None = None) -> str:
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
        "max_consecutive_failures": 3,
        "enforce_work_packet": True,
        "work_packet_verification_timeout_seconds": 60,
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


def test_dispatch_config_work_claim_gate_is_off_by_default(tmp_path: Path) -> None:
    config = DispatchConfig(repo_root=tmp_path)

    assert config.enforce_work_packet is False
    assert config.work_packet_verification_timeout_seconds == 600


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
def test_shipped_configs_document_gate_off(config_path: Path) -> None:
    content = config_path.read_text(encoding="utf-8")
    config = load_dispatch_config(repo_root=config_path.parent, config_path=config_path)

    assert "enforce_work_packet: false" in content
    assert config.enforce_work_packet is False


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
    config = _dispatch_config(tmp_path, enforce_work_packet=False)
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
        dry_run=True,
        scanners=(ScannerConfig(name="gitleaks", command="gitleaks"),),
    )
    messages: list[tuple[str, str]] = []

    orchestrator.run_loop(config, max_cycles=1, log=_collecting_log(messages))

    assert any(
        level == "WARN" and "Scanners configured (gitleaks)" in message
        for level, message in messages
    )


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


def test_stamp_commit_sha_fills_scope_sha_and_evidence() -> None:
    sha = "a" * 40

    stamped = stamp_commit_sha(_pending_claim(), sha)

    frontmatter = parse_handoff_frontmatter_text(stamped)
    assert frontmatter is not None
    assert frontmatter.scope_sha == sha
    assert f"- **Commit SHA verified:** {sha}" in stamped
    assert PENDING_COMMIT_SHA not in stamped


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
        level == "ERROR" and message.startswith("uncommitted_changes:")
        for level, message in messages
    )


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
    assert (tmp_path / "logs" / "dispatch" / "publish" / "gate.md").is_file()
    assert ("AGENT", "Work-claim gate PASSED for backend.") in messages


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
    assert review.reviewers == ("codex",) and review.max_rounds == 3
    assert config.handoff_full_path.read_text(encoding="utf-8") == bounce
