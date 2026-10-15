"""Claude Code PreToolUse hook for llm-handoff roles.

Blocks shell commands that dispatched roles must never run: git push,
curl, wget, recursive forced deletion (rm -rf), and git reset --hard.

Claude Code sends the hook event as JSON on stdin. Exit code 2 blocks the
tool call and shows the stderr message to Claude. Input that cannot be read
is blocked too (fail closed). Standard library only.

This is a guardrail against mistakes and simple prompt injection, not a
security boundary: aliases, scripts, or interpreters can still reach the
same effect. Keep push credentials out of the dispatch environment.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh", "dash", "pwsh", "powershell"})
PREFIX_WRAPPERS = frozenset(
    {"sudo", "env", "command", "exec", "nohup", "time", "nice", "builtin"}
)
NETWORK_TOOLS = frozenset(
    {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm"}
)
REMOVE_TOOLS = frozenset({"rm", "remove-item", "ri", "del", "erase"})
GIT_OPTIONS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
)
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|&\n`]|\$\(|[()]")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError as exc:
        return _block(f"could not parse the hook input ({exc})")
    if not isinstance(event, dict):
        return _block("hook input was not a JSON object")
    if event.get("tool_name") not in SHELL_TOOLS:
        return 0
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return _block("shell tool call had no command string")
    reason = blocked_reason(command)
    if reason is None:
        return 0
    return _block(reason)


def blocked_reason(command: str, *, depth: int = 0) -> str | None:
    for segment in _SEGMENT_SPLIT_RE.split(command):
        tokens = _tokens(segment)
        if not tokens:
            continue
        reason = _segment_reason(tokens, depth=depth)
        if reason is not None:
            return reason
    return None


def _segment_reason(tokens: list[str], *, depth: int) -> str | None:
    tokens = _strip_prefixes(tokens)
    if not tokens:
        return None
    program = _program_name(tokens[0])
    args = tokens[1:]

    if program in SHELL_WRAPPERS and depth < 3:
        for index, arg in enumerate(args[:-1]):
            if arg.lower() in {"-c", "-command", "/c"}:
                return blocked_reason(args[index + 1], depth=depth + 1)
        return None
    if program in NETWORK_TOOLS:
        return f"network downloads with {program} are not allowed for dispatched roles"
    if program == "git":
        return _git_reason(args)
    if program in REMOVE_TOOLS and _is_recursive_force(args):
        return "recursive forced deletion (rm -rf) is not allowed for dispatched roles"
    return None


def _git_reason(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in GIT_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return None
    subcommand = args[index]
    rest = args[index + 1 :]
    if subcommand == "push":
        return (
            "git push is not allowed for dispatched roles; pushing is a human decision"
        )
    if subcommand == "reset" and "--hard" in rest:
        return "git reset --hard is not allowed for dispatched roles; it discards work"
    return None


def _is_recursive_force(args: list[str]) -> bool:
    recursive = False
    force = False
    for arg in args:
        if arg == "--":
            break
        lowered = arg.lower()
        if lowered in {"--recursive", "-recurse"}:
            recursive = True
        elif lowered in {"--force", "-force"}:
            force = True
        elif re.fullmatch(r"-[A-Za-z]+", arg):
            letters = arg[1:]
            recursive = recursive or "r" in letters or "R" in letters
            force = force or "f" in letters
    return recursive and force


def _strip_prefixes(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ENV_ASSIGNMENT_RE.match(token):
            index += 1
            continue
        if _program_name(token) in PREFIX_WRAPPERS:
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            continue
        break
    return tokens[index:]


def _program_name(token: str) -> str:
    name = re.split(r"[\\/]", token)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _tokens(segment: str) -> list[str]:
    # Normalize Windows path separators so shlex does not treat them as
    # escapes; unbalanced quotes fall back to whitespace splitting.
    text = segment.replace("\\", "/").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _block(reason: str) -> int:
    print(f"Blocked by llm-handoff guard: {reason}.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
