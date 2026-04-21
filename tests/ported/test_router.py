from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff.router import (
    HandoffRouting,
    RoutingDecision,
    parse_handoff_frontmatter,
    parse_handoff_frontmatter_text,
    repair_handoff_frontmatter_text,
    route,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "handoffs"
LEGACY_FRONTMATTER_WARNING = (
    "Deprecated HANDOFF routing format: missing YAML routing frontmatter; "
    "falling back to legacy prose routing."
)

CLAUDE_MULTI_STORY_EPIC = """
## 2. CURRENT STATUS
- **Active Epic:** Security & Supply Chain Hardening — ACTIVE.
- **Remaining stories:** Story 2, Story 3, Story 4.
""".strip()


def _fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _legacy(expected: RoutingDecision) -> RoutingDecision:
    return RoutingDecision(
        route=expected.route,
        confidence=expected.confidence,
        source=expected.source,
        reasoning=expected.reasoning,
        warnings=[*expected.warnings, LEGACY_FRONTMATTER_WARNING],
    )


def test_parse_handoff_frontmatter_reads_required_and_optional_fields(
    tmp_path: Path,
) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(
        """---
next_agent: claude-audit
reason: Story complete; audit requested.
scope_sha: 82ce839
close_type: story
prior_sha: 3407c66
producer: codex
---

# Codex Handback
""",
        encoding="utf-8",
    )

    assert parse_handoff_frontmatter(handoff_path) == HandoffRouting(
        next_agent="claude-audit",
        reason="Story complete; audit requested.",
        scope_sha="82ce839",
        close_type="story",
        prior_sha="3407c66",
        producer="codex",
    )


def test_parse_handoff_frontmatter_reads_scope_metadata(
    tmp_path: Path,
) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(
        """---
next_agent: codex
reason: Implement the next synthesis story.
epic_id: E-SYN-1
story_id: E-SYN-1-S1
story_title: Synthesis Schema Update
remaining_stories:
  - E-SYN-1-S2 HTML Export Template Redesign
producer: gemini-pe
---

# Task Assignment
""",
        encoding="utf-8",
    )

    assert parse_handoff_frontmatter(handoff_path) == HandoffRouting(
        next_agent="codex",
        reason="Implement the next synthesis story.",
        producer="gemini-pe",
        epic_id="E-SYN-1",
        story_id="E-SYN-1-S1",
        story_title="Synthesis Schema Update",
        remaining_stories=("E-SYN-1-S2 HTML Export Template Redesign",),
    )


def test_parse_handoff_frontmatter_accepts_utf16_le_bom(
    tmp_path: Path,
) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(
        """---
next_agent: claude-audit
reason: manual frontend handback ready for audit.
scope_sha: 236f82f
close_type: story
producer: gemini-frontend
---

# Frontend Handback
""",
        encoding="utf-16",
    )

    assert parse_handoff_frontmatter(handoff_path) == HandoffRouting(
        next_agent="claude-audit",
        reason="manual frontend handback ready for audit.",
        scope_sha="236f82f",
        close_type="story",
        producer="gemini-frontend",
    )


def test_parse_handoff_frontmatter_returns_none_when_block_missing() -> None:
    assert (
        parse_handoff_frontmatter_text("# Legacy Handoff\n\nNext: dispatch Codex")
        is None
    )


def test_repair_handoff_frontmatter_quotes_reason_with_colon() -> None:
    handoff_content = """---
next_agent: codex
reason: Dispatch E2-S1: Implement backend asyncio.as_completed loop.
producer: gemini-pe
---

## Task Assignment
"""

    repair = repair_handoff_frontmatter_text(handoff_content)

    assert repair.repaired is True
    assert parse_handoff_frontmatter_text(repair.content) == HandoffRouting(
        next_agent="codex",
        reason="Dispatch E2-S1: Implement backend asyncio.as_completed loop.",
        producer="gemini-pe",
    )
    assert repair.warnings == (
        "Auto-repaired HANDOFF YAML frontmatter by quoting reason.",
    )


def test_route_uses_frontmatter_before_legacy_prose() -> None:
    handoff_content = """---
next_agent: codex
reason: Implement the next backend story.
producer: gemini-pe
---

## Next Step

- **Claude Code:** Audit this stale prose.
"""

    assert route(handoff_content) == RoutingDecision(
        route="Codex",
        confidence="HIGH",
        source="frontmatter.next_agent",
        reasoning="YAML routing frontmatter routes work to Codex.",
        warnings=[],
    )


@pytest.mark.parametrize(
    ("next_agent", "expected_route", "extra_frontmatter"),
    [
        ("claude-audit", "ClaudeCode-Audit", ""),
        (
            "claude-ledger",
            "Epic-Close",
            "scope_sha: 82ce839\nclose_type: epic\n",
        ),
        ("codex", "Codex", ""),
        ("backend", "Codex", ""),
        ("gemini-pe", "Gemini-PE", ""),
        ("planner", "Gemini-PE", ""),
        ("gemini-frontend", "Gemini-Frontend", ""),
        ("manual frontend", "Gemini-Frontend", ""),
        ("user", "Escalation", ""),
    ],
)
def test_route_supports_all_frontmatter_next_agent_values(
    next_agent: str,
    expected_route: str,
    extra_frontmatter: str,
) -> None:
    producer = "claude-audit" if next_agent == "claude-ledger" else "test"
    handoff_content = f"""---
next_agent: {next_agent}
reason: Route enum value for dispatch.
{extra_frontmatter}producer: {producer}
---

# Handoff
"""

    decision = route(handoff_content)

    assert decision.route == expected_route
    assert decision.confidence == "HIGH"
    assert decision.source == "frontmatter.next_agent"


def test_route_normalizes_frontend_frontmatter_alias() -> None:
    handoff_content = """---
next_agent: frontend
reason: Assign frontend implementation work.
producer: gemini-pe
---

# Handoff
"""

    assert route(handoff_content) == RoutingDecision(
        route="Gemini-Frontend",
        confidence="HIGH",
        source="frontmatter.next_agent",
        reasoning="YAML routing frontmatter routes work to Gemini-Frontend.",
        warnings=[
            "frontend frontmatter alias normalized to Gemini-Frontend."
        ],
    )


def test_route_normalizes_backend_frontmatter_alias() -> None:
    handoff_content = """---
next_agent: backend
reason: Hand off backend implementation to Codex.
producer: frontend
---

# Handoff
"""

    assert route(handoff_content) == RoutingDecision(
        route="Codex",
        confidence="HIGH",
        source="frontmatter.next_agent",
        reasoning="YAML routing frontmatter routes work to Codex.",
        warnings=["backend frontmatter alias normalized to Codex."],
    )


def test_route_normalizes_planner_frontmatter_alias() -> None:
    handoff_content = """---
next_agent: planner
reason: Return to PE scoping.
producer: claude-audit
---

# Handoff
"""

    assert route(handoff_content) == RoutingDecision(
        route="Gemini-PE",
        confidence="HIGH",
        source="frontmatter.next_agent",
        reasoning="YAML routing frontmatter routes work to Gemini-PE.",
        warnings=["planner frontmatter alias normalized to Gemini-PE."],
    )


def test_route_preserves_user_escalation_when_optional_prior_sha_is_invalid() -> None:
    handoff_content = """---
next_agent: user
reason: PO decision required before continuing.
prior_sha: 2eee66b29811e6ee4ffee970039c35474c678a39bce20d1905d1e935210864e5
producer: gemini-pe
---

## Escalation

Human decision required.
"""

    assert route(handoff_content) == RoutingDecision(
        route="Escalation",
        confidence="HIGH",
        source="frontmatter.next_agent",
        reasoning="YAML routing frontmatter names Escalation as the target agent.",
        warnings=[
            "Invalid HANDOFF routing frontmatter metadata: prior_sha must be a 7-40 character git SHA."
        ],
    )


def test_route_rejects_human_readable_frontmatter_next_agent_without_pre_normalization() -> (
    None
):
    handoff_content = """---
next_agent: Claude Code (Auditor)
reason: Frontend implementation complete; audit requested.
scope_sha: 25c45ca
close_type: story
producer: frontend (manual frontend)
---

# Handoff
"""

    assert route(handoff_content) == RoutingDecision(
        route="Unknown",
        confidence="LOW",
        source="frontmatter.invalid",
        reasoning="YAML routing frontmatter is present but invalid.",
        warnings=[
            "Invalid HANDOFF routing frontmatter: next_agent `Claude Code (Auditor)` is not recognized."
        ],
    )


def test_route_does_not_fall_back_to_legacy_when_frontmatter_is_malformed() -> None:
    handoff_content = """---
next_agent: [codex
reason: malformed YAML should stop routing
---

Next: dispatch Codex
"""

    assert route(handoff_content) == RoutingDecision(
        route="Unknown",
        confidence="LOW",
        source="frontmatter.parse_error",
        reasoning="YAML routing frontmatter is present but could not be parsed.",
        warnings=[
            "Invalid HANDOFF routing frontmatter: YAML frontmatter could not be parsed."
        ],
    )


def test_route_rejects_ledger_frontmatter_from_non_auditor_producer() -> None:
    handoff_content = """---
next_agent: claude-ledger
reason: Attempt to bypass the audit gate.
scope_sha: 82ce839
close_type: epic
producer: codex
---

# Codex Handback
"""

    assert route(handoff_content) == RoutingDecision(
        route="Unknown",
        confidence="LOW",
        source="frontmatter.invalid",
        reasoning="YAML routing frontmatter is present but invalid.",
        warnings=[
            "Invalid HANDOFF routing frontmatter: next_agent `claude-ledger` requires producer `auditor` or `claude-audit`."
        ],
    )


@pytest.mark.parametrize(
    ("fixture_name", "claude_md_content", "expected"),
    [
        pytest.param(
            "pe_task_assignment_codex.md",
            None,
            RoutingDecision(
                route="Codex",
                confidence="HIGH",
                source="task_assignment_block",
                reasoning="Task Assignment block names Codex as the target agent.",
                warnings=[],
            ),
            id="task_assignment_block_routes_to_codex",
        ),
        pytest.param(
            "codex_handback_story_close.md",
            None,
            RoutingDecision(
                route="ClaudeCode-Audit",
                confidence="HIGH",
                source="next_step_section",
                reasoning="Next Step section routes work to Claude Code for audit.",
                warnings=[],
            ),
            id="regression_handback_next_step_overrides_reporter_agent",
        ),
        pytest.param(
            "audit_story_close_next_story.md",
            None,
            RoutingDecision(
                route="Codex",
                confidence="HIGH",
                source="next_step_section",
                reasoning="Next Step section routes work to Codex.",
                warnings=[],
            ),
            id="audit_story_close_routes_to_next_story_implementer",
        ),
        pytest.param(
            "next_step_header_for_codex.md",
            None,
            RoutingDecision(
                route="Codex",
                confidence="HIGH",
                source="next_step_header",
                reasoning="Next Step header names Codex as the target agent.",
                warnings=[],
            ),
            id="regression_next_step_header_for_agent",
        ),
        pytest.param(
            "next_step_header_epic_close.md",
            None,
            RoutingDecision(
                route="Epic-Close",
                confidence="HIGH",
                source="next_step_header",
                reasoning="Next Step header routes Claude Code work to the epic-close flow.",
                warnings=[],
            ),
            id="regression_next_step_header_epic_close_from_body_context",
        ),
        pytest.param(
            "next_step_subheading_frontend.md",
            None,
            RoutingDecision(
                route="Gemini-Frontend",
                confidence="HIGH",
                source="next_step_subheading",
                reasoning="Next Step sub-heading routes work to Gemini-Frontend.",
                warnings=[],
            ),
            id="regression_next_step_subheading_agent_label",
        ),
        pytest.param(
            "next_step_qualifier_suffix_claude.md",
            None,
            RoutingDecision(
                route="ClaudeCode-Audit",
                confidence="HIGH",
                source="next_step_section",
                reasoning="Next Step section routes work to Claude Code for audit.",
                warnings=[],
            ),
            id="regression_next_step_agent_label_with_qualifier_suffix",
        ),
        pytest.param(
            "prose_next_agent_gemini_pe.md",
            None,
            RoutingDecision(
                route="Gemini-PE",
                confidence="HIGH",
                source="next_agent_prose",
                reasoning="Prose Next Agent line routes work to Gemini-PE.",
                warnings=[],
            ),
            id="regression_prose_next_agent_arrow_in_closed_handoff",
        ),
        pytest.param(
            "legacy_manual_frontend_dispatch.md",
            None,
            RoutingDecision(
                route="Gemini-Frontend",
                confidence="HIGH",
                source="canonical_dispatch_line",
                reasoning="Canonical dispatch line routes work to Gemini-Frontend.",
                warnings=[
                    "Legacy manual frontend reference normalized to Gemini-Frontend."
                ],
            ),
            id="legacy_manual_frontend_dispatch_maps_to_frontend",
        ),
        pytest.param(
            "misroute.md",
            None,
            RoutingDecision(
                route="ClaudeCode-Misroute",
                confidence="HIGH",
                source="canonical_dispatch_line",
                reasoning="Canonical dispatch line routes work to Claude Code for misroute clarification.",
                warnings=[],
            ),
            id="explicit_misroute_dispatch",
        ),
        pytest.param(
            "escalation.md",
            None,
            RoutingDecision(
                route="Escalation",
                confidence="HIGH",
                source="escalation_heading",
                reasoning="Escalation heading takes precedence over all other routing signals.",
                warnings=[],
            ),
            id="escalation_takes_priority",
        ),
        pytest.param(
            "story_close_ledger_language.md",
            CLAUDE_MULTI_STORY_EPIC,
            RoutingDecision(
                route="ClaudeCode-Audit",
                confidence="MEDIUM",
                source="next_step_section",
                reasoning="Next Step section keeps Claude Code on the audit path because the ledger wording is not an explicit epic-close signal.",
                warnings=[
                    "Finalizer wording is ambiguous; the project state shows remaining work, so Epic-Close was not selected."
                ],
            ),
            id="regression_story_close_ledger_language_is_not_epic_close",
        ),
        pytest.param(
            "conflicting_signals.md",
            None,
            RoutingDecision(
                route="Gemini-PE",
                confidence="LOW",
                source="next_step_section",
                reasoning="Next Step section overrides the Task Assignment block under router precedence.",
                warnings=[
                    "Multiple routing signals found; applied precedence order Next Step > Task Assignment."
                ],
            ),
            id="conflicting_signals_prefer_next_step_with_warning",
        ),
        pytest.param(
            "no_signal.md",
            None,
            RoutingDecision(
                route="Unknown",
                confidence="LOW",
                source="no_signal",
                reasoning="No recognizable routing signal was found in HANDOFF content.",
                warnings=["No recognized routing signal found."],
            ),
            id="no_signal_routes_to_unknown",
        ),
        pytest.param(
            "empty.md",
            None,
            RoutingDecision(
                route="Unknown",
                confidence="LOW",
                source="no_signal",
                reasoning="No recognizable routing signal was found in HANDOFF content.",
                warnings=["HANDOFF content is empty or whitespace."],
            ),
            id="empty_handoff_routes_to_unknown",
        ),
    ],
)
def test_route_matches_fixture(
    fixture_name: str,
    claude_md_content: str | None,
    expected: RoutingDecision,
) -> None:
    handoff_content = _fixture_text(fixture_name)

    assert route(handoff_content, claude_md_content=claude_md_content) == _legacy(
        expected
    )


@pytest.mark.parametrize(
    ("handoff_content", "expected"),
    [
        pytest.param(
            "Next: close epic",
            RoutingDecision(
                route="Epic-Close",
                confidence="HIGH",
                source="canonical_dispatch_line",
                reasoning="Canonical dispatch line routes work to the epic-close flow.",
                warnings=[],
            ),
            id="canonical_epic_close",
        ),
        pytest.param(
            "Next: Epic-Close",
            RoutingDecision(
                route="Epic-Close",
                confidence="HIGH",
                source="canonical_dispatch_line",
                reasoning="Canonical dispatch line routes work to the epic-close flow.",
                warnings=[],
            ),
            id="canonical_epic_close_route_name",
        ),
        pytest.param(
            "Next: dispatch Codex",
            RoutingDecision(
                route="Codex",
                confidence="HIGH",
                source="canonical_dispatch_line",
                reasoning="Canonical dispatch line routes work to Codex.",
                warnings=[],
            ),
            id="canonical_codex_dispatch",
        ),
        pytest.param(
            "Next: dispatch Claude Code",
            RoutingDecision(
                route="ClaudeCode-Audit",
                confidence="HIGH",
                source="canonical_dispatch_line",
                reasoning="Canonical dispatch line routes work to Claude Code for audit.",
                warnings=[],
            ),
            id="canonical_claude_audit_dispatch",
        ),
        pytest.param(
            "Next: dispatch Gemini",
            RoutingDecision(
                route="Gemini-PE",
                confidence="HIGH",
                source="canonical_dispatch_line",
                reasoning="Canonical dispatch line routes work to Gemini-PE.",
                warnings=[],
            ),
            id="canonical_generic_gemini_dispatch",
        ),
    ],
)
def test_route_supports_canonical_dispatch_variants(
    handoff_content: str, expected: RoutingDecision
) -> None:
    assert route(handoff_content) == _legacy(expected)


@pytest.mark.parametrize(
    ("handoff_content", "expected"),
    [
        pytest.param(
            "### Next Agent -> Claude Code",
            RoutingDecision(
                route="ClaudeCode-Audit",
                confidence="HIGH",
                source="next_agent_prose",
                reasoning="Prose Next Agent line routes work to Claude Code for audit.",
                warnings=[],
            ),
            id="prose_next_agent_claude_audit",
        ),
        pytest.param(
            "### Next Agent -> Claude Code (epic close + ledger push)",
            RoutingDecision(
                route="Epic-Close",
                confidence="HIGH",
                source="next_agent_prose",
                reasoning="Prose Next Agent line routes work to the epic-close flow.",
                warnings=[],
            ),
            id="prose_next_agent_epic_close",
        ),
        pytest.param(
            "### Next Agent -> manual frontend",
            RoutingDecision(
                route="Gemini-Frontend",
                confidence="HIGH",
                source="next_agent_prose",
                reasoning="Prose Next Agent line routes work to Gemini-Frontend.",
                warnings=[
                    "Legacy manual frontend reference normalized to Gemini-Frontend."
                ],
            ),
            id="prose_next_agent_manual_frontend",
        ),
    ],
)
def test_route_supports_prose_next_agent_variants(
    handoff_content: str, expected: RoutingDecision
) -> None:
    assert route(handoff_content) == _legacy(expected)


def test_route_uses_next_step_header_after_same_level_heading_boundary() -> None:
    handoff_content = """
    # Claude Code Audit

    ## Next Step For Codex

    Implement Story 2 after the audit closes.

    ## Verification

    - Gate clean.
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="Codex",
            confidence="HIGH",
            source="next_step_header",
            reasoning="Next Step header names Codex as the target agent.",
            warnings=[],
        )
    )


def test_route_does_not_treat_epic_close_noun_phrase_as_epic_close_instruction() -> (
    None
):
    handoff_content = """
    # Ledger Parse Failure Handling — Codex Handback

    ## Completed Work

    Addressed the Epic-Close failure mode where the ledger-updater returned prose.

    ## Next Step

    - **Claude Code:** Audit the ledger parse failure handling change and confirm it fails the Epic-Close cycle instead of rolling into stale-route pause.
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="ClaudeCode-Audit",
            confidence="HIGH",
            source="next_step_section",
            reasoning="Next Step section routes work to Claude Code for audit.",
            warnings=[],
        )
    )


def test_route_ignores_non_agent_bold_line_and_uses_header_agent() -> None:
    handoff_content = """
    # Audit Verdict

    ## Next Step For Gemini

    - **Scope:** clarify the follow-up work for Story 2.
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="Gemini-PE",
            confidence="HIGH",
            source="next_step_header",
            reasoning="Next Step header names Gemini-PE as the target agent.",
            warnings=[],
        )
    )


def test_route_routes_generic_gemini_to_frontend_when_action_demands_it() -> None:
    handoff_content = """
    ## Next Step

    - **Gemini:** Handle the React and Tailwind UI polish for the next story.
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="Gemini-Frontend",
            confidence="HIGH",
            source="next_step_section",
            reasoning="Next Step section routes work to Gemini-Frontend.",
            warnings=[],
        )
    )


def test_route_handles_next_step_claude_misroute_instruction() -> None:
    handoff_content = """
    ## Next Step

    - **Claude Code:** handle misroute clarification before re-dispatching.
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="ClaudeCode-Misroute",
            confidence="HIGH",
            source="next_step_section",
            reasoning="Next Step section routes work to Claude Code for misroute clarification.",
            warnings=[],
        )
    )


def test_route_uses_explicit_epic_close_metadata_over_claude_label() -> None:
    handoff_content = """
    ## Audit

    **Agent:** Claude Code (auditor)
    **Test SHA:** `3251966`
    **Implementation SHA:** `833da7d`
    **Verdict:** **APPROVED**
    **Close Type:** EPIC-CLOSE

    ### Suggested Next Step
    Claude Code: update `PROJECT_STATE.md`, update `PROJECT_STATE.md`, commit, and push.

    Canonical Routing Instruction:
    Next: ClaudeCode
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="Epic-Close",
            confidence="HIGH",
            source="close_type_metadata",
            reasoning="Close Type metadata declares the handoff ready for the epic-close flow.",
            warnings=[],
        )
    )


def test_route_uses_embedded_task_assignment_in_pe_review_report() -> None:
    handoff_content = """
    # Gemini PE Review

    The plan is approved and the next task assignment is below.

    ## Task Assignment

    **Agent:** Claude Code
    **Epic/Story:** Audit the completed dispatch fix

    ### Objective

    Audit the Codex implementation.

    ### Acceptance Criteria

    - Verify the routing behavior.
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="ClaudeCode-Audit",
            confidence="HIGH",
            source="task_assignment_block",
            reasoning="Task Assignment block names ClaudeCode-Audit as the target agent.",
            warnings=[],
        )
    )


def test_route_treats_ledger_close_and_push_as_epic_close_when_no_stories_remain() -> (
    None
):
    handoff_content = """
    # Dispatch Gemini Stream-JSON + Default Codex Resume — AUDIT APPROVED-WITH-NITS

    ## Next Step

    - **Claude Code (ledger close + push):**
      1. Append the ledger entry to `PROJECT_STATE.md`.
      2. Push `main` to `origin`.

    - **Gemini PE (AFTER ledger close + push):** Process the UAT remediation epic.
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="Epic-Close",
            confidence="HIGH",
            source="next_step_section",
            reasoning="Next Step section routes Claude Code work to the epic-close flow.",
            warnings=[],
        )
    )


def test_route_returns_unknown_for_unrecognized_task_assignment_agent() -> None:
    handoff_content = """
    ## Task Assignment

    **Agent:** RouterBot
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="Unknown",
            confidence="LOW",
            source="no_signal",
            reasoning="No recognizable routing signal was found in HANDOFF content.",
            warnings=["No recognized routing signal found."],
        )
    )


def test_route_returns_unknown_when_task_assignment_block_has_no_agent() -> None:
    handoff_content = """
    ## Task Assignment

    ### Objective

    Describe the next story.

    ## Notes

    No explicit agent was assigned.
    """.strip()

    assert route(handoff_content) == _legacy(
        RoutingDecision(
            route="Unknown",
            confidence="LOW",
            source="no_signal",
            reasoning="No recognizable routing signal was found in HANDOFF content.",
            warnings=["No recognized routing signal found."],
        )
    )
