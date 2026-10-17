from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from pydantic import ValidationError

from llm_handoff import config
from llm_handoff.agent_process import _resolve_command_binary, build_agent_env
from llm_handoff.normalizer_models import NormalizedNextAgent, normalizer_prompt
from llm_handoff.normalizer_providers.codex import (
    _run_codex_process as _run_cli_process,
)
from llm_handoff.provider_auth import auth_fix_instructions, find_auth_failure

# Headless Gemini CLI appends the `-p` text to whatever arrives on stdin. The
# request itself travels on stdin, so the raw next_agent text from HANDOFF.md
# stays off the command line (a Windows `.cmd` shim would pass it through
# cmd.exe argument parsing); `-p` only carries this fixed instruction.
GEMINI_CLI_NORMALIZER_INSTRUCTION = (
    "Answer the request above with only the JSON object it asks for: no "
    "prose, no explanation, and no code fence."
)
_CODE_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*|\s*```$")


def normalize_next_agent_with_gemini(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
    api_key: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
    repo_root: Path | None = None,
) -> str:
    # repo_root is accepted for adapter parity only: the CLI path runs from a
    # private temp directory, never from the target repository.
    del max_retries, repo_root
    if client is not None or _api_key_available(api_key):
        # Keep API-key auth and the CLI login separate so a bad key fails
        # closed instead of silently switching to the CLI's sign-in.
        resolved_client = client or _build_gemini_api_client(
            api_key=api_key,
            timeout_ms=timeout_ms,
        )
        return _normalize_next_agent_with_gemini_api(
            raw_value,
            client=resolved_client,
            model=model,
        )
    return _normalize_next_agent_with_gemini_cli(
        raw_value,
        model=model,
        timeout_ms=timeout_ms,
    )


def _api_key_available(api_key: str | None = None) -> bool:
    return bool(
        api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )


def build_gemini_normalizer_command(*, binary: str, model: str) -> list[str]:
    return [
        binary,
        # Read-only: the call needs no tools. Headless Gemini CLI auto-rejects
        # any tool call plan mode would not allow.
        "--approval-mode",
        "plan",
        # The run starts in an empty temp directory and reads nothing from
        # the repository, so it needs no container sandbox (and no Docker).
        "--sandbox=false",
        # Headless Gemini CLI refuses to run in a folder it has not been told
        # to trust; the private temp directory is ours and empty.
        "--skip-trust",
        "--model",
        model,
        "--output-format",
        "json",
        "-p",
        GEMINI_CLI_NORMALIZER_INSTRUCTION,
    ]


def gemini_cli_normalizer_prompt(raw_value: str) -> str:
    return (
        f"{normalizer_prompt(raw_value)}\n\n"
        'Reply with only a JSON object of the form {"normalized": "<value>"}.'
    )


def _normalize_next_agent_with_gemini_cli(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
) -> str:
    # The normalizer needs no repository access, so the turn runs from a
    # private temp directory instead of the target repo: agent-authored
    # next_agent text must not be able to steer Gemini CLI into reading
    # checkout files, and project-level .gemini/ settings do not apply.
    with tempfile.TemporaryDirectory(prefix="llm-handoff-normalizer-") as tmp:
        command = build_gemini_normalizer_command(
            binary=_resolve_command_binary(config.GEMINI_BINARY),
            model=model,
        )
        try:
            completed = _run_cli_process(
                command,
                cwd=tmp,
                env=_gemini_cli_login_env(),
                input=gemini_cli_normalizer_prompt(raw_value),
                timeout=timeout_ms / 1000,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"Gemini CLI next_agent normalization failed: {exc}"
            ) from exc

    auth_failure = find_auth_failure(completed.stdout, completed.stderr)
    if auth_failure is not None:
        raise RuntimeError(
            "Gemini CLI next_agent normalization failed: Gemini CLI is not "
            f"signed in ({auth_failure.strip()}). {auth_fix_instructions('gemini')} "
            "For the normalizer, GEMINI_API_KEY in the dispatcher's environment "
            "selects the API path instead of the CLI login."
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if detail:
            detail = f": {detail}"
        raise RuntimeError(
            "Gemini CLI next_agent normalization exited with code "
            f"{completed.returncode}{detail}"
        )
    return _parse_gemini_cli_normalization_output(completed.stdout)


def _parse_gemini_cli_normalization_output(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        raise RuntimeError("Gemini CLI normalizer returned no output.")
    # `--output-format json` wraps the answer as {"response": ..., "stats": ...};
    # an older CLI that ignores the flag prints the answer itself.
    response: object = text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and "response" in payload:
        response = payload["response"]
    elif isinstance(payload, dict):
        response = payload
    return _coerce_gemini_cli_response(response)


def _coerce_gemini_cli_response(response: object) -> str:
    if isinstance(response, str):
        answer = _CODE_FENCE_RE.sub("", response.strip()).strip()
        try:
            response = json.loads(answer)
        except json.JSONDecodeError:
            response = answer
        if isinstance(response, str):
            # A bare role name, quoted or not, is still an answer; the schema
            # decides whether it is one of the valid values.
            response = {"normalized": response.strip("'\"` .").lower()}
    try:
        return NormalizedNextAgent.model_validate(response).normalized
    except ValidationError as exc:
        raise RuntimeError(
            "Gemini CLI normalizer returned output that does not match the "
            "next_agent schema."
        ) from exc


def _gemini_cli_login_env() -> dict[str, str]:
    # Same allowlist as the Gemini role adapter, with vendor API keys
    # withheld: this path runs on the user's Gemini CLI sign-in only.
    return build_agent_env(
        config.AgentConfig(provider="gemini"),
        use_api_key_env=False,
    )


def _normalize_next_agent_with_gemini_api(
    raw_value: str,
    *,
    client: object,
    model: str,
) -> str:
    response = client.models.generate_content(
        model=model,
        contents=normalizer_prompt(raw_value),
        config=_gemini_generation_config(),
    )
    return _coerce_gemini_normalization_output(response)


def _build_gemini_api_client(
    *,
    api_key: str | None = None,
    timeout_ms: int,
) -> object:
    effective_api_key = (
        api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    if not effective_api_key:
        raise RuntimeError(
            "Gemini next_agent normalization requires GEMINI_API_KEY or GOOGLE_API_KEY."
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "Gemini next_agent normalization requires the google-genai package."
        ) from exc

    return genai.Client(
        api_key=effective_api_key,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )


def _gemini_generation_config() -> object:
    try:
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "Gemini next_agent normalization requires the google-genai package."
        ) from exc

    return types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=64,
        response_mime_type="application/json",
        response_schema=NormalizedNextAgent,
    )


def _coerce_gemini_normalization_output(response: object) -> str:
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        return NormalizedNextAgent.model_validate(parsed).normalized

    text = getattr(response, "text", None)
    if isinstance(text, str):
        return NormalizedNextAgent.model_validate_json(text).normalized

    raise RuntimeError("Gemini normalizer returned no structured output.")
