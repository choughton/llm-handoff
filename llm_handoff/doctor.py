"""Standalone preflight checks for `llm-handoff doctor`.

The doctor command checks a checkout before the dispatcher runs: Git, the
commit count (audits need a parent commit for the default review base), the
provider CLI binaries the configured roles launch, the container runtime
Gemini CLI roles need for their sandbox, whether the configured next-agent
normalizer has an API key or CLI to run with, whether the audit runs the
security reviewer (with the opt-out reason when it does not; when that
reviewer resolves to Gemini CLI, `gemini extensions list` is run to confirm
the security extension is active), whether the
work-claim gate is on (with the opt-out reason when it is not), each configured
scanner and whether the gate will run it (its leading executable is looked up
on PATH the way the gate does; scanner commands are never run), and the
current handoff file, validated with the same Work Packet requirement the
dispatch loop applies. When a role or fallback runs on Antigravity without
`unsafe_bypass`, it also checks the `agy` user settings file for the required
`permissions.allow` and `permissions.deny` entries, and for `permissions.deny`
or `permissions.ask` rules that override a required allow entry, reading that
file only. It also warns, per role launch, about interpreter grants (shell
rules that let an agent run `python`, `python3`, or `pytest`, or any command)
and `unsafe_bypass` launches, because an interpreter can run any command past
the shell allowlist; these lines are warnings only. In Linear mode (`tracker.provider: linear`) it reports, for each
provider CLI the configured roles launch, whether that CLI's MCP config files
define a server named `tracker.mcp_server`; it reads those MCP config files
only, never prints a value from them, and never runs the provider CLI.
Unless `--probe-tracker` is given, it does not start a dispatch, call a
provider, contact the network, or write any files.

`--probe-tracker <ISSUE>` (Linear mode only) adds one `tracker probe` line.
It launches the first configured Claude CLI role or fallback once, with no
built-in tools and only the tracker server's `get_issue` and
`list_issue_statuses` tools approved (write tools are denied), project and
local Claude settings skipped, and all hooks disabled, asks it to read the
issue and the team's workflow states, and checks that every configured state
name exists. The probe still writes no file and starts at most one provider
process.

`_command_available` and `_command_binary_name` live here so the supervisor's
`--doctor` mode and this command share one PATH lookup, including the Windows
`.cmd` shim fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from typing import Any, Literal

import yaml

from llm_handoff.agent_process import _run_logged_agent_command
from llm_handoff.agent_providers.claude import _build_claude_env
from llm_handoff.agent_providers.gemini import (
    detect_gemini_sandbox_runtime,
    gemini_sandbox_unavailable_message,
)
from llm_handoff.antigravity_permissions import (
    ANTIGRAVITY_REQUIRED_DENY,
    antigravity_allow_entries,
    antigravity_allowed_commands,
    antigravity_settings_path,
)
from llm_handoff.config import (
    CLAUDE_BINARY,
    CLAUDE_DEFAULT_ALLOWED_TOOLS,
    CLAUDE_DISALLOWED_TOOLS,
    CLAUDE_MODEL,
    CODEX_BINARY,
    DEFAULT_CONFIG_PATH,
    DEFAULT_HANDOFF_PATH,
    GEMINI_BINARY,
    AgentConfig,
    DispatchConfig,
    detect_repo_root,
    load_dispatch_config,
    scanner_command_placeholders,
)
from llm_handoff.provider_auth import ANTIGRAVITY_SETTINGS_PATH
from llm_handoff.router import HandoffFrontmatterError, parse_handoff_frontmatter_text
from llm_handoff.router import route as route_handoff
from llm_handoff.text_io import read_dispatch_text
from llm_handoff.validator import validate_handoff_text
from llm_handoff.work_claim import command_executable

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
    *,
    probe_tracker_issue: str | None = None,
) -> DoctorReport:
    """Run every preflight check and return the report without printing it.

    ``probe_tracker_issue`` (``--probe-tracker``) adds the opt-in tracker
    probe, which launches the Claude CLI once; without it no provider runs.
    """
    resolved_root = resolve_doctor_repo_root(repo_root, config_path)
    checks: list[DoctorCheck] = []

    dispatch_config = _check_config(resolved_root, config_path, checks)
    git_available = _check_git(resolved_root, checks)
    if dispatch_config is not None:
        _check_role_binaries(dispatch_config, resolved_root, checks)
        _check_container_runtime(dispatch_config, checks)
        _check_antigravity_permissions(dispatch_config, checks)
        _check_interpreter_grants(dispatch_config, checks)
        _check_normalizer(dispatch_config, resolved_root, checks)
        _check_security_review(dispatch_config, checks)
        _check_work_claim_gate(dispatch_config, checks)
        _check_scanners(dispatch_config, checks)
        _check_tracker_connectors(dispatch_config, resolved_root, checks)
        if probe_tracker_issue is not None:
            _check_tracker_probe(
                dispatch_config, resolved_root, probe_tracker_issue, checks
            )
        handoff_path = dispatch_config.handoff_full_path
        dispatcher_commit_mode = dispatch_config.commit_mode == "dispatcher"
        require_work_packet = dispatch_config.enforce_work_packet
        tracker_provider = dispatch_config.tracker.provider
    else:
        handoff_path = (resolved_root / DEFAULT_HANDOFF_PATH).resolve()
        dispatcher_commit_mode = False
        # The config did not load (already a FAIL); validate against the
        # built-in default, which runs the work-claim gate.
        require_work_packet = True
        tracker_provider = "none"
    _check_handoff(
        resolved_root,
        handoff_path,
        dispatcher_commit_mode=dispatcher_commit_mode,
        require_work_packet=require_work_packet,
        tracker_provider=tracker_provider,
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
    _check_git_history(repo_root, checks)

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


def _check_git_history(repo_root: Path, checks: list[DoctorCheck]) -> None:
    """Warn when the repository has fewer than two commits.

    Audit fan-out reviews ``<head>~1..<head>`` when no explicit base is given,
    and that parent does not exist in a repository with one commit. The
    check only reads history with ``rev-list``; it never writes into .git.
    """
    result = _run_git(repo_root, "rev-list", "--count", "--max-count=2", "HEAD")
    if result is None:
        checks.append(DoctorCheck("WARN", "git history", "could not count commits"))
        return
    if result.returncode != 0:
        checks.append(
            DoctorCheck(
                "WARN",
                "git history",
                "repository has no commits yet; commit before the first "
                "dispatcher run",
            )
        )
        return
    try:
        count = int(result.stdout.strip())
    except ValueError:
        checks.append(DoctorCheck("WARN", "git history", "could not count commits"))
        return
    if count >= 2:
        checks.append(
            DoctorCheck(
                "PASS", "git history", "repository has at least two commits"
            )
        )
    elif count == 1:
        checks.append(
            DoctorCheck(
                "WARN",
                "git history",
                "repository has one commit; the audit review base defaults to "
                "the parent of the reviewed commit, so audits need a second "
                "commit (or an explicit base) first",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "WARN",
                "git history",
                "repository has no commits yet; commit before the first "
                "dispatcher run",
            )
        )


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


_ANTIGRAVITY_CHECK_NAME = "antigravity permissions"
_GIT_PUSH_GRANT_PREFIXES = ("command(git push", "unsandboxed(git push")
_REGEX_GRANT_PREFIXES = ("command(regex:", "unsandboxed(regex:")
# Push command lines a regex allow entry must not match.
_GIT_PUSH_PROBES = ("git push", "git push origin main", "git -C . push origin main")


def _regex_grant_allows_push(entry: str) -> bool:
    """Return True when a regex allow ``entry`` matches a `git push` line.

    Only ``command(regex:<p>)`` and ``unsandboxed(regex:<p>)`` entries are
    checked; a pattern that does not compile is not counted.
    """
    if not entry.endswith(")"):
        return False
    for prefix in _REGEX_GRANT_PREFIXES:
        if entry.startswith(prefix):
            pattern = entry[len(prefix) : -1]
            break
    else:
        return False
    try:
        compiled = re.compile(pattern)
    except re.error:
        return False
    return any(compiled.search(probe) is not None for probe in _GIT_PUSH_PROBES)


def _read_antigravity_permissions(
    path: Path,
) -> tuple[list[str], list[str], list[str]] | str | None:
    """Read ``permissions.allow``, ``.deny``, and ``.ask`` from ``path``.

    Returns the three lists, None when the file does not exist, or a reason
    string when the file cannot be read or has the wrong shape. A missing key
    counts as an empty list. The file is only read, never opened for writing.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not be read ({exc})"
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        return f"is not valid JSON ({exc})"
    if not isinstance(data, dict):
        return "is not a JSON object"
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return 'has no "permissions" object'
    lists: list[list[str]] = []
    for key in ("allow", "deny", "ask"):
        value = permissions.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            return f'has a "permissions.{key}" value that is not a list of strings'
        lists.append(value)
    return lists[0], lists[1], lists[2]


def _command_rule_shadows(rule: str, command: str) -> bool:
    """Return True when the ``command(...)`` ``rule`` may match ``command``.

    Antigravity's exact matching rules are not documented in full, so this
    errs toward reporting: an exact match, a word prefix (``command(git)``
    for ``git status``), a ``*``/``?`` wildcard pattern, or a ``regex:``
    pattern that matches the command. Rules that are not ``command(...)``,
    and regex rules that do not compile, are not counted.
    """
    if not (rule.startswith("command(") and rule.endswith(")")):
        return False
    pattern = rule[len("command(") : -1].strip()
    if not pattern:
        return False
    if pattern.startswith("regex:"):
        try:
            return re.search(pattern[len("regex:") :], command) is not None
        except re.error:
            return False
    if pattern == command or command.startswith(pattern + " "):
        return True
    return fnmatch.fnmatchcase(command, pattern)


def _shadowing_rules(
    rules: list[str],
    allowed_commands: tuple[str, ...],
    allow_required: tuple[str, ...],
) -> list[tuple[str, list[str]]]:
    """Pair each rule in ``rules`` with the required allow entries it shadows.

    Each rule is tested against the plain commands in ``allowed_commands``;
    a hit reports the matching entry from ``allow_required``, which lines up
    with ``allowed_commands`` one-to-one.
    """
    shadowed: list[tuple[str, list[str]]] = []
    for rule in rules:
        hits = [
            entry
            for command, entry in zip(allowed_commands, allow_required, strict=True)
            if _command_rule_shadows(rule, command)
        ]
        if hits:
            shadowed.append((rule, hits))
    return shadowed


def _ascii_entry(entry: str) -> str:
    """Escape characters outside printable ASCII; keep regex backslashes as-is."""
    return "".join(
        ch if " " <= ch <= "~" else ch.encode("unicode_escape").decode("ascii")
        for ch in entry
    )


def _describe_shadowing(shadowed: list[tuple[str, list[str]]]) -> str:
    return "; ".join(
        f"`{_ascii_entry(rule)}` shadows {', '.join(hits)}" for rule, hits in shadowed
    )


def _check_antigravity_permissions(
    dispatch_config: DispatchConfig,
    checks: list[DoctorCheck],
) -> None:
    """Check the `agy` user settings file for the required grants.

    Covers every Antigravity role and fallback without ``unsafe_bypass``
    (``unsafe_bypass`` launches skip permission prompts and need no grants).
    Grants are user-wide, so the union of entries is checked once. The file
    is read only; the doctor never writes it.
    """
    labels = [
        label
        for label, agent_config in _role_launches(dispatch_config)
        if agent_config.provider == "antigravity" and not agent_config.unsafe_bypass
    ]
    if not labels:
        return
    covers = f"(covers {', '.join(labels)})"
    allowed_commands = antigravity_allowed_commands(
        dispatch_config.verification_allowed_prefixes
    )
    allow_required = antigravity_allow_entries(
        dispatch_config.verification_allowed_prefixes
    )
    path = antigravity_settings_path()
    result = _read_antigravity_permissions(path)
    if result is None:
        checks.append(
            DoctorCheck(
                "WARN",
                _ANTIGRAVITY_CHECK_NAME,
                f"{ANTIGRAVITY_SETTINGS_PATH} was not found {covers}; add "
                "permissions.allow entries: "
                + ", ".join(allow_required)
                + "; add permissions.deny entries: "
                + ", ".join(ANTIGRAVITY_REQUIRED_DENY),
            )
        )
        return
    if isinstance(result, str):
        checks.append(
            DoctorCheck(
                "WARN",
                _ANTIGRAVITY_CHECK_NAME,
                f"{ANTIGRAVITY_SETTINGS_PATH} {result} {covers}; expected "
                '{"permissions": {"allow": [...], "deny": [...]}}',
            )
        )
        return
    allow, deny, ask = result
    found = False
    for entry in allow:
        if entry.startswith(_GIT_PUSH_GRANT_PREFIXES) or _regex_grant_allows_push(
            entry
        ):
            found = True
            checks.append(
                DoctorCheck(
                    "FAIL",
                    _ANTIGRAVITY_CHECK_NAME,
                    f"{ANTIGRAVITY_SETTINGS_PATH} permissions.allow grants "
                    f"`{_ascii_entry(entry)}` "
                    f"{covers}; remove it, because agents must not "
                    "push",
                )
            )
    # Antigravity applies Deny > Ask > Allow, and a headless `agy -p` run
    # soft-denies any command that needs approval, so a deny or ask rule that
    # matches a required allow entry blocks that command despite the grant.
    for key, rules in (("deny", deny), ("ask", ask)):
        shadowed = _shadowing_rules(rules, allowed_commands, allow_required)
        if shadowed:
            found = True
            checks.append(
                DoctorCheck(
                    "WARN",
                    _ANTIGRAVITY_CHECK_NAME,
                    f"{ANTIGRAVITY_SETTINGS_PATH} permissions.{key} has rules "
                    f"that override required allow entries: "
                    f"{_describe_shadowing(shadowed)} {covers}; Antigravity "
                    "applies Deny > Ask > Allow and headless `agy -p` runs "
                    "deny commands that need approval, so narrow or remove "
                    "these rules",
                )
            )
    missing_allow = [entry for entry in allow_required if entry not in allow]
    if missing_allow:
        found = True
        checks.append(
            DoctorCheck(
                "WARN",
                _ANTIGRAVITY_CHECK_NAME,
                f"{ANTIGRAVITY_SETTINGS_PATH} permissions.allow is missing "
                f"{', '.join(missing_allow)} {covers}; entries are matched by "
                "exact string, and legacy `command(<cmd>)` exact grants do not "
                "cover commands run with arguments",
            )
        )
    missing_deny = [entry for entry in ANTIGRAVITY_REQUIRED_DENY if entry not in deny]
    if missing_deny:
        found = True
        checks.append(
            DoctorCheck(
                "WARN",
                _ANTIGRAVITY_CHECK_NAME,
                f"{ANTIGRAVITY_SETTINGS_PATH} permissions.deny is missing "
                f"{', '.join(missing_deny)} {covers}",
            )
        )
    if not found:
        checks.append(
            DoctorCheck(
                "PASS",
                _ANTIGRAVITY_CHECK_NAME,
                f"{ANTIGRAVITY_SETTINGS_PATH} has every required allow and deny "
                f"entry {covers}",
            )
        )


_INTERPRETER_CHECK_NAME = "interpreter grants"
_INTERPRETER_EXECUTABLES = ("python", "python3", "pytest")
_INTERPRETER_BYPASS_RISK = (
    "an interpreter can run any command, including `git push`, past the "
    "allowlist, so the allowlist is a guardrail, not a sandbox"
)
_CLAUDE_BASH_TOKEN_END = re.compile(r"[ :*]")


def _claude_interpreter_rule(rule: str) -> bool:
    """Return True when the Claude ``rule`` grants an interpreter or any command.

    Counts bare ``Bash``, ``Bash(*)``, and ``Bash(<x>)`` where the first token
    of ``<x>`` (text up to the first space, ``:``, or ``*``) is in
    ``_INTERPRETER_EXECUTABLES``.
    """
    if rule in ("Bash", "Bash(*)"):
        return True
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    inner = rule[len("Bash(") : -1]
    first = _CLAUDE_BASH_TOKEN_END.split(inner, maxsplit=1)[0]
    return first in _INTERPRETER_EXECUTABLES


def _antigravity_interpreter_command(command: str) -> bool:
    tokens = command.split()
    return bool(tokens) and tokens[0] in _INTERPRETER_EXECUTABLES


def _check_interpreter_grants(
    dispatch_config: DispatchConfig,
    checks: list[DoctorCheck],
) -> None:
    """Warn, per role launch, about grants that bypass the shell allowlist.

    Claude launches use ``allowed_tools`` or ``CLAUDE_DEFAULT_ALLOWED_TOOLS``
    (the rule in ``agent_providers/claude.py``); Antigravity launches use the
    required ``agy`` allow entries plus the verification prefixes (the
    settings file is not read here). ``unsafe_bypass`` launches have no shell
    allowlist at all. Every line is WARN or PASS, never FAIL.
    """
    allowed_commands = antigravity_allowed_commands(
        dispatch_config.verification_allowed_prefixes
    )
    allow_entries = antigravity_allow_entries(
        dispatch_config.verification_allowed_prefixes
    )
    agy_matches = [
        entry
        for command, entry in zip(allowed_commands, allow_entries, strict=True)
        if _antigravity_interpreter_command(command)
    ]
    added = False
    allowlisted_launch = False
    for label, agent_config in _role_launches(dispatch_config):
        provider = agent_config.provider
        if agent_config.unsafe_bypass:
            added = True
            checks.append(
                DoctorCheck(
                    "WARN",
                    _INTERPRETER_CHECK_NAME,
                    f"{label} (provider {provider}) sets unsafe_bypass: true, so "
                    "the launch has no shell allowlist at all",
                )
            )
            continue
        if provider == "claude":
            allowlisted_launch = True
            tools = (
                CLAUDE_DEFAULT_ALLOWED_TOOLS
                if agent_config.allowed_tools is None
                else agent_config.allowed_tools
            )
            matches = [rule for rule in tools if _claude_interpreter_rule(rule)]
            if matches:
                added = True
                checks.append(
                    DoctorCheck(
                        "WARN",
                        _INTERPRETER_CHECK_NAME,
                        f"{label} (provider claude) allows "
                        + ", ".join(f"`{_ascii_entry(rule)}`" for rule in matches)
                        + f"; {_INTERPRETER_BYPASS_RISK}",
                    )
                )
        elif provider == "antigravity":
            allowlisted_launch = True
            if agy_matches:
                added = True
                checks.append(
                    DoctorCheck(
                        "WARN",
                        _INTERPRETER_CHECK_NAME,
                        f"{label} (provider antigravity) needs the agy allow "
                        "entries "
                        + ", ".join(
                            f"`{_ascii_entry(entry)}`" for entry in agy_matches
                        )
                        + f"; {_INTERPRETER_BYPASS_RISK}, and these grants are "
                        "user-wide, so they apply to every agy run for this user",
                    )
                )
    if not added and allowlisted_launch:
        checks.append(
            DoctorCheck(
                "PASS",
                _INTERPRETER_CHECK_NAME,
                "no role has an interpreter grant",
            )
        )


_NORMALIZER_API_KEY_NAMES: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
}
# Providers whose normalizer can run through a local CLI login. The codex
# normalizer is CLI-only, so it has no API key entry above; claude and gemini
# use the CLI login only when no API key is set.
_NORMALIZER_CLI_BINARIES: dict[str, str] = {
    "claude": CLAUDE_BINARY,
    "codex": CODEX_BINARY,
    "gemini": GEMINI_BINARY,
}


def _check_normalizer(
    dispatch_config: DispatchConfig,
    repo_root: Path,
    checks: list[DoctorCheck],
) -> None:
    """Report whether the configured normalizer has a key or CLI to run with.

    The provider is never called, so auth is reported as not verified.
    """
    provider = dispatch_config.normalizer.provider
    key_names = _NORMALIZER_API_KEY_NAMES.get(provider, ())
    binary = _NORMALIZER_CLI_BINARIES.get(provider)

    present_key = next((name for name in key_names if os.environ.get(name)), None)
    if present_key is not None:
        checks.append(
            DoctorCheck(
                "PASS",
                "normalizer",
                f"provider {provider} can use {present_key} (auth not verified)",
            )
        )
        return
    if binary is not None and _role_binary_available(binary, repo_root):
        checks.append(
            DoctorCheck(
                "PASS",
                "normalizer",
                f"provider {provider} binary `{binary}` is on PATH "
                "(auth not verified)",
            )
        )
        return

    missing = [*key_names]
    if binary is not None:
        missing.append(f"the `{binary}` binary")
    checks.append(
        DoctorCheck(
            "FAIL",
            "normalizer",
            f"provider {provider} cannot run: {_describe_missing(missing)}",
        )
    )


def _check_security_review(
    dispatch_config: DispatchConfig,
    checks: list[DoctorCheck],
) -> None:
    """Report whether the audit runs the security reviewer.

    The dispatcher checks that the reviewer CLI can launch at startup; the
    doctor reports the setting, and the opt-out reason when it is off. When
    the reviewer for an implementer role resolves to Gemini CLI, the doctor
    also runs `gemini extensions list`, because that reviewer cannot start
    without the Gemini CLI security extension and the user has to install
    it; no model is called.
    """
    audit = dispatch_config.audit
    if not audit.security_enabled:
        checks.append(
            DoctorCheck(
                "WARN",
                "security review",
                "the security reviewer is off (audit.reviewers: [quality]); "
                f"opt-out reason: {audit.security_opt_out_reason}",
            )
        )
        return
    checks.append(
        DoctorCheck(
            "PASS",
            "security review",
            "the security reviewer is on (security provider: "
            f"{audit.security_provider or 'auto'})",
        )
    )
    if _security_reviewer_uses_gemini(dispatch_config):
        _check_gemini_security_extension(dispatch_config, checks)


def _security_reviewer_uses_gemini(dispatch_config: DispatchConfig) -> bool:
    """Whether any implementer's security review would run on Gemini CLI."""
    # audit_review imports this module for the binary lookup, so import it
    # here rather than at module level.
    from llm_handoff import audit_review

    for implementer_role in audit_review._IMPLEMENTER_ROLES:
        try:
            provider, _origin = audit_review.resolve_security_provider(
                dispatch_config, implementer_role=implementer_role, log=None
            )
        except audit_review.SecurityReviewerUnavailable:
            # Startup reports the missing reviewer CLI; the role binary
            # checks above already cover it here.
            continue
        if provider == "gemini":
            return True
    return False


def _check_gemini_security_extension(
    dispatch_config: DispatchConfig,
    checks: list[DoctorCheck],
) -> None:
    from llm_handoff import audit_review

    error = audit_review._gemini_extension_missing_error(dispatch_config)
    if error is None:
        checks.append(
            DoctorCheck(
                "PASS",
                "security extension",
                "the Gemini CLI security extension "
                f"({audit_review.GEMINI_SECURITY_EXTENSION_NAME}) is active",
            )
        )
        return
    checks.append(
        DoctorCheck(
            "FAIL",
            "security extension",
            error.removeprefix("security_reviewer_failed: ").removeprefix(
                "security_extension_missing: "
            ),
        )
    )


def _check_work_claim_gate(
    dispatch_config: DispatchConfig,
    checks: list[DoctorCheck],
) -> None:
    """Report whether the work-claim gate is on, and the opt-out reason if not.

    With the gate on, also report the Verification command allowlist: WARN
    when ``verification_allowed_prefixes`` is empty, PASS when it is set.
    """
    if not dispatch_config.enforce_work_packet:
        checks.append(
            DoctorCheck(
                "WARN",
                "work-claim gate",
                "the work-claim gate is off (enforce_work_packet: false); "
                f"opt-out reason: {dispatch_config.work_packet_opt_out_reason}",
            )
        )
        return
    checks.append(
        DoctorCheck(
            "PASS",
            "work-claim gate",
            "the work-claim gate is on (enforce_work_packet: true)",
        )
    )
    prefixes = dispatch_config.verification_allowed_prefixes
    if prefixes:
        checks.append(
            DoctorCheck(
                "PASS",
                "verification allowlist",
                "Work Packet Verification commands must start with one of: "
                + ", ".join(prefixes),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "WARN",
                "verification allowlist",
                "verification_allowed_prefixes is empty, so the work-claim gate "
                "runs any Work Packet Verification command; list the command "
                "prefixes you expect (for example `python -m pytest`)",
            )
        )


_SCANNERS_CHECK_NAME = "scanners"
_SCANNERS_DOC_POINTER = "see the Scanners section of CONFIGURATION.md"


def _check_scanners(
    dispatch_config: DispatchConfig,
    checks: list[DoctorCheck],
) -> None:
    """Report each configured scanner and whether the work-claim gate runs it.

    The leading program comes from ``work_claim.command_executable`` and is
    looked up with ``shutil.which`` only, the same lookup the gate uses (no
    ``.cmd`` fallback). Scanner commands are never run.
    """
    scanners = dispatch_config.scanners
    if not scanners:
        checks.append(
            DoctorCheck(
                "PASS",
                _SCANNERS_CHECK_NAME,
                "no scanners are configured, so the work-claim gate runs none; "
                f"{_SCANNERS_DOC_POINTER} to add one",
            )
        )
        return
    names = ", ".join(scanner.name for scanner in scanners)
    if not dispatch_config.enforce_work_packet:
        checks.append(
            DoctorCheck(
                "WARN",
                _SCANNERS_CHECK_NAME,
                f"scanners are configured ({names}) but none of them run while "
                "the work-claim gate is off (enforce_work_packet: false)",
            )
        )
        return
    for scanner in scanners:
        name = f"scanner {scanner.name}"
        executable = command_executable(scanner.command)
        # A leading placeholder is only known once the gate renders the
        # command, so doctor cannot check it either.
        if executable is None or "{" in executable:
            checks.append(
                DoctorCheck(
                    "WARN",
                    name,
                    "doctor could not check the executable because the command "
                    "does not start with a plain program name; the gate will run "
                    "the command and treat a not-found exit as a missing scanner",
                )
            )
            continue
        condition = _scanner_applies_condition(scanner.command)
        if shutil.which(executable) is not None:
            if condition:
                outcome = (
                    f"will run scanner {scanner.name} on claims {condition} "
                    "and skip it on other claims"
                )
            else:
                outcome = f"will run scanner {scanner.name}"
            checks.append(
                DoctorCheck(
                    "PASS",
                    name,
                    f"executable `{executable}` is on PATH; the work-claim gate "
                    f"{outcome}",
                )
            )
        elif scanner.required:
            claims = f"every claim {condition}" if condition else "every claim"
            checks.append(
                DoctorCheck(
                    "FAIL",
                    name,
                    f"executable `{executable}` was not found on PATH; {claims} "
                    f"will fail with scanner_missing:{scanner.name} until "
                    "the tool is installed or the scanner is set to "
                    "required: false",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "WARN",
                    name,
                    f"executable `{executable}` was not found on PATH; the "
                    f"work-claim gate will skip scanner {scanner.name} "
                    "(required: false)",
                )
            )


_SCANNER_LIST_PLACEHOLDERS = ("changed_files", "changed_py_files")


def _scanner_applies_condition(command: str) -> str:
    """Describe which claims a scanner applies to, or "" for every claim.

    The gate skips a scanner, before it looks up the executable, when the
    command references ``{changed_files}`` or ``{changed_py_files}`` and that
    list is empty for the claim (``work_claim.render_scanner_command``).
    """
    try:
        placeholders = scanner_command_placeholders(command)
    except ValueError:
        return ""
    used = [
        "{" + placeholder + "}"
        for placeholder in _SCANNER_LIST_PLACEHOLDERS
        if placeholder in placeholders
    ]
    if not used:
        return ""
    verb = "is" if len(used) == 1 else "are"
    return f"where {' and '.join(used)} {verb} nonempty"


_TRACKER_CONNECTOR_CHECK_NAME = "tracker connector"
_McpConfigKind = Literal["JSON", "TOML"]


def _read_mcp_config(path: Path, kind: _McpConfigKind) -> dict[str, Any] | str | None:
    """Read one JSON or TOML MCP config file for the tracker connector check.

    Returns the top-level object, None when the file does not exist, or a
    reason string when it cannot be read or parsed. A reason names the path
    and carries only the ``OSError``, ``json``, or ``tomllib`` message, never
    file content. The file is only read, never opened for writing.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{path} could not be read ({exc})"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"{path} is not valid UTF-8"
    try:
        data: object = json.loads(text) if kind == "JSON" else tomllib.loads(text)
    except (ValueError, RecursionError) as exc:
        # json.JSONDecodeError and tomllib.TOMLDecodeError are ValueErrors.
        return f"{path} is not valid {kind} ({exc})"
    if not isinstance(data, dict):
        return f"{path} is not a {kind} object"
    return data


def _defines_mcp_server(
    data: dict[str, Any], key: str, server: str, *, require_table: bool = False
) -> bool:
    servers = data.get(key)
    if not isinstance(servers, dict) or server not in servers:
        return False
    return not require_table or isinstance(servers[server], dict)


def _resolved_or_none(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _claude_project_entry(
    data: dict[str, Any], repo_root: Path
) -> dict[str, Any] | None:
    """Return the ``projects`` entry whose key resolves to ``repo_root``."""
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return None
    target = _resolved_or_none(repo_root)
    if target is None:
        return None
    for key, entry in projects.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(entry, dict):
            continue
        if _resolved_or_none(Path(key)) == target:
            return entry
    return None


def _connector_warn(
    provider: str,
    server: str,
    checked: list[str],
    problems: list[str],
    covers: str,
    *,
    extra: str = "",
) -> DoctorCheck:
    detail = "".join(f"; {problem}" for problem in problems)
    return DoctorCheck(
        "WARN",
        _TRACKER_CONNECTOR_CHECK_NAME,
        f"{provider}: no checked MCP config defines MCP server `{server}` "
        f"(checked {', '.join(checked)}){detail}{extra} {covers}",
    )


def _claude_strict_connector(path: Path, server: str, covers: str) -> DoctorCheck:
    """Check ``tracker.claude_mcp_config`` for Claude launches that load only it."""
    result = _read_mcp_config(path, "JSON")
    if result is None:
        reason = f"{path} was not found"
    elif isinstance(result, str):
        reason = result
    elif not isinstance(result.get("mcpServers"), dict):
        reason = f"{path} has no mcpServers object"
    elif server in result["mcpServers"]:
        return DoctorCheck(
            "PASS",
            _TRACKER_CONNECTOR_CHECK_NAME,
            f"claude: {path} (tracker.claude_mcp_config) defines MCP server "
            f"`{server}` {covers}",
        )
    else:
        reason = f"{path} does not define MCP server `{server}` under mcpServers"
    return DoctorCheck(
        "FAIL",
        _TRACKER_CONNECTOR_CHECK_NAME,
        f"claude: {reason} (tracker.claude_mcp_config); these launches pass "
        f"--strict-mcp-config and load only that file, so the Linear tools for "
        f"MCP server `{server}` cannot load {covers}",
    )


def _claude_user_connector(repo_root: Path, server: str, covers: str) -> DoctorCheck:
    """Check the project and user Claude configs that non-strict launches load."""
    project_path = repo_root / ".mcp.json"
    user_path = _claude_user_config_path()
    project_entry_label = f"{user_path} projects entry for {repo_root}"
    checked = [str(project_path), str(user_path), project_entry_label]
    problems: list[str] = []
    found: str | None = None

    result = _read_mcp_config(project_path, "JSON")
    if isinstance(result, str):
        problems.append(result)
    elif result is not None and _defines_mcp_server(result, "mcpServers", server):
        found = str(project_path)
    if found is None:
        result = _read_mcp_config(user_path, "JSON")
        if isinstance(result, str):
            problems.append(result)
        elif result is not None:
            if _defines_mcp_server(result, "mcpServers", server):
                found = f"{user_path} (user mcpServers)"
            else:
                entry = _claude_project_entry(result, repo_root)
                if entry is not None and _defines_mcp_server(
                    entry, "mcpServers", server
                ):
                    found = project_entry_label
    if found is not None:
        return DoctorCheck(
            "PASS",
            _TRACKER_CONNECTOR_CHECK_NAME,
            f"claude: {found} defines MCP server `{server}` {covers}",
        )
    return _connector_warn(
        "claude",
        server,
        checked,
        problems,
        covers,
        extra=(
            "; servers defined elsewhere (for example managed settings or "
            "plugins) are not seen"
        ),
    )


def _file_connector(
    provider: str,
    paths: list[Path],
    kind: _McpConfigKind,
    key: str,
    server: str,
    covers: str,
) -> DoctorCheck:
    """PASS on the first file in ``paths`` that defines ``key.<server>``."""
    problems: list[str] = []
    for path in paths:
        result = _read_mcp_config(path, kind)
        if isinstance(result, str):
            problems.append(result)
        elif result is not None and _defines_mcp_server(
            result, key, server, require_table=kind == "TOML"
        ):
            return DoctorCheck(
                "PASS",
                _TRACKER_CONNECTOR_CHECK_NAME,
                f"{provider}: {path} defines MCP server `{server}` {covers}",
            )
    return _connector_warn(
        provider, server, [str(path) for path in paths], problems, covers
    )


def _env_dir(name: str) -> Path | None:
    """Return the directory named by environment variable ``name`` if non-empty."""
    value = os.environ.get(name, "")
    if value.strip():
        return Path(value)
    return None


def _claude_user_config_path() -> Path:
    """User ``.claude.json``, relocated by ``CLAUDE_CONFIG_DIR`` when set."""
    config_dir = _env_dir("CLAUDE_CONFIG_DIR")
    if config_dir is not None:
        return config_dir / ".claude.json"
    return Path.home() / ".claude.json"


def _gemini_user_settings_path() -> Path:
    """User Gemini settings, rooted at ``GEMINI_CLI_HOME`` when set."""
    root = _env_dir("GEMINI_CLI_HOME")
    if root is None:
        root = Path.home()
    return root / ".gemini" / "settings.json"


def _codex_config_paths(repo_root: Path) -> list[Path]:
    codex_home = _env_dir("CODEX_HOME")
    if codex_home is not None:
        user_path = codex_home / "config.toml"
    else:
        user_path = Path.home() / ".codex" / "config.toml"
    return [user_path, repo_root / ".codex" / "config.toml"]


def _check_tracker_connectors(
    dispatch_config: DispatchConfig,
    repo_root: Path,
    checks: list[DoctorCheck],
) -> None:
    """Report, per provider CLI, whether its MCP config defines the tracker server.

    Runs only in Linear mode. Lines are grouped per provider (claude, codex,
    gemini), each ending with the role labels it covers. Only
    MCP config files are read; no provider CLI is run, nothing is written,
    and no value from a config file is printed. Only Claude launches that
    load ``tracker.claude_mcp_config`` with ``--strict-mcp-config`` can FAIL.
    """
    tracker = dispatch_config.tracker
    if not tracker.enabled or tracker.mcp_server is None:
        return
    server = tracker.mcp_server
    strict_path = dispatch_config.tracker_claude_mcp_config_path
    groups: dict[str, list[str]] = {
        "claude strict": [],
        "claude": [],
        "codex": [],
        "gemini": [],
    }
    for label, agent_config in _role_launches(dispatch_config):
        provider = agent_config.provider
        if (
            provider == "claude"
            and strict_path is not None
            and not agent_config.unsafe_bypass
        ):
            groups["claude strict"].append(label)
        elif provider in groups:
            groups[provider].append(label)

    for group, labels in groups.items():
        if not labels:
            continue
        covers = f"(covers {', '.join(labels)})"
        if group == "claude strict" and strict_path is not None:
            checks.append(_claude_strict_connector(strict_path, server, covers))
        elif group == "claude":
            checks.append(_claude_user_connector(repo_root, server, covers))
        elif group == "codex":
            checks.append(
                _file_connector(
                    "codex",
                    _codex_config_paths(repo_root),
                    "TOML",
                    "mcp_servers",
                    server,
                    covers,
                )
            )
        elif group == "gemini":
            checks.append(
                _file_connector(
                    "gemini",
                    [
                        repo_root / ".gemini" / "settings.json",
                        _gemini_user_settings_path(),
                    ],
                    "JSON",
                    "mcpServers",
                    server,
                    covers,
                )
            )


_TRACKER_PROBE_CHECK_NAME = "tracker probe"
_TRACKER_PROBE_TIMEOUT_MS = 180_000
_TRACKER_PROBE_ISSUE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}-[1-9][0-9]{0,8}$")
_TRACKER_PROBE_READ_TOOLS = ("get_issue", "list_issue_statuses")
_TRACKER_PROBE_WRITE_TOOLS = (
    "save_issue",
    "save_comment",
    "create_issue",
    "update_issue",
    "create_comment",
)
_TRACKER_PROBE_STATE_KEYS = (
    "eligible_state",
    "in_progress_state",
    "success_state",
    "blocked_state",
)
_TRACKER_PROBE_DETAIL_LIMIT = 200
# The probe runs from the repository root, so a committed
# `.claude/settings.json` could otherwise add hooks that run shell commands or
# HTTP calls, or pre-approve other tools. `--setting-sources user` skips the
# project and local settings files, and the inline `--settings` turns off
# every hook (and any status line command) that remains. `--bare` is not used
# because it never reads OAuth credentials and skips the user's MCP servers.
_TRACKER_PROBE_SETTING_SOURCES = "user"
_TRACKER_PROBE_SETTINGS: dict[str, Any] = {"disableAllHooks": True}
# No "$schema" key: Claude Code's --json-schema validator rejects it.
TRACKER_PROBE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issue_found": {"type": "boolean"},
        "team_states": {"type": "array", "items": {"type": "string"}},
        "error": {"type": "string"},
    },
    "required": ["issue_found", "team_states", "error"],
    "additionalProperties": False,
}
TRACKER_PROBE_PROMPT = (
    "This is a read-only Linear access check. Call the get_issue tool for the "
    "Linear issue {issue}. Call the list_issue_statuses tool for the Linear "
    "team {team}. Do not create, update, comment on, or change anything. "
    "Answer with the requested JSON only: set issue_found to true only when "
    "get_issue returned the issue; set team_states to the name of every "
    "workflow state that list_issue_statuses returned for the team; set error "
    "to a short reason when a tool is missing, is refused, or fails, "
    "otherwise to an empty string."
)


def _probe_fail(message: str) -> DoctorCheck:
    return DoctorCheck("FAIL", _TRACKER_PROBE_CHECK_NAME, message)


def _probe_detail(text: str) -> str:
    """One line, at most 200 characters, ASCII-escaped, no control bytes.

    ``backslashreplace`` escapes only non-ASCII characters (which covers
    Unicode format characters), so ASCII control bytes such as ESC, BEL, and
    backspace are escaped separately; otherwise they would reach the terminal
    and could rewrite or hide the doctor's report.
    """
    collapsed = " ".join(text.split())[:_TRACKER_PROBE_DETAIL_LIMIT]
    escaped = collapsed.encode("ascii", "backslashreplace").decode("ascii")
    return "".join(
        f"\\x{ord(char):02x}" if ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in escaped
    )


def _first_claude_launch(
    dispatch_config: DispatchConfig,
) -> tuple[str, AgentConfig] | None:
    for label, agent_config in _role_launches(dispatch_config):
        if agent_config.provider == "claude":
            return label, agent_config
    return None


def _tracker_probe_command(
    dispatch_config: DispatchConfig,
    binary: str,
    model: str,
    issue: str,
) -> list[str]:
    """Build the restricted one-shot Claude argv for the tracker probe."""
    tracker = dispatch_config.tracker
    prefix = f"mcp__{tracker.mcp_server}__"
    allowed = [prefix + tool for tool in _TRACKER_PROBE_READ_TOOLS]
    disallowed = [
        *CLAUDE_DISALLOWED_TOOLS,
        *(prefix + tool for tool in _TRACKER_PROBE_WRITE_TOOLS),
    ]
    command = [
        binary,
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--allowedTools",
        ",".join(allowed),
        "--disallowedTools",
        ",".join(disallowed),
        "--setting-sources",
        _TRACKER_PROBE_SETTING_SOURCES,
        "--settings",
        json.dumps(_TRACKER_PROBE_SETTINGS),
    ]
    mcp_config_path = dispatch_config.tracker_claude_mcp_config_path
    if mcp_config_path is not None:
        command.extend(["--mcp-config", str(mcp_config_path), "--strict-mcp-config"])
    command.extend(
        [
            "--model",
            model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(TRACKER_PROBE_JSON_SCHEMA),
            "--no-session-persistence",
            "-p",
            TRACKER_PROBE_PROMPT.format(issue=issue, team=tracker.team),
        ]
    )
    return command


def _parse_tracker_probe_output(stdout: str) -> dict[str, Any] | None:
    """Return the schema-valid ``structured_output`` object, or None."""
    try:
        envelope = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    if not isinstance(envelope, dict) or envelope.get("is_error") is True:
        return None
    result = envelope.get("structured_output")
    if not isinstance(result, dict):
        return None
    if set(result) != {"issue_found", "team_states", "error"}:
        return None
    if not isinstance(result["issue_found"], bool):
        return None
    if not isinstance(result["error"], str):
        return None
    states = result["team_states"]
    if not isinstance(states, list) or not all(isinstance(s, str) for s in states):
        return None
    return result


def _check_tracker_probe(
    dispatch_config: DispatchConfig,
    repo_root: Path,
    issue: str,
    checks: list[DoctorCheck],
) -> None:
    """Run the opt-in ``--probe-tracker`` check and append one line.

    Gates run first and launch nothing on failure. When they pass, the first
    Claude launch in ``_role_launches`` order runs once with no built-in
    tools, only the tracker server's read tools approved, its write tools
    denied, project and local settings skipped, and hooks disabled. Nothing
    is written; the schema goes inline in argv.
    """
    tracker = dispatch_config.tracker
    if not tracker.enabled or tracker.mcp_server is None:
        checks.append(
            _probe_fail(
                "--probe-tracker needs tracker.provider: linear; the tracker "
                "provider is none"
            )
        )
        return
    issue = issue.strip()
    if _TRACKER_PROBE_ISSUE_RE.fullmatch(issue) is None:
        checks.append(
            _probe_fail(
                "--probe-tracker expects a Linear issue identifier such as "
                "ENG-123"
            )
        )
        return
    launch = _first_claude_launch(dispatch_config)
    if launch is None:
        checks.append(
            _probe_fail(
                "the probe runs through the Claude CLI and no configured role "
                "or fallback uses provider claude"
            )
        )
        return
    label, agent_config = launch
    binary = agent_config.binary or CLAUDE_BINARY
    model = agent_config.model or CLAUDE_MODEL
    could_not_start = f"could not start the probe with Claude CLI `{binary}`"
    if not _role_binary_available(binary, repo_root):
        checks.append(_probe_fail(f"{could_not_start}: binary was not found"))
        return

    command = _tracker_probe_command(dispatch_config, binary, model, issue)
    try:
        result = _run_logged_agent_command(
            label,
            command,
            cwd=repo_root,
            timeout_ms=_TRACKER_PROBE_TIMEOUT_MS,
            env=_build_claude_env(agent_config),
            log=None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(
            _probe_fail(f"{could_not_start}: {type(exc).__name__}")
        )
        return
    if result.exit_code != 0:
        first_line = next(
            (line for line in result.stderr.splitlines() if line.strip()), ""
        )
        if first_line.startswith("Process timed out after"):
            checks.append(
                _probe_fail(
                    f"{could_not_start}: it timed out after "
                    f"{_TRACKER_PROBE_TIMEOUT_MS // 1000} seconds"
                )
            )
            return
        detail = _probe_detail(first_line) if first_line else "no stderr output"
        checks.append(
            _probe_fail(
                f"Claude CLI (launch {label}) exited with code "
                f"{result.exit_code}: {detail}"
            )
        )
        return
    parsed = _parse_tracker_probe_output(result.stdout)
    if parsed is None:
        checks.append(
            _probe_fail(
                f"the Claude CLI (launch {label}) returned no usable probe "
                "result"
            )
        )
        return
    if parsed["issue_found"] is not True:
        detail = _probe_detail(parsed["error"]) or "no reason given"
        checks.append(
            _probe_fail(
                f"the Claude CLI (launch {label}) could not read issue "
                f"{issue}: {detail}"
            )
        )
        return
    configured: list[str] = []
    for key in _TRACKER_PROBE_STATE_KEYS:
        value = getattr(tracker, key)
        if value is not None and value not in configured:
            configured.append(value)
    team_states = {state.strip() for state in parsed["team_states"]}
    missing = [name for name in configured if name.strip() not in team_states]
    if missing:
        checks.append(
            _probe_fail(
                f"issue {issue} was read (launch {label}), but the team's "
                "workflow states do not include the configured state name(s): "
                + ", ".join(f"`{_probe_detail(name)}`" for name in missing)
            )
        )
        return
    checks.append(
        DoctorCheck(
            "PASS",
            _TRACKER_PROBE_CHECK_NAME,
            f"the Claude CLI (launch {label}) read issue {issue} and confirmed "
            f"{len(configured)} of {len(configured)} configured state names in "
            "the team's workflow states",
        )
    )


def _describe_missing(names: list[str]) -> str:
    if len(names) == 1:
        return f"{names[0]} is not available"
    if len(names) == 2:
        return f"neither {names[0]} nor {names[1]} is available"
    return "none of " + ", ".join(names) + " is available"


def _check_handoff(
    repo_root: Path,
    handoff_path: Path,
    *,
    dispatcher_commit_mode: bool,
    git_available: bool,
    checks: list[DoctorCheck],
    require_work_packet: bool = True,
    tracker_provider: str = "none",
) -> None:
    """Validate the current handoff the way the dispatch loop does.

    ``require_work_packet`` mirrors ``enforce_work_packet`` so a handoff the
    dispatcher would reject for a missing Work Packet also fails here.
    ``tracker_provider`` mirrors ``tracker.provider`` so Linear-mode lease
    rules apply the same way.
    """
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
            require_work_packet=require_work_packet,
            dispatcher_commit_mode=dispatcher_commit_mode,
            tracker_provider=tracker_provider,
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
