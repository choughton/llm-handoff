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
    "TPM_HANDOFF_PROMPT.md",
]
AGENT_INSTRUCTION_DOCS = [
    ".claude/agents/backend.md",
    ".claude/agents/frontend.md",
    ".claude/agents/planner.md",
    ".codex/skills/llm-handoff/SKILL.md",
    ".codex/skills/llm-handoff-auditor/SKILL.md",
    ".codex/skills/llm-handoff-finalizer/SKILL.md",
    ".codex/skills/llm-handoff-frontend/SKILL.md",
    ".codex/skills/llm-handoff-planner/SKILL.md",
    ".codex/skills/llm-handoff-tpm/SKILL.md",
    ".codex/skills/llm-handoff-validator/SKILL.md",
]
PLANNER_INSTRUCTION_DOCS = [
    ".claude/agents/planner.md",
    ".codex/skills/llm-handoff-planner/SKILL.md",
    ".codex/skills/llm-handoff-tpm/SKILL.md",
    "docs/handoff/PLANNER_INITIAL_PROMPT.md",
    "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
    "docs/handoff/TPM_HANDOFF_PROMPT.md",
]
FRONTEND_INSTRUCTION_DOCS = [
    ".claude/agents/frontend.md",
    ".codex/skills/llm-handoff-frontend/SKILL.md",
    "docs/handoff/FRONTEND_HANDOFF_PROMPT.md",
]
ROLE_PROMPTS = [
    "AUDITOR_HANDOFF_PROMPT.md",
    "BACKEND_HANDOFF_PROMPT.md",
    "FINALIZER_HANDOFF_PROMPT.md",
    "FRONTEND_HANDOFF_PROMPT.md",
    "PLANNER_HANDOFF_PROMPT.md",
    "PLANNER_INITIAL_PROMPT.md",
    "TPM_HANDOFF_PROMPT.md",
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
    [
        "PLANNER_HANDOFF_PROMPT.md",
        "PLANNER_INITIAL_PROMPT.md",
        "TPM_HANDOFF_PROMPT.md",
    ],
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
    [
        "PLANNER_HANDOFF_PROMPT.md",
        "PLANNER_INITIAL_PROMPT.md",
        "TPM_HANDOFF_PROMPT.md",
    ],
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


def test_tpm_prompt_offers_no_finalizer_or_validator_route() -> None:
    content = (HANDOFF_DIR / "TPM_HANDOFF_PROMPT.md").read_text(encoding="utf-8")
    enum_lines = [
        line for line in content.splitlines() if line.startswith("next_agent: <enum>")
    ]

    assert len(enum_lines) == 1
    assert "finalizer" not in enum_lines[0]
    assert "validator" not in enum_lines[0]
    assert "producer: tpm" in content


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
    assert "Claude subagents, Codex skills, and Antigravity agents" in content
    assert "only the active dispatcher role writes `handoff.md`" in content.lower()
    assert "## Circuit Breakers" in content
    assert "three-fix circuit breaker" in content
    assert "THREE_FIX_CIRCUIT_BREAKER" in content


# ACTIVE-7: after an implementer's environment-refused handback, the planner
# never re-sends an unchanged Work Packet. The handbook carries the shared rule
# in its Escalation Protocol; each planner file carries the decision in a
# Handback Review section. Root files and their reference-workflow copies.
REFUSAL_REVIEW_PLANNER_FILES = (
    "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
    ".codex/skills/llm-handoff-planner/SKILL.md",
    ".agents/agents/planner.md",
)
REFUSAL_REVIEW_ROOTS = (REPO_ROOT, REPO_ROOT / "examples" / "reference-workflow")
REFUSAL_REVIEW_FILES = [
    pytest.param(root / relative_path, id=f"{root.name}:{relative_path}")
    for root in REFUSAL_REVIEW_ROOTS
    for relative_path in ("docs/handoff/HANDBOOK.md", *REFUSAL_REVIEW_PLANNER_FILES)
]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(content: str, heading: str) -> str:
    start = content.index(f"\n{heading}\n")
    end = content.find("\n## ", start + len(heading) + 2)
    return content[start : end if end != -1 else len(content)]


def _refusal_review_text(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    if path.name == "HANDBOOK.md":
        return _flat(_section(content, "## Escalation Protocol"))
    return _flat(_section(content, "## Handback Review"))


@pytest.mark.parametrize("path", REFUSAL_REVIEW_FILES)
def test_refusal_review_states_trigger_and_forbids_unchanged_redispatch(
    path: Path,
) -> None:
    text = _refusal_review_text(path)

    assert "`status: blocked_implementation_failure`" in text
    assert "`Environment refusal` bullet" in text
    assert "the same role an unchanged Work Packet" in text
    assert "Never" in text


@pytest.mark.parametrize("path", REFUSAL_REVIEW_FILES)
def test_refusal_review_gate_verified_path(path: Path) -> None:
    text = _refusal_review_text(path)

    assert "Gate-verified re-dispatch." in text
    assert "the refused command is the Work Packet's Verification command" in text
    assert "`enforce_work_packet: true`" in text
    assert "dispatcher commit mode always runs the gate" in text
    assert "Context or Objective" in text
    assert "Gate-Verified Handback" in text
    assert "Changing only the `reason` or the Next Step" in text
    assert "Keep the Verification command exactly as written" in text
    assert "current turn" in text
    assert "all five Verification Evidence fields" in text
    assert "`Commands run` beginning `deferred to the work-claim gate:`" in text
    assert "quoting the refusal" in text
    assert "`Output summary`" in text
    assert "A failing test is not an environment refusal" in text
    assert "PASS evidence for a command that did not pass" in text


@pytest.mark.parametrize("path", REFUSAL_REVIEW_FILES)
def test_refusal_review_user_path_names_a_matching_remedy(path: Path) -> None:
    text = _refusal_review_text(path)

    assert "Route to `user`." in text
    assert "`enforce_work_packet: false`" in text
    assert "cannot address the blocker" in text
    assert "`status: escalate_to_user`" in text
    assert "`interpreter_grants: true`" in text
    assert "`allowed_tools` entry" in text
    # The allowlist remedy is never offered for a missing binary or no TTY.
    assert "`missing binary`" in text
    assert "`no TTY`" in text
    assert "install or put on `PATH`" in text
    assert "interactive step the operator must run" in text
    assert "allowlist setting" in text


def test_refusal_review_planner_sections_point_at_the_handbook_rule() -> None:
    handbook = (HANDOFF_DIR / "HANDBOOK.md").read_text(encoding="utf-8")
    escalation = _section(handbook, "## Escalation Protocol")

    assert "**Environment refusal handbacks.**" in escalation
    assert "`planner_redispatch_after_refusal`" in escalation
    for root in REFUSAL_REVIEW_ROOTS:
        for relative_path in REFUSAL_REVIEW_PLANNER_FILES:
            text = _refusal_review_text(root / relative_path)
            assert (
                "handbook's Escalation Protocol rule for environment refusal "
                "handbacks" in text
            ), relative_path


@pytest.mark.parametrize(
    "relative_path", ("docs/handoff/HANDBOOK.md", *REFUSAL_REVIEW_PLANNER_FILES)
)
def test_refusal_review_root_and_reference_copies_match(relative_path: str) -> None:
    root, reference = REFUSAL_REVIEW_ROOTS

    assert _refusal_review_text(root / relative_path) == _refusal_review_text(
        reference / relative_path
    )


def test_contributing_documents_review_feedback() -> None:
    content = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "## Handling Review Feedback" in content
    assert "handoff prompts" in content
