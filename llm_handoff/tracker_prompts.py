"""Package-internal Linear-mode prompt templates.

The Markdown files under ``llm_handoff/prompt_templates/linear/`` are static
text: they name settings by their ``dispatch_config.yaml`` keys and embed no
values. This module loads them; ``llm-handoff init --tracker linear``
renders them as managed blocks in ``AGENTS.md`` and
``docs/handoff/HANDBOOK.md``, in a fresh repository or when it switches an
initialized one to Linear mode (``init --tracker none`` removes them again). ``linear_issue_template()`` returns the example
Linear issue description whose ``## Work Packet`` labels match the Work Packet
parser; the planner copies those fields into ``HANDOFF.md``.
"""

from __future__ import annotations

from importlib.resources import files

from llm_handoff.config import AgentRole

LINEAR_PROMPT_ROLES: tuple[AgentRole, ...] = (
    "planner",
    "tpm",
    "backend",
    "frontend",
    "auditor",
    "validator",
    "finalizer",
)

# Evidence-comment events and the roles allowed to post each one. Must match
# ``EVIDENCE_COMMENT.md``.
LINEAR_EVIDENCE_EVENTS: dict[str, tuple[AgentRole, ...]] = {
    "lease_opened": ("planner", "tpm"),
    "implementation_handoff": ("backend", "frontend"),
    "audit_passed": ("auditor",),
    "audit_failed": ("auditor",),
    "user_attention_requested": ("planner", "tpm", "auditor"),
    "user_attention_cleared": ("planner", "tpm", "auditor"),
    "epic_closed": ("finalizer",),
    "handoff_repaired": ("planner", "validator"),
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


def linear_role_section(role: str, *, tpm_configured: bool = False) -> str:
    """Linear-mode section for one role prompt.

    With ``tpm_configured`` the planner section is the epic-level one from
    ``roles/planner_with_tpm.md``: the tpm section holds the selection rules.
    Every other role returns the same text either way.
    """

    if role not in LINEAR_PROMPT_ROLES:
        raise ValueError(
            f"Unknown Linear prompt role {role!r}; expected one of "
            f"{', '.join(LINEAR_PROMPT_ROLES)}."
        )
    if role == "planner" and tpm_configured:
        return _read_template("roles", "planner_with_tpm.md")
    return _read_template("roles", f"{role}.md")


def linear_evidence_contract() -> str:
    """The YAML evidence-comment contract."""

    return _read_template("EVIDENCE_COMMENT.md")


def linear_issue_template() -> str:
    """The Linear issue template: the Work Packet source the planner copies."""

    return _read_template("ISSUE_TEMPLATE.md")
