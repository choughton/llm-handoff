"""Deterministic work-claim gate for implementer completion claims.

When ``enforce_work_packet`` is enabled, the dispatcher checks a
``ready_for_review`` handoff from ``backend`` or ``frontend`` against the
planner's ``## Work Packet`` before it spends an auditor session:

- the packet must parse (``work_packet_missing`` / ``work_packet_unparseable``);
- ``scope_sha`` must be the current HEAD (``scope_sha_not_head``);
- every file changed since dispatch must stay inside ``Files in scope`` and
  outside ``Files out of bounds`` (``scope_violation``);
- the working tree must not hold new uncommitted changes
  (``uncommitted_changes``);
- configured scanners must exit 0 (``scanner_failed:<name>``,
  ``scanner_missing:<name>``, ``scanner_timeout:<name>``);
- the ``Verification command`` must exit 0 (``verification_failed``).

Each failed check is a distinct error prefix so the orchestrator and recovery
machinery can key on it. Running the verification command executes text the
planner wrote; scanner commands are operator-authored config. See
``docs/ARCHITECTURE.md`` for the trust boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import Literal

import yaml

from llm_handoff.agent_process import _kill_process_tree, _process_group_popen_kwargs
from llm_handoff.config import ScannerConfig, scanner_command_placeholders
from llm_handoff.router import (
    HandoffFrontmatterError,
    HandoffRouting,
    parse_handoff_frontmatter_text,
)


WORK_PACKET_MISSING_PREFIX = "work_packet_missing"
WORK_PACKET_UNPARSEABLE_PREFIX = "work_packet_unparseable"
SCOPE_SHA_NOT_HEAD_PREFIX = "scope_sha_not_head"
SCOPE_VIOLATION_PREFIX = "scope_violation"
UNCOMMITTED_CHANGES_PREFIX = "uncommitted_changes"
VERIFICATION_FAILED_PREFIX = "verification_failed"
GIT_STATE_UNAVAILABLE_PREFIX = "git_state_unavailable"
SCANNER_FAILED_PREFIX = "scanner_failed"
SCANNER_MISSING_PREFIX = "scanner_missing"
SCANNER_TIMEOUT_PREFIX = "scanner_timeout"
SCANNER_CONFIG_INVALID_PREFIX = "scanner_config_invalid"
NO_CHANGES_PREFIX = "no_changes"

WORK_PACKET_ERROR_PREFIXES = frozenset(
    {WORK_PACKET_MISSING_PREFIX, WORK_PACKET_UNPARSEABLE_PREFIX}
)
# Scanner prefixes carry the scanner name after a colon: ``scanner_failed:<name>``.
WORK_CLAIM_ERROR_PREFIXES = frozenset(
    {
        WORK_PACKET_MISSING_PREFIX,
        WORK_PACKET_UNPARSEABLE_PREFIX,
        SCOPE_SHA_NOT_HEAD_PREFIX,
        SCOPE_VIOLATION_PREFIX,
        UNCOMMITTED_CHANGES_PREFIX,
        VERIFICATION_FAILED_PREFIX,
        GIT_STATE_UNAVAILABLE_PREFIX,
        SCANNER_FAILED_PREFIX,
        SCANNER_MISSING_PREFIX,
        SCANNER_TIMEOUT_PREFIX,
        SCANNER_CONFIG_INVALID_PREFIX,
        NO_CHANGES_PREFIX,
    }
)
# POSIX sh and cmd.exe exit codes for "command not found".
_MISSING_EXECUTABLE_EXIT_CODES = frozenset({127, 9009})
_SHELL_SYNTAX_CHARS = frozenset("=&|;<>$()`")

DISPATCHER_VERIFICATION_HEADING = "## Dispatcher Verification"
WORK_PACKET_HEADING = "## Work Packet"
PRIOR_HANDBACK_HEADING = "## Prior Handback"
VERIFICATION_OUTPUT_TAIL_CHARS = 4000
VERIFICATION_OUTPUT_LOG_LINES = 20
HANDOFF_ONLY_COMMIT_WALK_LIMIT = 10
_GIT_TIMEOUT_SECONDS = 30
_SHORT_SHA_LENGTH = 12

_WORK_PACKET_HEADING_RE = re.compile(r"(?i)^(#{1,6})[ \t]+Work Packet[ \t]*$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+")
_PACKET_FIELD_RE = re.compile(r"^\s*[-*+]\s+\*\*([^*]+?):?\*\*:?\s*(.*?)\s*$")
_LIST_MARKER_RE = re.compile(r"^[-*+]\s+")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# A Work Packet path entry is either a quoted span (backticks, double or single
# quotes), kept verbatim so paths may contain spaces, or a bare token that ends
# at whitespace, a comma, or a semicolon.
_PATH_TOKEN_RE = re.compile(r"`([^`]+)`|\"([^\"]+)\"|'([^']+)'|([^\s,;`\"']+)")
_NONE_VALUES = frozenset({"none", "(none)", "none.", "n/a", "na", "-", "--"})
_GLOB_CHARS = frozenset("*?[")

_FIELD_ALIASES = {
    "objective": "objective",
    "files in scope": "files_in_scope",
    "file in scope": "files_in_scope",
    "files out of bounds": "files_out_of_bounds",
    "file out of bounds": "files_out_of_bounds",
    "files out-of-bounds": "files_out_of_bounds",
    "context": "context",
    "verification command": "verification_command",
    "verification commands": "verification_command",
    "expected next route": "expected_next_route",
}

CheckOutcome = Literal["PASS", "FAIL", "SKIP"]


class WorkPacketError(ValueError):
    """Raised when the ``## Work Packet`` is missing or cannot be used."""

    def __init__(self, prefix: str, detail: str, *, section_text: str | None = None):
        super().__init__(f"{prefix}: {detail}")
        self.prefix = prefix
        self.detail = detail
        self.section_text = section_text


@dataclass(frozen=True)
class WorkPacket:
    files_in_scope: tuple[str, ...]
    files_out_of_bounds: tuple[str, ...]
    verification_commands: tuple[str, ...]
    section_text: str


@dataclass(frozen=True)
class PreDispatchState:
    head_sha: str | None
    dirty_files: tuple[str, ...]
    # Working-tree content fingerprint of each pre-existing dirty file, so the
    # gate can tell when an implementer altered one further without committing.
    dirty_fingerprints: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CommandRun:
    """One shell command run by the gate: a verification command or a scanner."""

    command: str
    exit_code: int | None
    elapsed_seconds: float
    output_tail: str
    timeout_seconds: float
    timed_out: bool = False
    failure_detail: str | None = None

    @property
    def succeeded(self) -> bool:
        return (
            not self.timed_out and self.failure_detail is None and self.exit_code == 0
        )

    def describe_outcome(self) -> str:
        if self.failure_detail is not None:
            return self.failure_detail
        if self.timed_out:
            return f"timed out after {self.timeout_seconds:g}s"
        return f"exited {self.exit_code} after {self.elapsed_seconds:.1f}s"

    def output_tail_lines(
        self,
        limit: int = VERIFICATION_OUTPUT_LOG_LINES,
    ) -> list[str]:
        lines = [
            line.rstrip() for line in self.output_tail.splitlines() if line.strip()
        ]
        return lines[-limit:]


VerificationRun = CommandRun


@dataclass(frozen=True)
class ScannerContext:
    """Values available to scanner command placeholders for one claim."""

    repo_root: Path
    diff_range: str
    changed_files: tuple[str, ...]
    changed_py_files: tuple[str, ...]


@dataclass(frozen=True)
class RenderedScannerCommand:
    command: str | None
    empty_placeholder: str | None = None


class ScannerCommandError(ValueError):
    """Raised when a scanner command cannot be rendered for this claim."""


@dataclass(frozen=True)
class ScannerRun:
    name: str
    command: str
    outcome: CheckOutcome
    detail: str
    run: CommandRun | None = None
    required: bool = True
    missing: bool = False
    skipped: bool = False


@dataclass(frozen=True)
class WorkClaimCheck:
    name: str
    outcome: CheckOutcome
    detail: str


@dataclass(frozen=True)
class WorkClaimResult:
    checks: tuple[WorkClaimCheck, ...]
    errors: tuple[str, ...]
    pre_dispatch_head: str | None
    head_sha: str | None
    scope_sha: str | None
    changed_files: tuple[str, ...]
    verification_runs: tuple[CommandRun, ...]
    work_packet: WorkPacket | None
    work_packet_text: str | None
    scanner_runs: tuple[ScannerRun, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def failed_prefixes(self) -> tuple[str, ...]:
        # Errors are ``<prefix>: <detail>``; scanner prefixes embed the scanner
        # name (``scanner_failed:<name>``), so split on the first ": ".
        return tuple(dict.fromkeys(error.split(": ", 1)[0] for error in self.errors))


@dataclass(frozen=True)
class BounceRoute:
    next_agent: str
    status: str


# ---------------------------------------------------------------------------
# Work Packet parsing
# ---------------------------------------------------------------------------


def parse_work_packet(handoff_content: str) -> WorkPacket:
    """Parse the ``## Work Packet`` section or raise :class:`WorkPacketError`."""

    section = extract_work_packet_section(handoff_content)
    if section is None:
        raise WorkPacketError(
            WORK_PACKET_MISSING_PREFIX,
            "the assignment has no ## Work Packet section.",
        )

    fields = _parse_packet_fields(section)
    files_in_scope = _split_path_list(fields.get("files_in_scope", []))
    files_out_of_bounds = _split_path_list(fields.get("files_out_of_bounds", []))
    verification_commands = _split_commands(fields.get("verification_command", []))

    problems: list[str] = []
    if "files_in_scope" not in fields:
        problems.append("missing Files in scope")
    elif not files_in_scope:
        problems.append("empty Files in scope")
    if "files_out_of_bounds" not in fields:
        problems.append("missing Files out of bounds (write `none` when unused)")
    if "verification_command" not in fields:
        problems.append("missing Verification command")
    elif not verification_commands:
        problems.append("empty Verification command")
    if problems:
        raise WorkPacketError(
            WORK_PACKET_UNPARSEABLE_PREFIX,
            "the ## Work Packet cannot be enforced: " + ", ".join(problems) + ".",
            section_text=section,
        )

    return WorkPacket(
        files_in_scope=files_in_scope,
        files_out_of_bounds=files_out_of_bounds,
        verification_commands=verification_commands,
        section_text=section,
    )


def work_packet_errors(handoff_content: str) -> list[str]:
    """Return validator-style error strings for an unusable Work Packet."""

    try:
        parse_work_packet(handoff_content)
    except WorkPacketError as exc:
        return [str(exc)]
    return []


def extract_work_packet_section(handoff_content: str) -> str | None:
    """Return the verbatim ``## Work Packet`` section, heading included."""

    normalized = handoff_content.lstrip("﻿").replace("\r\n", "\n")
    lines = normalized.split("\n")
    in_fence = False
    start_index: int | None = None
    heading_level = 0
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if start_index is None:
            match = _WORK_PACKET_HEADING_RE.match(line)
            if match is not None:
                start_index = index
                heading_level = len(match.group(1))
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None and len(heading.group(1)) <= heading_level:
            return "\n".join(lines[start_index:index]).rstrip() + "\n"
    if start_index is None:
        return None
    return "\n".join(lines[start_index:]).rstrip() + "\n"


def _parse_packet_fields(section: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in section.split("\n")[1:]:
        if not line.strip():
            continue
        field_match = _PACKET_FIELD_RE.match(line)
        if field_match is not None:
            raw_name = " ".join(field_match.group(1).split()).lower()
            canonical = _FIELD_ALIASES.get(raw_name)
            if canonical is None:
                current = None
                continue
            current = canonical
            values = fields.setdefault(canonical, [])
            value = field_match.group(2).strip()
            if value:
                values.append(value)
            continue
        if current is None or not line[0].isspace():
            current = None
            continue
        if _FENCE_RE.match(line):
            continue
        continuation = _LIST_MARKER_RE.sub("", line.strip(), count=1).strip()
        if continuation:
            fields[current].append(continuation)
    return fields


def _split_path_list(values: list[str]) -> tuple[str, ...]:
    """Split Work Packet path values into entries.

    Quoted entries such as `` `src/my file.py` `` are kept whole; bare entries
    are separated by commas, semicolons, or whitespace.
    """

    paths: list[str] = []
    for raw_value in values:
        for match in _PATH_TOKEN_RE.finditer(raw_value):
            quoted = next(
                (group for group in match.groups()[:3] if group is not None), None
            )
            part = quoted.strip() if quoted is not None else match.group(4)
            if part and part.lower() not in _NONE_VALUES:
                paths.append(normalize_repo_path(part))
    return tuple(dict.fromkeys(path for path in paths if path))


def _split_commands(values: list[str]) -> tuple[str, ...]:
    commands: list[str] = []
    for raw_value in values:
        command = raw_value.strip()
        if command.startswith(("```", "~~~")):
            continue
        command = command.strip("`").strip()
        if not command or command.lower() in _NONE_VALUES:
            continue
        commands.append(command)
    return tuple(dict.fromkeys(commands))


def normalize_repo_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if normalized.endswith("/") and len(normalized) > 1:
        return normalized.rstrip("/") + "/"
    return normalized


def path_matches_entry(path: str, entry: str) -> bool:
    """True when ``path`` is covered by a Work Packet path entry.

    Entries may name a file, a directory (``dir/`` or ``dir``), or a glob.
    """

    if not entry:
        return False
    if entry.endswith("/"):
        prefix = entry.rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if any(char in _GLOB_CHARS for char in entry):
        return fnmatch.fnmatchcase(path, entry)
    return path == entry or path.startswith(entry + "/")


def _matches_any(path: str, entries: tuple[str, ...]) -> bool:
    return any(path_matches_entry(path, entry) for entry in entries)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def capture_pre_dispatch_state(repo_root: Path) -> PreDispatchState:
    """Record HEAD, dirty files, and their content before an implementer dispatch."""

    dirty_files = git_dirty_files(repo_root) or ()
    return PreDispatchState(
        head_sha=git_head(repo_root),
        dirty_files=dirty_files,
        dirty_fingerprints=tuple(
            (path, working_tree_fingerprint(repo_root, path)) for path in dirty_files
        ),
    )


def working_tree_fingerprint(repo_root: Path, path: str) -> str:
    """Hash a working-tree file's bytes; marks absent or unreadable files."""

    full_path = repo_root / path
    try:
        return hashlib.sha256(full_path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "<absent>"
    except IsADirectoryError:
        return "<directory>"
    except OSError:
        return "<unreadable>"


def git_head(repo_root: Path) -> str | None:
    output = _git_stdout(repo_root, ["rev-parse", "--verify", "HEAD"])
    if output is None:
        return None
    return output.strip() or None


def git_resolve_commit(repo_root: Path, ref: str) -> str | None:
    output = _git_stdout(
        repo_root,
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
    )
    if output is None:
        return None
    return output.strip() or None


def git_changed_files(
    repo_root: Path,
    base: str,
    target: str,
) -> tuple[str, ...] | None:
    entries = _git_nul_entries(
        repo_root,
        ["diff", "--name-only", "-z", f"{base}..{target}"],
    )
    if entries is None:
        return None
    return tuple(sorted({normalize_repo_path(entry) for entry in entries}))


def git_commit_files(repo_root: Path, sha: str) -> tuple[str, ...] | None:
    entries = _git_nul_entries(
        repo_root,
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha],
    )
    if entries is None:
        return None
    return tuple(sorted({normalize_repo_path(entry) for entry in entries}))


def git_dirty_files(repo_root: Path) -> tuple[str, ...] | None:
    entries = _git_nul_entries(
        repo_root,
        ["status", "--porcelain", "--untracked-files=all", "-z"],
    )
    if entries is None:
        return None
    files: set[str] = set()
    iterator = iter(entries)
    for entry in iterator:
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        files.add(normalize_repo_path(path))
        if "R" in code or "C" in code:
            next(iterator, None)
    return tuple(sorted(files))


def _git_nul_entries(repo_root: Path, args: list[str]) -> tuple[str, ...] | None:
    output = _git_stdout(repo_root, args)
    if output is None:
        return None
    return tuple(entry for entry in output.split("\0") if entry)


def _git_stdout(repo_root: Path, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def head_equivalent_commits(
    repo_root: Path,
    head_sha: str,
    handoff_rel_path: str,
    *,
    limit: int = HANDOFF_ONLY_COMMIT_WALK_LIMIT,
) -> tuple[str, ...]:
    """HEAD plus ancestors reachable only through handoff-only commits.

    Implementers may commit the handoff after writing ``scope_sha`` into it,
    which puts a commit that touches nothing but the handoff file above the
    claimed work. Those commits do not change the claimed tree, so the claim
    is still "at HEAD".
    """

    commits = [head_sha]
    current = head_sha
    for _ in range(limit):
        files = git_commit_files(repo_root, current)
        if files != (handoff_rel_path,):
            break
        parent = git_resolve_commit(repo_root, f"{current}^")
        if parent is None:
            break
        commits.append(parent)
        current = parent
    return tuple(commits)


# ---------------------------------------------------------------------------
# Verification command execution
# ---------------------------------------------------------------------------


def run_shell_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: float,
) -> CommandRun:
    """Run one command through the shell in ``cwd`` with a bounded timeout.

    The Work Packet verification command is agent-written text; scanner
    commands are operator-authored config. Either way the command runs in the
    target repository with the dispatcher's own permissions, bounded by
    ``timeout_seconds``, and its process tree is killed on timeout.
    """

    started = time.monotonic()
    popen_kwargs: dict[str, object] = {
        "shell": True,
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        **_process_group_popen_kwargs(),
    }
    try:
        process = subprocess.Popen(command, **popen_kwargs)  # type: ignore[call-overload]
    except OSError as exc:
        return CommandRun(
            command=command,
            exit_code=None,
            elapsed_seconds=time.monotonic() - started,
            output_tail="",
            timeout_seconds=timeout_seconds,
            failure_detail=f"could not start: {exc}",
        )

    timed_out = False
    output = ""
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            output = ""
    return CommandRun(
        command=command,
        exit_code=None if timed_out else process.returncode,
        elapsed_seconds=time.monotonic() - started,
        output_tail=_output_tail(output or ""),
        timeout_seconds=timeout_seconds,
        timed_out=timed_out,
    )


run_verification_command = run_shell_command


def _output_tail(output: str) -> str:
    return output[-VERIFICATION_OUTPUT_TAIL_CHARS:].rstrip()


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------


def shell_quote(value: str) -> str:
    """Quote one argument for the platform shell used by ``run_shell_command``."""

    if os.name == "nt":
        return '"' + value.replace('"', '""') + '"'
    return shlex.quote(value)


def render_scanner_command(
    command: str,
    context: ScannerContext,
) -> RenderedScannerCommand:
    """Substitute scanner placeholders for one claim.

    Returns a command of ``None`` when the command references
    ``{changed_files}`` or ``{changed_py_files}`` and that list is empty, so the
    caller can skip the scanner instead of running it on nothing. Raises
    :class:`ScannerCommandError` for placeholders that cannot be filled.
    """

    try:
        placeholders = scanner_command_placeholders(command)
    except ValueError as exc:
        raise ScannerCommandError(str(exc)) from exc

    lists = {
        "changed_files": context.changed_files,
        "changed_py_files": context.changed_py_files,
    }
    for name, paths in lists.items():
        if name in placeholders and not paths:
            return RenderedScannerCommand(command=None, empty_placeholder=name)

    values = {
        "diff_range": context.diff_range,
        "changed_files": " ".join(shell_quote(path) for path in context.changed_files),
        "changed_py_files": " ".join(
            shell_quote(path) for path in context.changed_py_files
        ),
        "repo_root": shell_quote(str(context.repo_root)),
    }
    try:
        rendered = command.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise ScannerCommandError(f"command could not be rendered: {exc}") from exc
    return RenderedScannerCommand(command=rendered)


def run_scanner(
    scanner: ScannerConfig,
    context: ScannerContext | None,
    *,
    runner: Callable[..., CommandRun] = run_shell_command,
) -> tuple[ScannerRun, str | None]:
    """Run one configured scanner and return its record plus any gate error.

    ``context`` is ``None`` when the scope checks did not pass; the scanner is
    then recorded as skipped. A non-zero exit is ``scanner_failed:<name>``, a
    timeout is ``scanner_timeout:<name>``, and a missing executable is
    ``scanner_missing:<name>`` when the scanner is required or a skip when it
    is optional.
    """

    name = scanner.name
    if context is None:
        return (
            ScannerRun(
                name=name,
                command=scanner.command,
                outcome="SKIP",
                detail="not run; the scope checks did not pass.",
                required=scanner.required,
            ),
            None,
        )

    try:
        rendered = render_scanner_command(scanner.command, context)
    except ScannerCommandError as exc:
        detail = str(exc)
        return (
            ScannerRun(
                name=name,
                command=scanner.command,
                outcome="FAIL",
                detail=detail,
                required=scanner.required,
            ),
            f"{SCANNER_CONFIG_INVALID_PREFIX}:{name}: {detail}",
        )
    if rendered.command is None:
        return (
            ScannerRun(
                name=name,
                command=scanner.command,
                outcome="SKIP",
                detail=(
                    f"skipped; {{{rendered.empty_placeholder}}} is empty for this "
                    "claim."
                ),
                required=scanner.required,
                skipped=True,
            ),
            None,
        )

    executable = _command_executable(rendered.command)
    if executable is not None and shutil.which(executable) is None:
        return _missing_scanner(scanner, rendered.command, executable)

    run = runner(
        rendered.command,
        cwd=context.repo_root,
        timeout_seconds=scanner.timeout_ms / 1000,
    )
    detail = f"`{run.command}` {run.describe_outcome()}."
    if run.timed_out:
        return (
            ScannerRun(
                name=name,
                command=run.command,
                outcome="FAIL",
                detail=detail,
                run=run,
                required=scanner.required,
            ),
            f"{SCANNER_TIMEOUT_PREFIX}:{name}: {detail}",
        )
    if executable is None and run.exit_code in _MISSING_EXECUTABLE_EXIT_CODES:
        return _missing_scanner(scanner, run.command, run.command.split()[0], run=run)
    if not run.succeeded:
        return (
            ScannerRun(
                name=name,
                command=run.command,
                outcome="FAIL",
                detail=detail,
                run=run,
                required=scanner.required,
            ),
            f"{SCANNER_FAILED_PREFIX}:{name}: {detail}",
        )
    return (
        ScannerRun(
            name=name,
            command=run.command,
            outcome="PASS",
            detail=detail,
            run=run,
            required=scanner.required,
        ),
        None,
    )


def _missing_scanner(
    scanner: ScannerConfig,
    command: str,
    executable: str,
    *,
    run: CommandRun | None = None,
) -> tuple[ScannerRun, str | None]:
    detail = (
        f"executable `{executable}` was not found on PATH; install it or set "
        "required: false."
    )
    if scanner.required:
        return (
            ScannerRun(
                name=scanner.name,
                command=command,
                outcome="FAIL",
                detail=detail,
                run=run,
                required=True,
                missing=True,
            ),
            f"{SCANNER_MISSING_PREFIX}:{scanner.name}: {detail}",
        )
    return (
        ScannerRun(
            name=scanner.name,
            command=command,
            outcome="SKIP",
            detail="skipped; " + detail,
            run=run,
            required=False,
            missing=True,
        ),
        None,
    )


def _command_executable(command: str) -> str | None:
    """Return the leading program of a shell command when it is a plain token."""

    stripped = command.lstrip()
    if not stripped:
        return None
    if stripped[0] in "\"'":
        end = stripped.find(stripped[0], 1)
        if end == -1:
            return None
        token = stripped[1:end]
    else:
        token = stripped.split(None, 1)[0]
    if not token or any(char in _SHELL_SYNTAX_CHARS for char in token):
        return None
    return token


def _scanner_context(
    *,
    repo_root: Path,
    pre_dispatch_head: str,
    scope_sha: str,
    changed_files: tuple[str, ...],
) -> ScannerContext:
    existing = tuple(path for path in changed_files if (repo_root / path).is_file())
    return ScannerContext(
        repo_root=repo_root,
        diff_range=f"{pre_dispatch_head}..{scope_sha}",
        changed_files=existing,
        changed_py_files=tuple(path for path in existing if path.endswith(".py")),
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def verify_work_claim(
    *,
    repo_root: Path,
    handoff_path: Path,
    assignment_content: str,
    claim_content: str,
    pre_dispatch_state: PreDispatchState,
    verification_timeout_seconds: int,
    scanners: Sequence[ScannerConfig] = (),
    require_new_commit: bool = False,
) -> WorkClaimResult:
    """Check an implementer's ``ready_for_review`` claim against the packet.

    ``assignment_content`` is the handoff the implementer received (it holds
    the Work Packet); ``claim_content`` is the handoff it wrote back.
    ``scanners`` run in order after the scope checks pass and before the
    verification command; every scanner runs even when an earlier one fails.
    ``require_new_commit`` fails a claim whose commits since dispatch change
    no file other than the handoff (``no_changes``).
    """

    handoff_rel_path = normalize_repo_path(Path(handoff_path).as_posix())
    checks: list[WorkClaimCheck] = []
    errors: list[str] = []
    frontmatter = _parse_frontmatter_or_none(claim_content)
    producer = (
        frontmatter.producer
        if frontmatter is not None and frontmatter.producer
        else "unknown producer"
    )
    scope_sha = frontmatter.scope_sha if frontmatter is not None else None
    work_packet_text = extract_work_packet_section(assignment_content)

    head_sha = git_head(repo_root)
    pre_dispatch_head = pre_dispatch_state.head_sha
    if head_sha is None or pre_dispatch_head is None:
        detail = (
            f"git HEAD could not be resolved in {repo_root}; "
            "the work-claim gate cannot verify the claim."
        )
        checks.append(WorkClaimCheck("git", "FAIL", detail))
        errors.append(f"{GIT_STATE_UNAVAILABLE_PREFIX}: {detail}")
        return WorkClaimResult(
            checks=tuple(checks),
            errors=tuple(errors),
            pre_dispatch_head=pre_dispatch_head,
            head_sha=head_sha,
            scope_sha=scope_sha,
            changed_files=(),
            verification_runs=(),
            work_packet=None,
            work_packet_text=work_packet_text,
        )

    packet: WorkPacket | None = None
    try:
        packet = parse_work_packet(assignment_content)
    except WorkPacketError as exc:
        checks.append(WorkClaimCheck("work_packet", "FAIL", exc.detail))
        errors.append(str(exc))
    else:
        checks.append(
            WorkClaimCheck(
                "work_packet",
                "PASS",
                f"{len(packet.files_in_scope)} in-scope path(s), "
                f"{len(packet.files_out_of_bounds)} out-of-bounds path(s), "
                f"{len(packet.verification_commands)} verification command(s).",
            )
        )

    scope_check, scope_error, resolved_scope_sha = _scope_sha_check(
        repo_root,
        scope_sha=scope_sha,
        producer=producer,
        head_sha=head_sha,
        handoff_rel_path=handoff_rel_path,
    )
    checks.append(scope_check)
    if scope_error is not None:
        errors.append(scope_error)

    changed_files = git_changed_files(repo_root, pre_dispatch_head, head_sha)
    if changed_files is None:
        detail = (
            f"git diff {_short(pre_dispatch_head)}..{_short(head_sha)} failed; "
            "changed files could not be listed."
        )
        checks.append(WorkClaimCheck("diff_scope", "FAIL", detail))
        errors.append(f"{GIT_STATE_UNAVAILABLE_PREFIX}: {detail}")
        changed_files = ()
    elif packet is None:
        checks.append(
            WorkClaimCheck(
                "diff_scope",
                "SKIP",
                "not checked; the Work Packet did not parse.",
            )
        )
    else:
        diff_check, diff_errors = _diff_scope_check(
            packet,
            changed_files,
            handoff_rel_path=handoff_rel_path,
        )
        checks.append(diff_check)
        errors.extend(diff_errors)

    if require_new_commit and not any(
        path != handoff_rel_path for path in changed_files
    ):
        detail = (
            "no file other than the handoff changed in commits since dispatch "
            f"({_short(pre_dispatch_head)}..{_short(head_sha)})."
        )
        checks.append(WorkClaimCheck("new_commit", "FAIL", detail))
        errors.append(f"{NO_CHANGES_PREFIX}: {detail}")

    worktree_check, worktree_error = _worktree_check(
        repo_root,
        pre_dispatch_state=pre_dispatch_state,
        handoff_rel_path=handoff_rel_path,
    )
    checks.append(worktree_check)
    if worktree_error is not None:
        errors.append(worktree_error)

    scanner_runs: list[ScannerRun] = []
    if scanners:
        scope_checks_passed = (
            scope_error is None
            and worktree_error is None
            and resolved_scope_sha is not None
            and any(
                check.name == "diff_scope" and check.outcome == "PASS"
                for check in checks
            )
        )
        context = (
            _scanner_context(
                repo_root=repo_root,
                pre_dispatch_head=pre_dispatch_head,
                scope_sha=resolved_scope_sha,
                changed_files=changed_files,
            )
            if scope_checks_passed and resolved_scope_sha is not None
            else None
        )
        for scanner in scanners:
            scanner_run, scanner_error = run_scanner(scanner, context)
            scanner_runs.append(scanner_run)
            checks.append(
                WorkClaimCheck(
                    f"scanner:{scanner.name}",
                    scanner_run.outcome,
                    scanner_run.detail,
                )
            )
            if scanner_error is not None:
                errors.append(scanner_error)

    verification_runs: list[CommandRun] = []
    if packet is None:
        checks.append(
            WorkClaimCheck(
                "verification",
                "SKIP",
                "not run; the Work Packet did not parse.",
            )
        )
    else:
        verification_check, verification_error = _verification_check(
            packet,
            repo_root=repo_root,
            timeout_seconds=verification_timeout_seconds,
            runs=verification_runs,
        )
        checks.append(verification_check)
        if verification_error is not None:
            errors.append(verification_error)

    return WorkClaimResult(
        checks=tuple(checks),
        errors=tuple(dict.fromkeys(errors)),
        pre_dispatch_head=pre_dispatch_head,
        head_sha=head_sha,
        scope_sha=scope_sha,
        changed_files=changed_files,
        verification_runs=tuple(verification_runs),
        work_packet=packet,
        work_packet_text=work_packet_text,
        scanner_runs=tuple(scanner_runs),
    )


def _scope_sha_check(
    repo_root: Path,
    *,
    scope_sha: str | None,
    producer: str,
    head_sha: str,
    handoff_rel_path: str,
) -> tuple[WorkClaimCheck, str | None, str | None]:
    """Return the check, its error, and the resolved full ``scope_sha``."""

    if not scope_sha:
        detail = (
            f"producer {producer} omitted scope_sha; the claim must name the "
            f"current HEAD {_short(head_sha)}."
        )
        return (
            WorkClaimCheck("scope_sha", "FAIL", detail),
            f"{SCOPE_SHA_NOT_HEAD_PREFIX}: {detail}",
            None,
        )

    resolved = git_resolve_commit(repo_root, scope_sha)
    if resolved is None:
        detail = (
            f"scope_sha `{scope_sha}` does not resolve to a commit; HEAD is "
            f"{_short(head_sha)}."
        )
        return (
            WorkClaimCheck("scope_sha", "FAIL", detail),
            f"{SCOPE_SHA_NOT_HEAD_PREFIX}: {detail}",
            None,
        )

    equivalents = head_equivalent_commits(repo_root, head_sha, handoff_rel_path)
    if resolved not in equivalents:
        detail = (
            f"scope_sha `{scope_sha}` resolves to {_short(resolved)}, but HEAD is "
            f"{_short(head_sha)}; a completion claim must point at the current "
            "HEAD, not at a pre-existing commit."
        )
        return (
            WorkClaimCheck("scope_sha", "FAIL", detail),
            f"{SCOPE_SHA_NOT_HEAD_PREFIX}: {detail}",
            resolved,
        )

    depth = equivalents.index(resolved)
    if depth == 0:
        detail = f"scope_sha {_short(resolved)} is HEAD."
    else:
        detail = (
            f"scope_sha {_short(resolved)} is HEAD~{depth}; only handoff-only "
            "commits sit above it."
        )
    return WorkClaimCheck("scope_sha", "PASS", detail), None, resolved


def _diff_scope_check(
    packet: WorkPacket,
    changed_files: tuple[str, ...],
    *,
    handoff_rel_path: str,
) -> tuple[WorkClaimCheck, list[str]]:
    candidates = tuple(path for path in changed_files if path != handoff_rel_path)
    out_of_bounds_hits = tuple(
        path for path in candidates if _matches_any(path, packet.files_out_of_bounds)
    )
    unlisted = tuple(
        path
        for path in candidates
        if path not in out_of_bounds_hits
        and not _matches_any(path, packet.files_in_scope)
    )
    errors: list[str] = []
    if out_of_bounds_hits:
        errors.append(
            f"{SCOPE_VIOLATION_PREFIX}: changed files listed under Files out of "
            f"bounds: {', '.join(out_of_bounds_hits)}."
        )
    if unlisted:
        errors.append(
            f"{SCOPE_VIOLATION_PREFIX}: changed files outside Files in scope: "
            f"{', '.join(unlisted)}."
        )
    if errors:
        detail = " ".join(error.split(": ", 1)[1] for error in errors)
        return WorkClaimCheck("diff_scope", "FAIL", detail), errors
    if not candidates:
        detail = "no files other than the handoff changed since dispatch."
    else:
        detail = (
            f"{len(candidates)} changed file(s) stayed inside Files in scope: "
            f"{', '.join(candidates)}."
        )
    return WorkClaimCheck("diff_scope", "PASS", detail), []


def _worktree_check(
    repo_root: Path,
    *,
    pre_dispatch_state: PreDispatchState,
    handoff_rel_path: str,
) -> tuple[WorkClaimCheck, str | None]:
    dirty_now = git_dirty_files(repo_root)
    if dirty_now is None:
        detail = "git status failed; uncommitted changes could not be listed."
        return (
            WorkClaimCheck("worktree", "FAIL", detail),
            f"{GIT_STATE_UNAVAILABLE_PREFIX}: {detail}",
        )
    new_dirty = tuple(
        path
        for path in dirty_now
        if path != handoff_rel_path and path not in pre_dispatch_state.dirty_files
    )
    # A file that was already dirty before dispatch is only acceptable if its
    # working-tree content is unchanged; otherwise the verification command
    # would run against edits that scope_sha does not contain.
    before = dict(pre_dispatch_state.dirty_fingerprints)
    altered_dirty = tuple(
        path
        for path in dirty_now
        if path != handoff_rel_path
        and path in before
        and working_tree_fingerprint(repo_root, path) != before[path]
    )
    problems: list[str] = []
    if new_dirty:
        problems.append(
            f"working tree has uncommitted changes outside {handoff_rel_path}: "
            f"{', '.join(new_dirty)}"
        )
    if altered_dirty:
        problems.append(
            "files that were already uncommitted before dispatch were modified "
            f"further without being committed: {', '.join(altered_dirty)}"
        )
    if problems:
        detail = (
            "; ".join(problems)
            + "; commit them so scope_sha contains the claimed work."
        )
        return (
            WorkClaimCheck("worktree", "FAIL", detail),
            f"{UNCOMMITTED_CHANGES_PREFIX}: {detail}",
        )
    return (
        WorkClaimCheck(
            "worktree",
            "PASS",
            f"no new uncommitted changes outside {handoff_rel_path}.",
        ),
        None,
    )


def _verification_check(
    packet: WorkPacket,
    *,
    repo_root: Path,
    timeout_seconds: int,
    runs: list[CommandRun],
) -> tuple[WorkClaimCheck, str | None]:
    for command in packet.verification_commands:
        run = run_shell_command(
            command,
            cwd=repo_root,
            timeout_seconds=timeout_seconds,
        )
        runs.append(run)
        if not run.succeeded:
            detail = f"`{run.command}` {run.describe_outcome()}."
            return (
                WorkClaimCheck("verification", "FAIL", detail),
                f"{VERIFICATION_FAILED_PREFIX}: {detail}",
            )
    detail = "; ".join(f"`{run.command}` {run.describe_outcome()}" for run in runs)
    return WorkClaimCheck("verification", "PASS", detail + "."), None


# ---------------------------------------------------------------------------
# Bounce handoff
# ---------------------------------------------------------------------------


def bounce_route_for(result: WorkClaimResult, *, implementer: str) -> BounceRoute:
    """Decide who receives a failed claim.

    Scope, SHA, worktree, scanner, and verification failures go back to the
    implementer as ``verified_fail``. A missing or unusable Work Packet is the
    planner's artifact, so that bounce routes to ``planner``. Unreadable Git
    state and an unrenderable scanner command are operator problems and
    escalate to ``user``.
    """

    prefixes = set(result.failed_prefixes)
    if GIT_STATE_UNAVAILABLE_PREFIX in prefixes or any(
        prefix.split(":", 1)[0] == SCANNER_CONFIG_INVALID_PREFIX for prefix in prefixes
    ):
        return BounceRoute(next_agent="user", status="escalate_to_user")
    if prefixes & WORK_PACKET_ERROR_PREFIXES:
        return BounceRoute(next_agent="planner", status="verified_fail")
    return BounceRoute(next_agent=implementer, status="verified_fail")


def bounce_reason(result: WorkClaimResult) -> str:
    return f"Dispatcher verification failed: {', '.join(result.failed_prefixes)}."


def build_bounce_handoff(
    *,
    claim_content: str,
    result: WorkClaimResult,
    implementer: str,
    route: BounceRoute,
    bounce_count: int | None,
) -> str:
    """Rewrite a failed claim into a deterministic bounce handoff.

    The frontmatter keeps the story metadata, routes to ``route.next_agent``
    with ``producer: validator``, and names the failed checks in a quoted
    ``reason``. The body leads with ``## Dispatcher Verification`` evidence,
    carries the original ``## Work Packet`` forward verbatim, and keeps the
    implementer's handback under ``## Prior Handback`` with demoted headings.
    """

    mapping, body = _split_frontmatter(claim_content)
    if mapping is None:
        mapping = _mapping_from_routing(_parse_frontmatter_or_none(claim_content))

    ordered: dict[str, object] = {"next_agent": route.next_agent}
    for key, value in mapping.items():
        if key in {"next_agent", "reason", "status", "producer", "bounce_count"}:
            continue
        if value is None:
            continue
        ordered[str(key)] = value
    ordered["status"] = route.status
    if bounce_count is not None:
        ordered["bounce_count"] = bounce_count
    ordered["producer"] = "validator"

    dumped = yaml.safe_dump(
        ordered,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    ).rstrip()
    frontmatter_lines = dumped.split("\n")
    frontmatter_lines.insert(1, "reason: " + json.dumps(bounce_reason(result)))

    sections = [
        _dispatcher_verification_section(result, implementer=implementer, route=route)
    ]
    if result.work_packet_text:
        sections.append(result.work_packet_text.rstrip() + "\n")
    prior_body = body.strip()
    if prior_body:
        sections.append(
            PRIOR_HANDBACK_HEADING + "\n\n" + _demote_headings(prior_body) + "\n"
        )

    return "---\n" + "\n".join(frontmatter_lines) + "\n---\n\n" + "\n".join(sections)


def _dispatcher_verification_section(
    result: WorkClaimResult,
    *,
    implementer: str,
    route: BounceRoute,
) -> str:
    lines = [DISPATCHER_VERIFICATION_HEADING, ""]
    if route.next_agent == implementer:
        lines.append(
            f"The dispatcher checked the `ready_for_review` claim from "
            f"`{implementer}` before dispatching the auditor and rejected it. "
            "Fix every FAIL below, commit the fix, set `scope_sha` to the new "
            "`git rev-parse HEAD`, and claim again with "
            "`status: ready_for_review`."
        )
    elif route.next_agent == "planner":
        lines.append(
            f"The dispatcher could not verify the `ready_for_review` claim from "
            f"`{implementer}` because the assignment did not carry a usable "
            "`## Work Packet`. Re-issue the assignment with a complete Work "
            "Packet (Files in scope, Files out of bounds, Verification command) "
            "so the claim can be checked before audit. The implementer's work "
            "has not been audited."
        )
    else:
        lines.append(
            "The dispatcher could not read Git state for the work-claim gate. "
            "Resolve the repository state, then update this handoff to resume "
            "dispatch."
        )
    lines.append("")
    lines.append("- **Verdict:** FAIL")
    lines.append(f"- **Failed checks:** {', '.join(result.failed_prefixes)}")
    lines.append(f"- **Pre-dispatch HEAD:** `{result.pre_dispatch_head or 'unknown'}`")
    lines.append(f"- **Current HEAD:** `{result.head_sha or 'unknown'}`")
    lines.append(f"- **Claimed scope_sha:** `{result.scope_sha or 'missing'}`")
    changed = ", ".join(f"`{path}`" for path in result.changed_files) or "none"
    lines.append(f"- **Changed files since dispatch:** {changed}")
    lines.append("- **Checks:**")
    for check in result.checks:
        lines.append(f"  - {check.outcome} `{check.name}`: {check.detail}")
    failed_scanner_runs = [
        scanner_run
        for scanner_run in result.scanner_runs
        if scanner_run.run is not None and scanner_run.outcome == "FAIL"
    ]
    if failed_scanner_runs:
        lines.extend(["", "### Scanner Output", ""])
        for scanner_run in failed_scanner_runs:
            assert scanner_run.run is not None
            lines.append(
                f"Scanner `{scanner_run.name}`: `{scanner_run.command}` "
                f"({scanner_run.run.describe_outcome()})"
            )
            lines.append("")
            lines.append("````text")
            lines.append(scanner_run.run.output_tail or "(no output)")
            lines.append("````")
            lines.append("")
    if result.verification_runs:
        lines.extend(["", "### Verification Output", ""])
        for run in result.verification_runs:
            lines.append(f"Command: `{run.command}` ({run.describe_outcome()})")
            lines.append("")
            lines.append("````text")
            lines.append(run.output_tail or "(no output)")
            lines.append("````")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def record_passed_verification(claim_content: str, result: WorkClaimResult) -> str:
    """Return the claim with a passing ``## Dispatcher Verification`` section.

    The auditor reads it to learn that the Work Packet's Verification command
    already passed on the claimed commit, so it does not run it again. An
    existing section (from an earlier bounce) is replaced.
    """

    section = passed_verification_section(result)
    normalized = claim_content.replace("\r\n", "\n")
    start = normalized.find(f"\n{DISPATCHER_VERIFICATION_HEADING}\n")
    if start == -1:
        return normalized.rstrip("\n") + "\n\n" + section
    start += 1
    next_heading = re.search(r"(?m)^## ", normalized[start + 1 :])
    end = len(normalized) if next_heading is None else start + 1 + next_heading.start()
    return normalized[:start] + section + ("\n" if end < len(normalized) else "") + normalized[end:]


def passed_verification_section(result: WorkClaimResult) -> str:
    """The passing ``## Dispatcher Verification`` section for a claim."""

    lines = [DISPATCHER_VERIFICATION_HEADING, ""]
    lines.append(
        "The dispatcher verified this `ready_for_review` claim before "
        "dispatching the auditor, and every check passed. The Verification "
        "command below already ran on the verified commit, outside any agent "
        "sandbox; the auditor does not need to run it again."
    )
    lines.append("")
    lines.append("- **Verdict:** PASS")
    verified = result.head_sha or result.scope_sha or "unknown"
    lines.append(f"- **Verified commit:** `{verified}`")
    lines.append(f"- **Pre-dispatch HEAD:** `{result.pre_dispatch_head or 'unknown'}`")
    changed = ", ".join(f"`{path}`" for path in result.changed_files) or "none"
    lines.append(f"- **Changed files since dispatch:** {changed}")
    lines.append("- **Checks:**")
    for check in result.checks:
        lines.append(f"  - {check.outcome} `{check.name}`: {check.detail}")
    for run in result.verification_runs:
        lines.append(
            f"- **Verification command:** `{run.command}` ({run.describe_outcome()})"
        )
    return "\n".join(lines).rstrip() + "\n"


def _split_frontmatter(content: str) -> tuple[dict[str, object] | None, str]:
    normalized = content.lstrip("﻿").replace("\r\n", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, normalized
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() != "---":
            continue
        try:
            loaded = yaml.safe_load("\n".join(lines[1:index]))
        except yaml.YAMLError:
            return None, normalized
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            return None, normalized
        return loaded, "\n".join(lines[index + 1 :])
    return None, normalized


def _mapping_from_routing(frontmatter: HandoffRouting | None) -> dict[str, object]:
    if frontmatter is None:
        return {}
    mapping = frontmatter.model_dump()
    if not mapping.get("remaining_stories"):
        mapping.pop("remaining_stories", None)
    else:
        mapping["remaining_stories"] = list(mapping["remaining_stories"])
    return {key: value for key, value in mapping.items() if value is not None}


def _demote_headings(body: str) -> str:
    demoted: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            demoted.append(line)
            continue
        heading = _HEADING_RE.match(line) if not in_fence else None
        if heading is not None and len(heading.group(1)) < 6:
            demoted.append("#" + line)
            continue
        demoted.append(line)
    return "\n".join(demoted)


def _parse_frontmatter_or_none(content: str) -> HandoffRouting | None:
    try:
        return parse_handoff_frontmatter_text(content)
    except HandoffFrontmatterError:
        return None


def _short(sha: str | None) -> str:
    if not sha:
        return "unknown"
    return sha[:_SHORT_SHA_LENGTH]


__all__ = [
    "WORK_CLAIM_ERROR_PREFIXES",
    "WORK_PACKET_ERROR_PREFIXES",
    "BounceRoute",
    "CommandRun",
    "PreDispatchState",
    "RenderedScannerCommand",
    "ScannerCommandError",
    "ScannerContext",
    "ScannerRun",
    "VerificationRun",
    "WorkClaimCheck",
    "WorkClaimResult",
    "WorkPacket",
    "WorkPacketError",
    "bounce_reason",
    "bounce_route_for",
    "build_bounce_handoff",
    "passed_verification_section",
    "record_passed_verification",
    "capture_pre_dispatch_state",
    "extract_work_packet_section",
    "parse_work_packet",
    "path_matches_entry",
    "render_scanner_command",
    "run_scanner",
    "run_shell_command",
    "run_verification_command",
    "shell_quote",
    "verify_work_claim",
    "work_packet_errors",
]
