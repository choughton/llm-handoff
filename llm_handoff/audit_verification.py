"""Dispatcher verification for audits the work-claim gate never checked.

The work-claim gate runs the Work Packet's Verification command outside any
agent sandbox, but only on an implementer's ``ready_for_review`` claim. An
epic close, or any audit whose review range has no committed changes, reaches
the auditor without that check, and a sandboxed reviewer may not be able to
run the command at all (for example pytest's per-user temp directory under
the Codex Windows sandbox). For those audits the dispatcher runs the command
itself, on the audit commit, before the reviewers start.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from llm_handoff.router import HandoffRouting
from llm_handoff.work_claim import (
    DISPATCHER_VERIFICATION_HEADING,
    VERIFICATION_FAILED_PREFIX,
    VERIFICATION_NOT_ALLOWED_PREFIX,
    WORK_PACKET_UNPARSEABLE_PREFIX,
    CommandRun,
    git_head_and_dirty_files,
    has_passing_dispatcher_verification,
    normalize_repo_path,
    parse_work_packet_verification_commands,
    recorded_verification_commands,
    run_shell_command,
    verification_command_allowed,
)

LogFn = Callable[[str, str], None]


@dataclass(frozen=True)
class PreAuditVerification:
    """The outcome of the dispatcher's pre-audit Verification run."""

    passed: bool
    head_sha: str
    trigger: str
    runs: tuple[CommandRun, ...] = ()
    failed_prefix: str | None = None
    detail: str = ""


def pre_audit_trigger(
    frontmatter: HandoffRouting | None, scope_files: tuple[str, ...]
) -> str | None:
    """Name why the audit needs dispatcher verification, or None."""

    if frontmatter is not None and frontmatter.close_type == "epic":
        return "epic close"
    if not scope_files:
        return "empty review range"
    return None


def run_pre_audit_verification(
    *,
    repo_root: Path,
    handoff_content: str,
    handoff_rel_path: str,
    frontmatter: HandoffRouting | None,
    head_sha: str,
    scope_files: tuple[str, ...],
    timeout_seconds: int,
    env_passthrough: tuple[str, ...] = (),
    allowed_prefixes: tuple[str, ...] = (),
    log: LogFn | None = None,
) -> PreAuditVerification | None:
    """Run the Work Packet's Verification commands before an audit.

    Returns None when the step does not apply: the audit is neither an epic
    close nor an empty review range, the handoff already has a passing
    ``## Dispatcher Verification`` section for ``head_sha`` that lists the
    same commands, there is no Work Packet Verification command, or the
    working tree is not a clean checkout of ``head_sha`` (the handoff itself
    may be dirty). A pass is only credited to the commit when no other file
    differs from it. The commands run the way the work-claim gate runs them:
    allowlist checked first, the allowlisted gate environment, and a bounded
    timeout.
    """

    def _emit(level: str, message: str) -> None:
        if log is not None:
            log(level, message)

    trigger = pre_audit_trigger(frontmatter, scope_files)
    if trigger is None:
        return None
    parsed = parse_work_packet_verification_commands(handoff_content)
    if parsed is None:
        _emit(
            "WARN",
            f"Dispatcher verification skipped for this {trigger}: the handoff "
            "has no Work Packet Verification command, so the quality reviewer "
            "runs its own checks.",
        )
        return None
    commands, prose = parsed
    if has_passing_dispatcher_verification(handoff_content, head_sha):
        if commands and recorded_verification_commands(handoff_content) == commands:
            _emit(
                "INFO",
                f"Dispatcher verification: the handoff already records a PASS "
                f"for {head_sha[:12]} with the same commands; not running it "
                "again.",
            )
            return None
        _emit(
            "INFO",
            f"Dispatcher verification: the recorded PASS for {head_sha[:12]} "
            "lists different commands than the Work Packet; running them again.",
        )
    current_head, dirty_files = git_head_and_dirty_files(repo_root)
    if current_head is None or current_head.lower() != head_sha.lower():
        _emit(
            "WARN",
            f"Dispatcher verification skipped for this {trigger}: the working "
            f"tree is at {(current_head or 'unknown')[:12]}, not the audit "
            f"commit {head_sha[:12]}.",
        )
        return None
    handoff_entry = normalize_repo_path(handoff_rel_path)
    other_dirty = (
        None
        if dirty_files is None
        else [
            path for path in dirty_files if normalize_repo_path(path) != handoff_entry
        ]
    )
    if other_dirty is None or other_dirty:
        shown = (
            "git status failed"
            if other_dirty is None
            else "uncommitted changes in " + ", ".join(f"`{p}`" for p in other_dirty[:5])
            + (f" (+{len(other_dirty) - 5} more)" if len(other_dirty) > 5 else "")
        )
        _emit(
            "WARN",
            f"Dispatcher verification skipped for this {trigger}: the working "
            f"tree is not a clean checkout of {head_sha[:12]} ({shown}), so a "
            "pass could not be credited to the commit.",
        )
        return None

    if prose or not commands:
        detail = (
            "the Work Packet Verification command has lines that are not "
            "commands: " + ", ".join(f"`{line}`" for line in prose)
            if prose
            else "the Work Packet Verification command is empty"
        )
        return PreAuditVerification(
            passed=False,
            head_sha=head_sha,
            trigger=trigger,
            failed_prefix=WORK_PACKET_UNPARSEABLE_PREFIX,
            detail=detail + "; no command ran.",
        )
    # Check every command before running any, as the work-claim gate does.
    for command in commands:
        reason = verification_command_allowed(command, allowed_prefixes)
        if reason is not None:
            return PreAuditVerification(
                passed=False,
                head_sha=head_sha,
                trigger=trigger,
                failed_prefix=VERIFICATION_NOT_ALLOWED_PREFIX,
                detail=f"{reason} No Verification command ran.",
            )

    runs: list[CommandRun] = []
    for command in commands:
        _emit(
            "INFO",
            f"Dispatcher verification ({trigger}): running Work Packet "
            f"Verification command before the audit: {command}",
        )
        run = run_shell_command(
            command,
            cwd=repo_root,
            timeout_seconds=timeout_seconds,
            env_passthrough=env_passthrough,
        )
        runs.append(run)
        _emit(
            "INFO" if run.succeeded else "ERROR",
            f"Dispatcher verification: `{run.command}` {run.describe_outcome()}.",
        )
        if not run.succeeded:
            return PreAuditVerification(
                passed=False,
                head_sha=head_sha,
                trigger=trigger,
                runs=tuple(runs),
                failed_prefix=VERIFICATION_FAILED_PREFIX,
                detail=f"`{run.command}` {run.describe_outcome()}.",
            )
    return PreAuditVerification(
        passed=True, head_sha=head_sha, trigger=trigger, runs=tuple(runs)
    )


def passed_pre_audit_section(result: PreAuditVerification) -> str:
    """The passing ``## Dispatcher Verification`` section for the auditor."""

    lines = [DISPATCHER_VERIFICATION_HEADING, ""]
    lines.append(
        f"The work-claim gate does not check this audit ({result.trigger}), so "
        "the dispatcher ran the Work Packet's Verification command on the "
        "audit commit itself, outside any agent sandbox, before dispatching "
        "the reviewers. It passed; the auditor does not need to run it again."
    )
    lines.append("")
    lines.append("- **Verdict:** PASS")
    lines.append(f"- **Verified commit:** `{result.head_sha}`")
    for run in result.runs:
        lines.append(
            f"- **Verification command:** `{run.command}` ({run.describe_outcome()})"
        )
    return "\n".join(lines).rstrip() + "\n"


def failed_pre_audit_section(result: PreAuditVerification) -> str:
    """The failing ``## Dispatcher Verification`` section for the planner."""

    lines = [DISPATCHER_VERIFICATION_HEADING, ""]
    lines.append(
        f"The work-claim gate does not check this audit ({result.trigger}), so "
        "the dispatcher ran the Work Packet's Verification command on the "
        "audit commit itself, outside any agent sandbox, before dispatching "
        "the reviewers. It failed, and no reviewer ran. No implementer claim "
        "is under review, so this routes to the planner: decide whether the "
        "failure is a regression that needs a fix story, a Verification "
        "command to correct, or an environment problem for the user, then "
        "re-issue the audit. Do not point `--basetemp`, `TMP`, or `TEMP` "
        "inside the repository to work around a temp directory error."
    )
    lines.append("")
    lines.append("- **Verdict:** FAIL")
    lines.append(f"- **Failed check:** {result.failed_prefix}")
    lines.append(f"- **Audit commit:** `{result.head_sha}`")
    lines.append(f"- **Detail:** {result.detail}")
    if result.runs:
        lines.extend(["", "### Verification Output", ""])
        for run in result.runs:
            lines.append(f"Command: `{run.command}` ({run.describe_outcome()})")
            lines.append("")
            lines.append("````text")
            lines.append(run.output_tail or "(no output)")
            lines.append("````")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
