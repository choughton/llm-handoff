from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

from pydantic import ValidationError

from llm_handoff import config
from llm_handoff.agent_process import (
    _kill_process_tree,
    _process_group_popen_kwargs,
    _resolve_command_binary,
    build_agent_env,
)
from llm_handoff.normalizer_models import NormalizedNextAgent, normalizer_prompt

# Reading the prompt from stdin keeps the raw next_agent text from HANDOFF.md
# off the command line, where a Windows `.cmd` shim would pass it through
# cmd.exe argument parsing.
_STDIN_PROMPT_ARG = "-"

# After a timeout kills the process tree, wait at most this long for the
# pipes to close before giving up on the remaining output.
_POST_KILL_DRAIN_TIMEOUT_SECONDS = 1.0


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
        # The turn runs from a temp directory, which is not a Git checkout.
        "--skip-git-repo-check",
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
    # The normalizer needs no repository access. The read-only sandbox only
    # blocks writes, so the turn runs from a private temp directory instead of
    # the target repo: agent-authored next_agent text must not be able to
    # steer Codex into reading checkout files. The same directory holds the
    # schema and last-message files, so nothing is written into the repo.
    del repo_root
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
            completed = _run_codex_process(
                command,
                cwd=str(tmp_dir),
                env=_codex_cli_login_env(),
                input=normalizer_prompt(raw_value),
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


def _run_codex_process(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    input: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` with ``input`` on stdin, bounded by ``timeout`` seconds.

    ``subprocess.run`` kills only the immediate child on a timeout and then
    waits for the pipes to close, so a Codex tool process or the inner
    ``node`` behind a Windows ``.cmd`` shim could outlive the deadline. This
    starts the child in its own process group and, on a timeout, terminates
    the whole tree with the same helper the role adapters use.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_process_group_popen_kwargs(),
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.communicate(timeout=_POST_KILL_DRAIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # A descendant that escaped the tree kill still holds the pipes;
            # do not wait on it past the configured timeout.
            pass
        raise
    except BaseException:
        _kill_process_tree(process)
        raise
    return subprocess.CompletedProcess(
        command, process.returncode, stdout=stdout, stderr=stderr
    )


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
