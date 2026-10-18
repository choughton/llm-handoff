"""Antigravity CLI (`agy`) next-agent normalizer.

Each call writes a one-off workspace agent with only the ``finish`` tool into
a private temp directory, runs ``agy`` there in print mode with stream-json
output and a JSON schema, and accepts only the clean shape captured from
agy 1.4.2: ``user_input``, ``agent_response``, and ``finish`` steps, then one
``SUCCESS`` result whose ``structured_output`` matches the schema. Anything
else raises ``RuntimeError``, which leaves the handoff unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from pydantic import ValidationError

from llm_handoff import config
from llm_handoff.agent_process import (
    _kill_process_tree,
    _process_group_popen_kwargs,
    _resolve_command_binary,
    build_agent_env,
)
from llm_handoff.agent_providers.antigravity import _unwrap_stream_event
from llm_handoff.normalizer_models import NormalizedNextAgent, normalizer_prompt
from llm_handoff.provider_auth import (
    antigravity_auth_failure_line,
    find_antigravity_auth_failure,
    find_permission_soft_deny,
    permission_soft_deny_message,
)

# Each call names its agent with a fresh random suffix, so no global agent
# can already exist under that name and shadow the one written here.
ANTIGRAVITY_NORMALIZER_AGENT_PREFIX = "llm-handoff-normalizer-"
ANTIGRAVITY_NORMALIZER_DESCRIPTION = "Classifies one freeform next_agent value."
ANTIGRAVITY_NORMALIZER_SCHEMA_FILENAME = "normalizer.schema.json"
# The -p text is fixed. The raw next_agent value only reaches agy through
# the agent file body, never through the command line.
ANTIGRAVITY_NORMALIZER_INSTRUCTION = (
    "Classify the value in your instructions and answer with finish."
)
ANTIGRAVITY_NORMALIZER_SUCCESS_STATUS = "SUCCESS"
# The only step types in a clean run. Any other step, including every
# `tool` step whatever its tool_name, fails closed.
ANTIGRAVITY_NORMALIZER_ALLOWED_STEP_TYPES = frozenset(
    {"user_input", "agent_response", "finish"}
)
# The only stream-json events in a clean run.
_ALLOWED_EVENTS = frozenset({"init", "step_update", "result"})
# Longest status value quoted in an error message.
_MAX_STATUS_CHARS = 40

# After a timeout kills the process tree, wait at most this long for the
# pipes to close before giving up on the remaining output.
_POST_KILL_DRAIN_TIMEOUT_SECONDS = 1.0

_ERROR_PREFIX = "Antigravity CLI next_agent normalization"


def normalize_next_agent_with_antigravity(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
    api_key: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
    repo_root: Path | None = None,
) -> str:
    # CLI-only adapter: agy uses its own login. API keys, injected clients,
    # retries, and the repo root are accepted for adapter parity only; the
    # turn runs from a private temp directory, never from the target repo.
    del api_key, client, max_retries, repo_root
    with tempfile.TemporaryDirectory(prefix="llm-handoff-normalizer-") as tmp:
        tmp_dir = Path(tmp)
        schema_path = tmp_dir / ANTIGRAVITY_NORMALIZER_SCHEMA_FILENAME
        schema_path.write_text(
            json.dumps(antigravity_normalizer_schema(), indent=2) + "\n",
            encoding="utf-8",
        )
        agent_name = f"{ANTIGRAVITY_NORMALIZER_AGENT_PREFIX}{uuid.uuid4().hex}"
        agent_path = tmp_dir / ".agents" / "agents" / f"{agent_name}.md"
        agent_path.parent.mkdir(parents=True)
        agent_path.write_text(
            antigravity_normalizer_agent_file(agent_name, raw_value),
            encoding="utf-8",
        )
        command = build_antigravity_normalizer_command(
            binary=_resolve_command_binary(config.ANTIGRAVITY_BINARY),
            agent_name=agent_name,
            schema_path=schema_path,
            timeout_ms=timeout_ms,
            model=model,
        )
        try:
            completed = _run_antigravity_process(
                command,
                cwd=str(tmp_dir),
                env=build_agent_env(config.AgentConfig(provider="antigravity")),
                timeout=timeout_ms / 1000,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{_ERROR_PREFIX} timed out after {timeout_ms} ms."
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"{_ERROR_PREFIX} could not start `{command[0]}`: "
                f"{exc.strerror or type(exc).__name__}."
            ) from exc

    return _parse_antigravity_normalizer_result(completed)


def antigravity_normalizer_schema() -> dict[str, object]:
    # agy validates structured output against a closed object schema.
    schema = NormalizedNextAgent.model_json_schema()
    return {**schema, "additionalProperties": False}


def antigravity_normalizer_agent_file(agent_name: str, raw_value: str) -> str:
    """Return the workspace agent file for one normalizer call.

    The body is ``normalizer_prompt(raw_value)``, which quotes the value with
    ``repr``, so a newline or ``---`` in the value cannot end the frontmatter.
    ``commandExecutionPolicy: off`` is accepted by agy 1.4.2 but not shown to
    be honored; ``tools: [finish]`` and the step-type check are the boundary.
    """
    return (
        "---\n"
        f"name: {agent_name}\n"
        f"description: {ANTIGRAVITY_NORMALIZER_DESCRIPTION}\n"
        "tools: [finish]\n"
        "commandExecutionPolicy: off\n"
        "---\n"
        "\n"
        f"{normalizer_prompt(raw_value)}\n"
    )


def build_antigravity_normalizer_command(
    *,
    binary: str,
    agent_name: str,
    schema_path: Path,
    timeout_ms: int,
    model: str,
) -> list[str]:
    return [
        binary,
        "-p",
        ANTIGRAVITY_NORMALIZER_INSTRUCTION,
        "--agent",
        agent_name,
        "--output-format",
        "stream-json",
        "--json-schema",
        str(schema_path),
        "--print-timeout",
        f"{max(1, timeout_ms // 1000)}s",
        "--model",
        model,
    ]


def _run_antigravity_process(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` with no stdin, bounded by ``timeout`` seconds.

    The child starts in its own process group and, on a timeout, the whole
    tree is killed, so the inner ``node`` behind a Windows ``.cmd`` shim
    cannot outlive the deadline.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_process_group_popen_kwargs(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
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


def _parse_antigravity_normalizer_result(
    completed: subprocess.CompletedProcess[str],
) -> str:
    """Return the normalized value from one agy run, or raise RuntimeError.

    Error messages never quote stdout, which can echo the raw next_agent
    value; only the result status value is named.
    """
    stderr = completed.stderr or ""
    auth_failure = find_antigravity_auth_failure(stderr)
    if auth_failure is not None:
        raise RuntimeError(
            f"{_ERROR_PREFIX} failed: "
            f"{antigravity_auth_failure_line(auth_failure)}"
        )
    soft_deny = find_permission_soft_deny(stderr)
    if soft_deny is not None:
        raise RuntimeError(
            f"{_ERROR_PREFIX} failed: {permission_soft_deny_message(soft_deny)}"
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{_ERROR_PREFIX} exited with code {completed.returncode}."
        )

    results: list[dict[str, object]] = []
    for line_number, line in enumerate((completed.stdout or "").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            # `from None`: the decode error carries the stdout line.
            raise RuntimeError(
                f"{_ERROR_PREFIX} failed: stdout line {line_number} is not JSON."
            ) from None
        if not isinstance(event, dict):
            raise RuntimeError(
                f"{_ERROR_PREFIX} failed: stdout line {line_number} is not a "
                "JSON object."
            )
        name, payload = _unwrap_stream_event(event)
        if name not in _ALLOWED_EVENTS:
            raise RuntimeError(
                f"{_ERROR_PREFIX} failed: stdout line {line_number} is not an "
                "init, step_update, or result event."
            )
        if results:
            raise RuntimeError(
                f"{_ERROR_PREFIX} failed: stdout line {line_number} follows "
                "the result event."
            )
        if name == "step_update":
            step_type = payload.get("step_type")
            if step_type not in ANTIGRAVITY_NORMALIZER_ALLOWED_STEP_TYPES:
                raise RuntimeError(
                    f"{_ERROR_PREFIX} failed: stdout line {line_number} is a "
                    "step other than user_input, agent_response, or finish."
                )
        elif name == "result":
            results.append(payload)

    if not results:
        raise RuntimeError(f"{_ERROR_PREFIX} failed: agy printed no result event.")
    result = results[0]
    status = result.get("status")
    if status != ANTIGRAVITY_NORMALIZER_SUCCESS_STATUS:
        raise RuntimeError(
            f"{_ERROR_PREFIX} failed: result status is "
            f"{_describe_status(status)}, not "
            f"{ANTIGRAVITY_NORMALIZER_SUCCESS_STATUS}."
        )
    # `response` holds the raw finish arguments with extra keys; it is never
    # read, not even as a fallback.
    structured_output = result.get("structured_output")
    if not isinstance(structured_output, dict):
        raise RuntimeError(
            f"{_ERROR_PREFIX} failed: the result has no structured_output object."
        )
    if set(structured_output) != set(NormalizedNextAgent.model_fields):
        raise RuntimeError(
            f"{_ERROR_PREFIX} failed: structured_output keys do not match the "
            "next_agent schema."
        )
    try:
        return NormalizedNextAgent.model_validate(structured_output).normalized
    except ValidationError:
        # `from None`: the validation error quotes the model output.
        raise RuntimeError(
            f"{_ERROR_PREFIX} failed: structured_output does not match the "
            "next_agent schema."
        ) from None


def _describe_status(status: object) -> str:
    if not isinstance(status, str):
        return "missing" if status is None else "not a string"
    return repr(status[:_MAX_STATUS_CHARS])
