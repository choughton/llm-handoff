from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import re
import string
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)
import yaml


def _binary_name(base_name: str) -> str:
    return f"{base_name}.cmd" if os.name == "nt" else base_name


DISPATCH_WINDOW_TITLE = "llm-handoff dispatcher"
CODEX_SKILL_NAME = "llm-handoff"
CODEX_WEB_SEARCH_MODE = "disabled"

# Launch permissions. Each provider launches with the safer arguments below
# unless the role sets `unsafe_bypass: true`, which restores the previous
# approval/sandbox bypass flags exactly.
CLAUDE_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
CLAUDE_PERMISSION_MODE = "dontAsk"
CLAUDE_DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    # The Claude-side prompts delegate to the named subagent.
    "Agent",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    # The handoff-validator template resolves SHAs with `git cat-file -t`.
    "Bash(git cat-file:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git rev-parse:*)",
)
# Executables that `interpreter_grants` covers, for Claude and agy alike.
INTERPRETER_EXECUTABLES: tuple[str, ...] = ("python", "python3", "pytest")
# Interpreter rules added to the default list only with the top-level
# `interpreter_grants: true`. Any of them lets an agent run any command,
# `git push` included, past the allowlist, so they are an operator opt-in.
CLAUDE_INTERPRETER_ALLOWED_TOOLS: tuple[str, ...] = (
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(pytest:*)",
)


def claude_default_allowed_tools(interpreter_grants: bool) -> tuple[str, ...]:
    """Return the default Claude allowlist for one config.

    The base ``CLAUDE_DEFAULT_ALLOWED_TOOLS``, plus
    ``CLAUDE_INTERPRETER_ALLOWED_TOOLS`` when ``interpreter_grants`` is true.
    """

    if interpreter_grants:
        return (*CLAUDE_DEFAULT_ALLOWED_TOOLS, *CLAUDE_INTERPRETER_ALLOWED_TOOLS)
    return CLAUDE_DEFAULT_ALLOWED_TOOLS


CLAUDE_DISALLOWED_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")
CODEX_SANDBOX_ARGS: tuple[str, ...] = (
    "--sandbox",
    "workspace-write",
    "--ask-for-approval",
    "never",
)
CODEX_UNSAFE_BYPASS_ARGS: tuple[str, ...] = ("--yolo",)
CLAUDE_MODEL = "claude-opus-5-5"
NORMALIZER_PROVIDER = "claude"
NORMALIZER_MODEL = "claude-haiku-5-5"
OPENAI_NORMALIZER_MODEL = "gpt-5.4-mini"
# The codex normalizer runs through the Codex CLI login instead of the API.
CODEX_NORMALIZER_MODEL = OPENAI_NORMALIZER_MODEL
# The antigravity normalizer runs through the agy CLI login.
ANTIGRAVITY_NORMALIZER_MODEL = "gemini-3.8-flash-low"
NORMALIZER_TIMEOUT_MS = 60_000

AGENT_TIMEOUT_MS = 1_200_000
SUBAGENT_TIMEOUT_MS = 900_000
AUDIT_REVIEW_TIMEOUT_MS = 900_000

POLL_INTERVAL_SECONDS = 30
MAX_CONSECUTIVE_FAILURES = 3
WORK_PACKET_VERIFICATION_TIMEOUT_SECONDS = 600
# Shell control characters a Verification command (and every
# `verification_allowed_prefixes` entry) must not contain once the allowlist
# is set. A prefix match alone would otherwise allow chaining, piping,
# redirection, substitution, or a second line after an allowed prefix.
# `%` and `!` cover cmd.exe variable and delayed expansion: on Windows the
# command runs through cmd.exe, which expands `%NAME%` (and `!NAME!` when
# delayed expansion is on) before parsing, so an expanded value could inject
# `&` or another control character the literal scan never saw.
VERIFICATION_FORBIDDEN_CHARS: frozenset[str] = frozenset(
    {"&", "|", ";", "<", ">", "$", "`", "(", ")", "%", "!", "\r", "\n"}
)


def verification_whitespace_problem(text: str) -> str | None:
    """Describe the whitespace in ``text`` that literal prefix matching refuses.

    ``text`` is a stripped Verification command or
    `verification_allowed_prefixes` entry. The `agy` grant for a prefix
    matches the exact command line, so words must be separated by single
    spaces: a tab, any other non-space whitespace character, or a run of two
    or more spaces is refused. Returns ``None`` when ``text`` has none.
    """

    for char in text:
        if char.isspace() and char != " ":
            return f"the whitespace character {char!r}"
    if "  " in text:
        return "a run of two or more spaces"
    return None

SCANNER_TIMEOUT_MS = 300_000
SCANNER_PLACEHOLDERS = frozenset(
    {
        "diff_range",
        "changed_files",
        "changed_py_files",
        "changed_js_files",
        "repo_root",
    }
)
_SCANNER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

PLANNER_RESUME_DEFAULT = True

CODEX_BINARY = _binary_name("codex")
CLAUDE_BINARY = _binary_name("claude")
ANTIGRAVITY_BINARY = _binary_name("agy")
# Providers whose CLI command differs from the provider name.
PROVIDER_DEFAULT_CLI_NAMES: dict[str, str] = {"antigravity": "agy"}
# Runtime adapters that pass the role's `model` to the CLI as a launch flag.
_MODEL_FLAG_PROVIDERS: frozenset[str] = frozenset({"claude", "antigravity"})

DEFAULT_HANDOFF_PATH = Path("docs/handoff/HANDOFF.md")
DEFAULT_PROJECT_STATE_PATH = Path("PROJECT_STATE.md")
# Closed epics and their ledger lines; the finalizer moves them here so the
# project state file every role reads stays limited to current work.
DEFAULT_COMPLETED_WORK_PATH = Path("COMPLETED_WORK.md")
DEFAULT_SHARED_INIT_PROMPT_PATH = Path("docs/handoff/SHARED_REPO_INIT_PROMPT.md")
DEFAULT_CONFIG_PATH = Path("dispatch_config.yaml")
# Control files the roles read as instructions. A dispatch that changes one of
# these without listing it under the Work Packet's Files in scope routes the
# handoff to user. Patterns are repo-relative; `**` matches any depth.
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    "AGENTS.md",
    "docs/handoff/HANDBOOK.md",
    "docs/handoff/*_PROMPT.md",
    "docs/handoff/README.md",
    "dispatch_config.yaml",
    ".claude/**",
    ".codex/**",
    ".gemini/**",
    ".agents/**",
    ".githooks/**",
)
CODEX_OUTPUT_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "codex_final_response.schema.json"
)
CODEX_OUTPUT_DIRECTORY = Path("logs") / "dispatch" / "codex"
CODEX_OUTPUT_LAST_MESSAGE_PATH = CODEX_OUTPUT_DIRECTORY / "last-message.json"
CODEX_SESSION_STATE_PATH = CODEX_OUTPUT_DIRECTORY / "session.json"
# Per-story bounce counts for the three-fix circuit breaker. Kept outside the
# handoff file so a dispatcher restart cannot reset the breaker.
BOUNCE_COUNT_STATE_PATH = Path("logs") / "dispatch" / "bounce-counts.json"
# Uncommitted files the last implementer dispatch left for its story, so a
# dispatcher restart can still carry them over instead of treating them as
# operator edits.
CARRY_OVER_STATE_PATH = Path("logs") / "dispatch" / "carry-over.json"
# Written right before an implementer dispatch and deleted once it returns, so
# a dispatcher start can tell an implementer interrupted mid-dispatch from
# operator edits.
IN_FLIGHT_STATE_PATH = Path("logs") / "dispatch" / "in-flight.json"
# Operator chat messages queued while a role runs, so a held loop keeps them
# across a dispatcher restart.
CHAT_QUEUE_STATE_PATH = Path("logs") / "dispatch" / "chat-queue.json"
# Each role dispatch in the current run, with the session id the role reported.
DISPATCH_RECORD_STATE_PATH = Path("logs") / "dispatch" / "dispatch-record.json"
# Files a chat side-session changed, with their fingerprints and the message digest.
CHAT_CHANGES_STATE_PATH = Path("logs") / "dispatch" / "chat-changes.json"
# SHA-256 of the last handoff the dispatcher wrote itself (a bounce, review,
# deferred-findings, or escalation handoff). Cleared when an agent dispatch
# returns, so `doctor` and supervisor preflight trust a validator verdict only
# while HANDOFF.md still matches what the dispatcher wrote.
DISPATCHER_HANDOFF_STATE_PATH = Path("logs") / "dispatch" / "dispatcher-handoff.json"


def detect_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    candidates = (current, *current.parents)

    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate

    for candidate in candidates:
        if (candidate / DEFAULT_HANDOFF_PATH).exists():
            return candidate

    return current


AgentRole = Literal[
    "planner",
    "backend",
    "frontend",
    "auditor",
    "validator",
    "finalizer",
    "tpm",
]
ProviderName = Literal["codex", "claude", "antigravity", "openai"]
EnvMode = Literal["allowlist", "inherit"]
UnknownNormalizerPolicy = Literal["fail_closed"]
AuditReviewer = Literal["quality", "security"]
SecurityReviewProvider = Literal["claude", "codex", "antigravity"]
BlockSeverity = Literal["critical", "high", "medium", "low"]
# Who commits an implementer's work: the agent itself, or the dispatcher after
# checking every changed file against the Work Packet's scope.
CommitMode = Literal["agent", "dispatcher"]
# What happens to audited work: stay local, merge a story branch with a report
# commit, or push it and open a pull request.
PublishMode = Literal["local", "merge-commit", "pr"]
# Pull request reviewers whose review of the head commit must finish, without
# findings, before the dispatcher merges a story's pull request: `codex` is the
# ChatGPT Codex connector's GitHub review; `codex-local` is a `codex exec
# review` the dispatcher runs itself, for a repository the connector cannot
# read.
PublishReviewer = Literal["codex", "codex-local"]
# What the dispatcher does at a non-user pause: schedule one bounded planner
# recovery per pause streak, or pause and wait for a HANDOFF.md edit.
PauseRecoveryMode = Literal["planner", "off"]
# What `pr` mode does when a base-branch merge conflicts only in the project
# state file: have the planner resolve it, or pause for the operator.
ProjectStateConflictMode = Literal["planner", "off"]
# What `pr` mode does when a retried Codex review stalls or fails again: ask
# the operator, or merge without the review when the audit passed.
ReviewStallAction = Literal["prompt", "merge"]
DEFAULT_CODEX_BOT_LOGIN = "chatgpt-codex-connector[bot]"
# The security reviewer runs by default. Leaving it out of `audit.reviewers`
# also needs a non-empty `audit.security_opt_out_reason`.
DEFAULT_AUDIT_REVIEWERS: tuple[AuditReviewer, ...] = ("quality", "security")
# Order used to pick a security reviewer vendor when `audit.security_provider`
# is not set: the first vendor whose CLI is on PATH and that differs from the
# implementer's provider, else the implementer's own vendor. The `gemini`
# security reviewer was removed; naming it fails at config load.
SECURITY_REVIEW_PROVIDER_PREFERENCE: tuple[SecurityReviewProvider, ...] = (
    "claude",
    "codex",
    "antigravity",
)

DEFAULT_AGENT_PROVIDERS: dict[AgentRole, ProviderName] = {
    "planner": "antigravity",
    "backend": "codex",
    "frontend": "antigravity",
    "auditor": "claude",
    "validator": "claude",
    "finalizer": "claude",
}
RUNTIME_PROVIDER_ADAPTERS = frozenset({"claude", "codex", "antigravity"})
NORMALIZER_PROVIDER_ADAPTERS = frozenset({"antigravity", "claude", "codex", "openai"})
NORMALIZER_PROVIDER_MODELS: dict[ProviderName, str] = {
    "antigravity": ANTIGRAVITY_NORMALIZER_MODEL,
    "claude": NORMALIZER_MODEL,
    "codex": CODEX_NORMALIZER_MODEL,
    "openai": OPENAI_NORMALIZER_MODEL,
}
# The gemini next-agent normalizer was removed; naming it fails with this.
GEMINI_NORMALIZER_REMOVED_MESSAGE = (
    "The gemini provider was removed: Gemini CLI no longer serves personal "
    "Google accounts. Use provider: antigravity (the agy CLI)."
)
# The gemini security reviewer was removed; naming it fails with this.
GEMINI_SECURITY_PROVIDER_REMOVED_MESSAGE = (
    "The gemini provider was removed: Gemini CLI no longer serves personal "
    "Google accounts. Use security_provider: antigravity (the agy CLI)."
)
# The gemini role provider was removed; naming it for a role or a fallback
# fails with this.
GEMINI_PROVIDER_REMOVED_MESSAGE = (
    "The gemini provider was removed: Gemini CLI no longer serves personal "
    "Google accounts. Use provider: antigravity (the agy CLI)."
)
# The top-level planner_api_key_env key only fed the gemini adapter. `false`
# (the old default) is dropped; any other value fails with this.
PLANNER_API_KEY_ENV_REMOVED_MESSAGE = (
    "planner_api_key_env was removed with the gemini provider: Gemini CLI no "
    "longer serves personal Google accounts. Delete the planner_api_key_env "
    "key; a Codex role uses use_api_key_env: true for OPENAI_API_KEY."
)
REQUIRED_AGENT_ROLES = frozenset(DEFAULT_AGENT_PROVIDERS)

# Vendor API keys are never copied from one provider's launch to another's.
# Each key is only passed to its own provider, and only through the
# provider-specific rule in ``agent_process.build_agent_env`` (for example
# ``use_api_key_env``), never through ``env_passthrough``.
VENDOR_API_KEY_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    }
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    binary: str | None = None
    mention: str | None = None
    agent_name: str | None = None
    skill_name: str | None = None
    model: str | None = None
    permissions_flag: str | None = None
    resume: bool | None = None
    timeout_ms: int | None = None
    # Off by default: some CLIs write nothing for long stretches while they
    # work, so an idle limit only suits CLIs that stream steadily.
    idle_timeout_ms: int | None = None
    retries: int | None = None
    use_api_key_env: bool | None = None
    env_mode: EnvMode = "allowlist"
    env_passthrough: tuple[str, ...] = ()
    unsafe_bypass: bool = False
    allowed_tools: tuple[str, ...] | None = None
    # Runs this role's dispatch once more after a fatal failure of the primary
    # agent: a timeout, a rate or usage limit, a sign-in failure, or a CLI that
    # will not launch. One level only; a fallback has no fallback of its own.
    fallback: AgentConfig | None = None
    # Set only by DispatchConfig in Linear mode with tracker.claude_mcp_config;
    # a private attribute, so no YAML key can set it.
    _claude_mcp_config: Path | None = PrivateAttr(default=None)
    # Set only by DispatchConfig from the top-level `interpreter_grants`; a
    # private attribute, so no role-level YAML key can set it.
    _interpreter_grants: bool = PrivateAttr(default=False)
    # Set only by the Linear-mode derivation when it builds `allowed_tools` on
    # the flag-aware default list (the role left `allowed_tools` unset).
    _allowed_tools_from_default: bool = PrivateAttr(default=False)

    @property
    def claude_mcp_config_path(self) -> Path | None:
        """Resolved MCP config file this Claude launch loads, if any."""

        return self._claude_mcp_config

    @property
    def interpreter_grants(self) -> bool:
        """The loaded config's ``interpreter_grants`` value for this launch."""

        return self._interpreter_grants

    @property
    def uses_default_allowed_tools(self) -> bool:
        """Whether this launch's Claude allowlist builds on the default list.

        True when the role leaves ``allowed_tools`` unset, or when the
        Linear-mode derivation appended its tools to that default list. False
        for a list the role writes itself, which ``interpreter_grants`` does
        not change.
        """

        return self.allowed_tools is None or self._allowed_tools_from_default

    @property
    def claude_allowed_tools(self) -> tuple[str, ...]:
        """The Claude allowlist this launch uses without ``unsafe_bypass``.

        ``allowed_tools`` as written when the role sets it; otherwise
        ``claude_default_allowed_tools(interpreter_grants)``.
        """

        if self.allowed_tools is not None:
            return self.allowed_tools
        return claude_default_allowed_tools(self._interpreter_grants)

    @field_validator("provider", mode="before")
    @classmethod
    def _reject_removed_provider(cls, value: object) -> object:
        # Runs before the Literal check so the removal gets its own message.
        if value == "gemini":
            raise ValueError(GEMINI_PROVIDER_REMOVED_MESSAGE)
        return value

    @field_validator("timeout_ms", mode="after")
    @classmethod
    def _validate_timeout_ms(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("timeout_ms must be at least 1 when set.")
        return value

    @field_validator("idle_timeout_ms", mode="after")
    @classmethod
    def _validate_idle_timeout_ms(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("idle_timeout_ms must be at least 1 when set.")
        return value

    @field_validator("env_passthrough", mode="after")
    @classmethod
    def _validate_env_passthrough(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not _ENV_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"env_passthrough entry `{name}` is not a valid environment "
                    "variable name."
                )
            if name.upper() in VENDOR_API_KEY_ENV_NAMES:
                raise ValueError(
                    f"env_passthrough must not name the vendor API key `{name}`; "
                    "use the role's `use_api_key_env` setting for its own "
                    "provider key."
                )
        return value

    @field_validator("allowed_tools", mode="after")
    @classmethod
    def _validate_allowed_tools(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return value
        for rule in value:
            if not rule.strip() or "," in rule:
                raise ValueError(
                    f"allowed_tools entry `{rule}` must be one non-empty tool "
                    "rule without commas, for example `Bash(git status:*)`."
                )
        return tuple(rule.strip() for rule in value)

    @model_validator(mode="after")
    def _validate_launch_permissions(self) -> AgentConfig:
        if self.permissions_flag is not None and not self.unsafe_bypass:
            raise ValueError(
                "permissions_flag only applies with unsafe_bypass: true; the "
                "default launch uses --permission-mode dontAsk with allowed_tools."
            )
        if self.allowed_tools is not None and self.provider != "claude":
            raise ValueError("allowed_tools only applies to claude roles.")
        return self

    @model_validator(mode="after")
    def _validate_fallback(self) -> AgentConfig:
        fallback = self.fallback
        if fallback is None:
            return self
        if fallback.fallback is not None:
            raise ValueError(
                "fallback must not define its own fallback; only one failover "
                "level is supported."
            )
        if _launch_identity(fallback) == _launch_identity(self):
            raise ValueError(
                "fallback must launch a different provider, binary, or (for "
                "claude and antigravity) model than the primary agent."
            )
        return self

    @field_validator("retries", mode="after")
    @classmethod
    def _validate_retries(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("retries must be non-negative when set.")
        return value


AgentConfig.model_rebuild()


def _launch_identity(agent_config: AgentConfig) -> tuple[object, ...]:
    # Only the Claude and Antigravity adapters pass a model flag today, so a
    # model change is a different launch for those providers alone.
    provider = agent_config.provider
    binary = agent_config.binary or _binary_name(
        PROVIDER_DEFAULT_CLI_NAMES.get(provider, provider)
    )
    model = (
        agent_config.model
        if agent_config.provider in _MODEL_FLAG_PROVIDERS
        else None
    )
    return (agent_config.provider, binary, model)


class NormalizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName = NORMALIZER_PROVIDER
    model: str = NORMALIZER_MODEL
    timeout_ms: int = NORMALIZER_TIMEOUT_MS
    on_unknown: UnknownNormalizerPolicy = "fail_closed"

    @model_validator(mode="before")
    @classmethod
    def _fill_provider_model(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        provider = value.get("provider", NORMALIZER_PROVIDER)
        if "model" in value and value["model"] is not None:
            return value
        provider_default = NORMALIZER_PROVIDER_MODELS.get(provider)
        if provider_default is None:
            return value
        return {**value, "model": provider_default}

    @field_validator("provider", mode="before")
    @classmethod
    def _reject_removed_provider(cls, value: object) -> object:
        # Runs before the Literal check so the removal gets its own message.
        if value == "gemini":
            raise ValueError(GEMINI_NORMALIZER_REMOVED_MESSAGE)
        return value

    @field_validator("provider", mode="after")
    @classmethod
    def _validate_provider(cls, value: ProviderName) -> ProviderName:
        if value not in NORMALIZER_PROVIDER_ADAPTERS:
            raise ValueError(
                "no next-agent normalizer adapter is registered for provider "
                f"`{value}`."
            )
        return value

    @field_validator("timeout_ms", mode="after")
    @classmethod
    def _validate_timeout_ms(cls, value: int) -> int:
        if value < 1:
            raise ValueError("normalizer timeout_ms must be at least 1.")
        return value


class PublishConfig(BaseModel):
    """Per-story branches and how audited work reaches the base branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: PublishMode = "local"
    base_branch: str | None = None
    branch_prefix: str = "llm-handoff/"
    remote: str = "origin"
    auto_merge: bool = True
    wait_for_checks: bool = False
    wait_for_reviewers: tuple[PublishReviewer, ...] = ()
    review_timeout_seconds: int = 1200
    review_poll_seconds: int = 30
    # Correction rounds for review findings. Above 1, the dispatcher waits for
    # the reviewer's own review of each pushed fix, so the reviewer must be
    # set to review every push; the dispatcher requests a review only to
    # retry one the reviewer reports as failed.
    max_review_rounds: int = 1
    # A Codex review the summary shows running this long with no result is
    # treated like a failed one: one automatic review request, then
    # review_stall_action.
    review_stall_seconds: int = 600
    # What happens when the retried review also stalls, times out, fails, or
    # names no commit: "prompt" asks the operator (merge / retry / quit);
    # "merge" merges without a Codex review when the audit passed for the head
    # commit and every review thread is resolved.
    review_stall_action: ReviewStallAction = "prompt"
    codex_bot_login: str = DEFAULT_CODEX_BOT_LOGIN
    # pr mode: when merging the base branch into the story branch conflicts
    # only in project_state_path, "planner" has the planner resolve it once;
    # "off" pauses for the operator, as for any other conflict.
    resolve_project_state_conflicts: ProjectStateConflictMode = "planner"

    @field_validator("resolve_project_state_conflicts", mode="before")
    @classmethod
    def _reject_boolean_conflict_mode(cls, value: object) -> object:
        # YAML 1.1 reads a bare `off` as the boolean false.
        if isinstance(value, bool):
            raise ValueError(
                'publish.resolve_project_state_conflicts must be "planner" or '
                '"off"; YAML reads a bare off as a boolean, so write '
                'resolve_project_state_conflicts: "off" with quotes.'
            )
        return value

    @model_validator(mode="after")
    def _validate_reviewers(self) -> PublishConfig:
        if not self.wait_for_reviewers:
            return self
        if self.mode != "pr" or not self.auto_merge:
            raise ValueError(
                "publish.wait_for_reviewers only applies to `mode: pr` with "
                "`auto_merge: true`; without auto-merge the operator reviews "
                "and merges the pull request."
            )
        if len(set(self.wait_for_reviewers)) != len(self.wait_for_reviewers):
            raise ValueError("publish.wait_for_reviewers must not repeat a reviewer.")
        if {"codex", "codex-local"} <= set(self.wait_for_reviewers):
            raise ValueError(
                "publish.wait_for_reviewers takes `codex` or `codex-local`, not "
                "both: `codex-local` runs the Codex review locally in place of "
                "the GitHub one."
            )
        for name in (
            "review_timeout_seconds",
            "review_poll_seconds",
            "review_stall_seconds",
            "max_review_rounds",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"publish.{name} must be at least 1.")
        if not self.codex_bot_login.strip():
            raise ValueError("publish.codex_bot_login must not be empty.")
        return self

    @field_validator("branch_prefix", mode="after")
    @classmethod
    def _validate_branch_prefix(cls, value: str) -> str:
        if not value or any(char.isspace() for char in value) or ".." in value:
            raise ValueError(
                "publish.branch_prefix must be a non-empty branch-name prefix "
                "without spaces or `..`."
            )
        return value


class AuditConfig(BaseModel):
    """Audit fan-out settings.

    By default the auditor route runs a read-only fan-out: a quality reviewer
    and a security reviewer, with the dispatcher writing ``HANDOFF.md`` from
    their structured verdicts. Setting ``reviewers: [quality]`` opts out of
    the security reviewer and restores the single-auditor path; it requires a
    non-empty ``security_opt_out_reason``, which every dispatcher start logs
    as a warning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewers: tuple[AuditReviewer, ...] = DEFAULT_AUDIT_REVIEWERS
    security_provider: SecurityReviewProvider | None = None
    security_opt_out_reason: str | None = None
    security_model: str | None = None
    block_severity: BlockSeverity = "high"
    review_timeout_ms: int = AUDIT_REVIEW_TIMEOUT_MS

    @field_validator("security_provider", mode="before")
    @classmethod
    def _reject_removed_security_provider(cls, value: object) -> object:
        # Runs before the Literal check so the removal gets its own message.
        if value == "gemini":
            raise ValueError(GEMINI_SECURITY_PROVIDER_REMOVED_MESSAGE)
        return value

    @field_validator("security_opt_out_reason", mode="after")
    @classmethod
    def _strip_security_opt_out_reason(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("security_model", mode="after")
    @classmethod
    def _validate_security_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "audit.security_model must not be empty; remove it to borrow "
                "the first claude role's model."
            )
        return stripped

    @model_validator(mode="after")
    def _validate_security_model_scope(self) -> AuditConfig:
        if self.security_model is None:
            return self
        if not self.security_enabled:
            raise ValueError(
                "audit.security_model only applies when audit.reviewers "
                "includes `security`; remove it or add `security` to "
                "audit.reviewers."
            )
        if self.security_provider is not None and self.security_provider != "claude":
            raise ValueError(
                "audit.security_model applies only to the claude security "
                f"reviewer, but audit.security_provider is "
                f"`{self.security_provider}`; remove audit.security_model or "
                "set audit.security_provider to claude."
            )
        return self

    @model_validator(mode="after")
    def _validate_security_opt_out(self) -> AuditConfig:
        reason = self.security_opt_out_reason
        if self.security_enabled:
            if reason is not None:
                raise ValueError(
                    "audit.security_opt_out_reason only applies when "
                    "audit.reviewers leaves out `security`; remove it or remove "
                    "`security` from audit.reviewers."
                )
            return self
        if not reason:
            raise ValueError(
                "audit.reviewers leaves out `security`, so "
                "audit.security_opt_out_reason must give a non-empty reason for "
                "turning the security reviewer off."
            )
        return self

    @field_validator("reviewers", mode="after")
    @classmethod
    def _validate_reviewers(
        cls,
        value: tuple[AuditReviewer, ...],
    ) -> tuple[AuditReviewer, ...]:
        if not value:
            raise ValueError("audit.reviewers must list at least one reviewer.")
        if len(set(value)) != len(value):
            raise ValueError("audit.reviewers must not repeat a reviewer.")
        if "quality" not in value:
            raise ValueError(
                "audit.reviewers must include `quality`; the quality reviewer "
                "decides the forward route."
            )
        return value

    @field_validator("review_timeout_ms", mode="after")
    @classmethod
    def _validate_review_timeout_ms(cls, value: int) -> int:
        if value < 1:
            raise ValueError("audit.review_timeout_ms must be at least 1.")
        return value

    @property
    def security_enabled(self) -> bool:
        return "security" in self.reviewers


def scanner_command_placeholders(command: str) -> frozenset[str]:
    """Return the placeholders a scanner command references.

    Raises ``ValueError`` for an unknown placeholder, a format spec or
    conversion, or unbalanced braces. Literal braces must be doubled.
    """

    found: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(command))
    except ValueError as exc:
        raise ValueError(f"command has unbalanced braces ({exc}).") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in SCANNER_PLACEHOLDERS or format_spec or conversion:
            known = ", ".join(sorted(f"{{{name}}}" for name in SCANNER_PLACEHOLDERS))
            raise ValueError(
                f"command references unknown placeholder `{{{field_name}}}`; "
                f"known placeholders are {known} and literal braces must be "
                "doubled."
            )
        found.add(field_name)
    return frozenset(found)


class ScannerConfig(BaseModel):
    """One operator-configured scanner run by the work-claim gate.

    ``command`` is operator-authored shell text, run in ``repo_root`` with the
    placeholders from ``SCANNER_PLACEHOLDERS`` substituted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    command: str
    timeout_ms: int = SCANNER_TIMEOUT_MS
    required: bool = True

    @field_validator("name", mode="after")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if _SCANNER_NAME_RE.fullmatch(value) is None:
            raise ValueError(
                "scanner name must start with a letter or digit and contain only "
                f"letters, digits, `.`, `_`, or `-`; got `{value}`."
            )
        return value

    @field_validator("timeout_ms", mode="after")
    @classmethod
    def _validate_timeout_ms(cls, value: int) -> int:
        if value < 1:
            raise ValueError("scanner timeout_ms must be at least 1.")
        return value

    @model_validator(mode="after")
    def _validate_command(self) -> ScannerConfig:
        if not self.command.strip():
            raise ValueError(f"scanner_config_invalid:{self.name}: command is empty.")
        try:
            scanner_command_placeholders(self.command)
        except ValueError as exc:
            raise ValueError(f"scanner_config_invalid:{self.name}: {exc}") from exc
        return self


TrackerProvider = Literal["none", "linear"]
# Every tracker key other than `provider`. With `provider: none` all of them
# must be unset, so a half-configured block fails closed.
_TRACKER_SETTING_KEYS: tuple[str, ...] = (
    "team",
    "project",
    "label",
    "eligible_state",
    "in_progress_state",
    "success_state",
    "blocked_state",
    "user_attention_label",
    "mcp_server",
    "claude_mcp_config",
)
_TRACKER_STRING_KEYS: tuple[str, ...] = tuple(
    key for key in _TRACKER_SETTING_KEYS if key != "claude_mcp_config"
)
_TRACKER_LINEAR_REQUIRED_KEYS: tuple[str, ...] = (
    "team",
    "eligible_state",
    "in_progress_state",
    "success_state",
    "mcp_server",
)
# Keys that may be omitted in Linear mode but must not be blank when given.
_TRACKER_LINEAR_NON_BLANK_KEYS: tuple[str, ...] = (
    "project",
    "label",
    "blocked_state",
    "user_attention_label",
)
# `mcp_server` later forms the `mcp__<server>__` tool-name prefix.
_MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Linear MCP tools each role's Claude launch gets in Linear mode, unprefixed.
# `linear_tools_for_role` adds the `mcp__<tracker.mcp_server>__` prefix.
LINEAR_ROLE_TOOLS: dict[AgentRole, tuple[str, ...]] = {
    "planner": (
        "get_issue",
        "list_issues",
        "list_comments",
        "save_comment",
        "save_issue",
    ),
    "tpm": (
        "get_issue",
        "list_issues",
        "list_comments",
        "save_comment",
        "save_issue",
    ),
    "backend": ("get_issue", "list_issues", "list_comments", "save_comment"),
    "frontend": ("get_issue", "list_issues", "list_comments", "save_comment"),
    "auditor": (
        "get_issue",
        "list_issues",
        "list_comments",
        "save_comment",
        "save_issue",
    ),
    # Comment-only: the validator posts `handoff_repaired` on a repair turn and
    # never edits issues. Report-only validator launches drop `save_comment`
    # too (`without_linear_write_tools`).
    "validator": ("get_issue", "list_issues", "list_comments", "save_comment"),
    # Comment-only: the finalizer posts `epic_closed` and never edits issues.
    "finalizer": ("get_issue", "list_issues", "list_comments", "save_comment"),
}


def linear_tools_for_role(role: str, mcp_server: str) -> tuple[str, ...]:
    """Full ``mcp__<server>__<tool>`` names for one role, in table order."""

    return tuple(
        f"mcp__{mcp_server}__{tool}" for tool in LINEAR_ROLE_TOOLS.get(role, ())
    )


def _with_linear_tools(
    agent_config: AgentConfig,
    linear_tools: tuple[str, ...],
    mcp_config_path: Path | None,
) -> AgentConfig:
    if agent_config.provider != "claude" or agent_config.unsafe_bypass:
        return agent_config
    base = agent_config.claude_allowed_tools
    added = tuple(tool for tool in linear_tools if tool not in base)
    derived = agent_config.model_copy(update={"allowed_tools": (*base, *added)})
    derived._claude_mcp_config = mcp_config_path
    derived._interpreter_grants = agent_config._interpreter_grants
    derived._allowed_tools_from_default = agent_config.uses_default_allowed_tools
    return derived


# Linear MCP tools that change Linear, unprefixed. Report-only validator
# launches drop them (`without_linear_write_tools`).
LINEAR_WRITE_TOOLS: tuple[str, ...] = ("save_comment", "save_issue")


def _grants_any(entry: str, removed: frozenset[str], server_grant: str) -> bool:
    """Return True when an allowlist entry would grant a removed tool.

    Matches exact names, the bare ``mcp__<server>`` server grant, and wildcard
    entries such as ``mcp__<server>__*``, ``mcp__*``, or ``*``.
    """

    if entry in removed or entry == server_grant:
        return True
    return any(fnmatch.fnmatchcase(tool, entry) for tool in removed)


def _without_tools(
    agent_config: AgentConfig, removed: frozenset[str], server_grant: str = ""
) -> AgentConfig:
    if agent_config.provider != "claude" or agent_config.unsafe_bypass:
        return agent_config
    base = agent_config.claude_allowed_tools
    kept = tuple(
        tool for tool in base if not _grants_any(tool, removed, server_grant)
    )
    if kept == base:
        return agent_config
    derived = agent_config.model_copy(update={"allowed_tools": kept})
    derived._claude_mcp_config = agent_config._claude_mcp_config
    derived._interpreter_grants = agent_config._interpreter_grants
    derived._allowed_tools_from_default = agent_config.uses_default_allowed_tools
    return derived


def without_linear_write_tools(
    agent_config: AgentConfig, mcp_server: str
) -> AgentConfig:
    """Return ``agent_config`` (and its fallback) without Linear write tools.

    Removes ``mcp__<mcp_server>__save_comment`` and ``save_issue`` from a
    Claude launch's allowlist, so a report-only validator launch can read
    Linear but cannot change it. Entries that would still grant a write tool
    go too: the bare ``mcp__<mcp_server>`` server grant and wildcard entries
    that match a write tool, such as ``mcp__<mcp_server>__*``,
    ``mcp__<mcp_server>__save_*``, ``mcp__*``, or ``*``. Other providers,
    ``unsafe_bypass`` launches, and allowlists without such entries come back
    unchanged.
    """

    removed = frozenset(f"mcp__{mcp_server}__{tool}" for tool in LINEAR_WRITE_TOOLS)
    server_grant = f"mcp__{mcp_server}"
    updated = _without_tools(agent_config, removed, server_grant)
    fallback = agent_config.fallback
    if fallback is not None:
        stripped_fallback = _without_tools(fallback, removed, server_grant)
        if stripped_fallback is not fallback:
            private = updated._claude_mcp_config
            grants = updated._interpreter_grants
            from_default = updated._allowed_tools_from_default
            updated = updated.model_copy(update={"fallback": stripped_fallback})
            updated._claude_mcp_config = private
            updated._interpreter_grants = grants
            updated._allowed_tools_from_default = from_default
    return updated


def _with_interpreter_grants(
    agent_config: AgentConfig, interpreter_grants: bool
) -> AgentConfig:
    """Return ``agent_config`` (and its fallback) carrying the config flag."""

    fallback = agent_config.fallback
    if fallback is not None and fallback._interpreter_grants != interpreter_grants:
        fallback_copy = fallback.model_copy()
        fallback_copy._interpreter_grants = interpreter_grants
        fallback = fallback_copy
    if (
        agent_config._interpreter_grants == interpreter_grants
        and fallback is agent_config.fallback
    ):
        return agent_config
    private = agent_config._claude_mcp_config
    updated = agent_config.model_copy(update={"fallback": fallback})
    updated._claude_mcp_config = private
    updated._interpreter_grants = interpreter_grants
    updated._allowed_tools_from_default = agent_config._allowed_tools_from_default
    return updated


class TrackerConfig(BaseModel):
    """Optional issue-tracker binding (Linear mode).

    ``provider: none`` (the default) means vanilla mode and allows no other
    key. ``provider: linear`` binds the repository to a pool of Linear issues
    that agents read and update through their own CLI's MCP connector; the
    dispatcher never calls Linear and holds no Linear credential. In Linear
    mode the dispatcher uses this block to build each Claude role's tool list
    (``LINEAR_ROLE_TOOLS``) and, with ``claude_mcp_config``, its
    ``--mcp-config`` launch arguments.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: TrackerProvider = "none"
    team: str | None = None
    project: str | None = None
    label: str | None = None
    eligible_state: str | None = None
    in_progress_state: str | None = None
    success_state: str | None = None
    blocked_state: str | None = None
    user_attention_label: str | None = None
    mcp_server: str | None = None
    claude_mcp_config: Path | None = None

    @field_validator(*_TRACKER_STRING_KEYS, mode="after")
    @classmethod
    def _strip_strings(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _validate_provider_settings(self) -> TrackerConfig:
        if self.provider == "none":
            configured = [
                f"tracker.{key}"
                for key in _TRACKER_SETTING_KEYS
                if getattr(self, key) is not None
            ]
            if configured:
                raise ValueError(
                    "tracker.provider is `none`, but the tracker block also sets "
                    f"{', '.join(configured)}; remove those keys or set "
                    "tracker.provider: linear."
                )
            return self

        missing = [
            f"tracker.{key}"
            for key in _TRACKER_LINEAR_REQUIRED_KEYS
            if not getattr(self, key)
        ]
        if missing:
            raise ValueError(
                "tracker.provider: linear requires non-empty "
                f"{', '.join(missing)}."
            )
        for key in _TRACKER_LINEAR_NON_BLANK_KEYS:
            value = getattr(self, key)
            if value is not None and not value:
                raise ValueError(
                    f"tracker.{key} must not be empty; remove it or give a value."
                )
        if self.project is not None and self.label is not None:
            raise ValueError(
                "tracker.provider: linear needs exactly one of tracker.project "
                "and tracker.label, but both are set; keep one."
            )
        if self.project is None and self.label is None:
            raise ValueError(
                "tracker.provider: linear needs exactly one of tracker.project "
                "and tracker.label to define this repository's issue pool; set "
                "one."
            )
        if _MCP_SERVER_NAME_RE.fullmatch(self.mcp_server or "") is None:
            raise ValueError(
                "tracker.mcp_server must contain only letters, digits, `_`, or "
                f"`-` (it forms the mcp__<server>__ tool prefix); got "
                f"`{self.mcp_server}`."
            )
        return self

    @property
    def enabled(self) -> bool:
        return self.provider != "none"


def _default_agent_configs() -> dict[AgentRole, AgentConfig]:
    return {
        role: AgentConfig.model_validate(_agent_defaults_for_provider(role, provider))
        for role, provider in DEFAULT_AGENT_PROVIDERS.items()
    }


def _agent_defaults_for_provider(role: str, provider: object) -> dict[str, object]:
    if provider == "codex":
        return {
            "provider": provider,
            "binary": CODEX_BINARY,
            "skill_name": CODEX_SKILL_NAME,
            "resume": True,
            "timeout_ms": AGENT_TIMEOUT_MS,
        }
    if provider == "claude":
        return {
            "provider": provider,
            "binary": CLAUDE_BINARY,
            "model": NORMALIZER_MODEL if role == "validator" else CLAUDE_MODEL,
            "resume": False,
            "timeout_ms": SUBAGENT_TIMEOUT_MS,
        }
    if provider == "antigravity":
        return {
            "provider": provider,
            "binary": ANTIGRAVITY_BINARY,
            "resume": role == "planner",
            "timeout_ms": AGENT_TIMEOUT_MS,
        }
    return {"provider": provider}


def _repo_relative_path_error(key: str, value: str, rule: str) -> str:
    return (
        f"{key} {value!r} must be a relative path inside the repository: "
        f"{rule}."
    )


def _check_repo_relative_path(key: str, value: str) -> None:
    """Reject a handoff_path, project_state_path, or completed_work_path value
    that is not a plain repo-relative path. Raises ``ValueError`` naming the
    key and the rule."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        rule = "control characters are not allowed"
    elif not value.strip():
        rule = "the value is empty"
    elif value[:2] in {"//", "\\\\", "/\\", "\\/"}:
        rule = "UNC paths are not allowed"
    elif value.startswith(("/", "\\")):
        rule = "absolute paths are not allowed"
    elif re.match(r"^[A-Za-z]:", value):
        rule = "drive-letter paths are not allowed"
    elif ".." in re.split(r"[\\/]", value):
        rule = "'..' segments are not allowed"
    else:
        return
    raise ValueError(_repo_relative_path_error(key, value, rule))


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    handoff_path: Path = DEFAULT_HANDOFF_PATH
    project_state_path: Path = DEFAULT_PROJECT_STATE_PATH
    completed_work_path: Path = DEFAULT_COMPLETED_WORK_PATH
    agents: dict[AgentRole, AgentConfig] = Field(default_factory=_default_agent_configs)
    normalizer: NormalizerConfig = Field(default_factory=NormalizerConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    dry_run: bool = False
    use_manual_frontend: bool = False
    backend_resume: bool = True
    planner_resume: bool = PLANNER_RESUME_DEFAULT
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    pause_recovery: PauseRecoveryMode = "planner"
    # Show the operator prompt at pauses when stdin is a TTY, the run is not a
    # dry run, and poll_interval_seconds is above 0.
    interactive_pause: bool = True
    enforce_work_packet: bool = True
    work_packet_opt_out_reason: str | None = None
    work_packet_verification_timeout_seconds: int = (
        WORK_PACKET_VERIFICATION_TIMEOUT_SECONDS
    )
    # Extra environment variable names the Verification command and scanners
    # receive on top of the allowlisted base and platform variables.
    work_claim_env_passthrough: tuple[str, ...] = ()
    # Command prefixes a Work Packet Verification command must start with.
    # Empty means any command runs (with a startup and doctor WARN).
    verification_allowed_prefixes: tuple[str, ...] = ()
    scanners: tuple[ScannerConfig, ...] = ()
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS
    commit_mode: CommitMode = "agent"
    publish: PublishConfig = PublishConfig()
    tracker: TrackerConfig = TrackerConfig()
    # Operator opt-in. When true, the default Claude allowlist and the
    # generated agy grants include `python`, `python3`, and `pytest`, which
    # let an agent run any command (`git push` included) past the allowlist.
    # Roles with their own `allowed_tools` keep that list as written.
    interpreter_grants: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_auto_push(cls, value: object) -> object:
        # `auto_push` was parsed but never read. Accept the old `false` value so
        # existing configs keep loading; reject anything else.
        if not isinstance(value, dict) or "auto_push" not in value:
            return value
        if value["auto_push"] is False:
            return {key: item for key, item in value.items() if key != "auto_push"}
        raise ValueError(
            "auto_push has been removed: the dispatcher never read it. Delete "
            "the auto_push key from the config; publish.mode controls whether "
            "the dispatcher pushes."
        )

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_planner_api_key_env(cls, value: object) -> object:
        # `planner_api_key_env` only fed the removed gemini adapter. Accept the
        # old `false` default so existing configs keep loading; reject anything
        # else.
        if not isinstance(value, dict) or "planner_api_key_env" not in value:
            return value
        if value["planner_api_key_env"] is False:
            return {
                key: item
                for key, item in value.items()
                if key != "planner_api_key_env"
            }
        raise ValueError(PLANNER_API_KEY_ENV_REMOVED_MESSAGE)

    @model_validator(mode="after")
    def _validate_paths_inside_repo(self) -> DispatchConfig:
        # The field validator rejects unsafe spellings; this catches a value
        # that resolves outside the repository (a symlink escape) or onto the
        # repository root itself (such as `.`).
        root = self.repo_root.resolve()
        for key in ("handoff_path", "project_state_path", "completed_work_path"):
            path = getattr(self, key)
            resolved = (self.repo_root / path).resolve()
            if resolved == root:
                raise ValueError(_repo_relative_path_error(
                    key, str(path), "it names the repository root"
                ))
            if not resolved.is_relative_to(root):
                raise ValueError(_repo_relative_path_error(
                    key, str(path), "it resolves outside the repository"
                ))
        completed = (self.repo_root / self.completed_work_path).resolve()
        for key in ("handoff_path", "project_state_path"):
            if (self.repo_root / getattr(self, key)).resolve() == completed:
                raise ValueError(
                    f"completed_work_path {str(self.completed_work_path)!r} must "
                    f"name a different file than {key}."
                )
        return self

    @model_validator(mode="after")
    def _validate_commit_mode(self) -> DispatchConfig:
        if self.commit_mode == "dispatcher" and not self.enforce_work_packet:
            raise ValueError(
                "commit_mode: dispatcher requires enforce_work_packet: true; the "
                "dispatcher only commits files the Work Packet puts in scope, "
                "and the work-claim gate checks what it committed."
            )
        reason = self.work_packet_opt_out_reason
        if self.enforce_work_packet:
            if reason is not None:
                raise ValueError(
                    "work_packet_opt_out_reason only applies when the work-claim "
                    "gate is off (enforce_work_packet: false); remove it or set "
                    "enforce_work_packet: false."
                )
        elif not reason:
            raise ValueError(
                "enforce_work_packet: false turns the work-claim gate off, so "
                "work_packet_opt_out_reason must give a non-empty reason for "
                "turning it off."
            )
        if self.publish.mode != "local" and self.commit_mode != "dispatcher":
            raise ValueError(
                f"publish.mode: {self.publish.mode} requires commit_mode: "
                "dispatcher; the dispatcher owns the story branch and its commits."
            )
        return self

    @model_validator(mode="after")
    def _validate_tracker(self) -> DispatchConfig:
        if not self.tracker.enabled:
            return self
        if not self.enforce_work_packet:
            raise ValueError(
                f"tracker.provider: {self.tracker.provider} requires "
                "enforce_work_packet: true; Linear mode requires the work-claim "
                "gate, because the auditor only moves an issue to its success "
                "state after the gate passes."
            )
        # agy agent files limit each role's tools and give it no Linear MCP
        # access, so Linear mode fails closed on any antigravity launch.
        antigravity_locations: list[str] = []
        for role, agent_config in self.agents.items():
            if agent_config.provider == "antigravity":
                antigravity_locations.append(f"agents.{role}")
            fallback = agent_config.fallback
            if fallback is not None and fallback.provider == "antigravity":
                antigravity_locations.append(f"agents.{role}.fallback")
        if antigravity_locations:
            raise ValueError(
                f"tracker.provider: {self.tracker.provider} does not support "
                "provider: antigravity "
                f"({', '.join(antigravity_locations)}): agy agent files limit "
                "each role's tools and give it no Linear MCP access. Map these "
                "roles to claude or codex, or set tracker.provider: none."
            )
        mcp_config_path = self.tracker_claude_mcp_config_path
        if mcp_config_path is not None and not mcp_config_path.is_file():
            raise ValueError(
                "tracker.claude_mcp_config must name an existing file; "
                f"{mcp_config_path} does not exist or is not a file."
            )
        return self

    @model_validator(mode="after")
    def _apply_interpreter_grants(self) -> DispatchConfig:
        # Carry the top-level flag to every role and fallback so a launch with
        # `allowed_tools: None` picks the matching default list. Runs before
        # the Linear derivation, which builds on that default list.
        derived = {
            role: _with_interpreter_grants(agent_config, self.interpreter_grants)
            for role, agent_config in self.agents.items()
        }
        if any(derived[role] is not self.agents[role] for role in derived):
            # The model is frozen; set the derived agents once, during
            # validation.
            object.__setattr__(self, "agents", derived)
        return self

    @model_validator(mode="after")
    def _derive_linear_claude_tools(self) -> DispatchConfig:
        # Computed in memory from the tracker block; never written back to the
        # config file. Re-validating an already-derived config is a no-op
        # because tools already listed are skipped.
        if not self.tracker.enabled or self.tracker.mcp_server is None:
            return self
        mcp_config_path = self.tracker_claude_mcp_config_path
        if mcp_config_path is not None:
            mcp_config_path = mcp_config_path.resolve()
        derived: dict[AgentRole, AgentConfig] = {}
        for role, agent_config in self.agents.items():
            linear_tools = linear_tools_for_role(role, self.tracker.mcp_server)
            updated = _with_linear_tools(agent_config, linear_tools, mcp_config_path)
            if agent_config.fallback is not None:
                fallback = _with_linear_tools(
                    agent_config.fallback, linear_tools, mcp_config_path
                )
                if fallback is not agent_config.fallback:
                    private = updated._claude_mcp_config
                    from_default = updated._allowed_tools_from_default
                    updated = updated.model_copy(update={"fallback": fallback})
                    updated._claude_mcp_config = private
                    updated._allowed_tools_from_default = from_default
            derived[role] = updated
        # The model is frozen; set the derived agents once, during validation.
        object.__setattr__(self, "agents", derived)
        return self

    @field_validator("pause_recovery", mode="before")
    @classmethod
    def _reject_boolean_pause_recovery(cls, value: object) -> object:
        # YAML 1.1 reads a bare `off` as the boolean false.
        if isinstance(value, bool):
            raise ValueError(
                'pause_recovery must be "planner" or "off"; YAML reads a bare '
                'off as a boolean, so write pause_recovery: "off" with quotes.'
            )
        return value

    @field_validator("work_packet_opt_out_reason", mode="after")
    @classmethod
    def _strip_work_packet_opt_out_reason(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("protected_paths", mode="after")
    @classmethod
    def _validate_protected_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for pattern in value:
            candidate = pattern.strip().replace("\\", "/")
            while candidate.startswith("./"):
                candidate = candidate[2:]
            if (
                not candidate
                or candidate.startswith("/")
                or re.match(r"^[A-Za-z]:", candidate)
                or ".." in candidate.split("/")
            ):
                raise ValueError(
                    f"protected_paths entry `{pattern}` must be a relative path "
                    "or glob inside the repository."
                )
            normalized.append(candidate)
        return tuple(normalized)

    @field_validator("repo_root", mode="after")
    @classmethod
    def _resolve_repo_root(cls, value: Path) -> Path:
        return Path(value).resolve()

    @field_validator(
        "handoff_path", "project_state_path", "completed_work_path", mode="before"
    )
    @classmethod
    def _normalize_relative_path(cls, value: object, info: ValidationInfo) -> object:
        # Runs before Path conversion: Path("") would read as ".".
        if not isinstance(value, (str, Path)):
            return value
        _check_repo_relative_path(info.field_name or "path", str(value))
        return Path(value)

    @field_validator("agents", mode="after")
    @classmethod
    def _validate_agents(
        cls,
        value: dict[AgentRole, AgentConfig],
    ) -> dict[AgentRole, AgentConfig]:
        missing_roles = sorted(REQUIRED_AGENT_ROLES.difference(value))
        if missing_roles:
            raise ValueError(
                "agents must define the required reference roles: "
                f"{', '.join(missing_roles)}."
            )
        for role, agent_config in value.items():
            if agent_config.provider not in RUNTIME_PROVIDER_ADAPTERS:
                raise ValueError(
                    "no runtime adapter is registered for provider "
                    f"`{agent_config.provider}` on role `{role}`."
                )
            fallback = agent_config.fallback
            if (
                fallback is not None
                and fallback.provider not in RUNTIME_PROVIDER_ADAPTERS
            ):
                raise ValueError(
                    "no runtime adapter is registered for provider "
                    f"`{fallback.provider}` on role `{role}` fallback."
                )
        return value

    @field_validator("poll_interval_seconds", mode="after")
    @classmethod
    def _validate_poll_interval_seconds(cls, value: int) -> int:
        if value < 0:
            raise ValueError("poll_interval_seconds must be non-negative.")
        return value

    @field_validator("max_consecutive_failures", mode="after")
    @classmethod
    def _validate_max_consecutive_failures(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_consecutive_failures must be at least 1.")
        return value

    @field_validator("work_packet_verification_timeout_seconds", mode="after")
    @classmethod
    def _validate_work_packet_verification_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                "work_packet_verification_timeout_seconds must be at least 1."
            )
        return value

    @field_validator("work_claim_env_passthrough", mode="after")
    @classmethod
    def _validate_work_claim_env_passthrough(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for entry in value:
            name = entry.strip()
            if not name:
                raise ValueError(
                    "work_claim_env_passthrough entries must be non-empty "
                    "environment variable names."
                )
            if not _ENV_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"work_claim_env_passthrough entry `{entry}` is not a valid "
                    "environment variable name."
                )
            if name.upper() in VENDOR_API_KEY_ENV_NAMES:
                raise ValueError(
                    "work_claim_env_passthrough must not name the vendor API key "
                    f"`{name}`; Verification commands and scanners never receive "
                    "vendor API keys."
                )
            normalized.append(name)
        return tuple(normalized)

    @field_validator("verification_allowed_prefixes", mode="after")
    @classmethod
    def _validate_verification_allowed_prefixes(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for entry in value:
            prefix = entry.strip()
            if not prefix:
                raise ValueError(
                    "verification_allowed_prefixes entries must be non-empty "
                    f"command prefixes; got {entry!r}."
                )
            for char in prefix:
                if char in VERIFICATION_FORBIDDEN_CHARS:
                    raise ValueError(
                        f"verification_allowed_prefixes entry {entry!r} contains "
                        f"the shell control character {char!r}; prefixes must "
                        "not contain any of & | ; < > $ ` ( ) or a line break."
                    )
            problem = verification_whitespace_problem(prefix)
            if problem is not None:
                raise ValueError(
                    f"verification_allowed_prefixes entry {entry!r} contains "
                    f"{problem}; prefixes must use single spaces between words "
                    "so they match the agy grants built from them."
                )
            normalized.append(prefix)
        return tuple(normalized)

    @field_validator("scanners", mode="after")
    @classmethod
    def _validate_scanners(
        cls,
        value: tuple[ScannerConfig, ...],
    ) -> tuple[ScannerConfig, ...]:
        seen: set[str] = set()
        for scanner in value:
            if scanner.name in seen:
                raise ValueError(
                    f"scanner names must be unique; `{scanner.name}` repeats."
                )
            seen.add(scanner.name)
        return value

    @property
    def handoff_full_path(self) -> Path:
        return (self.repo_root / self.handoff_path).resolve()

    @property
    def project_state_full_path(self) -> Path:
        return (self.repo_root / self.project_state_path).resolve()

    @property
    def completed_work_full_path(self) -> Path:
        return (self.repo_root / self.completed_work_path).resolve()

    @property
    def tracker_claude_mcp_config_path(self) -> Path | None:
        """``tracker.claude_mcp_config`` resolved against ``repo_root``.

        Relative paths resolve from the repository root; absolute paths are
        returned as given. ``None`` when the key is unset.
        """

        configured = self.tracker.claude_mcp_config
        if configured is None:
            return None
        path = Path(configured)
        if path.is_absolute():
            return path
        return (self.repo_root / path).resolve()

    @property
    def backend_resume_enabled(self) -> bool:
        return self.backend_resume

    @property
    def planner_resume_enabled(self) -> bool:
        return self.planner_resume


def load_dispatch_config(
    *,
    repo_root: Path,
    config_path: Path | None = None,
    dry_run: bool = False,
    use_manual_frontend: bool = False,
    backend_resume: bool | None = None,
    planner_resume: bool | None = None,
    interactive_pause: bool | None = None,
) -> DispatchConfig:
    resolved_repo_root = Path(repo_root).resolve()
    data = _read_config_file(resolved_repo_root, config_path)
    data["repo_root"] = resolved_repo_root
    data["dry_run"] = dry_run
    data["use_manual_frontend"] = use_manual_frontend
    _merge_agent_defaults(data)
    if backend_resume is not None:
        data["backend_resume"] = backend_resume
    if planner_resume is not None:
        data["planner_resume"] = planner_resume
    if interactive_pause is not None:
        data["interactive_pause"] = interactive_pause
    return DispatchConfig.model_validate(data)


def _merge_agent_defaults(data: dict[str, object]) -> None:
    configured_agents = data.get("agents")
    if configured_agents is None or not isinstance(configured_agents, dict):
        return

    default_agents: dict[str, object] = {
        role: agent_config.model_dump(exclude_none=True)
        for role, agent_config in _default_agent_configs().items()
    }
    merged_agents = dict(default_agents)
    for role, configured in configured_agents.items():
        if isinstance(role, str) and isinstance(configured, dict):
            configured_provider = configured.get("provider")
            default_config = (
                _agent_defaults_for_provider(role, configured_provider)
                if configured_provider is not None
                else default_agents.get(role)
            )
            if isinstance(default_config, dict):
                merged = {**default_config, **configured}
                if configured.get("fallback") is not None:
                    merged["fallback"] = _merge_fallback_defaults(
                        role, configured["fallback"]
                    )
                merged_agents[role] = merged
                continue
        merged_agents[role] = configured
    data["agents"] = merged_agents


def _merge_fallback_defaults(role: str, fallback: object) -> object:
    if not isinstance(fallback, dict) or fallback.get("provider") is None:
        return fallback
    return {**_agent_defaults_for_provider(role, fallback["provider"]), **fallback}


def _read_config_file(repo_root: Path, config_path: Path | None) -> dict[str, object]:
    resolved_path = _resolve_config_path(repo_root, config_path)
    if resolved_path is None:
        return {}

    if not resolved_path.exists():
        raise FileNotFoundError(f"dispatch config not found: {resolved_path}")

    loaded = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"dispatch config must be a YAML mapping: {resolved_path}")
    return dict(loaded)


def _resolve_config_path(repo_root: Path, config_path: Path | None) -> Path | None:
    if config_path is not None:
        path = Path(config_path)
        if path.is_absolute():
            return path
        return (repo_root / path).resolve()

    default_path = repo_root / DEFAULT_CONFIG_PATH
    if default_path.exists():
        return default_path
    return None
