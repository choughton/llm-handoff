from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff.init_workflow import (
    InitConflictError,
    UnknownTemplateError,
    init_reference_workflow,
    template_files,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "examples" / "reference-workflow"


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
    target_file = tmp_path / "AGENTS.md"
    target_file.write_text("custom repo instructions\n", encoding="utf-8")

    with pytest.raises(InitConflictError) as exc_info:
        init_reference_workflow(tmp_path)

    assert Path("AGENTS.md") in exc_info.value.conflicts
    assert target_file.read_text(encoding="utf-8") == "custom repo instructions\n"


def test_init_reference_workflow_force_overwrites_conflicts(tmp_path: Path) -> None:
    target_file = tmp_path / "AGENTS.md"
    target_file.write_text("custom repo instructions\n", encoding="utf-8")

    result = init_reference_workflow(tmp_path, force=True)

    assert Path("AGENTS.md") in result.copied
    assert result.conflicts == ()
    assert target_file.read_text(encoding="utf-8") == (
        TEMPLATE_ROOT / "AGENTS.md"
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
