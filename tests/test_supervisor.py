from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

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
            output_lines=("[ERROR] Post-dispatch validation failed",),
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
        )
    ) == ("scripts/dispatch-other.ps1",)


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
    assert config.dispatch_args == ("--dry-run", "--config", "dispatch_config.yaml")


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
