from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llm_handoff import __main__ as main_module
from llm_handoff import antigravity_permissions as agy_perms
from llm_handoff import init_workflow

LEGACY_PROJECT_NAME = bytes.fromhex("43726f737366697265").decode("ascii")


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


def test_help_text_lists_doctor_subcommand(capsys) -> None:
    assert main_module.main(["--help"]) == 0
    assert "doctor" in capsys.readouterr().out

    assert main_module.main(["doctor", "--help"]) == 0
    output = capsys.readouterr().out
    assert "--repo-root" in output
    assert "--config" in output
    assert "--probe-tracker" in output


def test_doctor_probe_tracker_option_reaches_run_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from llm_handoff import doctor

    calls: list[dict[str, object]] = []

    def fake_run_doctor(**kwargs: object) -> doctor.DoctorReport:
        calls.append(kwargs)
        return doctor.DoctorReport(repo_root=tmp_path, checks=())

    monkeypatch.setattr(main_module, "run_doctor", fake_run_doctor)

    exit_code = main_module.main(
        ["doctor", "--probe-tracker", "ENG-1", "--repo-root", str(tmp_path)]
    )

    assert exit_code == 0
    assert calls == [
        {
            "repo_root": tmp_path,
            "config_path": None,
            "probe_tracker_issue": "ENG-1",
        }
    ]
    assert main_module.main(["doctor", "--repo-root", str(tmp_path)]) == 0
    assert calls[-1]["probe_tracker_issue"] is None
    capsys.readouterr()


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


def test_init_command_provider_codex_writes_all_codex_mapping(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main_module.main(["init", str(tmp_path), "--provider", "codex"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Provider mapping: all roles on codex" in output
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "provider: gemini" not in text
    assert "provider: claude" not in text
    assert "skill_name: llm-handoff-planner" in text


def test_init_command_provider_gemini_writes_all_gemini_mapping(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main_module.main(["init", str(tmp_path), "--provider", "gemini"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Provider mapping: all roles on gemini" in output
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "provider: claude" not in text
    assert "provider: codex" not in text
    assert 'mention: "@auditor"' in text


def test_init_command_provider_antigravity_writes_all_agy_mapping(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main_module.main(
        ["init", str(tmp_path), "--provider", "antigravity"]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Provider mapping: all roles on antigravity" in output
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    assert "provider: claude" not in text
    assert "provider: codex" not in text
    assert "provider: gemini" not in text
    assert text.count("binary: agy") == 6


def test_init_command_rejects_antigravity_with_linear(tmp_path: Path, capsys) -> None:
    for extra in ((), ("--dry-run",)):
        exit_code = main_module.main(
            [
                "init",
                str(tmp_path),
                "--provider",
                "antigravity",
                "--tracker",
                "linear",
                "--tracker-team",
                "ENG",
                "--tracker-project",
                "P",
                *extra,
            ]
        )

        assert exit_code == 2
        err = capsys.readouterr().err
        assert len(err.strip().splitlines()) == 1
        assert "Linear mode does not support provider: antigravity" in err
        assert "--provider claude" in err
        assert "--provider codex" in err
        assert "--tracker none" in err
        assert not any(tmp_path.iterdir())


def test_init_help_names_every_single_provider_mapping(capsys) -> None:
    assert main_module.main(["init", "--help"]) == 0

    # Drop the help panel borders so wrapped help text reads as one line.
    output = " ".join(capsys.readouterr().out.replace("\u2502", " ").split())
    assert "claude, codex, antigravity, or gemini" in output
    assert "codex when Codex is the only provider CLI on PATH" in output
    assert "antigravity when agy is the only provider CLI on PATH" in output
    assert "Gemini CLI is not detected" in output
    assert "gemini when Gemini CLI is the only provider CLI on PATH" not in output


def _node_fixture(root: Path) -> None:
    (root / "package.json").write_text(
        '{"scripts": {"test": "jest", "build": "tsc"}}', encoding="utf-8"
    )


def test_init_dry_run_lists_candidate_commands(tmp_path: Path, capsys) -> None:
    _node_fixture(tmp_path)

    assert main_module.main(["init", str(tmp_path), "--dry-run"]) == 0

    output = capsys.readouterr().out
    lines = output.splitlines()
    assert "Candidate commands: 2" in lines
    assert "  - npm run test  (from package.json)" in lines
    assert "  - npm run build  (from package.json)" in lines
    no_command = (
        "No verification command selected; rerun with "
        '--verification-command "<cmd>" to allow its executable.'
    )
    assert no_command in lines
    assert lines.index(no_command) > lines.index("Candidate commands: 2")
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_escapes_control_and_surrogate_characters_in_candidates(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"a\\u001b]0;spoof\\u0007b": "x", '
        '"c\\u202ed": "x", "test\\ud800": "x", "e\\u2028f": "x"}}',
        encoding="utf-8",
    )

    assert main_module.main(["init", str(tmp_path), "--dry-run"]) == 0

    output = capsys.readouterr().out
    output.encode("utf-8")  # strict: a lone surrogate would raise here
    assert "\x1b" not in output
    assert "\x07" not in output
    assert "\u202e" not in output
    lines = output.splitlines()
    assert "  - npm run a\\x1b]0;spoof\\x07b  (from package.json)" in lines
    assert "  - npm run c\\u202ed  (from package.json)" in lines
    assert "  - npm run test\\ud800  (from package.json)" in lines
    assert "  - npm run e\\u2028f  (from package.json)" in lines


def test_init_escapes_control_characters_in_verification_command(
    tmp_path: Path, capsys
) -> None:
    exit_code = main_module.main(
        ["init", str(tmp_path), "--verification-command", "npm test\x1b[2J"]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "Verification command: npm test\\x1b[2J; allowed_tools adds" in output


def test_init_reports_when_no_candidates_are_found(tmp_path: Path, capsys) -> None:
    assert main_module.main(["init", str(tmp_path), "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "Candidate commands: none found" in output
    assert "No verification command selected" not in output


def test_init_verification_command_adds_the_executable(
    tmp_path: Path, capsys
) -> None:
    _node_fixture(tmp_path)

    exit_code = main_module.main(
        ["init", str(tmp_path), "--verification-command", "npm test"]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert (
        "Verification command: npm test; allowed_tools adds Bash(npm:*) "
        "for claude roles"
    ) in output
    assert "No verification command selected" not in output
    assert "Bash(npm:*)" in (tmp_path / "dispatch_config.yaml").read_text(
        encoding="utf-8"
    )


def test_init_rejects_an_invalid_verification_command(
    tmp_path: Path, capsys
) -> None:
    exit_code = main_module.main(
        ["init", str(tmp_path), "--verification-command", "./x.sh"]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "./x.sh" in captured.err
    assert "Invalid --verification-command" in captured.err
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_command_rejects_unknown_provider(tmp_path: Path, capsys) -> None:
    exit_code = main_module.main(["init", str(tmp_path), "--provider", "openai"])

    assert exit_code == 2
    assert "Unknown provider mapping" in capsys.readouterr().err
    assert not (tmp_path / "AGENTS.md").exists()


def _stub_provider_which(monkeypatch: pytest.MonkeyPatch, names: set[str]) -> None:
    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        return f"/usr/bin/{name}" if name in names else None

    monkeypatch.setattr(init_workflow.shutil, "which", fake_which)


AGY_HEADER = "Antigravity (agy) permissions for ~/.gemini/antigravity-cli/settings.json:"
AGY_FOOTER = (
    "Add these entries to that settings file yourself, then run "
    "`llm-handoff doctor` to check them."
)


def test_init_dry_run_prints_agy_permissions_when_agy_is_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _stub_provider_which(monkeypatch, {"agy", "claude"})
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    target = tmp_path / "target"

    assert main_module.main(["init", str(target), "--dry-run"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert (
        "Provider mapping: reference (mixed providers); provider CLIs found: "
        "claude, agy"
    ) in lines
    start = lines.index(AGY_HEADER)
    assert lines[-1] == AGY_FOOTER
    block = lines[start:]
    expected_entries = [
        f"    - {entry}"
        for entry in (
            *agy_perms.ANTIGRAVITY_REQUIRED_ALLOW,
            *agy_perms.ANTIGRAVITY_REQUIRED_DENY,
        )
    ]
    for entry in expected_entries:
        assert entry in block
    assert "    - command(regex:^npm test( .*)?$)" not in block
    assert not home.exists()
    assert not target.exists()


def test_init_prints_the_verification_command_agy_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _stub_provider_which(monkeypatch, {"agy"})

    exit_code = main_module.main(
        ["init", str(tmp_path), "--verification-command", "npm test"]
    )

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    start = lines.index(AGY_HEADER)
    deny_index = lines.index("  permissions.deny:")
    assert lines[deny_index - 1] == "    - command(regex:^npm test( .*)?$)"
    assert start < deny_index
    assert lines[-1] == AGY_FOOTER


INTERPRETER_INIT_LINE = (
    "Verification command: python -m pytest tests -q; no role gets a python "
    "grant. The dispatcher gate runs this command itself. Roles can run it "
    "only after you set interpreter_grants: true in dispatch_config.yaml, "
    "which lets an agent run any command, git push included."
)


@pytest.mark.parametrize("executable", ["python", "python3", "pytest"])
def test_init_interpreter_verification_command_prints_the_opt_in_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, executable: str
) -> None:
    _stub_provider_which(monkeypatch, {"agy", "claude", "codex"})
    command = f"{executable} -m pytest tests -q"

    exit_code = main_module.main(
        ["init", str(tmp_path), "--verification-command", command]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    lines = output.splitlines()
    expected = INTERPRETER_INIT_LINE.replace(
        "python -m pytest tests -q", command
    ).replace("a python grant", f"a {executable} grant")
    assert lines.count(expected) == 1
    expected.encode("ascii")
    assert f"allowed_tools adds Bash({executable}:*)" not in output
    # The printed agy grants are the flag-off set.
    start = lines.index(AGY_HEADER)
    deny_index = lines.index("  permissions.deny:")
    assert lines[start + 2 : deny_index] == [
        f"    - {entry}" for entry in agy_perms.ANTIGRAVITY_REQUIRED_ALLOW
    ]
    assert "python( .*)?$)" not in output
    assert "pytest( .*)?$)" not in output
    text = (tmp_path / "dispatch_config.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert "interpreter_grants" not in data
    assert not any(
        line.startswith("interpreter_grants") for line in text.splitlines()
    )
    assert f"Bash({executable}:*)" not in text
    assert data["verification_allowed_prefixes"] == [command]


def test_init_omits_agy_permissions_when_agy_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _stub_provider_which(monkeypatch, {"claude"})

    assert main_module.main(["init", str(tmp_path), "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "Antigravity" not in output
    assert "permissions.allow" not in output
    assert "llm-handoff doctor" not in output
    assert output.splitlines()[-1] == "Candidate commands: none found"
