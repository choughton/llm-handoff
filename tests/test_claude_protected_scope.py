"""Pre-dispatch pause for `.claude` paths scoped to a dontAsk Claude role."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from llm_handoff import config as config_module
from llm_handoff import orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import DispatchConfig
from llm_handoff.validator import ValidationResult
from llm_handoff.work_claim import WorkPacket, claude_protected_scope_entries


USER_HANDOFF = """---
next_agent: user
reason: "Human decision required."
status: escalate_to_user
producer: planner
---

## Question

Which role should take the `.claude/` edits?
"""


def _handoff(next_agent: str, files_in_scope: str) -> str:
    return f"""---
next_agent: {next_agent}
reason: "Implement the helper sync story."
producer: planner
---

# Handoff: helper sync

## Work Packet

- **Objective:** Sync the helper files.
- **Files in scope:** {files_in_scope}
- **Files out of bounds:** none
- **Context:** none
- **Verification command:** `python -m pytest tests -q`
- **Expected next route:** auditor

## Next Step

- **{next_agent}:** Sync the helper files.
"""


NESTED_HANDOFF = _handoff(
    "backend",
    "`.agents/agents/ledger-updater.md`, "
    "`examples/reference-workflow/.claude/agents/ledger-updater.md`",
)
ROOT_HANDOFF = _handoff(
    "backend", "`.claude/agents/ledger-updater.md`, `.codex/skills/x/SKILL.md`"
)
UNPROTECTED_HANDOFF = _handoff(
    "backend", "`.agents/agents/ledger-updater.md`, `.codex/skills/x/SKILL.md`"
)


def _packet(*files_in_scope: str) -> WorkPacket:
    return WorkPacket(
        files_in_scope=files_in_scope,
        files_out_of_bounds=(),
        verification_commands=(),
        section_text="",
    )


def _agents(**overrides: dict[str, Any]) -> dict[str, Any]:
    agents = {
        role: agent.model_dump(exclude_none=True)
        for role, agent in config_module._default_agent_configs().items()
    }
    for role, values in overrides.items():
        agents[role] = {**agents[role], **values}
    return agents


def _config(repo_root: Path, **overrides: Any) -> DispatchConfig:
    agent_overrides = overrides.pop("agent_overrides", {"backend": {"provider": "claude"}})
    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "poll_interval_seconds": 0,
        "pause_recovery": "off",
        "agents": _agents(**agent_overrides),
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
        "enforce_work_packet": False,
        "work_packet_opt_out_reason": "Tests cover routing without the gate.",
    }
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _write_repo(tmp_path: Path, handoff_content: str) -> Path:
    handoff_path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(handoff_content, encoding="utf-8")
    (tmp_path / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("dispatch test fixture\n", encoding="utf-8")
    return handoff_path


def _dispatch_result() -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


class _Harness:
    """Stubbed agents for one run_loop call."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        handoff_path: Path,
        *,
        planner_writes: list[str] | None = None,
        backend_writes: list[str] | None = None,
    ) -> None:
        self.planner_instructions: list[str | None] = []
        self.backend_calls = 0
        self.logs: list[tuple[str, str]] = []
        planner_queue = list(planner_writes or [])
        backend_queue = list(backend_writes or [])

        def fake_planner(path: Path, **kwargs: Any) -> DispatchResult:
            self.planner_instructions.append(kwargs.get("additional_instruction"))
            if planner_queue:
                path.write_text(planner_queue.pop(0), encoding="utf-8")
            return _dispatch_result()

        def fake_backend(path: Path, **_kwargs: Any) -> DispatchResult:
            self.backend_calls += 1
            if backend_queue:
                path.write_text(backend_queue.pop(0), encoding="utf-8")
            return _dispatch_result()

        def fake_support(_name: str, _prompt: str, **_kwargs: Any) -> SubagentResult:
            return SubagentResult(
                stdout="VALID: YES\n", stderr="", exit_code=0, elapsed_seconds=0.01
            )

        monkeypatch.setattr(orchestrator, "invoke_planner_role", fake_planner)
        monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
        monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
        monkeypatch.setattr(
            orchestrator,
            "validate_handoff",
            lambda *_a, **_k: ValidationResult(verdict="YES", warnings=[], errors=[]),
        )
        monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)

    def run(self, config: DispatchConfig) -> int:
        return orchestrator.run_loop(
            config, log=lambda level, message: self.logs.append((level, message))
        )

    def messages(self, level: str) -> list[str]:
        return [message for logged, message in self.logs if logged == level]


@pytest.mark.parametrize(
    "entry",
    [
        ".claude/agents/ledger-updater.md",
        "examples/reference-workflow/.claude/agents/ledger-updater.md",
        ".claude/",
        ".claude",
        ".claude/**",
        "examples/*/.claude/agents/*.md",
        "pkg/.Claude/settings.json",
        # Memory exceptions cover Markdown files only, and only in the
        # project's own `.claude` folder.
        ".claude/agent-memory/",
        ".claude/agent-memory/reviewer/notes.json",
        "examples/app/.claude/agent-memory/reviewer/MEMORY.md",
        "examples/app/.claude/worktrees/feature/src/app.py",
        ".claude/agents/agent-memory/x.md",
    ],
)
def test_entries_with_a_claude_segment_are_protected(entry: str) -> None:
    assert claude_protected_scope_entries(_packet(entry)) == (entry,)


@pytest.mark.parametrize(
    "entry",
    [
        ".agents/agents/ledger-updater.md",
        ".codex/skills/llm-handoff/SKILL.md",
        ".claude-notes/todo.md",
        "docs/claude/notes.md",
        "src/settings.claude",
        "examples/reference-workflow/",
        # Writable exceptions Claude Code documents under the project's
        # `.claude` folder.
        ".claude/agent-memory/reviewer/MEMORY.md",
        ".claude/agent-memory-local/reviewer/notes.md",
        ".claude/agent-memory/**/*.md",
        ".claude/worktrees/feature/src/app.py",
        ".claude/worktrees/",
    ],
)
def test_entries_outside_a_protected_claude_folder_pass(entry: str) -> None:
    assert claude_protected_scope_entries(_packet(entry)) == ()


def test_only_the_protected_entries_are_returned() -> None:
    packet = _packet(
        ".agents/agents/x.md", "examples/app/.claude/agents/x.md", ".claude/x.md"
    )

    assert claude_protected_scope_entries(packet) == (
        "examples/app/.claude/agents/x.md",
        ".claude/x.md",
    )


@pytest.mark.parametrize("handoff", [NESTED_HANDOFF, ROOT_HANDOFF])
def test_reason_names_the_entries_and_the_options(
    tmp_path: Path, handoff: str
) -> None:
    reason = orchestrator._claude_protected_scope_pause_reason(
        _config(tmp_path), "backend", handoff
    )

    assert reason is not None
    assert reason.startswith("CLAUDE PROTECTED PATHS IN SCOPE: the backend role")
    assert "--permission-mode dontAsk" in reason
    assert "an operator applies those edits" in reason
    assert "provider is not claude" in reason
    assert ".agents/" not in reason
    assert ".codex/" not in reason
    assert ".claude/agents/ledger-updater.md`" in reason
    assert reason.isascii()


@pytest.mark.parametrize(
    ("route", "agent_overrides", "use_manual_frontend"),
    [
        ("backend", {"backend": {"provider": "codex"}}, False),
        ("backend", {"backend": {"provider": "claude", "unsafe_bypass": True}}, False),
        ("planner", {"planner": {"provider": "claude"}}, False),
        ("auditor", {}, False),
        ("frontend", {"frontend": {"provider": "claude"}}, True),
    ],
)
def test_no_reason_when_the_role_can_write_or_is_not_an_implementer(
    tmp_path: Path,
    route: str,
    agent_overrides: dict[str, Any],
    use_manual_frontend: bool,
) -> None:
    config = _config(
        tmp_path,
        agent_overrides=agent_overrides,
        use_manual_frontend=use_manual_frontend,
    )

    assert (
        orchestrator._claude_protected_scope_pause_reason(
            config, route, _handoff(route, "`.claude/agents/x.md`")
        )
        is None
    )


def test_frontend_claude_role_gets_a_reason(tmp_path: Path) -> None:
    config = _config(tmp_path, agent_overrides={"frontend": {"provider": "claude"}})

    reason = orchestrator._claude_protected_scope_pause_reason(
        config, "frontend", _handoff("frontend", "`app/.claude/agents/x.md`")
    )

    assert reason is not None
    assert "the frontend role" in reason


def test_no_reason_without_a_work_packet(tmp_path: Path) -> None:
    handoff = "## Next Step\n\n- **backend:** Edit `.claude/agents/x.md`.\n"

    assert (
        orchestrator._claude_protected_scope_pause_reason(
            _config(tmp_path), "backend", handoff
        )
        is None
    )


@pytest.mark.parametrize("handoff", [NESTED_HANDOFF, ROOT_HANDOFF])
def test_dispatcher_pauses_instead_of_dispatching_the_claude_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handoff: str
) -> None:
    handoff_path = _write_repo(tmp_path, handoff)
    harness = _Harness(monkeypatch, handoff_path)

    assert harness.run(_config(tmp_path)) == 0

    assert harness.backend_calls == 0
    pauses = harness.messages("PAUSE")
    assert any(
        message.startswith("CLAUDE PROTECTED PATHS IN SCOPE: the backend role")
        for message in pauses
    )
    assert not any(
        "Dispatching backend" in message for message in harness.messages("DISPATCH")
    )


def test_pause_recovery_gives_the_planner_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, NESTED_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, planner_writes=[USER_HANDOFF])

    assert harness.run(_config(tmp_path, pause_recovery="planner")) == 0

    assert harness.backend_calls == 0
    assert len(harness.planner_instructions) == 1
    instruction = harness.planner_instructions[0]
    assert instruction is not None
    assert "CLAUDE PROTECTED PATHS IN SCOPE" in instruction
    assert "examples/reference-workflow/.claude/agents/ledger-updater.md" in instruction


def test_non_claude_backend_dispatches_the_same_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, NESTED_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, backend_writes=[USER_HANDOFF])

    config = _config(tmp_path, agent_overrides={"backend": {"provider": "codex"}})
    assert harness.run(config) == 0

    assert harness.backend_calls == 1
    assert not any(
        "CLAUDE PROTECTED PATHS" in message for message in harness.messages("PAUSE")
    )


def test_unprotected_helper_paths_dispatch_the_claude_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, UNPROTECTED_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path, backend_writes=[USER_HANDOFF])

    assert harness.run(_config(tmp_path)) == 0

    assert harness.backend_calls == 1


def test_dry_run_logs_the_pause_it_would_take(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_path = _write_repo(tmp_path, ROOT_HANDOFF)
    harness = _Harness(monkeypatch, handoff_path)

    assert harness.run(_config(tmp_path, dry_run=True)) == 0

    assert harness.backend_calls == 0
    assert any(
        message.startswith("[DRY RUN] Would pause: CLAUDE PROTECTED PATHS IN SCOPE")
        for message in harness.messages("WARN")
    )
    assert "[DRY RUN] Would dispatch backend" not in harness.messages("DISPATCH")
