from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from pydantic import ValidationError

from llm_handoff import config
from llm_handoff.agent_process import _resolve_command_binary, build_agent_env
from llm_handoff.normalizer_models import NormalizedNextAgent, normalizer_prompt

# Reading the prompt from stdin keeps the raw next_agent text from HANDOFF.md
# off the command line, where a Windows `.cmd` shim would pass it through
# cmd.exe argument parsing.
_STDIN_PROMPT_ARG = "-"


def normalize_next_agent_with_codex(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
    api_key: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
    repo_root: Path | None = None,
) -> str:
    # CLI-only adapter: the Codex CLI uses its own login. API keys, injected
    # clients, and retries are accepted for adapter parity only.
    del api_key, client, max_retries
    return _normalize_next_agent_with_codex_cli(
        raw_value,
        model=model,
        timeout_ms=timeout_ms,
        repo_root=repo_root,
    )


def codex_normalizer_schema() -> dict[str, object]:
    # Codex structured output needs a closed object schema.
    schema = NormalizedNextAgent.model_json_schema()
    return {**schema, "additionalProperties": False}


def build_codex_normalizer_command(
    *,
    binary: str,
    model: str,
    schema_path: Path,
    last_message_path: Path,
) -> list[str]:
    return [
        binary,
        "exec",
        "--sandbox",
        "read-only",
        "-c",
        f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
        "--model",
        model,
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(last_message_path),
        _STDIN_PROMPT_ARG,
    ]


def _normalize_next_agent_with_codex_cli(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
    repo_root: Path | None = None,
) -> str:
    # Launch inside the target repo so project-level Codex settings come from
    # the repo being dispatched, not the dispatcher's own checkout.
    cwd = str(repo_root) if repo_root is not None else os.getcwd()
    # The schema and last-message files live in a private temp directory so
    # the normalizer never writes into the target repository.
    with tempfile.TemporaryDirectory(prefix="llm-handoff-normalizer-") as tmp:
        tmp_dir = Path(tmp)
        schema_path = tmp_dir / "normalizer.schema.json"
        last_message_path = tmp_dir / "normalizer.last-message.json"
        schema_path.write_text(
            json.dumps(codex_normalizer_schema(), indent=2) + "\n",
            encoding="utf-8",
        )
        command = build_codex_normalizer_command(
            binary=_resolve_command_binary(config.CODEX_BINARY),
            model=model,
            schema_path=schema_path,
            last_message_path=last_message_path,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=_codex_cli_login_env(),
                input=normalizer_prompt(raw_value),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_ms / 1000,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"Codex CLI next_agent normalization failed: {exc}"
            ) from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            if detail:
                detail = f": {detail}"
            raise RuntimeError(
                "Codex CLI next_agent normalization exited with code "
                f"{completed.returncode}{detail}"
            )

        last_message = _read_text(last_message_path)

    return _parse_codex_cli_normalization_output(last_message or completed.stdout)


def _parse_codex_cli_normalization_output(output: str) -> str:
    text = output.strip()
    if not text:
        raise RuntimeError("Codex CLI normalizer returned no output.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex CLI normalizer returned non-JSON output.") from exc
    try:
        return NormalizedNextAgent.model_validate(payload).normalized
    except ValidationError as exc:
        raise RuntimeError(
            "Codex CLI normalizer returned output that does not match the "
            "next_agent schema."
        ) from exc


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _codex_cli_login_env() -> dict[str, str]:
    # Same allowlist as the Codex role adapter, with vendor API keys withheld:
    # the normalizer runs on the user's Codex login and never on
    # OPENAI_API_KEY.
    return build_agent_env(
        config.AgentConfig(provider="codex"),
        use_api_key_env=False,
    )
