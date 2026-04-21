from __future__ import annotations

import pytest

from llm_handoff.handoff_normalizer import normalize_handoff_next_agent_text
from llm_handoff.router import RoutingDecision, route


LEGACY_FRONTEND_ALIAS = ("cross" + "fire") + "_frontend"


@pytest.mark.parametrize(
    "next_agent",
    [
        "backend",
        "planner",
        "frontend",
        "auditor",
        "finalizer",
        "user",
    ],
)
def test_route_supports_canonical_frontmatter_next_agent_values(
    next_agent: str,
) -> None:
    extra = (
        "scope_sha: 82ce839\nclose_type: epic\n" if next_agent == "finalizer" else ""
    )
    producer = "auditor" if next_agent == "finalizer" else "planner"
    handoff_content = f"""---
next_agent: {next_agent}
reason: Route enum value for dispatch.
{extra}producer: {producer}
---

# Handoff
"""

    assert route(handoff_content).route == next_agent


@pytest.mark.parametrize(
    "next_agent",
    [
        "codex",
        "gemini-pe",
        "gemini-frontend",
        "claude-audit",
        "claude-ledger",
        "manual frontend",
    ],
)
def test_route_rejects_provider_named_frontmatter_next_agent_values(
    next_agent: str,
) -> None:
    handoff_content = f"""---
next_agent: {next_agent}
reason: Provider names are adapter details, not public route names.
producer: planner
---

# Handoff
"""

    assert route(handoff_content) == RoutingDecision(
        route="unknown",
        confidence="LOW",
        source="frontmatter.invalid",
        reasoning="YAML routing frontmatter is present but invalid.",
        warnings=[
            f"Invalid HANDOFF routing frontmatter: next_agent `{next_agent}` is not recognized."
        ],
    )


def test_route_rejects_source_project_aliases() -> None:
    handoff_content = f"""---
next_agent: {LEGACY_FRONTEND_ALIAS}
reason: Should not be accepted in the public dispatcher.
producer: planner
---

# Handoff
"""

    assert route(handoff_content) == RoutingDecision(
        route="unknown",
        confidence="LOW",
        source="frontmatter.invalid",
        reasoning="YAML routing frontmatter is present but invalid.",
        warnings=[
            f"Invalid HANDOFF routing frontmatter: next_agent `{LEGACY_FRONTEND_ALIAS}` is not recognized."
        ],
    )


def test_route_rejects_broad_implementer_role() -> None:
    handoff_content = """---
next_agent: implementer
reason: Backend and frontend lanes should be explicit.
producer: planner
---

# Handoff
"""

    assert route(handoff_content) == RoutingDecision(
        route="unknown",
        confidence="LOW",
        source="frontmatter.invalid",
        reasoning="YAML routing frontmatter is present but invalid.",
        warnings=[
            "Invalid HANDOFF routing frontmatter: next_agent `implementer` is not recognized."
        ],
    )


def test_next_agent_normalizer_can_rewrite_freeform_backend_synonym() -> None:
    handoff_content = """---
next_agent: implementer
reason: Freeform role name from a writer.
producer: planner
---

# Handoff
"""

    normalization = normalize_handoff_next_agent_text(
        handoff_content,
        normalizer=lambda raw: "backend" if raw == "implementer" else "unknown",
    )

    assert normalization.rewritten
    assert normalization.original == "implementer"
    assert normalization.normalized == "backend"
    assert route(normalization.content).route == "backend"
