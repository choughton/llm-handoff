from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from anthropic import Anthropic
import instructor

from llm_handoff import config
from llm_handoff.normalizer_models import NormalizedNextAgent, normalizer_prompt


def normalize_next_agent_with_claude(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
    api_key: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
) -> str:
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
                "content": normalizer_prompt(raw_value),
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
        normalizer_prompt(raw_value),
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
