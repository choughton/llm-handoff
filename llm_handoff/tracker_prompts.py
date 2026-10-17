"""Package-internal Linear-mode prompt templates.

The Markdown files under ``llm_handoff/prompt_templates/linear/`` are static
text: they name settings by their ``dispatch_config.yaml`` keys and embed no
values. This module loads them; ``llm-handoff init --tracker linear``
renders them into a fresh repository as managed blocks in ``AGENTS.md`` and
``docs/handoff/HANDBOOK.md``.
"""

from __future__ import annotations

from importlib.resources import files

from llm_handoff.config import AgentRole

LINEAR_PROMPT_ROLES: tuple[AgentRole, ...] = (
    "planner",
    "backend",
    "frontend",
    "auditor",
    "validator",
    "finalizer",
)

# Evidence-comment events and the roles allowed to post each one. Must match
# ``EVIDENCE_COMMENT.md``.
LINEAR_EVIDENCE_EVENTS: dict[str, tuple[AgentRole, ...]] = {
    "lease_opened": ("planner",),
    "implementation_handoff": ("backend", "frontend"),
    "audit_passed": ("auditor",),
    "audit_failed": ("auditor",),
    "user_attention_requested": ("planner", "auditor"),
    "user_attention_cleared": ("planner", "auditor"),
}

# Events whose comment must carry commit_sha, commands_run, output_summary,
# and unresolved_concerns.
LINEAR_EVIDENCE_REQUIRED_EVENTS: tuple[str, ...] = (
    "implementation_handoff",
    "audit_passed",
    "audit_failed",
)

_TEMPLATE_DIR = ("prompt_templates", "linear")


def _read_template(*parts: str) -> str:
    resource = files("llm_handoff")
    for part in (*_TEMPLATE_DIR, *parts):
        resource = resource / part
    return resource.read_text(encoding="utf-8")


def linear_agents_section() -> str:
    """Shared Linear-mode section for the repository ``AGENTS.md``."""

    return _read_template("AGENTS.md")


def linear_role_section(role: str) -> str:
    """Linear-mode section for one role prompt."""

    if role not in LINEAR_PROMPT_ROLES:
        raise ValueError(
            f"Unknown Linear prompt role {role!r}; expected one of "
            f"{', '.join(LINEAR_PROMPT_ROLES)}."
        )
    return _read_template("roles", f"{role}.md")


def linear_evidence_contract() -> str:
    """The YAML evidence-comment contract."""

    return _read_template("EVIDENCE_COMMENT.md")
