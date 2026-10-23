from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from llm_handoff.config import (
    CLAUDE_DEFAULT_ALLOWED_TOOLS,
    IN_FLIGHT_STATE_PATH,
    INTERPRETER_EXECUTABLES,
    VERIFICATION_FORBIDDEN_CHARS,
    DispatchConfig,
    TrackerConfig,
    linear_tools_for_role,
    load_dispatch_config,
    verification_whitespace_problem,
)
from llm_handoff.roles import normalize_next_agent_value
from llm_handoff.router import (
    HandoffFrontmatterError,
    parse_handoff_frontmatter_text,
)
from llm_handoff.router import route as route_handoff
from llm_handoff.stack_detect import Candidate, detect_candidates
from llm_handoff.tracker_prompts import (
    LINEAR_PROMPT_ROLES,
    linear_agents_section,
    linear_evidence_contract,
    linear_issue_template,
    linear_role_section,
)

REFERENCE_TEMPLATE = "reference-workflow"
_PACKAGE_NAME = "llm_handoff"
# Wheels carry the template under llm_handoff/templates/ (pyproject.toml maps
# examples/reference-workflow/ there at build time).
_PACKAGED_TEMPLATES_DIR = "templates"
_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent


def _packaged_template_root(template: str = REFERENCE_TEMPLATE) -> Path | None:
    """Return the template bundled with the installed package, if present.

    Only a template that sits beside this module counts, so a source checkout
    never picks up a template from a different installed copy of the package.
    """

    try:
        candidate = resources.files(_PACKAGE_NAME) / _PACKAGED_TEMPLATES_DIR / template
        path = Path(os.fspath(candidate)).resolve()
    except (ModuleNotFoundError, TypeError, ValueError, OSError):
        return None
    if not path.is_dir():
        return None
    if path.parent.parent != _PACKAGE_DIR:
        return None
    return path


def resolve_template_root(template: str = REFERENCE_TEMPLATE) -> Path:
    """Locate a template: installed package data first, then the source layout."""

    packaged = _packaged_template_root(template)
    if packaged is not None:
        return packaged
    return _REPO_ROOT / "examples" / template


_TEMPLATE_ROOT = resolve_template_root()
_EXCLUDED_TEMPLATE_FILES = frozenset({Path("README.md")})

# Provider mappings `init` can write into dispatch_config.yaml. `reference`
# keeps the template's mixed mapping; `claude` puts every role on Claude Code;
# `codex` puts every role on the Codex CLI; `antigravity` puts every role on
# the Antigravity CLI (`agy`).
REFERENCE_MAPPING = "reference"
# A tracker switch keeps the initialized repository's agents block; its
# InitResult reports this instead of a mapping.
KEPT_MAPPING = "kept"
CLAUDE_MAPPING = "claude"
CODEX_MAPPING = "codex"
ANTIGRAVITY_MAPPING = "antigravity"
PROVIDER_MAPPINGS = (CLAUDE_MAPPING, CODEX_MAPPING, ANTIGRAVITY_MAPPING)
# Provider CLIs the mapping auto-detect looks for on PATH. `agy` selects the
# antigravity mapping.
PROVIDER_CLIS = ("claude", "codex", "agy")
_ANTIGRAVITY_CLI = "agy"
_CONFIG_PATH = Path("dispatch_config.yaml")
# Run-state files whose presence means a dispatcher run is active or was
# interrupted, so an initialized target must not be changed. The lock lives
# at the target root; in-flight.json is config.IN_FLIGHT_STATE_PATH;
# publish/story.json is publish.STATE_PATH (not imported: publish pulls in the
# whole publish stack), and logs/dispatch/story.json is checked as well.
_DISPATCH_LOCK_PATH = Path(".dispatch.lock")
_RUN_STATE_PATHS = (
    IN_FLIGHT_STATE_PATH,
    Path("logs") / "dispatch" / "story.json",
    Path("logs") / "dispatch" / "publish" / "story.json",
)
# next_agent values that hold an active assignment. planner, tpm, and user
# (with no tracker_issue) are between assignments.
_ACTIVE_ASSIGNMENT_ROLES = frozenset(
    {"backend", "frontend", "auditor", "validator", "finalizer"}
)
_BETWEEN_ASSIGNMENT_ROLES = frozenset({"planner", "tpm", "user"})
_TEXT_MARKERS = {
    Path("AGENTS.md"): (b"<!-- llm-handoff:begin -->", b"<!-- llm-handoff:end -->"),
    Path(".gitignore"): (b"# llm-handoff:begin", b"# llm-handoff:end"),
}
_SETTINGS_PATH = Path(".claude/settings.json")
_AGENTS_PATH = Path("AGENTS.md")
_HANDBOOK_PATH = Path("docs/handoff/HANDBOOK.md")
_VALIDATOR_AGENT_PATH = Path(".claude/agents/handoff-validator.md")
# Tools the handoff-validator subagent keeps in every mode; Linear init appends
# the validator's `mcp__<tracker.mcp_server>__*` tools after them.
_VALIDATOR_BASE_TOOLS = "Read, Grep, Glob, Bash"
# Claude subagent files whose `tools:` allowlist Linear init rewrites
# (handoff-validator, handoff-router, planner, tpm, frontend, backend, auditor,
# and ledger-updater), mapped to (role whose Linear tools are appended,
# built-in tools kept in every mode). A subagent's `tools:` list is an
# allowlist, so without the appended names the subagent cannot call the Linear
# MCP tools its role launch allows. Each base string must equal the template's
# `tools:` line without its `mcp__*` entries. The dispatcher runs
# handoff-router for a `next_agent: validator` handoff, so it gets the
# validator's tools.
_ROUTER_AGENT_PATH = Path(".claude/agents/handoff-router.md")
_PLANNER_AGENT_PATH = Path(".claude/agents/planner.md")
_TPM_AGENT_PATH = Path(".claude/agents/tpm.md")
_FRONTEND_AGENT_PATH = Path(".claude/agents/frontend.md")
_BACKEND_AGENT_PATH = Path(".claude/agents/backend.md")
_AUDITOR_AGENT_PATH = Path(".claude/agents/auditor.md")
_FINALIZER_AGENT_PATH = Path(".claude/agents/ledger-updater.md")
_WRITER_BASE_TOOLS = "Read, Edit, Write, Grep, Glob, Bash"
_AUDITOR_BASE_TOOLS = "Read, Grep, Glob, Bash"
# The finalizer needs Write to create a missing completed work file.
_FINALIZER_BASE_TOOLS = "Read, Edit, Write, Grep, Glob, Bash"
_LINEAR_AGENT_TOOLS: dict[Path, tuple[str, str]] = {
    _VALIDATOR_AGENT_PATH: ("validator", _VALIDATOR_BASE_TOOLS),
    _ROUTER_AGENT_PATH: ("validator", _VALIDATOR_BASE_TOOLS),
    _PLANNER_AGENT_PATH: ("planner", _WRITER_BASE_TOOLS),
    _TPM_AGENT_PATH: ("tpm", _WRITER_BASE_TOOLS),
    _FRONTEND_AGENT_PATH: ("frontend", _WRITER_BASE_TOOLS),
    _BACKEND_AGENT_PATH: ("backend", _WRITER_BASE_TOOLS),
    _AUDITOR_AGENT_PATH: ("auditor", _AUDITOR_BASE_TOOLS),
    _FINALIZER_AGENT_PATH: ("finalizer", _FINALIZER_BASE_TOOLS),
}

# `init --tracker` values. `none` keeps vanilla output byte for byte.
TRACKER_NONE = "none"
TRACKER_LINEAR = "linear"
TRACKER_CHOICES = (TRACKER_NONE, TRACKER_LINEAR)
DEFAULT_TRACKER_MCP_SERVER = "linear"
_DEFAULT_ELIGIBLE_STATE = "Todo"
_DEFAULT_IN_PROGRESS_STATE = "In Progress"
_DEFAULT_SUCCESS_STATE = "In Review"
# Managed-block markers for Linear mode. The config block uses YAML comments;
# AGENTS.md and HANDBOOK.md use HTML comments.
_TRACKER_CONFIG_MARKERS = ("# llm-handoff:tracker:begin", "# llm-handoff:tracker:end")
_LINEAR_TEXT_MARKERS = (
    b"<!-- llm-handoff:linear:begin -->",
    b"<!-- llm-handoff:linear:end -->",
)
# Order of the `--tracker-*` option names substituted into validation errors;
# `tracker.provider: linear` must be replaced before the shorter keys.
_TRACKER_OPTION_NAMES = (
    ("tracker.provider: linear", "--tracker linear"),
    ("tracker.mcp_server", "--tracker-mcp-server"),
    ("tracker.project", "--tracker-project"),
    ("tracker.label", "--tracker-label"),
    ("tracker.team", "--tracker-team"),
)

_CLAUDE_AGENTS_BLOCK = """agents:
  # All-Claude mapping written by `llm-handoff init` (Claude Code only).
  # Each role runs the matching subagent in .claude/agents/.
  planner:
    provider: claude
    binary: claude
    resume: false
    timeout_ms: 1200000

  backend:
    provider: claude
    binary: claude
    resume: false
    timeout_ms: 1200000

  frontend:
    provider: claude
    binary: claude
    resume: false
    timeout_ms: 1200000

  auditor:
    provider: claude
    binary: claude
    model: claude-opus-5-5
    resume: false
    timeout_ms: 900000

  validator:
    provider: claude
    binary: claude
    model: claude-haiku-5-5
    resume: false
    timeout_ms: 900000

  finalizer:
    provider: claude
    binary: claude
    model: claude-opus-5-5
    resume: false
    timeout_ms: 900000

"""
_CODEX_AGENTS_BLOCK = """agents:
  # All-Codex mapping written by `llm-handoff init` (Codex CLI only).
  # Each role runs the matching repo skill in .codex/skills/.
  planner:
    provider: codex
    binary: codex
    skill_name: llm-handoff-planner
    resume: false
    timeout_ms: 1200000

  backend:
    provider: codex
    binary: codex
    skill_name: llm-handoff
    resume: false
    timeout_ms: 1200000

  frontend:
    provider: codex
    binary: codex
    skill_name: llm-handoff-frontend
    resume: false
    timeout_ms: 1200000

  auditor:
    provider: codex
    binary: codex
    skill_name: llm-handoff-auditor
    resume: false
    timeout_ms: 900000

  validator:
    provider: codex
    binary: codex
    skill_name: llm-handoff-validator
    resume: false
    timeout_ms: 900000

  finalizer:
    provider: codex
    binary: codex
    skill_name: llm-handoff-finalizer
    resume: false
    timeout_ms: 900000

"""
_ANTIGRAVITY_AGENTS_BLOCK = """agents:
  # All-Antigravity mapping written by `llm-handoff init` (agy only).
  # Each role runs the matching agent file in .agents/agents/: planner,
  # backend, frontend, and auditor by role name; validator runs
  # handoff-validator or handoff-router, and finalizer runs ledger-updater.
  planner:
    provider: antigravity
    binary: agy
    resume: false
    timeout_ms: 1200000

  backend:
    provider: antigravity
    binary: agy
    resume: false
    timeout_ms: 1200000

  frontend:
    provider: antigravity
    binary: agy
    resume: false
    timeout_ms: 1200000

  auditor:
    provider: antigravity
    binary: agy
    resume: false
    timeout_ms: 900000

  validator:
    provider: antigravity
    binary: agy
    resume: false
    timeout_ms: 900000

  finalizer:
    provider: antigravity
    binary: agy
    resume: false
    timeout_ms: 900000

"""
_REFERENCE_SECURITY_PROVIDER_LINE = (
    "  # security_provider: claude   # claude | codex | antigravity; "
    "unset = auto\n"
)
_CLAUDE_SECURITY_PROVIDER_LINE = (
    "  # Same vendor as the implementer: the only CLI available.\n"
    "  security_provider: claude\n"
)
_CODEX_SECURITY_PROVIDER_LINE = (
    "  # Same vendor as the implementer: the only CLI available.\n"
    "  security_provider: codex\n"
)
_ANTIGRAVITY_SECURITY_PROVIDER_LINE = (
    "  # Same vendor as the implementer: the only CLI available.\n"
    "  security_provider: antigravity\n"
)
_REFERENCE_NORMALIZER_LINES = (
    "normalizer:\n"
    "  provider: claude\n"
    "  model: claude-haiku-5-5\n"
)
_CODEX_NORMALIZER_LINES = (
    "normalizer:\n"
    "  # All-Codex mapping: the normalizer runs through the Codex CLI login.\n"
    "  provider: codex\n"
)
_ANTIGRAVITY_NORMALIZER_LINES = (
    "normalizer:\n"
    "  # All-Antigravity mapping: the normalizer runs through the agy CLI login.\n"
    "  provider: antigravity\n"
)
# The template's top-level commit_mode line (anchored at the start of a line
# so an indented key never matches). The all-Codex mapping switches it: the
# Codex workspace-write sandbox keeps .git read-only, so an agent-mode
# implementer hands back blocked_implementation_failure on its first story.
_REFERENCE_COMMIT_MODE_RE = re.compile(r"^commit_mode: agent\n", re.MULTILINE)
_CODEX_COMMIT_MODE_LINE = (
    "# All-Codex mapping: the Codex workspace-write sandbox keeps .git read-only,\n"
    "# so the dispatcher commits implementer work.\n"
    "commit_mode: dispatcher\n"
)
_MAPPING_AGENTS_BLOCKS = {
    CLAUDE_MAPPING: _CLAUDE_AGENTS_BLOCK,
    CODEX_MAPPING: _CODEX_AGENTS_BLOCK,
    ANTIGRAVITY_MAPPING: _ANTIGRAVITY_AGENTS_BLOCK,
}
_MAPPING_SECURITY_PROVIDER_LINES = {
    CLAUDE_MAPPING: _CLAUDE_SECURITY_PROVIDER_LINE,
    CODEX_MAPPING: _CODEX_SECURITY_PROVIDER_LINE,
    ANTIGRAVITY_MAPPING: _ANTIGRAVITY_SECURITY_PROVIDER_LINE,
}
# Mappings whose normalizer cannot stay on the template's claude default
# because that CLI is not installed; the others keep the template lines.
_MAPPING_NORMALIZER_LINES = {
    CODEX_MAPPING: _CODEX_NORMALIZER_LINES,
    ANTIGRAVITY_MAPPING: _ANTIGRAVITY_NORMALIZER_LINES,
}
# Mappings that put a role on `provider: antigravity`; `init --tracker linear`
# refuses them because config load rejects antigravity in Linear mode.
_LINEAR_UNSUPPORTED_MAPPINGS = frozenset({ANTIGRAVITY_MAPPING})
# The reference mapping puts these roles on antigravity. `init --tracker
# linear` with that mapping rewrites their stanzas to claude so the written
# config loads; the other roles keep the template's mapping.
_LINEAR_REFERENCE_CLAUDE_ROLES = ("planner", "frontend")
_LINEAR_REFERENCE_CLAUDE_COMMENT = (
    "  # Linear mode does not support antigravity roles, so planner and "
    "frontend run on Claude."
)
_LINEAR_REFERENCE_CLAUDE_STANZA = (
    "    provider: claude",
    "    binary: claude",
    "    resume: false",
    "    timeout_ms: 1200000",
)
# The executable of a confirmed verification command becomes one
# `Bash(<exe>:*)` rule, so it must be a bare program name: no path separators,
# shell operators, quotes, or whitespace.
_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_AGENT_ROLE_RE = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_-]*):\s*$")
_AGENT_FIELD_RE = re.compile(r"^    (provider|binary):\s*(\S+)\s*$")
_VERIFICATION_TIMEOUT_LINE_RE = re.compile(
    r"^work_packet_verification_timeout_seconds:"
)


@dataclass(frozen=True)
class InitResult:
    template: str
    target_root: Path
    copied: tuple[Path, ...]
    skipped: tuple[Path, ...]
    conflicts: tuple[Path, ...]
    dry_run: bool
    force: bool
    provider_mapping: str = REFERENCE_MAPPING
    detected_providers: tuple[str, ...] = ()
    merged: tuple[Path, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    verification_command: str | None = None
    # Whether the Antigravity CLI (`agy`) is on PATH. It decides whether
    # `init` prints the agy permission entries. The provider mapping
    # auto-detect finds `agy` on its own through `detected_providers`.
    antigravity_detected: bool = False
    tracker: str = TRACKER_NONE
    # Set only by a tracker switch of an initialized repository: the mode the
    # repository was in, one "path: reason" line per conflict, and one
    # "path: what --force did" line per conflict --force resolved by leaving
    # a file as it is or by replacing an edited tracker block.
    switched_from: str | None = None
    conflict_details: tuple[str, ...] = ()
    forced: tuple[str, ...] = ()


class InitWorkflowError(RuntimeError):
    """Base error for target-repo initialization failures."""


class UnknownTemplateError(InitWorkflowError):
    def __init__(self, template: str) -> None:
        super().__init__(
            f"Unknown template {template!r}. Available templates: {REFERENCE_TEMPLATE}."
        )
        self.template = template


class UnknownProviderError(InitWorkflowError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            f"Unknown provider mapping {provider!r}. "
            f"Available mappings: {', '.join(PROVIDER_MAPPINGS)}."
        )
        self.provider = provider


class InvalidVerificationCommandError(InitWorkflowError):
    def __init__(self, command: str, token: str | None, detail: str) -> None:
        super().__init__(
            f"Invalid --verification-command {command!r}: {detail} "
            f"(rejected token: {token!r})."
        )
        self.command = command
        self.token = token


class UnknownTrackerError(InitWorkflowError):
    def __init__(self, tracker: str) -> None:
        super().__init__(
            f"Unknown tracker {tracker!r}. "
            f"Available trackers: {', '.join(TRACKER_CHOICES)}."
        )
        self.tracker = tracker


class InvalidTrackerOptionsError(InitWorkflowError):
    """A `--tracker-*` option is missing, misplaced, or has an invalid value."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"Invalid tracker options: {detail}")
        self.detail = detail


class TrackerUnsupportedMappingError(InvalidTrackerOptionsError):
    """`--tracker linear` with a mapping that puts roles on antigravity."""

    def __init__(self, mapping: str) -> None:
        super().__init__(
            f"Linear mode does not support provider: antigravity, and the "
            f"{mapping} mapping puts every role on it. Use --provider claude, "
            "--provider codex, or --tracker none."
        )
        self.mapping = mapping


class TrackerSwitchError(InitWorkflowError):
    """A tracker switch of an initialized repository cannot run as asked.

    Raised before anything is written, with ``dry_run`` and ``force`` too. The
    message is a single line.
    """

    def __init__(self, target_root: Path, problem: str) -> None:
        problem = _one_line(problem)
        super().__init__(
            f"Cannot switch the tracker mode of {target_root}: {problem}"
        )
        self.target_root = target_root
        self.problem = problem


class InitializedTargetError(InitWorkflowError):
    """An initialized target is not safe to change: refused before writing.

    The message is a single line. ``problem`` holds the specific reason.
    """

    def __init__(self, target_root: Path, problem: str) -> None:
        problem = _one_line(problem)
        super().__init__(
            f"Refusing to change the initialized repository {target_root}: {problem}"
        )
        self.target_root = target_root
        self.problem = problem


class InitConflictError(InitWorkflowError):
    def __init__(
        self,
        conflicts: tuple[Path, ...],
        message: str = "Target files already exist with different content.",
        details: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.conflicts = conflicts
        self.details = details


class TrackerSwitchConflictError(InitConflictError):
    """A tracker switch found files it cannot change safely; nothing was written.

    ``details`` holds one "path: reason" line per conflict.
    """

    def __init__(self, conflicts: tuple[Path, ...], details: tuple[str, ...]) -> None:
        super().__init__(
            conflicts,
            "Tracker switch conflicts; nothing was changed.",
            details,
        )


def template_files(template_root: Path = _TEMPLATE_ROOT) -> tuple[Path, ...]:
    files = []
    for path in template_root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(template_root)
        if relative_path in _EXCLUDED_TEMPLATE_FILES:
            continue
        # The template ships a Python hook script; never copy bytecode caches
        # that a local run may have left beside it.
        if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
            continue
        files.append(relative_path)
    return tuple(sorted(files))


def detect_provider_clis() -> tuple[str, ...]:
    """Return the provider CLIs found on PATH, in a stable order."""

    return tuple(name for name in PROVIDER_CLIS if shutil.which(name))


def resolve_provider_mapping(
    provider: str | None,
    available_providers: Iterable[str],
) -> str:
    """Pick the mapping to write: an explicit choice, else auto-detect.

    Auto-detection switches to a single-provider mapping only when that
    provider's CLI (Claude Code, Codex, or `agy`) is the only provider CLI
    installed; any other combination keeps the reference mapping.
    """

    if provider is not None:
        if provider not in PROVIDER_MAPPINGS:
            raise UnknownProviderError(provider)
        return provider
    available = set(available_providers)
    if available == {"claude"}:
        return CLAUDE_MAPPING
    if available == {"codex"}:
        return CODEX_MAPPING
    if available == {_ANTIGRAVITY_CLI}:
        return ANTIGRAVITY_MAPPING
    return REFERENCE_MAPPING


def render_config(template_text: str, mapping: str) -> str:
    """Return dispatch_config.yaml text for a provider mapping."""

    if mapping == REFERENCE_MAPPING:
        return template_text
    if mapping not in PROVIDER_MAPPINGS:
        raise UnknownProviderError(mapping)
    start = template_text.find("agents:\n")
    end = template_text.find("normalizer:\n", start) if start != -1 else -1
    if start == -1 or end == -1:
        raise InitWorkflowError(
            "Template dispatch_config.yaml no longer has the expected "
            "agents: and normalizer: blocks."
        )
    rendered = (
        template_text[:start] + _MAPPING_AGENTS_BLOCKS[mapping] + template_text[end:]
    )
    if rendered.count(_REFERENCE_SECURITY_PROVIDER_LINE) != 1:
        raise InitWorkflowError(
            "Template dispatch_config.yaml no longer has the expected "
            "audit.security_provider line."
        )
    rendered = rendered.replace(
        _REFERENCE_SECURITY_PROVIDER_LINE,
        _MAPPING_SECURITY_PROVIDER_LINES[mapping],
    )
    normalizer_lines = _MAPPING_NORMALIZER_LINES.get(mapping)
    if normalizer_lines is not None:
        if rendered.count(_REFERENCE_NORMALIZER_LINES) != 1:
            raise InitWorkflowError(
                "Template dispatch_config.yaml no longer has the expected "
                "normalizer provider and model lines."
            )
        rendered = rendered.replace(_REFERENCE_NORMALIZER_LINES, normalizer_lines)
    if mapping == CODEX_MAPPING:
        if len(_REFERENCE_COMMIT_MODE_RE.findall(rendered)) != 1:
            raise InitWorkflowError(
                "Template dispatch_config.yaml no longer has the expected "
                "commit_mode: agent line."
            )
        rendered = _REFERENCE_COMMIT_MODE_RE.sub(
            lambda _match: _CODEX_COMMIT_MODE_LINE, rendered
        )
    return rendered


def render_linear_reference_agents(config_text: str) -> str:
    """Move the reference mapping's antigravity roles to claude for Linear mode.

    Replaces the `planner` and `frontend` stanzas under the top-level
    `agents:` key with `provider: claude` stanzas and writes a one-line
    comment above `planner`. The new lines use the text's line ending; every
    other line is unchanged. Raises InitWorkflowError unless the `agents:`
    block holds exactly one stanza for each role.
    """

    lines = config_text.splitlines(keepends=True)
    newline = (_line_ending(lines[0]) if lines else "") or "\n"
    for role in _LINEAR_REFERENCE_CLAUDE_ROLES:
        agents_indexes = [
            index
            for index, line in enumerate(lines)
            if line.rstrip("\r\n") == "agents:"
        ]
        if len(agents_indexes) != 1:
            raise InitWorkflowError(
                "Template dispatch_config.yaml no longer has exactly one "
                "agents: block."
            )
        block_start = agents_indexes[0] + 1
        block_end = block_start
        while block_end < len(lines) and (
            not lines[block_end].strip() or lines[block_end][0] in " \t#"
        ):
            block_end += 1
        headers = [
            index
            for index in range(block_start, block_end)
            if lines[index].rstrip("\r\n") == f"  {role}:"
        ]
        if len(headers) != 1:
            raise InitWorkflowError(
                "Template dispatch_config.yaml no longer has exactly one "
                f"agents.{role} stanza."
            )
        header = headers[0]
        stop = header + 1
        while stop < block_end and lines[stop].startswith("    "):
            stop += 1
        replacement = [f"  {role}:"] + list(_LINEAR_REFERENCE_CLAUDE_STANZA)
        if role == "planner":
            replacement.insert(0, _LINEAR_REFERENCE_CLAUDE_COMMENT)
        lines[header:stop] = [f"{line}{newline}" for line in replacement]
    return "".join(lines)


def verification_executable(command: str) -> str:
    """Return the executable of a confirmed verification command.

    Raises InvalidVerificationCommandError for a command containing a shell
    control character (see `VERIFICATION_FORBIDDEN_CHARS`), an empty command,
    a command `shlex` cannot split, or a first token that is not a bare
    program name.
    """

    check_verification_command_chars(command)
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise InvalidVerificationCommandError(
            command, command, f"cannot split the command ({exc})"
        ) from exc
    if not tokens:
        raise InvalidVerificationCommandError(command, command, "the command is empty")
    executable = tokens[0]
    if not _EXECUTABLE_RE.fullmatch(executable):
        raise InvalidVerificationCommandError(
            command,
            executable,
            "the executable must be a bare program name matching "
            f"{_EXECUTABLE_RE.pattern} (no path separators or shell operators)",
        )
    return executable


def check_verification_command_chars(command: str) -> None:
    """Reject a confirmed verification command with a shell control character.

    `init` writes the command into `verification_allowed_prefixes`, whose
    entries may not contain any character in `VERIFICATION_FORBIDDEN_CHARS`,
    nor, once stripped, a tab, other non-space whitespace, or a run of two or
    more spaces (see `verification_whitespace_problem`).
    """

    for char in command:
        if char in VERIFICATION_FORBIDDEN_CHARS:
            raise InvalidVerificationCommandError(
                command,
                char,
                f"the command contains the shell control character {char!r}; "
                "a verification command must be a single command without "
                "chaining, pipes, redirection, substitution, or line breaks",
            )
    problem = verification_whitespace_problem(command.strip())
    if problem is not None:
        raise InvalidVerificationCommandError(
            command,
            command,
            f"the command contains {problem}; a verification command must use "
            "single spaces between words so it matches the agy grant built "
            "from it",
        )


def add_verification_allowed_prefixes(config_text: str, command: str) -> str:
    """Add `verification_allowed_prefixes` with ``command`` as its only entry.

    The key is inserted after the top-level
    `work_packet_verification_timeout_seconds:` line, with the command written
    as a JSON-quoted YAML string. The rest of the text is unchanged.
    """

    lines = config_text.splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if _VERIFICATION_TIMEOUT_LINE_RE.match(line)
    ]
    if len(matches) != 1:
        raise InitWorkflowError(
            "Template dispatch_config.yaml no longer has exactly one "
            "work_packet_verification_timeout_seconds line; cannot add "
            "verification_allowed_prefixes for the verification command."
        )
    index = matches[0]
    line = lines[index]
    newline = _line_ending(line) or "\n"
    if not _line_ending(line):
        lines[index] = line + newline
    lines.insert(
        index + 1,
        f"verification_allowed_prefixes:{newline}"
        f"  - {json.dumps(command)}{newline}",
    )
    rendered = "".join(lines)
    try:
        data = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise InitWorkflowError(
            "Rendered dispatch_config.yaml is not valid YAML."
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("verification_allowed_prefixes") != [command]
    ):
        raise InitWorkflowError(
            "Rendered dispatch_config.yaml does not carry the verification "
            "command as its only verification_allowed_prefixes entry."
        )
    return rendered


def is_interpreter_executable(executable: str) -> bool:
    """Return True when ``executable`` is one that ``interpreter_grants`` covers."""

    return executable in INTERPRETER_EXECUTABLES


def verification_allowed_tools(executable: str) -> tuple[str, ...]:
    """Return the Claude allowed_tools list that also allows ``executable``.

    An interpreter executable (``python``, ``python3``, ``pytest``) adds no
    rule: interpreter grants are the operator opt-in ``interpreter_grants``,
    and the dispatcher gate runs the verification command either way.
    """

    if is_interpreter_executable(executable):
        return CLAUDE_DEFAULT_ALLOWED_TOOLS
    rule = f"Bash({executable}:*)"
    if rule in CLAUDE_DEFAULT_ALLOWED_TOOLS:
        return CLAUDE_DEFAULT_ALLOWED_TOOLS
    return (*CLAUDE_DEFAULT_ALLOWED_TOOLS, rule)


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def add_verification_allowed_tools(config_text: str, executable: str) -> str:
    """Give every claude role in the `agents:` block an allowed_tools list.

    The list is inserted after each claude role's `binary:` line. Lines
    outside the `agents:` block and non-claude roles are left unchanged.
    """

    tools = verification_allowed_tools(executable)
    output: list[str] = []
    in_agents = False
    provider: str | None = None
    inserted: set[str] = set()
    role: str | None = None
    for line in config_text.splitlines(keepends=True):
        output.append(line)
        body = line.rstrip("\r\n")
        if not in_agents:
            if body.rstrip() == "agents:":
                in_agents = True
            continue
        if body.strip() and body[0] not in " \t#":
            in_agents = False
            continue
        role_match = _AGENT_ROLE_RE.match(body)
        if role_match:
            role = role_match.group(1)
            provider = None
            continue
        field_match = _AGENT_FIELD_RE.match(body)
        if field_match is None or role is None:
            continue
        key, value = field_match.groups()
        if key == "provider":
            provider = value
        elif provider == "claude" and role not in inserted:
            newline = _line_ending(line) or "\n"
            if not _line_ending(line):
                output[-1] = line + newline
            output.append(f"    allowed_tools:{newline}")
            output.extend(f"      - {rule}{newline}" for rule in tools)
            inserted.add(role)
    rendered = "".join(output)
    _check_allowed_tools(rendered, tools)
    return rendered


def _check_allowed_tools(rendered: str, tools: tuple[str, ...]) -> None:
    try:
        data = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise InitWorkflowError(
            "Rendered dispatch_config.yaml is not valid YAML."
        ) from exc
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        raise InitWorkflowError("Template dispatch_config.yaml has no agents block.")
    for name, agent in agents.items():
        if not isinstance(agent, dict):
            continue
        expected = list(tools) if agent.get("provider") == "claude" else None
        if agent.get("allowed_tools") != expected:
            raise InitWorkflowError(
                "Template dispatch_config.yaml no longer has the expected "
                f"provider/binary lines for role {name!r}; cannot add "
                "allowed_tools for the verification command."
            )


def build_tracker_config(
    tracker: str,
    *,
    team: str | None = None,
    project: str | None = None,
    label: str | None = None,
    mcp_server: str | None = None,
) -> TrackerConfig | None:
    """Validate the `init --tracker` options; return the block to write.

    Returns None for `--tracker none`. Raises UnknownTrackerError for an
    unknown tracker and InvalidTrackerOptionsError for a missing, misplaced,
    or invalid `--tracker-*` option. Nothing is written here.
    """

    if tracker not in TRACKER_CHOICES:
        raise UnknownTrackerError(tracker)
    if tracker == TRACKER_NONE:
        given = [
            name
            for name, value in (
                ("--tracker-team", team),
                ("--tracker-project", project),
                ("--tracker-label", label),
                ("--tracker-mcp-server", mcp_server),
            )
            if value is not None
        ]
        if given:
            raise InvalidTrackerOptionsError(
                f"{', '.join(given)} requires --tracker linear."
            )
        return None
    if team is None:
        raise InvalidTrackerOptionsError("--tracker linear requires --tracker-team.")
    if (project is None) == (label is None):
        raise InvalidTrackerOptionsError(
            "--tracker linear requires exactly one of --tracker-project and "
            "--tracker-label."
        )
    values: dict[str, str] = {"provider": TRACKER_LINEAR, "team": team}
    if project is not None:
        values["project"] = project
    else:
        values["label"] = label  # type: ignore[assignment]
    values.update(
        eligible_state=_DEFAULT_ELIGIBLE_STATE,
        in_progress_state=_DEFAULT_IN_PROGRESS_STATE,
        success_state=_DEFAULT_SUCCESS_STATE,
        mcp_server=DEFAULT_TRACKER_MCP_SERVER if mcp_server is None else mcp_server,
    )
    try:
        return TrackerConfig(**values)
    except ValidationError as exc:
        raise InvalidTrackerOptionsError(_tracker_error_detail(exc)) from exc


def _tracker_error_detail(exc: ValidationError) -> str:
    messages = []
    for error in exc.errors():
        message = str(error.get("msg", ""))
        message = message.removeprefix("Value error, ")
        for key, option in _TRACKER_OPTION_NAMES:
            message = message.replace(key, option)
        messages.append(" ".join(message.split()))
    return "; ".join(messages) or "the tracker options are invalid."


def _tracker_mapping(tracker: TrackerConfig) -> dict[str, str]:
    data: dict[str, str] = {"provider": tracker.provider, "team": tracker.team or ""}
    if tracker.project is not None:
        data["project"] = tracker.project
    else:
        data["label"] = tracker.label or ""
    data["eligible_state"] = tracker.eligible_state or ""
    data["in_progress_state"] = tracker.in_progress_state or ""
    data["success_state"] = tracker.success_state or ""
    data["mcp_server"] = tracker.mcp_server or ""
    return data


def _tracker_block_text(tracker: TrackerConfig, newline: str) -> str:
    """The managed tracker block, markers included, without a final newline."""

    dumped = yaml.safe_dump(
        {"tracker": _tracker_mapping(tracker)},
        sort_keys=False,
        default_flow_style=False,
    )
    begin, end = _TRACKER_CONFIG_MARKERS
    body = "".join(f"{line}{newline}" for line in dumped.splitlines())
    return f"{begin}{newline}{body}{end}"


def add_tracker_block(config_text: str, tracker: TrackerConfig) -> str:
    """Append the managed `tracker` block to dispatch_config.yaml text.

    The block uses the template text's line ending and is written as one line
    ending, the block, and one line ending, so removing exactly those bytes
    restores the original text (a text ending in a newline gets a blank line
    before the block). The rest of the text, including the commented
    `# tracker:` example, is unchanged.
    """

    lines = config_text.splitlines(keepends=True)
    newline = (_line_ending(lines[0]) if lines else "") or "\n"
    mapping = _tracker_mapping(tracker)
    block = _tracker_block_text(tracker, newline)
    rendered = f"{config_text}{newline}{block}{newline}"
    try:
        data = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise InitWorkflowError(
            "Rendered dispatch_config.yaml is not valid YAML."
        ) from exc
    if not isinstance(data, dict) or data.get("tracker") != mapping:
        raise InitWorkflowError(
            "Rendered dispatch_config.yaml does not carry the tracker block."
        )
    return rendered


def _linear_block(content: str) -> bytes:
    begin, end = _LINEAR_TEXT_MARKERS
    return begin + b"\n" + content.rstrip("\r\n").encode("utf-8") + b"\n" + end


def _linear_handbook_text(*, tpm_configured: bool = False) -> str:
    # `init` always renders the tpm-absent shape: the planner section holds
    # the selection rules and no tpm section is emitted.
    roles = [
        role
        for role in LINEAR_PROMPT_ROLES
        if tpm_configured or role != "tpm"
    ]
    sections = [linear_evidence_contract(), linear_issue_template()]
    sections.extend(
        linear_role_section(role, tpm_configured=tpm_configured) for role in roles
    )
    return "\n\n".join(section.strip("\r\n") for section in sections)


def _append_linear_handbook(template: bytes) -> bytes:
    block = _linear_block(_linear_handbook_text())
    if b"\r\n" in template:
        block = block.replace(b"\n", b"\r\n")
        newline = b"\r\n"
    else:
        newline = b"\n"
    separator = newline if template and not template.endswith(b"\n") else b""
    return template + separator + block + newline


def _merge_linear_agents(existing: bytes) -> bytes:
    """Add or replace the Linear block in merged AGENTS.md bytes.

    Raises ValueError for malformed Linear markers, which `init` reports as a
    conflict.
    """

    begin, end = _LINEAR_TEXT_MARKERS
    block = _linear_block(linear_agents_section())
    span = _marker_span(existing, begin, end)
    if span:
        return existing[: span[0]] + block + existing[span[1] :]
    separator = b"\n" if existing and not existing.endswith(b"\n") else b""
    return existing + separator + block + b"\n"


def render_validator_agent_tools(template: bytes, mcp_server: str) -> bytes:
    """Rewrite the `tools:` frontmatter line of the handoff-validator agent.

    The new line lists the base tools followed by the validator's Linear MCP
    tools for `mcp_server`. The line keeps its original ending; every other
    byte is unchanged. Raises InitWorkflowError unless the leading `---`
    frontmatter holds exactly one line that starts with `tools: `.
    """

    return render_linear_agent_tools(_VALIDATOR_AGENT_PATH, template, mcp_server)


def render_linear_agent_tools(
    relative_path: Path, template: bytes, mcp_server: str
) -> bytes:
    """Rewrite the `tools:` frontmatter line of one Claude subagent file.

    `relative_path` must be a key of `_LINEAR_AGENT_TOOLS`. The new line lists
    that file's built-in tools followed by its role's Linear MCP tools for
    `mcp_server`. The line keeps its original ending; every other byte is
    unchanged. Raises InitWorkflowError unless the leading `---` frontmatter
    holds exactly one line that starts with `tools: `.
    """

    lines = template.splitlines(keepends=True)
    tools_indexes = _frontmatter_tools_indexes(lines)
    if len(tools_indexes) != 1:
        raise InitWorkflowError(
            f"Template {relative_path.as_posix()} must have exactly one "
            "'tools: ' line in its frontmatter."
        )
    index = tools_indexes[0]
    line = lines[index]
    ending = line[len(line.rstrip(b"\r\n")) :]
    lines[index] = _linear_tools_line(relative_path, mcp_server) + ending
    return b"".join(lines)


def _frontmatter_tools_indexes(lines: list[bytes]) -> list[int]:
    """Indexes of the `tools: ` lines inside a leading `---` frontmatter."""

    indexes: list[int] = []
    if lines and lines[0].rstrip(b"\r\n") == b"---":
        for index in range(1, len(lines)):
            if lines[index].rstrip(b"\r\n") == b"---":
                break
            if lines[index].startswith(b"tools: "):
                indexes.append(index)
    return indexes


def _linear_tools_line(relative_path: Path, mcp_server: str) -> bytes:
    """The Linear-mode `tools:` line of one subagent file, without an ending."""

    role, base_tools = _LINEAR_AGENT_TOOLS[relative_path]
    tools = ", ".join(linear_tools_for_role(role, mcp_server))
    return f"tools: {base_tools}, {tools}".encode("utf-8")


def _config_is_rendered(
    mapping: str, executable: str | None, tracker: TrackerConfig | None = None
) -> bool:
    return mapping != REFERENCE_MAPPING or executable is not None or tracker is not None


def _source_is_rendered(
    relative_path: Path,
    mapping: str,
    executable: str | None,
    tracker: TrackerConfig | None,
) -> bool:
    if relative_path == _CONFIG_PATH:
        return _config_is_rendered(mapping, executable, tracker)
    return (
        relative_path == _HANDBOOK_PATH or relative_path in _LINEAR_AGENT_TOOLS
    ) and tracker is not None


def _source_bytes(
    relative_path: Path,
    mapping: str,
    executable: str | None = None,
    verification_command: str | None = None,
    tracker: TrackerConfig | None = None,
) -> bytes:
    source_path = _TEMPLATE_ROOT / relative_path
    if relative_path == _CONFIG_PATH and _config_is_rendered(
        mapping, executable, tracker
    ):
        if mapping == REFERENCE_MAPPING:
            # Keep the template's bytes, line endings included.
            text = source_path.read_bytes().decode("utf-8")
            if tracker is not None:
                # Config load rejects antigravity roles in Linear mode.
                text = render_linear_reference_agents(text)
        else:
            text = render_config(source_path.read_text(encoding="utf-8"), mapping)
        # An interpreter executable writes no allowed_tools list: the claude
        # roles keep the default list, so a later `interpreter_grants: true`
        # still reaches them. init never writes `interpreter_grants` itself.
        if executable is not None and not is_interpreter_executable(executable):
            text = add_verification_allowed_tools(text, executable)
        if verification_command is not None:
            text = add_verification_allowed_prefixes(text, verification_command)
        if tracker is not None:
            text = add_tracker_block(text, tracker)
        return text.encode("utf-8")
    if relative_path == _HANDBOOK_PATH and tracker is not None:
        return _append_linear_handbook(source_path.read_bytes())
    if relative_path in _LINEAR_AGENT_TOOLS and tracker is not None:
        return render_linear_agent_tools(
            relative_path,
            source_path.read_bytes(),
            tracker.mcp_server or DEFAULT_TRACKER_MCP_SERVER,
        )
    return source_path.read_bytes()


def _marker_span(content: bytes, begin: bytes, end: bytes) -> tuple[int, int] | None:
    if begin not in content and end not in content:
        return None
    if content.count(begin) != 1 or content.count(end) != 1:
        raise ValueError("Expected exactly one managed marker pair")
    start = content.index(begin)
    stop = content.index(end) + len(end)
    if start >= stop - len(end):
        raise ValueError("Reversed managed markers")
    for offset, marker in ((start, begin), (stop - len(end), end)):
        tail = content[offset + len(marker) :]
        if (offset and content[offset - 1 : offset] != b"\n") or (
            tail and not tail.startswith((b"\n", b"\r\n"))
        ):
            raise ValueError("Managed markers must occupy separate lines")
    return start, stop


def _merge_text(path: Path, existing: bytes, source: bytes) -> bytes:
    begin, end = _TEXT_MARKERS[path]
    unmarked_template = existing == source
    source_span = _marker_span(source, begin, end)
    block = (
        source[source_span[0] : source_span[1]]
        if source_span
        else begin + b"\n" + source.rstrip(b"\r\n") + b"\n" + end
    )
    span = _marker_span(existing, begin, end)
    if span:
        return existing[: span[0]] + block + existing[span[1] :]
    if unmarked_template:
        return block + b"\n"
    separator = b"\n" if existing and not existing.endswith(b"\n") else b""
    return existing + separator + block + b"\n"


def _merge_settings(existing: bytes, source: bytes) -> bytes:
    target = json.loads(existing)
    template = json.loads(source)
    if not isinstance(target, dict):
        raise TypeError("Settings must be an object")
    permissions = target.setdefault("permissions", {})
    hooks = target.setdefault("hooks", {})
    if not isinstance(permissions, dict) or not isinstance(hooks, dict):
        raise TypeError("Permissions and hooks must be objects")
    deny = permissions.setdefault("deny", [])
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(deny, list) or not all(isinstance(item, str) for item in deny):
        raise TypeError("Permission deny must be a list of strings")
    if not isinstance(entries, list):
        raise TypeError("PreToolUse must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list):
            raise TypeError("Invalid PreToolUse entry")
        if not all(isinstance(hook, dict) for hook in entry.get("hooks", [])):
            raise TypeError("Invalid PreToolUse hook")
    permissions["deny"] = list(dict.fromkeys(deny + template["permissions"]["deny"]))
    for entry in template["hooks"]["PreToolUse"]:
        missing = [
            hook
            for hook in entry["hooks"]
            if not any(
                candidate.get("matcher") == entry.get("matcher")
                and any(
                    item.get("command") == hook["command"]
                    for item in candidate.get("hooks", [])
                )
                for candidate in entries
            )
        ]
        if missing:
            entries.append({**entry, "hooks": missing})
    return (json.dumps(target, indent=2) + "\n").encode("utf-8")


def _has_linked_destination(target_root: Path, relative_path: Path) -> bool:
    """Reject linked destinations and paths with non-directory parents."""
    current = target_root
    for part in relative_path.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            return True
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
            return True
        if current != target_root / relative_path and not stat.S_ISDIR(metadata.st_mode):
            return True
        # Windows exposes reparse attributes on Python 3.11 too; pathlib's
        # is_junction() only exists from 3.12. Do not follow links to inspect them.
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return True
    return False


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _is_link(metadata: os.stat_result) -> bool:
    # Windows exposes junctions and other reparse points through
    # st_file_attributes; do not follow them.
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


_CONFIG_FIX_HINT = "Fix or remove it, then run init again."
_BETWEEN_ASSIGNMENTS_HINT = (
    "Wait until the handoff is between assignments (next_agent planner, tpm, "
    "or user with no tracker_issue), then run init again."
)


def _config_error_detail(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        messages = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "")).removeprefix("Value error, ")
            messages.append(f"{location}: {message}" if location else message)
        return "; ".join(messages) or "the config is invalid"
    return str(exc) or type(exc).__name__


def _load_initialized_config(root: Path) -> DispatchConfig:
    config_path = root / _CONFIG_PATH
    name = _CONFIG_PATH.as_posix()
    try:
        metadata = config_path.lstat()
    except OSError as exc:
        raise InitializedTargetError(
            root, f"{name} cannot be inspected ({exc}). {_CONFIG_FIX_HINT}"
        ) from exc
    if _is_link(metadata):
        raise InitializedTargetError(
            root,
            f"{name} is a symbolic link or junction, not a regular file. "
            f"{_CONFIG_FIX_HINT}",
        )
    if stat.S_ISDIR(metadata.st_mode):
        raise InitializedTargetError(
            root, f"{name} is a directory, not a config file. {_CONFIG_FIX_HINT}"
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise InitializedTargetError(
            root, f"{name} is not a regular file. {_CONFIG_FIX_HINT}"
        )
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InitializedTargetError(
            root, f"{name} cannot be read ({exc}). {_CONFIG_FIX_HINT}"
        ) from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InitializedTargetError(
            root, f"{name} is not valid YAML ({exc}). {_CONFIG_FIX_HINT}"
        ) from exc
    if loaded is not None and not isinstance(loaded, dict):
        raise InitializedTargetError(
            root, f"{name} is not a YAML mapping. {_CONFIG_FIX_HINT}"
        )
    try:
        return load_dispatch_config(repo_root=root, config_path=config_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise InitializedTargetError(
            root,
            f"{name} fails config validation ({_config_error_detail(exc)}). "
            f"{_CONFIG_FIX_HINT}",
        ) from exc


def _check_no_run_state(root: Path) -> None:
    for relative in (_DISPATCH_LOCK_PATH, *_RUN_STATE_PATHS):
        if os.path.lexists(root / relative):
            raise InitializedTargetError(
                root,
                f"{relative.as_posix()} exists, so a dispatcher run is active or "
                "was interrupted. Stop the run or finish its recovery, then run "
                "init again.",
            )


def _starter_handoff_bytes() -> bytes | None:
    try:
        return (_TEMPLATE_ROOT / "docs" / "handoff" / "HANDOFF.md").read_bytes()
    except OSError:
        return None


def _check_handoff_between_assignments(root: Path, config: DispatchConfig) -> None:
    relative = Path(config.handoff_path)
    name = relative.as_posix()
    handoff = root / relative
    if not os.path.lexists(handoff):
        return
    try:
        content = handoff.read_bytes()
    except OSError as exc:
        raise InitializedTargetError(
            root,
            f"the handoff {name} cannot be read ({exc}), so init cannot tell "
            f"whether an assignment is active. {_BETWEEN_ASSIGNMENTS_HINT}",
        ) from exc
    if content == _starter_handoff_bytes():
        return
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InitializedTargetError(
            root,
            f"the handoff {name} is not UTF-8 text ({exc}), so init cannot tell "
            f"whether an assignment is active. {_BETWEEN_ASSIGNMENTS_HINT}",
        ) from exc
    unknown = (
        f"the handoff {name} does not show a between-assignments state"
    )
    try:
        frontmatter = parse_handoff_frontmatter_text(text)
    except HandoffFrontmatterError as exc:
        raise InitializedTargetError(
            root, f"{unknown}: {exc} {_BETWEEN_ASSIGNMENTS_HINT}"
        ) from exc
    if frontmatter is None:
        raise InitializedTargetError(
            root,
            f"{unknown}: it has no YAML routing frontmatter. "
            f"{_BETWEEN_ASSIGNMENTS_HINT}",
        )
    if frontmatter.tracker_issue:
        raise InitializedTargetError(
            root,
            f"the handoff {name} holds a tracker lease (tracker_issue: "
            f"{frontmatter.tracker_issue}). {_BETWEEN_ASSIGNMENTS_HINT}",
        )
    next_agent = normalize_next_agent_value(frontmatter.next_agent)
    if next_agent in _ACTIVE_ASSIGNMENT_ROLES:
        raise InitializedTargetError(
            root,
            f"the handoff {name} has an active assignment (next_agent: "
            f"{next_agent}). {_BETWEEN_ASSIGNMENTS_HINT}",
        )
    if next_agent not in _BETWEEN_ASSIGNMENT_ROLES:
        raise InitializedTargetError(
            root,
            f"{unknown}: next_agent is {frontmatter.next_agent!r}. "
            f"{_BETWEEN_ASSIGNMENTS_HINT}",
        )
    # The dispatcher routes on the validated frontmatter, where a status takes
    # precedence over next_agent and an invalid or conflicting field routes
    # nowhere. Classify that effective route, not next_agent alone.
    decision = route_handoff(text)
    if decision.route in _ACTIVE_ASSIGNMENT_ROLES:
        raise InitializedTargetError(
            root,
            f"the handoff {name} has an active assignment (status: "
            f"{frontmatter.status} routes to {decision.route}). "
            f"{_BETWEEN_ASSIGNMENTS_HINT}",
        )
    if decision.route not in _BETWEEN_ASSIGNMENT_ROLES:
        detail = " ".join(
            (decision.warnings[0] if decision.warnings else decision.reasoning).split()
        )
        raise InitializedTargetError(
            root,
            f"{unknown}: the dispatcher cannot route it ({detail}). "
            f"{_BETWEEN_ASSIGNMENTS_HINT}",
        )


def preflight_initialized_target(target_root: Path) -> DispatchConfig | None:
    """Read-only safety check for a target that already has dispatch_config.yaml.

    Returns ``None`` for a fresh target (no ``dispatch_config.yaml`` entry,
    checked with ``os.path.lexists`` so a dangling link counts as present).
    For an initialized target, returns its validated config when it is safe to
    change, and otherwise raises ``InitializedTargetError`` with a single-line
    message. It refuses a config that is a directory, a link, unreadable,
    invalid YAML, or fails config validation; a present ``.dispatch.lock`` or
    dispatcher run-state file; and a handoff (the config's ``handoff_path``)
    that holds a ``tracker_issue`` lease, routes to an implementing or
    reviewing role, or cannot be read as a between-assignments state. The
    route is the one the dispatcher would take: a ``status`` takes precedence
    over ``next_agent``, and frontmatter the router rejects (an unrecognized
    status, a status that conflicts with ``next_agent``, a missing reason)
    counts as unknown. A missing handoff, the starter template, and valid
    frontmatter whose effective route is planner, tpm, or user with no
    ``tracker_issue`` pass. Nothing is written.
    """

    root = target_root.expanduser().resolve()
    if not os.path.lexists(root / _CONFIG_PATH):
        return None
    _check_no_run_state(root)
    config = _load_initialized_config(root)
    _check_handoff_between_assignments(root, config)
    return config


# Tracker switch of an initialized repository (LIN-14). The switch is a
# bounded transformation: it edits only the managed tracker block of
# dispatch_config.yaml, the Linear blocks of AGENTS.md and HANDBOOK.md, and
# the `tools:` line of each Claude subagent in _LINEAR_AGENT_TOOLS. Every
# destination is planned before anything is written.
_SWITCH_UNCHANGED = "unchanged"
_SWITCH_CHANGE = "change"
_SWITCH_CONFLICT = "conflict"
_SWITCH_FORCE_HINT = (
    "--force leaves conflicting files as they are, or replaces or removes "
    "a managed block, and reports each one."
)


@dataclass(frozen=True)
class _SwitchItem:
    path: Path
    action: str
    # Bytes to write for a change, or for a conflict --force resolves by
    # rewriting the file (an edited tracker block). None for a conflict
    # --force resolves by leaving the file as it is.
    content: bytes | None = None
    reason: str = ""
    # Whether --force may resolve this conflict. dispatch_config.yaml marker
    # and key conflicts and unsafe paths are never forceable.
    forceable: bool = False
    force_note: str = ""


def _first_line_ending(content: bytes) -> bytes:
    lines = content.splitlines(keepends=True)
    if lines:
        line = lines[0]
        if line.endswith(b"\r\n"):
            return b"\r\n"
        if line.endswith(b"\n"):
            return b"\n"
    return b"\n"


def _with_newline(block: bytes, newline: bytes) -> bytes:
    return block.replace(b"\n", b"\r\n") if newline == b"\r\n" else block


def _block_newline(block: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in block else b"\n"


def _unsafe_destination(root: Path, relative: Path) -> bool:
    return _has_linked_destination(root, relative) or (root / relative).is_dir()


def _unsafe_item(relative: Path) -> _SwitchItem:
    return _SwitchItem(
        relative,
        _SWITCH_CONFLICT,
        reason=(
            "is a link, junction, hard link, or directory, or has a "
            "non-directory or linked parent; init never writes through it"
        ),
    )


def _insert_text_block(content: bytes, block: bytes, newline: bytes) -> bytes:
    """Append a Linear block so `_remove_text_block` restores ``content``."""

    if not content or content.endswith(b"\n"):
        return content + block + newline
    return content + newline + block


def _remove_text_block(content: bytes, span: tuple[int, int]) -> bytes:
    start, stop = span
    if stop == len(content) and start > 0:
        # A block at the end with no line ending after it: drop the line
        # ending before it instead.
        start -= 2 if content[start - 2 : start] == b"\r\n" else 1
    elif content[stop : stop + 2] == b"\r\n":
        stop += 2
    elif content[stop : stop + 1] == b"\n":
        stop += 1
    return content[:start] + content[stop:]


def _text_block_restorable(original: bytes, removed: bytes, block: bytes) -> bool:
    """Whether a switch back to Linear would rebuild ``original`` exactly.

    A switch to Linear appends the block at the end of ``removed`` in the
    file's line endings (`_insert_text_block`), so a block that other content
    follows cannot be put back in its place.
    """

    newline = _first_line_ending(removed)
    again = _with_newline(block.replace(b"\r\n", b"\n"), newline)
    return _insert_text_block(removed, again, newline) == original


def _config_block_restorable(original: bytes, removed: bytes, block: bytes) -> bool:
    """Whether a switch back to Linear would rebuild ``original`` exactly.

    The config counterpart of `_text_block_restorable`: `_plan_config_switch`
    appends the tracker block after a line ending, at the end of the file.
    """

    newline = _first_line_ending(removed)
    again = _with_newline(block.replace(b"\r\n", b"\n"), newline)
    return removed + newline + again + newline == original


def _remove_config_block(content: bytes, span: tuple[int, int]) -> bytes:
    """Remove the tracker block with the line endings `add_tracker_block` adds."""

    start, stop = span
    if start > 0:
        start -= 2 if content[start - 2 : start] == b"\r\n" else 1
    if content[stop : stop + 2] == b"\r\n":
        stop += 2
    elif content[stop : stop + 1] == b"\n":
        stop += 1
    return content[:start] + content[stop:]


def _canonical_tracker_block(mapping: object, newline: bytes) -> bytes | None:
    """The block init writes for these tracker values, or None if it cannot."""

    if not isinstance(mapping, dict):
        return None
    values = {key: mapping.get(key) for key in ("team", "project", "label", "mcp_server")}
    if not all(value is None or isinstance(value, str) for value in values.values()):
        return None
    try:
        tracker = build_tracker_config(
            TRACKER_LINEAR,
            team=values["team"],
            project=values["project"],
            label=values["label"],
            mcp_server=values["mcp_server"],
        )
    except InitWorkflowError:
        return None
    if tracker is None:
        return None
    return _tracker_block_text(tracker, newline.decode("ascii")).encode("utf-8")


def _plan_config_switch(
    root: Path, config: DispatchConfig, tracker: TrackerConfig | None
) -> _SwitchItem:
    relative = _CONFIG_PATH
    if _unsafe_destination(root, relative):
        return _unsafe_item(relative)
    raw = (root / relative).read_bytes()
    begin, end = (marker.encode("utf-8") for marker in _TRACKER_CONFIG_MARKERS)
    try:
        span = _marker_span(raw, begin, end)
    except ValueError:
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            reason=(
                "its '# llm-handoff:tracker' markers are malformed or repeated; "
                "fix them by hand"
            ),
        )
    base = raw if span is None else _remove_config_block(raw, span)
    try:
        base_data = yaml.safe_load(base.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            reason="the config outside the managed tracker block is not valid YAML",
        )
    if isinstance(base_data, dict) and "tracker" in base_data:
        if span is None and tracker is None and config.tracker.provider == TRACKER_NONE:
            # A hand-written `tracker: {provider: none}` already selects none.
            return _SwitchItem(relative, _SWITCH_UNCHANGED)
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            reason=(
                "it has a tracker key outside the '# llm-handoff:tracker' "
                "block, which init does not manage; remove it by hand"
            ),
        )
    edited = False
    block = b""
    if span is not None:
        block = raw[span[0] : span[1]]
        try:
            block_data = yaml.safe_load(block.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            block_data = None
        if (
            not isinstance(block_data, dict)
            or set(block_data) != {"tracker"}
            or not isinstance(block_data["tracker"], dict)
        ):
            return _SwitchItem(
                relative,
                _SWITCH_CONFLICT,
                reason=(
                    "its managed tracker block holds something other than one "
                    "tracker mapping; move other settings out of the block"
                ),
            )
        edited = block != _canonical_tracker_block(
            block_data["tracker"], _block_newline(block)
        )
    if tracker is None:
        if span is None:
            return _SwitchItem(relative, _SWITCH_UNCHANGED)
        if edited:
            return _SwitchItem(
                relative,
                _SWITCH_CONFLICT,
                content=base,
                reason="its managed tracker block has local edits",
                forceable=True,
                force_note=(
                    "removed the locally edited managed tracker block; its "
                    "settings are not kept"
                ),
            )
        if not _config_block_restorable(raw, base, block):
            return _SwitchItem(
                relative,
                _SWITCH_CONFLICT,
                content=base,
                reason=(
                    "other content follows its managed tracker block, so a "
                    "switch back to Linear could not put the block back in "
                    "place; move the block to the end of the file"
                ),
                forceable=True,
                force_note=(
                    "removed the managed tracker block; a switch back to "
                    "Linear adds it at the end of the file, after the content "
                    "that followed it"
                ),
            )
        return _SwitchItem(relative, _SWITCH_CHANGE, content=base)
    if span is None:
        newline = _first_line_ending(raw)
        desired = _tracker_block_text(tracker, newline.decode("ascii")).encode("utf-8")
        return _SwitchItem(
            relative, _SWITCH_CHANGE, content=raw + newline + desired + newline
        )
    desired = _tracker_block_text(
        tracker, _block_newline(block).decode("ascii")
    ).encode("utf-8")
    if block == desired:
        return _SwitchItem(relative, _SWITCH_UNCHANGED)
    replaced = raw[: span[0]] + desired + raw[span[1] :]
    if edited:
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            content=replaced,
            reason="its managed tracker block has local edits",
            forceable=True,
            force_note=(
                "replaced the locally edited managed tracker block; its old "
                "settings are not kept"
            ),
        )
    return _SwitchItem(relative, _SWITCH_CHANGE, content=replaced)


def _plan_linear_text_switch(
    root: Path,
    relative: Path,
    desired: str | None,
    known: tuple[str, ...],
    *,
    guarded: bool,
) -> _SwitchItem:
    """Plan the Linear block of AGENTS.md or HANDBOOK.md.

    ``desired`` is the block text for Linear mode, or None to remove the
    block. ``known`` lists every block text init may have written, so a block
    that matches none of them (in LF or CRLF) counts as locally edited.
    ``guarded`` says whether the block text tells agents to ignore it when
    ``tracker.provider`` is not ``linear`` (AGENTS.md); --force may leave only
    a guarded, locally edited block behind on a switch to none.
    """

    if _unsafe_destination(root, relative):
        return _unsafe_item(relative)
    path = root / relative
    if not path.exists():
        if desired is None:
            return _SwitchItem(relative, _SWITCH_UNCHANGED)
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            reason="it is missing; run init without --tracker to restore it",
            forceable=True,
            force_note="left missing; the Linear section was not added",
        )
    raw = path.read_bytes()
    begin, end = _LINEAR_TEXT_MARKERS
    try:
        span = _marker_span(raw, begin, end)
    except ValueError:
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            reason="its '<!-- llm-handoff:linear -->' markers are malformed or repeated",
            forceable=True,
            force_note=(
                "left as it is, with its malformed Linear markers"
                + ("; the Linear section was not added" if desired is not None else "")
            ),
        )
    if span is None:
        if desired is None:
            return _SwitchItem(relative, _SWITCH_UNCHANGED)
        newline = _first_line_ending(raw)
        block = _with_newline(_linear_block(desired), newline)
        return _SwitchItem(
            relative, _SWITCH_CHANGE, content=_insert_text_block(raw, block, newline)
        )
    block = raw[span[0] : span[1]]
    known_blocks = {
        _with_newline(_linear_block(text), newline)
        for text in known
        for newline in (b"\n", b"\r\n")
    }
    if desired is None:
        removed = _remove_text_block(raw, span)
        if block in known_blocks:
            if _text_block_restorable(raw, removed, block):
                return _SwitchItem(relative, _SWITCH_CHANGE, content=removed)
            # --force removes the known block and keeps the content after
            # it, like the config's tracker block, so no Linear instructions
            # are left behind in file mode.
            return _SwitchItem(
                relative,
                _SWITCH_CONFLICT,
                content=removed,
                reason=(
                    "other content follows its Linear section, so a switch "
                    "back to Linear could not put the section back in place; "
                    "move the section to the end of the file"
                ),
                forceable=True,
                force_note=(
                    "removed the Linear section and kept the content after "
                    "it; a switch back to Linear adds the section at the end "
                    "of the file"
                ),
            )
        if guarded:
            return _SwitchItem(
                relative,
                _SWITCH_CONFLICT,
                reason="its Linear section has local edits",
                forceable=True,
                force_note=(
                    "left as it is; the locally edited Linear section stays (it "
                    "tells agents to ignore it when tracker.provider is not linear)"
                ),
            )
        # An unguarded section (the handbook's) that every role reads would
        # keep giving Linear instructions in file mode, so --force removes it.
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            content=removed,
            reason="its Linear section has local edits",
            forceable=True,
            force_note=(
                "removed the locally edited Linear section; its edits are not "
                "kept, because the section has no tracker.provider guard"
            ),
        )
    wanted = _with_newline(_linear_block(desired), _block_newline(block))
    if block == wanted:
        return _SwitchItem(relative, _SWITCH_UNCHANGED)
    if block in known_blocks:
        return _SwitchItem(
            relative,
            _SWITCH_CHANGE,
            content=raw[: span[0]] + wanted + raw[span[1] :],
        )
    return _SwitchItem(
        relative,
        _SWITCH_CONFLICT,
        reason="its Linear section has local edits",
        forceable=True,
        force_note="left as it is; the locally edited Linear section was not updated",
    )


def _template_tools_line(relative: Path) -> bytes:
    lines = (_TEMPLATE_ROOT / relative).read_bytes().splitlines(keepends=True)
    indexes = _frontmatter_tools_indexes(lines)
    if len(indexes) != 1:
        raise InitWorkflowError(
            f"Template {relative.as_posix()} must have exactly one "
            "'tools: ' line in its frontmatter."
        )
    return lines[indexes[0]].rstrip(b"\r\n")


def _plan_agent_tools_switch(
    root: Path,
    relative: Path,
    old_server: str | None,
    new_server: str | None,
) -> _SwitchItem | None:
    """Plan the `tools:` line of one Claude subagent file.

    ``old_server`` is the current Linear MCP server name (None in file mode)
    and ``new_server`` the one to write (None to switch to file mode). A
    missing subagent file has nothing to grant, so it is not planned.
    """

    if _unsafe_destination(root, relative):
        return _unsafe_item(relative)
    path = root / relative
    if not path.exists():
        return None
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    indexes = _frontmatter_tools_indexes(lines)
    if len(indexes) != 1:
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            reason="its frontmatter does not have exactly one 'tools: ' line",
            forceable=True,
            force_note="left as it is; its tools line was not changed",
        )
    index = indexes[0]
    line = lines[index]
    current = line.rstrip(b"\r\n")
    file_line = _template_tools_line(relative)
    known = {file_line}
    if old_server is not None:
        known.add(_linear_tools_line(relative, old_server))
    wanted = file_line if new_server is None else _linear_tools_line(relative, new_server)
    if current == wanted:
        return _SwitchItem(relative, _SWITCH_UNCHANGED)
    if current not in known:
        material = (
            "it keeps its locally modified tools line, which may still list "
            "Linear MCP tools"
            if new_server is None
            else "it keeps its locally modified tools line without the Linear "
            "MCP tools for this server"
        )
        return _SwitchItem(
            relative,
            _SWITCH_CONFLICT,
            reason="its tools line has local edits",
            forceable=True,
            force_note=f"left as it is; {material}",
        )
    lines[index] = wanted + line[len(current) :]
    return _SwitchItem(relative, _SWITCH_CHANGE, content=b"".join(lines))


def _validate_switched_config(root: Path, content: bytes, mode: str) -> None:
    """Load the planned config with the dispatcher's own validation.

    The bytes go to a temporary file outside the target, so nothing in the
    target is written.
    """

    name = _CONFIG_PATH.as_posix()
    with tempfile.TemporaryDirectory(prefix="llm-handoff-switch-") as scratch:
        candidate = Path(scratch) / _CONFIG_PATH.name
        candidate.write_bytes(content)
        try:
            loaded = load_dispatch_config(repo_root=root, config_path=candidate)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise TrackerSwitchError(
                root,
                f"{name} would fail config validation in {mode} mode "
                f"({_config_error_detail(exc)}). Change it by hand, then run "
                "init again. Nothing was changed.",
            ) from exc
    if loaded.tracker.provider != mode:
        raise TrackerSwitchError(
            root,
            f"{name} would select tracker mode {loaded.tracker.provider}, not "
            f"{mode}. Nothing was changed.",
        )


def _switch_tracker_mode(
    root: Path,
    config: DispatchConfig,
    tracker: TrackerConfig | None,
    *,
    template: str,
    dry_run: bool,
    force: bool,
) -> InitResult:
    old_mode = config.tracker.provider
    new_mode = TRACKER_NONE if tracker is None else TRACKER_LINEAR
    old_server = (
        (config.tracker.mcp_server or DEFAULT_TRACKER_MCP_SERVER)
        if old_mode == TRACKER_LINEAR
        else None
    )
    new_server = (
        None if tracker is None else (tracker.mcp_server or DEFAULT_TRACKER_MCP_SERVER)
    )
    tpm_configured = "tpm" in config.agents

    items: list[_SwitchItem] = []
    for relative in _LINEAR_AGENT_TOOLS:
        item = _plan_agent_tools_switch(root, relative, old_server, new_server)
        if item is not None:
            items.append(item)
    items.append(
        _plan_linear_text_switch(
            root,
            _AGENTS_PATH,
            None if tracker is None else linear_agents_section(),
            (linear_agents_section(),),
            guarded=True,
        )
    )
    items.append(
        _plan_linear_text_switch(
            root,
            _HANDBOOK_PATH,
            None
            if tracker is None
            else _linear_handbook_text(tpm_configured=tpm_configured),
            (
                _linear_handbook_text(tpm_configured=False),
                _linear_handbook_text(tpm_configured=True),
            ),
            guarded=False,
        )
    )
    # dispatch_config.yaml is planned and written last, so the selected mode
    # changes only after every other file.
    config_item = _plan_config_switch(root, config, tracker)
    items.append(config_item)

    config_writes = config_item.content is not None and (
        config_item.action == _SWITCH_CHANGE
        or (force and config_item.action == _SWITCH_CONFLICT and config_item.forceable)
    )
    if config_writes:
        _validate_switched_config(root, config_item.content or b"", new_mode)

    changed: list[Path] = []
    skipped: list[Path] = []
    conflicts: list[Path] = []
    details: list[str] = []
    forced: list[str] = []
    writes: list[tuple[Path, bytes]] = []
    for item in items:
        if item.action == _SWITCH_UNCHANGED:
            skipped.append(item.path)
            continue
        if item.action == _SWITCH_CHANGE:
            changed.append(item.path)
            writes.append((item.path, item.content or b""))
            continue
        if force and item.forceable:
            forced.append(f"{item.path.as_posix()}: {item.force_note}")
            if item.content is not None:
                changed.append(item.path)
                writes.append((item.path, item.content))
            continue
        conflicts.append(item.path)
        details.append(f"{item.path.as_posix()}: {item.reason}")

    result = InitResult(
        template=template,
        target_root=root,
        copied=(),
        skipped=tuple(skipped),
        conflicts=tuple(conflicts),
        dry_run=dry_run,
        force=force,
        provider_mapping=KEPT_MAPPING,
        merged=tuple(changed),
        tracker=new_mode,
        switched_from=old_mode,
        conflict_details=tuple(details),
        forced=tuple(forced),
    )
    if conflicts and not dry_run:
        raise TrackerSwitchConflictError(result.conflicts, result.conflict_details)
    if dry_run:
        return result
    for relative, content in writes:
        (root / relative).write_bytes(content)
    return result


def init_reference_workflow(
    target_root: Path,
    *,
    template: str = REFERENCE_TEMPLATE,
    dry_run: bool = False,
    force: bool = False,
    provider: str | None = None,
    available_providers: Iterable[str] | None = None,
    verification_command: str | None = None,
    antigravity_available: bool | None = None,
    tracker: str | None = None,
    tracker_team: str | None = None,
    tracker_project: str | None = None,
    tracker_label: str | None = None,
    tracker_mcp_server: str | None = None,
) -> InitResult:
    """Copy the reference workflow into a target, or switch its tracker mode.

    ``tracker`` None means no ``--tracker`` option: a fresh target is set up in
    file mode and an initialized one gets the ordinary template update. An
    explicit ``tracker`` on an initialized target (one with
    ``dispatch_config.yaml``) switches it to that mode instead: only the
    managed tracker block, the Linear blocks of ``AGENTS.md`` and
    ``docs/handoff/HANDBOOK.md``, and the Claude subagent ``tools:`` lines
    change, and the agents block is kept, so ``provider`` and
    ``verification_command`` are refused there.
    """

    if template != REFERENCE_TEMPLATE:
        raise UnknownTemplateError(template)
    if not _TEMPLATE_ROOT.is_dir():
        raise InitWorkflowError(f"Template directory not found: {_TEMPLATE_ROOT}")

    # Validate tracker options and the initialized-target preflight before
    # any file is planned, so every refusal also fires with dry_run and force.
    tracker_config = build_tracker_config(
        TRACKER_NONE if tracker is None else tracker,
        team=tracker_team,
        project=tracker_project,
        label=tracker_label,
        mcp_server=tracker_mcp_server,
    )
    resolved_root = target_root.expanduser().resolve()
    initialized_config = preflight_initialized_target(resolved_root)
    if initialized_config is not None and tracker is not None:
        given = [
            name
            for name, value in (
                ("--provider", provider),
                ("--verification-command", verification_command),
            )
            if value is not None
        ]
        if given:
            raise TrackerSwitchError(
                resolved_root,
                f"{' and '.join(given)} cannot be combined with --tracker on "
                "an initialized repository: a tracker switch keeps the "
                "existing agents and verification settings. Drop "
                f"{' and '.join(given)}, or run init without --tracker.",
            )
        return _switch_tracker_mode(
            resolved_root,
            initialized_config,
            tracker_config,
            template=template,
            dry_run=dry_run,
            force=force,
        )

    executable = (
        None
        if verification_command is None
        else verification_executable(verification_command)
    )
    detected = tuple(
        detect_provider_clis() if available_providers is None else available_providers
    )
    mapping = resolve_provider_mapping(provider, detected)
    if tracker_config is not None and mapping in _LINEAR_UNSUPPORTED_MAPPINGS:
        # The dispatcher would refuse to load the written config, so fail
        # before any file is planned (dry_run included).
        raise TrackerUnsupportedMappingError(mapping)
    antigravity_detected = (
        shutil.which("agy") is not None
        if antigravity_available is None
        else antigravity_available
    )

    target_root = target_root.expanduser().resolve()
    # Detect before anything is copied, so the template never shows up as a
    # candidate source.
    candidates = detect_candidates(target_root)
    copied: list[Path] = []
    skipped: list[Path] = []
    conflicts: list[Path] = []
    merged: list[Path] = []
    planned: dict[Path, bytes] = {}

    for relative_path in template_files():
        target_path = target_root / relative_path
        source = _source_bytes(
            relative_path, mapping, executable, verification_command, tracker_config
        )
        managed = relative_path in _TEXT_MARKERS or relative_path == _SETTINGS_PATH
        if _has_linked_destination(target_root, relative_path) or target_path.is_dir():
            conflicts.append(relative_path)
            continue
        if managed:
            exists = target_path.exists()
            existing = target_path.read_bytes() if exists else b""
            try:
                if relative_path in _TEXT_MARKERS:
                    rendered = _merge_text(relative_path, existing, source)
                    if relative_path == _AGENTS_PATH and tracker_config is not None:
                        rendered = _merge_linear_agents(rendered)
                else:
                    rendered = _merge_settings(existing if exists else b"{}", source)
            except (ValueError, TypeError):
                conflicts.append(relative_path)
                continue
            if exists and rendered == existing:
                skipped.append(relative_path)
            else:
                (merged if exists else copied).append(relative_path)
                planned[relative_path] = rendered
            continue
        if not target_path.exists():
            copied.append(relative_path)
            continue
        if target_path.is_dir():
            conflicts.append(relative_path)
            continue
        if force:
            copied.append(relative_path)
            continue
        if target_path.read_bytes() == source:
            skipped.append(relative_path)
            continue
        conflicts.append(relative_path)

    result = InitResult(
        template=template,
        target_root=target_root,
        copied=tuple(copied),
        skipped=tuple(skipped),
        conflicts=tuple(conflicts),
        dry_run=dry_run,
        force=force,
        provider_mapping=mapping,
        detected_providers=detected,
        merged=tuple(merged),
        candidates=candidates,
        verification_command=verification_command,
        antigravity_detected=antigravity_detected,
        tracker=TRACKER_NONE if tracker_config is None else TRACKER_LINEAR,
    )

    if conflicts and not dry_run:
        raise InitConflictError(result.conflicts)
    if dry_run:
        return result

    target_root.mkdir(parents=True, exist_ok=True)
    for relative_path in copied + merged:
        source_path = _TEMPLATE_ROOT / relative_path
        target_path = target_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if relative_path in planned:
            target_path.write_bytes(planned[relative_path])
        elif _source_is_rendered(relative_path, mapping, executable, tracker_config):
            target_path.write_bytes(
                _source_bytes(
                    relative_path,
                    mapping,
                    executable,
                    verification_command,
                    tracker_config,
                )
            )
        else:
            shutil.copy2(source_path, target_path)

    return result
