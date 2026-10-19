"""Recognize provider CLI sign-in failures and say how to fix them.

A provider CLI that is not signed in fails every dispatch the same way, so
retrying only burns the failure budget. The orchestrator and the supervisor
use these markers to stop at once and tell the operator what to run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePath
import re

from llm_handoff import config


AUTH_FAILURE_MARKERS = (
    "Failed to authenticate",
    "OAuth session expired",
    "Invalid API key",
    "authentication failed",
    "not authenticated",
    "not logged in",
    "please run /login",
    "401 Unauthorized",
)
# Antigravity CLI (agy): a headless run that is not signed in. The phrase is
# too generic to match for every provider, so only the agy adapter checks it
# (on agy's own stderr) and appends `antigravity_auth_failure_line`, which
# the shared markers above recognize.
ANTIGRAVITY_AUTH_FAILURE_MARKERS = ("authentication required",)
AUTH_FAILURE_PATTERNS = (
    re.compile(r"\bplease\s+run\b.*\bauth\s+login\b", re.IGNORECASE),
    re.compile(r"\brun\b.*\bauth\s+login\b.*\bto\s+authenticate\b", re.IGNORECASE),
    re.compile(r"\bplease\s+run\b.*\bcodex\s+login\b", re.IGNORECASE),
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
    "antigravity": (
        "Run `agy` once in a terminal and finish signing in. "
        'Check it with: agy -p "Reply with OK"'
    ),
}

# agy print mode cannot ask the operator for a permission, so it auto-denies
# the tool call, prints a notice on stderr, and still exits 0.
_SOFT_DENY_MARKERS = ("auto-denied", "cannot prompt for")
_SOFT_DENY_KIND_PATTERN = re.compile(r'the\s+"([^"]+)"\s+permission', re.IGNORECASE)
_SOFT_DENY_EXAMPLE_PATTERN = re.compile(r"e\.g\.\s+([A-Za-z0-9_.-]+\([^)]*\))")
ANTIGRAVITY_SETTINGS_PATH = "~/.gemini/antigravity-cli/settings.json"


def _first_matching_line(
    texts: tuple[str | None, ...],
    matches: Callable[[str], bool],
) -> str | None:
    for text in texts:
        if not text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and matches(stripped):
                return stripped
    return None


def find_auth_failure(*texts: str | None) -> str | None:
    """Return the first output line that reports a sign-in failure."""

    lowered_markers = tuple(marker.lower() for marker in AUTH_FAILURE_MARKERS)

    def matches(line: str) -> bool:
        lowered = line.lower()
        if any(marker in lowered for marker in lowered_markers):
            return True
        return any(pattern.search(line) for pattern in AUTH_FAILURE_PATTERNS)

    return _first_matching_line(texts, matches)


def find_antigravity_auth_failure(*texts: str | None) -> str | None:
    """Return the first agy output line that reports a sign-in failure.

    Only the agy adapter calls this, on agy's own stderr, because the
    marker phrase is too generic to match for other providers.
    """

    lowered_markers = tuple(
        marker.lower() for marker in ANTIGRAVITY_AUTH_FAILURE_MARKERS
    )
    return _first_matching_line(
        texts,
        lambda line: any(marker in line.lower() for marker in lowered_markers),
    )


def antigravity_auth_failure_line(detail: str) -> str:
    """Return the line the agy adapter appends to stderr for a sign-in failure.

    The line contains a shared marker, so `find_auth_failure` recognizes the
    adapter's result wherever the result is checked.
    """

    return f"agy is not authenticated: {detail}"


def find_permission_soft_deny(*texts: str | None) -> str | None:
    """Return the first output line that reports an auto-denied permission.

    agy print mode prints this notice when a tool needed a permission it
    cannot ask for; the run still exits 0.
    """

    return _first_matching_line(
        texts,
        lambda line: all(marker in line.lower() for marker in _SOFT_DENY_MARKERS),
    )


def permission_soft_deny_message(detail: str) -> str:
    """Return one line naming the denied permission and the allow entry to add."""

    kind_match = _SOFT_DENY_KIND_PATTERN.search(detail)
    kind = kind_match.group(1).strip() if kind_match else ""
    example_match = _SOFT_DENY_EXAMPLE_PATTERN.search(detail)
    if example_match:
        entry = example_match.group(1)
    elif kind:
        entry = f"{kind}(<target>)"
    else:
        entry = "<kind>(<target>)"
    subject = f'the "{kind}"' if kind else "a tool"
    return (
        f"agy denied {subject} permission because print mode cannot ask for "
        f"it, so this dispatch failed. Add `{entry}` under `permissions.allow` "
        f"in the agy user settings file ({ANTIGRAVITY_SETTINGS_PATH}); the "
        "entry applies to every agy run for that user."
    )


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
    if binary:
        cli = PurePath(binary).stem
    elif provider:
        cli = config.PROVIDER_DEFAULT_CLI_NAMES.get(provider, provider)
    else:
        cli = "provider"
    return [
        f"{role} could not start: the `{cli}` CLI is not signed in ({detail}).",
        f"Fix: {auth_fix_instructions(provider)}",
        (
            "Then start the dispatcher again. Retrying will not help until the "
            "CLI is signed in, so the dispatch loop stops here."
        ),
    ]
