from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Sequence
from uuid import uuid4

import yaml

import llm_handoff
from llm_handoff.agent_process import (
    _kill_process_tree,
    _process_group_popen_kwargs,
)
from llm_handoff.agent_roles import invoke_support_role
from llm_handoff.agent_types import SubagentResult
from llm_handoff.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_HANDOFF_PATH,
    AgentConfig,
    AgentRole,
    DispatchConfig,
    detect_repo_root,
    load_dispatch_config,
)
# The PATH lookup (with the Windows .cmd fallback) is shared with the
# standalone `llm-handoff doctor` command.
from llm_handoff.doctor import _command_available, _command_binary_name  # noqa: F401
from llm_handoff.provider_auth import AUTH_FAILURE_MARKERS, AUTH_FAILURE_PATTERNS
from llm_handoff.router import HandoffFrontmatterError, parse_handoff_frontmatter_text
from llm_handoff.router import route as route_handoff
from llm_handoff.text_io import read_dispatch_text
from llm_handoff.validator import validate_handoff_text


LogFn = Callable[[str, str], None]
DispatchRunner = Callable[["SupervisorConfig", LogFn], "DispatchProcessResult"]
RepairRunner = Callable[["SupervisorConfig", Path, LogFn], SubagentResult]
SnapshotReader = Callable[[Path], "RepoSnapshot"]

DEFAULT_SUPERVISOR_DIR = Path("logs") / "dispatch" / "supervisor"
DEFAULT_SUPERVISOR_SUMMARY_PATH = DEFAULT_SUPERVISOR_DIR / "supervisor-summary.jsonl"
DEFAULT_SUPERVISOR_LOCK_PATH = DEFAULT_SUPERVISOR_DIR / "dispatch-supervised.lock"
DEFAULT_SUPERVISOR_RUN_STATE_PATH = DEFAULT_SUPERVISOR_DIR / "current-run.json"
DEFAULT_SUPERVISOR_INCIDENT_DIRECTORY = DEFAULT_SUPERVISOR_DIR / "incidents"
DEFAULT_TAIL_LINES = 60
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300
DEFAULT_STALLED_OUTPUT_TIMEOUT_SECONDS = 1_800
DEFAULT_MAX_TRANSIENT_RETRIES = 1
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TIMESTAMP_RE = re.compile(r"^\[[0-9T:\-]+\]\s+")
_CYCLE_RE = re.compile(r"---\s+Cycle\s+([0-9]+)\s+---")
_ROUTE_RE = re.compile(r"Routing instruction:\s*(.+?)\s*$")
# Each marker must be a substring of a line the orchestrator really logs;
# tests/test_supervisor.py builds these messages from the orchestrator's own
# log output so the two cannot drift apart again.
_REPAIRABLE_ZERO_EXIT_MARKERS = (
    "Post-dispatch gate FAILED",
    "Handoff validation verdict: NO",
    "Producer repair failed",
    "No supported producer repair path",
    "Hit maximum consecutive failures",
)
# Shared with the orchestrator, which stops on the same sign-in failures.
_AUTH_FAILURE_MARKERS = AUTH_FAILURE_MARKERS
_AUTH_FAILURE_PATTERNS = AUTH_FAILURE_PATTERNS
_TRANSIENT_FAILURE_MARKERS = (
    "session not found",
    "deadline_exceeded",
    "rate limit",
    "temporarily unavailable",
    "connection reset",
    "network error",
    "network unreachable",
    "retry later",
)
_DEFAULT_ALLOWED_REPAIR_DIRECTORIES = (
    "docs/",
    ".claude/agents/",
    ".codex/skills/llm-handoff/",
    ".agents/agents/",
)
_DEFAULT_ALLOWED_REPAIR_FILES: tuple[str, ...] = ()
_REPAIR_ROLE_CHOICES = (
    "planner",
    "backend",
    "frontend",
    "auditor",
    "validator",
    "finalizer",
)


@dataclass(frozen=True)
class SupervisorConfig:
    repo_root: Path
    dispatch_args: tuple[str, ...] = ()
    config_path: Path | None = None
    repair_role: AgentRole | None = None
    max_repair_attempts: int = 0
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES
    tail_lines: int = DEFAULT_TAIL_LINES
    failure_directory: Path = DEFAULT_SUPERVISOR_DIR
    summary_path: Path = DEFAULT_SUPERVISOR_SUMMARY_PATH
    lock_path: Path = DEFAULT_SUPERVISOR_LOCK_PATH
    run_state_path: Path = DEFAULT_SUPERVISOR_RUN_STATE_PATH
    incident_directory: Path = DEFAULT_SUPERVISOR_INCIDENT_DIRECTORY
    stalled_output_timeout_seconds: int = DEFAULT_STALLED_OUTPUT_TIMEOUT_SECONDS
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    notify_bell: bool = False
    notify_command: str | None = None
    doctor: bool = False
    allowed_repair_paths: tuple[str, ...] = ()

    @property
    def failure_directory_full_path(self) -> Path:
        return _resolve_config_path(self.repo_root, self.failure_directory)

    @property
    def summary_path_full_path(self) -> Path:
        return _resolve_config_path(self.repo_root, self.summary_path)

    @property
    def lock_path_full_path(self) -> Path:
        return _resolve_config_path(self.repo_root, self.lock_path)

    @property
    def run_state_path_full_path(self) -> Path:
        return _resolve_config_path(self.repo_root, self.run_state_path)

    @property
    def incident_directory_full_path(self) -> Path:
        return _resolve_config_path(self.repo_root, self.incident_directory)

    @property
    def config_path_full_path(self) -> Path | None:
        if self.config_path is None:
            return None
        return _resolve_config_path(self.repo_root, self.config_path)


@dataclass(frozen=True)
class DispatchProcessResult:
    exit_code: int
    output_lines: tuple[str, ...]
    elapsed_seconds: float


@dataclass(frozen=True)
class RepoSnapshot:
    head_sha: str | None
    dirty_files: tuple[str, ...]
    handoff_sha: str | None


@dataclass(frozen=True)
class PreflightResult:
    verdict: str
    route: str | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    snapshot: RepoSnapshot


@dataclass(frozen=True)
class FailurePolicy:
    action: str
    reason: str


@dataclass
class HeartbeatState:
    cycle: str | None = None
    route: str | None = None


@dataclass(frozen=True)
class SupervisorLock:
    path: Path
    token: str


class SupervisorAlreadyRunning(RuntimeError):
    def __init__(self, lock_path: Path, pid: int | None) -> None:
        pid_text = f"pid {pid}" if pid is not None else "unknown pid"
        super().__init__(
            "Another llm-handoff supervisor is already running "
            f"({pid_text}; lock {lock_path}). Stop that process first, or "
            "delete the lock only after confirming the process is gone."
        )
        self.lock_path = lock_path
        self.pid = pid


def run_supervised_dispatch(
    config: SupervisorConfig,
    *,
    log: LogFn | None = None,
    dispatch_runner: DispatchRunner | None = None,
    repair_runner: RepairRunner | None = None,
    snapshot_reader: SnapshotReader | None = None,
) -> int:
    log_fn = log or _default_log
    _log_supervisor_startup(config, log_fn)
    if config.doctor:
        return _run_doctor(config, log_fn)
    try:
        with _supervisor_lock(config):
            log_fn("INFO", "Supervisor lock acquired.")
            preflight = _run_preflight(config, strict_handoff=False)
            _log_preflight(preflight, log_fn)
            _write_run_state(
                config,
                status="preflight_failed" if preflight.errors else "running",
                route=preflight.route,
                snapshot=preflight.snapshot,
                repair_attempts=0,
                last_event="preflight",
            )
            if preflight.errors:
                _append_summary_record(
                    config,
                    {
                        "event": "preflight_failed",
                        "errors": list(preflight.errors),
                        "warnings": list(preflight.warnings),
                        "route": preflight.route,
                    },
                )
                _notify(
                    config,
                    event="preflight_failed",
                    message="llm-handoff supervised preflight failed",
                    log=log_fn,
                )
                return 1
            return _run_supervised_dispatch_locked(
                config,
                log_fn,
                dispatch_runner=dispatch_runner,
                repair_runner=repair_runner,
                snapshot_reader=snapshot_reader,
            )
    except SupervisorAlreadyRunning as error:
        log_fn("ERROR", str(error))
        return 1


def _run_supervised_dispatch_locked(
    config: SupervisorConfig,
    log_fn: LogFn,
    *,
    dispatch_runner: DispatchRunner | None = None,
    repair_runner: RepairRunner | None = None,
    snapshot_reader: SnapshotReader | None = None,
) -> int:
    dispatch_fn = dispatch_runner or _run_dispatch_child
    repair_fn = repair_runner or _run_repair_role
    snapshot_fn = snapshot_reader or _default_snapshot_reader(config)
    seen_fingerprints: set[str] = set()
    repair_attempts = 0
    transient_retries = 0

    while True:
        current_snapshot = snapshot_fn(config.repo_root)
        current_route = _current_route(config)
        _write_run_state(
            config,
            status="dispatching",
            route=current_route,
            snapshot=current_snapshot,
            repair_attempts=repair_attempts,
            last_event="dispatch_start",
        )
        result = dispatch_fn(config, log_fn)
        policy = _failure_policy(result)
        if policy.action == "success":
            _append_summary_record(
                config,
                {
                    "event": "dispatch_exit",
                    "exit_code": result.exit_code,
                    "repair_attempts": repair_attempts,
                    "restart_outcome": "completed_without_repair"
                    if repair_attempts == 0
                    else "completed_after_repair",
                },
            )
            _write_run_state(
                config,
                status="completed",
                route=_current_route(config),
                snapshot=snapshot_fn(config.repo_root),
                repair_attempts=repair_attempts,
                last_event="dispatch_exit",
            )
            return result.exit_code

        fingerprint = _failure_fingerprint(result)
        if policy.action == "retry":
            if transient_retries < config.max_transient_retries:
                transient_retries += 1
                log_fn(
                    "WARN",
                    "Transient dispatch failure detected; retrying without repair "
                    f"({policy.reason}).",
                )
                _append_summary_record(
                    config,
                    {
                        "event": "transient_retry",
                        "reason": policy.reason,
                        "failure_fingerprint": fingerprint,
                        "retry": transient_retries,
                        "exit_code": result.exit_code,
                    },
                )
                _write_run_state(
                    config,
                    status="retrying",
                    route=_current_route(config),
                    snapshot=snapshot_fn(config.repo_root),
                    repair_attempts=repair_attempts,
                    last_event="transient_retry",
                )
                continue
            policy = FailurePolicy(
                action="stop",
                reason="transient_retry_budget_exhausted",
            )

        if policy.action == "stop":
            return _stop_with_incident(
                config,
                log_fn,
                result,
                fingerprint=fingerprint,
                reason=policy.reason,
                snapshot=snapshot_fn(config.repo_root),
                repair_attempts=repair_attempts,
            )

        if fingerprint in seen_fingerprints:
            return _stop_with_incident(
                config,
                log_fn,
                result,
                fingerprint=fingerprint,
                reason="repeated_failure_fingerprint",
                snapshot=snapshot_fn(config.repo_root),
                repair_attempts=repair_attempts,
            )
        seen_fingerprints.add(fingerprint)

        if repair_attempts >= config.max_repair_attempts or config.repair_role is None:
            return _stop_with_incident(
                config,
                log_fn,
                result,
                fingerprint=fingerprint,
                reason="repair_unavailable_or_exhausted",
                snapshot=snapshot_fn(config.repo_root),
                repair_attempts=repair_attempts,
            )

        before_snapshot = snapshot_fn(config.repo_root)
        failure_packet_path = _write_failure_packet(
            config,
            result,
            fingerprint=fingerprint,
            snapshot=before_snapshot,
        )
        repair_attempts += 1
        log_fn(
            "WARN",
            f"Repairable dispatch failure detected; invoking {config.repair_role}.",
        )
        log_fn("INFO", f"Failure packet: {failure_packet_path}")
        _write_run_state(
            config,
            status="repairing",
            route=_current_route(config),
            snapshot=before_snapshot,
            repair_attempts=repair_attempts,
            last_event="repair_start",
            failure_packet_path=failure_packet_path,
        )
        repair_result = repair_fn(config, failure_packet_path, log_fn)
        if repair_result.exit_code != 0:
            return _stop_with_incident(
                config,
                log_fn,
                result,
                fingerprint=fingerprint,
                reason="repair_failed",
                snapshot=snapshot_fn(config.repo_root),
                repair_attempts=repair_attempts,
                failure_packet_path=failure_packet_path,
            )

        after_snapshot = snapshot_fn(config.repo_root)
        if after_snapshot.head_sha == before_snapshot.head_sha:
            return _stop_with_incident(
                config,
                log_fn,
                result,
                fingerprint=fingerprint,
                reason="repair_no_head_change",
                snapshot=after_snapshot,
                repair_attempts=repair_attempts,
                failure_packet_path=failure_packet_path,
            )

        changed_files = _git_changed_files(
            config.repo_root,
            before_snapshot.head_sha,
            after_snapshot.head_sha,
        )
        out_of_scope_files = _out_of_scope_repair_files(
            changed_files,
            allowed_repair_paths=config.allowed_repair_paths,
        )
        if out_of_scope_files:
            return _stop_with_incident(
                config,
                log_fn,
                result,
                fingerprint=fingerprint,
                reason="repair_out_of_scope",
                snapshot=after_snapshot,
                repair_attempts=repair_attempts,
                failure_packet_path=failure_packet_path,
                changed_files=changed_files,
            )

        dirty_out_of_scope_files = _repair_dirty_files_out_of_scope(
            config,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        if dirty_out_of_scope_files:
            log_fn(
                "ERROR",
                "Repair role left uncommitted changes outside the allowed repair "
                f"scope: {', '.join(dirty_out_of_scope_files)}",
            )
            return _stop_with_incident(
                config,
                log_fn,
                result,
                fingerprint=fingerprint,
                reason="repair_dirty_out_of_scope",
                snapshot=after_snapshot,
                repair_attempts=repair_attempts,
                failure_packet_path=failure_packet_path,
                changed_files=_dedupe((*changed_files, *dirty_out_of_scope_files)),
            )

        _append_repair_summary_record(
            config,
            attempt=repair_attempts,
            failure_packet_path=failure_packet_path,
            fingerprint=fingerprint,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            repair_exit_code=repair_result.exit_code,
            restart_outcome="restarted",
            changed_files=changed_files,
        )
        log_fn("INFO", "Repair role changed HEAD; restarting dispatcher.")
        _notify(
            config,
            event="repair_restarted",
            message="llm-handoff supervisor repair committed and dispatcher is restarting",
            log=log_fn,
        )


_RUNTIME_STATE_DIRECTORY = "logs/dispatch/"


def _repair_dirty_files_out_of_scope(
    config: SupervisorConfig,
    *,
    before_snapshot: RepoSnapshot,
    after_snapshot: RepoSnapshot,
) -> tuple[str, ...]:
    """Uncommitted paths a repair introduced outside the allowed surface.

    The committed diff is checked separately; this catches edits the repair
    role made but never committed, which would otherwise ride into the
    restarted dispatcher. Dispatcher runtime state under logs/dispatch/
    (supervisor packets, provider session files, bounce counts) and paths
    that were already dirty before the repair are not blamed on it.
    """
    previously_dirty = set(before_snapshot.dirty_files)
    new_dirty_files = tuple(
        path
        for path in after_snapshot.dirty_files
        if path not in previously_dirty and not _is_runtime_state_path(path, config)
    )
    return _out_of_scope_repair_files(
        new_dirty_files,
        allowed_repair_paths=config.allowed_repair_paths,
    )


def _is_runtime_state_path(path: str, config: SupervisorConfig) -> bool:
    prefixes = [_RUNTIME_STATE_DIRECTORY]
    for directory in (
        config.failure_directory,
        config.incident_directory,
        config.summary_path.parent,
        config.run_state_path.parent,
        config.lock_path.parent,
    ):
        if not directory.is_absolute():
            prefixes.append(f"{directory.as_posix().rstrip('/')}/")
    return any(path.startswith(prefix) for prefix in prefixes)


def _stop_with_incident(
    config: SupervisorConfig,
    log: LogFn,
    result: DispatchProcessResult,
    *,
    fingerprint: str,
    reason: str,
    snapshot: RepoSnapshot,
    repair_attempts: int,
    failure_packet_path: Path | None = None,
    changed_files: tuple[str, ...] = (),
) -> int:
    log("ERROR", f"Supervised dispatch stopped: {reason}.")
    incident_path = _write_incident_bundle(
        config,
        result,
        fingerprint=fingerprint,
        reason=reason,
        snapshot=snapshot,
        failure_packet_path=failure_packet_path,
        changed_files=changed_files,
    )
    _append_summary_record(
        config,
        {
            "event": "dispatch_stop",
            "reason": reason,
            "exit_code": result.exit_code,
            "failure_fingerprint": fingerprint,
            "incident_path": str(incident_path),
            "repair_attempts": repair_attempts,
        },
    )
    _write_run_state(
        config,
        status="stopped",
        route=_current_route(config),
        snapshot=snapshot,
        repair_attempts=repair_attempts,
        last_event=reason,
        incident_path=incident_path,
    )
    _notify(
        config,
        event=reason,
        message=f"llm-handoff supervised dispatch stopped: {reason}",
        log=log,
    )
    return result.exit_code or 1


def _child_dispatch_env() -> dict[str, str]:
    env = os.environ.copy()
    # The source tree currently uses a namespace package with no __file__.
    # Anchor that case to this loaded module, not another namespace portion.
    package_file = llm_handoff.__file__ or __file__
    package_parent = Path(package_file).resolve().parent.parent
    # Installed packages are already discoverable without cwd on sys.path.
    # Source checkouts (including editable installs) need their import root.
    if package_parent.name.lower() not in {"site-packages", "dist-packages"}:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(package_parent)
        if existing:
            env["PYTHONPATH"] += os.pathsep + existing
    return env


def _run_dispatch_child(config: SupervisorConfig, log: LogFn) -> DispatchProcessResult:
    command = [
        sys.executable,
        "-P",
        "-m",
        "llm_handoff",
        *_child_dispatch_options(config),
        *config.dispatch_args,
    ]
    log("INFO", f"Starting supervised dispatch child: {_format_command(command)}")
    start_time = time.monotonic()
    output_tail: deque[str] = deque(maxlen=config.tail_lines)
    process = subprocess.Popen(
        command,
        cwd=config.repo_root,
        env=_child_dispatch_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_process_group_popen_kwargs(),
    )
    assert process.stdout is not None
    _write_run_state(
        config,
        status="child_running",
        route=_current_route(config),
        snapshot=_repo_snapshot(
            config.repo_root,
            handoff_path=_dispatch_handoff_path(config),
        ),
        repair_attempts=0,
        last_event="child_started",
        child_pid=process.pid,
    )
    output_queue: queue.Queue[str | None] = queue.Queue()
    reader_thread = threading.Thread(
        target=_enqueue_process_output,
        args=(process.stdout, output_queue),
        daemon=True,
    )
    reader_thread.start()
    last_output_at = start_time
    last_heartbeat_at = start_time
    heartbeat_state = HeartbeatState()
    stalled = False
    while True:
        try:
            raw_line = output_queue.get(timeout=1)
        except queue.Empty:
            now = time.monotonic()
            if (
                config.stalled_output_timeout_seconds > 0
                and now - last_output_at >= config.stalled_output_timeout_seconds
            ):
                stalled = True
                stall_line = (
                    "Supervisor stalled-output timeout reached after "
                    f"{config.stalled_output_timeout_seconds} seconds."
                )
                output_tail.append(stall_line)
                log("ERROR", stall_line)
                # The child dispatcher owns provider CLI grandchildren; a
                # plain kill() would orphan them mid-edit.
                _kill_process_tree(process)
                break
            if _should_emit_heartbeat(
                now=now,
                last_heartbeat_at=last_heartbeat_at,
                heartbeat_interval_seconds=config.heartbeat_interval_seconds,
            ):
                log(
                    "HEARTBEAT",
                    _format_heartbeat(
                        start_time=start_time,
                        now=now,
                        last_output_at=last_output_at,
                        state=heartbeat_state,
                    ),
                )
                last_heartbeat_at = now
            continue
        if raw_line is None:
            break
        now = time.monotonic()
        _write_child_output_line(raw_line)
        output_tail.append(raw_line.rstrip("\r\n"))
        last_output_at = now
        boundary = _record_child_boundary(heartbeat_state, raw_line)
        if boundary is not None:
            log(
                "HEARTBEAT",
                _format_heartbeat(
                    start_time=start_time,
                    now=now,
                    last_output_at=last_output_at,
                    state=heartbeat_state,
                    boundary=boundary,
                ),
            )
            last_heartbeat_at = now
    exit_code = process.wait()
    elapsed_seconds = time.monotonic() - start_time
    log("INFO", f"Dispatch child exited with code {exit_code}.")
    return DispatchProcessResult(
        exit_code=124 if stalled else exit_code,
        output_lines=tuple(output_tail),
        elapsed_seconds=elapsed_seconds,
    )


def _run_repair_role(
    config: SupervisorConfig,
    failure_packet_path: Path,
    log: LogFn,
) -> SubagentResult:
    if config.repair_role is None:
        return SubagentResult(
            stdout="", stderr="repair role disabled", exit_code=1, elapsed_seconds=0.0
        )
    dispatch_config = load_dispatch_config(
        repo_root=config.repo_root,
        config_path=config.config_path_full_path,
        dry_run=False,
    )
    agent_config = _supervisor_repair_agent_config(
        dispatch_config.agents[config.repair_role]
    )
    prompt = _build_supervisor_repair_prompt(
        config,
        failure_packet_path,
    )
    return invoke_support_role(
        "supervisor-repair",
        prompt,
        role=config.repair_role,
        handoff_path=dispatch_config.handoff_full_path,
        agent_config=agent_config,
        log=log,
        repo_root=dispatch_config.repo_root,
    )


def _supervisor_repair_agent_config(agent_config: AgentConfig) -> AgentConfig:
    """Drop Antigravity `agent_name` overrides for a supervised repair launch.

    An Antigravity launch resolves `--agent` as the role's `agent_name` before
    the helper name, so a role override would load the role's normal agent
    file instead of `.agents/agents/supervisor-repair.md`. Clearing the
    override on the primary and on an Antigravity fallback forces
    `--agent supervisor-repair`. Other providers keep their config as written.
    """

    fallback = agent_config.fallback
    if (
        fallback is not None
        and fallback.provider == "antigravity"
        and fallback.agent_name is not None
    ):
        fallback = fallback.model_copy(update={"agent_name": None})
    update: dict[str, object] = {}
    if fallback is not agent_config.fallback:
        update["fallback"] = fallback
    if agent_config.provider == "antigravity" and agent_config.agent_name is not None:
        update["agent_name"] = None
    if not update:
        return agent_config
    return agent_config.model_copy(update=update)


def _build_supervisor_repair_prompt(
    config: SupervisorConfig,
    failure_packet_path: Path,
) -> str:
    allowed_paths = ", ".join(_allowed_repair_paths(config.allowed_repair_paths))
    return (
        "This is llm-handoff supervised dispatcher repair, not normal product "
        "implementation. Read the failure packet at "
        f"{failure_packet_path}. Repair only dispatcher, handoff-protocol, "
        "wrapper, config, docs, or tests needed to make the supervised dispatch "
        "path continue safely. Do not push. Commit the repair before returning. "
        "If repair is unsafe, make no code changes and explain the blocker. "
        f"Allowed paths: {allowed_paths}."
    )


def _run_doctor(config: SupervisorConfig, log: LogFn) -> int:
    preflight = _run_preflight(config, check_lock=True, strict_handoff=True)
    _log_preflight(preflight, log)
    status = "doctor_failed" if preflight.errors else "doctor_passed"
    _write_run_state(
        config,
        status=status,
        route=preflight.route,
        snapshot=preflight.snapshot,
        repair_attempts=0,
        last_event="doctor",
    )
    _append_summary_record(
        config,
        {
            "event": "doctor",
            "verdict": preflight.verdict,
            "route": preflight.route,
            "errors": list(preflight.errors),
            "warnings": list(preflight.warnings),
        },
    )
    log("INFO", "Doctor mode complete; dispatch was not started.")
    return 1 if preflight.errors else 0


def _run_preflight(
    config: SupervisorConfig,
    *,
    check_lock: bool = False,
    strict_handoff: bool = True,
) -> PreflightResult:
    errors: list[str] = []
    warnings: list[str] = []
    route: str | None = None
    agents_path = config.repo_root / "AGENTS.md"
    # The child dispatcher reads handoff_path from dispatch_config.yaml, so
    # preflight must check the same file (for example a flat HANDOFF.md).
    # Validation must also know the child's commit mode, so a blocked handback
    # awaiting a dispatcher commit is not reported as missing its SHA.
    try:
        dispatch_config = _load_supervised_dispatch_config(config)
    except _DISPATCH_CONFIG_ERRORS as exc:
        errors.append(f"dispatch config could not be loaded: {exc}")
        handoff_path = (config.repo_root / DEFAULT_HANDOFF_PATH).resolve()
        dispatcher_commit_mode = False
        tracker_provider = "none"
    else:
        handoff_path = dispatch_config.handoff_full_path
        dispatcher_commit_mode = dispatch_config.commit_mode == "dispatcher"
        tracker_provider = dispatch_config.tracker.provider
    snapshot = _repo_snapshot(config.repo_root, handoff_path=handoff_path)

    if not agents_path.exists():
        warnings.append(f"missing AGENTS.md at {agents_path}")
    if not handoff_path.exists():
        errors.append(f"missing HANDOFF.md at {handoff_path}")
        return PreflightResult(
            verdict="FAIL",
            route=None,
            errors=tuple(errors),
            warnings=tuple(warnings),
            snapshot=snapshot,
        )

    try:
        handoff_content = read_dispatch_text(handoff_path)
    except OSError as exc:
        errors.append(f"could not read HANDOFF.md: {exc}")
        handoff_content = ""

    if handoff_content:
        decision = route_handoff(handoff_content)
        if decision.route == "unknown":
            _add_handoff_preflight_issue(
                errors,
                warnings,
                "router could not determine a dispatchable route",
                strict_handoff=strict_handoff,
            )
        else:
            route = decision.route
        warnings.extend(decision.warnings)
        try:
            frontmatter = parse_handoff_frontmatter_text(handoff_content)
        except HandoffFrontmatterError as exc:
            _add_handoff_preflight_issue(
                errors,
                warnings,
                f"handoff frontmatter parse failed: {exc}",
                strict_handoff=strict_handoff,
            )
            frontmatter = None
        if frontmatter is not None:
            validation = validate_handoff_text(
                handoff_content,
                frontmatter.producer or "unknown producer",
                git_cwd=config.repo_root,
                dispatcher_commit_mode=dispatcher_commit_mode,
                tracker_provider=tracker_provider,
            )
            for error in validation.errors:
                _add_handoff_preflight_issue(
                    errors,
                    warnings,
                    error,
                    strict_handoff=strict_handoff,
                )
            warnings.extend(validation.warnings)

    if check_lock:
        lock_payload = _read_json_file(config.lock_path_full_path)
        lock_pid = _extract_lock_pid(lock_payload)
        if lock_pid is not None and _is_process_running(lock_pid):
            errors.append(
                f"another llm-handoff supervisor appears to be running (pid {lock_pid})"
            )
        elif config.lock_path_full_path.exists():
            warnings.append(
                f"stale supervisor lock exists at {config.lock_path_full_path}"
            )

    for command_name in ("git",):
        if not _command_available(command_name):
            warnings.append(f"tool unavailable on PATH: {command_name}")

    return PreflightResult(
        verdict="FAIL" if errors else "PASS",
        route=route,
        errors=tuple(_dedupe(errors)),
        warnings=tuple(_dedupe(warnings)),
        snapshot=snapshot,
    )


def _add_handoff_preflight_issue(
    errors: list[str],
    warnings: list[str],
    issue: str,
    *,
    strict_handoff: bool,
) -> None:
    if strict_handoff:
        errors.append(issue)
    else:
        warnings.append(f"handoff_preflight_repairable: {issue}")


def _log_preflight(preflight: PreflightResult, log: LogFn) -> None:
    log("INFO", f"Preflight verdict: {preflight.verdict}")
    log("INFO", f"Preflight route:   {preflight.route or 'unknown'}")
    if preflight.snapshot.head_sha:
        log("INFO", f"Preflight HEAD:    {preflight.snapshot.head_sha}")
    log("INFO", f"Preflight dirty:   {len(preflight.snapshot.dirty_files)} file(s)")
    for warning in preflight.warnings:
        log("WARN", f"Preflight warning: {warning}")
    for error in preflight.errors:
        log("ERROR", f"Preflight error: {error}")


_DISPATCH_CONFIG_ERRORS = (OSError, ValueError, yaml.YAMLError)


def _load_supervised_dispatch_config(config: SupervisorConfig) -> DispatchConfig:
    return load_dispatch_config(
        repo_root=config.repo_root,
        config_path=config.config_path_full_path,
    )


def _load_dispatch_handoff_path(config: SupervisorConfig) -> Path:
    return _load_supervised_dispatch_config(config).handoff_full_path


def _dispatch_handoff_path(config: SupervisorConfig) -> Path:
    """Return the handoff file the child dispatcher will use.

    Falls back to the default layout when dispatch_config.yaml cannot be
    loaded; preflight reports that failure as an error separately.
    """
    try:
        return _load_dispatch_handoff_path(config)
    except _DISPATCH_CONFIG_ERRORS:
        return (config.repo_root / DEFAULT_HANDOFF_PATH).resolve()


def _default_snapshot_reader(config: SupervisorConfig) -> SnapshotReader:
    handoff_path = _dispatch_handoff_path(config)
    return lambda repo_root: _repo_snapshot(repo_root, handoff_path=handoff_path)


def _current_route(config: SupervisorConfig) -> str | None:
    try:
        content = read_dispatch_text(_dispatch_handoff_path(config))
    except OSError:
        return None
    decision = route_handoff(content)
    return None if decision.route == "unknown" else decision.route


def _write_run_state(
    config: SupervisorConfig,
    *,
    status: str,
    route: str | None,
    snapshot: RepoSnapshot,
    repair_attempts: int,
    last_event: str,
    child_pid: int | None = None,
    failure_packet_path: Path | None = None,
    incident_path: Path | None = None,
) -> None:
    state_path = config.run_state_path_full_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "last_event": last_event,
        "repo_root": str(config.repo_root),
        "dispatch_args": list(config.dispatch_args),
        "route": route,
        "head_sha": snapshot.head_sha,
        "handoff_sha": snapshot.handoff_sha,
        "dirty_files": list(snapshot.dirty_files),
        "repair_role": config.repair_role,
        "repair_attempts": repair_attempts,
        "child_pid": child_pid,
        "failure_packet_path": str(failure_packet_path)
        if failure_packet_path
        else None,
        "incident_path": str(incident_path) if incident_path else None,
    }
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_incident_bundle(
    config: SupervisorConfig,
    result: DispatchProcessResult,
    *,
    fingerprint: str,
    reason: str,
    snapshot: RepoSnapshot,
    failure_packet_path: Path | None = None,
    changed_files: tuple[str, ...] = (),
) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    incident_path = (
        config.incident_directory_full_path
        / f"incident-{timestamp}-{fingerprint[:12]}-{uuid4().hex[:8]}"
    )
    incident_path.mkdir(parents=True, exist_ok=True)
    summary = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "fingerprint": fingerprint,
        "repo_root": str(config.repo_root),
        "dispatch_args": list(config.dispatch_args),
        "repair_role": config.repair_role,
        "exit_code": result.exit_code,
        "elapsed_seconds": result.elapsed_seconds,
        "head_sha": snapshot.head_sha,
        "handoff_sha": snapshot.handoff_sha,
        "dirty_files": list(snapshot.dirty_files),
        "changed_files": list(changed_files),
        "failure_packet_path": str(failure_packet_path)
        if failure_packet_path
        else None,
    }
    (incident_path / "summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    (incident_path / "output-tail.log").write_text(
        "\n".join(result.output_lines) + ("\n" if result.output_lines else ""),
        encoding="utf-8",
    )
    handoff_path = _dispatch_handoff_path(config)
    if handoff_path.exists():
        (incident_path / "handoff-current.md").write_text(
            handoff_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    git_status = _git_stdout(
        config.repo_root,
        ["git", "status", "--short", "--untracked-files=all"],
    )
    (incident_path / "git-status.txt").write_text(
        git_status if git_status is not None else "(git status unavailable)",
        encoding="utf-8",
    )
    if failure_packet_path is not None and failure_packet_path.exists():
        (incident_path / "failure-packet.json").write_text(
            failure_packet_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    if changed_files:
        (incident_path / "changed-files.txt").write_text(
            "\n".join(changed_files) + "\n",
            encoding="utf-8",
        )
    return incident_path


def _failure_policy(result: DispatchProcessResult) -> FailurePolicy:
    if not _is_repairable_dispatch_failure(result):
        return FailurePolicy(action="success", reason="dispatch_completed")
    joined_output = "\n".join(result.output_lines)
    if result.exit_code == 124 or "stalled-output timeout" in joined_output:
        return FailurePolicy(action="stop", reason="stalled_output_timeout")
    if _contains_marker(joined_output, _AUTH_FAILURE_MARKERS) or _contains_pattern(
        joined_output,
        _AUTH_FAILURE_PATTERNS,
    ):
        return FailurePolicy(action="stop", reason="auth_failure")
    if _contains_marker(joined_output, _TRANSIENT_FAILURE_MARKERS):
        return FailurePolicy(action="retry", reason="transient_failure")
    return FailurePolicy(action="repair", reason="repairable_dispatch_failure")


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    normalized = text.lower()
    return any(marker.lower() in normalized for marker in markers)


def _contains_pattern(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _enqueue_process_output(
    output_stream,
    output_queue: queue.Queue[str | None],
) -> None:
    try:
        for raw_line in output_stream:
            output_queue.put(raw_line)
    finally:
        output_queue.put(None)


def _child_boundary_from_line(raw_line: str) -> str | None:
    normalized = _normalize_log_line(raw_line)
    if cycle_match := _CYCLE_RE.search(normalized):
        return f"cycle={cycle_match.group(1)}"
    if route_match := _ROUTE_RE.search(normalized):
        return f"route={route_match.group(1).strip()}"
    return None


def _record_child_boundary(state: HeartbeatState, raw_line: str) -> str | None:
    boundary = _child_boundary_from_line(raw_line)
    if boundary is None:
        return None
    key, value = boundary.split("=", 1)
    if getattr(state, key) == value:
        return None
    setattr(state, key, value)
    return boundary


def _should_emit_heartbeat(
    *,
    now: float,
    last_heartbeat_at: float,
    heartbeat_interval_seconds: int,
) -> bool:
    return (
        heartbeat_interval_seconds > 0
        and now - last_heartbeat_at >= heartbeat_interval_seconds
    )


def _format_heartbeat(
    *,
    start_time: float,
    now: float,
    last_output_at: float,
    state: HeartbeatState,
    boundary: str | None = None,
) -> str:
    parts = [
        f"elapsed={int(now - start_time)}s",
        f"last_output={int(now - last_output_at)}s",
    ]
    if state.cycle is not None:
        parts.append(f"cycle={state.cycle}")
    if state.route is not None:
        parts.append(f"route={state.route}")
    if boundary is not None:
        parts.append(f"boundary={boundary}")
    return " ".join(parts)


def _git_changed_files(
    repo_root: Path,
    before_head: str | None,
    after_head: str | None,
) -> tuple[str, ...]:
    if not before_head or not after_head or before_head == after_head:
        return ()
    output = _git_stdout(
        repo_root,
        ["git", "diff", "--name-only", f"{before_head}..{after_head}"],
    )
    if output is None:
        return ()
    return tuple(
        sorted(
            Path(line.strip()).as_posix()
            for line in output.splitlines()
            if line.strip()
        )
    )


def _out_of_scope_repair_files(
    changed_files: tuple[str, ...],
    *,
    allowed_repair_paths: tuple[str, ...] = (),
) -> tuple[str, ...]:
    allowed_paths = _allowed_repair_paths(allowed_repair_paths)
    allowed_files = {path for path in allowed_paths if not path.endswith("/")}
    allowed_dirs = tuple(path for path in allowed_paths if path.endswith("/"))
    return tuple(
        path
        for path in changed_files
        if path not in allowed_files
        and not any(path.startswith(allowed_dir) for allowed_dir in allowed_dirs)
    )


def _allowed_repair_paths(extra_paths: tuple[str, ...] = ()) -> tuple[str, ...]:
    normalized_extra = tuple(
        Path(path).as_posix().rstrip("/")
        + ("/" if str(path).endswith(("/", "\\")) else "")
        for path in extra_paths
        if path.strip()
    )
    return tuple(
        dict.fromkeys(
            (
                *_DEFAULT_ALLOWED_REPAIR_DIRECTORIES,
                *_DEFAULT_ALLOWED_REPAIR_FILES,
                *normalized_extra,
            )
        )
    )


def _notify(config: SupervisorConfig, *, event: str, message: str, log: LogFn) -> None:
    if config.notify_bell:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except OSError:
            pass
    if not config.notify_command:
        return
    env = os.environ.copy()
    env.update(
        {
            "LLM_HANDOFF_SUPERVISOR_EVENT": event,
            "LLM_HANDOFF_SUPERVISOR_MESSAGE": message,
            "LLM_HANDOFF_SUPERVISOR_REPO_ROOT": str(config.repo_root),
        }
    )
    try:
        completed = subprocess.run(
            config.notify_command,
            cwd=config.repo_root,
            env=env,
            # notify_command is operator-written shell text; the message is
            # passed in env vars, never interpolated into the command.
            shell=True,  # nosemgrep: subprocess-shell-true
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("WARN", f"Supervisor notification failed: {exc}")
        return
    if completed.returncode != 0:
        log("WARN", f"Supervisor notification exited with code {completed.returncode}.")


def _resolve_config_path(repo_root: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    return repo_root / path


def _log_supervisor_startup(config: SupervisorConfig, log: LogFn) -> None:
    dispatch_args = (
        _format_command(config.dispatch_args) if config.dispatch_args else "(none)"
    )
    repair_role = config.repair_role or "(disabled)"
    log("INFO", "================================================")
    log("INFO", "  llm-handoff supervised dispatcher starting")
    log("INFO", "  Operator wrapper, optional bounded repair role")
    log("INFO", "================================================")
    log("INFO", f"Repo root:          {config.repo_root}")
    log("INFO", f"Handoff path:       {_dispatch_handoff_path(config)}")
    log("INFO", f"Dispatcher args:    {dispatch_args}")
    log("INFO", f"Repair role:        {repair_role}")
    log(
        "INFO",
        f"Repair budget:      {_format_attempt_count(config.max_repair_attempts)}",
    )
    log("INFO", f"Transient retries:  {config.max_transient_retries}")
    log("INFO", f"Failure tail lines: {config.tail_lines}")
    log("INFO", f"Stall timeout:      {config.stalled_output_timeout_seconds}s")
    log("INFO", f"Heartbeat:          {config.heartbeat_interval_seconds}s / boundary")
    log("INFO", f"Failure packets:    {config.failure_directory_full_path}")
    log("INFO", f"Incident bundles:   {config.incident_directory_full_path}")
    log("INFO", f"Summary log:        {config.summary_path_full_path}")
    log("INFO", f"Run state:          {config.run_state_path_full_path}")
    log("INFO", f"Instance lock:      {config.lock_path_full_path}")


def _format_attempt_count(attempts: int) -> str:
    suffix = "attempt" if attempts == 1 else "attempts"
    if attempts <= 0:
        return f"{attempts} {suffix} (repair disabled)"
    return f"{attempts} {suffix}"


@contextmanager
def _supervisor_lock(config: SupervisorConfig):
    lock = _acquire_supervisor_lock(config)
    try:
        yield lock
    finally:
        _release_supervisor_lock(lock)


def _acquire_supervisor_lock(config: SupervisorConfig) -> SupervisorLock:
    lock_path = config.lock_path_full_path
    token = uuid4().hex
    payload = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "repo_root": str(config.repo_root),
        "token": token,
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            file_descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            existing_payload = _read_json_file(lock_path)
            existing_pid = _extract_lock_pid(existing_payload)
            if existing_pid is not None and _is_process_running(existing_pid):
                raise SupervisorAlreadyRunning(lock_path, existing_pid) from None
            if existing_pid is None:
                raise SupervisorAlreadyRunning(lock_path, None) from None
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                raise SupervisorAlreadyRunning(lock_path, existing_pid) from None
            continue
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as lock_file:
            json.dump(payload, lock_file, indent=2)
            lock_file.write("\n")
        return SupervisorLock(path=lock_path, token=token)


def _release_supervisor_lock(lock: SupervisorLock) -> None:
    payload = _read_json_file(lock.path)
    if payload.get("token") != lock.token:
        return
    try:
        lock.path.unlink()
    except FileNotFoundError:
        pass


def _read_json_file(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_lock_pid(payload: dict[str, object]) -> int | None:
    pid = payload.get("pid")
    if isinstance(pid, int):
        return pid
    if isinstance(pid, str) and pid.isdigit():
        return int(pid)
    return None


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _is_windows_process_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_windows_process_running(pid: int) -> bool:
    try:
        import ctypes
    except ImportError:
        return True

    error_access_denied = 5
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == error_access_denied


def _append_repair_summary_record(
    config: SupervisorConfig,
    *,
    attempt: int,
    failure_packet_path: Path,
    fingerprint: str,
    before_snapshot: RepoSnapshot,
    after_snapshot: RepoSnapshot,
    repair_exit_code: int,
    restart_outcome: str,
    incident_path: Path | None = None,
    changed_files: tuple[str, ...] = (),
) -> None:
    _append_summary_record(
        config,
        {
            "event": "repair_attempt",
            "repair_attempt": attempt,
            "failure_fingerprint": fingerprint,
            "failure_packet_path": str(failure_packet_path),
            "before_head_sha": before_snapshot.head_sha,
            "repair_head_sha": after_snapshot.head_sha,
            "before_handoff_sha": before_snapshot.handoff_sha,
            "after_handoff_sha": after_snapshot.handoff_sha,
            "repair_exit_code": repair_exit_code,
            "restart_outcome": restart_outcome,
            "incident_path": str(incident_path) if incident_path else None,
            "changed_files": list(changed_files),
        },
    )


def _append_summary_record(
    config: SupervisorConfig, payload: dict[str, object]
) -> None:
    summary_path = config.summary_path_full_path
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(config.repo_root),
        "dispatch_args": list(config.dispatch_args),
        **payload,
    }
    with summary_path.open("a", encoding="utf-8") as summary_file:
        summary_file.write(json.dumps(record, sort_keys=True))
        summary_file.write("\n")


def _is_repairable_dispatch_failure(result: DispatchProcessResult) -> bool:
    if result.exit_code != 0:
        return True
    return any(
        marker in line
        for line in result.output_lines
        for marker in _REPAIRABLE_ZERO_EXIT_MARKERS
    )


def _failure_fingerprint(result: DispatchProcessResult) -> str:
    normalized_lines = [_normalize_log_line(line) for line in result.output_lines]
    payload = {
        "exit_code": result.exit_code,
        "lines": normalized_lines[-20:],
    }
    return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _normalize_log_line(line: str) -> str:
    line = _ANSI_RE.sub("", line)
    line = _TIMESTAMP_RE.sub("", line)
    return line.strip()


def _write_failure_packet(
    config: SupervisorConfig,
    result: DispatchProcessResult,
    *,
    fingerprint: str,
    snapshot: RepoSnapshot,
) -> Path:
    failure_directory = config.failure_directory_full_path
    failure_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    packet_path = failure_directory / f"failure-{timestamp}-{fingerprint[:12]}.json"
    payload = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(config.repo_root),
        "dispatch_args": list(config.dispatch_args),
        "repair_role": config.repair_role,
        "exit_code": result.exit_code,
        "elapsed_seconds": result.elapsed_seconds,
        "fingerprint": fingerprint,
        "head_sha": snapshot.head_sha,
        "dirty_files": list(snapshot.dirty_files),
        "handoff_path": str(_dispatch_handoff_path(config)),
        "handoff_sha": snapshot.handoff_sha,
        "log_tail": list(result.output_lines[-config.tail_lines :]),
    }
    packet_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return packet_path


def _repo_snapshot(
    repo_root: Path,
    *,
    handoff_path: Path | None = None,
) -> RepoSnapshot:
    resolved_handoff_path = (
        handoff_path if handoff_path is not None else repo_root / DEFAULT_HANDOFF_PATH
    )
    return RepoSnapshot(
        head_sha=_git_stdout(repo_root, ["git", "rev-parse", "HEAD"]),
        dirty_files=_git_dirty_files(repo_root),
        handoff_sha=_file_sha(resolved_handoff_path),
    )


def _git_dirty_files(repo_root: Path) -> tuple[str, ...]:
    output = _git_stdout(
        repo_root,
        ["git", "status", "--porcelain", "--untracked-files=all"],
    )
    if output is None:
        return ()
    dirty_files: list[str] = []
    for line in output.splitlines():
        if len(line) < 3:
            continue
        path = line[2:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            dirty_files.append(Path(path).as_posix())
    return tuple(sorted(dirty_files))


def _git_stdout(repo_root: Path, command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _file_sha(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _dedupe(items: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _format_command(command: Sequence[str]) -> str:
    return " ".join(str(part) for part in command)


def _write_child_output_line(raw_line: str) -> None:
    try:
        sys.stdout.write(raw_line)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_line = raw_line.encode(encoding, errors="replace").decode(
            encoding,
            errors="replace",
        )
        sys.stdout.write(safe_line)
    sys.stdout.flush()


def _default_log(level: str, message: str) -> None:
    print(f"[SUPERVISOR] [{level}] {message}")


# --repo-root and --config are shared between the supervisor and the child
# dispatcher. They are resolved once in _parse_args, whichever side of "--"
# they appear on, so the child and the repair role always load the same
# repository and configuration.
def _child_dispatch_options(config: SupervisorConfig) -> list[str]:
    options = ["--repo-root", str(config.repo_root)]
    if config.config_path is not None:
        options.extend(["--config", str(config.config_path_full_path)])
    return options


def _take_child_option(
    dispatch_args: Sequence[str],
    option: str,
) -> tuple[list[str], Path | None]:
    """Remove ``option VALUE`` or ``option=VALUE`` from the pass-through args."""
    remaining: list[str] = []
    value: Path | None = None
    index = 0
    while index < len(dispatch_args):
        argument = dispatch_args[index]
        if argument == option and index + 1 < len(dispatch_args):
            value = Path(dispatch_args[index + 1])
            index += 2
            continue
        if argument.startswith(f"{option}="):
            value = Path(argument.split("=", 1)[1])
            index += 1
            continue
        remaining.append(argument)
        index += 1
    return remaining, value


def _merge_shared_option(
    parser: argparse.ArgumentParser,
    option: str,
    supervisor_value: Path | None,
    child_value: Path | None,
) -> Path | None:
    if supervisor_value is None:
        return child_value
    if child_value is None or child_value.resolve() == supervisor_value.resolve():
        return supervisor_value
    parser.error(
        f"{option} must be the same on both sides of '--': the supervisor got "
        f"{supervisor_value} and the dispatcher got {child_value}."
    )
    return None  # pragma: no cover - parser.error exits


def _parse_args(argv: Sequence[str] | None = None) -> SupervisorConfig:
    parser = argparse.ArgumentParser(
        description="Run llm-handoff with supervised operator controls.",
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run supervisor preflight checks without starting the dispatcher.",
    )
    parser.add_argument(
        "--repair-role",
        choices=_REPAIR_ROLE_CHOICES,
        default=None,
        help="Optional configured role to invoke for bounded supervisor repair.",
    )
    parser.add_argument("--max-repair-attempts", type=int, default=None)
    parser.add_argument(
        "--max-transient-retries",
        type=int,
        default=DEFAULT_MAX_TRANSIENT_RETRIES,
    )
    parser.add_argument("--tail-lines", type=int, default=DEFAULT_TAIL_LINES)
    parser.add_argument(
        "--stalled-output-timeout-seconds",
        type=int,
        default=DEFAULT_STALLED_OUTPUT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--heartbeat-interval-seconds",
        type=int,
        default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    )
    parser.add_argument("--repair-allow-path", action="append", default=())
    parser.add_argument("--notify-bell", action="store_true")
    parser.add_argument("--notify-command", default=None)
    known, dispatch_args = parser.parse_known_args(argv)
    if dispatch_args and dispatch_args[0] == "--":
        dispatch_args = dispatch_args[1:]
    dispatch_args, child_repo_root = _take_child_option(dispatch_args, "--repo-root")
    dispatch_args, child_config = _take_child_option(dispatch_args, "--config")
    repo_root_option = _merge_shared_option(
        parser, "--repo-root", known.repo_root, child_repo_root
    )
    config_option = _merge_shared_option(parser, "--config", known.config, child_config)
    root_start = repo_root_option
    if root_start is None and config_option is not None and config_option.is_absolute():
        root_start = config_option.parent
    repo_root = detect_repo_root(root_start)
    repair_role = known.repair_role
    max_repair_attempts = (
        1
        if repair_role is not None and known.max_repair_attempts is None
        else int(known.max_repair_attempts or 0)
    )
    config_path = config_option
    if config_path is None and (repo_root / DEFAULT_CONFIG_PATH).exists():
        config_path = DEFAULT_CONFIG_PATH
    return SupervisorConfig(
        repo_root=repo_root,
        dispatch_args=tuple(dispatch_args),
        config_path=config_path,
        repair_role=repair_role,
        max_repair_attempts=max_repair_attempts,
        max_transient_retries=known.max_transient_retries,
        tail_lines=known.tail_lines,
        stalled_output_timeout_seconds=known.stalled_output_timeout_seconds,
        heartbeat_interval_seconds=known.heartbeat_interval_seconds,
        allowed_repair_paths=tuple(known.repair_allow_path),
        notify_bell=known.notify_bell,
        notify_command=known.notify_command,
        doctor=known.doctor,
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = _parse_args(argv)
    return run_supervised_dispatch(config)


if __name__ == "__main__":
    raise SystemExit(main())
