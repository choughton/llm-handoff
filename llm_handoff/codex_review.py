"""Run ``codex exec review`` locally for the ``codex-local`` audit reviewer.

``audit.reviewers: [quality, security, codex-local]`` adds the Codex CLI's
own review command to the read-only audit fan-out, so a repository the
ChatGPT Codex connector cannot read still gets a Codex review, in every
publish mode. The audit runs ``codex exec review --base <scope base SHA>``
in the repository, read-only, with HEAD at the review scope's head commit.

``codex exec review`` ignores ``--output-schema`` and always ends with the
same text layout: an overall explanation, then (when there are findings) a
``Full review comments:`` or ``Review comment:`` line followed by one entry
per finding, with an em dash before its location::

    - [P2] Restore the empty-input guard in mean <em dash> /repo/src/stats.py:2-2
      Calling `mean([])` now raises `ZeroDivisionError` ...

A message that names review comments but yields no parsable finding is an
error, never a clean review.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re

from llm_handoff import config
from llm_handoff.agent_process import _run_command_streaming, build_agent_env
from llm_handoff.failover import classify_fatal_failure
from llm_handoff.redaction import redact_stderr_summary


# Where the review's final message is written; ``logs/`` is ignored by Git.
LAST_MESSAGE_PATH = Path("logs") / "dispatch" / "codex-review" / "last-message.txt"
_COMMENTS_HEADING_RE = re.compile(
    r"(?im)^\s*(?:full review comments|review comments?)\s*:\s*$"
)
# "- [P2] Title <em dash> path:2-2", with the location optional.
_FINDING_RE = re.compile(
    r"^- \[P(\d)\]\s+(.*?)(?:\s+\u2014\s+(.+?):(\d+)(?:-(\d+))?)?\s*$"
)


@dataclass(frozen=True)
class LocalFinding:
    priority: int
    title: str
    body: str
    path: str | None = None
    line: int | None = None

    def location(self) -> str:
        if self.path and self.line:
            return f"{self.path}:{self.line}"
        return self.path or "(no file)"


@dataclass(frozen=True)
class LocalReview:
    """A finished review: the overall explanation and every finding."""

    explanation: str
    findings: tuple[LocalFinding, ...] = ()


@dataclass(frozen=True)
class LocalReviewRun:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    review: LocalReview | None = None
    # Why the review did not finish; empty when ``review`` is set.
    error: str = ""
    usage_limited: bool = False
    timed_out: bool = False


def build_review_command(
    command_prefix: Sequence[str], base_ref: str, last_message_path: Path
) -> list[str]:
    return [
        *command_prefix,
        "exec",
        "--sandbox",
        "read-only",
        "review",
        "--base",
        base_ref,
        "-c",
        f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
        "--json",
        "--ephemeral",
        "--output-last-message",
        str(last_message_path),
    ]


def parse_review_message(text: str, repo_root: Path) -> LocalReview:
    """Read the final message of ``codex exec review``.

    Raises ``ValueError`` when the message is empty, or names review comments
    but none of them parses.
    """

    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        raise ValueError("codex exec review wrote no final message")
    heading = _COMMENTS_HEADING_RE.search(normalized)
    explanation = normalized[: heading.start()].strip() if heading else normalized
    findings: list[LocalFinding] = []
    if heading is not None:
        current: dict[str, object] | None = None
        body: list[str] = []

        def close() -> None:
            if current is not None:
                findings.append(
                    LocalFinding(body=_dedent(body), **current)  # type: ignore[arg-type]
                )

        for line in normalized[heading.end() :].split("\n"):
            match = _FINDING_RE.match(line)
            if match is None:
                if current is not None:
                    body.append(line)
                continue
            close()
            priority, title, path, start, end = match.groups()
            current = {
                "priority": int(priority),
                "title": title.strip(),
                "path": _repo_path(path, repo_root) if path else None,
                "line": int(end or start) if start else None,
            }
            body = []
        close()
        if not findings:
            raise ValueError(
                "codex exec review listed review comments, but none of them "
                "could be read"
            )
    return LocalReview(explanation=explanation, findings=tuple(findings))


def run_codex_review(
    *,
    command_prefix: Sequence[str],
    repo_root: Path,
    base_ref: str,
    timeout_ms: int,
    env: dict[str, str] | None = None,
    stop_on_chat_takeover: bool = True,
) -> LocalReviewRun:
    """Run one read-only ``codex exec review`` of HEAD against ``base_ref``.

    An empty ``command_prefix`` runs the configured Codex binary. ``env``
    defaults to the allowlisted environment of a Codex role without an API
    key. A chat takeover request stops the run, as it stops the other audit
    reviewers, unless ``stop_on_chat_takeover`` is False.
    """

    command_prefix = tuple(command_prefix) or (config.CODEX_BINARY,)
    last_message_path = (repo_root / LAST_MESSAGE_PATH).resolve()
    last_message_path.parent.mkdir(parents=True, exist_ok=True)
    last_message_path.unlink(missing_ok=True)
    command = build_review_command(command_prefix, base_ref, last_message_path)
    if env is None:
        env = build_agent_env(config.AgentConfig(provider="codex"))
    result = _run_command_streaming(
        command,
        cwd=repo_root,
        timeout_ms=timeout_ms,
        env=env,
        stop_on_chat_takeover=stop_on_chat_takeover,
    )
    outcome = {
        "command": tuple(command),
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if result.exit_code != 0:
        reason = classify_fatal_failure(
            exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr
        )
        summary = redact_stderr_summary(result.stderr) if result.stderr.strip() else ""
        return LocalReviewRun(
            **outcome,
            error=(
                f"codex exec review exited with code {result.exit_code}"
                + (f": {summary}" if summary else "")
            ),
            usage_limited=reason == "rate_limit",
            timed_out=reason == "timeout",
        )
    try:
        text = last_message_path.read_text(encoding="utf-8")
    except OSError:
        text = _last_agent_message(result.stdout)
    try:
        return LocalReviewRun(**outcome, review=parse_review_message(text, repo_root))
    except ValueError as exc:
        return LocalReviewRun(**outcome, error=str(exc))


def _last_agent_message(stdout: str) -> str:
    message = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            message = str(item.get("text") or "")
    return message


def _repo_path(path: str, repo_root: Path) -> str:
    """``path`` relative to ``repo_root`` with forward slashes, when inside it."""

    candidate = path.strip().replace("\\", "/")
    root = repo_root.resolve().as_posix().rstrip("/") + "/"
    # Windows paths compare without case; Codex may print another drive case.
    if candidate.lower().startswith(root.lower()):
        return candidate[len(root) :]
    return candidate


def _dedent(lines: list[str]) -> str:
    return "\n".join(
        line[2:] if line.startswith("  ") else line for line in lines
    ).strip()
