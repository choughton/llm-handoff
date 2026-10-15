from __future__ import annotations

from pathlib import Path
import re

from llm_handoff.config import load_dispatch_config
from llm_handoff.init_workflow import template_files
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
    ".codex/skills/llm-handoff-planner/SKILL.md",
    ".codex/skills/llm-handoff-frontend/SKILL.md",
    ".codex/skills/llm-handoff-auditor/SKILL.md",
    ".codex/skills/llm-handoff-validator/SKILL.md",
    ".codex/skills/llm-handoff-finalizer/SKILL.md",
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
    ".codex/skills/llm-handoff-planner/SKILL.md",
    ".codex/skills/llm-handoff-frontend/SKILL.md",
    ".codex/skills/llm-handoff-auditor/SKILL.md",
    ".codex/skills/llm-handoff-validator/SKILL.md",
    ".codex/skills/llm-handoff-finalizer/SKILL.md",
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

# The repo skill each role uses when it is mapped to `codex`.
CODEX_ROLE_SKILLS = {
    "planner": "llm-handoff-planner",
    "backend": "llm-handoff",
    "frontend": "llm-handoff-frontend",
    "auditor": "llm-handoff-auditor",
    "validator": "llm-handoff-validator",
    "finalizer": "llm-handoff-finalizer",
}
NEW_CODEX_ROLE_SKILLS = {
    role: skill for role, skill in CODEX_ROLE_SKILLS.items() if role != "backend"
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


def test_reference_workflow_ships_a_codex_skill_for_every_role() -> None:
    config = load_dispatch_config(
        repo_root=REFERENCE_WORKFLOW,
        config_path=REFERENCE_WORKFLOW / "dispatch_config.yaml",
    )

    assert set(CODEX_ROLE_SKILLS) == set(config.agents)
    for role, skill_name in CODEX_ROLE_SKILLS.items():
        relative_path = Path(".codex") / "skills" / skill_name / "SKILL.md"
        template_path = REFERENCE_WORKFLOW / relative_path
        assert template_path.is_file(), relative_path
        assert template_path.read_bytes() == (REPO_ROOT / relative_path).read_bytes()
        content = template_path.read_text(encoding="utf-8")
        assert content.startswith("---\n"), relative_path
        frontmatter = content.split("---\n", 2)[1]
        assert f'name: "{skill_name}"\n' in frontmatter, relative_path
        assert re.search(r'(?m)^description: ".+"$', frontmatter), relative_path
        assert f"`{role}` role" in content, relative_path
        for field in (
            "next_agent",
            "producer",
            "epic_id",
            "story_id",
            "story_title",
            "remaining_stories",
        ):
            assert field in content, (relative_path, field)
        assert "never run `git push`" in content.lower(), relative_path


def test_new_codex_role_skills_have_no_openai_yaml() -> None:
    for skill_name in NEW_CODEX_ROLE_SKILLS.values():
        for root in (REFERENCE_WORKFLOW, REPO_ROOT):
            agents_dir = root / ".codex" / "skills" / skill_name / "agents"
            assert not agents_dir.exists(), agents_dir


def test_codex_role_skills_do_not_point_at_dispatcher_docs() -> None:
    for skill_name in NEW_CODEX_ROLE_SKILLS.values():
        content = (
            REFERENCE_WORKFLOW / ".codex" / "skills" / skill_name / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "CONFIGURATION.md" not in content, skill_name
        assert "docs/ARCHITECTURE.md" not in content, skill_name


def test_shipped_template_files_do_not_point_at_dispatcher_docs() -> None:
    # Scan exactly the files `llm-handoff init` ships: every template file
    # except the top-level README.md and any bytecode caches. A target
    # repository has neither of these docs.
    shipped = template_files(REFERENCE_WORKFLOW)
    assert shipped
    assert Path("README.md") not in shipped

    for shipped_path in shipped:
        relative_path = shipped_path.as_posix()
        content = (REFERENCE_WORKFLOW / shipped_path).read_text(encoding="utf-8")
        assert "docs/ARCHITECTURE.md" not in content, relative_path
        if relative_path == "dispatch_config.yaml":
            for line in content.splitlines():
                if "CONFIGURATION.md" in line:
                    assert "llm-handoff's CONFIGURATION.md" in line, line
        else:
            assert "CONFIGURATION.md" not in content, relative_path


def test_codex_validator_skill_covers_validation_and_routing() -> None:
    content = (
        REFERENCE_WORKFLOW / ".codex" / "skills" / "llm-handoff-validator" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "## Handoff Validation" in content
    assert "## Handoff Routing" in content
    assert "The dispatcher's prompt names which task applies." in content


def test_codex_rules_allow_no_git_writes_and_forbid_push() -> None:
    rules = (REFERENCE_WORKFLOW / ".codex" / "rules" / "llm-handoff.rules").read_text(
        encoding="utf-8"
    )
    allowed = re.findall(
        r'pattern = (\[[^\]]*\]),\s*decision = "allow"', rules
    )
    forbidden = re.findall(
        r'pattern = (\[[^\]]*\]),\s*decision = "forbidden"', rules
    )

    # Prefix rules cannot constrain trailing arguments, so allowing
    # `git commit` would also allow `-a` and `--amend`.
    assert allowed == []
    assert '["git", "push"]' in forbidden
