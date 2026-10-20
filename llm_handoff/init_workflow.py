from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from llm_handoff.config import (
    CLAUDE_DEFAULT_ALLOWED_TOOLS,
    INTERPRETER_EXECUTABLES,
    VERIFICATION_FORBIDDEN_CHARS,
    TrackerConfig,
    linear_tools_for_role,
    verification_whitespace_problem,
)
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
CLAUDE_MAPPING = "claude"
CODEX_MAPPING = "codex"
ANTIGRAVITY_MAPPING = "antigravity"
PROVIDER_MAPPINGS = (CLAUDE_MAPPING, CODEX_MAPPING, ANTIGRAVITY_MAPPING)
# Provider CLIs the mapping auto-detect looks for on PATH. `agy` selects the
# antigravity mapping.
PROVIDER_CLIS = ("claude", "codex", "agy")
_ANTIGRAVITY_CLI = "agy"
_CONFIG_PATH = Path("dispatch_config.yaml")
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
# (handoff-validator, planner, frontend, backend, auditor, and ledger-updater),
# mapped to (role whose Linear tools are appended, built-in tools kept in every
# mode). A subagent's `tools:` list is an allowlist, so without the appended
# names the subagent cannot call the Linear MCP tools its role launch allows.
# Each base string must equal the template's `tools:` line without its
# `mcp__*` entries.
_PLANNER_AGENT_PATH = Path(".claude/agents/planner.md")
_FRONTEND_AGENT_PATH = Path(".claude/agents/frontend.md")
_BACKEND_AGENT_PATH = Path(".claude/agents/backend.md")
_AUDITOR_AGENT_PATH = Path(".claude/agents/auditor.md")
_FINALIZER_AGENT_PATH = Path(".claude/agents/ledger-updater.md")
_WRITER_BASE_TOOLS = "Read, Edit, Write, Grep, Glob, Bash"
_AUDITOR_BASE_TOOLS = "Read, Grep, Glob, Bash"
_FINALIZER_BASE_TOOLS = "Read, Edit, Grep, Glob, Bash"
_LINEAR_AGENT_TOOLS: dict[Path, tuple[str, str]] = {
    _VALIDATOR_AGENT_PATH: ("validator", _VALIDATOR_BASE_TOOLS),
    _PLANNER_AGENT_PATH: ("planner", _WRITER_BASE_TOOLS),
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


class TrackerRequiresFreshRepoError(InitWorkflowError):
    def __init__(self, target_root: Path) -> None:
        super().__init__(
            "--tracker linear only sets up a repository that has no "
            f"dispatch_config.yaml yet, but {target_root / _CONFIG_PATH} "
            "exists; switching an initialized repository is not supported yet."
        )
        self.target_root = target_root


class InitConflictError(InitWorkflowError):
    def __init__(self, conflicts: tuple[Path, ...]) -> None:
        super().__init__("Target files already exist with different content.")
        self.conflicts = conflicts


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


def add_tracker_block(config_text: str, tracker: TrackerConfig) -> str:
    """Append the managed `tracker` block to dispatch_config.yaml text.

    The block uses the template text's line ending. The rest of the text,
    including the commented `# tracker:` example, is unchanged.
    """

    lines = config_text.splitlines(keepends=True)
    newline = (_line_ending(lines[0]) if lines else "") or "\n"
    mapping = _tracker_mapping(tracker)
    dumped = yaml.safe_dump(
        {"tracker": mapping}, sort_keys=False, default_flow_style=False
    )
    begin, end = _TRACKER_CONFIG_MARKERS
    body = "".join(f"{line}{newline}" for line in dumped.splitlines())
    separator = newline if config_text and not config_text.endswith("\n") else ""
    rendered = (
        f"{config_text}{separator}{newline}{begin}{newline}{body}{end}{newline}"
    )
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


def _linear_handbook_text() -> str:
    sections = [linear_evidence_contract(), linear_issue_template()]
    sections.extend(linear_role_section(role) for role in LINEAR_PROMPT_ROLES)
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

    role, base_tools = _LINEAR_AGENT_TOOLS[relative_path]
    lines = template.splitlines(keepends=True)
    tools_indexes: list[int] = []
    if lines and lines[0].rstrip(b"\r\n") == b"---":
        for index in range(1, len(lines)):
            if lines[index].rstrip(b"\r\n") == b"---":
                break
            if lines[index].startswith(b"tools: "):
                tools_indexes.append(index)
    if len(tools_indexes) != 1:
        raise InitWorkflowError(
            f"Template {relative_path.as_posix()} must have exactly one "
            "'tools: ' line in its frontmatter."
        )
    index = tools_indexes[0]
    line = lines[index]
    ending = line[len(line.rstrip(b"\r\n")) :]
    tools = ", ".join(linear_tools_for_role(role, mcp_server))
    lines[index] = f"tools: {base_tools}, {tools}".encode("utf-8") + ending
    return b"".join(lines)


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
    tracker: str = TRACKER_NONE,
    tracker_team: str | None = None,
    tracker_project: str | None = None,
    tracker_label: str | None = None,
    tracker_mcp_server: str | None = None,
) -> InitResult:
    if template != REFERENCE_TEMPLATE:
        raise UnknownTemplateError(template)
    if not _TEMPLATE_ROOT.is_dir():
        raise InitWorkflowError(f"Template directory not found: {_TEMPLATE_ROOT}")

    # Validate tracker options and the fresh-repo rule before any file is
    # planned, so both errors also fire with dry_run.
    tracker_config = build_tracker_config(
        tracker,
        team=tracker_team,
        project=tracker_project,
        label=tracker_label,
        mcp_server=tracker_mcp_server,
    )
    if tracker_config is not None:
        resolved_root = target_root.expanduser().resolve()
        if os.path.lexists(resolved_root / _CONFIG_PATH):
            raise TrackerRequiresFreshRepoError(resolved_root)

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
