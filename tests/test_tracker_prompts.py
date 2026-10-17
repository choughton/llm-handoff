from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff import tracker_prompts
from llm_handoff.config import LINEAR_ROLE_TOOLS
from llm_handoff.tracker_prompts import (
    LINEAR_EVIDENCE_EVENTS,
    LINEAR_EVIDENCE_REQUIRED_EVENTS,
    LINEAR_PROMPT_ROLES,
    linear_agents_section,
    linear_evidence_contract,
    linear_role_section,
)

SOURCE_PACKAGE_DIR = Path(__file__).resolve().parents[1] / "llm_handoff"
TEMPLATE_DIR = SOURCE_PACKAGE_DIR / "prompt_templates" / "linear"
EXPECTED_FILES = {
    "AGENTS.md",
    "EVIDENCE_COMMENT.md",
    "roles/planner.md",
    "roles/backend.md",
    "roles/frontend.md",
    "roles/auditor.md",
    "roles/validator.md",
    "roles/finalizer.md",
}
REQUIRED_EVIDENCE_FIELDS = (
    "commit_sha",
    "commands_run",
    "output_summary",
    "unresolved_concerns",
)


def _all_sections() -> dict[str, str]:
    sections = {
        "AGENTS.md": linear_agents_section(),
        "EVIDENCE_COMMENT.md": linear_evidence_contract(),
    }
    for role in LINEAR_PROMPT_ROLES:
        sections[f"roles/{role}.md"] = linear_role_section(role)
    return sections


def test_prompt_roles_are_the_six_workflow_roles_in_order() -> None:
    assert LINEAR_PROMPT_ROLES == (
        "planner",
        "backend",
        "frontend",
        "auditor",
        "validator",
        "finalizer",
    )


def test_template_directory_holds_exactly_the_expected_files() -> None:
    found = {
        path.relative_to(TEMPLATE_DIR).as_posix()
        for path in TEMPLATE_DIR.rglob("*")
        if path.is_file()
    }
    assert found == EXPECTED_FILES


def test_every_loader_returns_non_empty_ascii_text() -> None:
    for name, text in _all_sections().items():
        assert text.strip(), name
        assert text.isascii(), name


def test_agents_section_heading_and_rules() -> None:
    text = linear_agents_section()
    assert text.startswith("## Linear Mode\n")
    for phrase in (
        "`tracker.provider`",
        "`docs/handoff/HANDOFF.md`",
        "`story_id` equals `tracker_issue`",
        "`tracker_url`",
        "`selection_basis`",
        "`priority`",
        "`manual_order`",
        "`created_at`",
        "MCP connector",
        "dispatcher never calls Linear",
        "`status: blocked_missing_context`",
        "`next_agent: user`",
        "tokens",
    ):
        assert phrase in text, phrase


def test_role_sections_have_role_headings() -> None:
    for role in LINEAR_PROMPT_ROLES:
        assert linear_role_section(role).startswith(f"## Linear Mode: {role}\n")


def test_evidence_contract_heading() -> None:
    assert linear_evidence_contract().startswith("## Linear Evidence Comment\n")


@pytest.mark.parametrize("role", ["user", "security", "", "Planner"])
def test_unknown_role_raises_value_error(role: str) -> None:
    with pytest.raises(ValueError):
        linear_role_section(role)


def test_no_placeholders_or_managed_block_markers() -> None:
    for name, text in _all_sections().items():
        assert "{" not in text, name
        assert "}" not in text, name
        assert "llm-handoff:begin" not in text, name
        assert "llm-handoff:end" not in text, name


def test_evidence_posters_have_save_comment() -> None:
    for event, roles in LINEAR_EVIDENCE_EVENTS.items():
        assert roles, event
        for role in roles:
            assert "save_comment" in LINEAR_ROLE_TOOLS[role], (event, role)


def test_evidence_event_role_mapping() -> None:
    assert LINEAR_EVIDENCE_EVENTS == {
        "lease_opened": ("planner",),
        "implementation_handoff": ("backend", "frontend"),
        "audit_passed": ("auditor",),
        "audit_failed": ("auditor",),
        "user_attention_requested": ("planner", "auditor"),
        "user_attention_cleared": ("planner", "auditor"),
    }
    assert LINEAR_EVIDENCE_REQUIRED_EVENTS == (
        "implementation_handoff",
        "audit_passed",
        "audit_failed",
    )


def test_contract_names_every_event_and_root_key() -> None:
    contract = linear_evidence_contract()
    assert "llm_handoff_event" in contract
    for event in LINEAR_EVIDENCE_EVENTS:
        assert f"`{event}`" in contract, event


def test_contract_table_matches_event_roles() -> None:
    contract = linear_evidence_contract()
    for event, roles in LINEAR_EVIDENCE_EVENTS.items():
        posters = ", ".join(f"`{role}`" for role in roles)
        assert f"| `{event}` | {posters} |" in contract, event


def test_contract_names_every_field() -> None:
    contract = linear_evidence_contract()
    for field in (
        "version",
        "issue",
        "tracker_url",
        "producer",
        "next_agent",
        "status",
        "state_from",
        "state_to",
        *REQUIRED_EVIDENCE_FIELDS,
    ):
        assert f"`{field}`" in contract, field


def test_contract_example_is_complete_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    contract = linear_evidence_contract()
    start = contract.index("```yaml\n") + len("```yaml\n")
    end = contract.index("```", start)
    example = yaml.safe_load(contract[start:end])
    assert example["llm_handoff_event"] in LINEAR_EVIDENCE_REQUIRED_EVENTS
    assert example["version"] == 1
    for field in (
        "issue",
        "tracker_url",
        "producer",
        "next_agent",
        "status",
        *REQUIRED_EVIDENCE_FIELDS,
    ):
        assert field in example, field
    allowed = LINEAR_EVIDENCE_EVENTS[example["llm_handoff_event"]]
    assert example["producer"] in allowed


@pytest.mark.parametrize("role", ["validator", "finalizer"])
def test_read_only_roles_mention_no_write_tools(role: str) -> None:
    text = linear_role_section(role)
    assert "save_comment" not in text
    assert "save_issue" not in text


def test_templates_load_from_source_tree() -> None:
    # The module under test is the source-tree copy, and the loaders return
    # the source-tree files byte for byte.
    module_dir = Path(tracker_prompts.__file__).resolve().parent
    assert module_dir == SOURCE_PACKAGE_DIR
    template_dir = TEMPLATE_DIR
    assert linear_agents_section() == (template_dir / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert linear_evidence_contract() == (
        template_dir / "EVIDENCE_COMMENT.md"
    ).read_text(encoding="utf-8")
    for role in LINEAR_PROMPT_ROLES:
        on_disk = (template_dir / "roles" / f"{role}.md").read_text(
            encoding="utf-8"
        )
        assert linear_role_section(role) == on_disk
