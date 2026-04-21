from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from llm_handoff.router import HandoffRouting
import llm_handoff.validator as validator
from llm_handoff.validator import parse_validation_output, validate_handoff


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "validator_outputs"


def _fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _write_handoff(tmp_path: Path, content: str) -> Path:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(content, encoding="utf-8")
    return handoff_path


def _with_frontmatter(
    body: str,
    *,
    next_agent: str,
    reason: str = "Route test handoff.",
    scope_sha: str | None = None,
    close_type: str | None = None,
    prior_sha: str | None = None,
    producer: str = "test",
) -> str:
    frontmatter = [
        "---",
        f"next_agent: {next_agent}",
        f"reason: {reason}",
    ]
    if scope_sha is not None:
        frontmatter.append(f"scope_sha: {scope_sha}")
    if close_type is not None:
        frontmatter.append(f"close_type: {close_type}")
    if prior_sha is not None:
        frontmatter.append(f"prior_sha: {prior_sha}")
    frontmatter.extend([f"producer: {producer}", "---", ""])
    return "\n".join(frontmatter) + body.lstrip()


@pytest.mark.parametrize(
    ("fixture_name", "expected_verdict", "warning_count", "error_count"),
    [
        ("valid_yes.txt", "YES", 0, 0),
        ("valid_no.txt", "NO", 1, 2),
        ("valid_warnings_only.txt", "WARNINGS-ONLY", 1, 0),
    ],
)
def test_parse_validation_output_handles_all_structured_verdict_shapes(
    fixture_name: str,
    expected_verdict: str,
    warning_count: int,
    error_count: int,
) -> None:
    result = parse_validation_output(_fixture_text(fixture_name))

    assert result.verdict == expected_verdict
    assert len(result.warnings) == warning_count
    assert len(result.errors) == error_count


def test_parse_validation_output_requires_valid_verdict_line() -> None:
    with pytest.raises(ValueError, match="Missing VALID verdict line"):
        parse_validation_output("ROUTING: PASS - backend")


def test_validate_handoff_rejects_unchanged_content_hash_for_backend(
    tmp_path: Path,
) -> None:
    handoff_content = _with_frontmatter(
        """# backend Handback

**Agent:** backend
**Epic/Story:** Dispatch Loop Python Rewrite / Story 3
**Status:** Complete and verified
**Latest verified repo SHA:** `877f54d07d06d033b6b3f6dded924d170e9e2116`

## Completed Work

- Added `llm_handoff/validator.py`.

## Verification

- `venv\\Scripts\\python.exe -m pytest -o addopts= llm_handoff\\tests\\test_validator.py`

## Next Step

- **auditor:** Audit Story 3 against the validator acceptance criteria.
""",
        next_agent="auditor",
        reason="Story 3 complete; audit requested.",
        scope_sha="877f54d",
        close_type="story",
        producer="backend",
    )
    handoff_path = _write_handoff(tmp_path, handoff_content)
    prior_handoff_sha = sha256(handoff_content.encode("utf-8")).hexdigest()

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha=prior_handoff_sha,
    )

    assert result.verdict == "NO"
    assert result.routing_instruction == "auditor"
    assert any("no_new_sha" in error for error in result.errors)


def test_validate_handoff_accepts_backend_handback_with_sha_and_routing(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# backend Handback

**Agent:** backend
**Epic/Story:** Dispatch Loop Python Rewrite / Story 3
**Status:** Complete and verified
**Latest verified repo SHA:** `877f54d07d06d033b6b3f6dded924d170e9e2116`

## Completed Work

- Added `llm_handoff/validator.py`.
- Added `llm_handoff/tests/test_validator.py`.

## Verification

- `venv\\Scripts\\python.exe -m pytest -o addopts= llm_handoff\\tests\\test_validator.py`
- `venv\\Scripts\\python.exe -m pytest -o addopts= llm_handoff\\tests`

## Next Step

- **auditor:** Audit Story 3 against the validator acceptance criteria and route Story 4 if approved.
""",
            next_agent="auditor",
            reason="Story 3 complete; audit requested.",
            scope_sha="877f54d",
            close_type="story",
            producer="backend",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="0" * 64,
    )

    assert result.verdict == "YES"
    assert result.warnings == []
    assert result.errors == []
    assert result.routing_instruction == "auditor"


def test_validate_handoff_accepts_utf16_le_bom_manual_frontend_handback(
    tmp_path: Path,
) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(
        _with_frontmatter(
            """# E1-S6 Frontend Handback

**Agent:** frontend (manual frontend)
**Latest verified repo SHA:** `236f82f812e405c595dd3fb194c98d8f1d11d89e`

## Completed Work

- Added the frontend stale-state signaling UI.

## Verification

- `npm test`
- `npm run build`
""",
            next_agent="auditor",
            reason="E1-S6 frontend implementation complete; audit requested.",
            scope_sha="236f82f",
            close_type="story",
            producer="frontend",
        ),
        encoding="utf-16",
    )

    result = validate_handoff(
        handoff_path,
        "manual frontend (GUI)",
        prior_handoff_sha="0" * 64,
    )

    assert result.verdict == "YES"
    assert result.routing_instruction == "auditor"
    assert result.errors == []


def test_validate_handoff_accepts_manual_frontend_results_and_execution_sha_sections(
    tmp_path: Path,
) -> None:
    impl_sha = "dd37847842b1fc2b27e7fd433be1206508f72327"
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            f"""## Implementer Handback

**Agent:** frontend (manual frontend)
**Epic/Story:** E6-S2
**Phase:** UAT Remediation

### Results
- Standardized tier headers to sentence case.
- Test Suites (`CardList.test.tsx`) confirmed successfully with exit 0.

### Execution SHA
Revert cleanly compiled as implementation SHA: `{impl_sha}`.
""",
            next_agent="auditor",
            reason="E6-S2 frontend implementation complete; audit requested.",
            scope_sha=impl_sha,
            close_type="story",
            producer="frontend",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "manual frontend (GUI)",
        prior_handoff_sha="0" * 64,
    )

    assert result.verdict == "YES"
    assert result.errors == []
    assert result.routing_instruction == "auditor"
    assert all(
        "acceptance_coverage_unclear" not in warning for warning in result.warnings
    )


def test_validate_handoff_returns_warnings_only_for_planner_without_sha(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """Mode: Non-Implementing Principal Engineer (review/orchestration only)

## Task Assignment

**Agent:** backend
**Epic/Story:** Dispatch Loop Python Rewrite / Story 3
**Phase:** Implementation

### Objective

Implement `llm_handoff/validator.py` as a pure-function module.

### Acceptance Criteria

- Parse the three structured verdict shapes.
- Enforce SHA, routing, and scope-claim checks.
""",
            next_agent="backend",
            reason="Implement validator story.",
            producer="planner",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "planner",
        prior_handoff_sha="1" * 64,
    )

    assert result.verdict == "WARNINGS-ONLY"
    assert result.errors == []
    assert result.routing_instruction == "backend"
    assert any("sha_missing" in warning for warning in result.warnings)


def test_validate_handoff_warns_only_for_long_frontmatter_reason(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# Auditor Report

**Agent:** auditor (auditor)
**Latest verified repo SHA:** `02b475f`

## Audit Verdict

APPROVED-WITH-NITS. Route the next implementation story to backend.
""",
            next_agent="backend",
            reason="x" * 200,
            scope_sha="02b475f",
            close_type="story",
            producer="auditor",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "auditor (audit)",
        prior_handoff_sha="1" * 64,
    )

    assert result.verdict == "WARNINGS-ONLY"
    assert result.routing_instruction == "backend"
    assert result.errors == []
    assert any("frontmatter_reason_too_long" in warning for warning in result.warnings)
    assert all(
        "routing_instruction_missing" not in warning for warning in result.warnings
    )


def test_validate_handoff_rejects_scope_claim_mismatch_for_backend(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """Mode: Non-Implementing Principal Engineer (review/orchestration only)

## Task Assignment

**Agent:** backend
**Epic/Story:** Dispatch Loop Python Rewrite / Story 3
**Phase:** Implementation

### Objective

Implement `llm_handoff/validator.py`.

### Acceptance Criteria

- Add the validator tests first.
""",
            next_agent="auditor",
            reason="Audit requested after malformed ownership.",
            scope_sha="877f54d",
            close_type="story",
            producer="backend",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="2" * 64,
    )

    assert result.verdict == "NO"
    assert any("scope_claim_mismatch" in error for error in result.errors)


def test_validate_handoff_rejects_missing_frontmatter_next_agent_with_producer(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        """---
reason: Missing next_agent should fail.
producer: backend
---

# backend Handback

**Agent:** backend
**Epic/Story:** Dispatch Loop Python Rewrite / Story 3
**Status:** Complete and verified
**Latest verified repo SHA:** `877f54d07d06d033b6b3f6dded924d170e9e2116`

## Completed Work

- Added `llm_handoff/validator.py`.

## Verification

- `venv\\Scripts\\python.exe -m pytest -o addopts= llm_handoff\\tests\\test_validator.py`
""",
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="3" * 64,
    )

    assert result.verdict == "NO"
    assert result.routing_instruction is None
    assert any("frontmatter_next_agent_missing" in error for error in result.errors)
    assert any("producer backend" in error for error in result.errors)


def test_validate_handoff_frontmatter_accepts_frontend_alias() -> None:
    result = validator.validate_handoff_frontmatter(
        HandoffRouting(
            next_agent="frontend",
            reason="Assign frontend implementation work.",
            producer="planner",
        )
    )

    assert result.verdict == "YES"
    assert result.routing_instruction == "frontend"
    assert result.errors == []
    assert result.warnings == []


def test_validate_handoff_frontmatter_accepts_backend_alias() -> None:
    result = validator.validate_handoff_frontmatter(
        HandoffRouting(
            next_agent="backend",
            reason="Hand off backend implementation to backend.",
            producer="frontend",
        )
    )

    assert result.verdict == "YES"
    assert result.routing_instruction == "backend"
    assert result.errors == []
    assert result.warnings == []


def test_validate_handoff_frontmatter_accepts_planner_alias() -> None:
    result = validator.validate_handoff_frontmatter(
        HandoffRouting(
            next_agent="planner",
            reason="Return to planner scoping.",
            producer="auditor",
        )
    )

    assert result.verdict == "YES"
    assert result.routing_instruction == "planner"
    assert result.errors == []
    assert result.warnings == []


def test_validate_handoff_frontmatter_rejects_missing_producer() -> None:
    result = validator.validate_handoff_frontmatter(
        HandoffRouting(
            next_agent="planner",
            reason="Return to planner scoping.",
        )
    )

    assert result.verdict == "NO"
    assert any("frontmatter_producer_missing" in error for error in result.errors)


def test_validate_handoff_rejects_epic_close_type_without_audit_or_ledger(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# backend Handback

**Agent:** backend
**Latest verified repo SHA:** `82ce839`

## Completed Work

- Completed the epic implementation.
""",
            next_agent="backend",
            reason="Epic complete but routed to implementer.",
            scope_sha="82ce839",
            close_type="epic",
            producer="backend",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="c" * 64,
    )

    assert result.verdict == "NO"
    assert any("frontmatter_routing_rule" in error for error in result.errors)


def test_validate_handoff_rejects_finalizer_without_epic_close_type(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# Auditor Report

**Agent:** auditor (auditor)
**Latest verified repo SHA:** `82ce839`

## Audit Verdict

APPROVED.
""",
            next_agent="finalizer",
            reason="Ledger requested without close_type.",
            producer="auditor",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "auditor (audit)",
        prior_handoff_sha="d" * 64,
    )

    assert result.verdict == "NO"
    assert any("frontmatter_routing_rule" in error for error in result.errors)


def test_validate_handoff_rejects_finalizer_from_non_auditor_producer(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# backend Handback

**Agent:** backend
**Latest verified repo SHA:** `82ce839`

## Completed Work

- Completed the epic implementation.
""",
            next_agent="finalizer",
            reason="Attempt to bypass the audit gate.",
            scope_sha="82ce839",
            close_type="epic",
            producer="backend",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="e" * 64,
    )

    assert result.verdict == "NO"
    assert any("frontmatter_routing_rule" in error for error in result.errors)


def test_validate_handoff_rejects_malformed_frontmatter_yaml(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        """---
next_agent: [codex
reason: malformed YAML should fail validation
---

## Task Assignment

**Agent:** backend
""",
    )

    result = validate_handoff(
        handoff_path,
        "planner",
        prior_handoff_sha="e" * 64,
    )

    assert result.verdict == "NO"
    assert result.routing_instruction is None
    assert any("frontmatter_parse_error" in error for error in result.errors)


def test_validate_handoff_rejects_missing_frontmatter_but_reports_legacy_route(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        """Mode: Non-Implementing Principal Engineer (review/orchestration only)

## Task Assignment

**Agent:** backend

### Objective

Implement the next story.

### Acceptance Criteria

- Keep legacy routing visible during rollout.
""",
    )

    result = validate_handoff(
        handoff_path,
        "planner",
        prior_handoff_sha="f" * 64,
    )

    assert result.verdict == "NO"
    assert result.routing_instruction == "backend"
    assert any("frontmatter_missing" in error for error in result.errors)


def test_validate_handoff_accepts_auditor_epic_close_metadata(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """## Audit

**Agent:** auditor (auditor)
**Epic/Story:** Dispatch Route-Missing Handoff Recovery
**Test SHA:** `3251966`
**Implementation SHA:** `833da7d`
**Handoff SHA:** `76ab974`
**Verdict:** **APPROVED**
**Close Type:** EPIC-CLOSE

### Audit Gate
All required checks passed.

### Suggested Next Step
auditor: update `PROJECT_STATE.md`, update `PROJECT_STATE.md`, commit, and push.

Canonical Routing Instruction:
Next: auditor
""",
            next_agent="finalizer",
            reason="Epic audit approved; ledger update requested.",
            scope_sha="833da7d",
            close_type="epic",
            producer="auditor",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "auditor (audit)",
        prior_handoff_sha="9" * 64,
    )

    assert result.verdict == "YES"
    assert result.routing_instruction == "finalizer"
    assert result.errors == []


def test_validate_handoff_accepts_audit_report_with_downstream_task_assignment(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# E2-S2 Frontend Polling Loop — AUDIT REPORT

## Verdict: APPROVED-WITH-NITS

## Close Type: STORY-CLOSE
Story close in multi-story epic. No ledger entry drafted.

## Audit Gate
- **tsc:** PASS
- **vitest:** PASS
- **build:** PASS
- **pytest:** PASS

## Diff Review
Completed work and verification are documented.

## Task Assignment

**Agent:** planner
**Epic/Story:** UAT-REMEDIATION-E2-S3

### Objective
Dispatch the next story to backend.

### Acceptance Criteria
- planner writes a backend task assignment.
""",
            next_agent="planner",
            reason="Audit approved; planner should dispatch the next story.",
            scope_sha="9aac032",
            close_type="story",
            producer="auditor",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "auditor (audit)",
        prior_handoff_sha="6" * 64,
    )

    assert result.verdict == "YES"
    assert result.routing_instruction == "planner"
    assert result.errors == []


def test_validate_handoff_warns_for_unknown_previous_agent(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """Mode: Non-Implementing Principal Engineer (review/orchestration only)

## Task Assignment

**Agent:** backend
**Epic/Story:** Dispatch Loop Python Rewrite / Story 7

### Objective

Retire the PowerShell dispatcher.

### Acceptance Criteria

- Delete both PowerShell scripts.

## Next Step

- **auditor:** Audit Story 7 and verify the cutover.
""",
            next_agent="auditor",
            reason="Audit Story 7.",
            scope_sha="877f54d",
            close_type="story",
            producer="planner",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "Mystery Agent",
        prior_handoff_sha="4" * 64,
    )

    assert result.verdict == "WARNINGS-ONLY"
    assert result.errors == []
    assert result.routing_instruction == "auditor"
    assert any("unknown_previous_agent" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("agent_text", "expected_route"),
    [
        ("auditor for misroute clarification", "validator"),
        ("backend", "backend"),
        ("planner", "planner"),
        ("frontend", "frontend"),
        ("manual frontend GUI", "frontend"),
    ],
)
def test_normalize_agent_handles_misroute_and_frontend_aliases(
    agent_text: str,
    expected_route: str,
) -> None:
    assert validator._normalize_agent(agent_text) == expected_route


def test_validate_handoff_rejects_human_readable_frontmatter_next_agent_without_pre_normalization(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        """---
next_agent: auditor (Auditor)
reason: Frontend implementation complete; audit requested.
scope_sha: 25c45ca
close_type: story
producer: frontend (manual frontend)
---

## Implementer Handback

**Agent:** frontend (manual frontend)
**Epic/Story:** E5-S3
**Latest verified repo SHA:** `25c45ca`

## Completed Work

- Built the Round History panel.

## Verification

- `npx tsc --noEmit`
- `npx vitest run`
""",
    )

    result = validate_handoff(
        handoff_path,
        "frontend",
        prior_handoff_sha="6" * 64,
    )

    assert result.verdict == "NO"
    assert result.routing_instruction is None
    assert any("frontmatter_next_agent_invalid" in error for error in result.errors)


def test_validate_handoff_rejects_planner_output_without_task_assignment_block(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """Mode: Non-Implementing Principal Engineer (review/orchestration only)

## Next Step

- **backend:** Implement Story 7.
""",
            next_agent="backend",
            reason="Implement Story 7.",
            producer="planner",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "planner",
        prior_handoff_sha="5" * 64,
    )

    assert result.verdict == "NO"
    assert any("scope_claim_mismatch" in error for error in result.errors)


def test_validate_handoff_accepts_frontmatter_producer_for_completed_work(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# Story 7 Completion Report

**Epic/Story:** Dispatch Loop Python Rewrite / Story 7
**Latest verified repo SHA:** `877f54d`

## Completed Work

- Deleted the PowerShell dispatch scripts.

## Verification

- `venv\\Scripts\\python.exe -m pytest llm_handoff\\tests`

## Next Step

- **auditor:** Audit Story 7.
""",
            next_agent="auditor",
            reason="Story 7 complete; audit requested.",
            scope_sha="877f54d",
            close_type="story",
            producer="backend",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="6" * 64,
    )

    assert result.verdict == "YES"
    assert result.routing_instruction == "auditor"
    assert result.errors == []


def test_validate_handoff_rejects_missing_agent_line_when_producer_mismatches(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# Story 7 Completion Report

**Epic/Story:** Dispatch Loop Python Rewrite / Story 7
**Latest verified repo SHA:** `877f54d`

## Completed Work

- Deleted the PowerShell dispatch scripts.

## Verification

- `venv\\Scripts\\python.exe -m pytest llm_handoff\\tests`

## Next Step

- **auditor:** Audit Story 7.
""",
            next_agent="auditor",
            reason="Story 7 complete; audit requested.",
            scope_sha="877f54d",
            close_type="story",
            producer="planner",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="6" * 64,
    )

    assert result.verdict == "NO"
    assert any("scope_claim_missing" in error for error in result.errors)


def test_validate_handoff_rejects_mismatched_agent_ownership_line(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# Story 7 Completion Report

**Agent:** Gemini
**Epic/Story:** Dispatch Loop Python Rewrite / Story 7
**Latest verified repo SHA:** `877f54d`

## Completed Work

- Deleted the PowerShell dispatch scripts.

## Verification

- `venv\\Scripts\\python.exe -m pytest llm_handoff\\tests`

## Next Step

- **auditor:** Audit Story 7.
""",
            next_agent="auditor",
            reason="Story 7 complete; audit requested.",
            scope_sha="877f54d",
            close_type="story",
            producer="backend",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="7" * 64,
    )

    assert result.verdict == "NO"
    assert any("scope_claim_mismatch" in error for error in result.errors)


def test_validate_handoff_warns_when_planner_sections_are_missing(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """Mode: Non-Implementing Principal Engineer (review/orchestration only)

## Task Assignment

**Agent:** backend
**Epic/Story:** Dispatch Loop Python Rewrite / Story 7
""",
            next_agent="backend",
            reason="Implement Story 7.",
            producer="planner",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "planner",
        prior_handoff_sha="8" * 64,
    )

    assert result.verdict == "WARNINGS-ONLY"
    assert any("Objective section" in warning for warning in result.warnings)
    assert any("Acceptance Criteria section" in warning for warning in result.warnings)


def test_validate_handoff_accepts_user_escalation_frontmatter(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# backend Handback

**Agent:** backend
**Latest verified repo SHA:** `877f54d`

## Completed Work

- Identified a blocking ambiguity.

## user

The routing remains ambiguous after review. Human decision required.
""",
            next_agent="user",
            reason="Ambiguous routing requires PO decision.",
            scope_sha="877f54d",
            producer="backend",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "backend",
        prior_handoff_sha="8" * 64,
    )

    assert result.verdict == "YES"
    assert result.routing_instruction == "user"
    assert all("scope_claim_mismatch" not in error for error in result.errors)
    assert all("Objective section" not in warning for warning in result.warnings)
    assert all(
        "Acceptance Criteria section" not in warning for warning in result.warnings
    )


def test_validate_handoff_preserves_escalation_route_with_invalid_prior_sha(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        """---
next_agent: user
reason: "PO decision required before continuing."
epic_id: UAT-REMEDIATION-E3-E6-S2.5
story_id: E6-S2
story_title: Tier header case
remaining_stories:
  - E6-S3 Reject Finding undo toast
prior_sha: 2eee66b29811e6ee4ffee970039c35474c678a39bce20d1905d1e935210864e5
producer: planner
---

Mode: Non-Implementing Principal Engineer (review/orchestration only)

## user

Human decision required.
""",
    )

    result = validate_handoff(
        handoff_path,
        "planner",
        prior_handoff_sha="8" * 64,
    )

    assert result.verdict == "NO"
    assert result.routing_instruction == "user"
    assert any("frontmatter_prior_sha_invalid" in error for error in result.errors)
    assert all("routing_instruction_missing" not in error for error in result.errors)
    assert all("scope_claim_mismatch" not in error for error in result.errors)
    assert all(
        "acceptance_coverage_unclear" not in warning for warning in result.warnings
    )


def test_validate_handoff_rejects_planner_self_loop_back_to_legacy_alias(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """Mode: Non-Implementing Principal Engineer (review/orchestration only)

## Task Assignment

**Agent:** planner
**Epic/Story:** Scope the next epic
""",
            next_agent="planner",
            reason="Planner self-loop fixture.",
            producer="planner",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "planner",
        prior_handoff_sha="9" * 64,
    )

    assert result.verdict == "NO"
    assert result.routing_instruction == "planner"
    assert any("planner_self_loop" in error for error in result.errors)


def test_validate_handoff_rejects_auditor_self_loop_back_to_auditor(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# Auditor Handback

**Agent:** auditor (auditor)
**Latest verified repo SHA:** `59206fb3f3ac027ef3ba07f4d7c8db0410edc926`

## Audit Summary

Audit complete.

## Next Step

- **auditor:** Audit the next item.
""",
            next_agent="auditor",
            reason="Auditor self-loop fixture.",
            scope_sha="59206fb",
            producer="auditor",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "auditor (audit)",
        prior_handoff_sha="a" * 64,
    )

    assert result.verdict == "NO"
    assert result.routing_instruction == "auditor"
    assert any("agent_self_loop" in error for error in result.errors)


def test_validate_handoff_allows_auditor_handoff_to_epic_close_for_ledger_close_push(
    tmp_path: Path,
) -> None:
    handoff_path = _write_handoff(
        tmp_path,
        _with_frontmatter(
            """# Dispatch Stream-JSON + Default backend Resume - AUDIT APPROVED-WITH-NITS

**Agent:** auditor
**Verified repo SHAs:** impl `82ce839`, tests `3407c66`

## Audit Verdict: APPROVED-WITH-NITS

## Next Step

- **auditor (ledger close + push):**
  1. Append the ledger entry to `PROJECT_STATE.md`.
  2. Push `main` to `origin`.

- **planner (AFTER ledger close + push):** Process the UAT remediation epic.
""",
            next_agent="finalizer",
            reason="Epic audit approved; ledger close requested.",
            scope_sha="82ce839",
            close_type="epic",
            producer="auditor",
        ),
    )

    result = validate_handoff(
        handoff_path,
        "auditor (audit)",
        prior_handoff_sha="b" * 64,
    )

    assert result.routing_instruction == "finalizer"
    assert all("agent_self_loop" not in error for error in result.errors)


def test_author_role_coalesces_support_variants() -> None:
    assert validator._author_role("auditor") == "auditor-family"
    assert validator._author_role("validator") == "auditor-family"
