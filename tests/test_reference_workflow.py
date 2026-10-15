from __future__ import annotations

from pathlib import Path
import re

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
    "docs/handoff/HANDBOOK.md",
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
    ".codex/rules/llm-handoff.rules",
    ".gemini/agents/planner.md",
    ".gemini/agents/frontend.md",
    ".gemini/policies/planner_guardrails.toml",
    ".claude/agents/auditor.md",
    ".claude/agents/handoff-router.md",
    ".claude/agents/handoff-validator.md",
    ".claude/agents/ledger-updater.md",
    ".claude/agents/planner.md",
    ".claude/agents/backend.md",
    ".claude/agents/frontend.md",
    ".claude/settings.json",
    ".claude/hooks/block_dangerous_commands.py",
)

COPIED_TEMPLATE_FILES = (
    "docs/handoff/HANDOFF.md",
    "docs/handoff/HANDBOOK.md",
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
    ".codex/rules/llm-handoff.rules",
    ".gemini/agents/planner.md",
    ".gemini/agents/frontend.md",
    ".gemini/policies/planner_guardrails.toml",
    ".claude/agents/auditor.md",
    ".claude/agents/handoff-router.md",
    ".claude/agents/handoff-validator.md",
    ".claude/agents/ledger-updater.md",
    ".claude/agents/planner.md",
    ".claude/agents/backend.md",
    ".claude/agents/frontend.md",
    ".geminiignore",
)

# The Claude subagent each role launches when it is mapped to `claude`.
CLAUDE_ROLE_AGENT_FILES = {
    "planner": ".claude/agents/planner.md",
    "backend": ".claude/agents/backend.md",
    "frontend": ".claude/agents/frontend.md",
    "auditor": ".claude/agents/auditor.md",
    "validator": ".claude/agents/handoff-validator.md",
    "finalizer": ".claude/agents/ledger-updater.md",
}


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
    assert decision.route == "planner"
    assert decision.confidence == "HIGH"


def test_reference_workflow_planner_prompt_has_work_packet_example() -> None:
    content = (
        REFERENCE_WORKFLOW / "docs" / "handoff" / "PLANNER_HANDOFF_PROMPT.md"
    ).read_text(encoding="utf-8")

    assert "## Worked Example" in content
    assert "Before (vague)" in content
    assert "After (specific)" in content
    assert "src/foo/bar.py" in content


def test_reference_workflow_ships_a_claude_agent_for_every_role() -> None:
    config = load_dispatch_config(
        repo_root=REFERENCE_WORKFLOW,
        config_path=REFERENCE_WORKFLOW / "dispatch_config.yaml",
    )

    assert set(CLAUDE_ROLE_AGENT_FILES) == set(config.agents)
    for role, relative_path in CLAUDE_ROLE_AGENT_FILES.items():
        content = (REFERENCE_WORKFLOW / relative_path).read_text(encoding="utf-8")
        name = Path(relative_path).stem
        assert content.startswith("---\n"), relative_path
        assert f"name: {name}\n" in content, relative_path
        if role in {"planner", "backend", "frontend"}:
            assert f"`{role}` role" in content, relative_path


def test_claude_role_agents_do_not_point_at_dispatcher_docs() -> None:
    for name in ("planner", "backend", "frontend"):
        content = (REFERENCE_WORKFLOW / ".claude" / "agents" / f"{name}.md").read_text(
            encoding="utf-8"
        )
        assert "CONFIGURATION.md" not in content
        assert "docs/ARCHITECTURE.md" not in content


def test_codex_rules_let_only_staging_and_hookless_commits_out_of_the_sandbox() -> None:
    rules = (REFERENCE_WORKFLOW / ".codex" / "rules" / "llm-handoff.rules").read_text(
        encoding="utf-8"
    )
    allowed = re.findall(
        r'pattern = (\[[^\]]*\]),\s*decision = "allow"', rules
    )
    forbidden = re.findall(
        r'pattern = (\[[^\]]*\]),\s*decision = "forbidden"', rules
    )

    assert allowed == ['["git", "add"]', '["git", "commit", "--no-verify"]']
    assert '["git", "push"]' in forbidden
    assert '["git", "commit", "--no-verify", "--amend"]' in forbidden
