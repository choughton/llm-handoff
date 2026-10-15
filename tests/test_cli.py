from __future__ import annotations

from pathlib import Path

from llm_handoff import __main__ as main_module

LEGACY_PROJECT_NAME = bytes.fromhex("43726f737366697265").decode("ascii")
LEGACY_TOOL_NAME = bytes.fromhex("616e746967726176697479").decode("ascii")


def test_init_dry_run_reports_merges_without_writes(tmp_path: Path, capsys) -> None:
    names = ("AGENTS.md", ".gitignore", ".geminiignore", ".claude/settings.json")
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}" if name.endswith(".json") else b"custom\r\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert main_module.main(["init", str(tmp_path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Merge: 4 file(s)" in output
    assert "Copy:" in output
    for name in names:
        assert name in output.replace("\\", "/")
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_help_text_names_public_dispatcher(capsys) -> None:
    exit_code = main_module.main(["--help"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "File-based handoff dispatcher" in output
    assert "--manual-frontend" in output
    assert "--config" in output
    assert LEGACY_PROJECT_NAME not in output
    assert LEGACY_TOOL_NAME not in output.lower()


def test_help_text_lists_doctor_subcommand(capsys) -> None:
    assert main_module.main(["--help"]) == 0
    assert "doctor" in capsys.readouterr().out

    assert main_module.main(["doctor", "--help"]) == 0
    output = capsys.readouterr().out
    assert "--repo-root" in output
    assert "--config" in output


def test_init_command_dry_run_previews_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main_module.main(["init", str(tmp_path), "--dry-run"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "reference-workflow" in output
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_command_copies_reference_workflow(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main_module.main(["init", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Initialized" in output
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "docs" / "handoff" / "HANDOFF.md").is_file()


def test_init_command_reports_conflicts(
    tmp_path: Path,
    capsys,
) -> None:
    (tmp_path / "PROJECT_STATE.md").write_text("custom\n", encoding="utf-8")

    exit_code = main_module.main(["init", str(tmp_path)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Initialization aborted" in captured.err
    assert "PROJECT_STATE.md" in captured.err


def test_init_command_provider_claude_writes_all_claude_mapping(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main_module.main(["init", str(tmp_path), "--provider", "claude"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Provider mapping: all roles on claude" in output
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "provider: gemini" not in text
    assert "provider: codex" not in text


def test_init_command_rejects_unknown_provider(tmp_path: Path, capsys) -> None:
    exit_code = main_module.main(["init", str(tmp_path), "--provider", "openai"])

    assert exit_code == 2
    assert "Unknown provider mapping" in capsys.readouterr().err
    assert not (tmp_path / "AGENTS.md").exists()
