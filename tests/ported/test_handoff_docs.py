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
