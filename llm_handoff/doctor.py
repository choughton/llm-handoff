"""Standalone preflight checks for `llm-handoff doctor`.

The doctor command checks a checkout before the dispatcher runs: Git, the
provider CLI binaries the configured roles launch, the container runtime
Gemini CLI roles need for their sandbox, and the current handoff file. It does
not start a dispatch, contact the network, or write any files.

`_command_available` and `_command_binary_name` live here so the supervisor's
`--doctor` mode and this command share one PATH lookup, including the Windows
`.cmd` shim fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Literal

import yaml

from llm_handoff.agent_providers.gemini import (
    detect_gemini_sandbox_runtime,
    gemini_sandbox_unavailable_message,
)
from llm_handoff.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_HANDOFF_PATH,
    AgentConfig,
    DispatchConfig,
    detect_repo_root,
    load_dispatch_config,
)
from llm_handoff.router import HandoffFrontmatterError, parse_handoff_frontmatter_text
from llm_handoff.router import route as route_handoff
from llm_handoff.text_io import read_dispatch_text
from llm_handoff.validator import validate_handoff_text

CheckStatus = Literal["PASS", "FAIL", "WARN"]

_CONFIG_ERRORS = (OSError, ValueError, yaml.YAMLError)
_GIT_TIMEOUT_SECONDS = 30


def _command_binary_name(command_name: str) -> str:
    return f"{command_name}.cmd" if os.name == "nt" else command_name


def _command_available(command_name: str) -> bool:
    if shutil.which(command_name) is not None:
        return True
    return shutil.which(_command_binary_name(command_name)) is not None


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    status: CheckStatus
    name: str
    message: str

    def format(self) -> str:
        return f"{self.status} {self.name}: {self.message}"


@dataclass(frozen=True, slots=True)
class DoctorReport:
    repo_root: Path
    checks: tuple[DoctorCheck, ...]

    @property
    def error_count(self) -> int:
        return sum(1 for check in self.checks if check.status == "FAIL")

    @property
    def warning_count(self) -> int:
        return sum(1 for check in self.checks if check.status == "WARN")

    @property
    def exit_code(self) -> int:
        return 1 if self.error_count else 0

    def summary(self) -> str:
        verdict = "FAIL" if self.error_count else "PASS"
        return (
            f"Doctor {verdict}: {self.error_count} error(s), "
            f"{self.warning_count} warning(s) in {self.repo_root}"
        )

    def lines(self) -> list[str]:
        return [check.format() for check in self.checks] + [self.summary()]


def resolve_doctor_repo_root(
    repo_root: Path | None = None,
    config_path: Path | None = None,
) -> Path:
    """Resolve the repository root the same way the dispatcher callback does."""
    root_start = repo_root
    if root_start is None and config_path is not None:
        root_start = config_path.parent
    return detect_repo_root(root_start)


def run_doctor(
    repo_root: Path | None = None,
    config_path: Path | None = None,
) -> DoctorReport:
    """Run every preflight check and return the report without printing it."""
    resolved_root = resolve_doctor_repo_root(repo_root, config_path)
    checks: list[DoctorCheck] = []

    dispatch_config = _check_config(resolved_root, config_path, checks)
    git_available = _check_git(resolved_root, checks)
    if dispatch_config is not None:
        _check_role_binaries(dispatch_config, resolved_root, checks)
        _check_container_runtime(dispatch_config, checks)
        handoff_path = dispatch_config.handoff_full_path
        dispatcher_commit_mode = dispatch_config.commit_mode == "dispatcher"
    else:
        handoff_path = (resolved_root / DEFAULT_HANDOFF_PATH).resolve()
        dispatcher_commit_mode = False
    _check_handoff(
        resolved_root,
        handoff_path,
        dispatcher_commit_mode=dispatcher_commit_mode,
        git_available=git_available,
        checks=checks,
    )
    return DoctorReport(repo_root=resolved_root, checks=tuple(checks))


def _check_config(
    repo_root: Path,
    config_path: Path | None,
    checks: list[DoctorCheck],
) -> DispatchConfig | None:
    try:
        dispatch_config = load_dispatch_config(
            repo_root=repo_root,
            config_path=config_path,
        )
    except _CONFIG_ERRORS as exc:
        checks.append(
            DoctorCheck("FAIL", "config", f"dispatch config could not be loaded: {exc}")
        )
        return None

    if config_path is not None:
        source = str(config_path)
    elif (repo_root / DEFAULT_CONFIG_PATH).exists():
        source = str(repo_root / DEFAULT_CONFIG_PATH)
    else:
        source = None
    if source is None:
        message = (
            f"no {DEFAULT_CONFIG_PATH.as_posix()} found; using the built-in "
            "defaults shipped in dispatch_config.example.yaml"
        )
    else:
        message = f"loaded {source}"
    checks.append(DoctorCheck("PASS", "config", message))
    return dispatch_config


def _check_git(repo_root: Path, checks: list[DoctorCheck]) -> bool:
    """Append the Git checks and return whether `git` is on PATH."""
    if not _command_available("git"):
        checks.append(DoctorCheck("FAIL", "git", "git is not on PATH"))
        return False
    checks.append(DoctorCheck("PASS", "git", "git is on PATH"))
    _check_git_repository(repo_root, checks)
    return True


def _check_git_repository(repo_root: Path, checks: list[DoctorCheck]) -> None:

    toplevel = _run_git(repo_root, "rev-parse", "--show-toplevel")
    if toplevel is None or toplevel.returncode != 0:
        detail = ""
        if toplevel is not None and toplevel.stderr.strip():
            detail = f" ({toplevel.stderr.strip().splitlines()[0]})"
        checks.append(
            DoctorCheck(
                "FAIL",
                "git repository",
                f"{repo_root} is not inside a Git repository{detail}",
            )
        )
        return
    checks.append(
        DoctorCheck(
            "PASS",
            "git repository",
            f"inside Git work tree {toplevel.stdout.strip()}",
        )
    )

    # --no-optional-locks keeps `git status` from refreshing the index, so the
    # doctor does not write into .git.
    status = _run_git(repo_root, "--no-optional-locks", "status", "--porcelain")
    if status is None or status.returncode != 0:
        checks.append(
            DoctorCheck("WARN", "git working tree", "could not read git status")
        )
        return
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        checks.append(
            DoctorCheck(
                "WARN",
                "git working tree",
                f"working tree is dirty ({len(dirty)} changed path(s))",
            )
        )
    else:
        checks.append(DoctorCheck("PASS", "git working tree", "working tree is clean"))


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _role_launches(
    dispatch_config: DispatchConfig,
) -> list[tuple[str, AgentConfig]]:
    launches: list[tuple[str, AgentConfig]] = []
    for role, agent_config in dispatch_config.agents.items():
        launches.append((role, agent_config))
        if agent_config.fallback is not None:
            launches.append((f"{role} fallback", agent_config.fallback))
    return launches


def _agent_binary(agent_config: AgentConfig) -> str:
    return agent_config.binary or _command_binary_name(agent_config.provider)


def _role_binary_available(binary: str, repo_root: Path) -> bool:
    """Return whether a role launch would find ``binary``.

    Mirrors ``agent_process._resolve_command_binary``: a path is used as
    given, and a ``.cmd`` name also resolves through its bare stem (for
    example ``claude.cmd`` -> ``claude.exe`` on Windows). Provider processes
    run with the repository root as their working directory, so a relative
    path such as ``./tools/codex`` is resolved against ``repo_root`` rather
    than the caller's current directory.
    """
    if Path(binary).is_absolute() or any(sep in binary for sep in ("\\", "/")):
        path = Path(binary)
        if not path.is_absolute():
            path = repo_root / path
        return path.is_file()
    if _command_available(binary):
        return True
    path = Path(binary)
    return path.suffix.lower() == ".cmd" and shutil.which(path.stem) is not None


def _check_role_binaries(
    dispatch_config: DispatchConfig,
    repo_root: Path,
    checks: list[DoctorCheck],
) -> None:
    for label, agent_config in _role_launches(dispatch_config):
        binary = _agent_binary(agent_config)
        name = f"role {label}"
        if _role_binary_available(binary, repo_root):
            checks.append(
                DoctorCheck(
                    "PASS",
                    name,
                    f"provider {agent_config.provider} binary `{binary}` is on PATH",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "FAIL",
                    name,
                    f"provider {agent_config.provider} binary `{binary}` "
                    "was not found on PATH",
                )
            )


def _check_container_runtime(
    dispatch_config: DispatchConfig,
    checks: list[DoctorCheck],
) -> None:
    # Roles with unsafe_bypass: true launch Gemini CLI with --sandbox=false and
    # do not need a container runtime, matching the Gemini provider.
    # A fallback's setting key is agents.<role>.fallback.unsafe_bypass.
    sandboxed_roles = [
        label.replace(" ", ".")
        for label, agent_config in _role_launches(dispatch_config)
        if agent_config.provider == "gemini" and not agent_config.unsafe_bypass
    ]
    if not sandboxed_roles:
        return
    runtime = detect_gemini_sandbox_runtime()
    if runtime is not None:
        checks.append(
            DoctorCheck(
                "PASS",
                "container runtime",
                f"Gemini CLI sandbox runtime `{runtime}` is on PATH",
            )
        )
        return
    for role in sandboxed_roles:
        checks.append(
            DoctorCheck(
                "FAIL",
                "container runtime",
                gemini_sandbox_unavailable_message(role),
            )
        )


def _check_handoff(
    repo_root: Path,
    handoff_path: Path,
    *,
    dispatcher_commit_mode: bool,
    git_available: bool,
    checks: list[DoctorCheck],
) -> None:
    if not handoff_path.exists():
        checks.append(
            DoctorCheck(
                "WARN",
                "handoff",
                f"no handoff file at {handoff_path}; create one before the first run",
            )
        )
        return
    try:
        handoff_content = read_dispatch_text(handoff_path)
    except OSError as exc:
        checks.append(
            DoctorCheck("FAIL", "handoff", f"could not read {handoff_path}: {exc}")
        )
        return

    decision = route_handoff(handoff_content)
    if decision.route == "unknown":
        checks.append(
            DoctorCheck(
                "WARN",
                "handoff route",
                "router could not determine a dispatchable route "
                "(expected for a placeholder handoff before the first run)",
            )
        )
    else:
        checks.append(
            DoctorCheck("PASS", "handoff route", f"routes to {decision.route}")
        )
    for warning in decision.warnings:
        checks.append(DoctorCheck("WARN", "handoff route", warning))

    try:
        frontmatter = parse_handoff_frontmatter_text(handoff_content)
    except HandoffFrontmatterError:
        frontmatter = None
    producer = (
        frontmatter.producer
        if frontmatter is not None and frontmatter.producer
        else "unknown producer"
    )
    # Without git on PATH (already reported by the git check), skip the
    # validator's Git-backed scope_sha lookup: it shells out to `git` and
    # would raise instead of letting the doctor print its report.
    if not git_available:
        checks.append(
            DoctorCheck(
                "WARN",
                "handoff validation",
                "git is not on PATH; skipped the scope_sha commit lookup",
            )
        )
    try:
        validation = validate_handoff_text(
            handoff_content,
            producer,
            git_cwd=repo_root if git_available else None,
            dispatcher_commit_mode=dispatcher_commit_mode,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(
            DoctorCheck(
                "FAIL",
                "handoff validation",
                f"handoff validator could not run: {exc}",
            )
        )
        return
    for error in validation.errors:
        checks.append(DoctorCheck("FAIL", "handoff validation", error))
    for warning in validation.warnings:
        checks.append(DoctorCheck("WARN", "handoff validation", warning))
    if not validation.errors:
        checks.append(
            DoctorCheck(
                "PASS",
                "handoff validation",
                f"{handoff_path} passes the handoff validator",
            )
        )
