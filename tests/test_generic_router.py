from __future__ import annotations

import pytest

from llm_handoff.router import RoutingDecision, route


@pytest.mark.parametrize(
    ("next_agent", "expected_route"),
    [
        ("backend", "Codex"),
        ("codex", "Codex"),
        ("planner", "Gemini-PE"),
        ("gemini-pe", "Gemini-PE"),
        ("frontend", "Gemini-Frontend"),
        ("gemini-frontend", "Gemini-Frontend"),
        ("claude-audit", "ClaudeCode-Audit"),
        ("claude-ledger", "Epic-Close"),
        ("user", "Escalation"),
    ],
)
def test_route_supports_generic_frontmatter_next_agent_values(
    next_agent: str,
    expected_route: str,
) -> None:
    extra = "scope_sha: 82ce839\nclose_type: epic\n" if next_agent == "claude-ledger" else ""
    producer = "claude-audit" if next_agent == "claude-ledger" else "planner"
    handoff_content = f"""---
next_agent: {next_agent}
reason: Route enum value for dispatch.
{extra}producer: {producer}
---

# Handoff
"""

    assert route(handoff_content).route == expected_route


def test_route_rejects_source_project_aliases() -> None:
    handoff_content = """---
next_agent: crossfire_frontend
reason: Should not be accepted in the public dispatcher.
producer: planner
---

# Handoff
"""

    assert route(handoff_content) == RoutingDecision(
        route="Unknown",
        confidence="LOW",
        source="frontmatter.invalid",
        reasoning="YAML routing frontmatter is present but invalid.",
        warnings=[
            "Invalid HANDOFF routing frontmatter: next_agent `crossfire_frontend` is not recognized."
        ],
    )
