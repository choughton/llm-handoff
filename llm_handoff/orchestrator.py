from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Literal, TextIO, cast

from llm_handoff.agent_providers import antigravity as antigravity_provider
from llm_handoff.agent_providers import claude as claude_provider
from llm_handoff.agent_providers import codex as codex_provider
from llm_handoff.agent_providers import gemini as gemini_provider
from llm_handoff.agent_types import HandoffStatus
from llm_handoff.agent_roles import (
    invoke_backend_role,
    invoke_frontend_role,
    invoke_planner_role,
    invoke_support_role,
)
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.audit_review import (
    AuditFanOutContext,
    describe_security_reviewer_model,
    run_audit_fan_out,
    security_reviewer_startup_error,
)
from llm_handoff.config import (
    BOUNCE_COUNT_STATE_PATH,
    AgentConfig,
    AgentRole,
    CLAUDE_DEFAULT_ALLOWED_TOOLS,
    DispatchConfig,
    NormalizerConfig,
)
from llm_handoff.ledger import (
    bounce_count,
    record_status_transition,
    run_epic_close,
    seed_bounce_counts,
)
from llm_handoff.handoff_normalizer import (
    CANONICAL_NEXT_AGENT_SET,
    normalize_handoff_next_agent_text,
    normalize_next_agent,
)
from llm_handoff.dispatcher_commit import (
    COMMIT_MODE_INSTRUCTION,
    DispatcherCommitResult,
    PLANNER_COMMIT_MODE_INSTRUCTION,
    commit_implementer_changes,
    git_position,
    undo_planner_handoff_commits,
    stamp_commit_sha,
)
from llm_handoff.operator_prompt import (
    HandoffChanged,
    OperatorInputReader,
    wait_for_operator_or_handoff_change,
)
from llm_handoff.provider_auth import auth_failure_messages, find_auth_failure
from llm_handoff.pr_review import ReviewGate
from llm_handoff.publish import (
    ensure_story_branch,
    publish_story,
    resume_paused_publish,
    save_review_responses,
    saved_story_base,
    settle_deferred_findings,
)
from llm_handoff.roles import normalize_next_agent_value
from llm_handoff.router import (
    HandoffFrontmatterError,
    RoutingDecision,
    RouteName,
    parse_handoff_frontmatter_text,
    repair_handoff_frontmatter_text,
    route as route_handoff,
)
from llm_handoff.text_io import read_dispatch_text
from llm_handoff.validator import (
    ValidationResult,
    parse_validation_output,
    validate_handoff,
)
from llm_handoff.work_claim import (
    ImplementerLeftovers,
    PreDispatchState,
    WorkClaimResult,
    bounce_route_for,
    build_bounce_handoff,
    capture_implementer_leftovers,
    capture_pre_dispatch_state,
    carry_over_key,
    claude_protected_scope_entries,
    git_changed_files,
    git_dirty_files,
    parse_work_packet_scope,
    path_is_in_scope,
    record_passed_verification,
    verify_work_claim,
)


# Prompt templates name the handoff file through {handoff_path} so the
# configured layout (the nested default or a flat HANDOFF.md) reaches
# every Claude-side agent; render them with _render_prompt.
AUDIT_PROMPT_TEMPLATE = (
    "Use the auditor agent to audit the work described in "
    "{handoff_path}. Read the repository instructions and relevant "
    "state files before judging the diff. Run the configured verification "
    "commands when available. Return a concise audit report, then update "
    "{handoff_path} with findings and a routing instruction for the "
    "next agent."
)

MISROUTE_PROMPT_TEMPLATE = (
    "Use the handoff-router agent to analyze {handoff_path}. The "
    "previous routing was flagged as a potential misroute. Determine the "
    "correct agent and write a corrected routing instruction to "
    "{handoff_path} with your reasoning."
)
HANDOFF_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate {handoff_path}. "
    "The dispatcher could not find a dispatchable routing instruction. "
    "Focus on why the handoff is not dispatchable, especially routing, SHA, "
    "ownership, and content requirements. Return ONLY the structured output format."
)
STALE_ROUTE_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate {handoff_path}. "
    "The dispatcher detected stale routing: HANDOFF.md still routes to "
    "{route}, and its content hash is unchanged since the previous cycle. "
    "Focus on why the prior cycle did not rewrite the handoff and what must "
    "change in routing, ownership, or content requirements. Return ONLY the "
    "structured output format."
)
EPIC_CLOSE_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate {handoff_path}. "
    "The dispatcher attempted finalizer, but the ledger-updater returned "
    "unparseable output: {parse_error}. "
    "Focus on whether the handoff was truly ready for epic close or should "
    "route to audit, planning, or escalation instead. Return ONLY the "
    "structured output format."
)
LOW_CONFIDENCE_ROUTE_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate {handoff_path}. "
    "The dispatcher found a {confidence}-confidence routing decision to "
    "{route}: {reasoning} "
    "Focus on whether the handoff is truly dispatchable or if the routing "
    "instruction needs to be clarified before dispatch. Return ONLY the "
    "structured output format."
)
PLANNER_SELF_LOOP_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate {handoff_path}. "
    "The dispatcher detected a planner self-loop: the planner just finished, "
    "and the updated HANDOFF.md still routes back to the planner. "
    "Focus on why the planner routed the handoff back to itself and what must "
    "change in routing, ownership, or content requirements. Return ONLY the "
    "structured output format."
)
AGENT_SELF_LOOP_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate {handoff_path}. "
    "The dispatcher detected a self-loop: {previous_agent} just finished, "
    "and the updated HANDOFF.md still routes back to the same agent. "
    "Focus on why the handoff routed work back to itself and what must "
    "change in routing, ownership, or content requirements. Return ONLY the "
    "structured output format."
)
POST_DISPATCH_MISSING_ROUTE_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate {handoff_path}. "
    "{previous_agent} just finished, but the updated HANDOFF.md is missing "
    "a dispatchable route. Focus on the route-missing failure and what must "
    "change for the dispatcher to safely continue to audit, implementation, "
    "epic close, or escalation. Return ONLY the structured output format."
)
POST_DISPATCH_ROUTING_RECOVERY_INSTRUCTION_TEMPLATE = (
    "The prior agent ({previous_agent}) completed work, but the updated "
    "HANDOFF.md does not contain a dispatchable routing instruction. Read the "
    "current HANDOFF.md and determine the correct next step. Rewrite "
    "HANDOFF.md with either a canonical dispatchable route for the next agent "
    "or an explicit user section if the work cannot be routed safely."
)
POST_DISPATCH_HANDOFF_HYGIENE_REPAIR_PROMPT_TEMPLATE = (
    "Repair only {handoff_path} from your immediately preceding "
    "handoff. Do not re-execute implementation work. Do not modify source, "
    "tests, project-state files, ledger files, or any file except "
    "{handoff_path}. Fix only these handoff hygiene validation errors: "
    "{errors}. Preserve the current frontmatter next_agent, producer, scope_sha, "
    "prior_sha, close_type, epic_id, story_id, story_title, and "
    "remaining_stories unless the listed validation error explicitly names that "
    "field. {commit_rule}"
)
POST_DISPATCH_HANDOFF_HYGIENE_REPAIR_COMMIT_RULE = (
    "Commit exactly {handoff_path} before finishing. Failing to edit "
    "HANDOFF.md or failing to commit the edit is a critical failure."
)
# In dispatcher commit mode the handoff is live state that never gets
# committed, so the repair must leave it in the working tree.
POST_DISPATCH_HANDOFF_HYGIENE_REPAIR_UNCOMMITTED_RULE = (
    "Dispatcher commit mode is on: leave {handoff_path} uncommitted and do not "
    "run `git add`, `git commit`, or any other command that writes to `.git`. "
    "Failing to edit HANDOFF.md is a critical failure."
)
POST_DISPATCH_HANDOFF_HYGIENE_PLANNER_RECOVERY_TEMPLATE = (
    "The producer failed one-shot HANDOFF hygiene repair after post-dispatch "
    "validation errors: {errors}. Do not modify source, tests, project-state "
    "files, ledger files, or completed work. Only rewrite "
    "{handoff_path} to produce a dispatchable next step, or route to "
    "user if unsafe. Preserve scope_sha, prior_sha, close_type, epic_id, "
    "story_id, story_title, remaining_stories, producer, and the audit/work "
    "evidence unless a listed validator error explicitly identifies that field "
    "as invalid. Commit exactly {handoff_path} before finishing."
)
PRE_DISPATCH_INVALID_FRONTMATTER_PLANNER_RECOVERY_TEMPLATE = (
    "The current HANDOFF.md has invalid but parseable routing frontmatter before "
    "dispatch. This is cleanup-only recovery. Do not execute implementation, "
    "audit, or scoping work yet. Do not modify source, tests, project-state "
    "files, ledger files, or completed work. Only rewrite "
    "{handoff_path} into a valid dispatchable handoff, or route to user "
    "if unsafe. Preserve the intended next_agent `{next_agent}` unless the "
    "handoff evidence proves that route is unsafe. Router warning: {warning}. "
    "Commit exactly {handoff_path} before finishing."
)
PRE_DISPATCH_INVALID_PLANNER_FRONTMATTER_TEMPLATE = (
    "The current HANDOFF.md routes to planner, but its routing frontmatter is "
    "invalid before dispatch. Treat this as a normal planner planning/scoping "
    "dispatch, not cleanup-only recovery. First account for the router warning "
    "while rewriting HANDOFF.md: {warning}. Then perform the planner work "
    "requested by the handoff body and produce the next dispatchable handoff. "
    "Do not stop after only fixing frontmatter. Do not route back to planner "
    "unless the result is an explicit user-facing pause/escalation that cannot "
    "be assigned to backend, frontend, or auditor. Do not modify "
    "source, tests, project-state files, ledger files, or completed work. Commit "
    "exactly {handoff_path} before finishing."
)
STALE_FINALIZER_RECOVERY_INSTRUCTION = (
    "The prior finalizer cycle already completed, but HANDOFF.md was not "
    "rewritten and still contains stale finalizer routing. Treat "
    "PROJECT_STATE.md and Git history as the source of truth for the current "
    "workflow phase. Evaluate the current repo state, scope the next phase, "
    "and rewrite HANDOFF.md for the next cycle instead of repeating "
    "finalizer."
)
RATIONALIZATION_RECOVERY_INSTRUCTION_TEMPLATE = (
    "The validator detected completion-claim rationalization in HANDOFF.md: "
    "{warnings}. Do not modify source, tests, project-state files, ledger files, "
    "or completed work. Rewrite {handoff_path} to either provide fresh "
    "verification evidence and a dispatchable route, or route to user if the "
    "completion claim cannot be supported. Commit exactly {handoff_path} "
    "before finishing."
)
# Bounded planner recovery at a dispatcher pause: one planner attempt per
# pause streak, carrying the pause reason and the validator's diagnosis.
PAUSE_RECOVERY_INSTRUCTION_TEMPLATE = (
    "The dispatcher paused instead of dispatching. Pause reason: "
    "{pause_reason} Validator diagnosis: {diagnosis} This is a bounded "
    "planner recovery: you get one attempt, and if the handoff is still not "
    "dispatchable afterwards the dispatcher waits for a person. Do not modify "
    "source, tests, project-state files, ledger files, or completed work. "
    "Rewrite {handoff_path} so the next route is dispatchable. If you cannot "
    "choose a route safely, route to user with `status: escalate_to_user` and "
    "one specific question for the user."
)
PAUSE_RECOVERY_NO_DIAGNOSIS = "The validator returned no diagnosis."
# Planner dispatch requested by a person at the operator prompt. The operator
# text is passed as a keyword value, so braces in it are not parsed again.
OPERATOR_PAUSE_INSTRUCTION_TEMPLATE = (
    "The dispatcher paused instead of dispatching. Pause reason: "
    "{pause_reason} A person at the dispatcher console sent this instruction "
    "from the operator prompt. {operator_request} Do not modify source, "
    "tests, project-state files, ledger files, or completed work. Rewrite "
    "{handoff_path} so the next route is dispatchable. If you cannot choose a "
    "route safely, route to user with `status: escalate_to_user` and one "
    "specific question for the user."
)
OPERATOR_BEST_JUDGMENT_REQUEST = (
    "The operator gave no guidance: resolve the pause with your best judgment."
)
OPERATOR_GUIDANCE_PREFIX = "Operator guidance: "
# Direct role dispatch requested with `@<role> <message>` at the operator
# prompt. The operator text is passed as a keyword value, so braces in it are
# not parsed again.
OPERATOR_ROLE_INSTRUCTION_TEMPLATE = (
    "The dispatcher paused instead of dispatching. Pause reason: "
    "{pause_reason} A person at the dispatcher console dispatched you directly "
    "from the operator prompt. {operator_request} Stay within your role's "
    "boundaries and the Work Packet's Files in scope, and write your normal "
    "handback to {handoff_path}."
)
# An `@auditor` dispatch runs the read-only audit fan-out when
# `audit.security_enabled` is true. The reviewer must not write the handoff
# there, so it gets this instruction instead of the normal-handback one.
OPERATOR_ROLE_AUDIT_FAN_OUT_INSTRUCTION_TEMPLATE = (
    "The dispatcher paused instead of dispatching. Pause reason: "
    "{pause_reason} A person at the dispatcher console dispatched you directly "
    "from the operator prompt. {operator_request} Apply the operator's "
    "message within the review above. This review stays read-only: do not "
    "edit {handoff_path} or any other file, and return only the structured "
    "JSON object the review instructions request."
)
OPERATOR_ROLE_NO_MESSAGE_REQUEST = (
    "The operator gave no message: continue the assignment in the handoff."
)
OPERATOR_ROLE_INSTRUCTION_PREFIX = "Operator instruction: "
THREE_FIX_CIRCUIT_BREAKER_INSTRUCTION_TEMPLATE = (
    "Story `{story_id}` has bounced between implementation and audit "
    "{bounce_count} times. Do not continue the same implementation loop. "
    "Re-scope the story into a smaller work packet, route to user if the scope "
    "is unsafe, or route to the correct next role with explicit acceptance "
    "criteria and verification evidence requirements."
)

LogFn = Callable[[str, str], None]
_COMMIT_SHA_RE = re.compile(r"(?i)\b[0-9a-f]{7,40}\b")
PROTECTED_FILE_MODIFIED_PREFIX = "protected_file_modified:"
_GLOB_CHARS = frozenset("*?[")


def _render_prompt(template: str, handoff_path: Path, **fields: object) -> str:
    return template.format(handoff_path=Path(handoff_path).as_posix(), **fields)


@dataclass(frozen=True, slots=True)
class Cycle:
    number: int
    route: RouteName
    handoff_sha: str
    handoff_path: Path


@dataclass(frozen=True, slots=True)
class PendingPlannerRecovery:
    handoff_sha: str
    additional_instruction: str
    kind: str = "routing"
    # Every recovery dispatches the planner except an operator `@<role>`
    # dispatch, which names its own route.
    route: RouteName = "planner"


@dataclass(frozen=True, slots=True)
class PreDispatchPlannerRecovery:
    decision: RoutingDecision
    additional_instruction: str


@dataclass(frozen=True, slots=True)
class HandoffRepairState:
    head_sha: str | None
    handoff_sha: str
    dirty_files: tuple[str, ...]
    critical_frontmatter: tuple[tuple[str, object], ...] | None


@dataclass(frozen=True, slots=True)
class HandoffRepairAttempt:
    attempted: bool
    succeeded: bool
    validation_result: ValidationResult | None = None
    failure_reason: str | None = None
    protected_violation: bool = False


@dataclass(frozen=True, slots=True)
class ProtectedFileSnapshot:
    head_sha: str | None
    digests: tuple[tuple[str, str], ...]


def run_loop(
    config: DispatchConfig,
    *,
    max_cycles: int | None = None,
    log: LogFn | None = None,
) -> int:
    log_fn = log or _default_log

    _log_startup_banner(config, log_fn)
    missing_sandbox_roles = _roles_missing_gemini_sandbox(config)
    if missing_sandbox_roles and not config.dry_run:
        for role in missing_sandbox_roles:
            _log(
                log_fn,
                "ERROR",
                gemini_provider.gemini_sandbox_unavailable_message(role),
            )
        return 1
    if not config.dry_run:
        # Stops the run before any agent launches when the security reviewer
        # (configured or chosen automatically) cannot launch.
        reviewer_launch_error = security_reviewer_startup_error(config, log=log_fn)
        if reviewer_launch_error is not None:
            _log(log_fn, "ERROR", reviewer_launch_error)
            return 1
    _restore_bounce_counts(config, log_fn)
    mark_startup_complete = getattr(log_fn, "mark_startup_complete", None)
    if callable(mark_startup_complete):
        mark_startup_complete()
    if config.publish.mode == "pr" and not config.dry_run:
        # A publish that paused on a base-branch merge resumes once the
        # operator has merged the base branch into the story branch.
        outcome = resume_paused_publish(
            repo_root=config.repo_root,
            remote=config.publish.remote,
            auto_merge=config.publish.auto_merge,
            wait_for_checks=config.publish.wait_for_checks,
            handoff_content=_read_required_text(config.handoff_full_path) or "",
            log=log_fn,
            review=_review_gate(config),
        )
        if outcome.handoff is not None:
            config.handoff_full_path.write_text(outcome.handoff, encoding="utf-8")
        if outcome.stop:
            return outcome.exit_code

    consecutive_failures = 0
    previous_cycle: Cycle | None = None
    cycle_number = 0
    pending_planner_recovery: PendingPlannerRecovery | None = None
    # One bounded planner recovery per pause streak. Only a cycle dispatched
    # without a forced instruction that passes validation clears it.
    pause_recovery_used = False
    # At most one operator input reader per run_loop call.
    operator_prompt = _OperatorPromptState()
    breaker_planner_seen: set[str] = set()
    planner_session_id: str | None = None
    planner_previous_handoff_sha: str | None = None
    implementer_base_sha: str | None = None
    implementer_base_story_key: str | None = None
    # (story key, HEAD before that story's first gated dispatch).
    gate_story_base: tuple[str | None, str] | None = None
    implementer_leftovers: ImplementerLeftovers | None = None

    while True:
        cycle_number += 1
        _log(log_fn, "INFO", f"--- Cycle {cycle_number} ---")

        handoff_content = _read_required_text(config.handoff_full_path)
        if handoff_content is None:
            _log(
                log_fn,
                "ERROR",
                f"Handoff file not found at: {config.handoff_full_path}",
            )
            return 1
        handoff_content = _repair_handoff_frontmatter_file(
            config.handoff_full_path,
            handoff_content,
            log_fn,
        )
        handoff_content = _normalize_handoff_next_agent_file(
            config.handoff_full_path,
            handoff_content,
            config.normalizer,
            log_fn,
            repo_root=config.repo_root,
        )

        project_state_content = _read_optional_text(config.project_state_full_path)
        handoff_sha = _content_sha(handoff_content)
        _log_handoff_scope(handoff_content, log_fn)
        cycle_frontmatter = _parse_frontmatter_or_none(handoff_content)
        forced_additional_instruction: str | None = None
        active_planner_recovery_kind: str | None = None
        if (
            pending_planner_recovery is not None
            and pending_planner_recovery.handoff_sha == handoff_sha
        ):
            if pending_planner_recovery.route == "planner":
                decision = RoutingDecision(
                    route="planner",
                    confidence="HIGH",
                    source="post_dispatch_routing_recovery",
                    reasoning=(
                        "The previous cycle ended with a non-dispatchable handoff, "
                        "so the planner is repairing routing or escalating."
                    ),
                    warnings=[],
                )
            else:
                decision = RoutingDecision(
                    route=pending_planner_recovery.route,
                    confidence="HIGH",
                    source="operator_role_dispatch",
                    reasoning=(
                        "The operator targeted the "
                        f"{pending_planner_recovery.route} role at the pause "
                        "prompt, so it runs directly on the current handoff."
                    ),
                    warnings=[],
                )
            forced_additional_instruction = (
                pending_planner_recovery.additional_instruction
            )
            active_planner_recovery_kind = pending_planner_recovery.kind
            pending_planner_recovery = None
        else:
            if pending_planner_recovery is not None:
                if pending_planner_recovery.route == "planner":
                    dropped_message = (
                        f"Dropped the scheduled {pending_planner_recovery.kind} "
                        "planner recovery because HANDOFF.md changed before it "
                        "ran; routing the current handoff normally."
                    )
                else:
                    dropped_message = (
                        f"Dropped the scheduled {pending_planner_recovery.kind} "
                        f"dispatch to {pending_planner_recovery.route} because "
                        "HANDOFF.md changed before it ran; routing the current "
                        "handoff normally."
                    )
                _log(log_fn, "WARN", dropped_message)
            pending_planner_recovery = None
            decision = route_handoff(
                handoff_content,
                project_state_content=project_state_content,
            )
        recovery_decision = _stale_route_recovery_decision(
            decision.route,
            project_state_content=project_state_content,
            previous_cycle=previous_cycle,
            handoff_sha=handoff_sha,
        )
        if recovery_decision is not None:
            _log(
                log_fn,
                "WARN",
                "STALE finalizer detected after a completed finalizer cycle; redirecting this cycle to the planner for forward routing.",
            )
            decision = recovery_decision

        if decision.route == "unknown":
            pre_dispatch_recovery = _pre_dispatch_invalid_frontmatter_recovery(
                decision,
                handoff_content,
                handoff_path=config.handoff_path,
            )
            if pre_dispatch_recovery is not None:
                if (
                    pre_dispatch_recovery.decision.source
                    == "pre_dispatch_planner_frontmatter_repair"
                ):
                    recovery_message = (
                        "HANDOFF.md routes to planner but has invalid frontmatter; "
                        "dispatching planner normally with frontmatter repair instruction."
                    )
                else:
                    recovery_message = (
                        "HANDOFF.md has invalid but parseable routing frontmatter; "
                        "routing to planner for cleanup-only recovery."
                    )
                _log(
                    log_fn,
                    "WARN",
                    recovery_message,
                )
                decision = pre_dispatch_recovery.decision
                forced_additional_instruction = (
                    pre_dispatch_recovery.additional_instruction
                )

        _log(log_fn, "INFO", f"Routing source: {decision.source}")

        if decision.route == "unknown":
            pause_diagnosis = _run_unknown_route_validator(config, log_fn)
            pause_reason = (
                "No routing instruction found in HANDOFF.md. Update the handoff; dispatch will resume after the file changes."
            )
            if _pause_recovery_available(config, pause_recovery_used, log_fn):
                pending_planner_recovery = _schedule_pause_recovery(
                    config,
                    pause_reason=pause_reason,
                    validation_result=pause_diagnosis,
                    log=log_fn,
                )
                pause_recovery_used = True
                continue
            pause = _pause_at_dispatcher_pause(
                config,
                handoff_sha=handoff_sha,
                reason=pause_reason,
                log=log_fn,
                operator_prompt=operator_prompt,
            )
            if pause.stop:
                return 0
            if pause.kind == "retry":
                previous_cycle = None
            elif pause.pending_planner_recovery is not None:
                pending_planner_recovery = pause.pending_planner_recovery
            continue

        if decision.route in {"backend", "frontend"} and cycle_frontmatter is not None:
            story_id = cycle_frontmatter.story_id
            if story_id and bounce_count(story_id) >= 3:
                if story_id in breaker_planner_seen:
                    _log(
                        log_fn,
                        "ERROR",
                        "THREE_FIX_CIRCUIT_BREAKER_ESCALATE: "
                        f"story_id={story_id} bounce_count={bounce_count(story_id)} planner recovery already attempted.",
                    )
                    decision = RoutingDecision(
                        route="user",
                        confidence="HIGH",
                        source="three_fix_circuit_breaker_escalation",
                        reasoning=(
                            "The story exceeded the three-fix circuit breaker "
                            "after planner recovery."
                        ),
                        warnings=[],
                    )
                else:
                    breaker_planner_seen.add(story_id)
                    count = bounce_count(story_id)
                    _log(
                        log_fn,
                        "WARN",
                        "THREE_FIX_CIRCUIT_BREAKER: "
                        f"story_id={story_id} bounce_count={count}; routing to planner for re-scoping.",
                    )
                    # The breaker also overrides an operator `@<role>`
                    # dispatch; its planner cycle is not that dispatch.
                    active_planner_recovery_kind = None
                    forced_additional_instruction = (
                        THREE_FIX_CIRCUIT_BREAKER_INSTRUCTION_TEMPLATE.format(
                            story_id=story_id,
                            bounce_count=count,
                        )
                    )
                    decision = RoutingDecision(
                        route="planner",
                        confidence="HIGH",
                        source="three_fix_circuit_breaker",
                        reasoning=(
                            "The story exceeded the three-fix circuit breaker, "
                            "so planner must re-scope before more implementation."
                        ),
                        warnings=[],
                    )

        if decision.route == "user":
            # `retry` on a user route re-reads the handoff, which still routes
            # to user, so the dispatcher pauses again at once.
            pause = _pause_at_dispatcher_pause(
                config,
                handoff_sha=handoff_sha,
                reason=(
                    "ESCALATION DETECTED in HANDOFF.md. Resolve it manually; dispatch will resume after the file changes."
                ),
                log=log_fn,
                operator_prompt=operator_prompt,
            )
            if pause.stop:
                return 0
            if pause.kind == "retry":
                previous_cycle = None
            elif pause.pending_planner_recovery is not None:
                pending_planner_recovery = pause.pending_planner_recovery
            continue

        current_cycle = Cycle(
            number=cycle_number,
            route=decision.route,
            handoff_sha=handoff_sha,
            handoff_path=config.handoff_full_path,
        )

        if (
            # A pause-recovery, operator planner, or operator role cycle may
            # repeat a route on an unchanged handoff; checking it would pause
            # on its own recovery.
            active_planner_recovery_kind
            not in {"pause", "operator", "operator_role"}
            and previous_cycle is not None
            and previous_cycle.route == current_cycle.route
            and previous_cycle.handoff_sha == current_cycle.handoff_sha
        ):
            pause_diagnosis = _run_stale_route_validator(
                config, current_cycle.route, log_fn
            )
            pause_reason = (
                f"STALE ROUTING DETECTED: same route ({current_cycle.route}) and unchanged HANDOFF hash."
            )
            if _pause_recovery_available(config, pause_recovery_used, log_fn):
                pending_planner_recovery = _schedule_pause_recovery(
                    config,
                    pause_reason=pause_reason,
                    validation_result=pause_diagnosis,
                    log=log_fn,
                )
                pause_recovery_used = True
                continue
            pause = _pause_at_dispatcher_pause(
                config,
                handoff_sha=current_cycle.handoff_sha,
                reason=pause_reason,
                log=log_fn,
                operator_prompt=operator_prompt,
            )
            if pause.stop:
                return 0
            if pause.kind == "retry":
                previous_cycle = None
            elif pause.pending_planner_recovery is not None:
                pending_planner_recovery = pause.pending_planner_recovery
            continue

        _log(log_fn, "INFO", f"Routing instruction: {decision.route}")
        for warning in decision.warnings:
            _log(log_fn, "WARN", warning)

        # An operator `@<role>` dispatch already saw this pause reason and
        # chose to run the role anyway, for example on the other files.
        protected_scope_reason = (
            None
            if active_planner_recovery_kind == "operator_role"
            else _claude_protected_scope_pause_reason(
                config, current_cycle.route, handoff_content
            )
        )

        if config.dry_run:
            if protected_scope_reason is not None:
                _log(
                    log_fn,
                    "WARN",
                    f"[DRY RUN] Would pause: {protected_scope_reason}",
                )
            else:
                _log_dry_run(config, current_cycle.route, log_fn)
            _log(log_fn, "INFO", "[DRY RUN] Single cycle complete. Exiting.")
            return 0

        if decision.confidence != "HIGH":
            pause_diagnosis = _run_low_confidence_route_validator(
                config, decision, log_fn
            )
            pause_reason = (
                f"Routing instruction {decision.route} is only {decision.confidence} confidence. Update HANDOFF.md; dispatch will resume after the file changes."
            )
            if _pause_recovery_available(config, pause_recovery_used, log_fn):
                pending_planner_recovery = _schedule_pause_recovery(
                    config,
                    pause_reason=pause_reason,
                    validation_result=pause_diagnosis,
                    log=log_fn,
                )
                pause_recovery_used = True
                continue
            pause = _pause_at_dispatcher_pause(
                config,
                handoff_sha=current_cycle.handoff_sha,
                reason=pause_reason,
                log=log_fn,
                operator_prompt=operator_prompt,
            )
            if pause.stop:
                return 0
            if pause.kind == "retry":
                previous_cycle = None
            elif pause.pending_planner_recovery is not None:
                pending_planner_recovery = pause.pending_planner_recovery
            continue

        if protected_scope_reason is not None:
            if _pause_recovery_available(config, pause_recovery_used, log_fn):
                pending_planner_recovery = _schedule_pause_recovery(
                    config,
                    pause_reason=protected_scope_reason,
                    validation_result=None,
                    log=log_fn,
                )
                pause_recovery_used = True
                continue
            pause = _pause_at_dispatcher_pause(
                config,
                handoff_sha=current_cycle.handoff_sha,
                reason=protected_scope_reason,
                log=log_fn,
                operator_prompt=operator_prompt,
            )
            if pause.stop:
                return 0
            if pause.kind == "retry":
                previous_cycle = None
            elif pause.pending_planner_recovery is not None:
                pending_planner_recovery = pause.pending_planner_recovery
            continue

        if current_cycle.route == "finalizer":
            _log(
                log_fn,
                "DISPATCH",
                "Dispatching auditor ledger-updater for epic close.",
            )
            protected_before = _capture_protected_snapshot(config)
            finalizer_git_before = (
                git_position(config.repo_root)
                if config.commit_mode == "dispatcher"
                else None
            )
            ledger_result = _invoke_epic_close(config=config, log=log_fn)
            if finalizer_git_before is not None:
                finalizer_check = undo_planner_handoff_commits(
                    config.repo_root,
                    config.handoff_path,
                    finalizer_git_before,
                    role="finalizer",
                    may_commit_other_files=True,
                )
                if finalizer_check.error is not None:
                    _log(
                        log_fn,
                        "ERROR",
                        "Dispatcher commit mode: "
                        f"{finalizer_check.error}. Take the handoff out of that "
                        "commit, then start the dispatcher again.",
                    )
                    return 1
                if finalizer_check.undone:
                    _log(
                        log_fn,
                        "WARN",
                        "The finalizer committed the handoff ("
                        + ", ".join(sha[:12] for sha in finalizer_check.undone)
                        + ") despite dispatcher commit mode; undid it with "
                        "`git reset --soft`. The handoff stays uncommitted.",
                    )
            if _enforce_protected_files(
                config=config,
                before=protected_before,
                role_label="finalizer",
                handoff_content=handoff_content,
                log=log_fn,
            ):
                previous_cycle = current_cycle
                consecutive_failures = 0
                continue
            if _stop_on_auth_failure(
                config,
                role="finalizer",
                exit_code=int(getattr(ledger_result, "subagent_exit_code", 0)),
                stdout=ledger_result.stdout,
                stderr=ledger_result.stderr,
                log=log_fn,
            ):
                return 1
            parse_error = ledger_result.parse_error
            if parse_error:
                pause_diagnosis = _run_epic_close_validator(
                    config, parse_error, log_fn
                )
                pause_reason = (
                    "finalizer ledger-updater output was unparseable. Update HANDOFF.md; dispatch will resume after the file changes."
                )
                if _pause_recovery_available(config, pause_recovery_used, log_fn):
                    pending_planner_recovery = _schedule_pause_recovery(
                        config,
                        pause_reason=pause_reason,
                        validation_result=pause_diagnosis,
                        log=log_fn,
                    )
                    pause_recovery_used = True
                    continue
                pause = _pause_at_dispatcher_pause(
                    config,
                    handoff_sha=current_cycle.handoff_sha,
                    reason=pause_reason,
                    log=log_fn,
                    operator_prompt=operator_prompt,
                )
                if pause.stop:
                    return 0
                if pause.kind == "retry":
                    previous_cycle = None
                elif pause.pending_planner_recovery is not None:
                    pending_planner_recovery = pause.pending_planner_recovery
                continue
            exit_code = int(getattr(ledger_result, "subagent_exit_code", 0))
            if exit_code != 0:
                consecutive_failures = _record_failure(
                    config,
                    consecutive_failures + 1,
                    log_fn,
                )
                if consecutive_failures >= config.max_consecutive_failures:
                    return 1
                continue

            previous_cycle = current_cycle
            consecutive_failures = 0
            if forced_additional_instruction is None:
                pause_recovery_used = False
        else:
            if (
                config.publish.mode != "local"
                and current_cycle.route in {"backend", "frontend"}
            ):
                branch_error = ensure_story_branch(
                    repo_root=config.repo_root,
                    prefix=config.publish.branch_prefix,
                    base_branch=config.publish.base_branch,
                    assignment_content=handoff_content,
                    implementer=current_cycle.route,
                    remote=config.publish.remote,
                    handoff_path=config.handoff_path,
                )
                if branch_error is not None:
                    _log(log_fn, "ERROR", f"Story branch not ready: {branch_error}.")
                    return 1
            pre_dispatch_state: PreDispatchState | None = None
            story_base_sha: str | None = None
            story_key = carry_over_key(handoff_content)
            if _work_claim_gate_applies(config, current_cycle.route):
                pre_dispatch_state = capture_pre_dispatch_state(
                    config.repo_root,
                    leftovers=implementer_leftovers,
                    story_key=story_key,
                )
                if pre_dispatch_state.head_sha is not None and (
                    story_key is None
                    or gate_story_base is None
                    or gate_story_base[0] != story_key
                ):
                    gate_story_base = (
                        story_key,
                        _story_start_sha(
                            config, handoff_content, pre_dispatch_state.head_sha
                        ),
                    )
                if gate_story_base is not None and gate_story_base[0] == story_key:
                    story_base_sha = gate_story_base[1]
                if pre_dispatch_state.carried_over:
                    story_id = _handoff_story_id(handoff_content)
                    _log(
                        log_fn,
                        "INFO",
                        "Carrying over uncommitted work from the previous "
                        f"{story_id or 'same Work Packet'} dispatch: "
                        f"{', '.join(pre_dispatch_state.carried_over)}.",
                    )
            if config.audit.security_enabled and current_cycle.route in {
                "backend",
                "frontend",
            }:
                # The audit fan-out reviews pre-dispatch HEAD..scope_sha, so
                # remember where the implementer started. A retry of the same
                # story keeps the first dispatch's base so the audit still
                # covers commits from earlier, bounced attempts.
                if (
                    implementer_base_sha is None
                    or story_key is None
                    or story_key != implementer_base_story_key
                ):
                    implementer_base_sha = (
                        pre_dispatch_state.head_sha
                        if pre_dispatch_state is not None
                        else _git_head(config.repo_root)
                    )
                    if story_key != implementer_base_story_key:
                        # The story's first dispatch in this run or since an
                        # approval. A pull request correction round re-audits
                        # the whole story branch; in `local` mode the range
                        # restarts at HEAD.
                        implementer_base_sha = _story_start_sha(
                            config, handoff_content, implementer_base_sha
                        )
                    implementer_base_story_key = story_key
            planner_git_before = (
                git_position(config.repo_root)
                if current_cycle.route == "planner"
                and config.commit_mode == "dispatcher"
                else None
            )
            protected_before = _capture_protected_snapshot(config)
            dispatch_result, previous_agent = _dispatch_route(
                current_cycle.route,
                config,
                log_fn,
                planner_session_id=planner_session_id,
                planner_previous_handoff_sha=planner_previous_handoff_sha,
                current_handoff_sha=current_cycle.handoff_sha,
                additional_instruction=(
                    forced_additional_instruction
                    if forced_additional_instruction is not None
                    else (
                        STALE_FINALIZER_RECOVERY_INSTRUCTION
                        if decision.source == "stale_epic_close_recovery"
                        else None
                    )
                ),
                audit_context=AuditFanOutContext(
                    cycle_number=cycle_number,
                    handoff_content=handoff_content,
                    implementer_base_sha=implementer_base_sha,
                ),
            )
            if pre_dispatch_state is not None:
                # Whatever this dispatch leaves uncommitted is this story's
                # work, so a later dispatch for the story can still commit it.
                implementer_leftovers = capture_implementer_leftovers(
                    config.repo_root,
                    handoff_path=config.handoff_path,
                    pre_dispatch_state=pre_dispatch_state,
                    story_key=story_key,
                )
            if planner_git_before is not None:
                planner_check = undo_planner_handoff_commits(
                    config.repo_root, config.handoff_path, planner_git_before
                )
                if planner_check.error is not None:
                    _log(
                        log_fn,
                        "ERROR",
                        "Dispatcher commit mode: "
                        f"{planner_check.error}. Fix the checkout, then start "
                        "the dispatcher again.",
                    )
                    return 1
                if planner_check.undone:
                    _log(
                        log_fn,
                        "WARN",
                        "The planner committed the handoff ("
                        + ", ".join(sha[:12] for sha in planner_check.undone)
                        + ") despite dispatcher commit mode; undid it with "
                        "`git reset --soft`. The handoff stays uncommitted.",
                    )
            if _enforce_protected_files(
                config=config,
                before=protected_before,
                role_label=previous_agent,
                handoff_content=handoff_content,
                log=log_fn,
            ):
                previous_cycle = current_cycle
                consecutive_failures = 0
                continue
            post_dispatch_handoff_content = _read_required_text(
                config.handoff_full_path
            )
            if post_dispatch_handoff_content is not None:
                post_dispatch_handoff_content = _repair_handoff_frontmatter_file(
                    config.handoff_full_path,
                    post_dispatch_handoff_content,
                    log_fn,
                )
                _normalize_handoff_next_agent_file(
                    config.handoff_full_path,
                    post_dispatch_handoff_content,
                    config.normalizer,
                    log_fn,
                    repo_root=config.repo_root,
                )
            _log_dispatch_completion(
                handoff_path=config.handoff_full_path,
                previous_agent=previous_agent,
                dispatch_result=dispatch_result,
                prior_handoff_content=handoff_content,
                prior_handoff_sha=current_cycle.handoff_sha,
                log=log_fn,
            )
            if current_cycle.route == "planner" and config.planner_resume_enabled:
                if dispatch_result.session_invalidated:
                    planner_session_id = None
                    planner_previous_handoff_sha = None
                if dispatch_result.exit_code == 0:
                    if dispatch_result.session_id:
                        planner_session_id = dispatch_result.session_id
                    if planner_session_id:
                        planner_previous_handoff_sha = current_cycle.handoff_sha
            if _stop_on_auth_failure(
                config,
                role=current_cycle.route,
                exit_code=dispatch_result.exit_code,
                stdout=dispatch_result.stdout,
                stderr=dispatch_result.stderr,
                log=log_fn,
            ):
                return 1
            if dispatch_result.stop_loop:
                _log(
                    log_fn,
                    "ERROR",
                    "An audit reviewer failed to start; stopping the dispatch "
                    "loop. Fix the reviewer CLI, then start the dispatcher again.",
                )
                return 1
            if dispatch_result.exit_code != 0:
                consecutive_failures = _record_failure(
                    config,
                    consecutive_failures + 1,
                    log_fn,
                )
                if consecutive_failures >= config.max_consecutive_failures:
                    return 1
                continue

            commit_result = _commit_for_implementer(
                config=config,
                route=current_cycle.route,
                assignment_content=handoff_content,
                pre_dispatch_state=pre_dispatch_state,
                log=log_fn,
                story_base_sha=story_base_sha,
            )
            _log(
                log_fn,
                "AGENT",
                f"Running post-dispatch validation for {previous_agent}...",
            )
            validation_result = validate_handoff(
                config.handoff_full_path,
                previous_agent,
                prior_handoff_sha=current_cycle.handoff_sha,
                require_work_packet=config.enforce_work_packet,
                dispatcher_commit_mode=config.commit_mode == "dispatcher",
            )
            _record_post_dispatch_status_transition(
                config=config,
                log=log_fn,
            )
            self_loop_kind = _self_loop_kind(validation_result)
            _log_validation(
                validation_result,
                previous_agent,
                log_fn,
                override_terminal_status=(
                    _self_loop_terminal_status(previous_agent, self_loop_kind)
                    if self_loop_kind is not None
                    else None
                ),
            )
            if validation_result.verdict == "NO" and _scope_sha_missing_for_gate(
                config=config,
                route=current_cycle.route,
                commit_result=commit_result,
                validation_result=validation_result,
            ):
                # A repair agent could only point scope_sha at a commit the
                # implementer did not make; the gate reports the real failure.
                _log(
                    log_fn,
                    "AGENT",
                    f"The dispatcher committed nothing for {previous_agent}, so "
                    "scope_sha stays empty; skipping the handoff repair and "
                    "letting the work-claim gate report the claim.",
                )
            elif validation_result.verdict == "NO":
                if active_planner_recovery_kind == "handoff_hygiene":
                    _log(
                        log_fn,
                        "ERROR",
                        "Planner failed to operationalize HANDOFF.md after producer hygiene repair was exhausted.",
                    )
                    _log(
                        log_fn,
                        "ERROR",
                        "Automatic HANDOFF hygiene recovery is exhausted; user repair is required.",
                    )
                    return 1

                repair_attempt = _attempt_post_dispatch_handoff_hygiene_repair(
                    config=config,
                    validation_result=validation_result,
                    previous_agent=previous_agent,
                    log=log_fn,
                )
                if repair_attempt.protected_violation:
                    previous_cycle = current_cycle
                    consecutive_failures = 0
                    continue
                if repair_attempt.succeeded and repair_attempt.validation_result:
                    validation_result = repair_attempt.validation_result
                    self_loop_kind = _self_loop_kind(validation_result)
                    _log(
                        log_fn,
                        "AGENT",
                        f"Running post-repair validation for {previous_agent}...",
                    )
                    _log_validation(
                        validation_result,
                        previous_agent,
                        log_fn,
                        override_terminal_status=(
                            _self_loop_terminal_status(
                                previous_agent,
                                self_loop_kind,
                            )
                            if self_loop_kind is not None
                            else None
                        ),
                    )
                    if validation_result.verdict != "NO":
                        if forced_additional_instruction is None:
                            pause_recovery_used = False
                        _run_work_claim_gate(
                            config=config,
                            route=current_cycle.route,
                            previous_agent=previous_agent,
                            assignment_content=handoff_content,
                            pre_dispatch_state=pre_dispatch_state,
                            log=log_fn,
                            story_base_sha=story_base_sha,
                        )
                        if _accepted_approval(config):
                            implementer_base_sha = None
                            implementer_base_story_key = None
                        previous_cycle = current_cycle
                        consecutive_failures = 0
                        if max_cycles is not None and cycle_number >= max_cycles:
                            return 0
                        _log(log_fn, "INFO", f"--- End of cycle {cycle_number} ---")
                        _log(log_fn, "INFO", "")
                        continue

                if repair_attempt.attempted and not repair_attempt.succeeded:
                    _log(
                        log_fn,
                        "AGENT",
                        "Post-dispatch handoff hygiene repair failed. Scheduling the planner to clean HANDOFF.md or escalate.",
                    )
                    current_handoff_content = (
                        _read_required_text(config.handoff_full_path) or ""
                    )
                    pending_planner_recovery = PendingPlannerRecovery(
                        handoff_sha=_content_sha(current_handoff_content),
                        additional_instruction=_render_prompt(
                            POST_DISPATCH_HANDOFF_HYGIENE_PLANNER_RECOVERY_TEMPLATE,
                            config.handoff_path,
                            errors=_format_validation_errors(validation_result),
                        ),
                        kind="handoff_hygiene",
                    )
                    previous_cycle = current_cycle
                    consecutive_failures = 0
                    continue

                if _should_schedule_planner_recovery(validation_result, previous_agent):
                    _log(
                        log_fn,
                        "AGENT",
                        f"Post-dispatch handoff for {previous_agent} is not dispatchable. Scheduling the planner to repair routing or escalate on the next cycle.",
                    )
                    current_handoff_content = (
                        _read_required_text(config.handoff_full_path) or ""
                    )
                    pending_planner_recovery = PendingPlannerRecovery(
                        handoff_sha=_content_sha(current_handoff_content),
                        additional_instruction=POST_DISPATCH_ROUTING_RECOVERY_INSTRUCTION_TEMPLATE.format(
                            previous_agent=previous_agent
                        ),
                    )
                    previous_cycle = current_cycle
                    consecutive_failures = 0
                    continue
                if _has_missing_route_error(validation_result):
                    pause_diagnosis = _run_post_dispatch_missing_route_validator(
                        config=config,
                        previous_agent=previous_agent,
                        log=log_fn,
                    )
                    pause_reason = _missing_route_pause_reason(previous_agent)
                    if _pause_recovery_available(config, pause_recovery_used, log_fn):
                        pending_planner_recovery = _schedule_pause_recovery(
                            config,
                            pause_reason=pause_reason,
                            validation_result=pause_diagnosis,
                            log=log_fn,
                        )
                        pause_recovery_used = True
                        previous_cycle = current_cycle
                        consecutive_failures = 0
                        continue
                    pause = _pause_at_dispatcher_pause(
                        config,
                        handoff_sha=_content_sha(
                            _read_required_text(config.handoff_full_path) or ""
                        ),
                        reason=pause_reason,
                        log=log_fn,
                        operator_prompt=operator_prompt,
                    )
                    if pause.stop:
                        return 0
                    if pause.kind == "retry":
                        previous_cycle = None
                    elif pause.pending_planner_recovery is not None:
                        pending_planner_recovery = pause.pending_planner_recovery
                    continue
                if self_loop_kind is not None:
                    pause_diagnosis = _run_self_loop_validator(
                        config=config,
                        previous_agent=previous_agent,
                        self_loop_kind=self_loop_kind,
                        log=log_fn,
                    )
                    pause_reason = _self_loop_pause_reason(
                        previous_agent,
                        self_loop_kind,
                    )
                    if _pause_recovery_available(config, pause_recovery_used, log_fn):
                        pending_planner_recovery = _schedule_pause_recovery(
                            config,
                            pause_reason=pause_reason,
                            validation_result=pause_diagnosis,
                            log=log_fn,
                        )
                        pause_recovery_used = True
                        previous_cycle = current_cycle
                        consecutive_failures = 0
                        continue
                    pause = _pause_at_dispatcher_pause(
                        config,
                        handoff_sha=_content_sha(
                            _read_required_text(config.handoff_full_path) or ""
                        ),
                        reason=pause_reason,
                        log=log_fn,
                        operator_prompt=operator_prompt,
                    )
                    if pause.stop:
                        return 0
                    if pause.kind == "retry":
                        previous_cycle = None
                    elif pause.pending_planner_recovery is not None:
                        pending_planner_recovery = pause.pending_planner_recovery
                    continue
                return 1

            if (
                validation_result.verdict != "NO"
                and forced_additional_instruction is None
            ):
                pause_recovery_used = False
            if _run_work_claim_gate(
                config=config,
                route=current_cycle.route,
                previous_agent=previous_agent,
                assignment_content=handoff_content,
                pre_dispatch_state=pre_dispatch_state,
                log=log_fn,
                story_base_sha=story_base_sha,
            ):
                previous_cycle = current_cycle
                consecutive_failures = 0
            elif _has_rationalization_warning(validation_result):
                _log(
                    log_fn,
                    "WARN",
                    "Completion-claim rationalization detected; scheduling planner recovery instead of advancing.",
                )
                current_handoff_content = (
                    _read_required_text(config.handoff_full_path) or ""
                )
                pending_planner_recovery = PendingPlannerRecovery(
                    handoff_sha=_content_sha(current_handoff_content),
                    additional_instruction=_render_prompt(
                        RATIONALIZATION_RECOVERY_INSTRUCTION_TEMPLATE,
                        config.handoff_path,
                        warnings=_format_rationalization_warnings(validation_result),
                    ),
                    kind="rationalization",
                )
                previous_cycle = current_cycle
                consecutive_failures = 0
                continue
            else:
                previous_cycle = current_cycle
                consecutive_failures = 0
                if _accepted_approval(config):
                    # The story is approved; its next implementer dispatch
                    # starts a new audit range from where its story starts.
                    implementer_base_sha = None
                    implementer_base_story_key = None
                if (
                    config.publish.mode != "local"
                    and current_cycle.route == "auditor"
                ):
                    outcome = publish_story(
                        repo_root=config.repo_root,
                        mode=config.publish.mode,
                        remote=config.publish.remote,
                        auto_merge=config.publish.auto_merge,
                        wait_for_checks=config.publish.wait_for_checks,
                        handoff_content=(
                            _read_required_text(config.handoff_full_path) or ""
                        ),
                        log=log_fn,
                        review=_review_gate(config),
                        base_branch=config.publish.base_branch,
                        branch_prefix=config.publish.branch_prefix,
                    )
                    if outcome.handoff is not None:
                        config.handoff_full_path.write_text(
                            outcome.handoff, encoding="utf-8"
                        )
                    if outcome.stop:
                        return outcome.exit_code
                if config.publish.mode == "pr" and current_cycle.route == "planner":
                    # The planner may have just decided deferred review
                    # findings that are holding a pull request open.
                    outcome = settle_deferred_findings(
                        repo_root=config.repo_root,
                        remote=config.publish.remote,
                        handoff_content=(
                            _read_required_text(config.handoff_full_path) or ""
                        ),
                        log=log_fn,
                    )
                    if outcome.stop:
                        return outcome.exit_code

        if max_cycles is not None and cycle_number >= max_cycles:
            return 0

        _log(log_fn, "INFO", f"--- End of cycle {cycle_number} ---")
        _log(log_fn, "INFO", "")


def _review_gate(config: DispatchConfig) -> ReviewGate | None:
    publish = config.publish
    if not publish.wait_for_reviewers:
        return None
    return ReviewGate(
        reviewers=publish.wait_for_reviewers,
        timeout_seconds=publish.review_timeout_seconds,
        poll_seconds=publish.review_poll_seconds,
        max_rounds=publish.max_review_rounds,
        codex_bot_login=publish.codex_bot_login,
    )


def _dispatch_route(
    route: RouteName,
    config: DispatchConfig,
    log: LogFn,
    *,
    planner_session_id: str | None = None,
    planner_previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    additional_instruction: str | None = None,
    audit_context: AuditFanOutContext | None = None,
) -> tuple[DispatchResult, str]:
    handoff_path = config.handoff_full_path

    if route == "backend":
        _log(log, "DISPATCH", "Dispatching backend.")
        return _invoke_dispatch_callable(
            invoke_backend_role,
            handoff_path,
            log=log,
            use_resume=config.backend_resume_enabled,
            additional_instruction=_with_commit_mode_instruction(
                config, additional_instruction
            ),
            agent_config=config.agents["backend"],
            repo_root=config.repo_root,
            **_failover_kwargs(config, "backend"),
        ), "backend"

    if route == "planner":
        _log(log, "DISPATCH", "Dispatching planner.")
        return (
            _invoke_dispatch_callable(
                invoke_planner_role,
                handoff_path,
                use_api_key_env=config.planner_api_key_env_enabled,
                additional_instruction=_with_commit_mode_instruction(
                    config,
                    additional_instruction,
                    instruction=PLANNER_COMMIT_MODE_INSTRUCTION,
                ),
                use_resume=config.planner_resume_enabled,
                session_id=planner_session_id,
                previous_handoff_sha=planner_previous_handoff_sha,
                current_handoff_sha=current_handoff_sha,
                log=log,
                agent_config=config.agents["planner"],
                repo_root=config.repo_root,
                **_failover_kwargs(config, "planner"),
            ),
            "planner",
        )

    if route == "frontend":
        if config.use_manual_frontend:
            _log(log, "DISPATCH", "Dispatching manual frontend (GUI, manual pause).")
        else:
            _log(log, "DISPATCH", "Dispatching frontend.")

        return (
            _invoke_dispatch_callable(
                invoke_frontend_role,
                handoff_path,
                use_manual_frontend=config.use_manual_frontend,
                use_api_key_env=config.planner_api_key_env_enabled,
                additional_instruction=_with_commit_mode_instruction(
                    config, additional_instruction
                ),
                log=log,
                agent_config=config.agents["frontend"],
                repo_root=config.repo_root,
                **_failover_kwargs(config, "frontend"),
            ),
            "frontend",
        )

    if route == "auditor":
        if config.audit.security_enabled:
            _log(
                log,
                "DISPATCH",
                "Dispatching audit fan-out (quality + security reviewers).",
            )
            return _dispatch_audit_fan_out(
                config,
                log,
                audit_context=audit_context,
                additional_instruction=additional_instruction,
            ), "auditor (audit)"
        _log(log, "DISPATCH", "Dispatching auditor for audit.")
        return _dispatch_from_subagent(
            _invoke_support_callable(
                "auditor",
                _with_additional_instruction(
                    _render_prompt(AUDIT_PROMPT_TEMPLATE, config.handoff_path),
                    additional_instruction,
                ),
                role="auditor",
                handoff_path=handoff_path,
                agent_config=config.agents["auditor"],
                log=log,
                repo_root=config.repo_root,
                **_failover_kwargs(config, "auditor"),
            )
        ), "auditor (audit)"

    if route == "validator":
        _log(log, "DISPATCH", "Dispatching validator for route clarification.")
        return _dispatch_from_subagent(
            _invoke_support_callable(
                "handoff-router",
                _render_prompt(MISROUTE_PROMPT_TEMPLATE, config.handoff_path),
                role="validator",
                handoff_path=handoff_path,
                agent_config=config.agents["validator"],
                log=log,
                repo_root=config.repo_root,
                **_failover_kwargs(config, "validator"),
            )
        ), "validator"

    raise ValueError(f"Unsupported route: {route}")


def _dispatch_audit_fan_out(
    config: DispatchConfig,
    log: LogFn,
    *,
    audit_context: AuditFanOutContext | None,
    additional_instruction: str | None = None,
) -> DispatchResult:
    handoff_path = config.handoff_full_path
    if audit_context is None:
        audit_context = AuditFanOutContext(
            cycle_number=0,
            handoff_content=_read_required_text(handoff_path) or "",
        )

    failover_kwargs = _failover_kwargs(config, "auditor")

    def quality_invoker(prompt: str, agent_config: AgentConfig) -> SubagentResult:
        return _invoke_support_callable(
            "auditor",
            _with_additional_instruction(prompt, additional_instruction),
            role="auditor",
            handoff_path=handoff_path,
            agent_config=agent_config,
            log=log,
            repo_root=config.repo_root,
            **failover_kwargs,
        )

    return run_audit_fan_out(
        config,
        context=audit_context,
        frontmatter=_parse_frontmatter_or_none(audit_context.handoff_content),
        quality_invoker=quality_invoker,
        log=log,
    )


def _with_additional_instruction(prompt: str, additional_instruction: str | None) -> str:
    """Append an operator instruction to a support-role prompt, if any."""

    if additional_instruction and additional_instruction.strip():
        return "\n\n".join((prompt, additional_instruction.strip()))
    return prompt


def _failover_kwargs(config: DispatchConfig, role: AgentRole) -> dict[str, object]:
    """Return the failover guard for a role that has a fallback agent.

    The protected-file gate only runs after both attempts return, so the guard
    snapshots the protected control files now and stops the fallback from
    starting when the primary changed one of them.
    """

    if config.agents[role].fallback is None:
        return {}
    before = _capture_protected_snapshot(config)
    if before is None:
        return {}

    def guard() -> str | None:
        changed = _protected_file_changes(config, before)
        if not changed:
            return None
        return f"it changed protected control files ({', '.join(changed)})"

    return {"failover_guard": guard}


def _invoke_dispatch_callable(
    func: Callable[..., DispatchResult],
    handoff_path: Path,
    **kwargs: object,
) -> DispatchResult:
    return func(handoff_path, **_supported_kwargs(func, kwargs))


def _invoke_support_callable(
    subagent_name: str,
    prompt: str,
    **kwargs: object,
) -> SubagentResult:
    return invoke_support_role(
        subagent_name,
        prompt,
        **_supported_kwargs(invoke_support_role, kwargs),
    )


def _supported_kwargs(
    func: Callable[..., object],
    kwargs: dict[str, object],
) -> dict[str, object]:
    signature = inspect.signature(func)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in signature.parameters}


def _dispatch_from_subagent(result: SubagentResult) -> DispatchResult:
    return DispatchResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=result.elapsed_seconds,
    )


def _invoke_epic_close(
    *,
    config: DispatchConfig,
    log: LogFn,
):
    return run_epic_close(
        **_supported_kwargs(
            run_epic_close,
            {"config": config, "log": log, **_failover_kwargs(config, "finalizer")},
        )
    )


def _attempt_post_dispatch_handoff_hygiene_repair(
    *,
    config: DispatchConfig,
    validation_result: ValidationResult,
    previous_agent: str,
    log: LogFn,
) -> HandoffRepairAttempt:
    handoff_content = _read_required_text(config.handoff_full_path) or ""
    if not _is_repairable_handoff_hygiene_failure(
        validation_result,
        handoff_content=handoff_content,
    ):
        return HandoffRepairAttempt(attempted=False, succeeded=False)

    before_state = _capture_handoff_repair_state(config)
    if before_state.head_sha is None:
        return HandoffRepairAttempt(
            attempted=True,
            succeeded=False,
            failure_reason="git HEAD could not be resolved before repair",
        )
    if before_state.critical_frontmatter is None:
        return HandoffRepairAttempt(
            attempted=True,
            succeeded=False,
            failure_reason="frontmatter could not be parsed before repair",
        )

    _log(
        log,
        "WARN",
        "Post-dispatch handoff hygiene failed for "
        f"{previous_agent}: {_format_validation_repair_issues(validation_result)}",
    )
    _log(
        log,
        "AGENT",
        f"Running one-shot producer repair for {config.handoff_path.as_posix()} only.",
    )
    protected_before = _capture_protected_snapshot(config)
    dispatcher_commit_mode = config.commit_mode == "dispatcher"
    git_before = git_position(config.repo_root) if dispatcher_commit_mode else None
    repair_result = _dispatch_handoff_hygiene_repair(
        config=config,
        previous_agent=previous_agent,
        validation_result=validation_result,
        log=log,
    )
    if git_before is not None:
        undo = undo_planner_handoff_commits(
            config.repo_root,
            config.handoff_path,
            git_before,
            role=f"{previous_agent} handoff repair",
        )
        if undo.error is not None:
            _log(log, "ERROR", f"Dispatcher commit mode: {undo.error}.")
            return HandoffRepairAttempt(
                attempted=True,
                succeeded=False,
                failure_reason=undo.error,
            )
        if undo.undone:
            _log(
                log,
                "WARN",
                f"The {previous_agent} handoff repair committed the handoff ("
                + ", ".join(sha[:12] for sha in undo.undone)
                + ") despite dispatcher commit mode; undid it with "
                "`git reset --soft`. The handoff stays uncommitted.",
            )
    if repair_result is not None and _enforce_protected_files(
        config=config,
        before=protected_before,
        role_label=f"{previous_agent} (handoff repair)",
        handoff_content=handoff_content,
        log=log,
    ):
        return HandoffRepairAttempt(
            attempted=True,
            succeeded=False,
            failure_reason="protected control file modified",
            protected_violation=True,
        )
    if repair_result is None:
        _log(log, "ERROR", f"No supported producer repair path for {previous_agent}.")
        return HandoffRepairAttempt(
            attempted=True,
            succeeded=False,
            failure_reason="unsupported producer repair path",
        )
    if repair_result.exit_code != 0:
        _log(
            log,
            "ERROR",
            f"Producer repair exited with code {repair_result.exit_code}.",
        )
        return HandoffRepairAttempt(
            attempted=True,
            succeeded=False,
            failure_reason=f"repair exit code {repair_result.exit_code}",
        )

    after_state = _capture_handoff_repair_state(config)
    postcondition_error = _handoff_repair_postcondition_error(
        config=config,
        before_state=before_state,
        after_state=after_state,
        validation_result=validation_result,
        dispatcher_commit_mode=dispatcher_commit_mode,
    )
    if postcondition_error is not None:
        _log(log, "ERROR", f"Producer repair failed invariant: {postcondition_error}")
        return HandoffRepairAttempt(
            attempted=True,
            succeeded=False,
            failure_reason=postcondition_error,
        )

    repaired_validation = validate_handoff(
        config.handoff_full_path,
        previous_agent,
        prior_handoff_sha=before_state.handoff_sha,
        require_work_packet=config.enforce_work_packet,
        dispatcher_commit_mode=config.commit_mode == "dispatcher",
    )
    if repaired_validation.verdict == "NO":
        _log(
            log,
            "ERROR",
            "Producer repair did not produce a valid handoff: "
            f"{_format_validation_errors(repaired_validation)}",
        )
        return HandoffRepairAttempt(
            attempted=True,
            succeeded=False,
            validation_result=repaired_validation,
            failure_reason="repair validation failed",
        )

    _log(log, "AGENT", "Producer handoff hygiene repair passed post-checks.")
    return HandoffRepairAttempt(
        attempted=True,
        succeeded=True,
        validation_result=repaired_validation,
    )


def _is_repairable_handoff_hygiene_failure(
    result: ValidationResult,
    *,
    handoff_content: str,
) -> bool:
    if result.verdict != "NO" or not result.errors:
        return False
    repairable_prefixes = (
        "frontmatter_reason_missing:",
        "frontmatter_producer_missing:",
        "scope_claim_missing:",
        "scope_claim_mismatch:",
        "work_packet_missing:",
        "work_packet_unparseable:",
    )
    for error in result.errors:
        if error.startswith("frontmatter_scope_sha_missing:"):
            if _COMMIT_SHA_RE.search(handoff_content) is None:
                return False
            continue
        if error.startswith("frontmatter_close_type_invalid:"):
            continue
        if not error.startswith(repairable_prefixes):
            return False
    return True


def _dispatch_handoff_hygiene_repair(
    *,
    config: DispatchConfig,
    previous_agent: str,
    validation_result: ValidationResult,
    log: LogFn,
) -> DispatchResult | None:
    repair_prompt = _render_prompt(
        POST_DISPATCH_HANDOFF_HYGIENE_REPAIR_PROMPT_TEMPLATE,
        config.handoff_path,
        errors=_format_validation_repair_issues(validation_result),
        commit_rule=_render_prompt(
            POST_DISPATCH_HANDOFF_HYGIENE_REPAIR_UNCOMMITTED_RULE
            if config.commit_mode == "dispatcher"
            else POST_DISPATCH_HANDOFF_HYGIENE_REPAIR_COMMIT_RULE,
            config.handoff_path,
        ),
    )
    previous_agent_lower = previous_agent.lower()
    if "auditor" in previous_agent_lower and "audit" in previous_agent_lower:
        return _dispatch_from_subagent(
            _invoke_support_callable(
                "auditor",
                repair_prompt,
                role="auditor",
                handoff_path=config.handoff_full_path,
                agent_config=config.agents["auditor"],
                log=log,
                repo_root=config.repo_root,
                **_failover_kwargs(config, "auditor"),
            )
        )
    if previous_agent == "backend":
        return _invoke_dispatch_callable(
            invoke_backend_role,
            config.handoff_full_path,
            log=log,
            use_resume=config.backend_resume_enabled,
            additional_instruction=repair_prompt,
            agent_config=config.agents["backend"],
            repo_root=config.repo_root,
            **_failover_kwargs(config, "backend"),
        )
    if previous_agent == "frontend":
        if config.use_manual_frontend:
            return None
        return _invoke_dispatch_callable(
            invoke_frontend_role,
            config.handoff_full_path,
            use_manual_frontend=config.use_manual_frontend,
            use_api_key_env=config.planner_api_key_env_enabled,
            additional_instruction=repair_prompt,
            log=log,
            agent_config=config.agents["frontend"],
            repo_root=config.repo_root,
            **_failover_kwargs(config, "frontend"),
        )
    return None


def _capture_handoff_repair_state(config: DispatchConfig) -> HandoffRepairState:
    handoff_content = _read_required_text(config.handoff_full_path) or ""
    return HandoffRepairState(
        head_sha=_git_head(config.repo_root),
        handoff_sha=_content_sha(handoff_content),
        dirty_files=_git_dirty_files(config.repo_root),
        critical_frontmatter=_critical_frontmatter_signature(handoff_content),
    )


def _handoff_repair_postcondition_error(
    *,
    config: DispatchConfig,
    before_state: HandoffRepairState,
    after_state: HandoffRepairState,
    validation_result: ValidationResult,
    dispatcher_commit_mode: bool = False,
) -> str | None:
    handoff_path = config.handoff_path.as_posix()
    if after_state.head_sha is None:
        return "git HEAD could not be resolved after repair"
    if dispatcher_commit_mode:
        # Handoff-only commits were already undone, so HEAD must not move.
        if after_state.head_sha != before_state.head_sha:
            return "HEAD changed; repair must not commit in dispatcher commit mode"
    elif after_state.head_sha == before_state.head_sha:
        return "HEAD did not change; repair did not create a commit"
    if after_state.handoff_sha == before_state.handoff_sha:
        return "HANDOFF.md hash did not change"

    if not dispatcher_commit_mode:
        changed_files = _git_changed_files_since(
            config.repo_root,
            before_state.head_sha or "",
            after_state.head_sha,
        )
        if changed_files != (handoff_path,):
            return (
                f"repair commit touched files outside {handoff_path}: "
                f"{', '.join(changed_files) or '<none>'}"
            )

    # Outside the handoff, the working tree must not change. In dispatcher
    # commit mode the handoff stays dirty; otherwise it must be committed.
    expected_dirty = tuple(
        path for path in before_state.dirty_files if path != handoff_path
    )
    after_dirty = (
        tuple(path for path in after_state.dirty_files if path != handoff_path)
        if dispatcher_commit_mode
        else after_state.dirty_files
    )
    if after_dirty != expected_dirty:
        return (
            "repair changed dirty worktree state outside the handoff: "
            f"before={before_state.dirty_files}, after={after_state.dirty_files}"
        )

    mutable_fields = _repair_mutable_frontmatter_fields(
        validation_result,
        before_state=before_state,
    )
    if _filter_frontmatter_signature(
        after_state.critical_frontmatter,
        mutable_fields,
    ) != _filter_frontmatter_signature(
        before_state.critical_frontmatter,
        mutable_fields,
    ):
        return "repair changed preserved routing frontmatter fields"
    return None


def _repair_mutable_frontmatter_fields(
    validation_result: ValidationResult,
    *,
    before_state: HandoffRepairState,
) -> frozenset[str]:
    fields: set[str] = set()
    if _frontmatter_field_value(before_state.critical_frontmatter, "producer") is None:
        fields.add("producer")
    if _frontmatter_field_value(before_state.critical_frontmatter, "scope_sha") is None:
        fields.add("scope_sha")
    for error in validation_result.errors:
        if error.startswith("frontmatter_producer_missing:"):
            fields.add("producer")
        elif error.startswith("frontmatter_scope_sha_missing:"):
            fields.add("scope_sha")
        elif error.startswith("frontmatter_scope_sha_invalid:"):
            fields.add("scope_sha")
        elif error.startswith("frontmatter_close_type_invalid:"):
            fields.add("close_type")
    return frozenset(fields)


def _frontmatter_field_value(
    signature: tuple[tuple[str, object], ...] | None,
    field_name: str,
) -> object | None:
    if signature is None:
        return None
    for key, value in signature:
        if key == field_name:
            return value
    return None


def _filter_frontmatter_signature(
    signature: tuple[tuple[str, object], ...] | None,
    mutable_fields: frozenset[str],
) -> tuple[tuple[str, object], ...] | None:
    if signature is None:
        return None
    return tuple((key, value) for key, value in signature if key not in mutable_fields)


def _critical_frontmatter_signature(
    handoff_content: str,
) -> tuple[tuple[str, object], ...] | None:
    try:
        frontmatter = parse_handoff_frontmatter_text(handoff_content)
    except HandoffFrontmatterError:
        return None
    if frontmatter is None:
        return None
    return (
        ("next_agent", frontmatter.next_agent),
        ("producer", frontmatter.producer),
        ("scope_sha", frontmatter.scope_sha),
        ("prior_sha", frontmatter.prior_sha),
        ("close_type", frontmatter.close_type),
        ("epic_id", frontmatter.epic_id),
        ("story_id", frontmatter.story_id),
        ("story_title", frontmatter.story_title),
        ("remaining_stories", frontmatter.remaining_stories),
    )


def _format_validation_errors(result: ValidationResult) -> str:
    return "; ".join(result.errors) if result.errors else "none"


def _format_validation_repair_issues(result: ValidationResult) -> str:
    issues = list(result.errors)
    issues.extend(
        warning
        for warning in result.warnings
        if warning.startswith("acceptance_coverage_unclear:")
    )
    return "; ".join(issues) if issues else "none"


def _git_head(repo_root: Path) -> str | None:
    return _run_git_stdout(repo_root, ["git", "rev-parse", "HEAD"])


def _git_dirty_files(repo_root: Path) -> tuple[str, ...]:
    # The NUL-separated parser keeps each entry's two-column status intact;
    # stripping the porcelain text would eat the first path's leading space.
    return git_dirty_files(repo_root) or ()


def _git_changed_files_since(
    repo_root: Path,
    before_head: str,
    after_head: str,
) -> tuple[str, ...]:
    output = _run_git_stdout(
        repo_root,
        ["git", "diff", "--name-only", f"{before_head}..{after_head}"],
    )
    if output is None:
        return ()
    return tuple(
        sorted(
            Path(line.strip()).as_posix()
            for line in output.splitlines()
            if line.strip()
        )
    )


def _run_git_stdout(repo_root: Path, command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _capture_protected_snapshot(
    config: DispatchConfig,
) -> ProtectedFileSnapshot | None:
    """Hash every protected control file before a dispatch.

    Uses ``_run_git_stdout`` directly so the snapshot does not consume the
    HEAD/diff helpers that the handoff repair postconditions rely on.
    """
    if not config.protected_paths:
        return None
    return ProtectedFileSnapshot(
        head_sha=_run_git_stdout(config.repo_root, ["git", "rev-parse", "HEAD"]),
        digests=_protected_file_digests(config),
    )


def _protected_file_digests(config: DispatchConfig) -> tuple[tuple[str, str], ...]:
    handoff_rel = _handoff_relative_path(config)
    digests: dict[str, str] = {}
    for pattern in config.protected_paths:
        for path in _protected_candidates(config.repo_root, pattern):
            relative = path.relative_to(config.repo_root).as_posix()
            if relative in digests or _same_repo_path(relative, handoff_rel):
                continue
            if not _protected_path_matches(relative, config.protected_paths):
                continue
            digests[relative] = _file_digest(path)
    return tuple(sorted(digests.items()))


def _protected_candidates(repo_root: Path, pattern: str) -> list[Path]:
    parts = pattern.split("/")
    literal: list[str] = []
    for part in parts:
        if any(char in _GLOB_CHARS for char in part):
            break
        literal.append(part)
    base = repo_root.joinpath(*literal) if literal else repo_root
    if len(literal) == len(parts):
        return [base] if base.is_file() else []
    if not base.is_dir():
        return []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        # Skip .git and nested repositories or worktrees (for example
        # .claude/worktrees/*): they are not this repo's control files.
        dirnames[:] = [
            name
            for name in dirnames
            if name != ".git"
            and not os.path.exists(os.path.join(dirpath, name, ".git"))
        ]
        files.extend(Path(dirpath, name) for name in filenames)
    return files


def _file_digest(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def _protected_path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    path_parts = _path_match_parts(path)
    return any(
        _glob_parts_match(path_parts, _path_match_parts(pattern))
        for pattern in patterns
    )


def _path_match_parts(path: str) -> tuple[str, ...]:
    normalized = path.replace("\\", "/").strip("/")
    if os.name == "nt":
        normalized = normalized.lower()
    return tuple(part for part in normalized.split("/") if part)


def _glob_parts_match(
    path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]
) -> bool:
    if not pattern_parts:
        return not path_parts
    head, rest = pattern_parts[0], pattern_parts[1:]
    if head == "**":
        return any(
            _glob_parts_match(path_parts[index:], rest)
            for index in range(len(path_parts) + 1)
        )
    if not path_parts:
        return False
    return fnmatchcase(path_parts[0], head) and _glob_parts_match(path_parts[1:], rest)


def _same_repo_path(first: str | None, second: str | None) -> bool:
    if first is None or second is None:
        return False
    return _path_match_parts(first) == _path_match_parts(second)


def _handoff_relative_path(config: DispatchConfig) -> str | None:
    try:
        return config.handoff_full_path.relative_to(config.repo_root).as_posix()
    except ValueError:
        return None


def _protected_file_changes(
    config: DispatchConfig,
    before: ProtectedFileSnapshot,
) -> tuple[str, ...]:
    after = _capture_protected_snapshot(config)
    if after is None:
        return ()
    before_digests = dict(before.digests)
    after_digests = dict(after.digests)
    changed = {
        path
        for path in before_digests.keys() | after_digests.keys()
        if before_digests.get(path) != after_digests.get(path)
    }
    if before.head_sha and after.head_sha and before.head_sha != after.head_sha:
        # List the paths every new commit touched, not the net diff, so a
        # change that a later commit (or the working tree) reverts still
        # counts. -m includes merge commits, diffed against each parent.
        output = _run_git_stdout(
            config.repo_root,
            [
                "git",
                "log",
                "--name-only",
                "--format=",
                "-m",
                "--no-renames",
                f"{before.head_sha}..{after.head_sha}",
            ],
        )
        handoff_rel = _handoff_relative_path(config)
        for line in (output or "").splitlines():
            relative = line.strip().replace("\\", "/")
            if not relative or _same_repo_path(relative, handoff_rel):
                continue
            if _protected_path_matches(relative, config.protected_paths):
                changed.add(relative)
    return tuple(sorted(changed))


def _enforce_protected_files(
    *,
    config: DispatchConfig,
    before: ProtectedFileSnapshot | None,
    role_label: str,
    handoff_content: str,
    log: LogFn,
) -> bool:
    """Fail closed when a dispatch changed protected control files.

    Returns True when the handoff was rewritten to route to ``user``; the
    caller must not run further gates or dispatches for this cycle.
    """
    if before is None:
        return False
    changed = _protected_file_changes(config, before)
    if not changed:
        return False

    # Read scope the way the work-claim gate and dispatcher commit do, so a
    # Files in scope list written as bullets counts here too.
    packet = parse_work_packet_scope(handoff_content)
    violations = tuple(
        path
        for path in changed
        if packet is None or not path_is_in_scope(path, packet)
    )
    allowed = tuple(path for path in changed if path not in violations)
    if allowed:
        _log(
            log,
            "INFO",
            "Protected file changes listed in the Work Packet Files in scope: "
            f"{', '.join(allowed)}",
        )
    if not violations:
        return False

    error = (
        f"{PROTECTED_FILE_MODIFIED_PREFIX} {role_label} changed protected control "
        f"files outside its Work Packet scope: {', '.join(violations)}"
    )
    _log(log, "ERROR", error)
    _write_protected_file_escalation(
        config,
        role_label=role_label,
        violations=violations,
        error=error,
    )
    _log(
        log,
        "ERROR",
        f"Routed {config.handoff_path.as_posix()} to user. Review the protected "
        "file changes before resuming dispatch.",
    )
    return True


def _write_protected_file_escalation(
    config: DispatchConfig,
    *,
    role_label: str,
    violations: tuple[str, ...],
    error: str,
) -> None:
    previous_content = _read_required_text(config.handoff_full_path)
    reason = (
        f"{PROTECTED_FILE_MODIFIED_PREFIX} {role_label} changed protected control "
        "files outside its Work Packet scope."
    )
    lines = [
        "---",
        "next_agent: user",
        f"reason: {json.dumps(reason)}",
        # A canonical role, matching the work-claim gate's dispatcher-written
        # failure handoffs; `dispatcher` is not a protocol producer.
        "producer: validator",
        "status: escalate_to_user",
        "---",
        "",
        "# Dispatcher Escalation: Protected Control File Modified",
        "",
        error,
        "",
        "Changed protected paths:",
        "",
        *[f"- `{path}`" for path in violations],
        "",
        "The dispatcher stopped routing work. Review these changes before "
        "resuming. Revert them if they were not intended. If the role should "
        "edit them, list them under **Files in scope** in the Work Packet. "
        "Then rewrite this handoff with the next route.",
        "",
        "## Handoff Content After The Dispatch",
        "",
    ]
    if previous_content is None:
        lines.append("The handoff file was missing after the dispatch.")
    else:
        fence = "~~~~"
        while fence in previous_content:
            fence += "~"
        lines.extend([f"{fence}markdown", previous_content.rstrip("\n"), fence])
    config.handoff_full_path.parent.mkdir(parents=True, exist_ok=True)
    config.handoff_full_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _stop_on_auth_failure(
    config: DispatchConfig,
    *,
    role: str,
    exit_code: int,
    stdout: str | None,
    stderr: str | None,
    log: LogFn,
) -> bool:
    """Log how to sign in and return True when a failed run was a sign-in failure.

    A provider CLI that is not signed in fails every retry the same way, so
    the loop stops instead of spending its failure budget.
    """

    if exit_code == 0:
        return False
    detail = find_auth_failure(stdout, stderr)
    if detail is None:
        return False
    agent_config = config.agents.get(role)  # type: ignore[call-overload]
    for message in auth_failure_messages(
        role=role,
        provider=agent_config.provider if agent_config is not None else None,
        binary=agent_config.binary if agent_config is not None else None,
        detail=detail,
    ):
        _log(log, "ERROR", message)
    return True


def _record_failure(
    config: DispatchConfig,
    consecutive_failures: int,
    log: LogFn,
) -> int:
    _log(
        log,
        "WARN",
        f"Failure count: {consecutive_failures} / {config.max_consecutive_failures}",
    )
    if consecutive_failures >= config.max_consecutive_failures:
        _log(
            log,
            "ERROR",
            f"Hit maximum consecutive failures ({config.max_consecutive_failures}). Stopping dispatch loop.",
        )
        return consecutive_failures

    if config.poll_interval_seconds > 0:
        _log(
            log,
            "WARN",
            f"Pausing {config.poll_interval_seconds}s before next attempt...",
        )
    time.sleep(config.poll_interval_seconds)
    return consecutive_failures


def _run_validator(
    config: DispatchConfig,
    subagent_name: str,
    prompt: str,
    label: str,
    log: LogFn,
) -> ValidationResult | None:
    """Run a validator subagent and return its parsed diagnosis.

    Returns None when the validator changed a protected file, exited nonzero,
    printed nothing, or printed output that does not parse.
    """

    handoff_content = _read_required_text(config.handoff_full_path) or ""
    protected_before = _capture_protected_snapshot(config)
    result = _invoke_support_callable(
        subagent_name,
        prompt,
        role="validator",
        handoff_path=config.handoff_full_path,
        agent_config=config.agents["validator"],
        log=log,
        repo_root=config.repo_root,
        **_failover_kwargs(config, "validator"),
    )
    _log(log, "AGENT", f"{label} exited with code {result.exit_code}")
    if _enforce_protected_files(
        config=config,
        before=protected_before,
        role_label=f"validator ({subagent_name})",
        handoff_content=handoff_content,
        log=log,
    ):
        return None

    if result.exit_code != 0:
        if result.stderr.strip():
            _log(log, "WARN", f"{label} stderr: {result.stderr.strip()}")
        return None

    if not result.stdout.strip():
        _log(log, "WARN", f"{label} returned no stdout.")
        return None

    try:
        validation_result = parse_validation_output(result.stdout)
    except ValueError:
        _log(log, "WARN", f"{label} returned unparseable output.")
        return None

    _log(log, "AGENT", f"Handoff validation verdict: {validation_result.verdict}")
    for warning in validation_result.warnings:
        _log(log, "WARN", warning)
    for error in validation_result.errors:
        _log(log, "ERROR", error)
    return validation_result


def _run_unknown_route_validator(
    config: DispatchConfig, log: LogFn
) -> ValidationResult | None:
    _log(
        log,
        "AGENT",
        "No dispatchable route found. Invoking the validator role...",
    )
    return _run_validator(
        config,
        "handoff-validator",
        _render_prompt(HANDOFF_VALIDATOR_PROMPT_TEMPLATE, config.handoff_path),
        "Handoff-validator",
        log,
    )


def _stale_route_recovery_decision(
    route: RouteName,
    *,
    project_state_content: str | None,
    previous_cycle: Cycle | None,
    handoff_sha: str,
) -> RoutingDecision | None:
    if route != "finalizer":
        return None
    if previous_cycle is None:
        return None
    if previous_cycle.route != "finalizer":
        return None
    if previous_cycle.handoff_sha != handoff_sha:
        return None
    return RoutingDecision(
        route="planner",
        confidence="HIGH",
        source="stale_epic_close_recovery",
        reasoning=(
            "The prior finalizer cycle completed successfully, but HANDOFF.md "
            "still routes to finalizer with unchanged content."
        ),
        warnings=[],
    )


def _pre_dispatch_invalid_frontmatter_recovery(
    decision: RoutingDecision,
    handoff_content: str,
    *,
    handoff_path: Path,
) -> PreDispatchPlannerRecovery | None:
    if decision.source != "frontmatter.invalid":
        return None

    try:
        frontmatter = parse_handoff_frontmatter_text(handoff_content)
    except HandoffFrontmatterError:
        return None
    if frontmatter is None:
        return None

    next_agent = (frontmatter.next_agent or "").strip()
    if not next_agent or next_agent == "user":
        return None
    if next_agent not in CANONICAL_NEXT_AGENT_SET:
        return None

    warning = "; ".join(decision.warnings) or decision.reasoning
    if next_agent == "planner":
        return PreDispatchPlannerRecovery(
            decision=RoutingDecision(
                route="planner",
                confidence="HIGH",
                source="pre_dispatch_planner_frontmatter_repair",
                reasoning=(
                    "HANDOFF frontmatter is invalid but parseable and routes to "
                    "planner, so the planner is dispatched normally with a "
                    "frontmatter repair instruction before doing the requested "
                    "planning work."
                ),
                warnings=decision.warnings,
            ),
            additional_instruction=_render_prompt(
                PRE_DISPATCH_INVALID_PLANNER_FRONTMATTER_TEMPLATE,
                handoff_path,
                warning=warning,
            ),
        )

    return PreDispatchPlannerRecovery(
        decision=RoutingDecision(
            route="planner",
            confidence="HIGH",
            source="pre_dispatch_frontmatter_recovery",
            reasoning=(
                "HANDOFF frontmatter is invalid but parseable and names a "
                "recognized next_agent, so the planner is repairing the "
                "handoff before normal dispatch continues."
            ),
            warnings=decision.warnings,
        ),
        additional_instruction=_render_prompt(
            PRE_DISPATCH_INVALID_FRONTMATTER_PLANNER_RECOVERY_TEMPLATE,
            handoff_path,
            next_agent=next_agent,
            warning=warning,
        ),
    )


def _log_handoff_scope(handoff_content: str, log: LogFn) -> None:
    try:
        frontmatter = parse_handoff_frontmatter_text(handoff_content)
    except HandoffFrontmatterError:
        return
    if frontmatter is None:
        return

    parts: list[str] = []
    if frontmatter.epic_id:
        parts.append(f"epic={frontmatter.epic_id}")
    story_summary = _handoff_story_summary(
        story_id=frontmatter.story_id,
        story_title=frontmatter.story_title,
    )
    if story_summary:
        parts.append(f"story={story_summary}")
    if frontmatter.remaining_stories:
        parts.append(f"remaining={'; '.join(frontmatter.remaining_stories)}")
    if not parts:
        return

    _log(log, "INFO", f"Handoff scope: {'; '.join(parts)}")


def _parse_frontmatter_or_none(handoff_content: str):
    try:
        return parse_handoff_frontmatter_text(handoff_content)
    except HandoffFrontmatterError:
        return None


def _handoff_story_id(handoff_content: str) -> str | None:
    frontmatter = _parse_frontmatter_or_none(handoff_content)
    return frontmatter.story_id if frontmatter is not None else None


def _story_start_sha(
    config: DispatchConfig, handoff_content: str, head: str | None
) -> str | None:
    """Return where the handoff's story started, or ``head`` when unknown.

    The story bases kept in memory reset when the dispatcher restarts
    mid-story, and HEAD may already hold the story's earlier commits. Outside
    ``local`` publish mode the saved story branch base outlives the run.
    """

    if head is None or config.publish.mode == "local":
        return head
    saved = saved_story_base(config.repo_root, _handoff_story_id(handoff_content))
    return saved or head


def _claude_protected_scope_pause_reason(
    config: DispatchConfig, route: RouteName, handoff_content: str
) -> str | None:
    """Explain why a Claude implementer cannot do this Work Packet, or None.

    Claude Code denies writes under any ``.claude`` directory in ``dontAsk``
    mode, before it reads the allow rules, so dispatching the packet would
    only end in ``blocked_missing_context``. ``unsafe_bypass`` launches with
    ``--dangerously-skip-permissions``, which allows those writes.
    """

    if route not in {"backend", "frontend"}:
        return None
    if route == "frontend" and config.use_manual_frontend:
        return None
    agent_config = config.agents[route]
    if agent_config.provider != "claude" or agent_config.unsafe_bypass:
        return None
    packet = parse_work_packet_scope(handoff_content)
    if packet is None:
        return None
    entries = claude_protected_scope_entries(packet)
    if not entries:
        return None
    return (
        f"CLAUDE PROTECTED PATHS IN SCOPE: the {route} role runs Claude Code "
        "in --permission-mode dontAsk, which denies every write under a "
        "`.claude` directory, so it cannot change these Files in scope: "
        + ", ".join(f"`{entry}`" for entry in entries)
        + ". Either an operator applies those edits and the planner drops them "
        "from the Work Packet, or the planner moves them to a role whose "
        "provider is not claude. Update HANDOFF.md; dispatch will resume after "
        "the file changes."
    )


def _work_claim_gate_applies(config: DispatchConfig, route: RouteName) -> bool:
    return config.enforce_work_packet and route in {"backend", "frontend"}


def _with_commit_mode_instruction(
    config: DispatchConfig,
    additional_instruction: str | None,
    *,
    instruction: str = COMMIT_MODE_INSTRUCTION,
) -> str | None:
    """Tell implementers and the planner not to touch ``.git`` when the
    dispatcher commits."""

    if config.commit_mode != "dispatcher":
        return additional_instruction
    if additional_instruction and additional_instruction.strip():
        return "\n\n".join((instruction, additional_instruction.strip()))
    return instruction


def _commit_for_implementer(
    *,
    config: DispatchConfig,
    route: RouteName,
    assignment_content: str,
    pre_dispatch_state: PreDispatchState | None,
    log: LogFn,
    story_base_sha: str | None = None,
) -> DispatcherCommitResult | None:
    """Commit an implementer's in-scope changes in ``dispatcher`` commit mode.

    Runs before handoff validation so the recorded ``scope_sha`` and evidence
    SHA are real. Anything left uncommitted is reported by the work-claim gate.
    When a retry changes no in-scope file, for example because it only reverted
    a forbidden one, but an earlier dispatch of the story already committed
    work since ``story_base_sha``, HEAD is recorded as the claim.
    Returns None when the commit step does not apply.
    """

    if (
        config.commit_mode != "dispatcher"
        or route not in {"backend", "frontend"}
        or pre_dispatch_state is None
    ):
        return None
    claim_content = _read_required_text(config.handoff_full_path)
    if claim_content is None:
        return None
    result = commit_implementer_changes(
        repo_root=config.repo_root,
        handoff_path=config.handoff_path,
        assignment_content=assignment_content,
        claim_content=claim_content,
        pre_dispatch_state=pre_dispatch_state,
        role=route,
    )
    head = _git_head(config.repo_root) if result.nothing_to_commit else None
    if result.error is not None:
        _log(log, "ERROR", f"Dispatcher commit for {route} failed: {result.error}.")
    elif (
        head is not None
        and story_base_sha is not None
        and head != story_base_sha
        and _story_committed_work(config, story_base_sha, head)
    ):
        config.handoff_full_path.write_text(
            stamp_commit_sha(claim_content, head),
            encoding="utf-8",
        )
        _log(
            log,
            "AGENT",
            f"Dispatcher commit for {route}: nothing new to commit; recorded the "
            f"story's earlier commit {head[:12]} as scope_sha.",
        )
    elif result.sha is None:
        _log(
            log,
            "INFO",
            f"Dispatcher commit for {route} skipped: {result.skipped_reason}.",
        )
    else:
        config.handoff_full_path.write_text(
            stamp_commit_sha(claim_content, result.sha),
            encoding="utf-8",
        )
        _log(
            log,
            "AGENT",
            f"Dispatcher committed {len(result.committed)} in-scope file(s) for "
            f"{route} as {result.sha[:12]}: {', '.join(result.committed)}.",
        )
    if result.left_uncommitted:
        _log(
            log,
            "WARN",
            "Left uncommitted (outside Files in scope or out of bounds): "
            f"{', '.join(result.left_uncommitted)}. The work-claim gate will "
            "report them.",
        )
    return result


def _story_committed_work(
    config: DispatchConfig, story_base_sha: str, head: str
) -> bool:
    changed = git_changed_files(config.repo_root, story_base_sha, head)
    handoff = config.handoff_path.as_posix()
    return changed is not None and any(path != handoff for path in changed)


def _scope_sha_missing_for_gate(
    *,
    config: DispatchConfig,
    route: RouteName,
    commit_result: DispatcherCommitResult | None,
    validation_result: ValidationResult,
) -> bool:
    """True when every validation error is a missing SHA that the dispatcher
    left out because it committed nothing for a claim the work-claim gate is
    about to check."""

    if (
        commit_result is None
        or commit_result.sha is not None
        or commit_result.error is not None
        or not _work_claim_gate_applies(config, route)
    ):
        return False
    if not validation_result.errors or not all(
        error.startswith(("frontmatter_scope_sha_missing:", "sha_missing:"))
        for error in validation_result.errors
    ):
        return False
    claim_content = _read_required_text(config.handoff_full_path)
    frontmatter = (
        _parse_frontmatter_or_none(claim_content) if claim_content else None
    )
    return (
        frontmatter is not None
        and _handoff_status(frontmatter.status) == HandoffStatus.READY_FOR_REVIEW
    )


def _run_work_claim_gate(
    *,
    config: DispatchConfig,
    route: RouteName,
    previous_agent: str,
    assignment_content: str,
    pre_dispatch_state: PreDispatchState | None,
    log: LogFn,
    story_base_sha: str | None = None,
) -> bool:
    """Verify a ``ready_for_review`` claim deterministically before audit.

    Returns True when the claim failed and the handoff was rewritten into a
    bounce, False when the gate passed or did not apply.
    """

    if not _work_claim_gate_applies(config, route) or pre_dispatch_state is None:
        return False
    claim_content = _read_required_text(config.handoff_full_path)
    if claim_content is None:
        return False
    frontmatter = _parse_frontmatter_or_none(claim_content)
    if (
        frontmatter is None
        or _handoff_status(frontmatter.status) != HandoffStatus.READY_FOR_REVIEW
    ):
        return False

    _log(log, "AGENT", f"Running deterministic work-claim gate for {previous_agent}...")
    result = verify_work_claim(
        repo_root=config.repo_root,
        handoff_path=config.handoff_path,
        assignment_content=assignment_content,
        claim_content=claim_content,
        pre_dispatch_state=pre_dispatch_state,
        verification_timeout_seconds=config.work_packet_verification_timeout_seconds,
        scanners=config.scanners,
        require_new_commit=True,
        dispatcher_commit_mode=config.commit_mode == "dispatcher",
        story_base_sha=story_base_sha,
        env_passthrough=config.work_claim_env_passthrough,
        verification_allowed_prefixes=config.verification_allowed_prefixes,
        log=log,
    )
    _log_work_claim_result(result, log)
    if result.passed:
        _log(log, "AGENT", f"Work-claim gate PASSED for {previous_agent}.")
        # Record the pass so the auditor does not re-run the verification.
        config.handoff_full_path.write_text(
            record_passed_verification(claim_content, result),
            encoding="utf-8",
        )
        if config.publish.mode != "local":
            save_review_responses(config.repo_root, claim_content)
        return False

    for error in result.errors:
        _log(log, "ERROR", error)
    bounce_route = bounce_route_for(result, implementer=route)
    count = record_status_transition(
        frontmatter.story_id,
        _handoff_status(bounce_route.status),
    )
    bounce_content = build_bounce_handoff(
        claim_content=claim_content,
        result=result,
        implementer=route,
        route=bounce_route,
        bounce_count=count if frontmatter.story_id else None,
    )
    config.handoff_full_path.write_text(bounce_content, encoding="utf-8")
    _log(
        log,
        "AGENT",
        f"Work-claim gate FAILED for {previous_agent}; HANDOFF.md now routes to "
        f"{bounce_route.next_agent} with status {bounce_route.status} "
        f"(checks: {', '.join(result.failed_prefixes)}).",
    )
    if frontmatter.story_id and bounce_route.status == "verified_fail":
        _log(
            log,
            "INFO",
            f"Bounce count for story_id={frontmatter.story_id} is now {count}.",
        )
    return True


def _log_work_claim_result(result: WorkClaimResult, log: LogFn) -> None:
    _log(
        log,
        "INFO",
        "work-claim: pre-dispatch HEAD "
        f"{_short_sha_preview(result.pre_dispatch_head or 'unknown')}; HEAD "
        f"{_short_sha_preview(result.head_sha or 'unknown')}; scope_sha "
        f"{result.scope_sha or 'missing'}",
    )
    _log(
        log,
        "INFO",
        "work-claim: changed files since dispatch: "
        f"{', '.join(result.changed_files) or 'none'}",
    )
    for check in result.checks:
        _log(log, "INFO", f"work-claim: {check.outcome} {check.name}: {check.detail}")
    for scanner_run in result.scanner_runs:
        if scanner_run.missing and not scanner_run.required:
            _log(
                log,
                "WARN",
                f"work-claim: optional scanner {scanner_run.name} skipped: "
                f"{scanner_run.detail}",
            )
        if scanner_run.run is not None:
            for line in scanner_run.run.output_tail_lines():
                _log(log, "INFO", f"work-claim | {scanner_run.name} | {line}")
    for run in result.verification_runs:
        _log(
            log,
            "INFO",
            f"work-claim: verification command `{run.command}` {run.describe_outcome()}",
        )
        for line in run.output_tail_lines():
            _log(log, "INFO", f"work-claim | {line}")


def _record_post_dispatch_status_transition(
    *,
    config: DispatchConfig,
    log: LogFn,
) -> None:
    handoff_content = _read_required_text(config.handoff_full_path)
    if handoff_content is None:
        return
    frontmatter = _parse_frontmatter_or_none(handoff_content)
    if frontmatter is None:
        return
    status = _handoff_status(frontmatter.status)
    state_path = _bounce_state_path(config)
    try:
        count = record_status_transition(
            frontmatter.story_id,
            status,
            state_path=state_path,
        )
    except OSError as exc:
        # The in-memory table is updated before the file write, so the
        # breaker still works for this process even if it cannot be made
        # restart-safe.
        _log(
            log,
            "WARN",
            f"Bounce count could not be persisted to {state_path}: {exc}",
        )
        count = bounce_count(frontmatter.story_id) if frontmatter.story_id else 0
    if status == HandoffStatus.VERIFIED_FAIL and frontmatter.story_id:
        _log(
            log,
            "INFO",
            f"Bounce count for story_id={frontmatter.story_id} is now {count}.",
        )
    if (
        status
        in {
            HandoffStatus.VERIFIED_PASS,
            HandoffStatus.ESCALATE_TO_USER,
        }
        and frontmatter.story_id
    ):
        _log(
            log,
            "INFO",
            f"Bounce count reset for story_id={frontmatter.story_id}.",
        )


def _accepted_approval(config: DispatchConfig) -> bool:
    """Return whether the handoff the dispatcher just accepted approves the story.

    Called only after validation, the work-claim gate, and recovery checks let
    the handoff stand, so a rejected ``verified_pass`` keeps the audit base.
    """

    handoff_content = _read_required_text(config.handoff_full_path)
    if handoff_content is None:
        return False
    frontmatter = _parse_frontmatter_or_none(handoff_content)
    return (
        frontmatter is not None
        and _handoff_status(frontmatter.status) == HandoffStatus.VERIFIED_PASS
    )


def _bounce_state_path(config: DispatchConfig) -> Path:
    return (config.repo_root / BOUNCE_COUNT_STATE_PATH).resolve()


def _restore_bounce_counts(config: DispatchConfig, log: LogFn) -> None:
    state_path = _bounce_state_path(config)
    restored = seed_bounce_counts(state_path)
    if not restored:
        return
    summary = "; ".join(
        f"{story_id}={count}" for story_id, count in sorted(restored.items())
    )
    _log(log, "INFO", f"Restored bounce counts from {state_path}: {summary}")


def _handoff_status(raw_status: str | None) -> HandoffStatus | None:
    if raw_status is None:
        return None
    try:
        return HandoffStatus(raw_status)
    except ValueError:
        return None


def _handoff_story_summary(
    *,
    story_id: str | None,
    story_title: str | None,
) -> str | None:
    if story_id and story_title:
        return f"{story_id} ({story_title})"
    return story_id or story_title


def _run_stale_route_validator(
    config: DispatchConfig,
    route: RouteName,
    log: LogFn,
) -> ValidationResult | None:
    _log(
        log,
        "AGENT",
        f"Stale HANDOFF detected for route {route}. Invoking the validator role...",
    )
    return _run_validator(
        config,
        "handoff-validator",
        _render_prompt(
            STALE_ROUTE_VALIDATOR_PROMPT_TEMPLATE,
            config.handoff_path,
            route=route,
        ),
        "Handoff-validator",
        log,
    )


def _run_epic_close_validator(
    config: DispatchConfig,
    parse_error: str,
    log: LogFn,
) -> ValidationResult | None:
    _log(
        log,
        "AGENT",
        "finalizer produced unparseable ledger-updater output. Invoking the validator role...",
    )
    return _run_validator(
        config,
        "handoff-validator",
        _render_prompt(
            EPIC_CLOSE_VALIDATOR_PROMPT_TEMPLATE,
            config.handoff_path,
            parse_error=parse_error,
        ),
        "Handoff-validator",
        log,
    )


def _run_low_confidence_route_validator(
    config: DispatchConfig,
    decision: RoutingDecision,
    log: LogFn,
) -> ValidationResult | None:
    _log(
        log,
        "AGENT",
        f"Routing decision for {decision.route} is only {decision.confidence} confidence. Invoking the validator role before dispatch...",
    )
    return _run_validator(
        config,
        "handoff-validator",
        _render_prompt(
            LOW_CONFIDENCE_ROUTE_VALIDATOR_PROMPT_TEMPLATE,
            config.handoff_path,
            confidence=decision.confidence,
            route=decision.route,
            reasoning=decision.reasoning,
        ),
        "Handoff-validator",
        log,
    )


def _run_self_loop_validator(
    *,
    config: DispatchConfig,
    previous_agent: str,
    self_loop_kind: str,
    log: LogFn,
) -> ValidationResult | None:
    if self_loop_kind == "planner":
        prompt = _render_prompt(
            PLANNER_SELF_LOOP_VALIDATOR_PROMPT_TEMPLATE,
            config.handoff_path,
        )
        message = "The planner produced a self-loop. Invoking the validator role..."
    else:
        prompt = _render_prompt(
            AGENT_SELF_LOOP_VALIDATOR_PROMPT_TEMPLATE,
            config.handoff_path,
            previous_agent=previous_agent,
        )
        message = (
            f"{previous_agent} produced a self-loop. Invoking the validator role..."
        )

    _log(log, "AGENT", message)
    return _run_validator(
        config,
        "handoff-validator",
        prompt,
        "Handoff-validator",
        log,
    )


def _run_post_dispatch_missing_route_validator(
    *,
    config: DispatchConfig,
    previous_agent: str,
    log: LogFn,
) -> ValidationResult | None:
    _log(
        log,
        "AGENT",
        f"{previous_agent} produced a handoff without a dispatchable route. Invoking the validator role...",
    )
    return _run_validator(
        config,
        "handoff-validator",
        _render_prompt(
            POST_DISPATCH_MISSING_ROUTE_VALIDATOR_PROMPT_TEMPLATE,
            config.handoff_path,
            previous_agent=previous_agent,
        ),
        "Handoff-validator",
        log,
    )


def _pause_until_handoff_changes(
    config: DispatchConfig,
    *,
    handoff_sha: str,
    reason: str,
    log: LogFn,
) -> bool:
    _log(log, "PAUSE", reason)
    if config.poll_interval_seconds <= 0:
        return False

    _log(
        log,
        "PAUSE",
        f"Waiting for HANDOFF.md to change; polling every {config.poll_interval_seconds}s.",
    )
    while True:
        time.sleep(config.poll_interval_seconds)
        handoff_content = _read_required_text(config.handoff_full_path)
        if handoff_content is None:
            _log(
                log,
                "WARN",
                f"Handoff file not found at: {config.handoff_full_path}",
            )
            continue

        if _content_sha(handoff_content) == handoff_sha:
            continue

        _log(log, "PAUSE", "Detected HANDOFF.md change. Resuming dispatch loop.")
        return True


def _operator_stdin_is_interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _operator_input_stream() -> TextIO:
    return sys.stdin


class _OperatorPromptState:
    """Holds the one operator input reader a run_loop call may create."""

    def __init__(self) -> None:
        self._reader: OperatorInputReader | None = None

    def reader(self) -> OperatorInputReader:
        if self._reader is None:
            self._reader = OperatorInputReader(_operator_input_stream())
        return self._reader


PauseOutcomeKind = Literal["resume", "stop", "quit", "retry", "planner"]


@dataclass(frozen=True, slots=True)
class PauseOutcome:
    kind: PauseOutcomeKind
    pending_planner_recovery: PendingPlannerRecovery | None = None

    @property
    def stop(self) -> bool:
        """True when run_loop should return 0."""

        return self.kind in {"stop", "quit"}


def _operator_prompt_enabled(config: DispatchConfig) -> bool:
    return (
        config.interactive_pause
        and not config.dry_run
        and config.poll_interval_seconds > 0
        and _operator_stdin_is_interactive()
    )


def _pause_at_dispatcher_pause(
    config: DispatchConfig,
    *,
    handoff_sha: str,
    reason: str,
    log: LogFn,
    operator_prompt: _OperatorPromptState,
) -> PauseOutcome:
    """Pause for a HANDOFF.md edit, or for the operator prompt at a TTY."""

    if not _operator_prompt_enabled(config):
        if _pause_until_handoff_changes(
            config,
            handoff_sha=handoff_sha,
            reason=reason,
            log=log,
        ):
            return PauseOutcome(kind="resume")
        return PauseOutcome(kind="stop")

    reader = operator_prompt.reader()
    if reader.console_unavailable:
        _log(
            log,
            "WARN",
            "Operator prompt unavailable: no console input for this terminal; "
            "waiting for HANDOFF.md to change.",
        )
        if _pause_until_handoff_changes(
            config,
            handoff_sha=handoff_sha,
            reason=reason,
            log=log,
        ):
            return PauseOutcome(kind="resume")
        return PauseOutcome(kind="stop")

    _log(log, "PAUSE", reason)
    result = wait_for_operator_or_handoff_change(
        reader,
        config.handoff_full_path,
        handoff_sha,
        config.poll_interval_seconds,
        log,
        hash_content=_content_sha,
    )
    if isinstance(result, HandoffChanged):
        _log(log, "PAUSE", "Detected HANDOFF.md change. Resuming dispatch loop.")
        return PauseOutcome(kind="resume")
    if result.kind == "quit":
        _log(log, "PAUSE", "The operator stopped the dispatcher from the prompt.")
        return PauseOutcome(kind="quit")
    if result.kind == "retry":
        _log(log, "PAUSE", "The operator asked to retry; re-running the cycle.")
        return PauseOutcome(kind="retry")

    current_handoff_content = _read_required_text(config.handoff_full_path) or ""
    if result.kind == "role" and result.role is not None:
        role = cast(RouteName, result.role)
        if result.text is None:
            operator_request = OPERATOR_ROLE_NO_MESSAGE_REQUEST
            summary = "no message"
        else:
            operator_request = OPERATOR_ROLE_INSTRUCTION_PREFIX + result.text
            summary = "the operator's message"
        # The audit fan-out reviewer is read-only and answers in JSON, so it
        # must not be told to write a normal handback.
        template = (
            OPERATOR_ROLE_AUDIT_FAN_OUT_INSTRUCTION_TEMPLATE
            if role == "auditor" and config.audit.security_enabled
            else OPERATOR_ROLE_INSTRUCTION_TEMPLATE
        )
        role_pending = PendingPlannerRecovery(
            handoff_sha=_content_sha(current_handoff_content),
            additional_instruction=_render_prompt(
                template,
                config.handoff_path,
                pause_reason=reason,
                operator_request=operator_request,
            ),
            kind="operator_role",
            route=role,
        )
        _log(
            log,
            "PAUSE",
            f"Operator prompt: scheduling a {role} dispatch with {summary}.",
        )
        return PauseOutcome(kind="planner", pending_planner_recovery=role_pending)

    if result.text is None:
        operator_request = OPERATOR_BEST_JUDGMENT_REQUEST
        summary = "a best judgment instruction"
    else:
        operator_request = OPERATOR_GUIDANCE_PREFIX + result.text
        summary = "the operator's guidance"
    pending = PendingPlannerRecovery(
        handoff_sha=_content_sha(current_handoff_content),
        additional_instruction=_render_prompt(
            OPERATOR_PAUSE_INSTRUCTION_TEMPLATE,
            config.handoff_path,
            pause_reason=reason,
            operator_request=operator_request,
        ),
        kind="operator",
    )
    _log(
        log,
        "PAUSE",
        f"Operator prompt: scheduling a planner dispatch with {summary}.",
    )
    return PauseOutcome(kind="planner", pending_planner_recovery=pending)


def _pause_recovery_available(
    config: DispatchConfig, used: bool, log: LogFn
) -> bool:
    if config.pause_recovery != "planner" or config.dry_run or used:
        return False
    # The pause validator may have rewritten HANDOFF.md to route to `user`,
    # for example the protected-file escalation. A user route never gets
    # automatic recovery, so the pause stands for a person to review.
    current_content = _read_required_text(config.handoff_full_path)
    frontmatter = (
        _parse_frontmatter_or_none(current_content)
        if current_content is not None
        else None
    )
    if frontmatter is not None and (frontmatter.next_agent or "").strip() == "user":
        _log(
            log,
            "PAUSE",
            "HANDOFF.md now routes to user; not scheduling planner recovery.",
        )
        return False
    return True


def _format_pause_diagnosis(result: ValidationResult | None) -> str:
    if result is None:
        return PAUSE_RECOVERY_NO_DIAGNOSIS
    errors = "; ".join(result.errors) if result.errors else "none"
    warnings = "; ".join(result.warnings) if result.warnings else "none"
    return f"verdict {result.verdict}; errors: {errors}; warnings: {warnings}."


def _schedule_pause_recovery(
    config: DispatchConfig,
    *,
    pause_reason: str,
    validation_result: ValidationResult | None,
    log: LogFn,
) -> PendingPlannerRecovery:
    _log(
        log,
        "PAUSE",
        f"{pause_reason} Pause recovery: scheduling one bounded planner recovery.",
    )
    current_handoff_content = _read_required_text(config.handoff_full_path) or ""
    return PendingPlannerRecovery(
        handoff_sha=_content_sha(current_handoff_content),
        additional_instruction=_render_prompt(
            PAUSE_RECOVERY_INSTRUCTION_TEMPLATE,
            config.handoff_path,
            pause_reason=pause_reason,
            diagnosis=_format_pause_diagnosis(validation_result),
        ),
        kind="pause",
    )


def _self_loop_kind(result: ValidationResult) -> str | None:
    if any(error.startswith("planner_self_loop:") for error in result.errors):
        return "planner"
    if any(error.startswith("agent_self_loop:") for error in result.errors):
        return "agent"
    return None


def _should_schedule_planner_recovery(
    result: ValidationResult,
    previous_agent: str,
) -> bool:
    if previous_agent == "planner":
        return False
    return _has_missing_route_error(result)


def _has_missing_route_error(result: ValidationResult) -> bool:
    return any(
        error.startswith("routing_instruction_missing:") for error in result.errors
    )


def _has_rationalization_warning(result: ValidationResult) -> bool:
    return any(
        warning.startswith("RATIONALIZATION_DETECTED:") for warning in result.warnings
    )


def _format_rationalization_warnings(result: ValidationResult) -> str:
    return "; ".join(
        warning
        for warning in result.warnings
        if warning.startswith("RATIONALIZATION_DETECTED:")
    )


def _self_loop_terminal_status(
    previous_agent: str,
    self_loop_kind: str,
) -> str:
    if self_loop_kind == "planner":
        return f"Post-dispatch gate PAUSED for {previous_agent}; the planner routed the handoff back to itself."
    return f"Post-dispatch gate PAUSED for {previous_agent}; HANDOFF routed work back to the same agent."


def _self_loop_pause_reason(
    previous_agent: str,
    self_loop_kind: str,
) -> str:
    if self_loop_kind == "planner":
        return "The planner routed HANDOFF.md back to itself. Update the handoff; dispatch will resume after the file changes."
    return f"{previous_agent} routed HANDOFF.md back to itself. Update the handoff; dispatch will resume after the file changes."


def _missing_route_pause_reason(previous_agent: str) -> str:
    return f"{previous_agent} produced HANDOFF.md without a dispatchable route. Update the handoff; dispatch will resume after the file changes."


def _log_dispatch_completion(
    *,
    handoff_path: Path,
    previous_agent: str,
    dispatch_result: DispatchResult,
    prior_handoff_content: str,
    prior_handoff_sha: str,
    log: LogFn,
) -> None:
    _log(log, "INFO", f"{previous_agent} exited with code {dispatch_result.exit_code}")

    current_handoff_content = _read_required_text(handoff_path)
    if current_handoff_content is None:
        _log(
            log,
            "WARN",
            f"{previous_agent} completed, but the handoff file is missing at {handoff_path}.",
        )
        return

    if _content_sha(current_handoff_content) != prior_handoff_sha:
        _log(log, "INFO", f"{previous_agent} updated {handoff_path} (hash changed)")

    added_shas = _new_commit_shas(prior_handoff_content, current_handoff_content)
    if added_shas:
        _log(
            log,
            "INFO",
            "New SHA(s) found in handoff file: "
            f"{_short_sha_preview(added_shas[0])} ({len(added_shas)} added)",
        )
        return

    current_shas = _extract_commit_shas(current_handoff_content)
    if current_shas:
        _log(
            log,
            "WARN",
            "No new SHAs in handoff file -- "
            f"all {len(current_shas)} SHA(s) were already present before dispatch",
        )


def _log_validation(
    result: ValidationResult,
    previous_agent: str,
    log: LogFn,
    *,
    override_terminal_status: str | None = None,
) -> None:
    _log(log, "AGENT", f"Handoff validation verdict: {result.verdict}")
    for warning in result.warnings:
        _log(log, "WARN", warning)
    for error in result.errors:
        _log(log, "ERROR", error)
    if override_terminal_status is not None:
        _log(log, "AGENT", override_terminal_status)
        return
    if result.verdict == "NO":
        _log(log, "AGENT", f"Post-dispatch gate FAILED for {previous_agent}.")
        return
    if result.verdict == "WARNINGS-ONLY":
        _log(
            log,
            "AGENT",
            f"Post-dispatch gate PASSED WITH WARNINGS for {previous_agent}.",
        )
        return
    _log(log, "AGENT", f"Post-dispatch gate PASSED for {previous_agent}.")


def _log_dry_run(config: DispatchConfig, route: RouteName, log: LogFn) -> None:
    if route == "backend":
        _log(log, "DISPATCH", "[DRY RUN] Would dispatch backend")
        return

    if route == "planner":
        _log(log, "DISPATCH", "[DRY RUN] Would dispatch planner")
        return

    if route == "frontend":
        target = (
            "manual frontend pause"
            if config.use_manual_frontend
            else "configured frontend provider"
        )
        _log(log, "DISPATCH", f"[DRY RUN] Would dispatch {target} for frontend work")
        return

    if route == "auditor":
        if config.audit.security_enabled:
            _log(
                log,
                "DISPATCH",
                "[DRY RUN] Would dispatch audit fan-out (quality + security reviewers)",
            )
            return
        _log(log, "DISPATCH", "[DRY RUN] Would dispatch auditor for audit")
        return

    if route == "validator":
        _log(
            log,
            "DISPATCH",
            "[DRY RUN] Would dispatch auditor for misroute clarification",
        )
        return

    if route == "finalizer":
        _log(
            log,
            "DISPATCH",
            "[DRY RUN] Would handle finalizer with auto ledger update",
        )
        return


def _log_startup_banner(config: DispatchConfig, log: LogFn) -> None:
    frontend_mode = (
        "manual frontend pause"
        if config.use_manual_frontend
        else "configured frontend provider"
    )

    _log(log, "INFO", "================================================")
    _log(log, "INFO", "  llm-handoff dispatch loop starting           ")
    _log(log, "INFO", "  Single-dispatch-per-cycle, HANDOFF.md routes  ")
    _log(log, "INFO", "================================================")
    _log(log, "INFO", f"Repo root:          {config.repo_root}")
    _log(
        log,
        "INFO",
        "Smart router:       ON (frontmatter primary; legacy fallback warning)",
    )
    _log(log, "INFO", "Handoff validation: ON (hard gate)")
    if config.enforce_work_packet:
        _log(
            log,
            "INFO",
            "Work-claim gate:    ON (runs the Work Packet verification command)",
        )
        if config.verification_allowed_prefixes:
            _log(
                log,
                "INFO",
                "Verification allowlist: "
                + ", ".join(config.verification_allowed_prefixes),
            )
        else:
            _log(
                log,
                "WARN",
                "Verification allowlist: OFF (verification_allowed_prefixes is "
                "empty; any Work Packet Verification command runs)",
            )
    else:
        _log(
            log,
            "WARN",
            "Work-claim gate:    OFF (enforce_work_packet: false; "
            f"work_packet_opt_out_reason: {config.work_packet_opt_out_reason})",
        )
    if config.scanners:
        scanner_names = ", ".join(scanner.name for scanner in config.scanners)
        if config.enforce_work_packet:
            _log(log, "INFO", f"Scanners:           {scanner_names}")
        else:
            _log(
                log,
                "WARN",
                f"Scanners configured ({scanner_names}) but enforce_work_packet is "
                "false; they will not run.",
            )
    _log(log, "INFO", "Finalizer route:    ON")
    _log(
        log,
        "INFO",
        "Pause recovery:     planner (one bounded planner recovery per pause streak)"
        if config.pause_recovery == "planner"
        else "Pause recovery:     off (pauses wait for a HANDOFF.md edit)",
    )
    _log(
        log,
        "INFO",
        "Interactive pause:  enabled (operator prompt at pauses; runs only "
        "when stdin is a TTY)"
        if config.interactive_pause
        else "Interactive pause:  disabled (pauses wait for a HANDOFF.md edit)",
    )
    _log(log, "INFO", "Chaining:           NONE (single dispatch per cycle)")
    _log(log, "INFO", f"Frontend agent:     {frontend_mode}")
    _log(
        log,
        "INFO",
        "backend session:      MANAGED RESUME (persisted thread id)"
        if config.backend_resume_enabled
        else "backend session:      STATELESS (new session per dispatch)",
    )
    _log(
        log,
        "INFO",
        "Planner session:    MANAGED RESUME (in-memory session id)"
        if config.planner_resume_enabled
        else "Planner session:    STATELESS (new session per dispatch)",
    )
    _log(
        log,
        "INFO",
        "Planner API env:    PRESERVE configured provider API key"
        if config.planner_api_key_env_enabled
        else "Planner API env:    STRIP configured provider API keys",
    )
    if config.audit.security_enabled:
        _log(
            log,
            "INFO",
            "Audit reviewers:    quality + security (read-only fan-out; "
            f"security provider: {config.audit.security_provider or 'auto'}; "
            f"security model: {describe_security_reviewer_model(config)}; "
            f"block severity: {config.audit.block_severity})",
        )
    else:
        _log(
            log,
            "WARN",
            "Audit reviewers:    quality only; the security reviewer is OFF "
            "(audit.security_opt_out_reason: "
            f"{config.audit.security_opt_out_reason})",
        )
    _log_launch_posture(config, log)
    _log(
        log,
        "INFO",
        f"Protected files:    ON ({len(config.protected_paths)} patterns; "
        "out-of-scope changes route to user)"
        if config.protected_paths
        else "Protected files:    OFF (protected_paths is empty)",
    )
    publish = config.publish
    if publish.mode == "local":
        _log(log, "INFO", "Publish:            local (commits stay on the current branch)")
    else:
        detail = f"story branches {publish.branch_prefix}*"
        if publish.mode == "pr":
            detail += (
                f"; push to {publish.remote} and open a PR after the audit passes; "
                + ("auto-merge" if publish.auto_merge else "operator merges")
                + ("; waits for PR checks" if publish.wait_for_checks else "")
                + (
                    "; waits for the "
                    + ", ".join(publish.wait_for_reviewers)
                    + f" review (up to {publish.review_timeout_seconds}s, "
                    f"{publish.max_review_rounds} correction round(s))"
                    if publish.wait_for_reviewers
                    else ""
                )
            )
        else:
            detail += "; merge with a report commit after the audit passes"
        _log(log, "INFO", f"Publish:            {publish.mode} ({detail})")


def _dispatched_roles(config: DispatchConfig) -> list[str]:
    roles = list(config.agents)
    if config.use_manual_frontend:
        roles = [role for role in roles if role != "frontend"]
    return roles


def _launch_agents(config: DispatchConfig) -> list[tuple[str, str, AgentConfig]]:
    """Return (label, config key, agent config) for every agent a run may launch."""

    agents: list[tuple[str, str, AgentConfig]] = []
    for role in _dispatched_roles(config):
        agent_config = config.agents[role]
        agents.append((role, f"agents.{role}", agent_config))
        if agent_config.fallback is not None:
            agents.append(
                (
                    f"{role} fallback",
                    f"agents.{role}.fallback",
                    agent_config.fallback,
                )
            )
    return agents


def _roles_missing_gemini_sandbox(config: DispatchConfig) -> list[str]:
    gemini_roles = [
        label
        for label, _key, agent_config in _launch_agents(config)
        if agent_config.provider == "gemini" and not agent_config.unsafe_bypass
    ]
    if not gemini_roles or gemini_provider.detect_gemini_sandbox_runtime():
        return []
    return gemini_roles


def _launch_args_for_role(config: DispatchConfig, role: str) -> list[str]:
    return _launch_args_for_agent(config.agents[role])


def _launch_args_for_agent(agent_config: AgentConfig) -> list[str]:
    if agent_config.provider == "claude":
        return claude_provider.claude_permission_args(agent_config)
    if agent_config.provider == "codex":
        return codex_provider.codex_permission_args(
            unsafe_bypass=agent_config.unsafe_bypass
        )
    if agent_config.provider == "gemini":
        return gemini_provider.gemini_sandbox_args(
            unsafe_bypass=agent_config.unsafe_bypass
        )
    if agent_config.provider == "antigravity":
        return antigravity_provider.antigravity_permission_args(
            unsafe_bypass=agent_config.unsafe_bypass
        )
    return []


def _log_launch_posture(config: DispatchConfig, log: LogFn) -> None:
    launch_agents = _launch_agents(config)
    gemini_runtime: str | None = None
    if any(agent_config.provider == "gemini" for _, _, agent_config in launch_agents):
        gemini_runtime = gemini_provider.detect_gemini_sandbox_runtime()
    for label, config_key, agent_config in launch_agents:
        args = _launch_args_for_agent(agent_config)
        summary = " ".join(args)
        if agent_config.provider == "claude" and not agent_config.unsafe_bypass:
            allowed = (
                agent_config.allowed_tools
                if agent_config.allowed_tools is not None
                else CLAUDE_DEFAULT_ALLOWED_TOOLS
            )
            summary = (
                f"--permission-mode dontAsk ({len(allowed)} allowed tool rules; "
                "WebFetch/WebSearch denied)"
            )
        if agent_config.provider == "gemini" and not agent_config.unsafe_bypass:
            summary = f"{summary} (sandbox runtime: {gemini_runtime or 'NOT FOUND'})"
        if agent_config.provider == "antigravity" and not agent_config.unsafe_bypass:
            summary = (
                "(no permission flags; workspace edits allowed, shell commands "
                "need permissions.allow grants in the agy user settings)"
            )
        _log(log, "INFO", f"Launch {label + ':':<12}{agent_config.provider} {summary}")
        if agent_config.unsafe_bypass:
            _log(
                log,
                "WARN",
                f"UNSAFE BYPASS: {label} ({agent_config.provider}) launches with "
                f"{' '.join(args)}; approvals and sandboxing are disabled. Remove "
                f"{config_key}.unsafe_bypass to restore the safe default.",
            )


def _read_required_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return read_dispatch_text(path)


def _read_optional_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return read_dispatch_text(path)


def _repair_handoff_frontmatter_file(
    handoff_path: Path,
    handoff_content: str,
    log: LogFn,
) -> str:
    repair = repair_handoff_frontmatter_text(handoff_content)
    if not repair.repaired:
        return handoff_content

    handoff_path.write_text(repair.content, encoding="utf-8")
    for warning in repair.warnings:
        _log(log, "WARN", warning)
    return repair.content


def _normalize_handoff_next_agent_file(
    handoff_path: Path,
    handoff_content: str,
    normalizer_config: NormalizerConfig,
    log: LogFn,
    *,
    repo_root: Path | None = None,
) -> str:
    model_invoked = False

    def resolve_next_agent(raw_value: str) -> str:
        # Case, whitespace, quote, and underscore variants of a canonical
        # role are resolved by the same deterministic normalization the
        # validator applies; only values it cannot place reach the model.
        nonlocal model_invoked
        deterministic = normalize_next_agent_value(raw_value)
        if deterministic is not None:
            _log(
                log,
                "INFO",
                f"router: next_agent '{raw_value}' resolved deterministically to '{deterministic}'; normalizer not invoked.",
            )
            return deterministic
        model_invoked = True
        _log(
            log,
            "WARN",
            f"router: next_agent '{raw_value}' is not a deterministic enum match; invoking normalizer.",
        )
        return normalize_next_agent(
            raw_value,
            provider=normalizer_config.provider,
            model=normalizer_config.model,
            timeout_ms=normalizer_config.timeout_ms,
            repo_root=repo_root,
        )

    try:
        normalization = normalize_handoff_next_agent_text(
            handoff_content,
            normalizer=resolve_next_agent,
        )
    except Exception as exc:
        _log(
            log,
            "WARN",
            f"router: next_agent normalization failed: {exc}; leaving HANDOFF.md unchanged.",
        )
        return handoff_content

    if normalization.unknown:
        _log(
            log,
            "INFO",
            f"router: next_agent normalizer returned '{normalization.normalized}' for '{normalization.original}'.",
        )
        _log(
            log,
            "WARN",
            f"router: next_agent '{normalization.original}' could not be normalized; leaving HANDOFF.md unchanged.",
        )
        return handoff_content

    if normalization.rewritten:
        if model_invoked:
            _log(
                log,
                "INFO",
                f"router: next_agent normalizer returned '{normalization.normalized}' for '{normalization.original}'.",
            )
        handoff_path.write_text(normalization.content, encoding="utf-8")
        _log(
            log,
            "INFO",
            "router: rewrote next_agent to deterministic output "
            f"'{normalization.normalized}' in HANDOFF.md.",
        )
        return normalization.content

    return handoff_content


def _content_sha(content: str) -> str:
    normalized_content = content.lstrip("\ufeff")
    return sha256(normalized_content.encode("utf-8")).hexdigest()


def _extract_commit_shas(content: str) -> list[str]:
    return list(dict.fromkeys(_COMMIT_SHA_RE.findall(content.lstrip("\ufeff"))))


def _new_commit_shas(previous_content: str, current_content: str) -> list[str]:
    previous_shas = set(_extract_commit_shas(previous_content))
    return [
        sha for sha in _extract_commit_shas(current_content) if sha not in previous_shas
    ]


def _short_sha_preview(sha: str) -> str:
    if len(sha) <= 12:
        return sha
    return f"{sha[:12]}..."


def _log(log: LogFn, level: str, message: str) -> None:
    log(level, message)


def _default_log(level: str, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    print(f"[{timestamp}] [{level}] {message}")


__all__ = ["Cycle", "run_loop"]
