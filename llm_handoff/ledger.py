from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Callable, Literal

from llm_handoff.agent_roles import invoke_support_role
from llm_handoff.config import DispatchConfig


PushStatus = Literal["PUSHED", "FAILED", "SKIPPED", "UNKNOWN"]

LEDGER_UPDATER_PROMPT = (
    "Use the finalizer or ledger-updater agent to update the project state. "
    "Read docs/handoff/HANDOFF.md for the completed scope and validation verdict. "
    "Update PROJECT_STATE.md with the completed scope, next state, and durable commit references. "
    "Rewrite docs/handoff/HANDOFF.md so YAML frontmatter routes the next cycle "
    "to planner for the next phase or to user if blocked; do not leave it "
    "routing to finalizer after the state update is complete. "
    "Commit PROJECT_STATE.md and docs/handoff/HANDOFF.md atomically with a "
    "clear message and any required Co-Authored-By trailer. Do not push unless "
    "the repository instructions explicitly allow this finalizer to push. "
    "Return ONLY this exact machine-readable format, one field per line: "
    "PROJECT STATE UPDATED: YES or NO; "
    "HANDOFF.MD REWRITTEN: YES or NO; "
    "SCOPE CLOSED: <scope name>; "
    "NEXT ROUTE: <planner, user, or another supported next_agent>; "
    "AUDIT SHA: <full or short sha>; "
    "COMMIT SHA: <single full or short sha for the PROJECT_STATE.md/HANDOFF.md commit>; "
    "PUSH RESULT: SKIPPED, PUSHED, or FAILED (optional detail); "
    "CHANGES MADE: followed by dash-prefixed bullet lines. "
    "If follow-up patch commits are created, list them under CHANGES MADE, not on "
    "the COMMIT SHA line. "
    "Do not return prose, explanations, markdown headings, or conversational text."
)

_YES_NO_LINE_RE = r"(?im)^{label}:\s*(YES|NO)(?:\s*\((.+)\))?\s*$"
_SCOPE_CLOSED_RE = re.compile(r"(?im)^SCOPE CLOSED:\s*(.+)\s*$")
_EPIC_CLOSED_RE = re.compile(r"(?im)^EPIC CLOSED:\s*(.+)\s*$")
_NEXT_ROUTE_RE = re.compile(r"(?im)^NEXT ROUTE:\s*(.+)\s*$")
_NEXT_EPIC_RE = re.compile(r"(?im)^NEXT EPIC(?:\s*\([^)]+\))?:\s*(.+)\s*$")
_AUDIT_SHA_LINE_RE = re.compile(r"(?im)^AUDIT SHA:\s*(.+)\s*$")
_COMMIT_SHA_LINE_RE = re.compile(r"(?im)^COMMIT SHA:\s*(.+)\s*$")
_PUSH_RESULT_RE = re.compile(
    r"(?im)^PUSH RESULT:\s*(SKIPPED|PUSHED|FAILED)(?:\s*\((.+)\))?\s*$"
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
    project_state_updated: bool = False
    ledger_updated: bool | None = None
    handoff_rewritten: bool = False
    scope_closed: str | None = None
    epic_closed: str | None = None
    next_route: str | None = None
    next_epic: str | None = None
    audit_sha: str | None = None
    commit_sha: str | None = None
    push_status: PushStatus = "UNKNOWN"
    push_detail: str | None = None
    changes_made: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        state_updated = self.project_state_updated or bool(self.ledger_updated)
        object.__setattr__(self, "project_state_updated", state_updated)
        if self.ledger_updated is None:
            object.__setattr__(self, "ledger_updated", state_updated)
        if self.scope_closed is None and self.epic_closed is not None:
            object.__setattr__(self, "scope_closed", self.epic_closed)
        if self.epic_closed is None:
            object.__setattr__(self, "epic_closed", self.scope_closed)
        if self.next_route is None and self.next_epic is not None:
            object.__setattr__(self, "next_route", self.next_epic)
        if self.next_epic is None:
            object.__setattr__(self, "next_epic", self.next_route)


@dataclass(frozen=True)
class _ParsedLedgerOutput:
    project_state_updated: bool
    handoff_rewritten: bool
    scope_closed: str
    next_route: str
    audit_sha: str
    commit_sha: str
    push_status: PushStatus
    push_detail: str | None
    changes_made: tuple[str, ...]

    @property
    def ledger_updated(self) -> bool:
        return self.project_state_updated

    @property
    def epic_closed(self) -> str:
        return self.scope_closed

    @property
    def next_epic(self) -> str:
        return self.next_route


def run_epic_close(
    *,
    config: DispatchConfig | None = None,
    log: LogFn | None = None,
) -> EpicCloseResult:
    kwargs = {}
    if config is not None:
        kwargs = {
            "role": "finalizer",
            "handoff_path": config.handoff_full_path,
            "agent_config": config.agents["finalizer"],
        }
    subagent_result = invoke_support_role(
        "ledger-updater",
        LEDGER_UPDATER_PROMPT,
        log=log,
        **kwargs,
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
            "treat repeated finalizer routing as stale and redirect to the planner.",
        )

    return EpicCloseResult(
        subagent_exit_code=subagent_result.exit_code,
        stdout=subagent_result.stdout,
        stderr=subagent_result.stderr,
        project_state_updated=parsed_output.project_state_updated,
        handoff_rewritten=parsed_output.handoff_rewritten,
        scope_closed=parsed_output.scope_closed,
        next_route=parsed_output.next_route,
        audit_sha=parsed_output.audit_sha,
        commit_sha=parsed_output.commit_sha,
        push_status=parsed_output.push_status,
        push_detail=parsed_output.push_detail,
        changes_made=parsed_output.changes_made,
    )


def _parse_subagent_output(output: str) -> _ParsedLedgerOutput:
    return _ParsedLedgerOutput(
        project_state_updated=_parse_yes_no_field_any(
            output,
            (
                "PROJECT STATE UPDATED",
                "PROJECT_STATE.MD UPDATED",
                "LEDGER UPDATED",
            ),
            "PROJECT STATE UPDATED",
        ),
        handoff_rewritten=_parse_yes_no_field(output, "HANDOFF.MD REWRITTEN"),
        scope_closed=_require_match_any(
            (_SCOPE_CLOSED_RE, _EPIC_CLOSED_RE), output, "SCOPE CLOSED"
        ).group(1),
        next_route=_require_match_any(
            (_NEXT_ROUTE_RE, _NEXT_EPIC_RE), output, "NEXT ROUTE"
        ).group(1),
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


def _parse_yes_no_field_any(
    output: str,
    labels: tuple[str, ...],
    display_label: str,
) -> bool:
    for label in labels:
        pattern = re.compile(_YES_NO_LINE_RE.format(label=re.escape(label)))
        match = pattern.search(output)
        if match is not None:
            return match.group(1) == "YES"
    raise ValueError(f"Missing {display_label} line.")


def _require_match(pattern: re.Pattern[str], output: str, label: str) -> re.Match[str]:
    match = pattern.search(output)
    if match is None:
        raise ValueError(f"Missing {label} line.")
    return match


def _require_match_any(
    patterns: tuple[re.Pattern[str], ...],
    output: str,
    label: str,
) -> re.Match[str]:
    for pattern in patterns:
        match = pattern.search(output)
        if match is not None:
            return match
    raise ValueError(f"Missing {label} line.")


def _parse_audit_sha(output: str) -> str:
    audit_value = _require_match(_AUDIT_SHA_LINE_RE, output, "AUDIT SHA").group(1)
    matches = _SHA_RE.findall(audit_value)
    if not matches:
        raise ValueError("Missing AUDIT SHA line.")
    # Some providers include implementation/test context before the audit ref.
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
