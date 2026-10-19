from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from llm_handoff.agents import DispatchResult, SubagentResult
from llm_handoff.story_list import (
    dropped_story_reasons,
    project_state_story_warnings,
    story_id_token,
    story_list_errors,
)
from llm_handoff.validator import validate_handoff, validate_handoff_text


def _handoff(
    *,
    next_agent: str = "planner",
    producer: str = "auditor",
    epic_id: str | None = "EPIC-A",
    story_id: str | None = "S1",
    remaining: list[str] | None = None,
    body: str = "# Handoff\n\n**Agent:** auditor\n",
    extra: str = "",
) -> str:
    lines = [
        "---",
        f"next_agent: {next_agent}",
        'reason: "Route the test handoff."',
    ]
    if epic_id is not None:
        lines.append(f"epic_id: {epic_id}")
    if story_id is not None:
        lines.append(f"story_id: {story_id}")
    if remaining is not None:
        if remaining:
            lines.append("remaining_stories:")
            lines.extend(f'  - "{entry}"' for entry in remaining)
        else:
            lines.append("remaining_stories: []")
    if extra:
        lines.append(extra)
    lines.append(f"producer: {producer}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


PRIOR = _handoff(
    story_id="S1",
    remaining=["S1 First story", "S2 Second story", "S3 Third story"],
)


@pytest.mark.parametrize(
    ("entry", "token"),
    [
        ("S2 Second story", "S2"),
        ("S2: Second story", "S2"),
        ("**S2** Second story", "S2"),
        ("`ABC-0d` Treat errors as failures", "ABC-0d"),
        ("   ", None),
        (None, None),
    ],
)
def test_story_id_token_reads_the_leading_id(entry: str | None, token: str | None) -> None:
    assert story_id_token(entry) == token


def test_kept_stories_pass() -> None:
    current = _handoff(remaining=["S1 First story", "S2 Second story", "S3 Third story"])

    assert story_list_errors(PRIOR, current) == []


def test_closed_story_may_leave_the_list() -> None:
    current = _handoff(story_id="S1", remaining=["S2 Second story", "S3 Third story"])

    assert story_list_errors(PRIOR, current) == []


def test_newly_assigned_story_may_move_to_story_id() -> None:
    current = _handoff(story_id="S2", remaining=["S3 Third story"])

    assert story_list_errors(PRIOR, current) == []


def test_silently_dropped_story_fails_and_names_the_entry() -> None:
    current = _handoff(story_id="S1", remaining=["S2 Second story"])

    errors = story_list_errors(PRIOR, current)

    assert len(errors) == 1
    assert errors[0].startswith("remaining_stories_dropped:")
    assert "`S3 Third story`" in errors[0]
    assert "S2 Second story" not in errors[0]
    assert "## Dropped Stories" in errors[0]


def test_rewrite_without_remaining_stories_fails_for_every_open_story() -> None:
    # The auditor rewrote the handoff and left the field out entirely.
    current = _handoff(story_id="S1", remaining=None)

    errors = story_list_errors(PRIOR, current)

    assert len(errors) == 1
    assert "`S2 Second story`" in errors[0]
    assert "`S3 Third story`" in errors[0]
    assert "S1 First story" not in errors[0]


def test_rewrite_without_epic_id_is_still_the_same_epic() -> None:
    current = _handoff(epic_id=None, story_id="S1", remaining=["S2 Second story"])

    assert story_list_errors(PRIOR, current) != []


def test_new_epic_skips_the_comparison() -> None:
    current = _handoff(epic_id="EPIC-B", story_id="T1", remaining=["T2 Next epic story"])

    assert story_list_errors(PRIOR, current) == []


def test_story_ids_match_across_title_case_and_punctuation_changes() -> None:
    current = _handoff(
        story_id="s1",
        remaining=["s2: Second story, renamed", "**S3** Third story"],
    )

    assert story_list_errors(PRIOR, current) == []


def test_dropped_stories_section_with_a_reason_passes() -> None:
    current = _handoff(
        story_id="S1",
        remaining=["S2 Second story"],
        body=(
            "# Handoff\n\n## Dropped Stories\n\n"
            "- S3: Superseded by S2; the user removed it from the plan.\n\n"
            "## Next Step\n\nScope S2.\n"
        ),
    )

    assert story_list_errors(PRIOR, current) == []


def test_dropped_stories_entry_without_a_reason_fails() -> None:
    current = _handoff(
        story_id="S1",
        remaining=["S2 Second story"],
        body="# Handoff\n\n## Dropped Stories\n\n- **S3**:\n",
    )

    errors = story_list_errors(PRIOR, current)

    assert len(errors) == 1
    assert "without a reason" in errors[0]
    assert "`S3 Third story`" in errors[0]


def test_dropped_stories_section_ends_at_the_next_heading() -> None:
    content = (
        "## Dropped Stories\n\n- S3: Out of scope.\n\n## Next Step\n\n- S4: not dropped\n"
    )

    assert dropped_story_reasons(content) == {"s3": "Out of scope."}


def test_title_only_entries_sharing_a_first_word_stay_distinct() -> None:
    assert story_id_token("Add login") is None
    prior = _handoff(story_id=None, remaining=["Add login", "Add billing"])
    current = _handoff(story_id=None, remaining=["Add login"])

    errors = story_list_errors(prior, current)

    assert len(errors) == 1
    assert "`Add billing`" in errors[0]
    assert "`Add login`" not in errors[0]


def test_title_only_story_may_close_or_be_dropped_with_a_reason() -> None:
    prior = _handoff(
        story_id="Add login",
        remaining=["Add login", "Add billing", "Add reports"],
    )
    current = _handoff(
        story_id="Add login",
        remaining=["add  billing"],
        body=(
            "# Handoff\n\n## Dropped Stories\n\n"
            "- **Add reports**: Moved to the next epic.\n"
        ),
    )

    assert story_list_errors(prior, current) == []


def test_title_only_drop_without_a_reason_fails() -> None:
    prior = _handoff(story_id=None, remaining=["Add login", "Add billing"])
    current = _handoff(
        story_id=None,
        remaining=["Add login"],
        body="# Handoff\n\n## Dropped Stories\n\n- Add billing\n",
    )

    errors = story_list_errors(prior, current)

    assert len(errors) == 1
    assert "without a reason" in errors[0]


def test_duplicate_story_keys_are_counted() -> None:
    prior = _handoff(story_id="S1", remaining=["S2 Login form", "S2 Login API"])
    current = _handoff(story_id="S1", remaining=["S2 Login form"])

    errors = story_list_errors(prior, current)

    assert len(errors) == 1
    assert "`S2 Login API`" in errors[0]


def test_project_state_warning_matches_title_only_entries_by_full_title() -> None:
    current = _handoff(remaining=["Add login", "Add billing"])

    warnings = project_state_story_warnings(
        current, "# State\n\n- Add login: in progress\n- Add reports\n"
    )

    assert len(warnings) == 1
    assert "`Add billing`" in warnings[0]
    assert "Add login" not in warnings[0]


def test_unparseable_handoffs_are_left_to_the_frontmatter_checks() -> None:
    assert story_list_errors("no frontmatter", _handoff(remaining=[])) == []
    assert story_list_errors(PRIOR, "no frontmatter") == []


def test_project_state_warning_names_stories_it_does_not_list() -> None:
    current = _handoff(remaining=["S2 Second story", "S10 Tenth story"])

    warnings = project_state_story_warnings(
        current, "# State\n\n- S1: done\n- S10: Tenth story\n"
    )

    assert len(warnings) == 1
    assert warnings[0].startswith("remaining_story_not_in_project_state:")
    assert "`S2 Second story`" in warnings[0]
    assert "S10" not in warnings[0]


def test_project_state_warning_does_not_match_an_id_prefix() -> None:
    current = _handoff(remaining=["ABC-0 Base story"])

    assert project_state_story_warnings(current, "- ABC-0d: other story\n") != []
    assert project_state_story_warnings(current, "- ABC-0: Base story\n") == []


def test_project_state_warning_skips_a_missing_project_state_file() -> None:
    assert project_state_story_warnings(_handoff(remaining=["S2 x"]), None) == []


def test_validator_fails_a_rewrite_that_drops_a_story() -> None:
    current = _handoff(story_id="S1", remaining=["S2 Second story"])

    result = validate_handoff_text(current, "auditor", prior_handoff_content=PRIOR)

    assert result.verdict == "NO"
    assert any(error.startswith("remaining_stories_dropped:") for error in result.errors)


def test_validator_skips_the_story_check_without_a_prior_handoff() -> None:
    current = _handoff(story_id="S1", remaining=["S2 Second story"])

    result = validate_handoff_text(current, "auditor")

    assert not any("remaining_stories_dropped" in error for error in result.errors)


def test_validator_reads_the_project_state_file_for_the_warning(tmp_path: Path) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(
        _handoff(story_id="S1", remaining=["S2 Second story"]), encoding="utf-8"
    )
    state_path = tmp_path / "PROJECT_STATE.md"
    state_path.write_text("# State\n\n- S1: First story\n", encoding="utf-8")

    result = validate_handoff(
        handoff_path, "auditor", project_state_path=state_path
    )

    assert any(
        warning.startswith("remaining_story_not_in_project_state:")
        and "S2 Second story" in warning
        for warning in result.warnings
    )


def test_validator_skips_the_project_state_warning_in_tracker_mode() -> None:
    current = _handoff(story_id="S1", remaining=["S2 Second story"])

    result = validate_handoff_text(
        current,
        "auditor",
        project_state_content="# State\n",
        tracker_provider="linear",
    )

    assert not any(
        "remaining_story_not_in_project_state" in warning for warning in result.warnings
    )


# Orchestrator wiring: the post-dispatch gate, producer repair, and planner
# recovery all compare against the handoff the failing dispatch started from.

_START = _handoff(
    next_agent="auditor",
    producer="backend",
    story_id="S1",
    remaining=["S1 First story", "S2 Second story", "S3 Third story"],
    body="## Next Step\n\n- **auditor:** Audit the current story.\n",
)
_AUDIT_BODY = (
    "# Auditor Handback\n\n**Agent:** auditor\n\n## Audit Summary\n\n"
    "Audit complete with verification.\n"
)
_PROJECT_STATE = "# PROJECT STATE\n\n- S1: First\n- S2: Second\n- S3: Third\n"


def _audit_handoff(remaining: list[str] | None) -> str:
    return _handoff(
        story_id="S1",
        remaining=remaining,
        extra="scope_sha: 59206fb\nclose_type: story",
        body=_AUDIT_BODY,
    )


def _repo(tmp_path: Path) -> tuple[Any, Any, Path]:
    handoff_path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text(_START, encoding="utf-8")
    (tmp_path / "PROJECT_STATE.md").write_text(_PROJECT_STATE, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("dispatch test fixture\n", encoding="utf-8")
    config_module = importlib.import_module("llm_handoff.config")
    config = config_module.DispatchConfig(
        repo_root=tmp_path,
        pause_recovery="off",
        poll_interval_seconds=0,
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        enforce_work_packet=False,
        work_packet_opt_out_reason="Tests cover routing without the gate.",
    )
    return importlib.import_module("llm_handoff.orchestrator"), config, handoff_path


def _subagent_result() -> SubagentResult:
    return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


def test_run_loop_fails_the_gate_and_repairs_a_dropped_story(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, config, handoff_path = _repo(tmp_path)
    log_messages: list[tuple[str, str]] = []
    prompts: list[str] = []

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        prompts.append(prompt)
        if len(prompts) == 1:
            handoff_path.write_text(_audit_handoff(["S2 Second story"]), encoding="utf-8")
        else:
            handoff_path.write_text(
                _audit_handoff(["S2 Second story", "S3 Third story"]),
                encoding="utf-8",
            )
        return _subagent_result()

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(orchestrator, "_git_head", Mock(side_effect=["a" * 40, "b" * 40]))
    monkeypatch.setattr(
        orchestrator,
        "_git_dirty_files",
        Mock(side_effect=[("docs/handoff/HANDOFF.md",), ()]),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git_changed_files_since",
        Mock(return_value=("docs/handoff/HANDOFF.md",)),
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    dropped_errors = [
        message
        for level, message in log_messages
        if level == "ERROR" and message.startswith("remaining_stories_dropped:")
    ]
    assert len(dropped_errors) == 1
    assert "`S3 Third story`" in dropped_errors[0]
    assert len(prompts) == 2
    assert "remaining_stories_dropped" in prompts[1]
    assert (
        "AGENT",
        "Producer handoff hygiene repair passed post-checks.",
    ) in log_messages


def test_run_loop_checks_planner_recovery_against_the_dropped_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, config, handoff_path = _repo(tmp_path)
    log_messages: list[tuple[str, str]] = []
    planner_instructions: list[str | None] = []

    def fake_subagent(subagent_name: str, prompt: str, *, log=None) -> SubagentResult:
        # Both the audit and its repair leave S3 out.
        handoff_path.write_text(_audit_handoff(["S2 Second story"]), encoding="utf-8")
        return _subagent_result()

    def fake_planner(
        path: Path,
        *,
        additional_instruction: str | None = None,
        **_kwargs: Any,
    ) -> DispatchResult:
        planner_instructions.append(additional_instruction)
        # The recovering planner starts from the auditor's rewrite, which no
        # longer names S3, and rebuilds the list without it.
        path.write_text(
            _handoff(
                next_agent="backend",
                producer="planner",
                story_id="S2",
                remaining=None,
                body="# Task Assignment\n\n**Agent:** backend\n\nImplement S2.\n",
            ),
            encoding="utf-8",
        )
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_subagent)
    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
    monkeypatch.setattr(orchestrator, "_git_head", Mock(return_value="a" * 40))
    monkeypatch.setattr(
        orchestrator,
        "_git_dirty_files",
        Mock(return_value=("docs/handoff/HANDOFF.md",)),
    )
    monkeypatch.setattr(
        orchestrator, "_git_changed_files_since", Mock(return_value=())
    )

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=3,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert len(planner_instructions) == 1
    assert "`S3 Third story`" in (planner_instructions[0] or "")
    assert exit_code == 1
    planner_errors = [
        message
        for level, message in log_messages
        if level == "ERROR" and message.startswith("remaining_stories_dropped:")
    ]
    # The repair fails its HEAD invariant before revalidating, so the audit
    # and the planner recovery report the drop.
    assert len(planner_errors) == 2
    assert (
        "ERROR",
        "Planner failed to operationalize HANDOFF.md after producer hygiene "
        "repair was exhausted.",
    ) in log_messages


def test_run_loop_passes_a_planner_rewrite_that_explains_the_drop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, config, handoff_path = _repo(tmp_path)
    handoff_path.write_text(
        _handoff(
            next_agent="planner",
            producer="auditor",
            story_id="S1",
            remaining=["S2 Second story", "S3 Third story"],
            body=_AUDIT_BODY,
        ),
        encoding="utf-8",
    )
    log_messages: list[tuple[str, str]] = []

    def fake_planner(path: Path, **_kwargs: Any) -> DispatchResult:
        path.write_text(
            _handoff(
                next_agent="backend",
                producer="planner",
                story_id="S2",
                remaining=[],
                body=(
                    "# Task Assignment\n\n**Agent:** backend\n\n"
                    "## Dropped Stories\n\n- S3: Merged into S2 by the user.\n"
                ),
            ),
            encoding="utf-8",
        )
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)

    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert not any(
        message.startswith("remaining_stories_dropped:") for _level, message in log_messages
    )


_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/handoff/HANDBOOK.md",
        "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
        "docs/handoff/AUDITOR_HANDOFF_PROMPT.md",
        ".claude/agents/planner.md",
        ".claude/agents/auditor.md",
        ".codex/skills/llm-handoff-planner/SKILL.md",
        ".codex/skills/llm-handoff-auditor/SKILL.md",
        ".agents/agents/planner.md",
        ".agents/agents/auditor.md",
    ],
)
def test_shipped_prompts_explain_dropped_stories(relative_path: str) -> None:
    for root in (_REPO_ROOT, _REPO_ROOT / "examples" / "reference-workflow"):
        content = (root / relative_path).read_text(encoding="utf-8")
        assert "## Dropped Stories" in content, root / relative_path
        if "planner" in relative_path.lower():
            assert "add `PROJECT_STATE.md` to Files in" in content, root / relative_path


def test_finalizer_prompt_records_listed_stories_in_project_state() -> None:
    ledger = importlib.import_module("llm_handoff.ledger")

    prompt = ledger.ledger_updater_prompt(project_state_path=Path("docs/STATE.md"))

    assert (
        "add every story that docs/handoff/HANDOFF.md names in remaining_stories "
        "and docs/STATE.md does not list yet"
    ) in prompt
