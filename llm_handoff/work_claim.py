"""Deterministic work-claim gate for implementer completion claims.

When ``enforce_work_packet`` is enabled, the dispatcher checks a
``ready_for_review`` handoff from ``backend`` or ``frontend`` against the
planner's ``## Work Packet`` before it spends an auditor session:

- the packet must parse (``work_packet_missing`` / ``work_packet_unparseable``);
- ``scope_sha`` must be the current HEAD (``scope_sha_not_head``);
- the Verification Evidence ``Commit SHA verified`` value must resolve to
  ``scope_sha`` or to a handoff-only commit above it
  (``evidence_sha_mismatch``);
- every file changed since dispatch must stay inside ``Files in scope`` and
  outside ``Files out of bounds`` (``scope_violation``);
- some file other than the handoff must have changed in commits since
  dispatch (``no_changes``);
- the working tree must not hold new uncommitted changes
  (``uncommitted_changes``);
- configured scanners must exit 0 (``scanner_failed:<name>``,
  ``scanner_missing:<name>``, ``scanner_timeout:<name>``);
- when ``verification_allowed_prefixes`` is set, every ``Verification
  command`` must start with an allowed prefix and contain no shell control
  character (``verification_not_allowed``);
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
import subprocess
import time
from typing import Literal, Mapping

import yaml

from llm_handoff.agent_process import (
    BASE_AGENT_ENV_NAMES,
    PLATFORM_AGENT_ENV_NAMES,
    PROVIDER_AGENT_ENV_PREFIXES,
    _kill_process_tree,
    _start_isolated_process,
)
from llm_handoff.config import (
    VENDOR_API_KEY_ENV_NAMES,
    VERIFICATION_FORBIDDEN_CHARS,
    ScannerConfig,
    scanner_command_placeholders,
    verification_whitespace_problem,
)
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
EVIDENCE_SHA_MISMATCH_PREFIX = "evidence_sha_mismatch"
VERIFICATION_NOT_ALLOWED_PREFIX = "verification_not_allowed"

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
        EVIDENCE_SHA_MISMATCH_PREFIX,
        VERIFICATION_NOT_ALLOWED_PREFIX,
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
# Claude Code's protected directory: dontAsk mode denies every write under it.
CLAUDE_PROTECTED_DIRECTORY = ".claude"
# Documented writable exceptions under the project's own `.claude` folder.
CLAUDE_WORKTREES_DIRECTORY = "worktrees"
CLAUDE_AGENT_MEMORY_DIRECTORIES = frozenset({"agent-memory", "agent-memory-local"})
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
# Prose such as "every file in `pkg/` other than `pkg/mod.py`" names
# exceptions after these phrases; they are not out-of-bounds entries.
_EXCEPTION_PHRASE_RE = re.compile(
    r"\b(?:other\s+than|except(?:\s+for)?|excluding|apart\s+from|but\s+not)\b",
    re.IGNORECASE,
)
_BACKTICK_SPAN_RE = re.compile(r"`[^`]*`")
_PROSE_PATH_CHARS = frozenset("/.*?[")
_WRAPPED_PATH_FIELDS = frozenset({"files_in_scope", "files_out_of_bounds"})
# A Verification command line that is only a connective joins two commands in
# prose; it is not a command.
_PROSE_COMMAND_RE = re.compile(r"(?i)^(?:and|or|then|plus|also)[,.;:]?$")
# A Verification command line written as prose quotes its commands in code
# spans, such as "`cmd1`, then" or "`cmd1` and `cmd2`". A span is a run of
# backticks, its text, and a matching run.
_COMMAND_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# Text around the spans that only joins commands. "or" is left out: it names
# alternatives, so running every span would not match the prose.
_COMMAND_JOINER_RE = re.compile(r"(?i)^(?:[\s,;.:]|\b(?:and|then|plus|also)\b)*$")
# Only the top-level ``## Verification Evidence`` section counts; a bounce
# demotes the headings of older handbacks under ``## Prior Handback``.
_VERIFICATION_EVIDENCE_HEADING_RE = re.compile(
    r"(?im)^##[ \t]+Verification Evidence[ \t]*$"
)
_ANY_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]+")
# A Verification Evidence label may carry one parenthetical note before its
# colon, as in ``- **Commands run (this turn):**``. Shared by every parser of
# evidence labels so they agree on what counts as a field.
EVIDENCE_LABEL_NOTE = r"(?:[ \t]*\([^()\n*]*\))?"
_EVIDENCE_COMMIT_FIELD_RE = re.compile(
    rf"(?im)^-[ \t]+\*\*Commit SHA verified{EVIDENCE_LABEL_NOTE}:\*\*[ \t]*(.*)$"
)
_SHA_TOKEN_RE = re.compile(r"(?i)\b[0-9a-f]{7,40}\b")
_EVIDENCE_VALUE_DETAIL_CHARS = 80

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
    # Dirty files the previous implementer dispatch for the same story left
    # behind, unchanged since. They are that story's work, not operator edits,
    # so they are not in ``dirty_files``: the commit step may stage them and the
    # gate requires them in ``scope_sha``.
    carried_over: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImplementerLeftovers:
    """Files an implementer dispatch left uncommitted, with their fingerprints.

    The dispatcher keeps this in memory between cycles and saves it with
    :func:`save_implementer_leftovers` so it survives a dispatcher restart.
    ``story_key`` comes from :func:`carry_over_key`.
    """

    story_key: str | None
    fingerprints: tuple[tuple[str, str], ...]


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
    dispatcher_commit_mode: bool = False
    # The same Verification command failed on the previous bounce too, no
    # commit since then changed a file other than the handoff, and no other
    # check failed.
    repeated_verification_failure: bool = False
    # The first Verification command that ``verification_allowed_prefixes``
    # rejected; set only when the check failed ``verification_not_allowed``.
    rejected_verification_command: str | None = None

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
    files_out_of_bounds = _split_path_list(
        fields.get("files_out_of_bounds", []), drop_exceptions=True
    )
    verification_commands, prose_lines = _split_commands(
        fields.get("verification_command", [])
    )

    problems: list[str] = []
    if "files_in_scope" not in fields:
        problems.append("missing Files in scope")
    elif not files_in_scope:
        problems.append("empty Files in scope")
    if "files_out_of_bounds" not in fields:
        problems.append("missing Files out of bounds (write `none` when unused)")
    if "verification_command" not in fields:
        problems.append("missing Verification command")
    elif not verification_commands and not prose_lines:
        problems.append("empty Verification command")
    if prose_lines:
        problems.append(
            "Verification command has lines that are not commands: "
            + ", ".join(f"`{line}`" for line in prose_lines)
            + " (every line runs as a command; write one command per nested "
            "list item with no prose around it, and put notes in the Context)"
        )
    # The narrowest entry decides scope, so an entry listed under both fields
    # resolves to out of bounds: the implementer may not change it, and the
    # dispatcher will not commit it. A packet cannot limit edits within a file;
    # the planner must pick one list and state any limit in the Objective.
    # ``src`` and ``src/`` name the same directory, so compare without the slash.
    out_of_bounds_entries = {entry.rstrip("/") for entry in files_out_of_bounds}
    conflicts = [
        path for path in files_in_scope if path.rstrip("/") in out_of_bounds_entries
    ]
    if conflicts:
        problems.append(
            "listed under both Files in scope and Files out of bounds: "
            + ", ".join(f"`{path}`" for path in conflicts)
            + " (keep each path in one list; state limits within a file in "
            "the Objective)"
        )
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


def parse_work_packet_scope(handoff_content: str) -> WorkPacket | None:
    """Parse only the path lists of the ``## Work Packet``, never raising.

    Checks that need scope but do not enforce the whole packet, such as the
    protected-file check, read Files in scope and Files out of bounds the same
    way :func:`parse_work_packet` does. None means there is no Work Packet.
    """

    section = extract_work_packet_section(handoff_content)
    if section is None:
        return None
    fields = _parse_packet_fields(section)
    return WorkPacket(
        files_in_scope=_split_path_list(fields.get("files_in_scope", [])),
        files_out_of_bounds=_split_path_list(
            fields.get("files_out_of_bounds", []), drop_exceptions=True
        ),
        verification_commands=(),
        section_text=section,
    )


def parse_work_packet_verification_commands(
    handoff_content: str,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Return the Work Packet's Verification commands and prose lines.

    Unlike :func:`parse_work_packet`, this ignores the path lists, so a
    verification-only packet (an epic close with no files in scope) still
    yields its commands. None means there is no Work Packet or it has no
    Verification command field.
    """

    section = extract_work_packet_section(handoff_content)
    if section is None:
        return None
    fields = _parse_packet_fields(section)
    if "verification_command" not in fields:
        return None
    return _split_commands(fields["verification_command"])


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
        stripped = line.strip()
        continuation = _LIST_MARKER_RE.sub("", stripped, count=1).strip()
        if not continuation:
            continue
        values = fields[current]
        if current in _WRAPPED_PATH_FIELDS and values and continuation == stripped:
            # A wrapped line without a list marker continues the previous
            # value, so prose such as "other than" stays with its path.
            values[-1] = f"{values[-1]} {continuation}"
        else:
            values.append(continuation)
    return fields


def _split_path_list(
    values: list[str], *, drop_exceptions: bool = False
) -> tuple[str, ...]:
    """Split Work Packet path values into entries.

    Quoted entries such as `` `src/my file.py` `` are kept whole; bare entries
    are separated by commas, semicolons, or whitespace. A value that uses
    backticks or an exception phrase is read as prose, such as "every file in
    `pkg/` other than `pkg/mod.py`": there, a bare word counts only when it
    looks like a path (it has a ``/``, a ``.``, or a glob character), so words
    such as "every file in" never become entries. With ``drop_exceptions``,
    entries after an exception phrase ("other than", "except", "excluding",
    "apart from", "but not") are dropped up to the next comma or semicolon,
    because the prose excludes them.
    """

    paths: list[str] = []
    for raw_value in values:
        unquoted = _BACKTICK_SPAN_RE.sub(
            lambda span: " " * len(span.group(0)), raw_value
        )
        prose = unquoted != raw_value or (
            _EXCEPTION_PHRASE_RE.search(unquoted) is not None
        )
        excluded: list[tuple[int, int]] = []
        if drop_exceptions:
            clause_start = 0
            for clause_end in [
                *(sep.start() for sep in re.finditer(r"[,;]", unquoted)),
                len(unquoted),
            ]:
                exception = _EXCEPTION_PHRASE_RE.search(
                    unquoted, clause_start, clause_end
                )
                if exception is not None:
                    excluded.append((exception.start(), clause_end))
                clause_start = clause_end + 1
        for match in _PATH_TOKEN_RE.finditer(raw_value):
            if any(start <= match.start() < end for start, end in excluded):
                continue
            quoted = next(
                (group for group in match.groups()[:3] if group is not None), None
            )
            part = quoted.strip() if quoted is not None else match.group(4)
            if prose and quoted is None:
                part = part.lstrip("(").rstrip(").:")
                if not any(char in _PROSE_PATH_CHARS for char in part):
                    continue
            if part and part.lower() not in _NONE_VALUES:
                paths.append(normalize_repo_path(part))
    return tuple(dict.fromkeys(path for path in paths if path))


def _split_commands(values: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split Verification command values into commands and prose lines.

    Every line becomes one command, so a line that is clearly prose (a lone
    connective such as ``and``, or a lead-in ending in ``:``) is returned in
    the second tuple instead; the gate would otherwise run it as a command.
    A line that quotes commands in code spans, such as ``"`cmd1`, then"`` or
    ``"`cmd1` and `cmd2`"``, yields each span as a command when the text
    around the spans only joins them; any other text around a span makes the
    line prose, because which part is the command is ambiguous.
    """

    commands: list[str] = []
    prose: list[str] = []
    for raw_value in values:
        line = raw_value.strip()
        if line.startswith(("```", "~~~")):
            continue
        spans = list(_COMMAND_SPAN_RE.finditer(line))
        if spans:
            around = _COMMAND_SPAN_RE.sub(" ", line)
            if not _COMMAND_JOINER_RE.match(around):
                prose.append(line)
                continue
            candidates = [span.group(2).strip() for span in spans]
        else:
            candidates = [line.strip("`").strip()]
        for command in candidates:
            if not command or command.lower() in _NONE_VALUES:
                continue
            if _PROSE_COMMAND_RE.match(command) or command.endswith(":"):
                prose.append(command)
                continue
            commands.append(command)
    return tuple(dict.fromkeys(commands)), tuple(dict.fromkeys(prose))


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


def _entry_specificity(path: str, entry: str) -> tuple[int, int] | None:
    """Rank how narrowly ``entry`` names ``path``; None when it does not match.

    An exact file entry outranks a directory or glob; among those, the longer
    entry is narrower.
    """

    if not path_matches_entry(path, entry):
        return None
    if path == entry:
        return (2, len(entry))
    literal = "".join(char for char in entry if char not in _GLOB_CHARS)
    return (1, len(literal))


def _best_specificity(path: str, entries: tuple[str, ...]) -> tuple[int, int] | None:
    ranks = [
        rank
        for rank in (_entry_specificity(path, entry) for entry in entries)
        if rank is not None
    ]
    return max(ranks) if ranks else None


def path_is_out_of_bounds(path: str, packet: WorkPacket) -> bool:
    """True when ``Files out of bounds`` covers ``path``.

    The narrowest matching entry decides, so ``Files in scope:
    pkg/mod.py`` with ``Files out of bounds: pkg/`` puts ``pkg/mod.py`` in
    scope and every other file under ``pkg/`` out of bounds. When the two
    lists name a path equally narrowly, out of bounds wins.
    """

    out_of_bounds = _best_specificity(path, packet.files_out_of_bounds)
    if out_of_bounds is None:
        return False
    in_scope = _best_specificity(path, packet.files_in_scope)
    return in_scope is None or out_of_bounds >= in_scope


def path_is_in_scope(path: str, packet: WorkPacket) -> bool:
    """True when ``Files in scope`` covers ``path`` and it is not out of bounds."""

    return _matches_any(path, packet.files_in_scope) and not path_is_out_of_bounds(
        path, packet
    )


def claude_protected_scope_entries(packet: WorkPacket) -> tuple[str, ...]:
    """Return the ``Files in scope`` entries that sit under a ``.claude`` folder.

    Claude Code treats any ``.claude`` directory as a protected path, at the
    repository root or nested such as ``examples/app/.claude/agents/x.md``. In
    ``dontAsk`` mode it denies writes there before it reads the allow rules, so
    a Claude role launched that way cannot change these entries. The match is
    on whole path segments and ignores case, so ``.claude-notes/`` does not
    count. The writable exceptions Claude Code documents under the project's
    own ``.claude`` folder (the repository root, where the role runs) are left
    out: its git worktrees under ``.claude/worktrees/`` and Markdown entries
    in a subagent memory folder (``.claude/agent-memory/`` or
    ``.claude/agent-memory-local/``).
    """

    return tuple(
        entry
        for entry in packet.files_in_scope
        if _claude_protected_entry(entry)
    )


def _claude_protected_entry(entry: str) -> bool:
    segments = [segment.lower() for segment in entry.split("/")]
    if CLAUDE_PROTECTED_DIRECTORY not in segments:
        return False
    if segments[0] != CLAUDE_PROTECTED_DIRECTORY or len(segments) < 3:
        return True
    if segments[1] == CLAUDE_WORKTREES_DIRECTORY:
        return False
    return not (
        segments[1] in CLAUDE_AGENT_MEMORY_DIRECTORIES
        and segments[-1].endswith(".md")
    )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def capture_pre_dispatch_state(
    repo_root: Path,
    *,
    leftovers: ImplementerLeftovers | None = None,
    story_key: str | None = None,
) -> PreDispatchState:
    """Record HEAD, dirty files, and their content before an implementer dispatch.

    Files in ``leftovers`` from an earlier dispatch with the same ``story_key``
    (see :func:`carry_over_key`) whose content has not changed since are carried
    over as implementer work instead of being recorded as pre-existing dirty
    files.
    """

    head_sha, dirty = git_head_and_dirty_files(repo_root)
    dirty_files = dirty or ()
    fingerprints = {
        path: working_tree_fingerprint(repo_root, path) for path in dirty_files
    }
    carried: tuple[str, ...] = ()
    if leftovers is not None and story_key and leftovers.story_key == story_key:
        carried = tuple(
            sorted(
                path
                for path, fingerprint in leftovers.fingerprints
                if fingerprints.get(path) == fingerprint
            )
        )
    dirty_files = tuple(path for path in dirty_files if path not in carried)
    return PreDispatchState(
        head_sha=head_sha,
        dirty_files=dirty_files,
        dirty_fingerprints=tuple((path, fingerprints[path]) for path in dirty_files),
        carried_over=carried,
    )


def capture_implementer_leftovers(
    repo_root: Path,
    *,
    handoff_path: Path,
    pre_dispatch_state: PreDispatchState,
    story_key: str | None,
) -> ImplementerLeftovers:
    """Record what an implementer dispatch left uncommitted, minus operator edits."""

    handoff_rel_path = normalize_repo_path(Path(handoff_path).as_posix())
    operator_dirty = set(pre_dispatch_state.dirty_files)
    leftover_paths = tuple(
        path
        for path in git_dirty_files(repo_root) or ()
        if path != handoff_rel_path and path not in operator_dirty
    )
    return ImplementerLeftovers(
        story_key=story_key,
        fingerprints=tuple(
            (path, working_tree_fingerprint(repo_root, path)) for path in leftover_paths
        ),
    )


def carry_over_key(handoff_content: str) -> str | None:
    """Identify the assignment an implementer dispatch works on.

    The key is the handoff's ``story_id``. ``story_id`` is optional, so without
    one the key is a hash of the ``## Work Packet`` section, which a bounce
    carries forward verbatim. None means neither exists, and nothing is carried
    over.
    """

    try:
        frontmatter = parse_handoff_frontmatter_text(handoff_content)
    except HandoffFrontmatterError:
        frontmatter = None
    story_id = frontmatter.story_id if frontmatter is not None else None
    if story_id:
        return f"story:{story_id}"
    section = extract_work_packet_section(handoff_content)
    if section is None:
        return None
    return "work-packet:" + hashlib.sha256(section.encode("utf-8")).hexdigest()


def prune_implementer_leftovers(
    repo_root: Path, leftovers: ImplementerLeftovers | None
) -> ImplementerLeftovers | None:
    """Drop leftovers that were committed or changed since they were recorded.

    Only a file that is still dirty with the recorded content can be carried
    over, so the rest no longer belong in the record. None when nothing is left.
    """

    if leftovers is None or not leftovers.fingerprints:
        return None
    dirty_files = git_dirty_files(repo_root)
    if dirty_files is None:
        # Git could not list dirty files; keep the record unchanged.
        return leftovers
    dirty = set(dirty_files)
    kept = tuple(
        (path, fingerprint)
        for path, fingerprint in leftovers.fingerprints
        if path in dirty and working_tree_fingerprint(repo_root, path) == fingerprint
    )
    if not kept:
        return None
    return ImplementerLeftovers(story_key=leftovers.story_key, fingerprints=kept)


def load_implementer_leftovers(state_path: Path) -> ImplementerLeftovers | None:
    """Read a carry-over record saved by :func:`save_implementer_leftovers`.

    A missing, unreadable, or malformed file loads nothing.
    """

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    story_key = payload.get("story_key")
    files = payload.get("fingerprints")
    if not isinstance(story_key, str) or not story_key or not isinstance(files, dict):
        return None
    fingerprints = tuple(
        sorted(
            (path, fingerprint)
            for path, fingerprint in files.items()
            if isinstance(path, str) and path and isinstance(fingerprint, str)
        )
    )
    if not fingerprints:
        return None
    return ImplementerLeftovers(story_key=story_key, fingerprints=fingerprints)


def save_implementer_leftovers(
    state_path: Path, leftovers: ImplementerLeftovers | None
) -> None:
    """Write the carry-over record, or delete it when nothing can carry over.

    A record without a story key never matches a later dispatch, so it is not
    kept. An ``OSError`` from the write propagates to the caller.
    """

    if leftovers is None or not leftovers.story_key or not leftovers.fingerprints:
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "story_key": leftovers.story_key,
        "fingerprints": dict(sorted(leftovers.fingerprints)),
    }
    temporary_path = state_path.with_name(f"{state_path.name}.tmp")
    temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(state_path)


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


# Porcelain v2 entries: ordinary ("1"), renamed or copied ("2", followed by
# the original path as its own entry), unmerged ("u"), and untracked ("?").
# The number is how many space-separated fields precede the path.
_STATUS_ARGS = ["status", "--porcelain=v2", "--untracked-files=all", "-z"]
_STATUS_FIELDS_BEFORE_PATH = {"1": 8, "2": 9, "u": 10, "?": 1}


def git_dirty_files(repo_root: Path) -> tuple[str, ...] | None:
    entries = _git_nul_entries(repo_root, _STATUS_ARGS)
    if entries is None:
        return None
    return _status_paths(entries)[1]


def git_head_and_dirty_files(
    repo_root: Path,
) -> tuple[str | None, tuple[str, ...] | None]:
    """Return :func:`git_head` and :func:`git_dirty_files` from one ``git status``.

    When ``git status`` fails, HEAD comes from :func:`git_head` and the dirty
    files are None, as the two separate calls would return.
    """

    entries = _git_nul_entries(
        repo_root, [*_STATUS_ARGS, "--branch", "--no-ahead-behind"]
    )
    if entries is None:
        return git_head(repo_root), None
    oid, files = _status_paths(entries)
    if oid is None:
        return git_head(repo_root), files
    # An unborn branch has no HEAD commit, so `git rev-parse` fails too.
    return (None if oid == "(initial)" else oid), files


def _status_paths(
    entries: tuple[str, ...],
) -> tuple[str | None, tuple[str, ...]]:
    """Parse ``git status --porcelain=v2 -z`` into the ``# branch.oid`` header
    value (None without ``--branch``) and the sorted dirty paths."""

    oid: str | None = None
    files: set[str] = set()
    iterator = iter(entries)
    for entry in iterator:
        if entry.startswith("# branch.oid "):
            oid = entry[len("# branch.oid ") :].strip() or None
            continue
        fields = _STATUS_FIELDS_BEFORE_PATH.get(entry[:1])
        if fields is None:
            continue
        parts = entry.split(" ", fields)
        if len(parts) <= fields:
            continue
        files.add(normalize_repo_path(parts[fields]))
        if entry[:1] == "2":
            next(iterator, None)
    return oid, tuple(sorted(files))


def git_path_is_ignored(repo_root: Path, path: str) -> bool:
    """True when Git ignores ``path``; False when it does not or Git fails."""

    return _git_stdout(repo_root, ["check-ignore", "-q", "--", path]) is not None


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

    return _head_equivalent_commits(
        _CommitLookups(repo_root), head_sha, handoff_rel_path, limit=limit
    )


def _head_equivalent_commits(
    lookups: _CommitLookups,
    head_sha: str,
    handoff_rel_path: str,
    *,
    limit: int = HANDOFF_ONLY_COMMIT_WALK_LIMIT,
) -> tuple[str, ...]:
    commits = [head_sha]
    current = head_sha
    for _ in range(limit):
        files = lookups.commit_files(current)
        if files != (handoff_rel_path,):
            break
        parent = lookups.resolve_commit(f"{current}^")
        if parent is None:
            break
        commits.append(parent)
        current = parent
    return tuple(commits)


class _CommitLookups:
    """Memoize commit lookups for one work-claim check.

    The gate resolves the same refs and lists the same commits' files more
    than once (scope_sha and the evidence SHA usually both name HEAD), and
    nothing commits while it checks, so each lookup runs Git once.
    """

    def __init__(self, repo_root: Path, *, head_sha: str | None = None) -> None:
        self.repo_root = repo_root
        self._resolved: dict[str, str | None] = {}
        self._files: dict[str, tuple[str, ...] | None] = {}
        if head_sha is not None:
            # HEAD is always a commit, so its SHA resolves to itself.
            self._resolved[head_sha] = head_sha

    def resolve_commit(self, ref: str) -> str | None:
        if ref not in self._resolved:
            self._resolved[ref] = git_resolve_commit(self.repo_root, ref)
        return self._resolved[ref]

    def commit_files(self, sha: str) -> tuple[str, ...] | None:
        if sha not in self._files:
            self._files[sha] = git_commit_files(self.repo_root, sha)
        return self._files[sha]


def evidence_commit_value(handoff_content: str) -> str | None:
    """Return the ``Commit SHA verified`` value from ``## Verification Evidence``.

    None means the section or the field is missing. Fenced code is not
    special-cased; the field is a single bullet line.
    """

    normalized = handoff_content.lstrip("﻿").replace("\r\n", "\n")
    heading = _VERIFICATION_EVIDENCE_HEADING_RE.search(normalized)
    if heading is None:
        return None
    next_heading = _ANY_HEADING_RE.search(normalized, heading.end())
    end = next_heading.start() if next_heading is not None else len(normalized)
    field = _EVIDENCE_COMMIT_FIELD_RE.search(normalized, heading.end(), end)
    if field is None:
        return None
    return field.group(1).strip()


def evidence_sha_token(value: str) -> str | None:
    """Return the first 7-40 character hex token in an evidence value.

    Agents often annotate the SHA, as in ``abc1234 (implementation commit)``,
    so the first hex token is the SHA the claim names.
    """

    match = _SHA_TOKEN_RE.search(value)
    return match.group(0) if match is not None else None


# ---------------------------------------------------------------------------
# Verification command execution
# ---------------------------------------------------------------------------


_WORK_CLAIM_ENV_NAMES = BASE_AGENT_ENV_NAMES | PLATFORM_AGENT_ENV_NAMES
# Provider-owned auth and config prefixes never reach gate shell commands,
# even when an operator lists such a name as a passthrough.
_WORK_CLAIM_BLOCKED_ENV_PREFIXES = tuple(
    sorted(
        {
            prefix
            for prefixes in PROVIDER_AGENT_ENV_PREFIXES.values()
            for prefix in prefixes
        }
    )
)


def build_work_claim_env(
    passthrough: tuple[str, ...] = (),
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment for a work-claim gate shell command.

    Only the base and platform process-plumbing names from
    ``agent_process`` plus the ``passthrough`` names are kept. Names are
    compared case-insensitively, as Windows environment names are, and the
    original key spelling is preserved. Vendor API keys (including
    ``GEMINI_API_KEY`` and ``GOOGLE_API_KEY``) and provider-owned variables
    (``CLAUDE_``, ``CODEX_``) are never passed, even when listed in
    ``passthrough``. Other ``GEMINI_*`` names are not blocked; they pass only
    when the operator lists them in ``passthrough``.
    """

    source = os.environ if environ is None else environ
    allowed_names = _WORK_CLAIM_ENV_NAMES | {name.upper() for name in passthrough}
    env: dict[str, str] = {}
    for key, value in source.items():
        upper_key = key.upper()
        if upper_key in VENDOR_API_KEY_ENV_NAMES:
            continue
        if upper_key.startswith(_WORK_CLAIM_BLOCKED_ENV_PREFIXES):
            continue
        if upper_key in allowed_names:
            env[key] = value
    return env


def run_shell_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: float,
    env_passthrough: tuple[str, ...] = (),
) -> CommandRun:
    """Run one command through the shell in ``cwd`` with a bounded timeout.

    The Work Packet verification command is agent-written text; scanner
    commands are operator-authored config. Either way the command runs in the
    target repository with the dispatcher's own permissions, bounded by
    ``timeout_seconds``, and its process tree is killed on timeout. The
    command gets the allowlisted environment from
    :func:`build_work_claim_env`, not the dispatcher's full environment;
    ``env_passthrough`` names extra variables to pass through.
    """

    started = time.monotonic()
    popen_kwargs: dict[str, object] = {
        "shell": True,
        "cwd": str(cwd),
        "env": build_work_claim_env(env_passthrough),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    timed_out = False
    output = ""
    try:
        process = _start_isolated_process(command, **popen_kwargs)
    except OSError as exc:
        return CommandRun(
            command=command,
            exit_code=None,
            elapsed_seconds=time.monotonic() - started,
            output_tail="",
            timeout_seconds=timeout_seconds,
            failure_detail=f"could not start: {exc}",
        )

    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            output = ""
    except BaseException:
        # The command runs in its own process group, so a console Ctrl+C
        # no longer reaches it; take its tree down before re-raising.
        _kill_process_tree(process)
        raise
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
    env_passthrough: tuple[str, ...] = (),
) -> tuple[ScannerRun, str | None]:
    """Run one configured scanner and return its record plus any gate error.

    ``context`` is ``None`` when the scope checks did not pass; the scanner is
    then recorded as skipped. ``env_passthrough`` is handed to ``runner`` so
    the scanner gets the allowlisted gate environment plus those names. A
    non-zero exit is ``scanner_failed:<name>``, a
    timeout is ``scanner_timeout:<name>``, and a missing executable is
    ``scanner_missing:<name>`` when the scanner is required or a skip when it
    is optional. When the command starts with a plain program token, the
    runner receives the command with that token replaced by the quoted
    absolute path :func:`find_scanner_executable` returned, so the shell
    cannot pick a same-named program from the repository instead.
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

    command = rendered.command
    executable = command_executable(command)
    if executable is not None:
        resolved = find_scanner_executable(
            executable, path=_env_path(build_work_claim_env(env_passthrough))
        )
        if resolved is None:
            return _missing_scanner(scanner, command, executable)
        # Run the program the lookup found, not the bare name: the shell would
        # resolve it again and, on Windows, cmd.exe checks the repository
        # (its working directory) before PATH.
        command = bind_command_executable(command, resolved)

    run = runner(
        command,
        cwd=context.repo_root,
        timeout_seconds=scanner.timeout_ms / 1000,
        env_passthrough=env_passthrough,
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


def command_executable(command: str) -> str | None:
    """Return the leading program of a shell command when it is a plain token.

    ``llm-handoff doctor`` imports this so its scanner check finds the same
    executable the gate looks up with :func:`find_scanner_executable`.
    """

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


def bind_command_executable(command: str, resolved: str) -> str:
    """Replace the leading program token of ``command`` with ``resolved``.

    The token is found the same way as in :func:`command_executable` (a
    quoted or whitespace-delimited first word), and ``resolved`` is quoted
    with :func:`shell_quote` so the shell runs that exact file. The rest of
    the command is kept byte for byte.
    """

    stripped = command.lstrip()
    if not stripped:
        return command
    if stripped[0] in "\"'":
        end = stripped.find(stripped[0], 1)
        if end == -1:
            return command
        rest = stripped[end + 1 :]
    else:
        parts = stripped.split(None, 1)
        rest = stripped[len(parts[0]) :]
    return shell_quote(resolved) + rest


_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def find_scanner_executable(
    executable: str,
    *,
    path: str | None = None,
) -> str | None:
    """Return the scanner program the gate would run, or ``None``.

    Unlike ``shutil.which``, this never searches the current directory: on
    Windows, Python 3.11's ``shutil.which`` checks it first even when
    ``path`` is given. A name with a path separator or drive is accepted only
    when it is absolute. A bare name is looked up in the absolute entries of
    ``path`` (default ``PATH``); empty and relative entries such as ``.`` are
    skipped. On Windows each ``PATHEXT`` extension is also tried unless the
    name already ends with one. There is no ``.cmd`` stem fallback.
    ``llm-handoff doctor`` and the work-claim gate both use this lookup.
    """

    if not executable:
        return None
    separators = ("/", "\\") if os.name == "nt" else ("/",)
    has_drive = bool(os.path.splitdrive(executable)[0])
    if has_drive or any(sep in executable for sep in separators):
        if not os.path.isabs(executable):
            return None
        for candidate in _executable_candidates(executable):
            if _is_executable_file(candidate):
                return candidate
        return None

    search_path = os.environ.get("PATH", "") if path is None else path
    for entry in search_path.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        for candidate in _executable_candidates(os.path.join(entry, executable)):
            if _is_executable_file(candidate):
                return candidate
    return None


def _executable_candidates(base: str) -> list[str]:
    candidates = [base]
    if os.name != "nt":
        return candidates
    pathext = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    extensions = [ext for ext in pathext.split(os.pathsep) if ext]
    lowered = base.lower()
    if any(lowered.endswith(ext.lower()) for ext in extensions):
        return candidates
    candidates.extend(base + ext for ext in extensions)
    return candidates


def _is_executable_file(candidate: str) -> bool:
    return os.path.isfile(candidate) and os.access(candidate, os.X_OK)


def _env_path(env: Mapping[str, str]) -> str:
    """Return the ``PATH`` value of ``env``, matching the name case-insensitively."""

    for key, value in env.items():
        if key.upper() == "PATH":
            return value
    return ""


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
    dispatcher_commit_mode: bool = False,
    story_base_sha: str | None = None,
    env_passthrough: tuple[str, ...] = (),
    verification_allowed_prefixes: tuple[str, ...] = (),
    log: Callable[[str, str], None] | None = None,
    chat_owned_paths: tuple[str, ...] = (),
) -> WorkClaimResult:
    """Check an implementer's ``ready_for_review`` claim against the packet.

    ``assignment_content`` is the handoff the implementer received (it holds
    the Work Packet); ``claim_content`` is the handoff it wrote back.
    ``scanners`` run in order after the scope checks pass and before the
    verification command; every scanner runs even when an earlier one fails.
    ``require_new_commit`` fails a claim whose commits since dispatch change
    no file other than the handoff (``no_changes``).
    ``dispatcher_commit_mode`` says the dispatcher, not the implementer, commits
    and records ``scope_sha``, so failure guidance never asks for a commit.
    ``story_base_sha`` is HEAD before the story's first dispatch; with it,
    ``no_changes`` counts commits from earlier dispatches of the same story, so
    a retry that only reverts a forbidden file can pass.
    ``env_passthrough`` names extra environment variables that the scanners
    and the verification command receive on top of the allowlisted gate
    environment (see :func:`build_work_claim_env`).
    ``verification_allowed_prefixes``, when non-empty, restricts which
    Verification commands run (see :func:`verification_command_allowed`); a
    rejected command fails ``verification_not_allowed`` and nothing runs.
    ``log``, when given, receives ``(level, message)`` with the exact command
    text before each scanner and Verification command run.
    ``chat_owned_paths`` are files the dispatcher committed because a chat
    side session changed them at the operator's request; ``diff_scope`` and
    ``worktree`` accept them whatever the Work Packet or pre-dispatch state
    says.
    """

    handoff_rel_path = normalize_repo_path(Path(handoff_path).as_posix())
    chat_paths = tuple(
        dict.fromkeys(
            path
            for path in (normalize_repo_path(item) for item in chat_owned_paths)
            if path and path != handoff_rel_path
        )
    )
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

    # Nothing commits or edits files during the git checks below, so one
    # `git status` gives HEAD now and the dirty files for the worktree check.
    head_sha, dirty_now = git_head_and_dirty_files(repo_root)
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

    lookups = _CommitLookups(repo_root, head_sha=head_sha)
    changed_files = _changed_since(repo_root, pre_dispatch_head, head_sha)
    # Files the story's commits changed: since this dispatch, or since the
    # story's first dispatch when an earlier one already committed work.
    story_files = changed_files
    if (
        story_base_sha
        and story_base_sha != pre_dispatch_head
        and changed_files is not None
        and not any(path != handoff_rel_path for path in changed_files)
    ):
        story_files = _changed_since(repo_root, story_base_sha, head_sha)
    has_story_work = story_files is not None and any(
        path != handoff_rel_path for path in story_files
    )

    if (
        dispatcher_commit_mode
        and not scope_sha
        and story_files is not None
        and not has_story_work
    ):
        # The dispatcher records scope_sha only when it commits; with nothing
        # committed there is no SHA to claim, and no_changes reports why.
        scope_check = WorkClaimCheck(
            "scope_sha",
            "SKIP",
            "scope_sha is empty because the dispatcher committed nothing for "
            "this claim.",
        )
        scope_error, resolved_scope_sha = None, None
    else:
        scope_check, scope_error, resolved_scope_sha = _scope_sha_check(
            lookups,
            scope_sha=scope_sha,
            producer=producer,
            head_sha=head_sha,
            handoff_rel_path=handoff_rel_path,
        )
    checks.append(scope_check)
    if scope_error is not None:
        errors.append(scope_error)

    if resolved_scope_sha is not None:
        evidence_check, evidence_error = _evidence_sha_check(
            lookups,
            claim_content=claim_content,
            scope_sha=scope_sha or resolved_scope_sha,
            resolved_scope_sha=resolved_scope_sha,
            handoff_rel_path=handoff_rel_path,
        )
        if evidence_check is not None:
            checks.append(evidence_check)
        if evidence_error is not None:
            errors.append(evidence_error)

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
            chat_owned_paths=chat_paths,
        )
        checks.append(diff_check)
        errors.extend(diff_errors)

    if require_new_commit and not has_story_work:
        detail = (
            "no file other than the handoff changed in commits since dispatch "
            f"({_short(pre_dispatch_head)}..{_short(head_sha)})."
        )
        checks.append(WorkClaimCheck("new_commit", "FAIL", detail))
        errors.append(f"{NO_CHANGES_PREFIX}: {detail}")

    worktree_check, worktree_errors = _worktree_check(
        repo_root,
        dirty_now=dirty_now,
        pre_dispatch_state=pre_dispatch_state,
        handoff_rel_path=handoff_rel_path,
        packet=packet,
        dispatcher_commit_mode=dispatcher_commit_mode,
        chat_owned_paths=chat_paths,
    )
    checks.append(worktree_check)
    errors.extend(worktree_errors)

    scanner_runs: list[ScannerRun] = []
    if scanners:
        scope_checks_passed = (
            scope_error is None
            and not worktree_errors
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
            scanner_kwargs: dict[str, object] = {}
            if log is not None:
                scanner_kwargs["runner"] = _logging_scanner_runner(
                    scanner.name, log
                )
            scanner_run, scanner_error = run_scanner(
                scanner,
                context,
                env_passthrough=env_passthrough,
                **scanner_kwargs,
            )
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
    repeated_verification_failure = False
    rejected_verification_command: str | None = None
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
            env_passthrough=env_passthrough,
            verification_allowed_prefixes=verification_allowed_prefixes,
            log=log,
        )
        checks.append(verification_check)
        if verification_error is not None and verification_error.startswith(
            f"{VERIFICATION_NOT_ALLOWED_PREFIX}: "
        ):
            rejected_verification_command = next(
                (
                    command
                    for command in packet.verification_commands
                    if verification_command_allowed(
                        command, verification_allowed_prefixes
                    )
                    is not None
                ),
                None,
            )
            errors.append(verification_error)
        elif verification_error is not None:
            # The bounce carries the packet forward unchanged, so an
            # implementer that commits nothing cannot change the outcome. Any
            # other failure, such as uncommitted work, is still the
            # implementer's to fix, so the claim stays with the implementer.
            repeated_verification_failure = (
                not errors
                and bool(verification_runs)
                and not any(path != handoff_rel_path for path in changed_files)
                and _previous_bounce_failed_command(
                    assignment_content, verification_runs[-1].command
                )
            )
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
        dispatcher_commit_mode=dispatcher_commit_mode,
        repeated_verification_failure=repeated_verification_failure,
        rejected_verification_command=rejected_verification_command,
    )


def _changed_since(
    repo_root: Path, base: str, head_sha: str
) -> tuple[str, ...] | None:
    """:func:`git_changed_files` for ``base..head_sha``, where ``head_sha`` is a
    resolved commit; an empty range needs no Git call."""

    if base == head_sha:
        return ()
    return git_changed_files(repo_root, base, head_sha)


def _logging_scanner_runner(
    name: str,
    log: Callable[[str, str], None],
) -> Callable[..., CommandRun]:
    """Wrap :func:`run_shell_command` so the rendered scanner command is logged
    immediately before it runs."""

    def runner(command: str, **kwargs: object) -> CommandRun:
        log("INFO", f"work-claim: running scanner {name}: {command}")
        return run_shell_command(command, **kwargs)  # type: ignore[arg-type]

    return runner


def _single_line(text: str) -> str:
    return text.replace("\r", "\\r").replace("\n", "\\n")


def verification_command_allowed(
    command: str,
    allowed_prefixes: Sequence[str],
) -> str | None:
    """Return ``None`` when ``command`` may run, or a one-line reason why not.

    An empty ``allowed_prefixes`` allows every command. Otherwise the command
    must contain no character in ``VERIFICATION_FORBIDDEN_CHARS``. With
    surrounding spaces and tabs stripped, it must then separate its words by
    single spaces (no tab, other non-space whitespace, or run of spaces) and
    either equal a prefix or start with a prefix followed by one space,
    compared case-sensitively. This literal match is what the `agy` grant
    ``antigravity_command_grant(prefix)`` accepts, so the gate never allows a
    command that grant would refuse.
    """

    if not allowed_prefixes:
        return None
    shown = _single_line(command)
    for char in command:
        if char in VERIFICATION_FORBIDDEN_CHARS:
            return (
                f"Verification command `{shown}` contains the shell control "
                f"character {char!r}."
            )
    stripped = command.strip(" \t")
    problem = verification_whitespace_problem(stripped)
    if problem is not None:
        return (
            f"Verification command `{shown}` contains {problem}; words must be "
            "separated by single spaces with no other whitespace."
        )
    for prefix in allowed_prefixes:
        literal = prefix.strip()
        if literal and (stripped == literal or stripped.startswith(f"{literal} ")):
            return None
    listed = ", ".join(f"`{_single_line(prefix)}`" for prefix in allowed_prefixes)
    return (
        f"Verification command `{shown}` does not start with any "
        f"verification_allowed_prefixes entry ({listed})."
    )


def _previous_bounce_failed_command(assignment_content: str, command: str) -> bool:
    """Return whether the assignment is a bounce whose verification ran ``command``
    and failed.

    Only the top-level ``## Dispatcher Verification`` section counts; older
    sections inside ``## Prior Handback`` have demoted headings.
    """

    normalized = assignment_content.replace("\r\n", "\n")
    start = normalized.find(f"\n{DISPATCHER_VERIFICATION_HEADING}\n")
    if start == -1:
        return False
    start += 1
    next_heading = re.search(r"(?m)^## ", normalized[start + 1 :])
    end = len(normalized) if next_heading is None else start + 1 + next_heading.start()
    section = normalized[start:end]
    return (
        "- **Verdict:** FAIL" in section
        and f"- FAIL `verification`: `{command}` " in section
    )


def _scope_sha_check(
    lookups: _CommitLookups,
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

    resolved = lookups.resolve_commit(scope_sha)
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

    equivalents = _head_equivalent_commits(lookups, head_sha, handoff_rel_path)
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


def _evidence_sha_check(
    lookups: _CommitLookups,
    *,
    claim_content: str,
    scope_sha: str,
    resolved_scope_sha: str,
    handoff_rel_path: str,
) -> tuple[WorkClaimCheck | None, str | None]:
    """Check that the evidence names the same commit as ``scope_sha``.

    The evidence may also name a commit above ``scope_sha`` when every commit
    in between touches only the handoff, the same allowance the scope_sha
    check makes for HEAD. Like ``new_commit``, the check is recorded only when
    it fails.
    """

    value = evidence_commit_value(claim_content)
    remedy = (
        f"Commit SHA verified must name scope_sha `{scope_sha}` "
        f"({_short(resolved_scope_sha)})."
    )
    if not value:
        detail = (
            "the claim has no Commit SHA verified value under "
            f"## Verification Evidence; {remedy}"
        )
    else:
        shown = value
        if len(shown) > _EVIDENCE_VALUE_DETAIL_CHARS:
            shown = shown[: _EVIDENCE_VALUE_DETAIL_CHARS - 3] + "..."
        token = evidence_sha_token(value)
        resolved = lookups.resolve_commit(token) if token else None
        if token is None:
            detail = f"Commit SHA verified `{shown}` names no commit SHA; {remedy}"
        elif resolved is None:
            detail = (
                f"Commit SHA verified `{token}` does not resolve to a commit; "
                f"{remedy}"
            )
        elif resolved_scope_sha in _head_equivalent_commits(
            lookups, resolved, handoff_rel_path
        ):
            return None, None
        else:
            detail = (
                f"Commit SHA verified `{token}` resolves to {_short(resolved)}, "
                f"but scope_sha resolves to {_short(resolved_scope_sha)}; the "
                "evidence must name the commit the claim points at."
            )
    return (
        WorkClaimCheck("evidence_sha", "FAIL", detail),
        f"{EVIDENCE_SHA_MISMATCH_PREFIX}: {detail}",
    )


def _diff_scope_check(
    packet: WorkPacket,
    changed_files: tuple[str, ...],
    *,
    handoff_rel_path: str,
    chat_owned_paths: tuple[str, ...] = (),
) -> tuple[WorkClaimCheck, list[str]]:
    candidates = tuple(path for path in changed_files if path != handoff_rel_path)
    chat_set = set(chat_owned_paths)
    chat_changed = tuple(path for path in candidates if path in chat_set)
    out_of_bounds_hits = tuple(
        path
        for path in candidates
        if path not in chat_set and path_is_out_of_bounds(path, packet)
    )
    unlisted = tuple(
        path
        for path in candidates
        if path not in chat_set
        and path not in out_of_bounds_hits
        and not path_is_in_scope(path, packet)
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
    scoped = tuple(path for path in candidates if path not in chat_set)
    if not candidates:
        detail = "no files other than the handoff changed since dispatch."
    elif not chat_changed:
        detail = (
            f"{len(candidates)} changed file(s) stayed inside Files in scope: "
            f"{', '.join(candidates)}."
        )
    else:
        detail = (
            f"{len(scoped)} changed file(s) stayed inside Files in scope"
            + (f": {', '.join(scoped)}." if scoped else ".")
            + f" {len(chat_changed)} chat change(s) (operator-influenced) "
            f"accepted: {', '.join(chat_changed)}."
        )
    return WorkClaimCheck("diff_scope", "PASS", detail), []


def _worktree_check(
    repo_root: Path,
    *,
    dirty_now: tuple[str, ...] | None,
    pre_dispatch_state: PreDispatchState,
    handoff_rel_path: str,
    packet: WorkPacket | None = None,
    dispatcher_commit_mode: bool = False,
    chat_owned_paths: tuple[str, ...] = (),
) -> tuple[WorkClaimCheck, list[str]]:
    if dirty_now is None:
        detail = "git status failed; uncommitted changes could not be listed."
        return (
            WorkClaimCheck("worktree", "FAIL", detail),
            [f"{GIT_STATE_UNAVAILABLE_PREFIX}: {detail}"],
        )
    chat_set = set(chat_owned_paths)
    new_dirty = tuple(
        path
        for path in dirty_now
        if path != handoff_rel_path and path not in pre_dispatch_state.dirty_files
    )
    # Committing a file the Work Packet does not allow cannot fix the claim, and
    # the dispatcher never commits one, so those files are a scope problem.
    # Chat changes count as in scope: the operator asked for them.
    not_allowed = tuple(
        path
        for path in new_dirty
        if packet is not None
        and path not in chat_set
        and not path_is_in_scope(path, packet)
    )
    uncommitted = tuple(path for path in new_dirty if path not in not_allowed)
    # A file that was already dirty before dispatch is only acceptable if its
    # working-tree content is unchanged; otherwise the verification command
    # would run against edits that scope_sha does not contain.
    before = dict(pre_dispatch_state.dirty_fingerprints)
    altered_dirty = tuple(
        path
        for path in dirty_now
        if path != handoff_rel_path
        and path not in chat_set
        and path in before
        and working_tree_fingerprint(repo_root, path) != before[path]
    )
    details: list[str] = []
    errors: list[str] = []
    if not_allowed and packet is not None:
        labelled = ", ".join(
            f"{path} ("
            + (
                "listed under Files out of bounds"
                if path_is_out_of_bounds(path, packet)
                else "outside Files in scope"
            )
            + ")"
            for path in not_allowed
        )
        detail = (
            "uncommitted changes to files the Work Packet does not allow: "
            f"{labelled}; revert them, because they cannot be part of this claim."
        )
        details.append(detail)
        errors.append(f"{SCOPE_VIOLATION_PREFIX}: {detail}")
    problems: list[str] = []
    if uncommitted:
        problems.append(
            f"working tree has uncommitted changes outside {handoff_rel_path}: "
            f"{', '.join(uncommitted)}"
        )
    if altered_dirty:
        problems.append(
            "files that were already uncommitted before dispatch were modified "
            f"further without being committed: {', '.join(altered_dirty)}"
        )
    if problems:
        if dispatcher_commit_mode:
            remedy = (
                "; the dispatcher commits only in-scope files that changed during "
                "this dispatch, so it did not commit these. Revert edits to files "
                "that were dirty before dispatch, and check the dispatcher log "
                "for a failed commit."
            )
        else:
            remedy = "; commit them so scope_sha contains the claimed work."
        detail = "; ".join(problems) + remedy
        details.append(detail)
        errors.append(f"{UNCOMMITTED_CHANGES_PREFIX}: {detail}")
    if errors:
        return WorkClaimCheck("worktree", "FAIL", " ".join(details)), errors
    detail = f"no new uncommitted changes outside {handoff_rel_path}."
    if chat_owned_paths:
        detail += (
            " Chat changes (operator-influenced) accepted: "
            f"{', '.join(chat_owned_paths)}."
        )
    return WorkClaimCheck("worktree", "PASS", detail), []


def _verification_check(
    packet: WorkPacket,
    *,
    repo_root: Path,
    timeout_seconds: int,
    runs: list[CommandRun],
    env_passthrough: tuple[str, ...] = (),
    verification_allowed_prefixes: tuple[str, ...] = (),
    log: Callable[[str, str], None] | None = None,
) -> tuple[WorkClaimCheck, str | None]:
    # Check every command before running any, so a rejected command later in
    # the list does not let an earlier one run first.
    for command in packet.verification_commands:
        reason = verification_command_allowed(command, verification_allowed_prefixes)
        if reason is not None:
            detail = (
                f"{reason} No Verification command ran; the Work Packet must use "
                "a command that starts with a verification_allowed_prefixes "
                "entry and contains no shell control character."
            )
            return (
                WorkClaimCheck("verification", "FAIL", detail),
                f"{VERIFICATION_NOT_ALLOWED_PREFIX}: {detail}",
            )
    for command in packet.verification_commands:
        if log is not None:
            log("INFO", f"work-claim: running verification command: {command}")
        run = run_shell_command(
            command,
            cwd=repo_root,
            timeout_seconds=timeout_seconds,
            env_passthrough=env_passthrough,
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

    Scope, SHA, evidence SHA, no-change, worktree, scanner, and verification
    failures go back to the implementer as ``verified_fail``. A missing or unusable Work Packet is the
    planner's artifact, so that bounce routes to ``planner``. So does a
    Verification command that failed again, as the only failed check, on a
    claim that committed nothing since the previous bounce: the implementer
    cannot change the packet the gate reads, so another implementer dispatch
    would fail the same way. A Verification command that
    ``verification_allowed_prefixes`` rejected (``verification_not_allowed``)
    is also the planner's to replace.
    Unreadable Git state and an unrenderable scanner command are operator
    problems and escalate to ``user``.
    """

    prefixes = set(result.failed_prefixes)
    if GIT_STATE_UNAVAILABLE_PREFIX in prefixes or any(
        prefix.split(":", 1)[0] == SCANNER_CONFIG_INVALID_PREFIX for prefix in prefixes
    ):
        return BounceRoute(next_agent="user", status="escalate_to_user")
    if (
        prefixes & WORK_PACKET_ERROR_PREFIXES
        or VERIFICATION_NOT_ALLOWED_PREFIX in prefixes
        or result.repeated_verification_failure
    ):
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
        if result.dispatcher_commit_mode:
            remedy = (
                "Fix every FAIL below, leave the fix uncommitted with "
                "`scope_sha` empty, and claim again with "
                "`status: ready_for_review`; the dispatcher commits the in-scope "
                "files and records `scope_sha`."
            )
        else:
            remedy = (
                "Fix every FAIL below, commit the fix, set `scope_sha` to the new "
                "`git rev-parse HEAD`, and claim again with "
                "`status: ready_for_review`."
            )
        lines.append(
            f"The dispatcher checked the `ready_for_review` claim from "
            f"`{implementer}` before dispatching the auditor and rejected it. "
            + remedy
        )
    elif (
        route.next_agent == "planner"
        and VERIFICATION_NOT_ALLOWED_PREFIX in result.failed_prefixes
    ):
        rejected = _single_line(result.rejected_verification_command or "unknown")
        lines.append(
            f"The dispatcher rejected the Verification command `{rejected}` "
            f"from the assignment's `## Work Packet` while checking the "
            f"`ready_for_review` claim from `{implementer}`: it did not match "
            "`verification_allowed_prefixes` or it contained a shell control "
            "character, so no Verification command ran. Re-issue the Work "
            "Packet with a Verification command that starts with an allowed "
            "prefix and contains no shell control character. The "
            "implementer's work has not been audited."
        )
    elif route.next_agent == "planner" and result.repeated_verification_failure:
        failed_command = result.verification_runs[-1].command
        lines.append(
            f"The Verification command `{failed_command}` failed again on the "
            f"`ready_for_review` claim from `{implementer}`, and no commit since "
            "the previous bounce changed a file other than the handoff. The "
            "dispatcher runs the command from the assignment's `## Work "
            "Packet`, which the implementer cannot change, so the claim was "
            "routed to the planner instead of the implementer. Check the "
            "Verification command and the implementer's handback below, then "
            "re-issue the assignment with a corrected Work Packet or re-scope "
            "the story. The implementer's work has not been audited."
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

    return replace_dispatcher_verification_section(
        claim_content, passed_verification_section(result)
    )


def replace_dispatcher_verification_section(content: str, section: str) -> str:
    """Return ``content`` with its top-level ``## Dispatcher Verification``
    section replaced by ``section``, or with ``section`` appended."""

    normalized = content.replace("\r\n", "\n")
    start = normalized.find(f"\n{DISPATCHER_VERIFICATION_HEADING}\n")
    if start == -1:
        return normalized.rstrip("\n") + "\n\n" + section
    start += 1
    next_heading = re.search(r"(?m)^## ", normalized[start + 1 :])
    end = len(normalized) if next_heading is None else start + 1 + next_heading.start()
    return normalized[:start] + section + ("\n" if end < len(normalized) else "") + normalized[end:]


_VERIFIED_COMMIT_RE = re.compile(
    r"(?m)^-[ \t]+\*\*Verified commit:\*\*[ \t]*`?([0-9a-fA-F]{7,40})`?"
)
_PASS_VERDICT_RE = re.compile(r"(?m)^-[ \t]+\*\*Verdict:\*\*[ \t]*PASS\b")


def has_passing_dispatcher_verification(content: str, commit_sha: str) -> bool:
    """True when the top-level ``## Dispatcher Verification`` section says
    ``PASS`` for ``commit_sha`` (a short or full SHA on either side)."""

    body = _dispatcher_verification_body(content)
    if body is None or _PASS_VERDICT_RE.search(body) is None:
        return False
    match = _VERIFIED_COMMIT_RE.search(body)
    if match is None:
        return False
    verified = match.group(1).lower()
    expected = commit_sha.strip().lower()
    return bool(expected) and (
        verified.startswith(expected) or expected.startswith(verified)
    )


def recorded_verification_commands(content: str) -> tuple[str, ...]:
    """The commands the top-level ``## Dispatcher Verification`` section lists
    on its ``- **Verification command:**`` lines, in order."""

    body = _dispatcher_verification_body(content)
    if body is None:
        return ()
    return tuple(match.group(1) for match in _RECORDED_COMMAND_RE.finditer(body))


_RECORDED_COMMAND_RE = re.compile(
    r"(?m)^-[ \t]+\*\*Verification command:\*\*[ \t]*`(.*)`[ \t]+\([^()\n]*\)[ \t]*$"
)


def _dispatcher_verification_body(content: str) -> str | None:
    normalized = content.replace("\r\n", "\n")
    start = normalized.find(f"\n{DISPATCHER_VERIFICATION_HEADING}\n")
    if start == -1:
        return None
    body = normalized[start + len(DISPATCHER_VERIFICATION_HEADING) + 2 :]
    next_heading = re.search(r"(?m)^## ", body)
    if next_heading is not None:
        body = body[: next_heading.start()]
    return body


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
    "VERIFICATION_NOT_ALLOWED_PREFIX",
    "BounceRoute",
    "CommandRun",
    "ImplementerLeftovers",
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
    "build_work_claim_env",
    "passed_verification_section",
    "record_passed_verification",
    "capture_implementer_leftovers",
    "carry_over_key",
    "capture_pre_dispatch_state",
    "load_implementer_leftovers",
    "prune_implementer_leftovers",
    "save_implementer_leftovers",
    "bind_command_executable",
    "command_executable",
    "evidence_commit_value",
    "evidence_sha_token",
    "extract_work_packet_section",
    "find_scanner_executable",
    "git_path_is_ignored",
    "parse_work_packet",
    "path_is_in_scope",
    "path_is_out_of_bounds",
    "path_matches_entry",
    "render_scanner_command",
    "run_scanner",
    "run_shell_command",
    "run_verification_command",
    "shell_quote",
    "verification_command_allowed",
    "verify_work_claim",
    "work_packet_errors",
]
