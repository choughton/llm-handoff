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
- the ``Verification command`` must exit 0 (``verification_failed``).

Each failed check is a distinct error prefix so the orchestrator and recovery
machinery can key on it. Running the verification command executes text the
planner wrote; see ``docs/ARCHITECTURE.md`` for the trust boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Literal

import yaml

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

WORK_PACKET_ERROR_PREFIXES = frozenset(
    {WORK_PACKET_MISSING_PREFIX, WORK_PACKET_UNPARSEABLE_PREFIX}
)
WORK_CLAIM_ERROR_PREFIXES = frozenset(
    {
        WORK_PACKET_MISSING_PREFIX,
        WORK_PACKET_UNPARSEABLE_PREFIX,
        SCOPE_SHA_NOT_HEAD_PREFIX,
        SCOPE_VIOLATION_PREFIX,
        UNCOMMITTED_CHANGES_PREFIX,
        VERIFICATION_FAILED_PREFIX,
        GIT_STATE_UNAVAILABLE_PREFIX,
    }
)

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
_PATH_SPLIT_RE = re.compile(r"[,;\n]+")
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


@dataclass(frozen=True)
class VerificationRun:
    command: str
    exit_code: int | None
    elapsed_seconds: float
    output_tail: str
    timeout_seconds: int
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
            return f"timed out after {self.timeout_seconds}s"
        return f"exited {self.exit_code} after {self.elapsed_seconds:.1f}s"

    def output_tail_lines(
        self,
        limit: int = VERIFICATION_OUTPUT_LOG_LINES,
    ) -> list[str]:
        lines = [
            line.rstrip() for line in self.output_tail.splitlines() if line.strip()
        ]
        return lines[-limit:]


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
    verification_runs: tuple[VerificationRun, ...]
    work_packet: WorkPacket | None
    work_packet_text: str | None

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def failed_prefixes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(error.split(":", 1)[0] for error in self.errors))


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
    paths: list[str] = []
    for raw_value in values:
        for token in _PATH_SPLIT_RE.split(raw_value):
            cleaned = token.strip().strip("`'\"").strip()
            if not cleaned or cleaned.lower() in _NONE_VALUES:
                continue
            for raw_part in cleaned.split():
                part = raw_part.strip("`'\",;")
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
    """Record HEAD and dirty files before an implementer dispatch."""

    return PreDispatchState(
        head_sha=git_head(repo_root),
        dirty_files=git_dirty_files(repo_root) or (),
    )


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


def run_verification_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: int,
) -> VerificationRun:
    """Run one Work Packet verification command through the shell.

    This executes agent-written text in the target repository. The implementer
    that produced the claim already ran there with provider permissions
    disabled, so the gate adds no new exposure, but the command is still
    bounded by ``timeout_seconds`` and its process tree is killed on timeout.
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
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **popen_kwargs)  # type: ignore[call-overload]
    except OSError as exc:
        return VerificationRun(
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
    return VerificationRun(
        command=command,
        exit_code=None if timed_out else process.returncode,
        elapsed_seconds=time.monotonic() - started,
        output_tail=_output_tail(output or ""),
        timeout_seconds=timeout_seconds,
        timed_out=timed_out,
    )


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _output_tail(output: str) -> str:
    return output[-VERIFICATION_OUTPUT_TAIL_CHARS:].rstrip()


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
) -> WorkClaimResult:
    """Check an implementer's ``ready_for_review`` claim against the packet.

    ``assignment_content`` is the handoff the implementer received (it holds
    the Work Packet); ``claim_content`` is the handoff it wrote back.
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

    scope_check, scope_error = _scope_sha_check(
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

    worktree_check, worktree_error = _worktree_check(
        repo_root,
        pre_dispatch_state=pre_dispatch_state,
        handoff_rel_path=handoff_rel_path,
    )
    checks.append(worktree_check)
    if worktree_error is not None:
        errors.append(worktree_error)

    verification_runs: list[VerificationRun] = []
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
    )


def _scope_sha_check(
    repo_root: Path,
    *,
    scope_sha: str | None,
    producer: str,
    head_sha: str,
    handoff_rel_path: str,
) -> tuple[WorkClaimCheck, str | None]:
    if not scope_sha:
        detail = (
            f"producer {producer} omitted scope_sha; the claim must name the "
            f"current HEAD {_short(head_sha)}."
        )
        return (
            WorkClaimCheck("scope_sha", "FAIL", detail),
            f"{SCOPE_SHA_NOT_HEAD_PREFIX}: {detail}",
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
        )

    depth = equivalents.index(resolved)
    if depth == 0:
        detail = f"scope_sha {_short(resolved)} is HEAD."
    else:
        detail = (
            f"scope_sha {_short(resolved)} is HEAD~{depth}; only handoff-only "
            "commits sit above it."
        )
    return WorkClaimCheck("scope_sha", "PASS", detail), None


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
    if new_dirty:
        detail = (
            f"working tree has uncommitted changes outside {handoff_rel_path}: "
            f"{', '.join(new_dirty)}; commit them so scope_sha contains the "
            "claimed work."
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
    runs: list[VerificationRun],
) -> tuple[WorkClaimCheck, str | None]:
    for command in packet.verification_commands:
        run = run_verification_command(
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

    Scope, SHA, worktree, and verification failures go back to the implementer
    as ``verified_fail``. A missing or unusable Work Packet is the planner's
    artifact, so that bounce routes to ``planner``. Unreadable Git state is an
    operator problem and escalates to ``user``.
    """

    prefixes = set(result.failed_prefixes)
    if GIT_STATE_UNAVAILABLE_PREFIX in prefixes:
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
    "PreDispatchState",
    "VerificationRun",
    "WorkClaimCheck",
    "WorkClaimResult",
    "WorkPacket",
    "WorkPacketError",
    "bounce_reason",
    "bounce_route_for",
    "build_bounce_handoff",
    "capture_pre_dispatch_state",
    "extract_work_packet_section",
    "parse_work_packet",
    "path_matches_entry",
    "run_verification_command",
    "verify_work_claim",
    "work_packet_errors",
]
