from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator


def _binary_name(base_name: str) -> str:
    return f"{base_name}.cmd" if os.name == "nt" else base_name


GEMINI_PE_MENTION = "@crossfire_pe"
GEMINI_FRONTEND_MENTION = "@crossfire_frontend"
CODEX_SKILL_NAME = "llm-crossfire-codex"
CODEX_WEB_SEARCH_MODE = "disabled"

CLAUDE_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
CLAUDE_MODEL = "claude-opus-4-7"

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
DEFAULT_CLAUDE_MD_PATH = Path("CLAUDE.md")
DEFAULT_SHARED_INIT_PROMPT_PATH = Path("docs/handoff/SHARED_REPO_INIT_PROMPT.md")
CODEX_OUTPUT_SCHEMA_PATH = (
    Path("tools") / "dispatch" / "schemas" / "codex_final_response.schema.json"
)
CODEX_OUTPUT_DIRECTORY = Path("logs") / "dispatch" / "codex"
CODEX_OUTPUT_LAST_MESSAGE_PATH = CODEX_OUTPUT_DIRECTORY / "last-message.json"
CODEX_SESSION_STATE_PATH = CODEX_OUTPUT_DIRECTORY / "session.json"


def detect_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    candidates = (current, *current.parents)

    for candidate in candidates:
        if (candidate / "AGENTS.md").exists() and (
            candidate / DEFAULT_HANDOFF_PATH
        ).exists():
            return candidate

    return current


class DispatchConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo_root: Path
    handoff_path: Path = DEFAULT_HANDOFF_PATH
    claude_md_path: Path = DEFAULT_CLAUDE_MD_PATH
    dry_run: bool = False
    use_antigravity: bool = False
    use_gemini_api_key_env: bool = False
    use_codex_resume: bool = True
    use_gemini_resume: bool = GEMINI_RESUME_DEFAULT
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES

    @field_validator("repo_root", mode="after")
    @classmethod
    def _resolve_repo_root(cls, value: Path) -> Path:
        return Path(value).resolve()

    @field_validator("handoff_path", "claude_md_path", mode="after")
    @classmethod
    def _normalize_relative_path(cls, value: Path) -> Path:
        return Path(value)

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
    def claude_md_full_path(self) -> Path:
        return (self.repo_root / self.claude_md_path).resolve()

