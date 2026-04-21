from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


LogFn = Callable[[str, str], None]


@dataclass(frozen=True)
class DispatchResult:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float
    session_id: str | None = None
    session_invalidated: bool = False


@dataclass(frozen=True)
class SubagentResult:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float


@dataclass(frozen=True)
class _ProcessResult:
    stdout: str
    stderr: str
    exit_code: int
    session_id: str | None = None
