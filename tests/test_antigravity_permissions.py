from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff import antigravity_permissions as perms
from llm_handoff.config import CLAUDE_DEFAULT_ALLOWED_TOOLS
from llm_handoff.provider_auth import ANTIGRAVITY_SETTINGS_PATH


def test_required_allow_entries_are_exact_and_ordered() -> None:
    assert perms.ANTIGRAVITY_REQUIRED_ALLOW == (
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


def test_required_allow_entries_mirror_claude_bash_rules() -> None:
    bash_rules = tuple(
        f"command({rule[len('Bash('):-len(':*)')]})"
        for rule in CLAUDE_DEFAULT_ALLOWED_TOOLS
        if rule.startswith("Bash(")
    )
    assert perms.ANTIGRAVITY_REQUIRED_ALLOW == bash_rules


def test_required_deny_entries_are_exact() -> None:
    assert perms.ANTIGRAVITY_REQUIRED_DENY == (
        "command(git push)",
        "unsandboxed(git push)",
    )


def test_allow_entries_without_verification_commands() -> None:
    assert perms.antigravity_allow_entries(()) == perms.ANTIGRAVITY_REQUIRED_ALLOW


def test_allow_entries_add_a_verification_command() -> None:
    entries = perms.antigravity_allow_entries(["  npm test  "])

    assert entries == (*perms.ANTIGRAVITY_REQUIRED_ALLOW, "command(npm test)")


def test_allow_entries_deduplicate_and_keep_first_seen_order() -> None:
    entries = perms.antigravity_allow_entries(
        ["python -m pytest", "pytest", "npm test", "python -m pytest", " npm test"]
    )

    assert entries == (
        *perms.ANTIGRAVITY_REQUIRED_ALLOW,
        "command(python -m pytest)",
        "command(npm test)",
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
    allow = perms.antigravity_allow_entries(["npm test"])
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
        "    - command(git push)",
        "    - unsandboxed(git push)",
    ]
    note = lines[-1]
    assert "every agy run for this user" in note
    assert "--sandbox" in note
    assert "only shell guardrail" in note


def test_format_output_is_ascii_only() -> None:
    lines = perms.format_antigravity_permissions(
        perms.antigravity_allow_entries(["npm test\x1b[2J", "café test"]),
        perms.ANTIGRAVITY_REQUIRED_DENY,
    )

    for line in lines:
        line.encode("ascii")
        assert all(" " <= ch <= "~" for ch in line), line
    assert "    - command(npm test\\x1b[2J)" in lines
    assert "    - command(caf\\xe9 test)" in lines
