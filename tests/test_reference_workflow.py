from __future__ import annotations

from pathlib import Path

from llm_handoff.config import load_dispatch_config
from llm_handoff.router import parse_handoff_frontmatter, route


REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_WORKFLOW = REPO_ROOT / "examples" / "reference-workflow"

REQUIRED_TEMPLATE_FILES = (
    "AGENTS.md",
    "PROJECT_STATE.md",
    "dispatch_config.yaml",
    ".geminiignore",
    "docs/handoff/HANDOFF.md",
    "docs/handoff/README.md",
    "docs/handoff/SHARED_REPO_INIT_PROMPT.md",
    "docs/handoff/PLANNER_INITIAL_PROMPT.md",
    "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
    "docs/handoff/BACKEND_HANDOFF_PROMPT.md",
    "docs/handoff/FRONTEND_HANDOFF_PROMPT.md",
    "docs/handoff/AUDITOR_HANDOFF_PROMPT.md",
    "docs/handoff/FINALIZER_HANDOFF_PROMPT.md",
    ".codex/skills/llm-handoff/SKILL.md",
    ".codex/skills/llm-handoff/agents/openai.yaml",
    ".gemini/agents/planner.md",
    ".gemini/agents/frontend.md",
    ".gemini/policies/planner_guardrails.toml",
    ".claude/agents/auditor.md",
    ".claude/agents/handoff-router.md",
    ".claude/agents/handoff-validator.md",
    ".claude/agents/ledger-updater.md",
)

COPIED_TEMPLATE_FILES = (
    "docs/handoff/HANDOFF.md",
    "docs/handoff/README.md",
    "docs/handoff/SHARED_REPO_INIT_PROMPT.md",
    "docs/handoff/PLANNER_INITIAL_PROMPT.md",
    "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
    "docs/handoff/BACKEND_HANDOFF_PROMPT.md",
    "docs/handoff/FRONTEND_HANDOFF_PROMPT.md",
    "docs/handoff/AUDITOR_HANDOFF_PROMPT.md",
    "docs/handoff/FINALIZER_HANDOFF_PROMPT.md",
    ".codex/skills/llm-handoff/SKILL.md",
    ".codex/skills/llm-handoff/agents/openai.yaml",
    ".gemini/agents/planner.md",
    ".gemini/agents/frontend.md",
    ".gemini/policies/planner_guardrails.toml",
    ".claude/agents/auditor.md",
    ".claude/agents/handoff-router.md",
    ".claude/agents/handoff-validator.md",
    ".claude/agents/ledger-updater.md",
    ".geminiignore",
)


def test_reference_workflow_contains_required_template_files() -> None:
    missing = [
        relative_path
        for relative_path in REQUIRED_TEMPLATE_FILES
        if not (REFERENCE_WORKFLOW / relative_path).is_file()
    ]

    assert missing == []


def test_reference_workflow_copied_templates_match_source() -> None:
    mismatched = [
        relative_path
        for relative_path in COPIED_TEMPLATE_FILES
        if (REFERENCE_WORKFLOW / relative_path).read_text(encoding="utf-8")
        != (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    ]

    assert mismatched == []


def test_reference_workflow_config_loads() -> None:
    config = load_dispatch_config(
        repo_root=REFERENCE_WORKFLOW,
        config_path=REFERENCE_WORKFLOW / "dispatch_config.yaml",
    )

    assert config.handoff_path == Path("docs/handoff/HANDOFF.md")
    assert config.project_state_path == Path("PROJECT_STATE.md")
    assert config.auto_push is False
    assert config.agents["planner"].provider == "gemini"
    assert config.agents["backend"].provider == "codex"
    assert config.agents["auditor"].provider == "claude"


def test_reference_workflow_starter_handoff_routes_to_planner() -> None:
    handoff_path = REFERENCE_WORKFLOW / "docs" / "handoff" / "HANDOFF.md"

    frontmatter = parse_handoff_frontmatter(handoff_path)
    decision = route(handoff_path.read_text(encoding="utf-8"))

    assert frontmatter is not None
    assert frontmatter.next_agent == "planner"
    assert frontmatter.producer == "user"
    assert decision.route == "Gemini-PE"
    assert decision.confidence == "HIGH"
