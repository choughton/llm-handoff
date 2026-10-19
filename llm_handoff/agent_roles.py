from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, TypeVar

from llm_handoff import config as config_module
from llm_handoff.agent_providers.antigravity import invoke_antigravity
from llm_handoff.agent_providers.claude import invoke_claude_subagent
from llm_handoff.agent_providers.codex import invoke_codex, unwrap_codex_final_message
from llm_handoff.agent_providers.manual import invoke_manual_frontend
from llm_handoff.agent_types import DispatchResult, LogFn, SubagentResult
from llm_handoff.config import AgentConfig, AgentRole
from llm_handoff.failover import FAILOVER_REASON_TEXT, classify_fatal_failure
from llm_handoff.provider_auth import auth_fix_instructions


_Result = TypeVar("_Result", DispatchResult, SubagentResult)
# Called after a fatal primary failure, before the fallback starts. Returns
# why the fallback must not run, or None to let it run.
FailoverGuard = Callable[[], str | None]
# A fresh `codex exec` must answer in the output schema's {status, summary}
# envelope; the dispatcher parses `summary`, so the report has to be all of it.
CODEX_SUPPORT_REPORT_INSTRUCTION = (
    "Your final message must fit the output schema's `status` and `summary` "
    "fields. Set `status` to a one-word result, and put the complete report "
    "these instructions ask for in `summary`, exactly as specified and with "
    "its line breaks. Do not shorten or paraphrase it."
)


def invoke_role(
    role: AgentRole,
    handoff_path: Path,
    *,
    agent_config: AgentConfig,
    log: LogFn | None = None,
    use_manual_frontend: bool = False,
    use_resume: bool = False,
    additional_instruction: str | None = None,
    session_id: str | None = None,
    previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    support_prompt: str | None = None,
    subagent_name: str | None = None,
    repo_root: Path | None = None,
    failover_guard: FailoverGuard | None = None,
) -> DispatchResult:
    # ``repo_root`` is the target repository the provider CLI must launch in.
    # ``None`` falls back to the process cwd for callers that already run
    # inside the target repo.
    if role == "frontend" and use_manual_frontend:
        return invoke_manual_frontend(
            handoff_path,
            additional_instruction=additional_instruction,
            log=log,
            repo_root=repo_root,
        )

    def run(launch_config: AgentConfig, is_fallback: bool) -> DispatchResult:
        # A fallback starts a fresh session: resume state and session ids
        # belong to the primary agent's provider.
        return _invoke_role_provider(
            role,
            handoff_path,
            agent_config=launch_config,
            log=log,
            use_resume=use_resume and not is_fallback,
            additional_instruction=additional_instruction,
            session_id=None if is_fallback else session_id,
            previous_handoff_sha=None if is_fallback else previous_handoff_sha,
            current_handoff_sha=current_handoff_sha,
            support_prompt=support_prompt,
            subagent_name=subagent_name,
            repo_root=repo_root,
        )

    return _run_with_failover(
        run,
        role=role,
        agent_config=agent_config,
        handoff_path=handoff_path,
        repo_root=repo_root,
        failover_guard=failover_guard,
        log=log,
    )


def _invoke_role_provider(
    role: AgentRole,
    handoff_path: Path,
    *,
    agent_config: AgentConfig,
    log: LogFn | None,
    use_resume: bool,
    additional_instruction: str | None,
    session_id: str | None,
    previous_handoff_sha: str | None,
    current_handoff_sha: str | None,
    support_prompt: str | None,
    subagent_name: str | None,
    repo_root: Path | None,
) -> DispatchResult:
    provider = agent_config.provider
    if provider == "codex":
        return invoke_codex(
            handoff_path,
            log=log,
            use_resume=use_resume,
            additional_instruction=_combine_instructions(
                support_prompt,
                additional_instruction,
            ),
            role_name=role,
            agent_name=_role_agent_name(agent_config, default="Codex"),
            binary=agent_config.binary,
            skill_name=agent_config.skill_name,
            timeout_ms=agent_config.timeout_ms,
            repo_root=repo_root,
            agent_config=agent_config,
        )

    if provider == "claude":
        prompt = support_prompt or _build_claude_role_prompt(
            role,
            handoff_path,
            additional_instruction=additional_instruction,
        )
        return _dispatch_from_subagent(
            invoke_claude_subagent(
                agent_config.agent_name or subagent_name or role,
                prompt,
                binary=agent_config.binary,
                model=agent_config.model,
                permissions_flag=agent_config.permissions_flag,
                timeout_ms=agent_config.timeout_ms,
                agent_name=_role_agent_name(
                    agent_config,
                    default=f"Claude {subagent_name or role}",
                ),
                log=log,
                repo_root=repo_root,
                agent_config=agent_config,
            )
        )

    if provider == "antigravity":
        return invoke_antigravity(
            role,
            handoff_path,
            agent_name=_role_agent_name(
                agent_config,
                default=f"Antigravity {subagent_name or role}",
            ),
            binary=agent_config.binary,
            model=agent_config.model,
            timeout_ms=agent_config.timeout_ms,
            additional_instruction=additional_instruction,
            support_prompt=support_prompt,
            subagent_name=subagent_name,
            use_resume=use_resume,
            session_id=session_id,
            previous_handoff_sha=previous_handoff_sha,
            current_handoff_sha=current_handoff_sha,
            log=log,
            repo_root=repo_root,
            agent_config=agent_config,
        )

    raise ValueError(f"Unsupported provider `{provider}` for role `{role}`.")


def invoke_backend_role(
    handoff_path: Path,
    *,
    log: LogFn | None = None,
    use_resume: bool = False,
    additional_instruction: str | None = None,
    agent_config: AgentConfig | None = None,
    repo_root: Path | None = None,
    failover_guard: FailoverGuard | None = None,
) -> DispatchResult:
    return invoke_role(
        "backend",
        handoff_path,
        agent_config=agent_config or _default_agent_config("backend"),
        log=log,
        use_resume=use_resume,
        additional_instruction=additional_instruction,
        repo_root=repo_root,
        failover_guard=failover_guard,
    )


def invoke_planner_role(
    handoff_path: Path,
    *,
    additional_instruction: str | None = None,
    use_resume: bool = False,
    session_id: str | None = None,
    previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    log: LogFn | None = None,
    agent_config: AgentConfig | None = None,
    repo_root: Path | None = None,
    failover_guard: FailoverGuard | None = None,
) -> DispatchResult:
    return invoke_role(
        "planner",
        handoff_path,
        agent_config=agent_config or _default_agent_config("planner"),
        additional_instruction=additional_instruction,
        use_resume=use_resume,
        session_id=session_id,
        previous_handoff_sha=previous_handoff_sha,
        current_handoff_sha=current_handoff_sha,
        log=log,
        repo_root=repo_root,
        failover_guard=failover_guard,
    )


def invoke_frontend_role(
    handoff_path: Path,
    *,
    use_manual_frontend: bool = False,
    additional_instruction: str | None = None,
    log: LogFn | None = None,
    agent_config: AgentConfig | None = None,
    repo_root: Path | None = None,
    failover_guard: FailoverGuard | None = None,
) -> DispatchResult:
    return invoke_role(
        "frontend",
        handoff_path,
        agent_config=agent_config or _default_agent_config("frontend"),
        use_manual_frontend=use_manual_frontend,
        additional_instruction=additional_instruction,
        log=log,
        repo_root=repo_root,
        failover_guard=failover_guard,
    )


def invoke_support_role(
    subagent_name: str,
    prompt: str,
    *,
    role: AgentRole | None = None,
    handoff_path: Path | None = None,
    agent_config: AgentConfig | None = None,
    log: LogFn | None = None,
    repo_root: Path | None = None,
    failover_guard: FailoverGuard | None = None,
) -> SubagentResult:
    resolved_role = role or "validator"
    if agent_config is None:
        return invoke_claude_subagent(
            subagent_name,
            prompt,
            log=log,
            repo_root=repo_root,
        )
    resolved_handoff_path = handoff_path or config_module.DEFAULT_HANDOFF_PATH

    def run(launch_config: AgentConfig, is_fallback: bool) -> SubagentResult:
        return _invoke_support_provider(
            subagent_name,
            prompt,
            role=resolved_role,
            handoff_path=resolved_handoff_path,
            agent_config=launch_config,
            use_resume=bool(launch_config.resume) and not is_fallback,
            log=log,
            repo_root=repo_root,
        )

    return _run_with_failover(
        run,
        role=resolved_role,
        agent_config=agent_config,
        handoff_path=resolved_handoff_path,
        repo_root=repo_root,
        failover_guard=failover_guard,
        log=log,
    )


def _invoke_support_provider(
    subagent_name: str,
    prompt: str,
    *,
    role: AgentRole,
    handoff_path: Path,
    agent_config: AgentConfig,
    use_resume: bool,
    log: LogFn | None,
    repo_root: Path | None,
) -> SubagentResult:
    if agent_config.provider == "claude":
        return invoke_claude_subagent(
            agent_config.agent_name or subagent_name,
            prompt,
            binary=agent_config.binary,
            model=agent_config.model,
            permissions_flag=agent_config.permissions_flag,
            timeout_ms=agent_config.timeout_ms,
            agent_name=_role_agent_name(
                agent_config,
                default=f"Claude {subagent_name}",
            ),
            log=log,
            repo_root=repo_root,
            agent_config=agent_config,
        )

    codex = agent_config.provider == "codex"
    result = _invoke_role_provider(
        role,
        handoff_path,
        agent_config=agent_config,
        log=log,
        use_resume=use_resume,
        additional_instruction=None,
        session_id=None,
        previous_handoff_sha=None,
        current_handoff_sha=None,
        support_prompt=(
            f"{prompt}\n\n{CODEX_SUPPORT_REPORT_INSTRUCTION}" if codex else prompt
        ),
        subagent_name=subagent_name,
        repo_root=repo_root,
    )
    stdout = result.stdout
    if codex:
        # The validator and ledger parsers read the report itself, not the
        # JSON envelope that Codex's output schema wraps it in.
        stdout = unwrap_codex_final_message(stdout)
    return SubagentResult(
        stdout=stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=result.elapsed_seconds,
        usage=result.usage,
    )


def _run_with_failover(
    run: Callable[[AgentConfig, bool], _Result],
    *,
    role: str,
    agent_config: AgentConfig,
    handoff_path: Path,
    repo_root: Path | None,
    log: LogFn | None,
    failover_guard: FailoverGuard | None = None,
) -> _Result:
    """Run the primary agent, then the fallback once after a fatal failure.

    The fallback is skipped when the primary rewrote the handoff before it
    failed, because running the role again could repeat a finished handoff,
    and when ``failover_guard`` reports a reason, such as a changed protected
    control file the fallback would otherwise read as instructions.
    """

    fallback = agent_config.fallback
    if fallback is None:
        return run(agent_config, False)

    handoff_file = _resolve_handoff_file(handoff_path, repo_root)
    handoff_before = _read_handoff_bytes(handoff_file)
    primary = run(agent_config, False)
    reason = classify_fatal_failure(
        exit_code=primary.exit_code,
        stdout=primary.stdout,
        stderr=primary.stderr,
    )
    if reason is None:
        return primary

    primary_label = _describe_agent(agent_config)
    fallback_label = _describe_agent(fallback)
    blocked = None
    if _read_handoff_bytes(handoff_file) != handoff_before:
        blocked = (
            f"it had already rewritten {handoff_file.name}, so running the role "
            "again could repeat a finished handoff"
        )
    elif failover_guard is not None:
        blocked = failover_guard()
    if blocked is not None:
        _emit(
            log,
            "WARN",
            f"{role} ({primary_label}) failed: {FAILOVER_REASON_TEXT[reason]}, "
            f"but {blocked}. Not failing over to {fallback_label}; this counts "
            "as a normal failure for the role.",
        )
        return primary

    _emit(
        log,
        "WARN",
        f"{role} ({primary_label}) failed: {FAILOVER_REASON_TEXT[reason]}. "
        f"Failing over to {fallback_label} for this dispatch.",
    )
    if reason == "auth":
        _emit(
            log,
            "WARN",
            f"Primary {primary_label} sign-in fix: "
            f"{auth_fix_instructions(agent_config.provider)}",
        )
    secondary = run(fallback, True)
    _emit(
        log,
        "INFO",
        f"{role} fallback ({fallback_label}) exited with code "
        f"{secondary.exit_code}.",
    )
    fallback_reason = classify_fatal_failure(
        exit_code=secondary.exit_code,
        stdout=secondary.stdout,
        stderr=secondary.stderr,
    )
    if fallback_reason == "auth":
        # The loop's sign-in stop names the role's primary CLI; name this one.
        _emit(
            log,
            "WARN",
            f"Fallback {fallback_label} sign-in fix: "
            f"{auth_fix_instructions(fallback.provider)}",
        )
    elapsed = primary.elapsed_seconds + secondary.elapsed_seconds
    if isinstance(secondary, DispatchResult):
        # The fallback's session belongs to another agent, and the primary's
        # session may have stopped mid-turn, so neither is resumed next cycle.
        return replace(
            secondary,
            elapsed_seconds=elapsed,
            session_id=None,
            session_invalidated=True,
        )
    return replace(secondary, elapsed_seconds=elapsed)


def _describe_agent(agent_config: AgentConfig) -> str:
    label = agent_config.provider
    if agent_config.provider == "claude" and agent_config.model:
        label = f"{label} {agent_config.model}"
    # Name a non-default binary so two agents on one provider read apart.
    default_cli = config_module.PROVIDER_DEFAULT_CLI_NAMES.get(
        agent_config.provider, agent_config.provider
    )
    if agent_config.binary and Path(agent_config.binary).stem != default_cli:
        cli = Path(agent_config.binary).stem
        label = f"{label} via {cli}"
    return label


def _resolve_handoff_file(handoff_path: Path, repo_root: Path | None) -> Path:
    path = Path(handoff_path)
    if path.is_absolute():
        return path
    return (Path(repo_root) if repo_root is not None else Path.cwd()) / path


def _read_handoff_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _emit(log: LogFn | None, level: str, message: str) -> None:
    if log is not None:
        log(level, message)


def _default_agent_config(role: AgentRole) -> AgentConfig:
    return config_module._default_agent_configs()[role]


def _dispatch_from_subagent(result: SubagentResult) -> DispatchResult:
    return DispatchResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=result.elapsed_seconds,
        usage=result.usage,
    )


def _role_agent_name(
    agent_config: AgentConfig,
    *,
    default: str,
) -> str:
    return agent_config.agent_name or default


def _combine_instructions(
    first: str | None,
    second: str | None,
) -> str | None:
    parts = [part.strip() for part in (first, second) if part and part.strip()]
    if not parts:
        return None
    return "\n\n".join(parts)


def _build_claude_role_prompt(
    role: AgentRole,
    handoff_path: Path,
    *,
    additional_instruction: str | None = None,
) -> str:
    prompt = (
        f"Use the {role} agent to execute the work described in {handoff_path}. "
        "Read the repository instructions and relevant state files before acting. "
        "Follow HANDOFF.md as the live task file, update it when you finish, "
        "and commit any required changes before returning control."
    )
    if additional_instruction:
        prompt = f"{prompt} Additional instruction: {additional_instruction.strip()}"
    return prompt
