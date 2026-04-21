from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable, Literal, get_args

from anthropic import Anthropic
import instructor
from pydantic import BaseModel, Field

from llm_handoff import config


DEFAULT_NORMALIZER_PROVIDER = config.NORMALIZER_PROVIDER
DEFAULT_NORMALIZER_MODEL = config.NORMALIZER_MODEL
ValidAgent = Literal[
    "auditor",
    "backend",
    "claude-audit",
    "claude-ledger",
    "codex",
    "finalizer",
    "frontend",
    "gemini-pe",
    "gemini-frontend",
    "planner",
    "validator",
    "user",
    "unknown",
]

CANONICAL_NEXT_AGENTS = tuple(
    agent for agent in get_args(ValidAgent) if agent != "unknown"
)
CANONICAL_NEXT_AGENT_SET = frozenset(CANONICAL_NEXT_AGENTS)
_NEXT_AGENT_LINE_RE = re.compile(r"^(\s*next_agent\s*:\s*).*$")


class NormalizedNextAgent(BaseModel):
    normalized: ValidAgent = Field(
        description=(
            "Canonical enum value for the given freeform next_agent input. "
            "Return 'unknown' if the input does not plausibly match any valid "
            "agent; do not guess when ambiguous."
        )
    )


@dataclass(frozen=True)
class HandoffNextAgentNormalization:
    content: str
    original: str | None = None
    normalized: str | None = None
    rewritten: bool = False
    unknown: bool = False


def normalize_next_agent(
    freeform: str,
    *,
    provider: str = DEFAULT_NORMALIZER_PROVIDER,
    model: str = DEFAULT_NORMALIZER_MODEL,
    timeout_ms: int = config.NORMALIZER_TIMEOUT_MS,
    api_key: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
) -> str:
    """Return a canonical next_agent enum value, or 'unknown' if no match."""

    raw_value = freeform.strip()
    if not raw_value:
        return "unknown"
    if raw_value in CANONICAL_NEXT_AGENT_SET:
        return raw_value

    if provider != "claude":
        raise ValueError(f"Unsupported next_agent normalizer provider `{provider}`.")

    if client is not None:
        return _normalize_next_agent_with_instructor(
            raw_value,
            client=client,
            model=model,
            max_retries=max_retries,
        )

    if _api_key_available(api_key):
        # Keep API-key auth and CLI OAuth separate so a bad key fails closed
        # instead of silently using a local interactive session.
        try:
            return _normalize_next_agent_with_instructor(
                raw_value,
                client=_build_claude_api_client(api_key),
                model=model,
                max_retries=max_retries,
            )
        except RuntimeError as exc:
            if not _is_sdk_auth_resolution_failure(exc):
                raise

    return _normalize_next_agent_with_claude_cli(
        raw_value,
        model=model,
        timeout_ms=timeout_ms,
    )


def _normalize_next_agent_with_instructor(
    raw_value: str,
    *,
    client: object,
    model: str,
    max_retries: int,
) -> str:
    instructed = instructor.from_anthropic(client)
    result = instructed.messages.create(
        model=model,
        response_model=NormalizedNextAgent,
        max_retries=max_retries,
        max_tokens=64,
        temperature=0,
        messages=[
            {
                "role": "user",
                "content": _normalizer_prompt(raw_value),
            }
        ],
    )
    return result.normalized


def _normalize_next_agent_with_claude_cli(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
) -> str:
    command = [
        _resolve_command_binary(config.CLAUDE_BINARY),
        config.CLAUDE_PERMISSIONS_FLAG,
        "--model",
        model,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(NormalizedNextAgent.model_json_schema()),
        "--no-session-persistence",
        "-p",
        _normalizer_prompt(raw_value),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=os.getcwd(),
            env=_claude_cli_oauth_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_ms / 1000,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Claude CLI next_agent normalization failed: {exc}"
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if detail:
            detail = f": {detail}"
        raise RuntimeError(
            f"Claude CLI next_agent normalization exited with code {completed.returncode}{detail}"
        )

    return _parse_claude_cli_normalization_output(completed.stdout)


def _parse_claude_cli_normalization_output(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude CLI normalizer returned non-JSON output.") from exc

    if isinstance(payload, dict) and "structured_output" in payload:
        response_payload = payload["structured_output"]
    elif isinstance(payload, dict) and "normalized" in payload:
        response_payload = payload
    elif isinstance(payload, dict) and "result" in payload:
        response_payload = _coerce_cli_result_payload(payload["result"])
    else:
        response_payload = payload

    return NormalizedNextAgent.model_validate(response_payload).normalized


def _api_key_available(api_key: str | None = None) -> bool:
    return bool(api_key or os.environ.get("ANTHROPIC_API_KEY"))


def _build_claude_api_client(api_key: str | None = None) -> Anthropic:
    effective_api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if effective_api_key:
        return Anthropic(api_key=effective_api_key)
    return Anthropic()


def _is_sdk_auth_resolution_failure(exc: RuntimeError) -> bool:
    return "could not resolve authentication method" in str(exc).lower()


def _coerce_cli_result_payload(result: object) -> object:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"normalized": result.strip()}
    return result


def _normalizer_prompt(raw_value: str) -> str:
    return (
        "Given this freeform `next_agent` value from a HANDOFF.md "
        f"frontmatter block: {raw_value!r}\n\n"
        "Return exactly one canonical enum value that best matches the intended "
        "receiving agent. Valid values are: "
        f"{', '.join(get_args(ValidAgent))}. Return 'unknown' if nothing "
        "plausibly matches or the value is ambiguous."
    )


def _claude_cli_oauth_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _resolve_command_binary(command: str) -> str:
    if not command:
        return command
    if Path(command).is_absolute() or any(sep in command for sep in ("\\", "/")):
        return command

    resolved = shutil.which(command)
    if resolved:
        return resolved

    path = Path(command)
    if path.suffix.lower() == ".cmd":
        resolved_without_cmd = shutil.which(path.stem)
        if resolved_without_cmd:
            return resolved_without_cmd

    return command


def normalize_handoff_next_agent_text(
    handoff_content: str,
    *,
    normalizer: Callable[[str], str] = normalize_next_agent,
) -> HandoffNextAgentNormalization:
    lines = handoff_content.splitlines()
    if not lines or lines[0].strip() != "---":
        return HandoffNextAgentNormalization(content=handoff_content)

    end_index = _frontmatter_end_index(lines)
    if end_index is None:
        return HandoffNextAgentNormalization(content=handoff_content)

    next_agent_index, original = _next_agent_line(lines, end_index)
    if next_agent_index is None or original is None:
        return HandoffNextAgentNormalization(content=handoff_content)
    if original in CANONICAL_NEXT_AGENT_SET:
        return HandoffNextAgentNormalization(
            content=handoff_content,
            original=original,
            normalized=original,
        )

    normalized = normalizer(original)
    if normalized == "unknown":
        return HandoffNextAgentNormalization(
            content=handoff_content,
            original=original,
            normalized=normalized,
            unknown=True,
        )
    if normalized not in CANONICAL_NEXT_AGENT_SET:
        raise ValueError(
            f"next_agent normalizer returned unsupported value `{normalized}`."
        )

    rewritten_lines = [*lines]
    match = _NEXT_AGENT_LINE_RE.match(rewritten_lines[next_agent_index])
    if match is None:
        return HandoffNextAgentNormalization(content=handoff_content)
    rewritten_lines[next_agent_index] = f"{match.group(1)}{normalized}"
    rewritten_content = "\n".join(rewritten_lines)
    if handoff_content.endswith("\n"):
        rewritten_content += "\n"

    return HandoffNextAgentNormalization(
        content=rewritten_content,
        original=original,
        normalized=normalized,
        rewritten=True,
    )


def _frontmatter_end_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return index
    return None


def _next_agent_line(
    lines: list[str],
    frontmatter_end_index: int,
) -> tuple[int | None, str | None]:
    for index in range(1, frontmatter_end_index):
        line = lines[index]
        match = _NEXT_AGENT_LINE_RE.match(line)
        if match is None:
            continue
        return index, line.split(":", 1)[1].strip().strip("'\"")
    return None, None

