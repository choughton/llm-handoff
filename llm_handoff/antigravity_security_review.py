"""Antigravity CLI (`agy`) read-only security reviewer.

Each review writes a one-off workspace agent into a private temp directory
outside the repository and runs ``agy`` there in print mode with stream-json
output and the security verdict JSON schema. The agent file is written only
by this module, under a fresh random name, and allows only the read tools in
``ANTIGRAVITY_SECURITY_TOOLS``: no ``run_command``, and no write, browser,
image, MCP, or subagent tools. The review input is a copy of the review
scope's diff inside the temp directory; the repository root is named as
read-only context. If the system temp directory (``TMPDIR``, ``TEMP``, or
``TMP``) resolves inside the repository, the review fails closed before any
file is written.

Output parsing fails closed. Only ``init``, ``step_update``, and ``result``
events are accepted; only ``user_input``, ``agent_response``, ``finish``, and
``tool`` steps whose ``tool_name`` is in the reviewer tool list; then exactly
one ``SUCCESS`` result whose ``structured_output`` has exactly the verdict
keys. Error text is fixed and never quotes agy's stdout or stderr. The step
checks run only after agy exits, and user-wide ``permissions.allow`` grants
still apply to the run.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Mapping
import uuid

from llm_handoff import config
from llm_handoff.agent_process import _resolve_command_binary, build_agent_env
from llm_handoff.agent_providers.antigravity import _unwrap_stream_event
from llm_handoff.normalizer_providers.antigravity import _run_antigravity_process
from llm_handoff.provider_auth import (
    find_antigravity_auth_failure,
    find_permission_soft_deny,
)


# The only tools the reviewer agent gets. Every one of them is read-only;
# `finish` returns the structured verdict.
ANTIGRAVITY_SECURITY_TOOLS: tuple[str, ...] = (
    "view_file",
    "list_dir",
    "grep_search",
    "find_by_name",
    "finish",
)
# Each review names its agent with a fresh random suffix, so no global agent
# can already exist under that name and shadow the one written here.
ANTIGRAVITY_SECURITY_AGENT_PREFIX = "llm-handoff-security-"
ANTIGRAVITY_SECURITY_DESCRIPTION = (
    "Read-only security review of one committed diff."
)
ANTIGRAVITY_SECURITY_SCHEMA_FILENAME = "security.schema.json"
ANTIGRAVITY_SECURITY_DIFF_FILENAME = "scope.diff"
# The -p text is fixed. The review instructions reach agy only through the
# agent file body, and the diff contents only through the temp diff file.
ANTIGRAVITY_SECURITY_INSTRUCTION = (
    "Perform the security review described in your instructions and answer "
    "with finish."
)
ANTIGRAVITY_SECURITY_SUFFIX_TEMPLATE = (
    " You cannot run `git` or any other shell command in this review. The "
    "review input is the diff file {diff_path}; open it with view_file. The "
    "repository root {repo_root} is read-only context: you may open files "
    "under it with view_file, list_dir, grep_search, and find_by_name, and "
    "you must not change anything. Return the verdict object by calling "
    "finish."
)
ANTIGRAVITY_SECURITY_SUCCESS_STATUS = "SUCCESS"
ANTIGRAVITY_SECURITY_ALLOWED_STEP_TYPES = frozenset(
    {"user_input", "agent_response", "finish", "tool"}
)
ANTIGRAVITY_SECURITY_VERDICT_KEYS = frozenset({"verdict", "summary", "findings"})
_ALLOWED_EVENTS = frozenset({"init", "step_update", "result"})
_ALLOWED_TOOLS = frozenset(ANTIGRAVITY_SECURITY_TOOLS)

_FAILED = "security_reviewer_failed: antigravity security reviewer"
_MALFORMED = "security_verdict_malformed: antigravity security reviewer"


@dataclass(frozen=True)
class AntigravitySecurityRun:
    """One agy security review: the argv that ran and its parsed outcome."""

    command: tuple[str, ...]
    exit_code: int
    elapsed_seconds: float
    stdout: str
    stderr: str
    structured_output: dict[str, object] | None = None
    error: str | None = None
    start_failure: str | None = None


def antigravity_security_model(
    agent_config: config.AgentConfig | None,
) -> str | None:
    """Return the model borrowed from the antigravity role, if it sets one."""
    if agent_config is None or not agent_config.model:
        return None
    return agent_config.model


def antigravity_security_env(
    agent_config: config.AgentConfig | None,
) -> dict[str, str]:
    """Return the reviewer environment: the antigravity role's env rules."""
    return build_agent_env(
        agent_config or config.AgentConfig(provider="antigravity")
    )


def antigravity_security_suffix(*, diff_path: Path, repo_root: Path) -> str:
    return ANTIGRAVITY_SECURITY_SUFFIX_TEMPLATE.format(
        diff_path=diff_path.as_posix(),
        repo_root=Path(repo_root).resolve().as_posix(),
    )


def antigravity_security_agent_file(agent_name: str, instructions: str) -> str:
    """Return the workspace agent file for one security review.

    ``commandExecutionPolicy: off`` is accepted by agy but not shown to be
    honored. The pre-execution boundary is agy's enforcement of the
    ``tools`` list; user-wide ``permissions.allow`` grants still apply.
    """
    tools = ", ".join(ANTIGRAVITY_SECURITY_TOOLS)
    return (
        "---\n"
        f"name: {agent_name}\n"
        f"description: {ANTIGRAVITY_SECURITY_DESCRIPTION}\n"
        f"tools: [{tools}]\n"
        "commandExecutionPolicy: off\n"
        "---\n"
        "\n"
        f"{instructions}\n"
    )


def build_antigravity_security_command(
    *,
    binary: str,
    agent_name: str,
    schema_path: Path | str,
    timeout_ms: int,
    model: str | None,
) -> list[str]:
    # Never adds the unsafe-bypass arguments, even when the antigravity role
    # sets unsafe_bypass: the reviewer is read-only.
    command = [
        binary,
        "-p",
        ANTIGRAVITY_SECURITY_INSTRUCTION,
        "--agent",
        agent_name,
        "--output-format",
        "stream-json",
        "--json-schema",
        str(schema_path),
        "--print-timeout",
        f"{max(1, timeout_ms // 1000)}s",
    ]
    if model:
        command.extend(["--model", model])
    return command


def run_antigravity_security_review(
    *,
    binary: str,
    model: str | None,
    env: Mapping[str, str],
    timeout_ms: int,
    diff_source: Path,
    build_instructions: Callable[[Path], str],
    schema: Mapping[str, object],
    repo_root: Path,
) -> AntigravitySecurityRun:
    """Run one agy security review from a private temp directory.

    ``build_instructions`` receives the temp diff copy's path and returns the
    agent file body. The temp directory is removed after the run, including
    after a timeout or an error.
    """
    start = time.monotonic()
    resolved_binary = _resolve_reviewer_binary(binary, repo_root)
    command: list[str] = [resolved_binary]
    temp_base = reviewer_temp_base(repo_root)
    if temp_base is None:
        # TMPDIR, TEMP, or TMP points inside the checkout. Fail closed rather
        # than expose the reviewer workspace to the repository.
        return AntigravitySecurityRun(
            command=tuple(command),
            exit_code=1,
            elapsed_seconds=time.monotonic() - start,
            stdout="",
            stderr="",
            error=(
                f"{_FAILED} failed: the system temp directory is inside the "
                "repository; point TMPDIR, TEMP, or TMP outside the repository."
            ),
            start_failure="preflight",
        )
    with tempfile.TemporaryDirectory(
        prefix="llm-handoff-security-", dir=str(temp_base)
    ) as tmp:
        tmp_dir = Path(tmp)
        if _is_within(tmp_dir, repo_root):
            # Defense in depth: the created directory resolves inside the
            # repository, for example through a symlink or junction.
            return AntigravitySecurityRun(
                command=tuple(command),
                exit_code=1,
                elapsed_seconds=time.monotonic() - start,
                stdout="",
                stderr="",
                error=(
                    f"{_FAILED} failed: the reviewer temp directory resolves "
                    "inside the repository."
                ),
                start_failure="preflight",
            )
        schema_path = tmp_dir / ANTIGRAVITY_SECURITY_SCHEMA_FILENAME
        schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        diff_path = tmp_dir / ANTIGRAVITY_SECURITY_DIFF_FILENAME
        shutil.copyfile(diff_source, diff_path)
        agent_name = f"{ANTIGRAVITY_SECURITY_AGENT_PREFIX}{uuid.uuid4().hex}"
        agent_path = tmp_dir / ".agents" / "agents" / f"{agent_name}.md"
        agent_path.parent.mkdir(parents=True)
        agent_path.write_text(
            antigravity_security_agent_file(agent_name, build_instructions(diff_path)),
            encoding="utf-8",
        )
        command = build_antigravity_security_command(
            binary=resolved_binary,
            agent_name=agent_name,
            schema_path=schema_path,
            timeout_ms=timeout_ms,
            model=model,
        )
        try:
            completed = _run_antigravity_process(
                command,
                cwd=str(tmp_dir),
                env=dict(env),
                timeout=timeout_ms / 1000,
            )
        except subprocess.TimeoutExpired as exc:
            return AntigravitySecurityRun(
                command=tuple(command),
                exit_code=1,
                elapsed_seconds=time.monotonic() - start,
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr),
                error=f"{_FAILED} timed out after {timeout_ms} ms.",
            )
        except OSError as exc:
            return AntigravitySecurityRun(
                command=tuple(command),
                exit_code=1,
                elapsed_seconds=time.monotonic() - start,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc.strerror or ''}".strip(),
                error=(
                    f"{_FAILED} could not start `{command[0]}`: "
                    f"{exc.strerror or type(exc).__name__}."
                ),
                start_failure="launch",
            )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    structured_output, error, start_failure = parse_antigravity_security_output(
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return AntigravitySecurityRun(
        command=tuple(command),
        exit_code=completed.returncode,
        elapsed_seconds=time.monotonic() - start,
        stdout=stdout,
        stderr=stderr,
        structured_output=structured_output,
        error=error,
        start_failure=start_failure,
    )


def parse_antigravity_security_output(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> tuple[dict[str, object] | None, str | None, str | None]:
    """Return ``(structured_output, error, start_failure)`` for one agy run.

    Exactly one of ``structured_output`` and ``error`` is set. Error text
    never quotes stdout or stderr; only fixed text, line numbers, and the
    exit code are named.
    """
    if find_antigravity_auth_failure(stderr) is not None:
        return (
            None,
            f"{_FAILED} failed: agy is not authenticated (sign in by running "
            "`agy`).",
            "auth",
        )
    if find_permission_soft_deny(stderr) is not None:
        return (
            None,
            f"{_FAILED} failed: agy auto-denied a tool permission; the security "
            "reviewer allows no shell tools.",
            None,
        )
    if returncode != 0:
        return None, f"{_FAILED} exited with code {returncode}.", None

    results: list[dict[str, object]] = []
    for line_number, line in enumerate(stdout.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if results:
            return (
                None,
                f"{_MALFORMED} failed: stdout line {line_number} follows the "
                "result event.",
                None,
            )
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return (
                None,
                f"{_MALFORMED} failed: stdout line {line_number} is not JSON.",
                None,
            )
        if not isinstance(event, dict):
            return (
                None,
                f"{_MALFORMED} failed: stdout line {line_number} is not a JSON "
                "object.",
                None,
            )
        name, payload = _unwrap_stream_event(event)
        if name not in _ALLOWED_EVENTS:
            return (
                None,
                f"{_MALFORMED} failed: stdout line {line_number} is not an "
                "init, step_update, or result event.",
                None,
            )
        if name == "step_update":
            step_error = _step_error(payload, line_number)
            if step_error is not None:
                return None, step_error, None
        elif name == "result":
            results.append(payload)

    if len(results) != 1:
        return None, f"{_MALFORMED} failed: agy printed no result event.", None
    result = results[0]
    if result.get("status") != ANTIGRAVITY_SECURITY_SUCCESS_STATUS:
        # The status value is not quoted: it is agy output.
        return (
            None,
            f"{_FAILED} failed: result status is not "
            f"{ANTIGRAVITY_SECURITY_SUCCESS_STATUS}.",
            None,
        )
    # `response` holds the raw finish arguments; it is never read, not even
    # as a fallback.
    structured_output = result.get("structured_output")
    if not isinstance(structured_output, dict):
        return (
            None,
            f"{_MALFORMED} failed: the result has no structured_output object.",
            None,
        )
    if set(structured_output) != ANTIGRAVITY_SECURITY_VERDICT_KEYS:
        return (
            None,
            f"{_MALFORMED} failed: structured_output keys are not exactly "
            "verdict, summary, and findings.",
            None,
        )
    return structured_output, None, None


def _step_error(payload: Mapping[str, object], line_number: int) -> str | None:
    # The type checks come first: a list or object value is unhashable and
    # would raise TypeError on frozenset membership instead of failing closed.
    step_type = payload.get("step_type")
    if (
        not isinstance(step_type, str)
        or step_type not in ANTIGRAVITY_SECURITY_ALLOWED_STEP_TYPES
    ):
        return (
            f"{_FAILED} failed: stdout line {line_number} is a step other than "
            "user_input, agent_response, finish, or an allowed tool."
        )
    tool_name = payload.get("tool_name")
    if step_type == "tool" and (
        not isinstance(tool_name, str) or tool_name not in _ALLOWED_TOOLS
    ):
        return (
            f"{_FAILED} failed: stdout line {line_number} is a tool step whose "
            "tool is missing or outside the reviewer tool list."
        )
    return None


def reviewer_temp_base(repo_root: Path) -> Path | None:
    """Return the resolved system temp directory, or None if it is in the repo.

    ``tempfile.gettempdir()`` honors ``TMPDIR``, ``TEMP``, and ``TMP``; a
    value inside the checkout would put the reviewer workspace, its diff and
    schema copies, and its agent file inside the repository.
    """
    base = Path(tempfile.gettempdir()).resolve()
    if _is_within(base, repo_root):
        return None
    return base


def _is_within(path: Path, repo_root: Path) -> bool:
    return Path(path).resolve().is_relative_to(Path(repo_root).resolve())


def _resolve_reviewer_binary(binary: str, repo_root: Path) -> str:
    # agy runs from the temp directory, so a repo-relative binary path is
    # resolved against the repository root first.
    path = Path(binary)
    if not path.is_absolute() and any(sep in binary for sep in ("\\", "/")):
        return str((Path(repo_root) / path).resolve())
    return _resolve_command_binary(binary)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


__all__ = [
    "ANTIGRAVITY_SECURITY_INSTRUCTION",
    "ANTIGRAVITY_SECURITY_TOOLS",
    "AntigravitySecurityRun",
    "antigravity_security_agent_file",
    "antigravity_security_env",
    "antigravity_security_model",
    "antigravity_security_suffix",
    "build_antigravity_security_command",
    "parse_antigravity_security_output",
    "run_antigravity_security_review",
]
