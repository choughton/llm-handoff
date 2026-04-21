from __future__ import annotations

from pathlib import Path

from llm_handoff import config as config_module
from llm_handoff.agent_providers.claude import invoke_claude_subagent
from llm_handoff.agent_providers.codex import invoke_codex
from llm_handoff.agent_providers.gemini import invoke_gemini
from llm_handoff.agent_providers.manual import invoke_manual_frontend
from llm_handoff.agent_types import DispatchResult, LogFn, SubagentResult
from llm_handoff.config import AgentConfig, AgentRole


def invoke_role(
    role: AgentRole,
    handoff_path: Path,
    *,
    agent_config: AgentConfig,
    log: LogFn | None = None,
    use_manual_frontend: bool = False,
    use_resume: bool = False,
    use_api_key_env: bool = False,
    additional_instruction: str | None = None,
    session_id: str | None = None,
    previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    support_prompt: str | None = None,
    subagent_name: str | None = None,
) -> DispatchResult:
    if role == "frontend" and use_manual_frontend:
        return invoke_manual_frontend(
            handoff_path,
            additional_instruction=additional_instruction,
            log=log,
        )

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
        )

    if provider == "gemini":
        return invoke_gemini(
            role,
            handoff_path,
            mention=agent_config.mention,
            agent_name=_role_agent_name(
                agent_config,
                default=f"Gemini {role}",
            ),
            binary=agent_config.binary,
            timeout_ms=agent_config.timeout_ms,
            max_retries=agent_config.retries,
            use_api_key_env=use_api_key_env
            if agent_config.use_api_key_env is None
            else agent_config.use_api_key_env,
            additional_instruction=_combine_instructions(
                support_prompt,
                additional_instruction,
            ),
            use_resume=use_resume,
            session_id=session_id,
            previous_handoff_sha=previous_handoff_sha,
            current_handoff_sha=current_handoff_sha,
            log=log,
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
            )
        )

    raise ValueError(f"Unsupported provider `{provider}` for role `{role}`.")


def invoke_backend_role(
    handoff_path: Path,
    *,
    log: LogFn | None = None,
    use_resume: bool = False,
    additional_instruction: str | None = None,
    agent_config: AgentConfig | None = None,
) -> DispatchResult:
    return invoke_role(
        "backend",
        handoff_path,
        agent_config=agent_config or _default_agent_config("backend"),
        log=log,
        use_resume=use_resume,
        additional_instruction=additional_instruction,
    )


def invoke_planner_role(
    handoff_path: Path,
    *,
    use_api_key_env: bool = False,
    additional_instruction: str | None = None,
    use_resume: bool = False,
    session_id: str | None = None,
    previous_handoff_sha: str | None = None,
    current_handoff_sha: str | None = None,
    log: LogFn | None = None,
    agent_config: AgentConfig | None = None,
) -> DispatchResult:
    return invoke_role(
        "planner",
        handoff_path,
        agent_config=agent_config or _default_agent_config("planner"),
        use_api_key_env=use_api_key_env,
        additional_instruction=additional_instruction,
        use_resume=use_resume,
        session_id=session_id,
        previous_handoff_sha=previous_handoff_sha,
        current_handoff_sha=current_handoff_sha,
        log=log,
    )


def invoke_frontend_role(
    handoff_path: Path,
    *,
    use_manual_frontend: bool = False,
    use_api_key_env: bool = False,
    additional_instruction: str | None = None,
    log: LogFn | None = None,
    agent_config: AgentConfig | None = None,
) -> DispatchResult:
    return invoke_role(
        "frontend",
        handoff_path,
        agent_config=agent_config or _default_agent_config("frontend"),
        use_manual_frontend=use_manual_frontend,
        use_api_key_env=use_api_key_env,
        additional_instruction=additional_instruction,
        log=log,
    )


def invoke_support_role(
    subagent_name: str,
    prompt: str,
    *,
    role: AgentRole | None = None,
    handoff_path: Path | None = None,
    agent_config: AgentConfig | None = None,
    log: LogFn | None = None,
) -> SubagentResult:
    resolved_role = role or "validator"
    if agent_config is None:
        return invoke_claude_subagent(subagent_name, prompt, log=log)
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
        )

    result = invoke_role(
        resolved_role,
        handoff_path or config_module.DEFAULT_HANDOFF_PATH,
        agent_config=agent_config,
        log=log,
        use_resume=bool(agent_config.resume),
        use_api_key_env=bool(agent_config.use_api_key_env),
        support_prompt=prompt,
        subagent_name=subagent_name,
    )
    return SubagentResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=result.elapsed_seconds,
    )


def _default_agent_config(role: AgentRole) -> AgentConfig:
    return config_module._default_agent_configs()[role]


def _dispatch_from_subagent(result: SubagentResult) -> DispatchResult:
    return DispatchResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=result.elapsed_seconds,
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
