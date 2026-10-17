"""Tracker lease frontmatter fields: tracker_issue, tracker_url, selection_basis."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llm_handoff import audit_review, doctor, orchestrator
from llm_handoff.audit_review import (
    MergeDecision,
    QualityReviewVerdict,
    ReviewScope,
    ReviewVerdict,
    ReviewerRun,
)
from llm_handoff.router import (
    HandoffRouting,
    parse_handoff_frontmatter_text,
    repair_handoff_frontmatter_text,
)
from llm_handoff.validator import (
    validate_handoff_frontmatter,
    validate_handoff_text,
)


ISSUE = "ENG-42"
URL = "https://linear.app/example/issue/ENG-42/add-the-thing"
TRACKER_CODES = (
    "frontmatter_selection_basis_invalid",
    "frontmatter_tracker_url_invalid",
    "tracker_issue_missing",
    "tracker_url_missing",
    "tracker_story_id_mismatch",
    "selection_basis_missing",
    "tracker_fields_ignored",
)


def _routing(**fields: object) -> HandoffRouting:
    base: dict[str, object] = {
        "next_agent": "backend",
        "reason": "Assigning the story.",
        "producer": "planner",
        "story_id": ISSUE,
        "tracker_issue": ISSUE,
        "tracker_url": URL,
        "selection_basis": "priority",
    }
    base.update(fields)
    return HandoffRouting.model_validate(
        {key: value for key, value in base.items() if value is not None}
    )


def _codes(messages: list[str]) -> list[str]:
    return [message.split(":", 1)[0] for message in messages]


def _linear(parsed: HandoffRouting):
    return validate_handoff_frontmatter(parsed, tracker_provider="linear")


def _vanilla(parsed: HandoffRouting):
    return validate_handoff_frontmatter(parsed, tracker_provider="none")


# --------------------------------------------------------------------------
# parsing and recovery
# --------------------------------------------------------------------------


def test_frontmatter_parses_all_three_lease_fields() -> None:
    parsed = parse_handoff_frontmatter_text(
        "---\n"
        "next_agent: backend\n"
        'reason: "Assign."\n'
        f"story_id: {ISSUE}\n"
        f"tracker_issue: '  {ISSUE}  '\n"
        f"tracker_url: {URL}\n"
        "selection_basis: manual_order\n"
        "producer: planner\n"
        "---\n"
    )

    assert parsed is not None
    assert parsed.tracker_issue == ISSUE
    assert parsed.tracker_url == URL
    assert parsed.selection_basis == "manual_order"


def test_frontmatter_without_lease_fields_leaves_them_none() -> None:
    parsed = parse_handoff_frontmatter_text(
        "---\nnext_agent: backend\nreason: \"Assign.\"\nproducer: planner\n---\n"
    )

    assert parsed is not None
    assert parsed.tracker_issue is None
    assert parsed.tracker_url is None
    assert parsed.selection_basis is None


def test_frontmatter_recovery_keeps_lease_fields() -> None:
    # The unquoted `: ` in reason breaks YAML, so recovery must rebuild the
    # mapping; it must not give up on the lease keys.
    content = (
        "---\n"
        "next_agent: backend\n"
        "reason: Assign: the next issue\n"
        f"story_id: {ISSUE}\n"
        f"tracker_issue: {ISSUE}\n"
        f"tracker_url: {URL}\n"
        "selection_basis: created_at\n"
        "producer: planner\n"
        "---\n"
        "\n# Body\n"
    )

    repair = repair_handoff_frontmatter_text(content)

    assert repair.repaired is True
    parsed = parse_handoff_frontmatter_text(repair.content)
    assert parsed is not None
    assert parsed.reason == "Assign: the next issue"
    assert parsed.tracker_issue == ISSUE
    assert parsed.tracker_url == URL
    assert parsed.selection_basis == "created_at"


# --------------------------------------------------------------------------
# both modes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["none", "linear"])
@pytest.mark.parametrize("basis", ["priority", "manual_order", "created_at"])
def test_valid_selection_basis_is_accepted(provider: str, basis: str) -> None:
    result = validate_handoff_frontmatter(
        _routing(selection_basis=basis), tracker_provider=provider
    )

    assert "frontmatter_selection_basis_invalid" not in _codes(result.errors)


@pytest.mark.parametrize("provider", ["none", "linear"])
def test_invalid_selection_basis_is_an_error(provider: str) -> None:
    result = validate_handoff_frontmatter(
        _routing(selection_basis="vibes"), tracker_provider=provider
    )

    matches = [
        error
        for error in result.errors
        if error.startswith("frontmatter_selection_basis_invalid:")
    ]
    assert len(matches) == 1
    assert "`vibes`" in matches[0]
    assert result.verdict == "NO"


@pytest.mark.parametrize("provider", ["none", "linear"])
@pytest.mark.parametrize("bad_url", ["", "   ", "https://linear.app/a b"])
def test_invalid_tracker_url_is_an_error(provider: str, bad_url: str) -> None:
    result = validate_handoff_frontmatter(
        _routing(tracker_url=bad_url), tracker_provider=provider
    )

    assert "frontmatter_tracker_url_invalid" in _codes(result.errors)
    assert result.verdict == "NO"


# --------------------------------------------------------------------------
# Linear mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize("next_agent", ["backend", "frontend", "auditor"])
def test_linear_lease_route_with_all_fields_passes(next_agent: str) -> None:
    result = _linear(_routing(next_agent=next_agent))

    assert not [code for code in _codes(result.errors) if code in TRACKER_CODES]
    assert not [code for code in _codes(result.warnings) if code in TRACKER_CODES]


@pytest.mark.parametrize("next_agent", ["backend", "frontend", "auditor"])
@pytest.mark.parametrize("blank", [None, "", "  "])
def test_linear_missing_tracker_issue_is_an_error(
    next_agent: str, blank: str | None
) -> None:
    result = _linear(_routing(next_agent=next_agent, tracker_issue=blank))

    assert "tracker_issue_missing" in _codes(result.errors)
    assert "tracker_story_id_mismatch" not in _codes(result.errors)


@pytest.mark.parametrize("next_agent", ["backend", "frontend", "auditor"])
def test_linear_missing_tracker_url_is_an_error(next_agent: str) -> None:
    result = _linear(_routing(next_agent=next_agent, tracker_url=None))

    assert "tracker_url_missing" in _codes(result.errors)


@pytest.mark.parametrize("story_id", ["ENG-41", None])
def test_linear_story_id_must_equal_tracker_issue(story_id: str | None) -> None:
    result = _linear(_routing(story_id=story_id))

    matches = [
        error
        for error in result.errors
        if error.startswith("tracker_story_id_mismatch:")
    ]
    assert len(matches) == 1
    assert f"`{ISSUE}`" in matches[0]
    if story_id is not None:
        assert f"`{story_id}`" in matches[0]


@pytest.mark.parametrize("next_agent", ["backend", "frontend"])
def test_linear_planner_assignment_requires_selection_basis(next_agent: str) -> None:
    result = _linear(_routing(next_agent=next_agent, selection_basis=None))

    assert "selection_basis_missing" in _codes(result.errors)


def test_linear_selection_basis_not_required_from_non_planner() -> None:
    result = _linear(
        _routing(next_agent="auditor", producer="backend", selection_basis=None)
    )

    assert "selection_basis_missing" not in _codes(result.errors)


@pytest.mark.parametrize("next_agent", ["planner", "user"])
def test_linear_planner_and_user_routes_are_exempt(next_agent: str) -> None:
    result = _linear(
        _routing(
            next_agent=next_agent,
            producer="auditor",
            story_id="OTHER-1",
            tracker_issue=None,
            tracker_url=None,
            selection_basis=None,
        )
    )

    codes = _codes(result.errors) + _codes(result.warnings)
    assert not [code for code in codes if code in TRACKER_CODES]


def test_linear_mode_does_not_emit_ignored_warning() -> None:
    result = _linear(_routing())

    assert "tracker_fields_ignored" not in _codes(result.warnings)


# --------------------------------------------------------------------------
# vanilla mode
# --------------------------------------------------------------------------


def test_vanilla_mode_warns_once_naming_present_fields() -> None:
    result = _vanilla(_routing(selection_basis=None))

    ignored = [
        warning
        for warning in result.warnings
        if warning.startswith("tracker_fields_ignored:")
    ]
    assert len(ignored) == 1
    assert "tracker_issue" in ignored[0]
    assert "tracker_url" in ignored[0]
    assert "selection_basis" not in ignored[0]
    assert not [code for code in _codes(result.errors) if code in TRACKER_CODES]
    assert result.verdict == "WARNINGS-ONLY"


def test_vanilla_mode_skips_linear_rules() -> None:
    result = _vanilla(
        _routing(story_id="OTHER-1", tracker_url=None, selection_basis=None)
    )

    assert not [code for code in _codes(result.errors) if code in TRACKER_CODES]
    assert result.verdict != "NO"


_VANILLA_HANDOFF = (
    "---\n"
    "next_agent: backend\n"
    'reason: "Assign the story."\n'
    'story_id: "S-1"\n'
    "producer: planner\n"
    "---\n"
    "\n"
    "# Handoff\n"
    "\n"
    "## Work Packet\n"
    "\n"
    "- **Objective:** Do the thing.\n"
    "- **Files in scope:** `src/app.py`\n"
    "- **Files out of bounds:** none\n"
    "- **Context:** none\n"
    "- **Verification command:**\n"
    "  - python -m pytest tests -q\n"
    "- **Expected next route:** auditor\n"
)


def test_vanilla_handoff_without_fields_is_unchanged() -> None:
    default = validate_handoff_text(_VANILLA_HANDOFF, "planner")
    explicit = validate_handoff_text(
        _VANILLA_HANDOFF, "planner", tracker_provider="none"
    )

    assert explicit == default
    codes = _codes(default.errors) + _codes(default.warnings)
    assert not [code for code in codes if code in TRACKER_CODES]


def test_validate_handoff_text_passes_tracker_provider_through() -> None:
    linear = validate_handoff_text(
        _VANILLA_HANDOFF, "planner", tracker_provider="linear"
    )

    assert "tracker_issue_missing" in _codes(linear.errors)
    assert "selection_basis_missing" in _codes(linear.errors)


def test_doctor_check_handoff_uses_tracker_provider(tmp_path: Path) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_text(_VANILLA_HANDOFF, encoding="utf-8")

    def _run(provider: str) -> list[doctor.DoctorCheck]:
        checks: list[doctor.DoctorCheck] = []
        doctor._check_handoff(
            tmp_path,
            handoff,
            dispatcher_commit_mode=False,
            git_available=False,
            checks=checks,
            tracker_provider=provider,
        )
        return checks

    linear_fails = [
        check.message for check in _run("linear") if check.status == "FAIL"
    ]
    vanilla_fails = [
        check.message for check in _run("none") if check.status == "FAIL"
    ]

    assert any(detail.startswith("tracker_issue_missing:") for detail in linear_fails)
    assert not any(
        detail.startswith("tracker_issue_missing:") for detail in vanilla_fails
    )


# --------------------------------------------------------------------------
# repair prompts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        orchestrator.POST_DISPATCH_HANDOFF_HYGIENE_REPAIR_PROMPT_TEMPLATE,
        orchestrator.POST_DISPATCH_HANDOFF_HYGIENE_PLANNER_RECOVERY_TEMPLATE,
    ],
)
def test_repair_templates_preserve_lease_fields(template: str) -> None:
    preserve = template[template.index("Preserve") :]
    for name in ("tracker_issue", "tracker_url", "selection_basis"):
        assert name in preserve


# --------------------------------------------------------------------------
# audit fan-out handoff
# --------------------------------------------------------------------------


def _render_audit(tmp_path: Path, frontmatter: HandoffRouting) -> str:
    def _run(name: str, verdict: ReviewVerdict) -> ReviewerRun:
        return ReviewerRun(
            name=name,
            provider="claude",
            command=(),
            exit_code=0,
            elapsed_seconds=0.0,
            stdout="",
            stderr="",
            verdict=verdict,
        )

    quality = QualityReviewVerdict(
        verdict="pass", next_agent="planner", reason="Looks good."
    )
    return audit_review.render_audit_handoff(
        frontmatter=frontmatter,
        scope=ReviewScope(
            base_sha="a" * 40,
            head_sha="b" * 40,
            files=("src/app.py",),
            diff_path=tmp_path / "scope.diff",
        ),
        runs={
            "quality": _run("quality", quality),
            "security": _run("security", ReviewVerdict(verdict="pass")),
        },
        quality=quality,
        decision=MergeDecision(blocked=False, blocking=(), notes=(), reasons=()),
        implementer_role="backend",
        block_severity="high",
    )


def _frontmatter_block(content: str) -> dict[str, object]:
    return yaml.safe_load(content.split("---", 2)[1])


def test_audit_handoff_carries_lease_fields(tmp_path: Path) -> None:
    rendered = _render_audit(
        tmp_path, _routing(next_agent="auditor", producer="backend")
    )

    loaded = _frontmatter_block(rendered)
    assert loaded["tracker_issue"] == ISSUE
    assert loaded["tracker_url"] == URL
    assert loaded["selection_basis"] == "priority"
    keys = list(loaded)
    assert keys.index("tracker_issue") == keys.index("story_id") + 1
    assert keys.index("tracker_url") == keys.index("story_id") + 2
    assert keys.index("selection_basis") == keys.index("story_id") + 3


def test_audit_handoff_omits_absent_lease_fields(tmp_path: Path) -> None:
    rendered = _render_audit(
        tmp_path,
        _routing(
            next_agent="auditor",
            producer="backend",
            tracker_issue=None,
            tracker_url=None,
            selection_basis=None,
        ),
    )

    loaded = _frontmatter_block(rendered)
    for name in ("tracker_issue", "tracker_url", "selection_basis"):
        assert name not in loaded
        assert name not in rendered


# --------------------------------------------------------------------------
# selection callout
# --------------------------------------------------------------------------


def _callout(frontmatter: HandoffRouting | None, provider: str) -> list[tuple[str, str]]:
    logged: list[tuple[str, str]] = []
    orchestrator._log_tracker_selection(
        frontmatter,
        tracker_provider=provider,
        log=lambda level, message: logged.append((level, message)),
    )
    return logged


@pytest.mark.parametrize(
    ("basis", "phrase"),
    [("manual_order", "by manual order"), ("created_at", "by creation date")],
)
def test_selection_callout_warns_for_non_priority_basis(
    basis: str, phrase: str
) -> None:
    logged = _callout(_routing(selection_basis=basis), "linear")

    assert logged == [
        ("WARN", f"{ISSUE} was selected without a priority ({phrase})")
    ]


def test_selection_callout_falls_back_to_story_id() -> None:
    logged = _callout(
        _routing(tracker_issue=None, story_id="S-9", selection_basis="created_at"),
        "linear",
    )

    assert logged == [
        ("WARN", "S-9 was selected without a priority (by creation date)")
    ]


def test_selection_callout_silent_for_priority() -> None:
    assert _callout(_routing(selection_basis="priority"), "linear") == []


@pytest.mark.parametrize("basis", ["manual_order", "created_at"])
def test_selection_callout_silent_in_vanilla_mode(basis: str) -> None:
    assert _callout(_routing(selection_basis=basis), "none") == []


def test_selection_callout_silent_for_non_planner_producer() -> None:
    assert (
        _callout(_routing(producer="backend", selection_basis="manual_order"), "linear")
        == []
    )


def test_selection_callout_silent_without_frontmatter() -> None:
    assert _callout(None, "linear") == []
