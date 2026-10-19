from __future__ import annotations

import dataclasses
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from llm_handoff.agent_types import SubagentResult


def _load_supervisor_module():
    return importlib.import_module("llm_handoff.supervisor")


def _write_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    handoff_path = repo_root / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        """---
next_agent: planner
reason: Scope the next task.
producer: user
---

# Handoff

Plan the next bounded task.
""",
        encoding="utf-8",
    )
    (repo_root / "AGENTS.md").write_text("repo instructions\n", encoding="utf-8")
    return repo_root


def _dispatch_result(*, exit_code: int = 0):
    supervisor = _load_supervisor_module()
    return supervisor.DispatchProcessResult(
        exit_code=exit_code,
        output_lines=("[INFO] Dispatch summary: failure_events=0",),
        elapsed_seconds=0.1,
    )


def _repair_result(*, exit_code: int = 0) -> SubagentResult:
    return SubagentResult(
        stdout="",
        stderr="",
        exit_code=exit_code,
        elapsed_seconds=0.1,
    )


def test_supervisor_doctor_reports_preflight_without_dispatch(tmp_path: Path) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    config = supervisor.SupervisorConfig(repo_root=repo_root, doctor=True)
    log_messages: list[tuple[str, str]] = []

    exit_code = supervisor.run_supervised_dispatch(
        config,
        log=lambda level, message: log_messages.append((level, message)),
        dispatch_runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("doctor mode must not dispatch"),
        ),
    )

    assert exit_code == 0
    assert ("INFO", "Doctor mode complete; dispatch was not started.") in log_messages
    state = json.loads(config.run_state_path_full_path.read_text(encoding="utf-8"))
    assert state["status"] == "doctor_passed"
    assert state["route"] == "planner"


def test_supervisor_logs_runtime_controls_and_writes_run_state(
    tmp_path: Path,
) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    config = supervisor.SupervisorConfig(
        repo_root=repo_root,
        dispatch_args=("--dry-run",),
    )
    log_messages: list[tuple[str, str]] = []

    exit_code = supervisor.run_supervised_dispatch(
        config,
        log=lambda level, message: log_messages.append((level, message)),
        dispatch_runner=lambda _config, _log: _dispatch_result(),
    )

    assert exit_code == 0
    assert ("INFO", "Repair role:        (disabled)") in log_messages
    assert ("INFO", "Heartbeat:          300s / boundary") in log_messages
    assert (
        "INFO",
        f"Run state:          {config.run_state_path_full_path}",
    ) in log_messages
    state = json.loads(config.run_state_path_full_path.read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["dispatch_args"] == ["--dry-run"]


def test_supervisor_writes_incident_bundle_when_repair_disabled(
    tmp_path: Path,
) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    config = supervisor.SupervisorConfig(repo_root=repo_root, max_repair_attempts=0)

    exit_code = supervisor.run_supervised_dispatch(
        config,
        dispatch_runner=lambda _config, _log: supervisor.DispatchProcessResult(
            exit_code=1,
            output_lines=("[INFO] --- Cycle 1 ---", "[ERROR] dispatcher failed"),
            elapsed_seconds=0.1,
        ),
    )

    assert exit_code == 1
    incident_dirs = list(config.incident_directory_full_path.glob("incident-*"))
    assert len(incident_dirs) == 1
    assert (incident_dirs[0] / "summary.json").exists()
    assert (incident_dirs[0] / "handoff-current.md").exists()
    assert (
        (incident_dirs[0] / "output-tail.log")
        .read_text(encoding="utf-8")
        .endswith("[ERROR] dispatcher failed\n")
    )


def test_supervisor_optional_repair_role_restarts_after_scoped_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    config = supervisor.SupervisorConfig(
        repo_root=repo_root,
        repair_role="validator",
        max_repair_attempts=1,
    )
    dispatch_results = [
        supervisor.DispatchProcessResult(
            exit_code=1,
            output_lines=("[AGENT] Post-dispatch gate FAILED for backend.",),
            elapsed_seconds=0.1,
        ),
        supervisor.DispatchProcessResult(
            exit_code=0,
            output_lines=("[INFO] Dispatch summary: failure_events=0",),
            elapsed_seconds=0.1,
        ),
    ]
    snapshots = [
        supervisor.RepoSnapshot(head_sha="a" * 40, dirty_files=(), handoff_sha="h1"),
        supervisor.RepoSnapshot(head_sha="a" * 40, dirty_files=(), handoff_sha="h1"),
        supervisor.RepoSnapshot(head_sha="b" * 40, dirty_files=(), handoff_sha="h2"),
        supervisor.RepoSnapshot(head_sha="b" * 40, dirty_files=(), handoff_sha="h2"),
        supervisor.RepoSnapshot(head_sha="b" * 40, dirty_files=(), handoff_sha="h2"),
    ]
    repair_packets: list[Path] = []
    monkeypatch.setattr(
        supervisor,
        "_git_changed_files",
        lambda *_args: ("docs/handoff/HANDOFF.md",),
    )

    def fake_repair_runner(_config: Any, failure_packet_path: Path, _log: Any) -> Any:
        repair_packets.append(failure_packet_path)
        return _repair_result()

    exit_code = supervisor.run_supervised_dispatch(
        config,
        dispatch_runner=lambda _config, _log: dispatch_results.pop(0),
        repair_runner=fake_repair_runner,
        snapshot_reader=lambda _repo_root: snapshots.pop(0),
    )

    assert exit_code == 0
    assert len(repair_packets) == 1
    packet = json.loads(repair_packets[0].read_text(encoding="utf-8"))
    assert packet["repair_role"] == "validator"


def test_supervisor_failure_policy_uses_specific_auth_and_network_markers() -> None:
    supervisor = _load_supervisor_module()

    assert (
        supervisor._failure_policy(
            supervisor.DispatchProcessResult(
                exit_code=1,
                output_lines=("Please run `codex auth login` to authenticate.",),
                elapsed_seconds=0.1,
            )
        ).action
        == "stop"
    )
    assert (
        supervisor._failure_policy(
            supervisor.DispatchProcessResult(
                exit_code=1,
                output_lines=("Help topic: codex auth login",),
                elapsed_seconds=0.1,
            )
        ).action
        == "repair"
    )
    assert (
        supervisor._failure_policy(
            supervisor.DispatchProcessResult(
                exit_code=1,
                output_lines=("network error while contacting model API",),
                elapsed_seconds=0.1,
            )
        ).action
        == "retry"
    )
    assert (
        supervisor._failure_policy(
            supervisor.DispatchProcessResult(
                exit_code=1,
                output_lines=("network calls completed before validation failed",),
                elapsed_seconds=0.1,
            )
        ).action
        == "repair"
    )


def test_supervisor_boundary_heartbeat_helpers() -> None:
    supervisor = _load_supervisor_module()
    state = supervisor.HeartbeatState()

    assert supervisor._child_boundary_from_line("[INFO] --- Cycle 2 ---")
    assert supervisor._child_boundary_from_line("[INFO] Routing instruction: backend")
    assert supervisor._should_emit_heartbeat(
        now=300.0,
        last_heartbeat_at=0.0,
        heartbeat_interval_seconds=300,
    )
    assert not supervisor._should_emit_heartbeat(
        now=299.9,
        last_heartbeat_at=0.0,
        heartbeat_interval_seconds=300,
    )
    assert (
        supervisor._record_child_boundary(
            state,
            "[INFO] Routing instruction: backend",
        )
        == "route=backend"
    )
    assert (
        supervisor._record_child_boundary(
            state,
            "[INFO] Routing instruction: backend",
        )
        is None
    )


def test_git_dirty_files_parses_real_porcelain_spacing(monkeypatch) -> None:
    supervisor = _load_supervisor_module()
    monkeypatch.setattr(
        supervisor,
        "_git_stdout",
        lambda *_args: "\n".join(
            [
                " M llm_handoff/supervisor.py",
                "M  tests/test_supervisor.py",
                "?? docs/SUPERVISED_DISPATCH.md",
            ]
        ),
    )

    assert supervisor._git_dirty_files(Path.cwd()) == (
        "docs/SUPERVISED_DISPATCH.md",
        "llm_handoff/supervisor.py",
        "tests/test_supervisor.py",
    )


def test_supervisor_repair_scope_prefixes_are_exact() -> None:
    supervisor = _load_supervisor_module()

    assert supervisor._out_of_scope_repair_files(
        (
            "scripts/dispatch-supervised.ps1",
            "scripts/dispatch-other.ps1",
            "docs/handoff/HANDOFF.md",
        ),
        allowed_repair_paths=("scripts/dispatch-supervised.ps1",),
    ) == ("scripts/dispatch-other.ps1",)


_GENERIC_REPAIR_SCOPE_FILES = (
    "llm_handoff/supervisor.py",
    "tests/test_supervisor.py",
    "README.md",
    "examples/reference-workflow/AGENTS.md",
    "docs/handoff/HANDOFF.md",
    ".claude/agents/planner.md",
)


def test_supervisor_default_repair_scope_excludes_target_source_and_tests() -> None:
    supervisor = _load_supervisor_module()

    assert supervisor._out_of_scope_repair_files(_GENERIC_REPAIR_SCOPE_FILES) == (
        "llm_handoff/supervisor.py",
        "tests/test_supervisor.py",
        "README.md",
        "examples/reference-workflow/AGENTS.md",
    )


def test_supervisor_repair_allow_paths_widen_default_scope() -> None:
    supervisor = _load_supervisor_module()

    assert supervisor._out_of_scope_repair_files(
        _GENERIC_REPAIR_SCOPE_FILES,
        allowed_repair_paths=("llm_handoff/", "tests/", "README.md"),
    ) == ("examples/reference-workflow/AGENTS.md",)


def test_supervisor_repair_prompt_lists_generic_default_paths(
    tmp_path: Path,
) -> None:
    supervisor = _load_supervisor_module()
    config = supervisor.SupervisorConfig(repo_root=tmp_path)

    prompt = supervisor._build_supervisor_repair_prompt(
        config,
        tmp_path / "failure-packet.md",
    )

    assert (
        "Allowed paths: docs/, .claude/agents/, .codex/skills/llm-handoff/, "
        ".agents/agents/." in prompt
    )
    assert ".gemini/agents/" not in prompt
    assert "llm_handoff/" not in prompt


def test_supervisor_default_repair_scope_admits_antigravity_agent_files() -> None:
    supervisor = _load_supervisor_module()

    assert supervisor._out_of_scope_repair_files(
        (
            ".agents/agents/supervisor-repair.md",
            ".agents/agents/planner.md",
            ".agents/settings.json",
        )
    ) == (".agents/settings.json",)


@pytest.mark.parametrize("repair_role", ["validator", "backend"])
def test_antigravity_repair_role_launches_shipped_supervisor_repair_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_role: str,
) -> None:
    from llm_handoff.agent_providers import antigravity as agy_provider
    from llm_handoff.agent_types import _ProcessResult
    from llm_handoff.init_workflow import init_reference_workflow

    supervisor = _load_supervisor_module()
    target = tmp_path / "target"
    init_reference_workflow(target, provider="antigravity", available_providers=())
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> _ProcessResult:
        del kwargs
        commands.append(list(command))
        return _ProcessResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(agy_provider, "_run_antigravity_command", fake_run)
    config = supervisor.SupervisorConfig(
        repo_root=target,
        config_path=target / "dispatch_config.yaml",
        repair_role=repair_role,
        max_repair_attempts=1,
    )

    supervisor._run_repair_role(
        config,
        target / "failure-packet.json",
        lambda level, message: None,
    )

    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("--agent") + 1] == "supervisor-repair"
    assert (target / ".agents" / "agents" / "supervisor-repair.md").is_file()
    repo_root = Path(__file__).resolve().parent.parent
    assert (
        repo_root
        / "examples"
        / "reference-workflow"
        / ".agents"
        / "agents"
        / "supervisor-repair.md"
    ).is_file()


@pytest.mark.parametrize("repair_role", ["validator", "backend"])
def test_antigravity_repair_ignores_role_agent_name_on_primary_and_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_role: str,
) -> None:
    from llm_handoff.agent_providers import antigravity as agy_provider
    from llm_handoff.agent_types import _ProcessResult
    from llm_handoff.failover import TIMEOUT_MARKER
    from llm_handoff.init_workflow import init_reference_workflow

    supervisor = _load_supervisor_module()
    target = tmp_path / "target"
    init_reference_workflow(target, provider="antigravity", available_providers=())
    real_load = supervisor.load_dispatch_config

    def load_with_agent_names(**kwargs: Any) -> Any:
        loaded = real_load(**kwargs)
        role_config = loaded.agents[repair_role]
        assert role_config.provider == "antigravity"
        fallback = role_config.model_copy(
            update={
                "agent_name": "custom-fallback-agent",
                "model": "other-model",
                "fallback": None,
            }
        )
        overridden = role_config.model_copy(
            update={"agent_name": "custom-role-agent", "fallback": fallback}
        )
        return loaded.model_copy(
            update={"agents": {**loaded.agents, repair_role: overridden}}
        )

    monkeypatch.setattr(supervisor, "load_dispatch_config", load_with_agent_names)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> _ProcessResult:
        del kwargs
        commands.append(list(command))
        if len(commands) == 1:
            # A fatal primary timeout makes the fallback launch too.
            return _ProcessResult(
                stdout="",
                stderr=f"{TIMEOUT_MARKER} 1 seconds",
                exit_code=124,
            )
        return _ProcessResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(agy_provider, "_run_antigravity_command", fake_run)
    config = supervisor.SupervisorConfig(
        repo_root=target,
        config_path=target / "dispatch_config.yaml",
        repair_role=repair_role,
        max_repair_attempts=1,
    )

    supervisor._run_repair_role(
        config,
        target / "failure-packet.json",
        lambda level, message: None,
    )

    assert len(commands) == 2
    for command in commands:
        assert command[command.index("--agent") + 1] == "supervisor-repair"
        assert "custom-role-agent" not in command
        assert "custom-fallback-agent" not in command


def test_supervisor_repair_agent_config_keeps_non_antigravity_agent_names() -> None:
    from llm_handoff.config import AgentConfig

    supervisor = _load_supervisor_module()
    claude = AgentConfig(provider="claude", agent_name="custom-claude-agent")

    assert supervisor._supervisor_repair_agent_config(claude) is claude

    mixed = AgentConfig(
        provider="claude",
        agent_name="custom-claude-agent",
        fallback=AgentConfig(provider="antigravity", agent_name="custom-agy-agent"),
    )
    repaired = supervisor._supervisor_repair_agent_config(mixed)

    assert repaired.agent_name == "custom-claude-agent"
    assert repaired.fallback is not None
    assert repaired.fallback.agent_name is None


def test_supervisor_main_parses_options_and_forwards_dispatch_args(
    tmp_path: Path,
    monkeypatch,
) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    captured: dict[str, Any] = {}

    def fake_run_supervised_dispatch(config: Any) -> int:
        captured["config"] = config
        return 7

    monkeypatch.setattr(
        supervisor, "run_supervised_dispatch", fake_run_supervised_dispatch
    )

    exit_code = supervisor.main(
        [
            "--repo-root",
            str(repo_root),
            "--repair-role",
            "validator",
            "--heartbeat-interval-seconds",
            "300",
            "--stalled-output-timeout-seconds",
            "900",
            "--notify-command",
            "echo supervisor",
            "--",
            "--dry-run",
            "--config",
            "dispatch_config.yaml",
        ]
    )

    assert exit_code == 7
    config = captured["config"]
    assert config.repo_root == repo_root.resolve()
    assert config.repair_role == "validator"
    assert config.max_repair_attempts == 1
    assert config.heartbeat_interval_seconds == 300
    assert config.stalled_output_timeout_seconds == 900
    assert config.notify_command == "echo supervisor"
    assert config.dispatch_args == ("--dry-run",)
    assert config.config_path == Path("dispatch_config.yaml")


def test_supervisor_main_forwards_child_help_arg(tmp_path: Path, monkeypatch) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    captured: dict[str, Any] = {}

    def fake_run_supervised_dispatch(config: Any) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr(
        supervisor, "run_supervised_dispatch", fake_run_supervised_dispatch
    )

    assert supervisor.main(["--repo-root", str(repo_root), "--", "--help"]) == 0
    assert captured["config"].dispatch_args == ("--help",)


def test_dispatch_supervised_wrappers_use_source_python_entrypoint() -> None:
    ps1_text = (Path("scripts") / "dispatch-supervised.ps1").read_text(encoding="utf-8")
    cmd_text = (Path("scripts") / "dispatch-supervised.cmd").read_text(encoding="utf-8")

    assert "-m llm_handoff.supervisor @args" in ps1_text
    assert "-m llm_handoff.supervisor %*" in cmd_text


def test_repairable_markers_match_real_orchestrator_log_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Build the marker corpus from the orchestrator's own log lines rather
    # than from hand-typed strings so the supervisor cannot drift away from
    # what the dispatcher actually emits.
    supervisor = _load_supervisor_module()
    orchestrator = importlib.import_module("llm_handoff.orchestrator")
    from llm_handoff.agent_types import DispatchResult
    from llm_handoff.config import DispatchConfig
    from llm_handoff.validator import ValidationResult

    repo_root = _write_repo(tmp_path)
    config = DispatchConfig(
        repo_root=repo_root,
        poll_interval_seconds=0,
        max_consecutive_failures=1,
    )
    lines: list[str] = []

    def log(level: str, message: str) -> None:
        lines.append(f"[{level}] {message}")

    failed_validation = ValidationResult(
        verdict="NO",
        warnings=[],
        errors=[
            "scope_claim_missing: Handoff is missing an **Agent:** ownership line."
        ],
        routing_instruction="auditor",
    )

    # Post-dispatch gate failure and the validation verdict line.
    orchestrator._log_validation(failed_validation, "backend", log)

    # Producer repair ran but violated a post-check (HEAD did not change).
    monkeypatch.setattr(orchestrator, "_git_head", lambda _repo_root: "a" * 40)
    monkeypatch.setattr(orchestrator, "_git_dirty_files", lambda _repo_root: ())
    monkeypatch.setattr(
        orchestrator,
        "_dispatch_handoff_hygiene_repair",
        lambda **_kwargs: DispatchResult(
            stdout="", stderr="", exit_code=0, elapsed_seconds=0.0
        ),
    )
    orchestrator._attempt_post_dispatch_handoff_hygiene_repair(
        config=config,
        validation_result=failed_validation,
        previous_agent="backend",
        log=log,
    )

    # No repair lane exists for the producer.
    monkeypatch.setattr(
        orchestrator,
        "_dispatch_handoff_hygiene_repair",
        lambda **_kwargs: None,
    )
    orchestrator._attempt_post_dispatch_handoff_hygiene_repair(
        config=config,
        validation_result=failed_validation,
        previous_agent="frontend",
        log=log,
    )

    # Consecutive failure budget exhausted.
    orchestrator._record_failure(config, config.max_consecutive_failures, log)

    for marker in supervisor._REPAIRABLE_ZERO_EXIT_MARKERS:
        matching = tuple(line for line in lines if marker in line)
        assert matching, f"marker {marker!r} never appears in orchestrator output"
        assert supervisor._is_repairable_dispatch_failure(
            supervisor.DispatchProcessResult(
                exit_code=0,
                output_lines=matching,
                elapsed_seconds=0.1,
            )
        )

    # A clean cycle must not trip the zero-exit repair path.
    lines.clear()
    orchestrator._log_validation(
        ValidationResult(
            verdict="YES", warnings=[], errors=[], routing_instruction="auditor"
        ),
        "backend",
        log,
    )
    assert not supervisor._is_repairable_dispatch_failure(
        supervisor.DispatchProcessResult(
            exit_code=0,
            output_lines=tuple(lines),
            elapsed_seconds=0.1,
        )
    )


FLAT_HANDOFF_CONTENT = """---
next_agent: planner
reason: Scope the next task.
producer: user
---

# Flat Handoff

Plan the next bounded task.
"""


def _write_flat_repo(tmp_path: Path) -> Path:
    # CONFIGURATION.md's supported flat layout: HANDOFF.md at the repo root and
    # no docs/handoff directory at all.
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    (repo_root / "HANDOFF.md").write_text(FLAT_HANDOFF_CONTENT, encoding="utf-8")
    (repo_root / "AGENTS.md").write_text("repo instructions\n", encoding="utf-8")
    (repo_root / "dispatch_config.yaml").write_text(
        "handoff_path: HANDOFF.md\n",
        encoding="utf-8",
    )
    return repo_root


def test_supervisor_honors_flat_handoff_path_from_dispatch_config(
    tmp_path: Path,
) -> None:
    from hashlib import sha256

    supervisor = _load_supervisor_module()
    repo_root = _write_flat_repo(tmp_path)
    flat_handoff = (repo_root / "HANDOFF.md").resolve()
    config = supervisor.SupervisorConfig(
        repo_root=repo_root,
        config_path=Path("dispatch_config.yaml"),
        doctor=True,
    )
    log_messages: list[tuple[str, str]] = []

    exit_code = supervisor.run_supervised_dispatch(
        config,
        log=lambda level, message: log_messages.append((level, message)),
        dispatch_runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("doctor mode must not dispatch"),
        ),
    )

    assert exit_code == 0
    assert ("INFO", f"Handoff path:       {flat_handoff}") in log_messages
    state = json.loads(config.run_state_path_full_path.read_text(encoding="utf-8"))
    assert state["status"] == "doctor_passed"
    assert state["route"] == "planner"
    assert state["handoff_sha"] == sha256(flat_handoff.read_bytes()).hexdigest()
    assert supervisor._current_route(config) == "planner"

    snapshot = supervisor._default_snapshot_reader(config)(repo_root)
    assert snapshot.handoff_sha == state["handoff_sha"]
    packet_path = supervisor._write_failure_packet(
        config,
        _dispatch_result(exit_code=1),
        fingerprint="f" * 40,
        snapshot=snapshot,
    )
    assert json.loads(packet_path.read_text(encoding="utf-8"))["handoff_path"] == (
        str(flat_handoff)
    )
    incident_path = supervisor._write_incident_bundle(
        config,
        _dispatch_result(exit_code=1),
        fingerprint="f" * 40,
        reason="test",
        snapshot=snapshot,
    )
    assert (incident_path / "handoff-current.md").read_text(encoding="utf-8") == (
        FLAT_HANDOFF_CONTENT
    )


def test_supervisor_preflight_reports_unloadable_dispatch_config(
    tmp_path: Path,
) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    (repo_root / "dispatch_config.yaml").write_text(
        "handoff_path: [\n",
        encoding="utf-8",
    )
    config = supervisor.SupervisorConfig(repo_root=repo_root, doctor=True)
    log_messages: list[tuple[str, str]] = []

    exit_code = supervisor.run_supervised_dispatch(
        config,
        log=lambda level, message: log_messages.append((level, message)),
        dispatch_runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("doctor mode must not dispatch"),
        ),
    )

    assert exit_code == 1
    state = json.loads(config.run_state_path_full_path.read_text(encoding="utf-8"))
    assert state["status"] == "doctor_failed"
    assert any(
        level == "ERROR"
        and message.startswith("Preflight error: dispatch config could not be loaded")
        for level, message in log_messages
    )


BLOCKED_PENDING_COMMIT_HANDOFF = """---
next_agent: user
reason: Backend cannot continue without user input.
producer: backend
status: blocked_missing_context
---

# backend Handback

The build tool named in the Work Packet is not installed, so the change
could not be verified. Edits are left uncommitted in the working tree.

## Verification Evidence

- **Commands run:** `make build`
- **Output summary:** command not found
- **Commit SHA verified:** pending-dispatcher-commit
- **Files changed or reviewed:** src/example.py
- **Unresolved concerns:** build tool missing
"""


def _run_doctor_on_blocked_pending_commit_handoff(
    tmp_path: Path,
    commit_mode_config: str,
) -> tuple[int, list[tuple[str, str]]]:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    (repo_root / "docs" / "handoff" / "HANDOFF.md").write_text(
        BLOCKED_PENDING_COMMIT_HANDOFF,
        encoding="utf-8",
    )
    (repo_root / "dispatch_config.yaml").write_text(
        commit_mode_config,
        encoding="utf-8",
    )
    config = supervisor.SupervisorConfig(repo_root=repo_root, doctor=True)
    log_messages: list[tuple[str, str]] = []

    exit_code = supervisor.run_supervised_dispatch(
        config,
        log=lambda level, message: log_messages.append((level, message)),
        dispatch_runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("doctor mode must not dispatch"),
        ),
    )
    return exit_code, log_messages


def test_supervisor_doctor_accepts_blocked_handback_pending_dispatcher_commit(
    tmp_path: Path,
) -> None:
    exit_code, log_messages = _run_doctor_on_blocked_pending_commit_handoff(
        tmp_path,
        "commit_mode: dispatcher\nenforce_work_packet: true\n",
    )

    assert exit_code == 0
    assert not any(
        level == "ERROR" and "sha_missing" in message
        for level, message in log_messages
    )
    assert any(
        level == "WARN" and message.startswith("Preflight warning: sha_pending")
        for level, message in log_messages
    )


def test_supervisor_doctor_requires_sha_for_pending_marker_in_agent_commit_mode(
    tmp_path: Path,
) -> None:
    exit_code, log_messages = _run_doctor_on_blocked_pending_commit_handoff(
        tmp_path,
        "commit_mode: agent\n",
    )

    assert exit_code == 1
    assert (
        "ERROR",
        "Preflight error: sha_missing: Handoff is missing a git commit SHA.",
    ) in log_messages
    assert not any("sha_pending" in message for _level, message in log_messages)


class _ExitedChild:
    # A child that produces no output and exits immediately; enough to see
    # which command line the supervisor built.
    def __init__(self) -> None:
        self.stdout = iter(())
        self.pid = 1
        self.returncode = 0

    def poll(self) -> int:
        return 0

    def wait(self) -> int:
        return 0

    def kill(self) -> None:
        return None


def test_supervisor_shares_repo_root_and_config_with_child_and_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    (repo_root / "alt.yaml").write_text(
        "agents:\n  validator:\n    provider: claude\n    binary: claude-alt\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        supervisor,
        "run_supervised_dispatch",
        lambda config: captured.update(config=config) or 0,
    )

    # --config after "--" used to reach only the child; the supervisor's
    # repair role then loaded the default config instead.
    assert (
        supervisor.main(
            ["--repo-root", str(repo_root), "--", "--dry-run", "--config=alt.yaml"]
        )
        == 0
    )
    config = captured["config"]
    assert config.repo_root == repo_root.resolve()
    assert config.config_path == Path("alt.yaml")
    assert config.dispatch_args == ("--dry-run",)

    popen_commands: list[list[str]] = []
    popen_options: list[dict[str, Any]] = []

    def fake_popen(command: list[str], **_kwargs: Any) -> _ExitedChild:
        popen_commands.append(list(command))
        popen_options.append(_kwargs)
        return _ExitedChild()

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        supervisor,
        "_repo_snapshot",
        lambda _repo_root, **_kwargs: supervisor.RepoSnapshot(
            head_sha=None, dirty_files=(), handoff_sha=None
        ),
    )

    result = supervisor._run_dispatch_child(config, lambda level, message: None)

    assert result.exit_code == 0
    assert popen_options[0]["cwd"] == config.repo_root
    assert popen_options[0]["env"] == supervisor._child_dispatch_env()
    assert popen_commands == [
        [
            sys.executable,
            "-P",
            "-m",
            "llm_handoff",
            "--repo-root",
            str(repo_root.resolve()),
            "--config",
            str(repo_root.resolve() / "alt.yaml"),
            "--dry-run",
        ]
    ]

    repair_calls: list[dict[str, Any]] = []

    def fake_invoke(subagent_name: str, prompt: str, **kwargs: Any) -> SubagentResult:
        del subagent_name, prompt
        repair_calls.append(kwargs)
        return _repair_result()

    monkeypatch.setattr(supervisor, "invoke_support_role", fake_invoke)
    repair_config = dataclasses.replace(
        config, repair_role="validator", max_repair_attempts=1
    )

    supervisor._run_repair_role(
        repair_config,
        repo_root / "failure-packet.json",
        lambda level, message: None,
    )

    assert len(repair_calls) == 1
    assert repair_calls[0]["agent_config"].binary == "claude-alt"


@pytest.mark.parametrize("existing", [None, "", "existing-import-root"])
def test_child_env_preserves_source_import_and_parent_environment(
    monkeypatch, existing: str | None,
) -> None:
    supervisor = _load_supervisor_module()
    if existing is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", existing)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DISPATCH_TEST_VALUE", "preserved")
    before = dict(os.environ)

    env = supervisor._child_dispatch_env()

    root = str(Path(supervisor.__file__).resolve().parent.parent)
    assert env["PYTHONPATH"] == root + (os.pathsep + existing if existing else "")
    assert env["OPENAI_API_KEY"] == "test-key"
    assert env["DISPATCH_TEST_VALUE"] == "preserved"
    assert os.environ == before
    assert env is not os.environ


@pytest.mark.parametrize("directory", ["site-packages", "dist-packages"])
@pytest.mark.parametrize("existing", [None, "existing-import-root"])
def test_child_env_does_not_inject_installed_package(
    tmp_path: Path, monkeypatch, directory: str, existing: str | None,
) -> None:
    supervisor = _load_supervisor_module()
    monkeypatch.setattr(
        supervisor.llm_handoff, "__file__",
        str(tmp_path / directory / "llm_handoff" / "__init__.py"),
    )
    if existing is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", existing)

    assert supervisor._child_dispatch_env() == dict(os.environ)


def test_child_import_ignores_target_decoy(tmp_path: Path, monkeypatch) -> None:
    supervisor = _load_supervisor_module()
    decoy = tmp_path / "llm_handoff"
    decoy.mkdir()
    (decoy / "__init__.py").write_text(
        "raise RuntimeError('target package imported')\n", encoding="ascii",
    )
    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = subprocess.run(
        [sys.executable, "-P", "-c",
         "import llm_handoff; "
         "print(llm_handoff.__file__ or "
         "next(iter(llm_handoff.__spec__.submodule_search_locations)))"],
        cwd=tmp_path, env=supervisor._child_dispatch_env(),
        capture_output=True, text=True, check=True,
    )
    resolved = Path(result.stdout.strip()).resolve()
    package_file = supervisor.llm_handoff.__file__
    expected = (Path(package_file) if package_file
                else Path(supervisor.__file__).parent)
    assert resolved == expected.resolve()
    assert not resolved.is_relative_to(tmp_path)


def test_supervisor_rejects_conflicting_shared_options(tmp_path: Path) -> None:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        supervisor._parse_args(
            [
                "--repo-root",
                str(repo_root),
                "--config",
                "a.yaml",
                "--",
                "--config",
                "b.yaml",
            ]
        )

    assert excinfo.value.code == 2


def _run_repair_scenario(
    tmp_path: Path,
    monkeypatch,
    *,
    before_dirty: tuple[str, ...],
    after_dirty: tuple[str, ...],
) -> tuple[int, Any, list[tuple[str, str]]]:
    supervisor = _load_supervisor_module()
    repo_root = _write_repo(tmp_path)
    config = supervisor.SupervisorConfig(
        repo_root=repo_root,
        repair_role="validator",
        max_repair_attempts=1,
    )
    dispatch_results = [
        supervisor.DispatchProcessResult(
            exit_code=1,
            output_lines=("[AGENT] Post-dispatch gate FAILED for backend.",),
            elapsed_seconds=0.1,
        ),
        _dispatch_result(),
    ]
    before = supervisor.RepoSnapshot(
        head_sha="a" * 40, dirty_files=before_dirty, handoff_sha="h1"
    )
    after = supervisor.RepoSnapshot(
        head_sha="b" * 40, dirty_files=after_dirty, handoff_sha="h2"
    )
    # snapshot_fn call order: loop start, before repair, after repair, then
    # loop start and success record on a restart.
    snapshots = [before, before, after, after, after]
    monkeypatch.setattr(
        supervisor,
        "_git_changed_files",
        lambda *_args: ("docs/handoff/HANDOFF.md",),
    )
    log_messages: list[tuple[str, str]] = []

    exit_code = supervisor.run_supervised_dispatch(
        config,
        log=lambda level, message: log_messages.append((level, message)),
        dispatch_runner=lambda _config, _log: dispatch_results.pop(0),
        repair_runner=lambda _config, _packet, _log: _repair_result(),
        snapshot_reader=lambda _repo_root: snapshots.pop(0),
    )
    return exit_code, config, log_messages


def _summary_records(config: Any) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in config.summary_path_full_path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def test_supervisor_stops_when_repair_leaves_dirty_files_outside_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    exit_code, config, log_messages = _run_repair_scenario(
        tmp_path,
        monkeypatch,
        before_dirty=(),
        after_dirty=("src/app.py",),
    )

    assert exit_code == 1
    assert any(
        level == "ERROR"
        and "uncommitted changes outside the allowed repair scope: src/app.py"
        in message
        for level, message in log_messages
    )
    stop_records = [
        record
        for record in _summary_records(config)
        if record["event"] == "dispatch_stop"
    ]
    assert [record["reason"] for record in stop_records] == [
        "repair_dirty_out_of_scope"
    ]
    incident_path = Path(stop_records[0]["incident_path"])
    assert (incident_path / "changed-files.txt").read_text(
        encoding="utf-8"
    ) == "docs/handoff/HANDOFF.md\nsrc/app.py\n"


def test_supervisor_tolerates_in_scope_and_runtime_state_dirty_files_after_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    exit_code, config, _log_messages = _run_repair_scenario(
        tmp_path,
        monkeypatch,
        before_dirty=(),
        after_dirty=(
            "docs/notes.md",
            "logs/dispatch/codex/session.json",
            "logs/dispatch/supervisor/failure-20260101T000000Z-abc.json",
        ),
    )

    assert exit_code == 0
    repair_records = [
        record
        for record in _summary_records(config)
        if record["event"] == "repair_attempt"
    ]
    assert [record["restart_outcome"] for record in repair_records] == ["restarted"]


def test_supervisor_does_not_blame_repair_for_pre_existing_dirty_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    exit_code, _config, _log_messages = _run_repair_scenario(
        tmp_path,
        monkeypatch,
        before_dirty=("src/legacy.py",),
        after_dirty=("src/legacy.py",),
    )

    assert exit_code == 0
