from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Callable, Literal

from llm_handoff.agents import invoke_claude_subagent


PushStatus = Literal["PUSHED", "FAILED", "UNKNOWN"]

LEDGER_UPDATER_PROMPT = (
    "Use the ledger-updater agent to update the project ledger. "
    "Read docs/handoff/HANDOFF.md for the completed epic details and audit verdict. "
    "Append a one-line entry to docs/COMPLETED_WORK_LEDGER.md. "
    "Update CLAUDE.md section 2 Current Status to reflect the epic closure. "
    "Rewrite docs/handoff/HANDOFF.md so YAML frontmatter routes the next cycle "
    "to Gemini-PE for the next campaign phase or to user if blocked; do not "
    "leave it routing to claude-ledger after the ledger entry is complete. "
    "Commit docs/COMPLETED_WORK_LEDGER.md, CLAUDE.md, and docs/handoff/HANDOFF.md "
    "atomically with a verbose AGENTS.md-compliant message and Co-Authored-By "
    "trailer, then push main to origin per AGENTS.md section 4.6 (ledger "
    "maintainer is authorized to push at epic boundary). "
    "Return ONLY this exact machine-readable format, one field per line: "
    "LEDGER UPDATED: YES or NO; "
    "CLAUDE.MD UPDATED: YES or NO; "
    "HANDOFF.MD REWRITTEN: YES or NO; "
    "EPIC CLOSED: <epic name>; "
    "NEXT EPIC (routed to Gemini-PE): <epic name or None>; "
    "AUDIT SHA: <full or short sha>; "
    "COMMIT SHA: <single full or short sha for the ledger/CLAUDE.md/HANDOFF.md commit>; "
    "PUSH RESULT: PUSHED or FAILED (optional detail); "
    "CHANGES MADE: followed by dash-prefixed bullet lines. "
    "If follow-up patch commits are created, list them under CHANGES MADE, not on "
    "the COMMIT SHA line. "
    "Do not return prose, explanations, markdown headings, or conversational text."
)

_YES_NO_LINE_RE = r"(?im)^{label}:\s*(YES|NO)(?:\s*\((.+)\))?\s*$"
_EPIC_CLOSED_RE = re.compile(r"(?im)^EPIC CLOSED:\s*(.+)\s*$")
_NEXT_EPIC_RE = re.compile(r"(?im)^NEXT EPIC \(routed to Gemini-PE\):\s*(.+)\s*$")
_AUDIT_SHA_LINE_RE = re.compile(r"(?im)^AUDIT SHA:\s*(.+)\s*$")
_COMMIT_SHA_LINE_RE = re.compile(r"(?im)^COMMIT SHA:\s*(.+)\s*$")
_PUSH_RESULT_RE = re.compile(
    r"(?im)^PUSH RESULT:\s*(PUSHED|FAILED)(?:\s*\((.+)\))?\s*$"
)
_CHANGES_MADE_RE = re.compile(r"(?ims)^CHANGES MADE:\s*(.+)$")
_SHA_RE = re.compile(r"(?i)\b[0-9a-f]{7,40}\b")

logger = logging.getLogger(__name__)
LogFn = Callable[[str, str], None]


@dataclass(frozen=True)
class EpicCloseResult:
    subagent_exit_code: int
    stdout: str
    stderr: str
    parse_error: str | None = None
    ledger_updated: bool = False
    claude_md_updated: bool = False
    handoff_rewritten: bool = False
    epic_closed: str | None = None
    next_epic: str | None = None
    audit_sha: str | None = None
    commit_sha: str | None = None
    push_status: PushStatus = "UNKNOWN"
    push_detail: str | None = None
    changes_made: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ParsedLedgerOutput:
    ledger_updated: bool
    claude_md_updated: bool
    handoff_rewritten: bool
    epic_closed: str
    next_epic: str
    audit_sha: str
    commit_sha: str
    push_status: PushStatus
    push_detail: str | None
    changes_made: tuple[str, ...]


def run_epic_close(*, log: LogFn | None = None) -> EpicCloseResult:
    subagent_result = invoke_claude_subagent(
        subagent_name="ledger-updater",
        prompt=LEDGER_UPDATER_PROMPT,
        log=log,
    )
    _emit(
        log,
        "AGENT",
        f"Subagent ledger-updater exited with code {subagent_result.exit_code}",
    )

    if subagent_result.exit_code != 0:
        _emit(
            log,
            "ERROR",
            f"ledger-updater failed with exit code {subagent_result.exit_code}.",
        )
        if subagent_result.stderr:
            _emit(
                log,
                "WARN",
                f"ledger-updater stderr: {subagent_result.stderr.strip()}",
            )
        return EpicCloseResult(
            subagent_exit_code=subagent_result.exit_code,
            stdout=subagent_result.stdout,
            stderr=subagent_result.stderr,
        )

    try:
        parsed_output = _parse_subagent_output(subagent_result.stdout)
    except ValueError as exc:
        parse_error = str(exc)
        _emit(
            log, "ERROR", f"ledger-updater returned unparseable output: {parse_error}"
        )
        if subagent_result.stdout.strip():
            _emit(
                log,
                "WARN",
                "ledger-updater stdout (first 500 chars): "
                f"{subagent_result.stdout.strip()[:500]}",
            )
        else:
            _emit(log, "WARN", "ledger-updater returned no stdout.")
        if subagent_result.stderr.strip():
            _emit(
                log,
                "WARN",
                "ledger-updater stderr (first 500 chars): "
                f"{subagent_result.stderr.strip()[:500]}",
            )
        return EpicCloseResult(
            subagent_exit_code=1,
            stdout=subagent_result.stdout,
            stderr=subagent_result.stderr,
            parse_error=parse_error,
        )

    if parsed_output.push_status == "FAILED":
        _emit(
            log,
            "ERROR",
            "ledger-updater reported push failed: "
            f"{parsed_output.push_detail or 'no detail provided.'}",
        )
    if not parsed_output.handoff_rewritten:
        _emit(
            log,
            "WARN",
            "ledger-updater reported HANDOFF.MD REWRITTEN: NO; dispatcher will "
            "treat repeated Epic-Close routing as stale and redirect to Gemini-PE.",
        )

    return EpicCloseResult(
        subagent_exit_code=subagent_result.exit_code,
        stdout=subagent_result.stdout,
        stderr=subagent_result.stderr,
        ledger_updated=parsed_output.ledger_updated,
        claude_md_updated=parsed_output.claude_md_updated,
        handoff_rewritten=parsed_output.handoff_rewritten,
        epic_closed=parsed_output.epic_closed,
        next_epic=parsed_output.next_epic,
        audit_sha=parsed_output.audit_sha,
        commit_sha=parsed_output.commit_sha,
        push_status=parsed_output.push_status,
        push_detail=parsed_output.push_detail,
        changes_made=parsed_output.changes_made,
    )


def _parse_subagent_output(output: str) -> _ParsedLedgerOutput:
    return _ParsedLedgerOutput(
        ledger_updated=_parse_yes_no_field(output, "LEDGER UPDATED"),
        claude_md_updated=_parse_yes_no_field(output, "CLAUDE.MD UPDATED"),
        handoff_rewritten=_parse_yes_no_field(output, "HANDOFF.MD REWRITTEN"),
        epic_closed=_require_match(_EPIC_CLOSED_RE, output, "EPIC CLOSED").group(1),
        next_epic=_require_match(_NEXT_EPIC_RE, output, "NEXT EPIC").group(1),
        audit_sha=_parse_audit_sha(output),
        commit_sha=_parse_commit_sha(output),
        push_status=_require_match(_PUSH_RESULT_RE, output, "PUSH RESULT").group(1),
        push_detail=_strip_or_none(
            _require_match(_PUSH_RESULT_RE, output, "PUSH RESULT").group(2)
        ),
        changes_made=_parse_changes_made(output),
    )


def _parse_yes_no_field(output: str, label: str) -> bool:
    pattern = re.compile(_YES_NO_LINE_RE.format(label=re.escape(label)))
    match = pattern.search(output)
    if match is None:
        raise ValueError(f"Missing {label} line.")
    return match.group(1) == "YES"


def _require_match(pattern: re.Pattern[str], output: str, label: str) -> re.Match[str]:
    match = pattern.search(output)
    if match is None:
        raise ValueError(f"Missing {label} line.")
    return match


def _parse_audit_sha(output: str) -> str:
    audit_value = _require_match(_AUDIT_SHA_LINE_RE, output, "AUDIT SHA").group(1)
    matches = _SHA_RE.findall(audit_value)
    if not matches:
        raise ValueError("Missing AUDIT SHA line.")
    # Claude sometimes includes impl/tests context before the actual audit ref.
    return matches[-1]


def _parse_commit_sha(output: str) -> str:
    commit_value = _require_match(_COMMIT_SHA_LINE_RE, output, "COMMIT SHA").group(1)
    matches = _SHA_RE.findall(commit_value)
    if not matches:
        raise ValueError("Missing COMMIT SHA line.")
    # Keep the ledger commit as the machine-readable field even if prose lists
    # follow-up patch commits after it.
    return matches[0]


def _parse_changes_made(output: str) -> tuple[str, ...]:
    match = _CHANGES_MADE_RE.search(output)
    if match is None:
        return ()

    changes: list[str] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            changes.append(stripped[2:])
            continue
        break
    return tuple(changes)


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _emit(log: LogFn | None, level: str, message: str) -> None:
    if log is not None:
        log(level, message)
        return

    if level == "WARN":
        logger.warning(message)
        return
    if level == "ERROR":
        logger.error(message)
        return
    logger.info(message)

