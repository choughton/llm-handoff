"""The `agy` permission entries that Antigravity-mapped roles need.

The Antigravity adapter passes no `--sandbox` flag, so the grants in the `agy`
user settings file are the only shell guardrail for those roles. This module
holds the one shared list of entries: `llm-handoff init` prints it, and
`llm-handoff doctor` checks it against the settings file. Neither command
writes that file; the user adds the entries.

The allow entries mirror the `Bash(...)` rules in
`CLAUDE_DEFAULT_ALLOWED_TOOLS`. Each one is an anchored regex grant,
``command(regex:^<cmd>( .*)?$)``, so it covers the command with any
arguments (``git rev-parse HEAD``, ``git commit -m ...``) but not a longer
command name (``git statusx``). `git push` is denied in both the plain and the
unsandboxed form by a regex that also blocks option-prefixed pushes such as
``git -C . push``; that deny also refuses any other `git` command line that
contains `` push``.

It also holds the `tools:` list that every shipped `agy` agent file under
`.agents/agents/` declares. A file without that list gets only read tools and
cannot write files; the read-only `handoff-validator` file declares the same
list without the write tools.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from llm_handoff.provider_auth import ANTIGRAVITY_SETTINGS_PATH

# The commands every Antigravity role may run, with any arguments.
ANTIGRAVITY_ALLOWED_COMMANDS: tuple[str, ...] = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "git cat-file",
    "git add",
    "git commit",
    "git rev-parse",
    "python",
    "python3",
    "pytest",
)

# Regex metacharacters escaped in a grant. `re.escape` is not used because it
# also escapes spaces, and RE2-style engines reject `\ `.
_REGEX_METACHARACTERS = frozenset("\\.+*?()|[]{}^$")


def antigravity_command_grant(command: str) -> str:
    """Return the ``agy`` allow entry that covers ``command`` with arguments.

    The result is ``command(regex:^<escaped>( .*)?$)``, where ``<escaped>`` is
    the stripped command with a backslash before each regex metacharacter
    (``\\ . + * ? ( ) | [ ] { } ^ $``). Spaces, ``-``, and every other
    character are left as they are.
    """

    escaped = "".join(
        f"\\{ch}" if ch in _REGEX_METACHARACTERS else ch for ch in command.strip()
    )
    return f"command(regex:^{escaped}( .*)?$)"


ANTIGRAVITY_REQUIRED_ALLOW: tuple[str, ...] = tuple(
    antigravity_command_grant(command) for command in ANTIGRAVITY_ALLOWED_COMMANDS
)
ANTIGRAVITY_REQUIRED_DENY: tuple[str, ...] = (
    "command(regex:^git( .*)? push( .*)?$)",
    "unsandboxed(regex:^git( .*)? push( .*)?$)",
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


def antigravity_allowed_commands(
    verification_commands: Iterable[str],
) -> tuple[str, ...]:
    """Return the required commands plus each stripped verification command.

    Empty commands are skipped, duplicates are dropped, and first-seen order
    is kept. The result lines up one-to-one with `antigravity_allow_entries`.
    """

    commands = list(ANTIGRAVITY_ALLOWED_COMMANDS)
    for command in verification_commands:
        stripped = command.strip()
        if stripped:
            commands.append(stripped)
    return tuple(dict.fromkeys(commands))


def antigravity_allow_entries(verification_commands: Iterable[str]) -> tuple[str, ...]:
    """Return the required allow entries plus one per verification command.

    Each verification command becomes ``antigravity_command_grant(<cmd>)``.
    Empty commands are skipped, duplicates are dropped, and first-seen order
    is kept.
    """

    return tuple(
        antigravity_command_grant(command)
        for command in antigravity_allowed_commands(verification_commands)
    )


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
