from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from llm_handoff import init_workflow as init_workflow_module
from llm_handoff.config import CLAUDE_DEFAULT_ALLOWED_TOOLS, load_dispatch_config
from llm_handoff.init_workflow import (
    ANTIGRAVITY_MAPPING,
    CLAUDE_MAPPING,
    CODEX_MAPPING,
    REFERENCE_MAPPING,
    InitConflictError,
    InitWorkflowError,
    InvalidVerificationCommandError,
    TrackerUnsupportedMappingError,
    UnknownProviderError,
    UnknownTemplateError,
    check_verification_command_chars,
    init_reference_workflow,
    render_config,
    resolve_provider_mapping,
    template_files,
    verification_allowed_tools,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "examples" / "reference-workflow"


@pytest.mark.parametrize("name", ["AGENTS.md", ".geminiignore"])
def test_unmarked_template_migrates_without_duplication(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    original = (TEMPLATE_ROOT / name).read_bytes()
    path.write_bytes(original)
    preview = init_reference_workflow(tmp_path, dry_run=True)
    assert Path(name) in preview.merged
    assert path.read_bytes() == original
    result = init_reference_workflow(tmp_path)
    assert Path(name) in result.merged
    begin, end = (
        (b"<!-- llm-handoff:begin -->", b"<!-- llm-handoff:end -->")
        if name == "AGENTS.md"
        else (b"# llm-handoff:begin", b"# llm-handoff:end")
    )
    content = path.read_bytes()
    body = original if name == "AGENTS.md" else original.replace(b"\r\n", b"\n")
    assert content == begin + b"\n" + body.rstrip(b"\r\n") + b"\n" + end + b"\n"
    second = init_reference_workflow(tmp_path)
    assert second.copied == second.merged == second.conflicts == ()
    assert path.read_bytes() == content


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "name", ["AGENTS.md", ".gitignore", ".geminiignore", ".claude/settings.json"]
)
@pytest.mark.parametrize("dangling", [False, True])
def test_linked_managed_file_conflicts(
    tmp_path: Path, name: str, force: bool, dangling: bool
) -> None:
    root = tmp_path / "repo"
    path = root / name
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    original = b"{}" if name.endswith(".json") else b"private content\n"
    if not dangling:
        outside.write_bytes(original)
    try:
        path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    assert Path(name) in init_reference_workflow(root, dry_run=True, force=force).conflicts
    with pytest.raises(InitConflictError) as error:
        init_reference_workflow(root, force=force)
    assert Path(name) in error.value.conflicts
    assert path.is_symlink()
    assert outside.exists() is not dangling
    if not dangling:
        assert outside.read_bytes() == original
    assert not (root / "dispatch_config.yaml").exists()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "name", ["AGENTS.md", ".gitignore", ".geminiignore", ".claude/settings.json"]
)
def test_hard_linked_managed_file_conflicts(
    tmp_path: Path, name: str, force: bool
) -> None:
    root = tmp_path / "repo"
    path = root / name
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    original = b"{}" if name.endswith(".json") else b"private content\n"
    outside.write_bytes(original)
    path.hardlink_to(outside)
    assert path.stat().st_nlink == 2

    preview = init_reference_workflow(root, dry_run=True, force=force)
    assert preview.conflicts == (Path(name),)
    with pytest.raises(InitConflictError) as error:
        init_reference_workflow(root, force=force)
    assert error.value.conflicts == preview.conflicts
    assert path.read_bytes() == outside.read_bytes() == original
    assert path.stat().st_nlink == 2
    assert not (root / "dispatch_config.yaml").exists()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("parent", [".claude", ".claude/agents", "docs/handoff"])
def test_non_directory_destination_parent_conflicts(
    tmp_path: Path, parent: str, force: bool
) -> None:
    root = tmp_path / "repo"
    path = root / parent
    path.parent.mkdir(parents=True)
    path.write_bytes(b"keep this file\n")
    expected = tuple(p for p in template_files() if Path(parent) in p.parents)
    assert expected

    preview = init_reference_workflow(root, dry_run=True, force=force)
    assert preview.conflicts == expected
    with pytest.raises(InitConflictError) as error:
        init_reference_workflow(root, force=force)
    assert error.value.conflicts == expected
    assert path.read_bytes() == b"keep this file\n"
    assert not (root / "AGENTS.md").exists()


@pytest.mark.parametrize("force", [False, True])
def test_linked_settings_parent_conflicts(tmp_path: Path, force: bool) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    settings = outside / "settings.json"
    settings.write_bytes(b"{}")
    try:
        (root / ".claude").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    preview = init_reference_workflow(root, dry_run=True, force=force)
    assert Path(".claude/settings.json") in preview.conflicts
    with pytest.raises(InitConflictError):
        init_reference_workflow(root, force=force)
    assert settings.read_bytes() == b"{}"
    assert list(outside.iterdir()) == [settings]
    assert not (root / "AGENTS.md").exists()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_junction_settings_parent_without_pathlib_support(
    tmp_path: Path, force: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the Python 3.11 API surface even on newer interpreters.
    monkeypatch.delattr(Path, "is_junction", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    settings = outside / "settings.json"
    settings.write_bytes(b"{}")
    junction = root / ".claude"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True, capture_output=True,
    )
    try:
        preview = init_reference_workflow(root, dry_run=True, force=force)
        assert Path(".claude/settings.json") in preview.conflicts
        with pytest.raises(InitConflictError) as error:
            init_reference_workflow(root, force=force)
        assert Path(".claude/settings.json") in error.value.conflicts
        assert settings.read_bytes() == b"{}"
        assert list(outside.iterdir()) == [settings]
        assert not (root / "AGENTS.md").exists()
    finally:
        junction.rmdir()


@pytest.mark.parametrize("force", [False, True])
def test_managed_files_preserve_content_and_repeat(tmp_path: Path, force: bool) -> None:
    originals = {
        Path("AGENTS.md"): b"# Custom\r\nKeep these instructions.",
        Path(".gitignore"): b"custom/\r\n",
        Path(".geminiignore"): b"private/\n",
    }
    for path, content in originals.items():
        (tmp_path / path).write_bytes(content)
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    custom = {
        "permissions": {"deny": ["Custom", "WebFetch", "Custom"], "allow": ["Read"]},
        "hooks": {"Stop": [{"custom": True}], "PreToolUse": []},
        "custom": {"nested": [1, True, "keep"]},
    }
    settings.write_text(json.dumps(custom), encoding="utf-8")
    result = init_reference_workflow(tmp_path, force=force)
    assert set(result.merged) == {*originals, Path(".claude/settings.json")}
    for path, content in originals.items():
        assert (tmp_path / path).read_bytes().startswith(content)
    merged = json.loads(settings.read_bytes())
    template = json.loads((TEMPLATE_ROOT / ".claude/settings.json").read_bytes())
    assert merged["permissions"]["deny"] == list(
        dict.fromkeys(custom["permissions"]["deny"] + template["permissions"]["deny"])
    )
    assert merged["permissions"]["allow"] == ["Read"]
    assert merged["custom"] == custom["custom"]
    assert merged["hooks"]["Stop"] == custom["hooks"]["Stop"]
    assert merged["hooks"]["PreToolUse"] == template["hooks"]["PreToolUse"]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    second = init_reference_workflow(tmp_path)
    assert second.copied == second.merged == second.conflicts == ()
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize("name", ["AGENTS.md", ".gitignore", ".geminiignore"])
def test_managed_block_replaced_in_place(tmp_path: Path, name: str) -> None:
    begin, end = (
        (b"<!-- llm-handoff:begin -->", b"<!-- llm-handoff:end -->")
        if name == "AGENTS.md"
        else (b"# llm-handoff:begin", b"# llm-handoff:end")
    )
    path = tmp_path / name
    prefix, suffix = b"custom\r\n\r\n", b"\r\nuser suffix\r\n"
    path.write_bytes(prefix + begin + b"\r\nstale\r\n" + end + suffix)
    result = init_reference_workflow(tmp_path)
    content = path.read_bytes()
    assert Path(name) in result.merged
    assert content.startswith(prefix + begin)
    assert content.endswith(end + suffix)
    assert b"\r\nstale\r\n" not in content
    assert content.count(begin) == content.count(end) == 1
    assert not init_reference_workflow(tmp_path).merged


@pytest.mark.parametrize("name", ["AGENTS.md", ".gitignore", ".geminiignore"])
@pytest.mark.parametrize(
    "shape",
    ["{b}", "{e}", "{e}\n{b}", "{b}\n{b}\n{e}", "{b}\n{e}\n{b}\n{e}", "text{b}\n{e}"],
)
@pytest.mark.parametrize("force", [False, True])
def test_malformed_markers_conflict(
    tmp_path: Path, name: str, shape: str, force: bool
) -> None:
    begin, end = (
        ("<!-- llm-handoff:begin -->", "<!-- llm-handoff:end -->")
        if name == "AGENTS.md"
        else ("# llm-handoff:begin", "# llm-handoff:end")
    )
    path = tmp_path / name
    original = shape.format(b=begin, e=end).encode()
    path.write_bytes(original)
    assert init_reference_workflow(tmp_path, dry_run=True, force=force).conflicts == (
        Path(name),
    )
    with pytest.raises(InitConflictError) as error:
        init_reference_workflow(tmp_path, force=force)
    assert error.value.conflicts == (Path(name),)
    assert path.read_bytes() == original
    assert not (tmp_path / "dispatch_config.yaml").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"[]",
        b'{"permissions": null}',
        b'{"permissions": {"deny": "bad"}}',
        b'{"hooks": {"PreToolUse": [null]}}',
        b'{"hooks": {"PreToolUse": [{"hooks": [null]}]}}',
    ],
)
def test_invalid_settings_conflict(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    path.write_bytes(content)
    with pytest.raises(InitConflictError) as error:
        init_reference_workflow(tmp_path, force=True)
    assert error.value.conflicts == (Path(".claude/settings.json"),)
    assert path.read_bytes() == content


def test_settings_hook_identity_uses_matcher_and_command(tmp_path: Path) -> None:
    template = json.loads((TEMPLATE_ROOT / ".claude/settings.json").read_bytes())
    entry = template["hooks"]["PreToolUse"][0]
    entry["hooks"][0]["timeout"] = 99
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps(template), encoding="utf-8")
    init_reference_workflow(tmp_path)
    assert json.loads(path.read_bytes()) == template


def test_new_gitignore_matches_template(tmp_path: Path) -> None:
    init_reference_workflow(tmp_path)
    assert (tmp_path / ".gitignore").read_bytes() == (
        TEMPLATE_ROOT / ".gitignore"
    ).read_bytes()


def test_init_reference_workflow_copies_template_files(tmp_path: Path) -> None:
    result = init_reference_workflow(tmp_path)

    assert result.copied
    assert result.conflicts == ()
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "PROJECT_STATE.md").is_file()
    assert (tmp_path / "dispatch_config.yaml").is_file()
    assert (tmp_path / "docs" / "handoff" / "HANDOFF.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "llm-handoff" / "SKILL.md").is_file()
    assert (tmp_path / ".gemini" / "agents" / "planner.md").is_file()
    assert (tmp_path / ".claude" / "agents" / "auditor.md").is_file()
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert (tmp_path / ".claude" / "hooks" / "block_dangerous_commands.py").is_file()
    assert (tmp_path / ".gemini" / "policies" / "planner_guardrails.toml").is_file()
    assert not (tmp_path / "README.md").exists()


def test_template_files_skip_python_bytecode(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "hooks" / "__pycache__").mkdir(parents=True)
    (tmp_path / ".claude" / "hooks" / "guard.py").write_text("", encoding="utf-8")
    (
        tmp_path / ".claude" / "hooks" / "__pycache__" / "guard.cpython-311.pyc"
    ).write_bytes(b"\x00")
    (tmp_path / "stray.pyc").write_bytes(b"\x00")

    assert template_files(tmp_path) == (Path(".claude/hooks/guard.py"),)


def test_init_reference_workflow_dry_run_does_not_write(tmp_path: Path) -> None:
    result = init_reference_workflow(tmp_path, dry_run=True)

    assert result.dry_run is True
    assert result.copied
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "docs").exists()


def test_init_reference_workflow_skips_identical_files(tmp_path: Path) -> None:
    first = init_reference_workflow(tmp_path)
    second = init_reference_workflow(tmp_path)

    assert first.copied
    assert second.copied == ()
    assert Path("AGENTS.md") in second.skipped
    assert second.conflicts == ()


def test_init_reference_workflow_fails_on_existing_different_file(
    tmp_path: Path,
) -> None:
    target_file = tmp_path / "PROJECT_STATE.md"
    target_file.write_text("custom repo instructions\n", encoding="utf-8")

    with pytest.raises(InitConflictError) as exc_info:
        init_reference_workflow(tmp_path)

    assert Path("PROJECT_STATE.md") in exc_info.value.conflicts
    assert target_file.read_text(encoding="utf-8") == "custom repo instructions\n"


def test_init_reference_workflow_force_overwrites_conflicts(tmp_path: Path) -> None:
    target_file = tmp_path / "PROJECT_STATE.md"
    target_file.write_text("custom repo instructions\n", encoding="utf-8")

    result = init_reference_workflow(tmp_path, force=True)

    assert Path("PROJECT_STATE.md") in result.copied
    assert result.conflicts == ()
    assert target_file.read_text(encoding="utf-8") == (
        TEMPLATE_ROOT / "PROJECT_STATE.md"
    ).read_text(encoding="utf-8")


def test_init_reference_workflow_force_does_not_overwrite_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").mkdir()

    with pytest.raises(InitConflictError) as exc_info:
        init_reference_workflow(tmp_path, force=True)

    assert Path("AGENTS.md") in exc_info.value.conflicts


def test_init_reference_workflow_rejects_unknown_template(tmp_path: Path) -> None:
    with pytest.raises(UnknownTemplateError):
        init_reference_workflow(tmp_path, template="missing-template")


def test_init_provider_claude_writes_an_all_claude_config(tmp_path: Path) -> None:
    result = init_reference_workflow(
        tmp_path,
        provider="claude",
        available_providers=("claude", "codex", "gemini"),
    )

    assert result.provider_mapping == CLAUDE_MAPPING
    config = load_dispatch_config(
        repo_root=tmp_path,
        config_path=tmp_path / "dispatch_config.yaml",
    )
    assert {agent.provider for agent in config.agents.values()} == {"claude"}
    assert config.audit.security_provider == "claude"
    assert config.normalizer.provider == "claude"
    assert config.commit_mode == "agent"
    for name in ("planner", "backend", "frontend"):
        assert (tmp_path / ".claude" / "agents" / f"{name}.md").is_file()


def test_init_auto_detects_a_claude_only_machine(tmp_path: Path) -> None:
    result = init_reference_workflow(tmp_path, available_providers=("claude",))

    assert result.provider_mapping == CLAUDE_MAPPING
    assert result.detected_providers == ("claude",)
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "provider: gemini" not in text
    assert "provider: codex" not in text


@pytest.mark.parametrize(
    "available",
    [
        (),
        ("claude", "codex"),
        ("codex", "gemini"),
        ("claude", "codex", "gemini"),
        ("gemini",),
        ("claude", "agy"),
        ("codex", "agy"),
        ("agy", "gemini"),
        ("claude", "codex", "agy"),
    ],
)
def test_init_keeps_the_reference_mapping_unless_a_single_provider(
    tmp_path: Path,
    available: tuple[str, ...],
) -> None:
    result = init_reference_workflow(tmp_path, available_providers=available)

    assert result.provider_mapping == REFERENCE_MAPPING
    assert (tmp_path / "dispatch_config.yaml").read_bytes() == (
        TEMPLATE_ROOT / "dispatch_config.yaml"
    ).read_bytes()


def test_init_claude_mapping_is_repeatable(tmp_path: Path) -> None:
    init_reference_workflow(tmp_path, provider="claude", available_providers=())
    second = init_reference_workflow(
        tmp_path, provider="claude", available_providers=()
    )

    assert second.copied == ()
    assert Path("dispatch_config.yaml") in second.skipped
    assert second.conflicts == ()


def test_init_reports_a_mapping_change_as_a_config_conflict(tmp_path: Path) -> None:
    init_reference_workflow(tmp_path, available_providers=("claude", "codex"))

    with pytest.raises(InitConflictError) as exc_info:
        init_reference_workflow(tmp_path, provider="claude", available_providers=())

    assert exc_info.value.conflicts == (Path("dispatch_config.yaml"),)


def test_init_rejects_an_unknown_provider_mapping(tmp_path: Path) -> None:
    with pytest.raises(UnknownProviderError) as error:
        init_reference_workflow(tmp_path, provider="openai", available_providers=())
    assert "Available mappings: claude, codex, antigravity." in str(error.value)
    assert not (tmp_path / "AGENTS.md").exists()


CODEX_ROLE_SKILLS = {
    "planner": "llm-handoff-planner",
    "backend": "llm-handoff",
    "frontend": "llm-handoff-frontend",
    "auditor": "llm-handoff-auditor",
    "validator": "llm-handoff-validator",
    "finalizer": "llm-handoff-finalizer",
}


def _template_config_text() -> str:
    return (TEMPLATE_ROOT / "dispatch_config.yaml").read_text(encoding="utf-8")


def test_render_config_codex_puts_every_role_on_codex() -> None:
    template = _template_config_text()
    rendered = render_config(template, CODEX_MAPPING)
    data = yaml.safe_load(rendered)
    reference = yaml.safe_load(template)

    assert set(data["agents"]) == set(CODEX_ROLE_SKILLS)
    for role, skill_name in CODEX_ROLE_SKILLS.items():
        agent = data["agents"][role]
        assert agent == {
            "provider": "codex",
            "binary": "codex",
            "skill_name": skill_name,
            "resume": False,
            "timeout_ms": reference["agents"][role]["timeout_ms"],
        }, role
    assert data["audit"]["security_provider"] == "codex"
    assert "  # Same vendor as the implementer: the only CLI available.\n" in rendered
    assert data["normalizer"]["provider"] == "codex"
    assert "model" not in data["normalizer"]
    assert "claude-haiku-5-5" not in rendered
    assert "Claude model" not in rendered
    assert "provider: claude" not in rendered
    assert "provider: gemini" not in rendered


def test_render_config_codex_uses_the_all_claude_timeouts() -> None:
    template = _template_config_text()
    claude = yaml.safe_load(render_config(template, CLAUDE_MAPPING))["agents"]
    codex = yaml.safe_load(render_config(template, CODEX_MAPPING))["agents"]

    assert {role: agent["timeout_ms"] for role, agent in codex.items()} == {
        role: agent["timeout_ms"] for role, agent in claude.items()
    }


@pytest.mark.parametrize(
    "mapping", [CLAUDE_MAPPING, CODEX_MAPPING, ANTIGRAVITY_MAPPING]
)
@pytest.mark.parametrize(
    "anchor",
    [
        "agents:\n",
        "normalizer:\n",
        "  # security_provider: claude   # claude | codex | antigravity; "
        "unset = auto\n",
    ],
)
def test_render_config_rejects_a_template_without_an_anchor(
    mapping: str, anchor: str
) -> None:
    template = _template_config_text()
    assert anchor in template
    broken = template.replace(anchor, "")

    with pytest.raises(InitWorkflowError):
        render_config(broken, mapping)


@pytest.mark.parametrize(
    "replacement",
    [
        "normalizer:\n",
        "normalizer:\n  provider: claude\n",
        "normalizer:\n  provider: codex\n  model: claude-haiku-5-5\n",
    ],
)
@pytest.mark.parametrize("mapping", [CODEX_MAPPING, ANTIGRAVITY_MAPPING])
def test_render_config_rejects_a_template_without_the_normalizer_lines(
    mapping: str,
    replacement: str,
) -> None:
    template = _template_config_text()
    original = "normalizer:\n  provider: claude\n  model: claude-haiku-5-5\n"
    assert template.count(original) == 1
    broken = template.replace(original, replacement)

    with pytest.raises(InitWorkflowError):
        render_config(broken, mapping)


def test_render_config_codex_switches_commit_mode_to_dispatcher() -> None:
    template = _template_config_text()
    rendered = render_config(template, CODEX_MAPPING)

    assert template.count("\ncommit_mode: agent\n") == 1
    assert "\ncommit_mode: agent\n" not in rendered
    assert rendered.count("\ncommit_mode: dispatcher\n") == 1
    data = yaml.safe_load(rendered)
    assert data["commit_mode"] == "dispatcher"
    # dispatcher commits need the work-claim gate, which the template keeps on.
    assert data["enforce_work_packet"] is True
    # The explanatory comment above the template line survives the swap.
    assert "# Who commits implementer work: agent (default) or dispatcher." in rendered
    assert "keeps .git read-only" in rendered


def test_render_config_commit_mode_stays_agent_outside_the_codex_mapping() -> None:
    template = _template_config_text()

    assert yaml.safe_load(template)["commit_mode"] == "agent"
    assert render_config(template, REFERENCE_MAPPING) == template
    for mapping in (CLAUDE_MAPPING, ANTIGRAVITY_MAPPING):
        rendered = render_config(template, mapping)
        assert rendered.count("\ncommit_mode: agent\n") == 1, mapping
        assert "commit_mode: dispatcher\n" not in rendered, mapping
        assert yaml.safe_load(rendered)["commit_mode"] == "agent", mapping


@pytest.mark.parametrize(
    "replacement",
    [
        "",
        "commit_mode: dispatcher\n",
        "  commit_mode: agent\n",
        "commit_mode: agent\ncommit_mode: agent\n",
        "# commit_mode: agent\n",
    ],
)
def test_render_config_codex_rejects_a_template_without_the_commit_mode_line(
    replacement: str,
) -> None:
    template = _template_config_text()
    original = "commit_mode: agent\n"
    assert template.count(original) == 1
    broken = template.replace(original, replacement)

    with pytest.raises(InitWorkflowError, match="commit_mode: agent"):
        render_config(broken, CODEX_MAPPING)
    # The other mappings never touch the line, so they still render.
    assert render_config(broken, CLAUDE_MAPPING)
    assert render_config(broken, ANTIGRAVITY_MAPPING)
    assert render_config(broken, REFERENCE_MAPPING) == broken


def test_init_provider_codex_writes_a_loadable_all_codex_config(
    tmp_path: Path,
) -> None:
    result = init_reference_workflow(
        tmp_path,
        provider="codex",
        available_providers=("claude", "codex", "gemini"),
    )

    assert result.provider_mapping == CODEX_MAPPING
    config = load_dispatch_config(
        repo_root=tmp_path,
        config_path=tmp_path / "dispatch_config.yaml",
    )
    assert set(config.agents) == set(CODEX_ROLE_SKILLS)
    for role, skill_name in CODEX_ROLE_SKILLS.items():
        assert config.agents[role].provider == "codex", role
        assert config.agents[role].skill_name == skill_name, role
    assert config.audit.security_provider == "codex"
    assert config.normalizer.provider == "codex"
    assert config.commit_mode == "dispatcher"
    assert config.enforce_work_packet is True
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert text.count("\ncommit_mode: dispatcher\n") == 1
    assert "commit_mode: agent\n" not in text
    for skill_name in CODEX_ROLE_SKILLS.values():
        assert (tmp_path / ".codex" / "skills" / skill_name / "SKILL.md").is_file()


def test_init_auto_detects_a_codex_only_machine(tmp_path: Path) -> None:
    result = init_reference_workflow(tmp_path, available_providers=("codex",))

    assert result.provider_mapping == CODEX_MAPPING
    assert result.detected_providers == ("codex",)
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "provider: gemini" not in text
    assert "provider: claude" not in text
    assert text == render_config(_template_config_text(), CODEX_MAPPING)
    config = load_dispatch_config(
        repo_root=tmp_path,
        config_path=tmp_path / "dispatch_config.yaml",
    )
    assert config.commit_mode == "dispatcher"


def test_init_codex_mapping_is_repeatable(tmp_path: Path) -> None:
    init_reference_workflow(tmp_path, provider="codex", available_providers=())
    second = init_reference_workflow(
        tmp_path, provider="codex", available_providers=()
    )

    assert second.copied == ()
    assert Path("dispatch_config.yaml") in second.skipped
    assert second.conflicts == ()


def test_verification_command_with_codex_mapping_changes_no_agent(
    tmp_path: Path,
) -> None:
    init_reference_workflow(
        tmp_path,
        provider="codex",
        available_providers=(),
        verification_command="npm test",
    )

    tools = _loaded_allowed_tools(tmp_path)
    assert all(value is None for value in tools.values())
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "allowed_tools" not in text
    assert yaml.safe_load(text)["verification_allowed_prefixes"] == ["npm test"]


def test_init_keeps_the_reference_mapping_on_a_gemini_only_machine(
    tmp_path: Path,
) -> None:
    result = init_reference_workflow(tmp_path, available_providers=("gemini",))

    assert result.provider_mapping == REFERENCE_MAPPING
    assert result.detected_providers == ("gemini",)
    assert (tmp_path / "dispatch_config.yaml").read_bytes() == (
        TEMPLATE_ROOT / "dispatch_config.yaml"
    ).read_bytes()


ANTIGRAVITY_ROLES = ("planner", "backend", "frontend", "auditor", "validator", "finalizer")


def test_render_config_antigravity_puts_every_role_on_antigravity() -> None:
    template = _template_config_text()
    rendered = render_config(template, ANTIGRAVITY_MAPPING)
    data = yaml.safe_load(rendered)
    reference = yaml.safe_load(template)

    assert set(data["agents"]) == set(ANTIGRAVITY_ROLES)
    for role in ANTIGRAVITY_ROLES:
        agent = data["agents"][role]
        assert agent == {
            "provider": "antigravity",
            "binary": "agy",
            "resume": False,
            "timeout_ms": reference["agents"][role]["timeout_ms"],
        }, role
    assert "unsafe_bypass" not in rendered
    assert data["audit"]["security_provider"] == "antigravity"
    assert (
        "  # Same vendor as the implementer: the only CLI available.\n"
        "  security_provider: antigravity\n"
    ) in rendered
    assert data["normalizer"]["provider"] == "antigravity"
    assert "model" not in data["normalizer"]
    assert "agy CLI login" in rendered
    assert "claude-haiku-5-5" not in rendered
    assert "provider: claude" not in rendered
    assert "provider: codex" not in rendered
    assert "provider: gemini" not in rendered
    # Template defaults the mapping leaves alone.
    assert data["commit_mode"] == reference["commit_mode"]
    assert "interpreter_grants" not in data
    assert "# interpreter_grants: false\n" in rendered


def test_render_config_antigravity_uses_the_all_claude_timeouts() -> None:
    template = _template_config_text()
    claude = yaml.safe_load(render_config(template, CLAUDE_MAPPING))["agents"]
    agy = yaml.safe_load(render_config(template, ANTIGRAVITY_MAPPING))["agents"]

    assert {role: agent["timeout_ms"] for role, agent in agy.items()} == {
        role: agent["timeout_ms"] for role, agent in claude.items()
    }


def test_init_provider_antigravity_writes_a_loadable_all_agy_config(
    tmp_path: Path,
) -> None:
    result = init_reference_workflow(
        tmp_path,
        provider="antigravity",
        available_providers=("claude", "codex", "agy"),
    )

    assert result.provider_mapping == ANTIGRAVITY_MAPPING
    config = load_dispatch_config(
        repo_root=tmp_path,
        config_path=tmp_path / "dispatch_config.yaml",
    )
    assert set(config.agents) == set(ANTIGRAVITY_ROLES)
    for role in ANTIGRAVITY_ROLES:
        assert config.agents[role].provider == "antigravity", role
        assert config.agents[role].binary == "agy", role
        assert config.agents[role].agent_name is None, role
        assert config.agents[role].unsafe_bypass is False, role
    assert config.audit.security_provider == "antigravity"
    assert config.normalizer.provider == "antigravity"
    assert config.normalizer.model == "gemini-3.8-flash-low"


def test_init_auto_detects_an_agy_only_machine(tmp_path: Path) -> None:
    result = init_reference_workflow(tmp_path, available_providers=("agy",))

    assert result.provider_mapping == ANTIGRAVITY_MAPPING
    assert result.detected_providers == ("agy",)
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert text == render_config(_template_config_text(), ANTIGRAVITY_MAPPING)


def test_init_antigravity_mapping_is_repeatable(tmp_path: Path) -> None:
    init_reference_workflow(tmp_path, provider="antigravity", available_providers=())
    second = init_reference_workflow(
        tmp_path, provider="antigravity", available_providers=()
    )

    assert second.copied == ()
    assert Path("dispatch_config.yaml") in second.skipped
    assert second.conflicts == ()


def test_verification_command_with_antigravity_mapping_changes_no_agent(
    tmp_path: Path,
) -> None:
    init_reference_workflow(
        tmp_path,
        provider="antigravity",
        available_providers=(),
        verification_command="npm test",
    )

    tools = _loaded_allowed_tools(tmp_path)
    assert all(value is None for value in tools.values())
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "allowed_tools" not in text
    data = yaml.safe_load(text)
    assert data["verification_allowed_prefixes"] == ["npm test"]
    assert data["agents"] == yaml.safe_load(
        render_config(_template_config_text(), ANTIGRAVITY_MAPPING)
    )["agents"]


def _antigravity_launch_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, set[str]]:
    """Return the `agy --agent` names each all-agy role launches with.

    The provider command runner is stubbed, so no CLI starts. Each role is
    invoked the way the dispatcher invokes it: planner, backend, and frontend
    through `invoke_role` with no subagent name; auditor, validator, and
    finalizer through `invoke_support_role` with the subagent names the
    orchestrator and ledger pass (`auditor`; `handoff-validator` and
    `handoff-router`; `ledger-updater`).
    """

    from llm_handoff import agent_roles
    from llm_handoff.agent_providers import antigravity as agy_provider
    from llm_handoff.agent_types import _ProcessResult

    target = tmp_path / "target"
    init_reference_workflow(target, provider="antigravity", available_providers=())
    config = load_dispatch_config(
        repo_root=target, config_path=target / "dispatch_config.yaml"
    )
    seen: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> _ProcessResult:
        seen.append(command[command.index("--agent") + 1])
        return _ProcessResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(agy_provider, "_run_antigravity_command", fake_run)
    handoff = target / "docs" / "handoff" / "HANDOFF.md"
    launches: dict[str, set[str]] = {}
    for role in ("planner", "backend", "frontend"):
        seen.clear()
        agent_roles.invoke_role(
            role, handoff, agent_config=config.agents[role], repo_root=target
        )
        launches[role] = set(seen)
    for role, subagents in (
        ("auditor", ("auditor",)),
        ("validator", ("handoff-validator", "handoff-router")),
        ("finalizer", ("ledger-updater",)),
    ):
        seen.clear()
        for subagent in subagents:
            agent_roles.invoke_support_role(
                subagent,
                "prompt",
                role=role,
                handoff_path=handoff,
                agent_config=config.agents[role],
                repo_root=target,
            )
        launches[role] = set(seen)
    return launches


def test_every_all_agy_role_resolves_to_a_shipped_agent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = _antigravity_launch_agents(tmp_path, monkeypatch)

    assert set(launches) == set(ANTIGRAVITY_ROLES)
    assert launches["validator"] == {"handoff-validator", "handoff-router"}
    assert launches["finalizer"] == {"ledger-updater"}
    for role, names in launches.items():
        assert names, role
        for name in names:
            for root in (REPO_ROOT, TEMPLATE_ROOT):
                path = root / ".agents" / "agents" / f"{name}.md"
                assert path.is_file(), (role, path)


def _loaded_allowed_tools(root: Path) -> dict[str, tuple[str, ...] | None]:
    config = load_dispatch_config(
        repo_root=root,
        config_path=root / "dispatch_config.yaml",
    )
    return {name: agent.allowed_tools for name, agent in config.agents.items()}


def test_verification_command_adds_allowed_tools_to_reference_claude_roles(
    tmp_path: Path,
) -> None:
    result = init_reference_workflow(
        tmp_path, available_providers=(), verification_command="npm test"
    )

    assert result.provider_mapping == REFERENCE_MAPPING
    assert result.verification_command == "npm test"
    expected = (*CLAUDE_DEFAULT_ALLOWED_TOOLS, "Bash(npm:*)")
    tools = _loaded_allowed_tools(tmp_path)
    for name in ("auditor", "validator", "finalizer"):
        assert tools[name] == expected
    for name in ("planner", "backend", "frontend"):
        assert tools[name] is None


def test_verification_command_only_inserts_allowed_tools_and_allowlist(
    tmp_path: Path,
) -> None:
    init_reference_workflow(
        tmp_path, available_providers=(), verification_command="npm test"
    )

    template = (TEMPLATE_ROOT / "dispatch_config.yaml").read_bytes()
    rendered = (tmp_path / "dispatch_config.yaml").read_bytes()
    newline = b"\r\n" if b"\r\n" in template else b"\n"
    block = newline.join(
        [b"    allowed_tools:"]
        + [f"      - {rule}".encode() for rule in CLAUDE_DEFAULT_ALLOWED_TOOLS]
        + [b"      - Bash(npm:*)", b""]
    )
    allowlist = newline.join(
        [b"verification_allowed_prefixes:", b'  - "npm test"', b""]
    )
    assert rendered.count(block) == 3
    assert rendered.count(allowlist) == 1
    assert rendered.replace(block, b"").replace(allowlist, b"") == template
    timeout_line = b"work_packet_verification_timeout_seconds: 600" + newline
    assert timeout_line + allowlist in rendered


@pytest.mark.parametrize("provider", [None, "claude"])
def test_verification_command_writes_a_loadable_allowlist(
    tmp_path: Path, provider: str | None
) -> None:
    command = 'python -m pytest tests -q -k "not slow"'
    init_reference_workflow(
        tmp_path,
        provider=provider,
        available_providers=(),
        verification_command=command,
    )

    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "verification_allowed_prefixes:\n" in text.replace("\r\n", "\n")
    assert f"  - {json.dumps(command)}" in text
    assert yaml.safe_load(text)["verification_allowed_prefixes"] == [command]
    config = load_dispatch_config(
        repo_root=tmp_path, config_path=tmp_path / "dispatch_config.yaml"
    )
    assert config.verification_allowed_prefixes == (command,)


@pytest.mark.parametrize("provider", [None, "claude"])
def test_no_verification_command_leaves_the_allowlist_unset(
    tmp_path: Path, provider: str | None
) -> None:
    init_reference_workflow(tmp_path, provider=provider, available_providers=())

    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "verification_allowed_prefixes" not in yaml.safe_load(text)
    assert not any(
        line.startswith("verification_allowed_prefixes")
        for line in text.splitlines()
    )


@pytest.mark.parametrize(
    ("command", "char"),
    [
        ("pytest && whoami", "&"),
        ("pytest | tee out", "|"),
        ("pytest > out.txt", ">"),
        ("pytest $(whoami)", "$"),
        ("pytest `whoami`", "`"),
        ("pytest %CD%", "%"),
        ("pytest\nwhoami", "\n"),
    ],
)
def test_verification_command_with_shell_control_char_is_rejected(
    tmp_path: Path, command: str, char: str
) -> None:
    with pytest.raises(InvalidVerificationCommandError) as error:
        init_reference_workflow(
            tmp_path, available_providers=(), verification_command=command
        )
    assert repr(char) in str(error.value)
    assert error.value.token == char
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "command",
    [
        "python  -m pytest",
        "python -m pytest\ttests",
        "python -m pytest  -q",
        "  python   -m pytest\ttests",
        "npm\x0ctest",
    ],
)
def test_verification_command_with_irregular_whitespace_is_rejected(
    tmp_path: Path, command: str
) -> None:
    with pytest.raises(InvalidVerificationCommandError) as error:
        init_reference_workflow(
            tmp_path, available_providers=(), verification_command=command
        )
    assert "single spaces between words" in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_verification_command_with_surrounding_whitespace_is_accepted() -> None:
    check_verification_command_chars("  python -m pytest -q\t")


def test_verification_command_with_claude_mapping_covers_all_roles(
    tmp_path: Path,
) -> None:
    init_reference_workflow(
        tmp_path,
        provider="claude",
        available_providers=(),
        verification_command="npm test",
    )

    expected = (*CLAUDE_DEFAULT_ALLOWED_TOOLS, "Bash(npm:*)")
    tools = _loaded_allowed_tools(tmp_path)
    assert set(tools) == {
        "planner", "backend", "frontend", "auditor", "validator", "finalizer"
    }
    assert all(value == expected for value in tools.values())


@pytest.mark.parametrize("executable", ["python", "python3", "pytest"])
def test_verification_allowed_tools_adds_no_interpreter_rule(executable: str) -> None:
    tools = verification_allowed_tools(executable)

    assert tools == CLAUDE_DEFAULT_ALLOWED_TOOLS
    assert f"Bash({executable}:*)" not in tools


def test_verification_allowed_tools_adds_other_executables() -> None:
    assert verification_allowed_tools("npm") == (
        *CLAUDE_DEFAULT_ALLOWED_TOOLS,
        "Bash(npm:*)",
    )


@pytest.mark.parametrize("provider", [None, "claude"])
def test_python_verification_command_leaves_roles_on_the_default_list(
    tmp_path: Path, provider: str | None
) -> None:
    command = "python -m pytest tests -q"
    init_reference_workflow(
        tmp_path,
        provider=provider,
        available_providers=(),
        verification_command=command,
    )

    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert "interpreter_grants" not in data
    assert "Bash(python:*)" not in text
    assert "allowed_tools" not in data and all(
        "allowed_tools" not in agent for agent in data["agents"].values()
    )
    assert data["verification_allowed_prefixes"] == [command]
    loaded = load_dispatch_config(
        repo_root=tmp_path, config_path=tmp_path / "dispatch_config.yaml"
    )
    assert loaded.interpreter_grants is False
    for agent in loaded.agents.values():
        assert agent.allowed_tools is None
        if agent.provider == "claude":
            assert agent.claude_allowed_tools == CLAUDE_DEFAULT_ALLOWED_TOOLS


def test_python_verification_roles_follow_a_later_opt_in(tmp_path: Path) -> None:
    init_reference_workflow(
        tmp_path,
        provider="claude",
        available_providers=(),
        verification_command="python -m pytest",
    )
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "interpreter_grants: true\n" + config_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    for agent in loaded.agents.values():
        assert "Bash(python:*)" in agent.claude_allowed_tools


@pytest.mark.parametrize(
    "command",
    ["", "./run-tests.sh", "C:\\tools\\x.exe", "a;b", "'unterminated"],
)
def test_invalid_verification_command_writes_nothing(
    tmp_path: Path, command: str
) -> None:
    with pytest.raises(InvalidVerificationCommandError):
        init_reference_workflow(
            tmp_path, available_providers=(), verification_command=command
        )
    assert list(tmp_path.iterdir()) == []


def test_invalid_verification_command_names_the_token(tmp_path: Path) -> None:
    with pytest.raises(InvalidVerificationCommandError) as error:
        init_reference_workflow(
            tmp_path, available_providers=(), verification_command="./run-tests.sh -q"
        )
    assert "'./run-tests.sh'" in str(error.value)


def test_verification_command_rerun_is_skipped_identical(tmp_path: Path) -> None:
    init_reference_workflow(
        tmp_path, available_providers=(), verification_command="npm test"
    )
    second = init_reference_workflow(
        tmp_path, available_providers=(), verification_command="npm test"
    )

    assert Path("dispatch_config.yaml") in second.skipped
    assert second.copied == second.conflicts == ()


def test_verification_command_change_is_a_conflict_unless_forced(
    tmp_path: Path,
) -> None:
    init_reference_workflow(tmp_path, available_providers=())

    with pytest.raises(InitConflictError) as error:
        init_reference_workflow(
            tmp_path, available_providers=(), verification_command="npm test"
        )
    assert error.value.conflicts == (Path("dispatch_config.yaml"),)

    result = init_reference_workflow(
        tmp_path, available_providers=(), verification_command="npm test", force=True
    )
    assert Path("dispatch_config.yaml") in result.copied
    assert _loaded_allowed_tools(tmp_path)["auditor"][-1] == "Bash(npm:*)"


def test_candidates_are_reported_on_dry_run(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

    result = init_reference_workflow(tmp_path, dry_run=True, available_providers=())

    assert [(c.command, c.source) for c in result.candidates] == [
        ("go build ./...", "go.mod"),
        ("go test ./...", "go.mod"),
    ]
    assert result.verification_command is None
    assert not (tmp_path / "AGENTS.md").exists()


def test_resolve_provider_mapping_prefers_an_explicit_choice() -> None:
    assert resolve_provider_mapping("claude", ("codex", "gemini")) == CLAUDE_MAPPING
    assert resolve_provider_mapping(None, ("claude",)) == CLAUDE_MAPPING
    assert resolve_provider_mapping(None, ("claude", "gemini")) == REFERENCE_MAPPING
    assert resolve_provider_mapping("codex", ("claude",)) == CODEX_MAPPING
    assert resolve_provider_mapping(None, ("codex",)) == CODEX_MAPPING
    assert resolve_provider_mapping(None, ("claude", "codex")) == REFERENCE_MAPPING
    assert resolve_provider_mapping(None, ("codex", "gemini")) == REFERENCE_MAPPING
    assert resolve_provider_mapping(None, ("gemini",)) == REFERENCE_MAPPING
    assert (
        resolve_provider_mapping("antigravity", ("claude", "codex"))
        == ANTIGRAVITY_MAPPING
    )
    assert resolve_provider_mapping(None, ("agy",)) == ANTIGRAVITY_MAPPING
    assert resolve_provider_mapping(None, ("claude", "agy")) == REFERENCE_MAPPING
    assert resolve_provider_mapping(None, ("agy", "gemini")) == REFERENCE_MAPPING
    assert resolve_provider_mapping(None, ()) == REFERENCE_MAPPING


def test_unknown_provider_error_lists_every_mapping() -> None:
    with pytest.raises(UnknownProviderError) as error:
        resolve_provider_mapping("openai", ())
    assert "Available mappings: claude, codex, antigravity." in str(error.value)


def test_resolve_provider_mapping_rejects_the_removed_gemini_mapping() -> None:
    with pytest.raises(UnknownProviderError):
        resolve_provider_mapping("gemini", ("claude", "codex"))
    assert not hasattr(init_workflow_module, "GEMINI_MAPPING")


def test_provider_mapping_and_cli_order() -> None:
    assert init_workflow_module.PROVIDER_MAPPINGS == (
        "claude",
        "codex",
        "antigravity",
    )
    assert init_workflow_module.PROVIDER_CLIS == ("claude", "codex", "agy")


def test_init_antigravity_available_sets_antigravity_detected(tmp_path: Path) -> None:
    result = init_reference_workflow(
        tmp_path, dry_run=True, available_providers=(), antigravity_available=True
    )
    assert result.antigravity_detected is True

    result = init_reference_workflow(
        tmp_path, dry_run=True, available_providers=(), antigravity_available=False
    )
    assert result.antigravity_detected is False


def _fake_which(names: set[str]):
    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        return f"/usr/bin/{name}" if name in names else None

    return fake_which


def test_init_detects_agy_beside_claude_as_the_reference_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        init_workflow_module.shutil, "which", _fake_which({"agy", "claude"})
    )

    result = init_reference_workflow(tmp_path, dry_run=True)

    assert result.antigravity_detected is True
    assert result.detected_providers == ("claude", "agy")
    assert result.provider_mapping == REFERENCE_MAPPING


def test_init_detects_an_agy_only_path_as_the_antigravity_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_workflow_module.shutil, "which", _fake_which({"agy"}))

    result = init_reference_workflow(tmp_path, dry_run=True)

    assert result.antigravity_detected is True
    assert result.detected_providers == ("agy",)
    assert result.provider_mapping == ANTIGRAVITY_MAPPING


def test_init_ignores_gemini_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        init_workflow_module.shutil, "which", _fake_which({"gemini"})
    )

    result = init_reference_workflow(tmp_path, dry_run=True)

    assert result.antigravity_detected is False
    assert result.detected_providers == ()
    assert result.provider_mapping == REFERENCE_MAPPING


def test_init_reports_no_agy_when_it_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_workflow_module.shutil, "which", _fake_which({"claude"}))

    result = init_reference_workflow(tmp_path, dry_run=True)

    assert result.antigravity_detected is False
    assert result.provider_mapping == CLAUDE_MAPPING


def test_init_does_not_touch_the_agy_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    target = tmp_path / "target"

    init_reference_workflow(target, available_providers=(), antigravity_available=True)

    assert not home.exists()


def _reference_linear_init(target: Path, **kwargs: object):
    return init_reference_workflow(
        target,
        available_providers=(),
        antigravity_available=False,
        tracker="linear",
        tracker_team="ENG",
        tracker_project="Platform",
        **kwargs,
    )


def test_reference_linear_init_puts_planner_and_frontend_on_claude(
    tmp_path: Path,
) -> None:
    result = _reference_linear_init(tmp_path)

    assert result.provider_mapping == REFERENCE_MAPPING
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "provider: antigravity" not in text
    assert (
        "  # Linear mode does not support antigravity roles, so planner and "
        "frontend run on Claude.\n  planner:\n"
    ) in text
    config = load_dispatch_config(repo_root=tmp_path)
    assert config.tracker.provider == "linear"
    for role in ("planner", "frontend"):
        agent = config.agents[role]
        assert agent.provider == "claude"
        assert agent.binary == "claude"
        assert agent.resume is False
        assert agent.timeout_ms == 1200000
    # The other roles keep the template mapping.
    template = load_dispatch_config(
        repo_root=TEMPLATE_ROOT,
        config_path=TEMPLATE_ROOT / "dispatch_config.yaml",
    )
    for role in ("backend", "auditor", "validator", "finalizer"):
        assert config.agents[role].provider == template.agents[role].provider
        assert config.agents[role].binary == template.agents[role].binary
        assert config.agents[role].resume == template.agents[role].resume


def test_reference_linear_init_dry_run_writes_nothing(tmp_path: Path) -> None:
    _reference_linear_init(tmp_path, dry_run=True)

    assert not any(tmp_path.iterdir())


def test_antigravity_linear_init_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TrackerUnsupportedMappingError):
        _reference_linear_init(tmp_path, provider="antigravity")

    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_render_linear_reference_agents_keeps_other_lines_and_line_endings(
    newline: str,
) -> None:
    template = (TEMPLATE_ROOT / "dispatch_config.yaml").read_text(encoding="utf-8")
    source = template.replace("\n", newline)

    rendered = init_workflow_module.render_linear_reference_agents(source)

    assert rendered.count(newline) == rendered.count("\n")
    assert "provider: antigravity" not in rendered
    assert "binary: agy" not in rendered
    loaded = yaml.safe_load(rendered)
    for role in ("planner", "frontend"):
        assert loaded["agents"][role] == {
            "provider": "claude",
            "binary": "claude",
            "resume": False,
            "timeout_ms": 1200000,
        }
    original = yaml.safe_load(source)
    for role in ("backend", "auditor", "validator", "finalizer"):
        assert loaded["agents"][role] == original["agents"][role]
    assert {k: v for k, v in loaded.items() if k != "agents"} == {
        k: v for k, v in original.items() if k != "agents"
    }


def test_render_linear_reference_agents_rejects_a_missing_stanza() -> None:
    template = (TEMPLATE_ROOT / "dispatch_config.yaml").read_text(encoding="utf-8")
    broken = template.replace("  frontend:\n", "  ui:\n")

    with pytest.raises(InitWorkflowError, match="agents.frontend"):
        init_workflow_module.render_linear_reference_agents(broken)
