from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from typing import Callable, Literal

from llm_handoff.agent_types import HandoffStatus
from llm_handoff.agent_roles import FailoverGuard, invoke_support_role
from llm_handoff.redaction import redact_secrets
from llm_handoff.config import (
    DEFAULT_COMPLETED_WORK_PATH,
    DEFAULT_HANDOFF_PATH,
    DEFAULT_PROJECT_STATE_PATH,
    DispatchConfig,
)


PushStatus = Literal["PUSHED", "FAILED", "SKIPPED", "UNKNOWN"]

LEDGER_UPDATER_PROMPT_TEMPLATE = (
    "Use the finalizer or ledger-updater agent to update the project state. "
    "Read {handoff_path} for the completed scope and validation verdict. "
    "Update {project_state_path} with the next state and keep it to current "
    "work: move the closed epic's section (its intro, Fit line, and story list) "
    "and the status notes about it from {project_state_path} to "
    "{completed_work_path}, and append one ledger line for the closed scope, "
    "with its durable commit references, to the ledger in "
    "{completed_work_path}. Create {completed_work_path} with a ledger section "
    "if it does not exist. "
    "Also add every story that {handoff_path} names in remaining_stories and "
    "{project_state_path} does not list yet, so planned work outlives the handoff. "
    "Rewrite {handoff_path} so YAML frontmatter routes the next cycle "
    "to planner for the next phase or to user if blocked; do not leave it "
    "routing to finalizer after the state update is complete. "
    "{commit_instruction} Do not push unless "
    "the repository instructions explicitly allow this finalizer to push. "
    "Return ONLY this exact machine-readable format, one field per line: "
    "PROJECT STATE UPDATED: YES or NO; "
    "HANDOFF.MD REWRITTEN: YES or NO; "
    "SCOPE CLOSED: <scope name>; "
    "NEXT ROUTE: <planner, user, or another supported next_agent>; "
    "AUDIT SHA: <full or short sha>; "
    "COMMIT SHA: <single full or short sha for the {project_state_path} commit>; "
    "PUSH RESULT: SKIPPED, PUSHED, or FAILED (optional detail); "
    "CHANGES MADE: followed by dash-prefixed bullet lines. "
    "If follow-up patch commits are created, list them under CHANGES MADE, not on "
    "the COMMIT SHA line. "
    "Do not return prose, explanations, markdown headings, or conversational text."
)


_AGENT_COMMIT_INSTRUCTION = (
    "Commit {project_state_path}, {completed_work_path}, and {handoff_path} "
    "atomically with a clear message and any required Co-Authored-By trailer."
)
# In dispatcher commit mode the handoff is live dispatch state that stays out
# of commits; a committed copy lands on the base branch and in the next story
# branch's pull request.
_DISPATCHER_COMMIT_INSTRUCTION = (
    "Dispatcher commit mode is on: commit only {project_state_path} and "
    "{completed_work_path}, in one commit with a clear message and any required "
    "Co-Authored-By trailer, and leave {handoff_path} uncommitted even if the "
    "repository protocol asks you to commit it. If neither file changed, make no "
    "commit; then keep the audited SHA as scope_sha and report it on the COMMIT "
    "SHA line."
)


def ledger_updater_prompt(
    handoff_path: Path = DEFAULT_HANDOFF_PATH,
    commit_mode: str = "agent",
    project_state_path: Path = DEFAULT_PROJECT_STATE_PATH,
    completed_work_path: Path = DEFAULT_COMPLETED_WORK_PATH,
) -> str:
    # The finalizer prompt names the handoff, project state, and completed
    # work files by their configured relative paths so non-default layouts
    # get correct instructions too.
    path = Path(handoff_path).as_posix()
    state_path = Path(project_state_path).as_posix()
    completed_path = Path(completed_work_path).as_posix()
    commit_instruction = (
        _DISPATCHER_COMMIT_INSTRUCTION
        if commit_mode == "dispatcher"
        else _AGENT_COMMIT_INSTRUCTION
    )
    return LEDGER_UPDATER_PROMPT_TEMPLATE.format(
        handoff_path=path,
        project_state_path=state_path,
        completed_work_path=completed_path,
        commit_instruction=commit_instruction.format(
            handoff_path=path,
            project_state_path=state_path,
            completed_work_path=completed_path,
        ),
    )


LEDGER_UPDATER_PROMPT = ledger_updater_prompt()

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
_BOUNCE_COUNTS: dict[str, int] = {}


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
    # The finalizer's provider session id, or None; kept on every returned
    # result so ``@finalizer`` chat can resume the finished session.
    session_id: str | None = None

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


def bounce_count(story_id: str) -> int:
    return _BOUNCE_COUNTS.get(story_id, 0)


def increment_bounce(story_id: str, *, state_path: Path | None = None) -> int:
    # Memory is updated before the file write so a failed write never loses
    # the in-process count.
    _BOUNCE_COUNTS[story_id] = bounce_count(story_id) + 1
    _persist_bounce_count(state_path, story_id, _BOUNCE_COUNTS[story_id])
    return _BOUNCE_COUNTS[story_id]


def reset_bounce(story_id: str, *, state_path: Path | None = None) -> None:
    _BOUNCE_COUNTS.pop(story_id, None)
    _persist_bounce_count(state_path, story_id, None)


def record_status_transition(
    story_id: str | None,
    status: HandoffStatus | None,
    *,
    state_path: Path | None = None,
) -> int:
    """Update the story's bounce count for a post-dispatch status.

    When ``state_path`` is given the change is also written to that JSON file
    so the three-fix circuit breaker survives a dispatcher restart; an
    ``OSError`` from that write propagates to the caller.
    """
    if not story_id or status is None:
        return 0
    if status == HandoffStatus.VERIFIED_FAIL:
        return increment_bounce(story_id, state_path=state_path)
    if status in {
        HandoffStatus.VERIFIED_PASS,
        HandoffStatus.ESCALATE_TO_USER,
    }:
        reset_bounce(story_id, state_path=state_path)
    return bounce_count(story_id)


def seed_bounce_counts(state_path: Path) -> dict[str, int]:
    """Load persisted bounce counts into memory and return what was loaded.

    Persisted values override in-memory values for the stories they name;
    stories only known in memory are left untouched. A missing or unreadable
    file loads nothing.
    """
    persisted = _read_bounce_state(state_path)
    _BOUNCE_COUNTS.update(persisted)
    return dict(persisted)


def _read_bounce_state(state_path: Path) -> dict[str, int]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    counts = payload.get("bounce_counts") if isinstance(payload, dict) else None
    if not isinstance(counts, dict):
        return {}
    return {
        story_id: count
        for story_id, count in counts.items()
        if isinstance(story_id, str)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
    }


def _persist_bounce_count(
    state_path: Path | None,
    story_id: str,
    count: int | None,
) -> None:
    if state_path is None:
        return
    counts = _read_bounce_state(state_path)
    if count is None:
        counts.pop(story_id, None)
    else:
        counts[story_id] = count
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "bounce_counts": dict(sorted(counts.items()))}
    temporary_path = state_path.with_name(f"{state_path.name}.tmp")
    temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(state_path)


def run_epic_close(
    *,
    config: DispatchConfig | None = None,
    log: LogFn | None = None,
    failover_guard: FailoverGuard | None = None,
) -> EpicCloseResult:
    kwargs = {}
    prompt = LEDGER_UPDATER_PROMPT
    if config is not None:
        prompt = ledger_updater_prompt(
            config.handoff_path,
            config.commit_mode,
            config.project_state_path,
            config.completed_work_path,
        )
        kwargs = {
            "role": "finalizer",
            "handoff_path": config.handoff_full_path,
            "agent_config": config.agents["finalizer"],
            "repo_root": config.repo_root,
        }
        if failover_guard is not None:
            kwargs["failover_guard"] = failover_guard
    subagent_result = invoke_support_role(
        "ledger-updater",
        prompt,
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
                "ledger-updater stderr: "
                f"{redact_secrets(subagent_result.stderr.strip())}",
            )
        return EpicCloseResult(
            subagent_exit_code=subagent_result.exit_code,
            stdout=subagent_result.stdout,
            stderr=subagent_result.stderr,
            session_id=subagent_result.session_id,
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
                f"{redact_secrets(subagent_result.stderr.strip())[:500]}",
            )
        return EpicCloseResult(
            subagent_exit_code=1,
            stdout=subagent_result.stdout,
            stderr=subagent_result.stderr,
            parse_error=parse_error,
            session_id=subagent_result.session_id,
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
        session_id=subagent_result.session_id,
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
