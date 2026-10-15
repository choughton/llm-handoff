from __future__ import annotations

import os
from pathlib import Path
import re
import string
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml


def _binary_name(base_name: str) -> str:
    return f"{base_name}.cmd" if os.name == "nt" else base_name


DISPATCH_WINDOW_TITLE = "llm-handoff dispatcher"
GEMINI_PLANNER_MENTION = "@planner"
GEMINI_FRONTEND_MENTION = "@frontend"
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
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(pytest:*)",
)
CLAUDE_DISALLOWED_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")
CODEX_SANDBOX_ARGS: tuple[str, ...] = (
    "--sandbox",
    "workspace-write",
    "--ask-for-approval",
    "never",
)
CODEX_UNSAFE_BYPASS_ARGS: tuple[str, ...] = ("--yolo",)
GEMINI_SANDBOX_ARGS: tuple[str, ...] = ("--yolo", "--sandbox")
GEMINI_UNSAFE_BYPASS_ARGS: tuple[str, ...] = ("--yolo", "--sandbox=false")
CLAUDE_MODEL = "claude-opus-5-5"
NORMALIZER_PROVIDER = "claude"
NORMALIZER_MODEL = "claude-haiku-5-5"
GEMINI_NORMALIZER_MODEL = "gemini-2.5-flash"
OPENAI_NORMALIZER_MODEL = "gpt-5.4-mini"
NORMALIZER_TIMEOUT_MS = 60_000

AGENT_TIMEOUT_MS = 1_200_000
SUBAGENT_TIMEOUT_MS = 900_000
AUDIT_REVIEW_TIMEOUT_MS = 900_000

POLL_INTERVAL_SECONDS = 30
MAX_CONSECUTIVE_FAILURES = 3
WORK_PACKET_VERIFICATION_TIMEOUT_SECONDS = 600
SCANNER_TIMEOUT_MS = 300_000
SCANNER_PLACEHOLDERS = frozenset(
    {"diff_range", "changed_files", "changed_py_files", "repo_root"}
)
_SCANNER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_BASE_SECONDS = 60
GEMINI_RESUME_DEFAULT = True

GEMINI_BINARY = _binary_name("gemini")
CODEX_BINARY = _binary_name("codex")
CLAUDE_BINARY = _binary_name("claude")

DEFAULT_HANDOFF_PATH = Path("docs/handoff/HANDOFF.md")
DEFAULT_PROJECT_STATE_PATH = Path("PROJECT_STATE.md")
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
]
ProviderName = Literal["codex", "gemini", "claude", "openai"]
EnvMode = Literal["allowlist", "inherit"]
UnknownNormalizerPolicy = Literal["fail_closed"]
AuditReviewer = Literal["quality", "security"]
SecurityReviewProvider = Literal["claude", "gemini", "codex"]
BlockSeverity = Literal["critical", "high", "medium", "low"]
# Who commits an implementer's work: the agent itself, or the dispatcher after
# checking every changed file against the Work Packet's scope.
CommitMode = Literal["agent", "dispatcher"]
# What happens to audited work: stay local, merge a story branch with a report
# commit, or push it and open a pull request.
PublishMode = Literal["local", "merge-commit", "pr"]
# GitHub pull request reviewers whose review of the head commit must finish,
# without findings, before the dispatcher merges a story's pull request.
PublishReviewer = Literal["codex"]
DEFAULT_CODEX_BOT_LOGIN = "chatgpt-codex-connector[bot]"
DEFAULT_AUDIT_REVIEWERS: tuple[AuditReviewer, ...] = ("quality",)
# Order used to pick a security reviewer vendor that differs from the
# implementer's provider when `audit.security_provider` is not set.
SECURITY_REVIEW_PROVIDER_PREFERENCE: tuple[SecurityReviewProvider, ...] = (
    "claude",
    "codex",
    "gemini",
)

DEFAULT_AGENT_PROVIDERS: dict[AgentRole, ProviderName] = {
    "planner": "gemini",
    "backend": "codex",
    "frontend": "gemini",
    "auditor": "claude",
    "validator": "claude",
    "finalizer": "claude",
}
RUNTIME_PROVIDER_ADAPTERS = frozenset({"claude", "codex", "gemini"})
NORMALIZER_PROVIDER_ADAPTERS = frozenset({"claude", "gemini", "openai"})
NORMALIZER_PROVIDER_MODELS: dict[ProviderName, str] = {
    "claude": NORMALIZER_MODEL,
    "gemini": GEMINI_NORMALIZER_MODEL,
    "openai": OPENAI_NORMALIZER_MODEL,
}
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
                "claude) model than the primary agent."
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
    # Only the Claude adapter passes a model flag today, so a model change is
    # a different launch for claude alone.
    binary = agent_config.binary or _binary_name(agent_config.provider)
    model = agent_config.model if agent_config.provider == "claude" else None
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
    # set to review every push; the dispatcher never requests a review.
    max_review_rounds: int = 1
    codex_bot_login: str = DEFAULT_CODEX_BOT_LOGIN

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
        for name in ("review_timeout_seconds", "review_poll_seconds", "max_review_rounds"):
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
    """Opt-in audit fan-out settings.

    The defaults keep the single-auditor path: one quality reviewer that
    rewrites the handoff itself. Adding ``security`` to ``reviewers`` switches
    the auditor route to a read-only fan-out where the dispatcher writes
    ``HANDOFF.md`` from structured reviewer verdicts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewers: tuple[AuditReviewer, ...] = DEFAULT_AUDIT_REVIEWERS
    security_provider: SecurityReviewProvider | None = None
    block_severity: BlockSeverity = "high"
    review_timeout_ms: int = AUDIT_REVIEW_TIMEOUT_MS

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
    if provider == "gemini":
        return {
            "provider": provider,
            "binary": GEMINI_BINARY,
            "mention": _default_gemini_mention_for_role(role),
            "resume": GEMINI_RESUME_DEFAULT if role == "planner" else False,
            "timeout_ms": AGENT_TIMEOUT_MS,
            "retries": GEMINI_MAX_RETRIES,
        }
    if provider == "claude":
        return {
            "provider": provider,
            "binary": CLAUDE_BINARY,
            "model": NORMALIZER_MODEL if role == "validator" else CLAUDE_MODEL,
            "resume": False,
            "timeout_ms": SUBAGENT_TIMEOUT_MS,
        }
    return {"provider": provider}


def _default_gemini_mention_for_role(role: str) -> str:
    if role == "planner":
        return GEMINI_PLANNER_MENTION
    if role == "frontend":
        return GEMINI_FRONTEND_MENTION
    return f"@{role}"


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    handoff_path: Path = DEFAULT_HANDOFF_PATH
    project_state_path: Path = DEFAULT_PROJECT_STATE_PATH
    auto_push: bool = False
    agents: dict[AgentRole, AgentConfig] = Field(default_factory=_default_agent_configs)
    normalizer: NormalizerConfig = Field(default_factory=NormalizerConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    dry_run: bool = False
    use_manual_frontend: bool = False
    planner_api_key_env: bool = False
    backend_resume: bool = True
    planner_resume: bool = GEMINI_RESUME_DEFAULT
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    enforce_work_packet: bool = False
    work_packet_verification_timeout_seconds: int = (
        WORK_PACKET_VERIFICATION_TIMEOUT_SECONDS
    )
    scanners: tuple[ScannerConfig, ...] = ()
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS
    commit_mode: CommitMode = "agent"
    publish: PublishConfig = PublishConfig()

    @model_validator(mode="after")
    def _validate_commit_mode(self) -> DispatchConfig:
        if self.commit_mode == "dispatcher" and not self.enforce_work_packet:
            raise ValueError(
                "commit_mode: dispatcher requires enforce_work_packet: true; the "
                "dispatcher only commits files the Work Packet puts in scope, "
                "and the work-claim gate checks what it committed."
            )
        if self.publish.mode != "local" and self.commit_mode != "dispatcher":
            raise ValueError(
                f"publish.mode: {self.publish.mode} requires commit_mode: "
                "dispatcher; the dispatcher owns the story branch and its commits."
            )
        return self

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

    @field_validator("handoff_path", "project_state_path", mode="after")
    @classmethod
    def _normalize_relative_path(cls, value: Path) -> Path:
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
    def backend_resume_enabled(self) -> bool:
        return self.backend_resume

    @property
    def planner_resume_enabled(self) -> bool:
        return self.planner_resume

    @property
    def planner_api_key_env_enabled(self) -> bool:
        return self.planner_api_key_env


def load_dispatch_config(
    *,
    repo_root: Path,
    config_path: Path | None = None,
    dry_run: bool = False,
    use_manual_frontend: bool = False,
    planner_api_key_env: bool = False,
    backend_resume: bool | None = None,
    planner_resume: bool | None = None,
) -> DispatchConfig:
    resolved_repo_root = Path(repo_root).resolve()
    data = _read_config_file(resolved_repo_root, config_path)
    data["repo_root"] = resolved_repo_root
    data["dry_run"] = dry_run
    data["use_manual_frontend"] = use_manual_frontend
    data["planner_api_key_env"] = planner_api_key_env
    _merge_agent_defaults(data)
    if backend_resume is not None:
        data["backend_resume"] = backend_resume
    if planner_resume is not None:
        data["planner_resume"] = planner_resume
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
