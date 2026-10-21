from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml

from llm_handoff.antigravity_permissions import (
    ANTIGRAVITY_AGENT_TOOLS,
    ANTIGRAVITY_AGENT_WRITE_TOOLS,
)
from llm_handoff.config import load_dispatch_config
from llm_handoff.init_workflow import template_files
from llm_handoff.router import parse_handoff_frontmatter, route


REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_WORKFLOW = REPO_ROOT / "examples" / "reference-workflow"

REQUIRED_TEMPLATE_FILES = (
    "AGENTS.md",
    "PROJECT_STATE.md",
    "dispatch_config.yaml",
    "docs/handoff/HANDOFF.md",
    "docs/handoff/HANDBOOK.md",
    "docs/handoff/README.md",
    "docs/handoff/SHARED_REPO_INIT_PROMPT.md",
    "docs/handoff/PLANNER_INITIAL_PROMPT.md",
    "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
    "docs/handoff/TPM_HANDOFF_PROMPT.md",
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
    ".codex/skills/llm-handoff-tpm/SKILL.md",
    ".codex/rules/llm-handoff.rules",
    ".claude/agents/auditor.md",
    ".claude/agents/handoff-router.md",
    ".claude/agents/handoff-validator.md",
    ".claude/agents/ledger-updater.md",
    ".claude/agents/planner.md",
    ".claude/agents/tpm.md",
    ".claude/agents/backend.md",
    ".claude/agents/frontend.md",
    ".claude/settings.json",
    ".claude/hooks/block_dangerous_commands.py",
    ".agents/agents/planner.md",
    ".agents/agents/tpm.md",
    ".agents/agents/backend.md",
    ".agents/agents/frontend.md",
    ".agents/agents/auditor.md",
    ".agents/agents/handoff-validator.md",
    ".agents/agents/handoff-router.md",
    ".agents/agents/ledger-updater.md",
    ".agents/agents/supervisor-repair.md",
)

COPIED_TEMPLATE_FILES = (
    "docs/handoff/HANDBOOK.md",
    "docs/handoff/README.md",
    "docs/handoff/SHARED_REPO_INIT_PROMPT.md",
    "docs/handoff/PLANNER_INITIAL_PROMPT.md",
    "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
    "docs/handoff/TPM_HANDOFF_PROMPT.md",
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
    ".codex/skills/llm-handoff-tpm/SKILL.md",
    ".codex/rules/llm-handoff.rules",
    ".claude/agents/auditor.md",
    ".claude/agents/handoff-router.md",
    ".claude/agents/handoff-validator.md",
    ".claude/agents/ledger-updater.md",
    ".claude/agents/planner.md",
    ".claude/agents/tpm.md",
    ".claude/agents/backend.md",
    ".claude/agents/frontend.md",
    ".agents/agents/planner.md",
    ".agents/agents/tpm.md",
    ".agents/agents/backend.md",
    ".agents/agents/frontend.md",
    ".agents/agents/auditor.md",
    ".agents/agents/handoff-validator.md",
    ".agents/agents/handoff-router.md",
    ".agents/agents/ledger-updater.md",
    ".agents/agents/supervisor-repair.md",
)

# Gemini CLI instruction files that neither the repository nor the template
# ships any more; `init` must not write them.
REMOVED_GEMINI_PATHS = (".gemini", ".geminiignore")

# The Antigravity agent files, named for the `--agent` value the adapter
# passes: the role, the support subagent name for validator and finalizer,
# or `supervisor-repair` for a supervised repair launch.
ANTIGRAVITY_AGENT_FILES = (
    ".agents/agents/planner.md",
    ".agents/agents/tpm.md",
    ".agents/agents/backend.md",
    ".agents/agents/frontend.md",
    ".agents/agents/auditor.md",
    ".agents/agents/handoff-validator.md",
    ".agents/agents/handoff-router.md",
    ".agents/agents/ledger-updater.md",
    ".agents/agents/supervisor-repair.md",
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
# The optional `tpm` role is not in the reference config, so its skill is
# checked beside the configured roles rather than in CODEX_ROLE_SKILLS.
CODEX_TPM_SKILL = "llm-handoff-tpm"
NEW_CODEX_ROLE_SKILLS_WITH_TPM = {**NEW_CODEX_ROLE_SKILLS, "tpm": CODEX_TPM_SKILL}


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


def test_gemini_instruction_files_are_not_shipped() -> None:
    for root in (REPO_ROOT, REFERENCE_WORKFLOW):
        for relative_path in REMOVED_GEMINI_PATHS:
            assert not (root / relative_path).exists(), root / relative_path
    shipped = template_files(REFERENCE_WORKFLOW)
    assert not [
        path for path in shipped if path.parts[0] in REMOVED_GEMINI_PATHS
    ]


FINALIZER_HELPER_FILES = (
    ".codex/skills/llm-handoff-finalizer/SKILL.md",
    ".claude/agents/ledger-updater.md",
    ".agents/agents/ledger-updater.md",
)


def test_finalizer_helpers_defer_to_the_configured_project_state_path() -> None:
    for root in (REPO_ROOT, REFERENCE_WORKFLOW):
        for relative_path in FINALIZER_HELPER_FILES:
            path = root / relative_path
            content = path.read_text(encoding="utf-8")

            assert "project state file named in the dispatcher prompt" in content, path
            count = content.count("PROJECT_STATE.md")
            assert count <= 1, path
            if count == 1:
                assert "(`PROJECT_STATE.md` by default)" in content, path
            assert "PROJECT_STATE.md/HANDOFF.md commit" not in content, path


NON_CLAUDE_ROLE_HELPER_FILES = (
    ".agents/agents/planner.md",
    ".agents/agents/tpm.md",
    ".agents/agents/backend.md",
    ".agents/agents/frontend.md",
    ".agents/agents/auditor.md",
    ".agents/agents/handoff-validator.md",
    ".agents/agents/handoff-router.md",
    ".codex/skills/llm-handoff/SKILL.md",
    ".codex/skills/llm-handoff-planner/SKILL.md",
    ".codex/skills/llm-handoff-tpm/SKILL.md",
    ".codex/skills/llm-handoff-frontend/SKILL.md",
    ".codex/skills/llm-handoff-auditor/SKILL.md",
    ".codex/skills/llm-handoff-validator/SKILL.md",
)

PROJECT_STATE_DEFERRAL = (
    "The project state file (`PROJECT_STATE.md` unless the dispatcher prompt "
    "or `AGENTS.md` names another path)"
)


def test_non_claude_role_helpers_defer_to_the_configured_project_state_path() -> None:
    for root in (REPO_ROOT, REFERENCE_WORKFLOW):
        for relative_path in NON_CLAUDE_ROLE_HELPER_FILES:
            path = root / relative_path
            content = path.read_text(encoding="utf-8")

            assert PROJECT_STATE_DEFERRAL in content, path
            assert "`PROJECT_STATE.md` if present" not in content, path
            assert "Do not modify `PROJECT_STATE.md`" not in content, path
            assert "Do not edit `PROJECT_STATE.md`" not in content, path
            is_planner = (
                path.name == "planner.md" or "llm-handoff-planner" in relative_path
            )
            if not is_planner:
                assert content.count("PROJECT_STATE.md") == 1, path


# Claude subagent files for the dispatcher roles. The ledger-updater is
# checked by the finalizer test above.
CLAUDE_ROLE_HELPER_FILES = (
    ".claude/agents/planner.md",
    ".claude/agents/tpm.md",
    ".claude/agents/backend.md",
    ".claude/agents/frontend.md",
    ".claude/agents/auditor.md",
    ".claude/agents/handoff-validator.md",
    ".claude/agents/handoff-router.md",
)


def test_claude_role_helpers_defer_to_the_configured_project_state_path() -> None:
    for root in (REPO_ROOT, REFERENCE_WORKFLOW):
        for relative_path in CLAUDE_ROLE_HELPER_FILES:
            path = root / relative_path
            content = path.read_text(encoding="utf-8")

            assert PROJECT_STATE_DEFERRAL in content, path
            assert "`PROJECT_STATE.md` if present" not in content, path
            assert "Do not modify `PROJECT_STATE.md`" not in content, path
            assert "Do not edit `PROJECT_STATE.md`" not in content, path
            if path.name != "planner.md":
                assert content.count("PROJECT_STATE.md") == 1, path


def test_reference_workflow_config_loads() -> None:
    config = load_dispatch_config(
        repo_root=REFERENCE_WORKFLOW,
        config_path=REFERENCE_WORKFLOW / "dispatch_config.yaml",
    )

    assert config.handoff_path == Path("docs/handoff/HANDOFF.md")
    assert config.project_state_path == Path("PROJECT_STATE.md")
    assert config.agents["planner"].provider == "antigravity"
    assert config.agents["frontend"].provider == "antigravity"
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


def test_reference_workflow_ships_an_antigravity_agent_for_every_role() -> None:
    read_only_tools = [
        tool
        for tool in ANTIGRAVITY_AGENT_TOOLS
        if tool not in ANTIGRAVITY_AGENT_WRITE_TOOLS
    ]
    for relative_path in ANTIGRAVITY_AGENT_FILES:
        template_path = REFERENCE_WORKFLOW / relative_path
        assert template_path.is_file(), relative_path
        assert template_path.read_bytes() == (REPO_ROOT / relative_path).read_bytes()
        for root in (REPO_ROOT, REFERENCE_WORKFLOW):
            path = root / relative_path
            content = path.read_text(encoding="utf-8")
            assert content.startswith("---\n"), path
            frontmatter = yaml.safe_load(content.split("---\n", 2)[1])
            assert isinstance(frontmatter, dict), path
            assert frontmatter.get("name") == Path(relative_path).stem, path
            description = frontmatter.get("description")
            assert isinstance(description, str) and description.strip(), path
            assert set(frontmatter) == {"name", "description", "tools"}, path
            tools = frontmatter["tools"]
            assert isinstance(tools, list), path
            assert all(isinstance(tool, str) for tool in tools), path
            assert len(tools) == len(set(tools)), path
            assert all(tool in ANTIGRAVITY_AGENT_TOOLS for tool in tools), path
            if Path(relative_path).stem == "handoff-validator":
                assert tools == read_only_tools, path
            else:
                assert tools == list(ANTIGRAVITY_AGENT_TOOLS), path


def test_antigravity_validator_limits_run_command_to_read_only_git() -> None:
    relative_path = Path(".agents") / "agents" / "handoff-validator.md"
    for root in (REPO_ROOT, REFERENCE_WORKFLOW):
        path = root / relative_path
        content = " ".join(path.read_text(encoding="utf-8").split())
        for command in (
            "`git cat-file`",
            "`git status`",
            "`git rev-parse`",
            "`git log`",
            "`git show`",
            "`git diff`",
        ):
            assert command in content, (path, command)
        assert (
            "Never run `git add`, `git commit`, `python`, `python3`, `pytest`, or"
            in content
        ), path


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


def test_reference_workflow_ships_a_codex_tpm_skill() -> None:
    relative_path = Path(".codex") / "skills" / CODEX_TPM_SKILL / "SKILL.md"
    template_path = REFERENCE_WORKFLOW / relative_path
    assert template_path.is_file(), relative_path
    assert template_path.read_bytes() == (REPO_ROOT / relative_path).read_bytes()
    content = template_path.read_text(encoding="utf-8")
    assert content.startswith("---\n"), relative_path
    frontmatter = content.split("---\n", 2)[1]
    assert f'name: "{CODEX_TPM_SKILL}"\n' in frontmatter, relative_path
    assert re.search(r'(?m)^description: ".+"$', frontmatter), relative_path
    assert "`tpm` role" in content, relative_path
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
    assert "producer: tpm" in content, relative_path
    enum_lines = [
        line for line in content.splitlines() if line.startswith("next_agent: <enum>")
    ]
    assert enum_lines, relative_path
    for line in enum_lines:
        assert "finalizer" not in line, relative_path
        assert "validator" not in line, relative_path


def test_new_codex_role_skills_have_no_openai_yaml() -> None:
    for skill_name in NEW_CODEX_ROLE_SKILLS_WITH_TPM.values():
        for root in (REFERENCE_WORKFLOW, REPO_ROOT):
            agents_dir = root / ".codex" / "skills" / skill_name / "agents"
            assert not agents_dir.exists(), agents_dir


def test_codex_role_skills_do_not_point_at_dispatcher_docs() -> None:
    for skill_name in NEW_CODEX_ROLE_SKILLS_WITH_TPM.values():
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


@pytest.mark.parametrize(
    "relative_path",
    [
        ".codex/skills/llm-handoff-validator/SKILL.md",
    ],
)
def test_single_file_validator_helpers_cover_validation_and_routing(
    relative_path: str,
) -> None:
    content = (REFERENCE_WORKFLOW / relative_path).read_text(encoding="utf-8")

    assert "## Handoff Validation" in content
    assert "## Handoff Routing" in content
    assert "The dispatcher's prompt names which task applies" in content
    assert "VALID: YES | NO | WARNINGS-ONLY" in content
    assert "ROUTING UPDATED: YES" in content


LINEAR_EVIDENCE_HELPER_FILES = (
    REPO_ROOT / ".codex" / "skills" / "llm-handoff-validator" / "SKILL.md",
    REFERENCE_WORKFLOW / ".codex" / "skills" / "llm-handoff-validator" / "SKILL.md",
    REPO_ROOT / ".agents" / "agents" / "handoff-validator.md",
    REFERENCE_WORKFLOW / ".agents" / "agents" / "handoff-validator.md",
    REPO_ROOT / ".claude" / "agents" / "handoff-validator.md",
    REFERENCE_WORKFLOW / ".claude" / "agents" / "handoff-validator.md",
)


def test_validator_helpers_report_a_linear_evidence_row() -> None:
    for path in LINEAR_EVIDENCE_HELPER_FILES:
        content = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(REPO_ROOT).as_posix()

        assert "LINEAR-EVIDENCE:" in content, relative_path
        assert "## Linear Mode: validator" in content, relative_path


PLANNER_PROMPT_FILES = (
    ".claude/agents/planner.md",
    ".codex/skills/llm-handoff-planner/SKILL.md",
    "docs/handoff/PLANNER_HANDOFF_PROMPT.md",
    "docs/handoff/PLANNER_INITIAL_PROMPT.md",
    ".agents/agents/planner.md",
)
# PLANNER_INITIAL_PROMPT.md seeds the first assignment and never sees an
# epic close, so it is the one planner prompt without the epic-close rules.
PLANNER_EPIC_CLOSE_PROMPT_FILES = tuple(
    relative_path
    for relative_path in PLANNER_PROMPT_FILES
    if relative_path != "docs/handoff/PLANNER_INITIAL_PROMPT.md"
)
AUDITOR_PROMPT_FILES = (
    ".claude/agents/auditor.md",
    ".codex/skills/llm-handoff-auditor/SKILL.md",
    "docs/handoff/AUDITOR_HANDOFF_PROMPT.md",
    ".agents/agents/auditor.md",
)


def test_planner_prompts_do_not_offer_a_finalizer_route() -> None:
    # The validator and router accept `next_agent: finalizer` only from
    # `producer: auditor`, so a planner prompt must not offer that route.
    for relative_path in PLANNER_PROMPT_FILES:
        content = (REFERENCE_WORKFLOW / relative_path).read_text(encoding="utf-8")
        assert "approved epic closeout" not in content, relative_path
        assert "Use `next_agent: finalizer`" not in content, relative_path
        for line in content.splitlines():
            if line.startswith("next_agent: <enum>"):
                assert "finalizer" not in line, relative_path


def test_planner_prompts_send_an_epic_close_to_the_auditor() -> None:
    assert len(PLANNER_EPIC_CLOSE_PROMPT_FILES) == 4
    for relative_path in PLANNER_EPIC_CLOSE_PROMPT_FILES:
        content = (REFERENCE_WORKFLOW / relative_path).read_text(encoding="utf-8")
        assert "`close_type: epic` and a concrete `scope_sha`" in content, relative_path
        assert "is not evidence of a missing audit" in content, relative_path
        assert "logs/dispatch/audit/<timestamp>-cycle<N>/review.json" in content, (
            relative_path
        )


def test_auditor_prompts_route_the_last_story_of_an_epic_to_the_finalizer() -> None:
    for relative_path in AUDITOR_PROMPT_FILES:
        content = (REFERENCE_WORKFLOW / relative_path).read_text(encoding="utf-8")
        assert "`remaining_stories: []`" in content, relative_path
        assert "do not send it to `planner` to decide" in content, relative_path
        assert "Only the auditor may hand off to `finalizer`." in content, relative_path


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


IMPLEMENTER_PROMPT_FILES = (
    Path(".claude") / "agents" / "backend.md",
    Path(".claude") / "agents" / "frontend.md",
    Path(".codex") / "skills" / "llm-handoff" / "SKILL.md",
    Path(".codex") / "skills" / "llm-handoff-frontend" / "SKILL.md",
    Path("docs") / "handoff" / "BACKEND_HANDOFF_PROMPT.md",
    Path("docs") / "handoff" / "FRONTEND_HANDOFF_PROMPT.md",
    Path(".agents") / "agents" / "backend.md",
    Path(".agents") / "agents" / "frontend.md",
)


def test_implementer_prompts_carry_story_fields_forward() -> None:
    for relative_path in IMPLEMENTER_PROMPT_FILES:
        content = " ".join(
            (REFERENCE_WORKFLOW / relative_path).read_text(encoding="utf-8").split()
        )

        assert (
            "Keep `epic_id`, `story_id`, `story_title`, and `remaining_stories` "
            "from the assignment unchanged in the handback."
        ) in content, relative_path
        assert (
            "If the assignment has `remaining_stories: []`, "
            "write `remaining_stories: []`"
        ) in content, relative_path
