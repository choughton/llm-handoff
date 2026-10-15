from __future__ import annotations

from pathlib import Path
import time

from llm_handoff.agent_process import _resolve_handoff_path, _wait_for_manual_continue
from llm_handoff.agent_types import DispatchResult, LogFn


def invoke_manual_frontend(
    handoff_path: Path,
    *,
    additional_instruction: str | None = None,
    log: LogFn | None = None,
    repo_root: Path | None = None,
) -> DispatchResult:
    # Relative handoff paths resolve against the target repo, not the process
    # cwd, so a dispatcher run from its own checkout names the right file.
    resolved_repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
    resolved_handoff_path = _resolve_handoff_path(handoff_path, resolved_repo_root)
    message = (
        "manual frontend is a manual GUI step. Complete the frontend work using "
        f"{resolved_handoff_path}, then press any key to continue dispatch."
    )
    if additional_instruction:
        message = f"{message} Additional instruction: {additional_instruction.strip()}"
    start_time = time.monotonic()

    if log is not None:
        log("PAUSE", message)
    else:
        print(message, flush=True)

    _wait_for_manual_continue()

    return DispatchResult(
        stdout="",
        stderr="",
        exit_code=0,
        elapsed_seconds=time.monotonic() - start_time,
    )
