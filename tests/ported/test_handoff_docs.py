from __future__ import annotations

from pathlib import Path

import pytest


HANDOFF_DIR = Path(__file__).resolve().parents[2] / "docs" / "handoff"
REPO_ROOT = Path(__file__).resolve().parents[2]
INSTRUCTION_DOCS = [
    "AUDITOR_HANDOFF_PROMPT.md",
    "BACKEND_HANDOFF_PROMPT.md",
    "FINALIZER_HANDOFF_PROMPT.md",
    "FRONTEND_HANDOFF_PROMPT.md",
    "HANDBOOK.md",
    "PLANNER_HANDOFF_PROMPT.md",
    "PLANNER_INITIAL_PROMPT.md",
    "README.md",
    "SHARED_REPO_INIT_PROMPT.md",
]
AGENT_INSTRUCTION_DOCS = [
    ".codex/skills/llm-handoff/SKILL.md",
    ".gemini/agents/frontend.md",
    ".gemini/agents/planner.md",
]
PLANNER_INSTRUCTION_DOCS = [
    ".gemini/agents/planner.md",
    "docs/handoff/PLANNER_INITIAL_PROMPT.md",
    "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
]
FRONTEND_INSTRUCTION_DOCS = [
    ".gemini/agents/frontend.md",
    "docs/handoff/FRONTEND_HANDOFF_PROMPT.md",
]
ROLE_PROMPTS = [
    "AUDITOR_HANDOFF_PROMPT.md",
    "BACKEND_HANDOFF_PROMPT.md",
    "FINALIZER_HANDOFF_PROMPT.md",
    "FRONTEND_HANDOFF_PROMPT.md",
    "PLANNER_HANDOFF_PROMPT.md",
    "PLANNER_INITIAL_PROMPT.md",
]
CANONICAL_STATUSES = {
    "ready_for_review",
    "verified_pass",
    "verified_fail",
    "blocked_missing_context",
    "blocked_implementation_failure",
    "escalate_to_user",
}


@pytest.mark.parametrize("doc_name", INSTRUCTION_DOCS)
def test_handoff_instruction_docs_document_yaml_frontmatter(doc_name: str) -> None:
    content = (HANDOFF_DIR / doc_name).read_text(encoding="utf-8")
    normalized = content.lower()

    assert "yaml" in normalized
    assert "frontmatter" in normalized
    assert "next_agent" in content
    assert "producer" in content
    assert "epic_id" in content
    assert "story_id" in content
    assert "story_title" in content
    assert "remaining_stories" in content


@pytest.mark.parametrize("doc_path", AGENT_INSTRUCTION_DOCS)
def test_agent_instruction_docs_document_scope_metadata(doc_path: str) -> None:
    content = (REPO_ROOT / doc_path).read_text(encoding="utf-8")

    assert "next_agent" in content
    assert "producer" in content
    assert "epic_id" in content
    assert "story_id" in content
    assert "story_title" in content
    assert "remaining_stories" in content


@pytest.mark.parametrize("doc_path", PLANNER_INSTRUCTION_DOCS)
def test_planner_instruction_docs_forbid_push_and_quote_reason(doc_path: str) -> None:
    content = (REPO_ROOT / doc_path).read_text(encoding="utf-8")
    normalized = content.lower()

    assert "git push" in normalized
    assert (
        "not authorized to push" in normalized or "never run `git push`" in normalized
    )
    assert "quote every `reason`" in normalized


@pytest.mark.parametrize("doc_path", FRONTEND_INSTRUCTION_DOCS)
def test_frontend_instruction_docs_require_concrete_scope_sha(
    doc_path: str,
) -> None:
    content = (REPO_ROOT / doc_path).read_text(encoding="utf-8")
    normalized = content.lower()

    assert "git rev-parse head" in normalized
    assert "scope_sha: head" in normalized
    assert "7-40" in normalized
    assert "hex" in normalized
    assert "producer: frontend" in normalized


def test_handoff_readme_documents_verification_evidence() -> None:
    content = (HANDOFF_DIR / "README.md").read_text(encoding="utf-8")

    assert "## Verification Evidence" in content
    assert "Commands run" in content
    assert "Output summary" in content
    assert "Commit SHA verified" in content
    assert "Files changed or reviewed" in content
    assert "Unresolved concerns" in content
    assert "never `HEAD`" in content


@pytest.mark.parametrize(
    "doc_name",
    [
        "AUDITOR_HANDOFF_PROMPT.md",
        "BACKEND_HANDOFF_PROMPT.md",
        "FINALIZER_HANDOFF_PROMPT.md",
        "FRONTEND_HANDOFF_PROMPT.md",
    ],
)
def test_prompts_contain_evidence_requirement(doc_name: str) -> None:
    content = (HANDOFF_DIR / doc_name).read_text(encoding="utf-8")

    assert "Verification Evidence" in content
    assert "stale" in content.lower() or doc_name == "AUDITOR_HANDOFF_PROMPT.md"


@pytest.mark.parametrize("doc_name", ROLE_PROMPTS)
def test_role_prompts_reference_handbook(doc_name: str) -> None:
    content = (HANDOFF_DIR / doc_name).read_text(encoding="utf-8")

    assert "Start by reading `docs/handoff/HANDBOOK.md`" in "\n".join(
        content.splitlines()[:8]
    )


def test_auditor_prompt_two_phase_structure() -> None:
    content = (HANDOFF_DIR / "AUDITOR_HANDOFF_PROMPT.md").read_text(encoding="utf-8")

    assert "Phase 1 - spec compliance" in content
    assert "Phase 2 - code quality" in content
    assert "If phase 1 fails, stop there" in content
    for phrase in (
        "missing scope",
        "scope creep",
        "unrequested extra",
        "wrong files touched",
    ):
        assert phrase in content


def test_auditor_agent_matches_phase_ordering() -> None:
    content = (REPO_ROOT / ".claude" / "agents" / "auditor.md").read_text(
        encoding="utf-8"
    )

    assert "phase 1 spec compliance" in content
    assert "phase 2 code quality" in content
    assert "halt and emit `status: verified_fail`" in content


@pytest.mark.parametrize(
    "doc_name",
    ["PLANNER_HANDOFF_PROMPT.md", "PLANNER_INITIAL_PROMPT.md"],
)
def test_planner_prompt_work_packet_fields(doc_name: str) -> None:
    content = (HANDOFF_DIR / doc_name).read_text(encoding="utf-8")
    for field in (
        "Objective",
        "Files in scope",
        "Files out of bounds",
        "Context",
        "Verification command",
        "Expected next route",
    ):
        assert f"**{field}:**" in content


@pytest.mark.parametrize(
    "doc_name",
    ["PLANNER_HANDOFF_PROMPT.md", "PLANNER_INITIAL_PROMPT.md"],
)
def test_planner_prompt_ban_list(doc_name: str) -> None:
    content = (HANDOFF_DIR / doc_name).read_text(encoding="utf-8")
    for phrase in (
        "add validation",
        "handle errors appropriately",
        "write tests",
        "implement later",
        "as needed",
    ):
        assert phrase in content


def test_backend_frontend_pushback_section_consistent() -> None:
    backend = (HANDOFF_DIR / "BACKEND_HANDOFF_PROMPT.md").read_text(encoding="utf-8")
    frontend = (HANDOFF_DIR / "FRONTEND_HANDOFF_PROMPT.md").read_text(encoding="utf-8")

    for phrase in (
        "Handling Audit Feedback",
        "verify it against the actual changed files",
        "focused test/command",
        "blocked_implementation_failure",
    ):
        assert phrase in backend
        assert phrase in frontend


def test_handbook_exists() -> None:
    content = (HANDOFF_DIR / "HANDBOOK.md").read_text(encoding="utf-8")
    for heading in (
        "## How HANDOFF.md Works",
        "## Frontmatter Schema Reference",
        "## Status Enum Reference",
        "## Evidence Block Reference",
        "## When To Flag Uncertainty",
        "## Escalation Protocol",
        "## Common Failure Modes",
    ):
        assert heading in content
    assert len(content.splitlines()) < 500


def test_role_prompts_do_not_duplicate_status_enum_list() -> None:
    enum_lines = [f"- `{status}`" for status in CANONICAL_STATUSES]
    for doc_name in ROLE_PROMPTS:
        content = (HANDOFF_DIR / doc_name).read_text(encoding="utf-8")
        assert sum(line in content for line in enum_lines) < 6


def test_shared_init_prompt_references_handbook_first() -> None:
    content = (HANDOFF_DIR / "SHARED_REPO_INIT_PROMPT.md").read_text(encoding="utf-8")

    assert "1. Read `docs/handoff/HANDBOOK.md`" in content


def test_architecture_documents_subagents_and_circuit_breaker() -> None:
    content = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "## Provider-Native Subagents" in content
    assert "Claude subagents, Codex skills, and Gemini agents" in content
    assert "only the active dispatcher role writes `handoff.md`" in content.lower()
    assert "## Circuit Breakers" in content
    assert "three-fix circuit breaker" in content
    assert "THREE_FIX_CIRCUIT_BREAKER" in content


def test_contributing_documents_review_feedback() -> None:
    content = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "## Handling Review Feedback" in content
    assert "handoff prompts" in content
