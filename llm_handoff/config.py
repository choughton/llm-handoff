from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml


def _binary_name(base_name: str) -> str:
    return f"{base_name}.cmd" if os.name == "nt" else base_name


DISPATCH_WINDOW_TITLE = "llm-handoff dispatcher"
GEMINI_PLANNER_MENTION = "@planner"
GEMINI_FRONTEND_MENTION = "@frontend"
CODEX_SKILL_NAME = "llm-handoff"
CODEX_WEB_SEARCH_MODE = "disabled"

CLAUDE_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
CLAUDE_MODEL = "claude-opus-4-7"
NORMALIZER_PROVIDER = "claude"
NORMALIZER_MODEL = "claude-haiku-4-5"
NORMALIZER_TIMEOUT_MS = 60_000

AGENT_TIMEOUT_MS = 1_200_000
SUBAGENT_TIMEOUT_MS = 900_000

POLL_INTERVAL_SECONDS = 30
MAX_CONSECUTIVE_FAILURES = 3

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
CODEX_OUTPUT_SCHEMA_PATH = (
    Path("llm_handoff") / "schemas" / "codex_final_response.schema.json"
)
CODEX_OUTPUT_DIRECTORY = Path("logs") / "dispatch" / "codex"
CODEX_OUTPUT_LAST_MESSAGE_PATH = CODEX_OUTPUT_DIRECTORY / "last-message.json"
CODEX_SESSION_STATE_PATH = CODEX_OUTPUT_DIRECTORY / "session.json"


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
UnknownNormalizerPolicy = Literal["fail_closed"]

SUPPORTED_AGENT_PROVIDERS: dict[AgentRole, ProviderName] = {
    "planner": "gemini",
    "backend": "codex",
    "frontend": "gemini",
    "auditor": "claude",
    "validator": "claude",
    "finalizer": "claude",
}
REQUIRED_AGENT_ROLES = frozenset(SUPPORTED_AGENT_PROVIDERS)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    binary: str | None = None
    mention: str | None = None
    skill_name: str | None = None
    model: str | None = None
    permissions_flag: str | None = None
    resume: bool | None = None
    timeout_ms: int | None = None
    retries: int | None = None
    use_api_key_env: bool | None = None

    @field_validator("timeout_ms", mode="after")
    @classmethod
    def _validate_timeout_ms(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("timeout_ms must be at least 1 when set.")
        return value

    @field_validator("retries", mode="after")
    @classmethod
    def _validate_retries(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("retries must be non-negative when set.")
        return value


class NormalizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName = NORMALIZER_PROVIDER
    model: str = NORMALIZER_MODEL
    timeout_ms: int = NORMALIZER_TIMEOUT_MS
    on_unknown: UnknownNormalizerPolicy = "fail_closed"

    @field_validator("provider", mode="after")
    @classmethod
    def _validate_provider(cls, value: ProviderName) -> ProviderName:
        if value != "claude":
            raise ValueError(
                "current reference dispatcher supports provider `claude` for "
                "next-agent normalization. Additional normalizer providers are "
                "planned but not implemented yet."
            )
        return value

    @field_validator("timeout_ms", mode="after")
    @classmethod
    def _validate_timeout_ms(cls, value: int) -> int:
        if value < 1:
            raise ValueError("normalizer timeout_ms must be at least 1.")
        return value


def _default_agent_configs() -> dict[AgentRole, AgentConfig]:
    return {
        "planner": AgentConfig(
            provider="gemini",
            binary=GEMINI_BINARY,
            mention=GEMINI_PLANNER_MENTION,
            resume=GEMINI_RESUME_DEFAULT,
            timeout_ms=AGENT_TIMEOUT_MS,
            retries=GEMINI_MAX_RETRIES,
        ),
        "backend": AgentConfig(
            provider="codex",
            binary=CODEX_BINARY,
            skill_name=CODEX_SKILL_NAME,
            resume=True,
            timeout_ms=AGENT_TIMEOUT_MS,
        ),
        "frontend": AgentConfig(
            provider="gemini",
            binary=GEMINI_BINARY,
            mention=GEMINI_FRONTEND_MENTION,
            resume=False,
            timeout_ms=AGENT_TIMEOUT_MS,
        ),
        "auditor": AgentConfig(
            provider="claude",
            binary=CLAUDE_BINARY,
            model=CLAUDE_MODEL,
            permissions_flag=CLAUDE_PERMISSIONS_FLAG,
            resume=False,
            timeout_ms=SUBAGENT_TIMEOUT_MS,
        ),
        "validator": AgentConfig(
            provider="claude",
            binary=CLAUDE_BINARY,
            model=NORMALIZER_MODEL,
            permissions_flag=CLAUDE_PERMISSIONS_FLAG,
            resume=False,
            timeout_ms=SUBAGENT_TIMEOUT_MS,
        ),
        "finalizer": AgentConfig(
            provider="claude",
            binary=CLAUDE_BINARY,
            model=CLAUDE_MODEL,
            permissions_flag=CLAUDE_PERMISSIONS_FLAG,
            resume=False,
            timeout_ms=SUBAGENT_TIMEOUT_MS,
        ),
    }


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    handoff_path: Path = DEFAULT_HANDOFF_PATH
    project_state_path: Path = DEFAULT_PROJECT_STATE_PATH
    auto_push: bool = False
    agents: dict[AgentRole, AgentConfig] = Field(default_factory=_default_agent_configs)
    normalizer: NormalizerConfig = Field(default_factory=NormalizerConfig)
    dry_run: bool = False
    use_manual_frontend: bool = False
    planner_api_key_env: bool = False
    backend_resume: bool = True
    planner_resume: bool = GEMINI_RESUME_DEFAULT
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES

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
            expected_provider = SUPPORTED_AGENT_PROVIDERS[role]
            if agent_config.provider != expected_provider:
                raise ValueError(
                    "current reference dispatcher supports provider "
                    f"`{expected_provider}` for role `{role}`; configured "
                    f"`{agent_config.provider}`. True provider portability is "
                    "planned but not implemented yet."
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
            default_config = default_agents.get(role)
            if isinstance(default_config, dict):
                merged_agents[role] = {**default_config, **configured}
                continue
        merged_agents[role] = configured
    data["agents"] = merged_agents


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
