"""The `agy` permission entries that Antigravity-mapped roles need.

The Antigravity adapter passes no `--sandbox` flag, so the grants in the `agy`
user settings file are the only shell guardrail for those roles. This module
holds the one shared list of entries: `llm-handoff init` prints it, and
`llm-handoff doctor` checks it against the settings file. Neither command
writes that file; the user adds the entries.

The allow entries mirror the `Bash(...)` rules in
`CLAUDE_DEFAULT_ALLOWED_TOOLS`. `git push` is denied in both the plain and the
unsandboxed form.

It also holds the `tools:` list that every shipped `agy` agent file under
`.agents/agents/` declares. A file without that list gets only read tools and
cannot write files; the read-only `handoff-validator` file declares the same
list without the write tools.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from llm_handoff.provider_auth import ANTIGRAVITY_SETTINGS_PATH

ANTIGRAVITY_REQUIRED_ALLOW: tuple[str, ...] = (
    "command(git status)",
    "command(git diff)",
    "command(git log)",
    "command(git show)",
    "command(git cat-file)",
    "command(git add)",
    "command(git commit)",
    "command(git rev-parse)",
    "command(python)",
    "command(python3)",
    "command(pytest)",
)
ANTIGRAVITY_REQUIRED_DENY: tuple[str, ...] = (
    "command(git push)",
    "unsandboxed(git push)",
)

# The `tools:` list in each shipped `.agents/agents/*.md` file. A tool name
# `agy` does not know makes it exit before the run starts. Web, browser,
# image, MCP, and subagent tools are left out on purpose.
ANTIGRAVITY_AGENT_TOOLS: tuple[str, ...] = (
    "view_file",
    "list_dir",
    "grep_search",
    "find_by_name",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "run_command",
)
# The write tools; the read-only `handoff-validator` agent file omits these.
ANTIGRAVITY_AGENT_WRITE_TOOLS: tuple[str, ...] = (
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
)


def antigravity_allow_entries(verification_commands: Iterable[str]) -> tuple[str, ...]:
    """Return the required allow entries plus one per verification command.

    Each verification command becomes ``command(<cmd>)`` (stripped). Empty
    commands are skipped, duplicates are dropped, and first-seen order is kept.
    """

    entries = list(ANTIGRAVITY_REQUIRED_ALLOW)
    for command in verification_commands:
        stripped = command.strip()
        if stripped:
            entries.append(f"command({stripped})")
    return tuple(dict.fromkeys(entries))


def antigravity_settings_path() -> Path:
    """Return the `agy` user settings file (``ANTIGRAVITY_SETTINGS_PATH``)."""

    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def _ascii_safe(text: str) -> str:
    """Escape every character outside printable ASCII."""

    return "".join(
        ch if " " <= ch <= "~" else ch.encode("unicode_escape").decode("ascii")
        for ch in text
    )


def format_antigravity_permissions(
    allow: Iterable[str],
    deny: Iterable[str],
) -> list[str]:
    """Return the printable, ASCII-only lines describing the `agy` grants."""

    lines = [
        "Antigravity (agy) permissions for "
        f"{ANTIGRAVITY_SETTINGS_PATH}:",
        "  permissions.allow:",
    ]
    lines.extend(f"    - {_ascii_safe(entry)}" for entry in allow)
    lines.append("  permissions.deny:")
    lines.extend(f"    - {_ascii_safe(entry)}" for entry in deny)
    lines.append(
        "  These grants apply to every agy run for this user, and the adapter "
        "passes no --sandbox flag, so these grants are the only shell "
        "guardrail for agy roles."
    )
    return lines
