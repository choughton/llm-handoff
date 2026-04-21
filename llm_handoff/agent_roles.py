from __future__ import annotations

from pathlib import Path

from llm_handoff.agent_providers.claude import invoke_claude_subagent
from llm_handoff.agent_providers.codex import invoke_codex
from llm_handoff.agent_providers.gemini import invoke_gemini
from llm_handoff.agent_providers.manual import invoke_manual_frontend
from llm_handoff.agent_types import DispatchResult, LogFn, SubagentResult


def invoke_backend_role(
    handoff_path: Path,
    *,
    log: LogFn | None = None,
    use_resume: bool = False,
    additional_instruction: str | None = None,
) -> DispatchResult:
    return invoke_codex(
        handoff_path,
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
) -> DispatchResult:
    return invoke_gemini(
        "Planner",
        handoff_path,
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
) -> DispatchResult:
    if use_manual_frontend:
        return invoke_manual_frontend(
            handoff_path,
            additional_instruction=additional_instruction,
            log=log,
        )
    return invoke_gemini(
        "Frontend",
        handoff_path,
        use_api_key_env=use_api_key_env,
        additional_instruction=additional_instruction,
        log=log,
    )


def invoke_support_role(
    subagent_name: str,
    prompt: str,
    *,
    log: LogFn | None = None,
) -> SubagentResult:
    return invoke_claude_subagent(subagent_name, prompt, log=log)
