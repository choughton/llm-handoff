from __future__ import annotations

from pathlib import Path
import re

import pytest

from llm_handoff import antigravity_permissions as perms
from llm_handoff.config import (
    CLAUDE_DEFAULT_ALLOWED_TOOLS,
    claude_default_allowed_tools,
)
from llm_handoff.provider_auth import ANTIGRAVITY_SETTINGS_PATH


def test_allowed_commands_are_exact_and_ordered() -> None:
    assert perms.ANTIGRAVITY_ALLOWED_COMMANDS == (
        "git status",
        "git diff",
        "git log",
        "git show",
        "git cat-file",
        "git add",
        "git commit",
        "git rev-parse",
    )


def test_interpreter_commands_are_exact_and_ordered() -> None:
    assert perms.ANTIGRAVITY_INTERPRETER_COMMANDS == ("python", "python3", "pytest")
    for command in perms.ANTIGRAVITY_INTERPRETER_COMMANDS:
        assert command not in perms.ANTIGRAVITY_ALLOWED_COMMANDS


_INTERPRETER_ALLOW = (
    "command(regex:^python( .*)?$)",
    "command(regex:^python3( .*)?$)",
    "command(regex:^pytest( .*)?$)",
)


def test_required_allow_entries_are_exact_and_ordered() -> None:
    assert perms.ANTIGRAVITY_REQUIRED_ALLOW == (
        "command(regex:^git status( .*)?$)",
        "command(regex:^git diff( .*)?$)",
        "command(regex:^git log( .*)?$)",
        "command(regex:^git show( .*)?$)",
        "command(regex:^git cat-file( .*)?$)",
        "command(regex:^git add( .*)?$)",
        "command(regex:^git commit( .*)?$)",
        "command(regex:^git rev-parse( .*)?$)",
    )
    assert perms.ANTIGRAVITY_REQUIRED_ALLOW == tuple(
        perms.antigravity_command_grant(command)
        for command in perms.ANTIGRAVITY_ALLOWED_COMMANDS
    )
    for entry in _INTERPRETER_ALLOW:
        assert entry not in perms.ANTIGRAVITY_REQUIRED_ALLOW


def test_required_allow_entries_mirror_claude_bash_rules() -> None:
    bash_rules = tuple(
        perms.antigravity_command_grant(rule[len("Bash(") : -len(":*)")])
        for rule in CLAUDE_DEFAULT_ALLOWED_TOOLS
        if rule.startswith("Bash(")
    )
    assert perms.ANTIGRAVITY_REQUIRED_ALLOW == bash_rules


def test_opt_in_allow_entries_mirror_claude_interpreter_rules() -> None:
    bash_rules = tuple(
        perms.antigravity_command_grant(rule[len("Bash(") : -len(":*)")])
        for rule in claude_default_allowed_tools(True)
        if rule.startswith("Bash(")
    )
    assert perms.antigravity_allow_entries((), interpreter_grants=True) == bash_rules
    assert bash_rules == (*perms.ANTIGRAVITY_REQUIRED_ALLOW, *_INTERPRETER_ALLOW)


def test_required_deny_entries_are_exact() -> None:
    assert perms.ANTIGRAVITY_REQUIRED_DENY == (
        "command(regex:^git( .*)? push( .*)?$)",
        "unsandboxed(regex:^git( .*)? push( .*)?$)",
    )


def test_command_grant_for_a_plain_command() -> None:
    assert (
        perms.antigravity_command_grant("  git status ")
        == "command(regex:^git status( .*)?$)"
    )


def test_command_grant_escapes_regex_metacharacters_only() -> None:
    assert (
        perms.antigravity_command_grant("a.b (c)")
        == "command(regex:^a\\.b \\(c\\)( .*)?$)"
    )
    assert (
        perms.antigravity_command_grant("x\\+*?|[]{}^$-y")
        == "command(regex:^x\\\\\\+\\*\\?\\|\\[\\]\\{\\}\\^\\$-y( .*)?$)"
    )


def _regex_pattern(entry: str) -> str:
    prefix = entry[: entry.index("(regex:") + len("(regex:")]
    assert entry.endswith(")")
    return entry[len(prefix) : -1]


@pytest.mark.parametrize(
    "command",
    (*perms.ANTIGRAVITY_ALLOWED_COMMANDS, *perms.ANTIGRAVITY_INTERPRETER_COMMANDS),
)
def test_required_allow_patterns_cover_arguments_but_not_longer_names(
    command: str,
) -> None:
    entry = perms.antigravity_command_grant(command)
    assert entry in perms.antigravity_allow_entries((), interpreter_grants=True)
    pattern = _regex_pattern(entry)

    assert re.fullmatch(pattern, command)
    assert re.fullmatch(pattern, f"{command} --x")
    assert re.fullmatch(pattern, f"{command}x") is None


@pytest.mark.parametrize(
    "line",
    [
        "git push",
        "git push origin main",
        "git -c core.pager=cat push origin main",
        "git -C . push origin main",
    ],
)
def test_required_deny_pattern_blocks_option_prefixed_pushes(line: str) -> None:
    for entry in perms.ANTIGRAVITY_REQUIRED_DENY:
        assert re.fullmatch(_regex_pattern(entry), line)


def test_required_deny_pattern_does_not_block_git_status() -> None:
    for entry in perms.ANTIGRAVITY_REQUIRED_DENY:
        assert re.fullmatch(_regex_pattern(entry), "git status") is None
        assert re.search(_regex_pattern(entry), "git status --short") is None


@pytest.mark.parametrize("interpreter_grants", [False, True])
def test_allowed_commands_line_up_with_allow_entries(interpreter_grants: bool) -> None:
    verification = ["python -m pytest", "pytest", "  ", "npm test", " npm test"]
    commands = perms.antigravity_allowed_commands(
        verification, interpreter_grants=interpreter_grants
    )

    if interpreter_grants:
        assert commands == (
            *perms.ANTIGRAVITY_ALLOWED_COMMANDS,
            *perms.ANTIGRAVITY_INTERPRETER_COMMANDS,
            "python -m pytest",
            "npm test",
        )
    else:
        assert commands == (*perms.ANTIGRAVITY_ALLOWED_COMMANDS, "npm test")
    assert perms.antigravity_allow_entries(
        verification, interpreter_grants=interpreter_grants
    ) == tuple(perms.antigravity_command_grant(command) for command in commands)


@pytest.mark.parametrize(
    "func", [perms.antigravity_allowed_commands, perms.antigravity_allow_entries]
)
def test_allow_helpers_require_the_interpreter_flag_keyword(func) -> None:
    with pytest.raises(TypeError):
        func(())
    with pytest.raises(TypeError):
        func((), False)


def test_python_verification_prefix_is_skipped_with_the_flag_off() -> None:
    verification = ["python -m pytest tests -q"]
    grant = "command(regex:^python -m pytest tests -q( .*)?$)"

    off = perms.antigravity_allow_entries(verification, interpreter_grants=False)
    on = perms.antigravity_allow_entries(verification, interpreter_grants=True)

    assert off == perms.ANTIGRAVITY_REQUIRED_ALLOW
    assert grant not in off
    for entry in _INTERPRETER_ALLOW:
        assert entry not in off
    assert on == (*perms.ANTIGRAVITY_REQUIRED_ALLOW, *_INTERPRETER_ALLOW, grant)
    # git push deny entries do not depend on the flag.
    assert perms.ANTIGRAVITY_REQUIRED_DENY == (
        "command(regex:^git( .*)? push( .*)?$)",
        "unsandboxed(regex:^git( .*)? push( .*)?$)",
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python", True),
        ("python -m pytest", True),
        ("  python3 -c x", True),
        ("pytest -q", True),
        ("pythonx", False),
        ("npm test", False),
        ("", False),
        ("uv run python", False),
    ],
)
def test_is_interpreter_command_checks_the_first_token(
    command: str, expected: bool
) -> None:
    assert perms.is_interpreter_command(command) is expected


def test_agent_tools_are_exact_and_ordered() -> None:
    assert perms.ANTIGRAVITY_AGENT_TOOLS == (
        "view_file",
        "list_dir",
        "grep_search",
        "find_by_name",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "run_command",
    )


def test_agent_tools_have_no_duplicates() -> None:
    tools = perms.ANTIGRAVITY_AGENT_TOOLS
    assert len(tools) == len(set(tools))


def test_agent_tools_contain_every_write_tool() -> None:
    assert perms.ANTIGRAVITY_AGENT_WRITE_TOOLS == (
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
    )
    for tool in perms.ANTIGRAVITY_AGENT_WRITE_TOOLS:
        assert tool in perms.ANTIGRAVITY_AGENT_TOOLS


@pytest.mark.parametrize(
    "tool",
    [
        "search_web",
        "read_url_content",
        "generate_image",
        "call_mcp_tool",
        "send_message",
        "schedule",
        "manage_task",
    ],
)
def test_agent_tools_leave_out_web_image_mcp_and_subagent_tools(tool: str) -> None:
    assert tool not in perms.ANTIGRAVITY_AGENT_TOOLS


def test_allow_entries_without_verification_commands() -> None:
    assert (
        perms.antigravity_allow_entries((), interpreter_grants=False)
        == perms.ANTIGRAVITY_REQUIRED_ALLOW
    )
    assert perms.antigravity_allow_entries((), interpreter_grants=True) == (
        *perms.ANTIGRAVITY_REQUIRED_ALLOW,
        *_INTERPRETER_ALLOW,
    )


@pytest.mark.parametrize("interpreter_grants", [False, True])
def test_allow_entries_add_a_verification_command(interpreter_grants: bool) -> None:
    entries = perms.antigravity_allow_entries(
        ["  npm test  "], interpreter_grants=interpreter_grants
    )

    assert entries[-1] == "command(regex:^npm test( .*)?$)"
    assert entries[:-1] == perms.antigravity_allow_entries(
        (), interpreter_grants=interpreter_grants
    )


def test_allow_entries_deduplicate_and_keep_first_seen_order() -> None:
    entries = perms.antigravity_allow_entries(
        ["python -m pytest", "pytest", "npm test", "python -m pytest", " npm test"],
        interpreter_grants=True,
    )

    assert entries == (
        *perms.ANTIGRAVITY_REQUIRED_ALLOW,
        *_INTERPRETER_ALLOW,
        "command(regex:^python -m pytest( .*)?$)",
        "command(regex:^npm test( .*)?$)",
    )
    assert len(entries) == len(set(entries))


def test_settings_path_is_under_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    path = perms.antigravity_settings_path()

    assert path == tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    assert ANTIGRAVITY_SETTINGS_PATH == "~/.gemini/antigravity-cli/settings.json"


def test_format_lists_every_entry_and_the_guardrail_note() -> None:
    allow = perms.antigravity_allow_entries(["npm test"], interpreter_grants=False)
    lines = perms.format_antigravity_permissions(
        allow, perms.ANTIGRAVITY_REQUIRED_DENY
    )

    assert ANTIGRAVITY_SETTINGS_PATH in lines[0]
    assert "  permissions.allow:" in lines
    assert "  permissions.deny:" in lines
    allow_index = lines.index("  permissions.allow:")
    deny_index = lines.index("  permissions.deny:")
    assert lines[allow_index + 1 : deny_index] == [f"    - {entry}" for entry in allow]
    assert lines[deny_index + 1 : deny_index + 3] == [
        "    - command(regex:^git( .*)? push( .*)?$)",
        "    - unsandboxed(regex:^git( .*)? push( .*)?$)",
    ]
    note = lines[-1]
    assert "every agy run for this user" in note
    assert "--sandbox" in note
    assert "only shell guardrail" in note


def test_format_output_is_ascii_only() -> None:
    lines = perms.format_antigravity_permissions(
        perms.antigravity_allow_entries(
            ["npm test\x1b[2J", "café test"], interpreter_grants=False
        ),
        perms.ANTIGRAVITY_REQUIRED_DENY,
    )

    for line in lines:
        line.encode("ascii")
        assert all(" " <= ch <= "~" for ch in line), line
    assert "    - command(regex:^npm test\\x1b\\[2J( .*)?$)" in lines
    assert "    - command(regex:^caf\\xe9 test( .*)?$)" in lines
