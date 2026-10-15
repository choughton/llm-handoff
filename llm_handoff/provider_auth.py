"""Recognize provider CLI sign-in failures and say how to fix them.

A provider CLI that is not signed in fails every dispatch the same way, so
retrying only burns the failure budget. The orchestrator and the supervisor
use these markers to stop at once and tell the operator what to run.
"""

from __future__ import annotations

from pathlib import PurePath
import re


AUTH_FAILURE_MARKERS = (
    "Failed to authenticate",
    "OAuth session expired",
    "Invalid API key",
    "authentication failed",
    "not authenticated",
    "not logged in",
    "please run /login",
    "401 Unauthorized",
    # Gemini CLI: Google no longer accepts personal Gemini Code Assist sign-ins.
    "IneligibleTierError",
    "no longer supported for Gemini Code Assist",
)
AUTH_FAILURE_PATTERNS = (
    re.compile(r"\bplease\s+run\b.*\bauth\s+login\b", re.IGNORECASE),
    re.compile(r"\brun\b.*\bauth\s+login\b.*\bto\s+authenticate\b", re.IGNORECASE),
    re.compile(r"\bplease\s+run\b.*\b(?:codex|gemini)\s+login\b", re.IGNORECASE),
)

_FIX_INSTRUCTIONS = {
    "claude": (
        "Run `claude` in a terminal, type /login, and finish signing in. "
        'Check it with: claude -p "Reply with OK"'
    ),
    "codex": (
        "Run `codex login` and finish signing in. "
        'Check it with: codex exec "Reply with OK"'
    ),
    "gemini": (
        "Run `gemini` in a terminal and finish signing in. If Google reports "
        "IneligibleTierError, personal Gemini Code Assist sign-ins no longer "
        "work: set GEMINI_API_KEY (from Google AI Studio) and add "
        "`use_api_key_env: true` to each Gemini role in dispatch_config.yaml, "
        "or map those roles to another provider. "
        'Check it with: gemini -p "Reply with OK"'
    ),
}


def find_auth_failure(*texts: str | None) -> str | None:
    """Return the first output line that reports a sign-in failure."""

    lowered_markers = tuple(marker.lower() for marker in AUTH_FAILURE_MARKERS)
    for text in texts:
        if not text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if any(marker in lowered for marker in lowered_markers):
                return stripped
            if any(pattern.search(stripped) for pattern in AUTH_FAILURE_PATTERNS):
                return stripped
    return None


def auth_fix_instructions(provider: str | None) -> str:
    """Return the sign-in steps for one provider CLI."""

    if provider in _FIX_INSTRUCTIONS:
        return _FIX_INSTRUCTIONS[provider]
    return (
        "Sign in to the provider CLI configured for this role, check that a "
        "non-interactive call succeeds, then start the dispatcher again."
    )


def auth_failure_messages(
    *,
    role: str,
    provider: str | None,
    binary: str | None,
    detail: str,
) -> list[str]:
    """Return the ERROR lines the orchestrator logs before it stops."""

    # Name the command the operator types, not a Windows `.cmd` shim path.
    cli = PurePath(binary).stem if binary else (provider or "provider")
    return [
        f"{role} could not start: the `{cli}` CLI is not signed in ({detail}).",
        f"Fix: {auth_fix_instructions(provider)}",
        (
            "Then start the dispatcher again. Retrying will not help until the "
            "CLI is signed in, so the dispatch loop stops here."
        ),
    ]
