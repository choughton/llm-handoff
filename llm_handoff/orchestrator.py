from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import time
from typing import Callable

from llm_handoff.agents import (
    DispatchResult,
    SubagentResult,
    invoke_claude_subagent,
    invoke_codex,
    invoke_gemini,
    invoke_manual_frontend,
)
from llm_handoff.config import DispatchConfig
from llm_handoff.ledger import run_epic_close
from llm_handoff.handoff_normalizer import (
    CANONICAL_NEXT_AGENT_SET,
    normalize_handoff_next_agent_text,
    normalize_next_agent,
)
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


AUDIT_PROMPT = (
    "Use the auditor agent to audit the work described in "
    "docs/handoff/HANDOFF.md. Read the repository instructions and relevant "
    "state files before judging the diff. Run the configured verification "
    "commands when available. Return a concise audit report, then update "
    "docs/handoff/HANDOFF.md with findings and a routing instruction for the "
    "next agent."
)

MISROUTE_PROMPT = (
    "Use the handoff-router agent to analyze docs/handoff/HANDOFF.md. The "
    "previous routing was flagged as a potential misroute. Determine the "
    "correct agent and write a corrected routing instruction to "
    "docs/handoff/HANDOFF.md with your reasoning."
)
HANDOFF_VALIDATOR_PROMPT = (
    "Use the handoff-validator agent to validate docs/handoff/HANDOFF.md. "
    "The dispatcher could not find a dispatchable routing instruction. "
    "Focus on why the handoff is not dispatchable, especially routing, SHA, "
    "ownership, and content requirements. Return ONLY the structured output format."
)
STALE_ROUTE_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate docs/handoff/HANDOFF.md. "
    "The dispatcher detected stale routing: HANDOFF.md still routes to "
    "{route}, and its content hash is unchanged since the previous cycle. "
    "Focus on why the prior cycle did not rewrite the handoff and what must "
    "change in routing, ownership, or content requirements. Return ONLY the "
    "structured output format."
)
EPIC_CLOSE_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate docs/handoff/HANDOFF.md. "
    "The dispatcher attempted Epic-Close, but the ledger-updater returned "
    "unparseable output: {parse_error}. "
    "Focus on whether the handoff was truly ready for epic close or should "
    "route to audit, planning, or escalation instead. Return ONLY the "
    "structured output format."
)
LOW_CONFIDENCE_ROUTE_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate docs/handoff/HANDOFF.md. "
    "The dispatcher found a {confidence}-confidence routing decision to "
    "{route}: {reasoning} "
    "Focus on whether the handoff is truly dispatchable or if the routing "
    "instruction needs to be clarified before dispatch. Return ONLY the "
    "structured output format."
)
PLANNER_SELF_LOOP_VALIDATOR_PROMPT = (
    "Use the handoff-validator agent to validate docs/handoff/HANDOFF.md. "
    "The dispatcher detected a planner self-loop: the planner just finished, "
    "and the updated HANDOFF.md still routes back to the planner. "
    "Focus on why the planner routed the handoff back to itself and what must "
    "change in routing, ownership, or content requirements. Return ONLY the "
    "structured output format."
)
AGENT_SELF_LOOP_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate docs/handoff/HANDOFF.md. "
    "The dispatcher detected a self-loop: {previous_agent} just finished, "
    "and the updated HANDOFF.md still routes back to the same agent. "
    "Focus on why the handoff routed work back to itself and what must "
    "change in routing, ownership, or content requirements. Return ONLY the "
    "structured output format."
)
POST_DISPATCH_MISSING_ROUTE_VALIDATOR_PROMPT_TEMPLATE = (
    "Use the handoff-validator agent to validate docs/handoff/HANDOFF.md. "
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
    "or an explicit Escalation section if the work cannot be routed safely."
)
STALE_EPIC_CLOSE_GEMINI_RECOVERY_INSTRUCTION = (
    "The prior Epic-Close cycle already completed, but HANDOFF.md was not "
    "rewritten and still contains stale Epic-Close routing. Treat "
    "PROJECT_STATE.md and Git history as the source of truth for the current "
    "workflow phase. Evaluate the current repo state, scope the next phase, "
    "and rewrite HANDOFF.md for the next cycle instead of repeating "
    "Epic-Close."
)

LogFn = Callable[[str, str], None]
_COMMIT_SHA_RE = re.compile(r"(?i)\b[0-9a-f]{7,40}\b")


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


def run_loop(
    config: DispatchConfig,
    *,
    max_cycles: int | None = None,
    log: LogFn | None = None,
) -> int:
    log_fn = log or _default_log

    _log_startup_banner(config, log_fn)
    mark_startup_complete = getattr(log_fn, "mark_startup_complete", None)
    if callable(mark_startup_complete):
        mark_startup_complete()

    consecutive_failures = 0
    previous_cycle: Cycle | None = None
    cycle_number = 0
    pending_planner_recovery: PendingPlannerRecovery | None = None
    gemini_pe_session_id: str | None = None
    gemini_pe_previous_handoff_sha: str | None = None

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
            log_fn,
        )

        claude_md_content = _read_optional_text(config.claude_md_full_path)
        handoff_sha = _content_sha(handoff_content)
        _log_handoff_scope(handoff_content, log_fn)
        forced_additional_instruction: str | None = None
        if (
            pending_planner_recovery is not None
            and pending_planner_recovery.handoff_sha == handoff_sha
        ):
            decision = RoutingDecision(
                route="Gemini-PE",
                confidence="HIGH",
                source="post_dispatch_routing_recovery",
                reasoning=(
                    "The previous cycle ended with a non-dispatchable handoff, "
                    "so the planner is repairing routing or escalating."
                ),
                warnings=[],
            )
            forced_additional_instruction = (
                pending_planner_recovery.additional_instruction
            )
            pending_planner_recovery = None
        else:
            pending_planner_recovery = None
            decision = route_handoff(
                handoff_content,
                claude_md_content=claude_md_content,
            )
        recovery_decision = _stale_route_recovery_decision(
            decision.route,
            claude_md_content=claude_md_content,
            previous_cycle=previous_cycle,
            handoff_sha=handoff_sha,
        )
        if recovery_decision is not None:
            _log(
                log_fn,
                "WARN",
                "STALE Epic-Close detected after a completed finalizer cycle; redirecting this cycle to the planner for forward routing.",
            )
            decision = recovery_decision

        _log(log_fn, "INFO", f"Routing source: {decision.source}")

        if decision.route == "Unknown":
            _run_unknown_route_validator(log_fn)
            if not _pause_until_handoff_changes(
                config,
                handoff_sha=handoff_sha,
                reason=(
                    "No routing instruction found in HANDOFF.md. Update the handoff; dispatch will resume after the file changes."
                ),
                log=log_fn,
            ):
                return 0
            continue

        if decision.route == "Escalation":
            if not _pause_until_handoff_changes(
                config,
                handoff_sha=handoff_sha,
                reason=(
                    "ESCALATION DETECTED in HANDOFF.md. Resolve it manually; dispatch will resume after the file changes."
                ),
                log=log_fn,
            ):
                return 0
            continue

        current_cycle = Cycle(
            number=cycle_number,
            route=decision.route,
            handoff_sha=handoff_sha,
            handoff_path=config.handoff_full_path,
        )

        if (
            previous_cycle is not None
            and previous_cycle.route == current_cycle.route
            and previous_cycle.handoff_sha == current_cycle.handoff_sha
        ):
            _run_stale_route_validator(current_cycle.route, log_fn)
            if not _pause_until_handoff_changes(
                config,
                handoff_sha=current_cycle.handoff_sha,
                reason=(
                    f"STALE ROUTING DETECTED: same route ({current_cycle.route}) and unchanged HANDOFF hash."
                ),
                log=log_fn,
            ):
                return 0
            continue

        _log(log_fn, "INFO", f"Routing instruction: {decision.route}")
        for warning in decision.warnings:
            _log(log_fn, "WARN", warning)

        if config.dry_run:
            _log_dry_run(config, current_cycle.route, log_fn)
            _log(log_fn, "INFO", "[DRY RUN] Single cycle complete. Exiting.")
            return 0

        if decision.confidence != "HIGH":
            _run_low_confidence_route_validator(decision, log_fn)
            if not _pause_until_handoff_changes(
                config,
                handoff_sha=current_cycle.handoff_sha,
                reason=(
                    f"Routing instruction {decision.route} is only {decision.confidence} confidence. Update HANDOFF.md; dispatch will resume after the file changes."
                ),
                log=log_fn,
            ):
                return 0
            continue

        if current_cycle.route == "Epic-Close":
            _log(
                log_fn,
                "DISPATCH",
                "Dispatching Claude Code ledger-updater for epic close.",
            )
            ledger_result = run_epic_close(log=log_fn)
            parse_error = ledger_result.parse_error
            if parse_error:
                _run_epic_close_validator(parse_error, log_fn)
                if not _pause_until_handoff_changes(
                    config,
                    handoff_sha=current_cycle.handoff_sha,
                    reason=(
                        "Epic-Close ledger-updater output was unparseable. Update HANDOFF.md; dispatch will resume after the file changes."
                    ),
                    log=log_fn,
                ):
                    return 0
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
        else:
            dispatch_result, previous_agent = _dispatch_route(
                current_cycle.route,
                config,
                log_fn,
                gemini_pe_session_id=gemini_pe_session_id,
                gemini_pe_previous_handoff_sha=gemini_pe_previous_handoff_sha,
                current_handoff_sha=current_cycle.handoff_sha,
                additional_instruction=(
                    forced_additional_instruction
                    if forced_additional_instruction is not None
                    else (
                        STALE_EPIC_CLOSE_GEMINI_RECOVERY_INSTRUCTION
                        if decision.source == "stale_epic_close_recovery"
                        else None
                    )
                ),
            )
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
                    log_fn,
                )
            _log_dispatch_completion(
                handoff_path=config.handoff_full_path,
                previous_agent=previous_agent,
                dispatch_result=dispatch_result,
                prior_handoff_content=handoff_content,
                prior_handoff_sha=current_cycle.handoff_sha,
                log=log_fn,
            )
            if current_cycle.route == "Gemini-PE" and config.use_gemini_resume:
                if dispatch_result.session_invalidated:
                    gemini_pe_session_id = None
                    gemini_pe_previous_handoff_sha = None
                if dispatch_result.exit_code == 0:
                    if dispatch_result.session_id:
                        gemini_pe_session_id = dispatch_result.session_id
                    if gemini_pe_session_id:
                        gemini_pe_previous_handoff_sha = current_cycle.handoff_sha
            if dispatch_result.exit_code != 0:
                consecutive_failures = _record_failure(
                    config,
                    consecutive_failures + 1,
                    log_fn,
                )
                if consecutive_failures >= config.max_consecutive_failures:
                    return 1
                continue

            _log(
                log_fn,
                "AGENT",
                f"Running post-dispatch validation for {previous_agent}...",
            )
            validation_result = validate_handoff(
                config.handoff_full_path,
                previous_agent,
                prior_handoff_sha=current_cycle.handoff_sha,
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
            if validation_result.verdict == "NO":
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
                    _run_post_dispatch_missing_route_validator(
                        previous_agent=previous_agent,
                        log=log_fn,
                    )
                    if not _pause_until_handoff_changes(
                        config,
                        handoff_sha=_content_sha(
                            _read_required_text(config.handoff_full_path) or ""
                        ),
                        reason=_missing_route_pause_reason(previous_agent),
                        log=log_fn,
                    ):
                        return 0
                    continue
                if self_loop_kind is not None:
                    _run_self_loop_validator(
                        previous_agent=previous_agent,
                        self_loop_kind=self_loop_kind,
                        log=log_fn,
                    )
                    if not _pause_until_handoff_changes(
                        config,
                        handoff_sha=_content_sha(
                            _read_required_text(config.handoff_full_path) or ""
                        ),
                        reason=_self_loop_pause_reason(
                            previous_agent,
                            self_loop_kind,
                        ),
                        log=log_fn,
                    ):
                        return 0
                    continue
                return 1

            previous_cycle = current_cycle
            consecutive_failures = 0

        if max_cycles is not None and cycle_number >= max_cycles:
            return 0

        _log(log_fn, "INFO", f"--- End of cycle {cycle_number} ---")
        _log(log_fn, "INFO", "")


def _dispatch_route(
    route: RouteName,
    config: DispatchConfig,
    log: LogFn,
    *,
    gemini_pe_session_id: str | None = None,
    gemini_pe_previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    additional_instruction: str | None = None,
) -> tuple[DispatchResult, str]:
    handoff_path = config.handoff_full_path

    if route == "Codex":
        _log(log, "DISPATCH", "Dispatching Codex.")
        return invoke_codex(
            handoff_path,
            log=log,
            use_resume=config.use_codex_resume,
        ), "Codex"

    if route == "Gemini-PE":
        _log(log, "DISPATCH", "Dispatching Gemini PE.")
        return (
            invoke_gemini(
                "PE",
                handoff_path,
                use_api_key_env=config.use_gemini_api_key_env,
                additional_instruction=additional_instruction,
                use_resume=config.use_gemini_resume,
                session_id=gemini_pe_session_id,
                previous_handoff_sha=gemini_pe_previous_handoff_sha,
                current_handoff_sha=current_handoff_sha,
                log=log,
            ),
            "Gemini-PE",
        )

    if route == "Gemini-Frontend":
        if config.use_manual_frontend:
            _log(log, "DISPATCH", "Pausing for manual frontend work.")
            return invoke_manual_frontend(handoff_path, log=log), "Manual frontend"

        _log(log, "DISPATCH", "Dispatching Gemini Frontend.")
        return (
            invoke_gemini(
                "Frontend",
                handoff_path,
                use_api_key_env=config.use_gemini_api_key_env,
                additional_instruction=additional_instruction,
                log=log,
            ),
            "Gemini-Frontend",
        )

    if route == "ClaudeCode-Audit":
        _log(log, "DISPATCH", "Dispatching Claude Code for audit.")
        return _dispatch_from_subagent(
            invoke_claude_subagent("auditor", AUDIT_PROMPT, log=log)
        ), "Claude Code (audit)"

    if route == "ClaudeCode-Misroute":
        _log(log, "DISPATCH", "Dispatching Claude Code for misroute clarification.")
        return _dispatch_from_subagent(
            invoke_claude_subagent("handoff-router", MISROUTE_PROMPT, log=log)
        ), "Claude Code (misroute)"

    raise ValueError(f"Unsupported route: {route}")


def _dispatch_from_subagent(result: SubagentResult) -> DispatchResult:
    return DispatchResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=result.elapsed_seconds,
    )


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
    subagent_name: str,
    prompt: str,
    label: str,
    log: LogFn,
) -> None:
    result = invoke_claude_subagent(
        subagent_name,
        prompt,
        log=log,
    )
    _log(log, "AGENT", f"{label} exited with code {result.exit_code}")

    if result.exit_code != 0:
        if result.stderr.strip():
            _log(log, "WARN", f"{label} stderr: {result.stderr.strip()}")
        return

    if not result.stdout.strip():
        _log(log, "WARN", f"{label} returned no stdout.")
        return

    try:
        validation_result = parse_validation_output(result.stdout)
    except ValueError:
        _log(log, "WARN", f"{label} returned unparseable output.")
        return

    _log(log, "AGENT", f"Handoff validation verdict: {validation_result.verdict}")
    for warning in validation_result.warnings:
        _log(log, "WARN", warning)
    for error in validation_result.errors:
        _log(log, "ERROR", error)


def _run_unknown_route_validator(log: LogFn) -> None:
    _log(
        log,
        "AGENT",
        "No dispatchable route found. Invoking Claude handoff-validator...",
    )
    _run_validator(
        "handoff-validator",
        HANDOFF_VALIDATOR_PROMPT,
        "Handoff-validator",
        log,
    )


def _stale_route_recovery_decision(
    route: RouteName,
    *,
    claude_md_content: str | None,
    previous_cycle: Cycle | None,
    handoff_sha: str,
) -> RoutingDecision | None:
    if route != "Epic-Close":
        return None
    if previous_cycle is None:
        return None
    if previous_cycle.route != "Epic-Close":
        return None
    if previous_cycle.handoff_sha != handoff_sha:
        return None
    return RoutingDecision(
        route="Gemini-PE",
        confidence="HIGH",
        source="stale_epic_close_recovery",
        reasoning=(
            "The prior Epic-Close cycle completed successfully, but HANDOFF.md "
            "still routes to Epic-Close with unchanged content."
        ),
        warnings=[],
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


def _handoff_story_summary(
    *,
    story_id: str | None,
    story_title: str | None,
) -> str | None:
    if story_id and story_title:
        return f"{story_id} ({story_title})"
    return story_id or story_title


def _run_stale_route_validator(route: RouteName, log: LogFn) -> None:
    _log(
        log,
        "AGENT",
        f"Stale HANDOFF detected for route {route}. Invoking Claude handoff-validator...",
    )
    _run_validator(
        "handoff-validator",
        STALE_ROUTE_VALIDATOR_PROMPT_TEMPLATE.format(route=route),
        "Handoff-validator",
        log,
    )


def _run_epic_close_validator(parse_error: str, log: LogFn) -> None:
    _log(
        log,
        "AGENT",
        "Epic-Close produced unparseable ledger-updater output. Invoking Claude handoff-validator...",
    )
    _run_validator(
        "handoff-validator",
        EPIC_CLOSE_VALIDATOR_PROMPT_TEMPLATE.format(parse_error=parse_error),
        "Handoff-validator",
        log,
    )


def _run_low_confidence_route_validator(
    decision: RoutingDecision,
    log: LogFn,
) -> None:
    _log(
        log,
        "AGENT",
        f"Routing decision for {decision.route} is only {decision.confidence} confidence. Invoking Claude handoff-validator before dispatch...",
    )
    _run_validator(
        "handoff-validator",
        LOW_CONFIDENCE_ROUTE_VALIDATOR_PROMPT_TEMPLATE.format(
            confidence=decision.confidence,
            route=decision.route,
            reasoning=decision.reasoning,
        ),
        "Handoff-validator",
        log,
    )


def _run_self_loop_validator(
    *,
    previous_agent: str,
    self_loop_kind: str,
    log: LogFn,
) -> None:
    if self_loop_kind == "planner":
        prompt = PLANNER_SELF_LOOP_VALIDATOR_PROMPT
        message = "The planner produced a self-loop. Invoking Claude handoff-validator..."
    else:
        prompt = AGENT_SELF_LOOP_VALIDATOR_PROMPT_TEMPLATE.format(
            previous_agent=previous_agent
        )
        message = f"{previous_agent} produced a self-loop. Invoking Claude handoff-validator..."

    _log(log, "AGENT", message)
    _run_validator(
        "handoff-validator",
        prompt,
        "Handoff-validator",
        log,
    )


def _run_post_dispatch_missing_route_validator(
    *,
    previous_agent: str,
    log: LogFn,
) -> None:
    _log(
        log,
        "AGENT",
        f"{previous_agent} produced a handoff without a dispatchable route. Invoking Claude handoff-validator...",
    )
    _run_validator(
        "handoff-validator",
        POST_DISPATCH_MISSING_ROUTE_VALIDATOR_PROMPT_TEMPLATE.format(
            previous_agent=previous_agent
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
    if "gemini" in previous_agent.lower():
        return False
    return _has_missing_route_error(result)


def _has_missing_route_error(result: ValidationResult) -> bool:
    return any(
        error.startswith("routing_instruction_missing:") for error in result.errors
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
    if route == "Codex":
        _log(log, "DISPATCH", "[DRY RUN] Would dispatch Codex")
        return

    if route == "Gemini-PE":
        _log(log, "DISPATCH", "[DRY RUN] Would dispatch Gemini PE")
        return

    if route == "Gemini-Frontend":
        target = (
            "manual frontend pause"
            if config.use_manual_frontend
            else "Gemini Frontend (CLI)"
        )
        _log(log, "DISPATCH", f"[DRY RUN] Would dispatch {target} for frontend work")
        return

    if route == "ClaudeCode-Audit":
        _log(log, "DISPATCH", "[DRY RUN] Would dispatch Claude Code for audit")
        return

    if route == "ClaudeCode-Misroute":
        _log(
            log,
            "DISPATCH",
            "[DRY RUN] Would dispatch Claude Code for misroute clarification",
        )
        return

    if route == "Epic-Close":
        _log(
            log,
            "DISPATCH",
            "[DRY RUN] Would handle Epic-Close with auto ledger update",
        )
        return


def _log_startup_banner(config: DispatchConfig, log: LogFn) -> None:
    frontend_mode = (
        "manual frontend pause"
        if config.use_manual_frontend
        else "Gemini CLI frontend"
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
    _log(log, "INFO", "Finalizer route:    ON")
    _log(log, "INFO", "Chaining:           NONE (single dispatch per cycle)")
    _log(log, "INFO", f"Frontend agent:     {frontend_mode}")
    _log(
        log,
        "INFO",
        "Codex session:      MANAGED RESUME (persisted thread id)"
        if config.use_codex_resume
        else "Codex session:      STATELESS (new session per dispatch)",
    )
    _log(
        log,
        "INFO",
        "Gemini PE session:  MANAGED RESUME (in-memory session id)"
        if config.use_gemini_resume
        else "Gemini PE session:  STATELESS (new session per dispatch)",
    )
    _log(
        log,
        "INFO",
        "Gemini API env:     PRESERVE GEMINI_API_KEY; STRIP GOOGLE_API_KEY"
        if config.use_gemini_api_key_env
        else "Gemini API env:     STRIP GOOGLE_API_KEY/GEMINI_API_KEY",
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
    log: LogFn,
) -> str:
    try:
        original_next_agent = _handoff_next_agent_value(handoff_content)
        if original_next_agent and original_next_agent not in CANONICAL_NEXT_AGENT_SET:
            _log(
                log,
                "WARN",
                f"router: next_agent '{original_next_agent}' is not a deterministic enum match; invoking normalizer.",
            )
        normalization = normalize_handoff_next_agent_text(
            handoff_content,
            normalizer=normalize_next_agent,
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


def _handoff_next_agent_value(handoff_content: str) -> str | None:
    lines = handoff_content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    for line in lines[1:]:
        if line.strip() == "---":
            return None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == "next_agent":
            return value.strip().strip("'\"")

    return None


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

