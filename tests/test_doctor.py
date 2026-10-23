from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from llm_handoff import __main__ as main_module
from llm_handoff import antigravity_permissions as agy_perms
from llm_handoff import config as config_module
from llm_handoff import doctor
from llm_handoff import supervisor
from llm_handoff.agent_types import _ProcessResult

ROLES = ("planner", "backend", "frontend", "auditor", "validator", "finalizer")
REAL_WHICH = shutil.which
NORMALIZER_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)

VALID_HANDOFF = """---
next_agent: backend
reason: "Planner assigns the first story."
epic_id: "E1"
story_id: "S1"
story_title: "First story"
producer: planner
---

# Handoff: S1 First story

## Work Packet

- **Objective:** Add a small feature.
- **Files in scope:** `src/app.py`
- **Files out of bounds:** `docs/**`
- **Context:** None.
- **Verification command:** `python -m pytest tests -q`
- **Expected next route:** auditor

## Next Step

- **backend:** Implement the Work Packet.
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_repo(
    tmp_path: Path,
    providers: dict[str, str],
    *,
    handoff: str | None = VALID_HANDOFF,
    commit: bool = True,
    normalizer_provider: str | None = None,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    lines = ["agents:"]
    for role in ROLES:
        provider = providers[role]
        lines.append(f"  {role}:")
        lines.append(f"    provider: {provider}")
        lines.append(f"    binary: fake-{provider}")
    if normalizer_provider is not None:
        lines.append("normalizer:")
        lines.append(f"  provider: {normalizer_provider}")
    (repo / "dispatch_config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if handoff is not None:
        handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
        handoff_path.parent.mkdir(parents=True)
        handoff_path.write_text(handoff, encoding="utf-8")
    if commit:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _stub_which(monkeypatch: pytest.MonkeyPatch, available: set[str]) -> None:
    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        if name == "git" or name == doctor._command_binary_name("git"):
            return REAL_WHICH(name, *args, **kwargs)
        if name in available:
            return f"/usr/bin/{name}"
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    def fake_find_scanner(name: str, *args: object, **kwargs: object) -> str | None:
        return f"/usr/bin/{name}" if name in available else None

    # Scanner lookups use their own helper, not shutil.which.
    monkeypatch.setattr(doctor, "find_scanner_executable", fake_find_scanner)
    # The default claude normalizer is runnable with an Anthropic key; tests
    # of the normalizer check clear it and choose their own provider inputs.
    for name in NORMALIZER_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "doctor-test-key")


def _run(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    exit_code = main_module.main(["doctor", "--repo-root", str(repo)])
    return exit_code, capsys.readouterr().out


MIXED = {
    "planner": "codex",
    "backend": "codex",
    "frontend": "claude",
    "auditor": "claude",
    "validator": "claude",
    "finalizer": "claude",
}
NO_GEMINI = {role: ("codex" if role == "backend" else "claude") for role in ROLES}


def test_doctor_happy_path_passes_every_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, MIXED)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = output.strip().splitlines()
    check_lines = lines[:-1]
    assert check_lines
    assert not any(line.startswith("FAIL") for line in check_lines)
    for prefix in (
        "PASS config:",
        "PASS git:",
        "PASS git repository:",
        "PASS git working tree:",
        "PASS normalizer:",
        "PASS handoff route: routes to backend",
        "PASS handoff validation:",
    ):
        assert any(line.startswith(prefix) for line in check_lines), prefix
    for role in ROLES:
        assert any(line.startswith(f"PASS role {role}:") for line in check_lines), role
    assert lines[-1].startswith("Doctor PASS: 0 error(s)")


def test_doctor_reports_missing_role_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI)
    _stub_which(monkeypatch, {"fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert (
        "FAIL role backend: provider codex binary `fake-codex` was not found on PATH"
        in output
    )
    assert "PASS role auditor:" in output


def test_doctor_resolves_cmd_binary_through_bare_stem_like_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "binary: fake-claude", "binary: fake-claude.cmd"
        ),
        encoding="utf-8",
    )
    # Only the bare name is on PATH, as with claude.exe on Windows.
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "PASS role auditor: provider claude binary `fake-claude.cmd`" in output


def test_doctor_resolves_relative_path_binary_against_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    tool = repo / "tools" / "fake-codex"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "binary: fake-codex", "binary: ./tools/fake-codex"
        ),
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-claude"})
    # Invoke from outside the checkout; provider processes run in the repo.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "PASS role backend: provider codex binary `./tools/fake-codex`" in output


def test_doctor_reports_missing_relative_path_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "binary: fake-codex", "binary: ./tools/fake-codex"
        ),
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-claude"})
    # A same-named file relative to the caller's cwd must not count.
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "fake-codex").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert "FAIL role backend: provider codex binary `./tools/fake-codex`" in output


def test_doctor_reports_when_git_is_missing_and_handoff_has_scope_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    handoff = VALID_HANDOFF.replace(
        "producer: planner\n",
        "producer: planner\nscope_sha: 0123456789abcdef0123456789abcdef01234567\n",
    )
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=handoff)
    available = {"fake-codex", "fake-claude"}
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: f"/usr/bin/{name}" if name in available else None,
    )

    def no_git(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    lines = output.strip().splitlines()
    assert "FAIL git: git is not on PATH" in lines
    assert any(line.startswith("PASS handoff route:") for line in lines)
    assert (
        "WARN handoff validation: git is not on PATH; skipped the scope_sha "
        "commit lookup" in lines
    )
    assert not any("could not run" in line for line in lines)
    assert lines[-1].startswith("Doctor FAIL:")


def test_doctor_has_no_container_runtime_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # The container runtime check served only the removed gemini provider.
    repo = _make_repo(tmp_path, NO_GEMINI)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "container runtime" not in output


def test_doctor_fails_config_for_a_removed_gemini_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, {**NO_GEMINI, "planner": "gemini"})
    _stub_which(monkeypatch, {"fake-gemini", "fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert "FAIL config: dispatch config could not be loaded" in output
    assert config_module.GEMINI_PROVIDER_REMOVED_MESSAGE in output


def test_doctor_reports_malformed_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff="---\n---\n\n# Handoff\n")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert "FAIL handoff validation:" in output
    assert "WARN handoff route:" in output


def test_doctor_warns_when_handoff_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "WARN handoff: no handoff file at" in output


def test_doctor_reports_security_reviewer_on_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert (
        "PASS security review: the security reviewer is on (security provider: auto)"
        in output
    )
    assert "WARN security review:" not in output


def test_doctor_warns_with_the_security_opt_out_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "audit:\n"
        + "  reviewers: [quality]\n"
        + '  security_opt_out_reason: "Local prototype with no user data."\n',
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    warn_lines = [
        line for line in output.splitlines() if line.startswith("WARN security review:")
    ]
    assert len(warn_lines) == 1
    assert "security reviewer is off" in warn_lines[0]
    assert "Local prototype with no user data." in warn_lines[0]


def test_doctor_fails_on_security_opt_out_without_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "audit:\n  reviewers: [quality]\n",
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert "FAIL config: dispatch config could not be loaded" in output
    assert "security_opt_out_reason" in output


def test_doctor_reports_work_claim_gate_on_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert (
        "PASS work-claim gate: the work-claim gate is on (enforce_work_packet: true)"
        in output
    )
    assert "WARN work-claim gate:" not in output


def test_doctor_warns_with_the_work_claim_gate_opt_out_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "enforce_work_packet: false\n"
        + 'work_packet_opt_out_reason: "Planner prompts do not emit packets yet."\n',
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    warn_lines = [
        line
        for line in output.splitlines()
        if line.startswith("WARN work-claim gate:")
    ]
    assert len(warn_lines) == 1
    assert "work-claim gate is off (enforce_work_packet: false)" in warn_lines[0]
    assert "Planner prompts do not emit packets yet." in warn_lines[0]
    assert "PASS work-claim gate:" not in output


def test_doctor_fails_on_work_claim_gate_opt_out_without_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "enforce_work_packet: false\n",
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert "FAIL config: dispatch config could not be loaded" in output
    assert "work_packet_opt_out_reason" in output


def test_doctor_warns_when_the_verification_allowlist_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    warn_lines = [
        line
        for line in output.splitlines()
        if line.startswith("WARN verification allowlist:")
    ]
    assert len(warn_lines) == 1
    assert "verification_allowed_prefixes is empty" in warn_lines[0]
    assert "PASS verification allowlist:" not in output


def test_doctor_passes_when_the_verification_allowlist_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + 'verification_allowed_prefixes: ["python -m pytest", "npm test"]\n',
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    pass_lines = [
        line
        for line in output.splitlines()
        if line.startswith("PASS verification allowlist:")
    ]
    assert len(pass_lines) == 1
    assert "python -m pytest, npm test" in pass_lines[0]
    assert "WARN verification allowlist:" not in output


def test_doctor_omits_the_verification_allowlist_when_the_gate_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "enforce_work_packet: false\n"
        + 'work_packet_opt_out_reason: "Planner prompts do not emit packets yet."\n',
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "verification allowlist" not in output


def _add_config(repo: Path, text: str) -> None:
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + text, encoding="utf-8"
    )


def _scanner_lines(output: str) -> list[str]:
    return [
        line
        for line in output.splitlines()
        if " scanners:" in line or " scanner " in line
    ]


SCANNER_CONFIG = """scanners:
  - name: gitleaks
    command: gitleaks git --redact --log-opts="{diff_range}" {repo_root}
  - name: bandit
    command: bandit -q {changed_py_files}
  - name: pip-audit
    command: pip-audit --strict
    required: false
  - name: envscan
    command: FOO=1 envscan {changed_files}
"""


def test_doctor_passes_scanners_when_none_are_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _scanner_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith("PASS scanners: no scanners are configured")
    assert "work-claim gate runs none" in lines[0]
    assert "Scanners section of CONFIGURATION.md" in lines[0]


def test_doctor_warns_when_scanners_are_configured_but_the_gate_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(
        repo,
        "enforce_work_packet: false\n"
        'work_packet_opt_out_reason: "Planner prompts do not emit packets yet."\n'
        + SCANNER_CONFIG,
    )
    # None of the scanner executables are on PATH; the per-scanner checks
    # must be skipped, so no FAIL is reported for them.
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _scanner_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith("WARN scanners:")
    assert "gitleaks, bandit, pip-audit, envscan" in lines[0]
    assert "none of them run while the work-claim gate is off" in lines[0]


def test_doctor_reports_each_scanner_in_config_order_with_the_gate_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(repo, SCANNER_CONFIG)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude", "gitleaks"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    lines = _scanner_lines(output)
    assert [line.split(":", 1)[0] for line in lines] == [
        "PASS scanner gitleaks",
        "FAIL scanner bandit",
        "WARN scanner pip-audit",
        "WARN scanner envscan",
    ]
    assert "executable `gitleaks` is on PATH" in lines[0]
    assert "will run scanner gitleaks" in lines[0]
    assert "skip it on other claims" not in lines[0]
    assert "executable `bandit` was not found on PATH" in lines[1]
    # bandit uses {changed_py_files}; the gate skips it, before the PATH
    # lookup, on claims without changed Python files.
    assert (
        "every claim where {changed_py_files} is nonempty will fail with "
        "scanner_missing:bandit"
    ) in lines[1]
    assert "required: false" in lines[1]
    assert "executable `pip-audit` was not found on PATH" in lines[2]
    assert "will skip scanner pip-audit" in lines[2]
    assert "doctor could not check the executable" in lines[3]
    assert "does not start with a plain program name" in lines[3]


def test_doctor_names_the_leading_placeholder_of_a_scanner_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(
        repo,
        "scanners:\n"
        "  - name: wrapped\n"
        "    command: \"{changed_files} --check\"\n",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _scanner_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith("WARN scanner wrapped:")
    assert "starts with a placeholder" in lines[0]
    assert "(`{changed_files}`)" in lines[0]
    assert "does not start with a plain program name" not in lines[0]


def test_doctor_passes_every_scanner_whose_executable_is_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(
        repo,
        "scanners:\n"
        "  - name: bandit\n"
        "    command: bandit -q {changed_py_files}\n"
        "  - name: pip-audit\n"
        "    command: '\"pip-audit\" --strict'\n"
        "    required: false\n",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude", "bandit", "pip-audit"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _scanner_lines(output)
    assert len(lines) == 2
    assert lines[0].startswith("PASS scanner bandit:")
    assert (
        "will run scanner bandit on claims where {changed_py_files} is "
        "nonempty and skip it on other claims"
    ) in lines[0]
    assert lines[1].startswith("PASS scanner pip-audit:")
    assert "executable `pip-audit`" in lines[1]
    assert "will run scanner pip-audit" in lines[1]
    assert "skip it on other claims" not in lines[1]


def test_doctor_names_changed_js_files_in_scanner_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(
        repo,
        "scanners:\n"
        "  - name: eslint\n"
        "    command: npx --yes=false -- eslint --max-warnings 0 {changed_js_files}\n",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude", "npx"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _scanner_lines(output)[0]
    assert line.startswith("PASS scanner eslint:")
    assert "executable `npx`" in line
    assert (
        "will run scanner eslint on claims where {changed_js_files} is "
        "nonempty and skip it on other claims"
    ) in line


def test_doctor_scanner_with_both_list_placeholders_names_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(
        repo,
        "scanners:\n"
        "  - name: lint\n"
        "    command: lint {changed_files} {changed_py_files}\n",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    line = _scanner_lines(output)[0]
    assert line.startswith("FAIL scanner lint:")
    assert (
        "every claim where {changed_files} and {changed_py_files} are nonempty "
        "will fail with scanner_missing:lint"
    ) in line


def test_doctor_scanner_lookup_has_no_cmd_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # Role binaries also resolve `<name>.cmd`; the gate does not, so neither
    # does the doctor's scanner check.
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(repo, "scanners:\n  - name: bandit\n    command: bandit -q\n")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude", "bandit.cmd"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    assert _scanner_lines(output)[0].startswith("FAIL scanner bandit:")


def _plant_fakescan(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory.mkdir(parents=True)
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        planted = directory / "fakescan.bat"
        planted.write_text("@exit /b 0\r\n", encoding="utf-8", newline="")
    else:
        planted = directory / "fakescan"
        planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        planted.chmod(0o755)
    return planted


def _use_real_scanner_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_handoff import work_claim

    monkeypatch.setattr(
        doctor, "find_scanner_executable", work_claim.find_scanner_executable
    )


def test_doctor_fails_scanner_that_is_only_in_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(repo, "scanners:\n  - name: fakescan\n    command: fakescan -q\n")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    _use_real_scanner_lookup(monkeypatch)
    planted = _plant_fakescan(tmp_path / "cwd", monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(planted.parent)
    monkeypatch.setenv("PATH", str(other) + os.pathsep + os.environ.get("PATH", ""))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    line = _scanner_lines(output)[0]
    assert line.startswith("FAIL scanner fakescan:")
    assert "was not found on PATH" in line


def test_doctor_passes_scanner_on_an_absolute_path_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _add_config(repo, "scanners:\n  - name: fakescan\n    command: fakescan -q\n")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    _use_real_scanner_lookup(monkeypatch)
    planted = _plant_fakescan(tmp_path / "cwd", monkeypatch)
    monkeypatch.chdir(planted.parent)
    monkeypatch.setenv(
        "PATH", str(planted.parent) + os.pathsep + os.environ.get("PATH", "")
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _scanner_lines(output)[0].startswith("PASS scanner fakescan:")


def test_doctor_does_not_run_scanner_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    marker = tmp_path / "scanner-ran.txt"
    _add_config(
        repo,
        "scanners:\n"
        "  - name: marker\n"
        f"    command: python -c \"open(r'{marker}', 'w').close()\"\n"
        "  - name: shellmarker\n"
        f"    command: X=1 python -c \"open(r'{marker}', 'w').close()\"\n",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude", "python"})
    real_run = subprocess.run
    calls: list[object] = []

    def recording_run(args: object, *rest: object, **kwargs: object) -> object:
        calls.append(args)
        return real_run(args, *rest, **kwargs)

    def refuse_popen(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"doctor started a process: {args!r}")

    monkeypatch.setattr(subprocess, "run", recording_run)
    monkeypatch.setattr("llm_handoff.work_claim.run_shell_command", refuse_popen)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _scanner_lines(output)[0].startswith("PASS scanner marker:")
    assert not marker.exists()
    for args in calls:
        assert isinstance(args, list) and args[0] == "git", args


# A Work Packet section the gate cannot parse: it has no Files in scope or
# Verification command.
HANDOFF_WITHOUT_WORK_PACKET = """---
next_agent: backend
reason: "Planner assigns the first story."
epic_id: "E1"
story_id: "S1"
story_title: "First story"
producer: planner
---

# Handoff: S1 First story

## Work Packet

- **Objective:** Add a small feature.
- **Expected next route:** auditor

## Next Step

- **backend:** Implement the Work Packet.
"""


def test_doctor_fails_handoff_without_work_packet_when_gate_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=HANDOFF_WITHOUT_WORK_PACKET)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    fail_lines = [
        line
        for line in output.splitlines()
        if line.startswith("FAIL handoff validation:")
    ]
    assert any(
        "while enforce_work_packet is enabled" in line for line in fail_lines
    ), output
    assert "PASS handoff validation:" not in output


def test_doctor_accepts_handoff_without_work_packet_when_gate_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(
        tmp_path, NO_GEMINI, handoff=HANDOFF_WITHOUT_WORK_PACKET, commit=False
    )
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "enforce_work_packet: false\n"
        + 'work_packet_opt_out_reason: "Planner prompts do not emit packets yet."\n',
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "FAIL handoff validation:" not in output
    assert "enforce_work_packet is enabled" not in output
    assert "PASS handoff validation:" in output



def _commit_handoff(repo: Path, *, producer: str) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text(
        f"""---
next_agent: planner
reason: "Epic closed; planner should select the next assignment."
status: verified_pass
scope_sha: {head}
producer: {producer}
---

# Epic close

The epic was recorded in PROJECT_STATE.md at {head}.

## Next Step

- **planner:** Select the next bounded assignment.
""",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "epic close")


def test_doctor_accepts_finalizer_epic_close_without_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # The dispatch loop routes from this handoff without validating it, so
    # doctor must not report it as a FAIL.
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _commit_handoff(repo, producer="finalizer")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "EVIDENCE_MISSING" not in output
    assert "PASS handoff validation:" in output


def test_doctor_still_fails_auditor_verified_pass_without_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _commit_handoff(repo, producer="auditor")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    assert "FAIL handoff validation: EVIDENCE_MISSING" in output


# TRANS-3 / PR #251 Finding 1: dispatcher-written validator verdicts.


def _validator_verdict_handoff(head: str, builder: str) -> str:
    """A `producer: validator` verdict from the dispatcher's own publish builders."""

    from llm_handoff.pr_review import ReviewFinding
    from llm_handoff.publish import (
        StoryState,
        build_deferred_findings_handoff,
        build_review_bounce_handoff,
    )

    state = StoryState(
        branch="llm-handoff/s1-first-story",
        base_branch="main",
        base_sha=head,
        story_id="S1",
        story_title="First story",
        epic_id="E1",
        # The assignment's Work Packet, as publish saves it for the story.
        work_packet="## Work Packet\n\n"
        + VALID_HANDOFF.split("## Work Packet\n\n", 1)[1].split("\n## ", 1)[0],
    )
    url = "https://example.invalid/pull/1"
    if builder == "bounce":
        return build_review_bounce_handoff(
            state=state,
            findings=[
                ReviewFinding(
                    comment_id=1, path="src/app.py", line=1, body="Edge case.", url=""
                )
            ],
            url=url,
            head_sha=head,
            review_round=1,
            max_rounds=2,
        )
    return build_deferred_findings_handoff(
        state=state,
        findings=[
            {"number": 1, "location": "src/app.py:1", "body": "Edge case."}
        ],
        url=url,
        head_sha=head,
    )


_VERDICT_BUILDERS = pytest.mark.parametrize("builder", ["bounce", "deferred"])


def _verdict_repo(tmp_path: Path, builder: str) -> tuple[Path, str]:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, _validator_verdict_handoff(head, builder)


def _write_as_dispatcher(repo: Path, content: str) -> None:
    """Write and record the handoff through the dispatcher's own writer."""

    from llm_handoff import orchestrator

    config = config_module.load_dispatch_config(repo_root=repo)
    config.handoff_full_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator._write_dispatcher_handoff(config, content)


def _handoff_validation_checks(repo: Path) -> list[doctor.DoctorCheck]:
    report = doctor.run_doctor(repo_root=repo)
    return [check for check in report.checks if check.name == "handoff validation"]


_VALIDATOR_VERDICT_ERROR = (
    "frontmatter_status_producer_rule: producer `validator` cannot emit status "
    "`verified_"
)


def _only_the_producer_rule_fails(checks: list[doctor.DoctorCheck]) -> bool:
    """The verdict's evidence and SHA pass; only the untrusted verdict fails."""

    failures = [check.message for check in checks if check.status == "FAIL"]
    return len(failures) == 1 and failures[0].startswith(_VALIDATOR_VERDICT_ERROR)


@_VERDICT_BUILDERS
def test_doctor_passes_a_recorded_dispatcher_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: str
) -> None:
    repo, content = _verdict_repo(tmp_path, builder)
    _write_as_dispatcher(repo, content)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    checks = _handoff_validation_checks(repo)

    assert [check.status for check in checks] == ["PASS"], checks
    assert (repo / config_module.DISPATCHER_HANDOFF_STATE_PATH).is_file()


@_VERDICT_BUILDERS
def test_doctor_fails_an_unrecorded_validator_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: str
) -> None:
    repo, content = _verdict_repo(tmp_path, builder)
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(content, encoding="utf-8")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    checks = _handoff_validation_checks(repo)

    assert _only_the_producer_rule_fails(checks), checks


@_VERDICT_BUILDERS
def test_doctor_fails_a_recorded_verdict_edited_afterward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: str
) -> None:
    repo, content = _verdict_repo(tmp_path, builder)
    _write_as_dispatcher(repo, content)
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
    edited = content.replace("Edge case.", "Edge case, edited.")
    assert edited != content
    handoff_path.write_text(edited, encoding="utf-8")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    checks = _handoff_validation_checks(repo)

    assert _only_the_producer_rule_fails(checks), checks


@_VERDICT_BUILDERS
def test_doctor_fails_a_verdict_whose_record_an_agent_planted_then_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: str
) -> None:
    from llm_handoff import orchestrator, validator

    repo, content = _verdict_repo(tmp_path, builder)
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(content, encoding="utf-8")
    # An agent writes a matching record during its turn; the dispatcher
    # clears it when the dispatch returns.
    assert validator.record_dispatcher_handoff(repo, content)
    config = config_module.load_dispatch_config(repo_root=repo)
    orchestrator._clear_dispatcher_handoff_after_dispatch(config, None)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    checks = _handoff_validation_checks(repo)

    assert not (repo / config_module.DISPATCHER_HANDOFF_STATE_PATH).exists()
    assert _only_the_producer_rule_fails(checks), checks


def test_doctor_warns_on_dirty_tree_and_falls_back_to_default_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None, commit=False)
    (repo / "dispatch_config.yaml").unlink()
    (repo / "notes.txt").write_text("dirty\n", encoding="utf-8")
    # The built-in defaults put planner and frontend on Antigravity.
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _write_agy_settings(tmp_path, _complete_agy_settings())
    _stub_which(
        monkeypatch,
        {
            doctor._command_binary_name("codex"),
            doctor._command_binary_name("claude"),
            doctor._command_binary_name("agy"),
        },
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "PASS config: no dispatch_config.yaml found" in output
    assert "WARN git working tree: working tree is dirty" in output
    assert "PASS role planner: provider antigravity" in output
    assert "PASS role frontend: provider antigravity" in output


def test_doctor_fails_outside_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="fatal: not a git repository\n"
        )

    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    report = doctor.run_doctor(repo_root=tmp_path)

    assert report.exit_code == 1
    assert any(
        check.status == "FAIL" and check.name == "git repository"
        for check in report.checks
    )
    assert not any(check.name == "git history" for check in report.checks)
    assert "git history" not in "\n".join(report.lines())


def _git_history_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if " git history:" in line]


def test_doctor_warns_on_a_repository_with_no_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, commit=False)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _git_history_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith("WARN git history:")
    assert "no commits" in lines[0]
    assert "commit before the first dispatcher run" in lines[0]


def test_doctor_warns_on_a_repository_with_one_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _git_history_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith("WARN git history:")
    assert "one commit" in lines[0]
    assert "audit review base" in lines[0]


def test_doctor_passes_git_history_with_two_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _git_history_lines(output) == [
        "PASS git history: repository has at least two commits"
    ]


def test_doctor_warns_when_commits_cannot_be_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run_git = doctor._run_git

    def fake_run_git(
        repo_root: Path, *args: str
    ) -> subprocess.CompletedProcess[str] | None:
        if args[:1] == ("rev-list",):
            return None
        return real_run_git(repo_root, *args)

    repo = _make_repo(tmp_path, NO_GEMINI)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    monkeypatch.setattr(doctor, "_run_git", fake_run_git)

    report = doctor.run_doctor(repo_root=repo)

    history = [check for check in report.checks if check.name == "git history"]
    assert [(check.status, check.message) for check in history] == [
        ("WARN", "could not count commits")
    ]
    assert report.exit_code == 0


def test_doctor_reports_unloadable_explicit_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code = main_module.main(
        ["doctor", "--repo-root", str(repo), "--config", "missing.yaml"]
    )

    assert exit_code == 1
    assert "FAIL config: dispatch config could not be loaded" in capsys.readouterr().out


def _normalizer_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if " normalizer:" in line]


def test_doctor_passes_codex_normalizer_without_calling_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, normalizer_provider="codex")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude", config_module.CODEX_BINARY})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    real_run = subprocess.run

    def git_only_run(args: object, *a: object, **k: object) -> object:
        assert isinstance(args, list) and args[0] == "git", args
        return real_run(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", git_only_run)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _normalizer_lines(output) == [
        f"PASS normalizer: provider codex binary `{config_module.CODEX_BINARY}` "
        "is on PATH (auth not verified)"
    ]


def test_doctor_fails_codex_normalizer_without_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, normalizer_provider="codex")
    # The role binaries resolve; only the normalizer's codex CLI is missing.
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    # An OpenAI key does not make the CLI-only codex normalizer runnable.
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-test-key")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert _normalizer_lines(output) == [
        "FAIL normalizer: provider codex cannot run: the "
        f"`{config_module.CODEX_BINARY}` binary is not available"
    ]
    lines = output.strip().splitlines()
    for prefix in ("PASS config:", "PASS git:", "PASS handoff validation:"):
        assert any(line.startswith(prefix) for line in lines), prefix
    assert lines[-1].startswith("Doctor FAIL: 1 error(s)")


def test_doctor_fails_openai_normalizer_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, normalizer_provider="openai")
    _stub_which(
        monkeypatch, {"fake-codex", "fake-claude", config_module.CODEX_BINARY}
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert _normalizer_lines(output) == [
        "FAIL normalizer: provider openai cannot run: OPENAI_API_KEY is not available"
    ]
    assert output.strip().splitlines()[-1].startswith("Doctor FAIL:")


def test_doctor_fails_claude_normalizer_without_key_or_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert _normalizer_lines(output) == [
        "FAIL normalizer: provider claude cannot run: neither ANTHROPIC_API_KEY "
        f"nor the `{config_module.CLAUDE_BINARY}` binary is available"
    ]


def test_doctor_passes_antigravity_normalizer_with_the_agy_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, normalizer_provider="antigravity")
    _stub_which(
        monkeypatch,
        {"fake-codex", "fake-claude", config_module.ANTIGRAVITY_BINARY},
    )
    # No API key is set: the agy normalizer runs on the CLI login.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _normalizer_lines(output) == [
        "PASS normalizer: provider antigravity binary "
        f"`{config_module.ANTIGRAVITY_BINARY}` is on PATH (auth not verified)"
    ]


def test_doctor_fails_antigravity_normalizer_without_agy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, normalizer_provider="antigravity")
    # The role binaries resolve; only the normalizer's agy CLI is missing.
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    # API keys do not make the CLI-only agy normalizer runnable.
    monkeypatch.setenv("GEMINI_API_KEY", "doctor-test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "doctor-test-key")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert _normalizer_lines(output) == [
        "FAIL normalizer: provider antigravity cannot run: the "
        f"`{config_module.ANTIGRAVITY_BINARY}` binary is not available"
    ]
    assert output.strip().splitlines()[-1].startswith("Doctor FAIL: 1 error(s)")
    # The boundary WARN is added even when the agy binary is missing.
    assert _normalizer_boundary_lines(output) == [_NORMALIZER_BOUNDARY_LINE]


def _normalizer_boundary_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if " normalizer boundary:" in line]


_NORMALIZER_BOUNDARY_LINE = (
    "WARN normalizer boundary: provider antigravity: the only pre-execution "
    "boundary is agy's enforcement of the agent file's `tools: [finish]`; the "
    "user-wide permissions.allow grants in "
    f"{doctor.ANTIGRAVITY_SETTINGS_PATH} still apply to the normalizer run, "
    "and the post-run step and result checks keep the handoff unchanged but "
    "cannot undo a command that already ran"
)


def test_doctor_warns_on_antigravity_normalizer_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, normalizer_provider="antigravity")
    _stub_which(
        monkeypatch,
        {"fake-codex", "fake-claude", config_module.ANTIGRAVITY_BINARY},
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    exit_code, output = _run(repo, capsys)

    assert _normalizer_boundary_lines(output) == [_NORMALIZER_BOUNDARY_LINE]
    # The WARN does not change the exit code: the same checks without the
    # boundary line give the same result.
    report = doctor.run_doctor(repo_root=repo)
    boundary = [
        check for check in report.checks if check.name == "normalizer boundary"
    ]
    assert [check.status for check in boundary] == ["WARN"]
    without = doctor.DoctorReport(
        repo_root=report.repo_root,
        checks=tuple(c for c in report.checks if c.name != "normalizer boundary"),
    )
    assert exit_code == report.exit_code == without.exit_code == 0, output


def test_doctor_has_no_normalizer_boundary_line_for_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI)
    _stub_which(
        monkeypatch,
        {"fake-codex", "fake-claude", config_module.CLAUDE_BINARY},
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "PASS normalizer: provider claude" in output
    assert _normalizer_boundary_lines(output) == []
    assert "normalizer boundary" not in output


def _fake_codex_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "codex.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    else:
        script = bin_dir / "codex"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    return bin_dir


def test_doctor_passes_an_all_codex_init_with_only_codex_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    real_git = REAL_WHICH("git")
    assert real_git is not None, "the doctor's git checks need git on PATH"
    bin_dir = _fake_codex_bin(tmp_path)
    # Only the fake codex CLI and git itself: no claude or gemini CLI.
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bin_dir), str(Path(real_git).parent)])
    )
    for name in NORMALIZER_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    assert shutil.which("codex") is not None
    assert shutil.which("claude") is None
    assert shutil.which("gemini") is None

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert main_module.main(["init", str(repo), "--provider", "codex"]) == 0
    init_output = capsys.readouterr().out
    assert "Provider mapping: all roles on codex" in init_output
    assert "provider CLIs found: codex" in init_output
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")

    report = doctor.run_doctor(repo_root=repo)
    exit_code, output = _run(repo, capsys)

    assert [check for check in report.checks if check.status == "FAIL"] == []
    assert report.exit_code == 0
    assert exit_code == 0, output
    assert not any(line.startswith("FAIL") for line in output.splitlines())
    for role in ROLES:
        assert f"PASS role {role}: provider codex binary `codex` is on PATH" in output
    assert _normalizer_lines(output) == [
        f"PASS normalizer: provider codex binary `{config_module.CODEX_BINARY}` "
        "is on PATH (auth not verified)"
    ]


def test_doctor_does_not_write_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, MIXED)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    before = sorted(p.relative_to(repo) for p in repo.rglob("*") if ".git" not in p.parts)

    _run(repo, capsys)

    after = sorted(p.relative_to(repo) for p in repo.rglob("*") if ".git" not in p.parts)
    assert after == before
    assert not (repo / "logs").exists()


def test_supervisor_command_available_is_shared_with_doctor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert supervisor._command_available is doctor._command_available
    assert supervisor._command_binary_name is doctor._command_binary_name

    shim = doctor._command_binary_name("toolx")
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: "/bin/toolx" if name == shim else None
    )
    assert supervisor._command_available("toolx") is True

    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert supervisor._command_available("toolx") is False


# --- Antigravity permissions -------------------------------------------------

AGY_CHECK = "antigravity permissions"
_grant = agy_perms.antigravity_command_grant


def _agy_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    planner_extra: str = "",
    backend_fallback: bool = True,
    extra_config: str = "",
) -> Path:
    """Put planner (and a backend fallback) on Antigravity; home is tmp_path."""
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None, commit=False)
    config_path = repo / "dispatch_config.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        "  planner:\n    provider: claude\n    binary: fake-claude\n",
        "  planner:\n    provider: antigravity\n    binary: fake-agy\n" + planner_extra,
    )
    if backend_fallback:
        text = text.replace(
            "  backend:\n    provider: codex\n    binary: fake-codex\n",
            "  backend:\n    provider: codex\n    binary: fake-codex\n"
            "    fallback:\n      provider: antigravity\n      binary: fake-agy\n",
        )
    config_path.write_text(text + extra_config, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _stub_which(monkeypatch, {"fake-agy", "fake-codex", "fake-claude"})
    return repo


def _agy_settings_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".gemini" / "antigravity-cli" / "settings.json"


def _write_agy_settings(tmp_path: Path, content: object) -> Path:
    path = _agy_settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    return path


def _complete_agy_settings(extra_allow: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "permissions": {
            "allow": [*agy_perms.ANTIGRAVITY_REQUIRED_ALLOW, *extra_allow],
            "deny": list(agy_perms.ANTIGRAVITY_REQUIRED_DENY),
        }
    }


def _agy_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if f" {AGY_CHECK}:" in line]


def test_doctor_antigravity_check_absent_without_antigravity_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _agy_lines(output) == []


def test_doctor_antigravity_check_absent_for_unsafe_bypass_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(
        tmp_path,
        monkeypatch,
        planner_extra="    unsafe_bypass: true\n",
        backend_fallback=False,
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "PASS role planner: provider antigravity" in output
    assert _agy_lines(output) == []


def test_doctor_antigravity_warns_when_settings_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(f"WARN {AGY_CHECK}:")
    assert "~/.gemini/antigravity-cli/settings.json was not found" in line
    assert "(covers planner, backend fallback)" in line
    for entry in (
        *agy_perms.ANTIGRAVITY_REQUIRED_ALLOW,
        *agy_perms.ANTIGRAVITY_REQUIRED_DENY,
    ):
        assert entry in line
    assert not _agy_settings_path(tmp_path).exists()


def test_doctor_antigravity_warns_on_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, "{not json")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert "is not valid JSON" in lines[0]
    assert "(covers planner, backend fallback)" in lines[0]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ([], "is not a JSON object"),
        ({"other": 1}, 'has no "permissions" object'),
        ({"permissions": []}, 'has no "permissions" object'),
        (
            {"permissions": {"allow": "command(git status)"}},
            '"permissions.allow" value that is not a list of strings',
        ),
        (
            {"permissions": {"allow": [], "deny": [1]}},
            '"permissions.deny" value that is not a list of strings',
        ),
    ],
)
def test_doctor_antigravity_warns_on_wrong_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    content: object,
    reason: str,
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, content)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert reason in lines[0]


def test_doctor_antigravity_warns_when_settings_file_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    # A directory at the settings path cannot be read as a file.
    _agy_settings_path(tmp_path).mkdir(parents=True)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert "could not be read" in lines[0]


def test_doctor_antigravity_missing_keys_count_as_empty_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, {"permissions": {}})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 2
    assert all(line.startswith(f"WARN {AGY_CHECK}:") for line in lines)
    assert "permissions.allow is missing" in lines[0]
    assert "permissions.deny is missing" in lines[1]


@pytest.mark.parametrize("grant", ["command(git push)", "command(git push origin main)"])
def test_doctor_antigravity_fails_when_allow_grants_git_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, grant: str
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(grant,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    lines = _agy_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(f"FAIL {AGY_CHECK}:")
    assert f"`{grant}`" in lines[0]
    assert "(covers planner, backend fallback)" in lines[0]


def test_doctor_antigravity_warns_on_missing_allow_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(
        tmp_path,
        monkeypatch,
        extra_config='verification_allowed_prefixes: ["npm test"]\n',
    )
    settings = _complete_agy_settings()
    allow = settings["permissions"]["allow"]  # type: ignore[index]
    allow.remove(_grant("git diff"))
    allow.append("command(regex:^npm test$)")
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(f"WARN {AGY_CHECK}:")
    assert (
        f"permissions.allow is missing {_grant('git diff')}, {_grant('npm test')}"
        in line
    )
    assert "entries are matched by exact string" in line
    assert "legacy `command(<cmd>)` exact grants do not cover" in line
    assert "regex grants" not in line
    assert "(covers planner, backend fallback)" in line


def test_doctor_antigravity_warns_on_legacy_exact_allow_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    settings = _complete_agy_settings()
    settings["permissions"]["allow"] = [  # type: ignore[index]
        f"command({command})" for command in agy_perms.ANTIGRAVITY_ALLOWED_COMMANDS
    ]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert "permissions.allow is missing " + ", ".join(
        agy_perms.ANTIGRAVITY_REQUIRED_ALLOW
    ) in lines[0]


def test_doctor_antigravity_warns_on_missing_deny_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    settings = _complete_agy_settings()
    settings["permissions"]["deny"] = []  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert lines == [
        f"WARN {AGY_CHECK}: ~/.gemini/antigravity-cli/settings.json "
        "permissions.deny is missing command(regex:^git( .*)? push( .*)?$) "
        "(covers planner)"
    ]


def test_doctor_antigravity_does_not_require_an_unsandboxed_deny_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    _write_agy_settings(tmp_path, _complete_agy_settings())

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"PASS {AGY_CHECK}:")
    assert "unsandboxed" not in output


def test_doctor_antigravity_warns_on_leftover_unsandboxed_deny_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    settings = _complete_agy_settings()
    settings["permissions"]["deny"].append(  # type: ignore[index]
        "unsandboxed(regex:^git( .*)? push( .*)?$)"
    )
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert lines == [
        f"WARN {AGY_CHECK}: ~/.gemini/antigravity-cli/settings.json "
        "permissions.deny has `unsandboxed(regex:^git( .*)? push( .*)?$)` "
        "(covers planner); agy ignores `unsandboxed(...)` rules, so they allow "
        "or deny nothing; remove them (the `command(...)` entries are the ones "
        "agy applies)"
    ]


def test_doctor_antigravity_warns_on_unsandboxed_entries_in_each_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    settings = _complete_agy_settings(
        extra_allow=("unsandboxed(npm test)", "unsandboxed(regex:(push)")
    )
    settings["permissions"]["ask"] = ["unsandboxed(*)"]  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 2, output
    assert all(line.startswith(f"WARN {AGY_CHECK}:") for line in lines)
    assert (
        "permissions.allow has `unsandboxed(npm test)`, `unsandboxed(regex:(push)`"
        in lines[0]
    )
    assert "permissions.ask has `unsandboxed(*)`" in lines[1]
    assert all("agy ignores `unsandboxed(...)` rules" in line for line in lines)


@pytest.mark.parametrize(
    "grant",
    [
        "unsandboxed(git push)",
        "unsandboxed(regex:^git status( .*)?$)",
        "unsandboxed(regex:^git( .*)?$)",
        "unsandboxed(regex:.*)",
    ],
)
def test_doctor_antigravity_warns_not_fails_on_unsandboxed_push_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, grant: str
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(grant,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert f"permissions.allow has `{grant}`" in lines[0]
    assert "agy ignores `unsandboxed(...)` rules" in lines[0]


def test_doctor_antigravity_warns_on_legacy_exact_deny_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    settings = _complete_agy_settings()
    settings["permissions"]["deny"] = [  # type: ignore[index]
        "command(git push)",
        "unsandboxed(git push)",
    ]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 2, output
    assert "permissions.deny has `unsandboxed(git push)`" in lines[0]
    assert lines[1] == (
        f"WARN {AGY_CHECK}: ~/.gemini/antigravity-cli/settings.json "
        "permissions.deny is missing command(regex:^git( .*)? push( .*)?$) "
        "(covers planner)"
    )


@pytest.mark.parametrize("grant", ["command(regex:push)", "command(regex:.*)"])
def test_doctor_antigravity_fails_when_regex_allow_matches_git_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, grant: str
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(grant,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"FAIL {AGY_CHECK}:")
    assert f"`{grant}`" in lines[0]
    assert "agents must not push" in lines[0]
    assert "cannot verify" not in lines[0]


@pytest.mark.parametrize(
    "grant",
    [
        "command(regex:^git push --force$)",
        "command(regex:^git push origin feature-x$)",
        "command(regex:^git -c x=y push origin main$)",
        "command(regex:^git status$)",
        "command(regex:^GIT( .*)?$)",
        "command(regex:^git( .*)?$)",
        "command(regex:^git push( .*)?$)",
        "command(regex:^git -C \\. push)",
        "command(regex:^git[)",
    ],
)
def test_doctor_antigravity_fails_when_regex_allow_names_git_unverifiably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, grant: str
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(grant,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    line = lines[0]
    assert line.startswith(f"FAIL {AGY_CHECK}:")
    assert f"`{grant}`" in line
    assert "(covers planner, backend fallback)" in line
    assert "cannot verify that this regex entry does not allow `git push`" in line
    assert "remove it or replace it with a generated grant" in line
    assert "agents must not push" not in line


@pytest.mark.parametrize("prefix", ["git", "git -c x=y", "git push", "git -C ."])
def test_doctor_antigravity_fails_on_generated_grant_for_push_capable_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, prefix: str
) -> None:
    repo = _agy_repo(
        tmp_path,
        monkeypatch,
        extra_config=f'verification_allowed_prefixes: ["{prefix}"]\n',
    )
    grant = _grant(prefix)
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(grant,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    # A `git push` prefix also draws the deny-shadowing WARN; only FAILs count.
    fails = [
        line for line in _agy_lines(output) if line.startswith(f"FAIL {AGY_CHECK}:")
    ]
    assert len(fails) == 1, output
    assert f"`{grant}`" in fails[0]
    assert "agents must not push" in fails[0]


_INTERP_GRANTS = (_grant("python"), _grant("python3"), _grant("pytest"))


@pytest.mark.parametrize(
    ("prefixes", "grants", "opt_in"),
    [
        ([], ("command(regex:^git status( .*)?$)",), False),
        (
            ["python -m pytest"],
            (*_INTERP_GRANTS, "command(regex:^python -m pytest( .*)?$)"),
            True,
        ),
        (["git stash list"], (_grant("git stash list"),), False),
    ],
)
def test_doctor_antigravity_accepts_generated_grants_that_cannot_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    prefixes: list[str],
    grants: tuple[str, ...],
    opt_in: bool,
) -> None:
    repo = _agy_repo(
        tmp_path,
        monkeypatch,
        extra_config=f"verification_allowed_prefixes: {json.dumps(prefixes)}\n"
        + ("interpreter_grants: true\n" if opt_in else ""),
    )
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=grants))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"PASS {AGY_CHECK}:")


@pytest.mark.parametrize(
    ("command", "can_push"),
    [
        ("git", True),
        ("git -c x=y", True),
        ("git -C .", True),
        ("git push", True),
        ("git remote push", True),
        ("git  status", False),
        ("git status", False),
        ("git pushx", False),
        ("python -m pytest", False),
        ("npm push", False),
        ("", False),
    ],
)
def test_plain_command_can_push(command: str, can_push: bool) -> None:
    assert doctor._plain_command_can_push(command) is can_push


def test_doctor_antigravity_reports_one_push_fail_per_allow_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(
        tmp_path,
        monkeypatch,
        extra_config='verification_allowed_prefixes: ["git push"]\n',
    )
    grants = (
        _grant("git push"),
        "command(regex:^git push --force$)",
        "command(regex:.*)",
        "command(git push)",
    )
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=grants))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    fails = [
        line for line in _agy_lines(output) if line.startswith(f"FAIL {AGY_CHECK}:")
    ]
    assert len(fails) == len(grants), output
    for line, grant in zip(fails, grants, strict=True):
        assert f"`{grant}`" in line


def test_doctor_antigravity_push_fail_escapes_control_characters_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    grant = "command(regex:^git\\.? push\x1b?)"
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(grant,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    assert "\x1b" not in output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert "`command(regex:^git\\.? push\\x1b?)`" in lines[0]


@pytest.mark.parametrize(
    "grant",
    [
        "command(regex:[)",
        "command(regex:^git status( .*)?$)",
        "command(regex:^npm publish$)",
        "command(regex:^npm test$)",
    ],
)
def test_doctor_antigravity_ignores_non_push_and_uncompilable_regex_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, grant: str
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(grant,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"PASS {AGY_CHECK}:")


@pytest.mark.parametrize(
    ("rule", "shadowed"),
    [
        ("command(*)", f"{_grant('git status')}, {_grant('git diff')}"),
        ("command(git)", f"{_grant('git status')}, {_grant('git diff')}"),
        ("command(git *)", f"{_grant('git status')}, {_grant('git diff')}"),
        (
            "command(regex:^git (st|di))",
            f"{_grant('git status')}, {_grant('git diff')}",
        ),
        ("command(git status)", _grant("git status")),
        ("command(npm test)", _grant("npm test")),
    ],
)
def test_doctor_antigravity_warns_when_ask_rules_shadow_required_allows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    rule: str,
    shadowed: str,
) -> None:
    repo = _agy_repo(
        tmp_path,
        monkeypatch,
        extra_config='verification_allowed_prefixes: ["npm test"]\n',
    )
    settings = _complete_agy_settings(extra_allow=(_grant("npm test"),))
    settings["permissions"]["ask"] = [rule, "command(git push)"]  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    line = lines[0]
    assert line.startswith(f"WARN {AGY_CHECK}:")
    assert "permissions.ask has rules that override required allow entries" in line
    assert f"`{rule}` shadows {shadowed}" in line
    assert "`command(git push)`" not in line
    assert "Deny > Ask > Allow" in line
    assert "(covers planner, backend fallback)" in line
    assert "PASS" not in line


def test_doctor_antigravity_warns_when_deny_rules_shadow_required_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    settings = _complete_agy_settings()
    settings["permissions"]["deny"].append("command(git log)")  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert "permissions.deny has rules that override required allow entries" in lines[0]
    assert f"`command(git log)` shadows {_grant('git log')}" in lines[0]


def test_doctor_antigravity_shadowing_covers_opt_in_interpreter_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(
        tmp_path,
        monkeypatch,
        backend_fallback=False,
        extra_config="interpreter_grants: true\n",
    )
    settings = _complete_agy_settings(extra_allow=_INTERP_GRANTS)
    settings["permissions"]["deny"].append("command(pytest)")  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert f"`command(pytest)` shadows {_grant('pytest')}" in lines[0]


@pytest.mark.parametrize(
    "ask",
    [
        ["command(npm publish)", "command(regex:^curl)"],
        ["command(regex:[)"],
        [],
    ],
)
def test_doctor_antigravity_passes_with_non_shadowing_ask_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, ask: list[str]
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    settings = _complete_agy_settings()
    settings["permissions"]["ask"] = ask  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"PASS {AGY_CHECK}:")


def test_doctor_antigravity_warns_when_ask_is_not_a_list_of_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    settings = _complete_agy_settings()
    settings["permissions"]["ask"] = "command(*)"  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert '"permissions.ask" value that is not a list of strings' in lines[0]


def test_doctor_antigravity_passes_with_a_complete_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(
        tmp_path,
        monkeypatch,
        extra_config='verification_allowed_prefixes: ["npm test"]\n',
    )
    _write_agy_settings(
        tmp_path, _complete_agy_settings(extra_allow=(_grant("npm test"),))
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(f"PASS {AGY_CHECK}:")
    assert "(covers planner, backend fallback)" in lines[0]


def test_doctor_antigravity_passes_with_exactly_the_required_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    _write_agy_settings(
        tmp_path,
        {
            "permissions": {
                "allow": list(agy_perms.ANTIGRAVITY_REQUIRED_ALLOW),
                "deny": list(agy_perms.ANTIGRAVITY_REQUIRED_DENY),
            }
        },
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert lines == [
        f"PASS {AGY_CHECK}: ~/.gemini/antigravity-cli/settings.json has every "
        "required allow and deny entry (covers planner)"
    ]
    assert "shadows" not in output


def test_shadowing_rules_do_not_flag_the_required_deny_entries() -> None:
    allow = agy_perms.antigravity_allow_entries(
        ["npm test", "python -m pytest"], interpreter_grants=True
    )
    commands = agy_perms.antigravity_allowed_commands(
        ["npm test", "python -m pytest"], interpreter_grants=True
    )

    assert (
        doctor._shadowing_rules(
            list(agy_perms.ANTIGRAVITY_REQUIRED_DENY), commands, allow
        )
        == []
    )
    assert doctor._shadowing_rules(["command(regex:^python)"], commands, allow) == [
        (
            "command(regex:^python)",
            [
                _grant("python"),
                _grant("python3"),
                _grant("python -m pytest"),
            ],
        )
    ]


@pytest.mark.parametrize(
    "entry",
    [
        _grant("python"),
        _grant("python3"),
        _grant("pytest"),
        _grant("python -m pytest tests -q"),
        "command(python)",
        "command(pytest -q)",
    ],
)
def test_doctor_antigravity_warns_on_user_wide_interpreter_grants_with_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, entry: str
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(entry,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    line = lines[0]
    assert line.startswith(f"WARN {AGY_CHECK}:")
    assert f"grants the interpreter entry `{entry}`" in line
    assert "user-wide and still applies" in line
    assert "including `git push`" in line
    assert "remove it, or set interpreter_grants: true" in line
    assert "(covers planner, backend fallback)" in line


@pytest.mark.parametrize(
    "entry",
    [
        "command(regex:^python3?( .*)?$)",
        "command(regex:^npm test( .*)?$)",
        _grant("pythonx"),
        "command(npm test)",
        "unsandboxed(python)",
    ],
)
def test_doctor_antigravity_ignores_other_entries_for_the_interpreter_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, entry: str
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=(entry,)))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "interpreter entry" not in output


def test_doctor_antigravity_accepts_interpreter_grants_with_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(
        tmp_path, monkeypatch, extra_config="interpreter_grants: true\n"
    )
    _write_agy_settings(tmp_path, _complete_agy_settings(extra_allow=_INTERP_GRANTS))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"PASS {AGY_CHECK}:")


def test_doctor_antigravity_requires_interpreter_grants_with_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(
        tmp_path, monkeypatch, extra_config="interpreter_grants: true\n"
    )
    _write_agy_settings(tmp_path, _complete_agy_settings())

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert "permissions.allow is missing " + ", ".join(_INTERP_GRANTS) in lines[0]


def test_settings_interpreter_grant_matches_generated_and_exact_forms() -> None:
    assert doctor._settings_interpreter_grant(_grant("python -m pytest"))
    assert doctor._settings_interpreter_grant("command(python3 -m pytest)")
    assert not doctor._settings_interpreter_grant(_grant("git status"))
    assert not doctor._settings_interpreter_grant("command(regex:^python.*$)")
    assert not doctor._settings_interpreter_grant("unsandboxed(regex:^python( .*)?$)")


def test_doctor_antigravity_leaves_the_settings_file_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch)
    settings_path = _write_agy_settings(tmp_path, {"permissions": {"allow": []}})
    os.utime(settings_path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    before_bytes = settings_path.read_bytes()
    before_mtime = settings_path.stat().st_mtime_ns

    doctor.run_doctor(repo_root=repo)
    _run(repo, capsys)

    assert settings_path.read_bytes() == before_bytes
    assert settings_path.stat().st_mtime_ns == before_mtime
    assert sorted(p.name for p in settings_path.parent.iterdir()) == ["settings.json"]


# --- Tracker connectors (Linear mode) ----------------------------------------

TRACKER_CHECK = "tracker connector"
LINEAR_TRACKER = """tracker:
  provider: linear
  team: ENG
  project: Platform
  eligible_state: Todo
  in_progress_state: In Progress
  success_state: In Review
  mcp_server: linear
"""
CLAUDE_ROLES_NO_GEMINI = "planner, frontend, auditor, validator, finalizer"
SECRET_SENTINEL = "sk-sentinel-DO-NOT-PRINT-0123456789"


def _role_block(role: str, provider: str) -> str:
    return f"  {role}:\n    provider: {provider}\n    binary: fake-{provider}\n"


def _linear_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, str] = NO_GEMINI,
    *,
    claude_mcp_config: str | None = None,
    role_extra: dict[str, str] | None = None,
) -> Path:
    """Linear-mode repo with home at tmp_path/home and config-dir variables unset.

    ``CODEX_HOME`` and ``CLAUDE_CONFIG_DIR`` are unset.
    ``role_extra`` appends YAML lines (indented four spaces) to a role block.
    """
    repo = _make_repo(tmp_path, providers, handoff=None, commit=False)
    config_path = repo / "dispatch_config.yaml"
    text = config_path.read_text(encoding="utf-8")
    for role, extra in (role_extra or {}).items():
        block = _role_block(role, providers[role])
        text = text.replace(block, block + extra)
    tracker = LINEAR_TRACKER
    if claude_mcp_config is not None:
        tracker += f"  claude_mcp_config: {claude_mcp_config}\n"
    config_path.write_text(text + tracker, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _stub_which(
        monkeypatch,
        {
            "fake-claude",
            "fake-codex",
            "fake-agy",
            "fake-antigravity",
        },
    )
    return repo


def _write_json(path: Path, content: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")
    return path


def _write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _servers(name: str = "linear") -> dict[str, object]:
    return {"mcpServers": {name: {"command": "npx", "args": ["linear-mcp"]}}}


def _tracker_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if f" {TRACKER_CHECK}:" in line]


def _provider_line(output: str, provider: str) -> str:
    matches = [
        line
        for line in _tracker_lines(output)
        if line.split(":", 1)[1].strip().startswith(f"{provider}:")
    ]
    assert len(matches) == 1, output
    return matches[0]


def test_doctor_tracker_connector_absent_in_vanilla_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, MIXED, handoff=None, commit=False)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)
    assert exit_code == 0, output
    assert TRACKER_CHECK not in output

    _add_config(repo, "tracker:\n  provider: none\n")
    exit_code, output = _run(repo, capsys)
    assert exit_code == 0, output
    assert TRACKER_CHECK not in output


def test_doctor_tracker_connector_claude_mcp_config_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path, monkeypatch, claude_mcp_config="config/linear-mcp.json"
    )
    _write_json(repo / "config" / "linear-mcp.json", _servers())

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"PASS {TRACKER_CHECK}: claude:")
    assert "linear-mcp.json" in line
    assert "`linear`" in line
    assert line.endswith(f"(covers {CLAUDE_ROLES_NO_GEMINI})")


def test_doctor_tracker_connector_claude_mcp_config_fails_without_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path, monkeypatch, claude_mcp_config="config/linear-mcp.json"
    )
    _write_json(repo / "config" / "linear-mcp.json", _servers("other"))
    # A user-level definition does not count: strict launches load only the file.
    _write_json(tmp_path / "home" / ".claude.json", _servers())

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"FAIL {TRACKER_CHECK}: claude:")
    assert "linear-mcp.json" in line
    assert "`linear`" in line
    assert "--strict-mcp-config" in line
    assert line.endswith(f"(covers {CLAUDE_ROLES_NO_GEMINI})")


def test_doctor_tracker_connector_claude_mcp_config_fails_without_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path, monkeypatch, claude_mcp_config="config/linear-mcp.json"
    )
    _write_json(repo / "config" / "linear-mcp.json", {"mcpServers": ["linear"]})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"FAIL {TRACKER_CHECK}: claude:")
    assert "has no mcpServers object" in line


def test_doctor_tracker_connector_claude_mcp_config_fails_on_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path, monkeypatch, claude_mcp_config="config/linear-mcp.json"
    )
    _write_json(repo / "config" / "linear-mcp.json", '{"mcpServers": {')

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"FAIL {TRACKER_CHECK}: claude:")
    assert "is not valid JSON" in line
    assert "linear-mcp.json" in line


def test_doctor_tracker_connector_claude_passes_from_project_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _write_json(repo / ".mcp.json", _servers())

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"PASS {TRACKER_CHECK}: claude:")
    assert ".mcp.json" in line
    assert line.endswith(f"(covers {CLAUDE_ROLES_NO_GEMINI})")


def test_doctor_tracker_connector_claude_passes_from_user_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _write_json(tmp_path / "home" / ".claude.json", _servers())

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"PASS {TRACKER_CHECK}: claude:")
    assert ".claude.json (user mcpServers)" in line


def test_doctor_tracker_connector_claude_passes_from_matching_project_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _write_json(
        tmp_path / "home" / ".claude.json",
        {
            "projects": {
                str(tmp_path / "elsewhere"): {"mcpServers": {"other": {}}},
                str(repo): _servers(),
            }
        },
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"PASS {TRACKER_CHECK}: claude:")
    assert "projects entry for" in line


def test_doctor_tracker_connector_claude_ignores_other_project_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _write_json(
        tmp_path / "home" / ".claude.json",
        {"projects": {str(tmp_path / "elsewhere"): _servers()}},
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _provider_line(output, "claude").startswith(f"WARN {TRACKER_CHECK}:")


def test_doctor_tracker_connector_claude_honors_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    config_dir = tmp_path / "claude-config"
    _write_json(config_dir / ".claude.json", _servers())
    # The default user file is ignored while CLAUDE_CONFIG_DIR is set.
    _write_json(tmp_path / "home" / ".claude.json", "not json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"PASS {TRACKER_CHECK}: claude:")
    assert f"{config_dir / '.claude.json'} (user mcpServers)" in line


def test_doctor_tracker_connector_claude_config_dir_hides_default_user_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    _write_json(tmp_path / "home" / ".claude.json", _servers())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"WARN {TRACKER_CHECK}: claude:")
    assert str(config_dir / ".claude.json") in line
    assert str(tmp_path / "home" / ".claude.json") not in line


def test_doctor_tracker_connector_claude_warns_when_no_source_defines_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _write_json(repo / ".mcp.json", _servers("other"))
    _write_json(tmp_path / "home" / ".claude.json", "not json")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "claude")
    assert line.startswith(f"WARN {TRACKER_CHECK}: claude:")
    assert "`linear`" in line
    assert str(repo / ".mcp.json") in line
    assert str(tmp_path / "home" / ".claude.json") in line
    assert "projects entry for" in line
    assert "is not valid JSON" in line
    assert "managed settings or plugins" in line
    assert line.endswith(f"(covers {CLAUDE_ROLES_NO_GEMINI})")


def test_doctor_tracker_connector_splits_unsafe_bypass_claude_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path,
        monkeypatch,
        claude_mcp_config="config/linear-mcp.json",
        role_extra={"finalizer": "    unsafe_bypass: true\n"},
    )
    _write_json(repo / "config" / "linear-mcp.json", _servers())
    _write_json(repo / ".mcp.json", _servers())

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    claude_lines = [
        line for line in _tracker_lines(output) if f"{TRACKER_CHECK}: claude:" in line
    ]
    assert len(claude_lines) == 2, output
    assert "tracker.claude_mcp_config" in claude_lines[0]
    assert claude_lines[0].endswith("(covers planner, frontend, auditor, validator)")
    assert str(repo / ".mcp.json") in claude_lines[1]
    assert claude_lines[1].endswith("(covers finalizer)")


def test_doctor_tracker_connector_codex_passes_from_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    codex_home = tmp_path / "codex-home"
    _write_text(codex_home / "config.toml", '[mcp_servers.linear]\ncommand = "npx"\n')
    # The default user path is ignored while CODEX_HOME is set.
    _write_text(tmp_path / "home" / ".codex" / "config.toml", "not = [valid")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "codex")
    assert line == (
        f"PASS {TRACKER_CHECK}: codex: {codex_home / 'config.toml'} defines MCP "
        "server `linear` (covers backend)"
    )


def test_doctor_tracker_connector_codex_passes_from_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEX_HOME", "")
    _write_text(
        tmp_path / "home" / ".codex" / "config.toml",
        '[mcp_servers.other]\ncommand = "npx"\n',
    )
    _write_text(
        repo / ".codex" / "config.toml", '[mcp_servers.linear]\ncommand = "npx"\n'
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "codex")
    assert line.startswith(f"PASS {TRACKER_CHECK}: codex:")
    assert str(repo / ".codex" / "config.toml") in line


def test_doctor_tracker_connector_codex_warns_on_invalid_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _write_text(tmp_path / "home" / ".codex" / "config.toml", "[mcp_servers.linear\n")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    line = _provider_line(output, "codex")
    assert line.startswith(f"WARN {TRACKER_CHECK}: codex:")
    assert "is not valid TOML" in line
    assert str(tmp_path / "home" / ".codex" / "config.toml") in line
    assert str(repo / ".codex" / "config.toml") in line
    assert line.endswith("(covers backend)")


def test_doctor_tracker_connector_fails_config_load_for_antigravity_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path, monkeypatch, {**NO_GEMINI, "planner": "antigravity"}
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1, output
    assert "FAIL config: dispatch config could not be loaded" in output
    assert "does not support provider: antigravity" in output
    assert "agents.planner" in output
    assert _tracker_lines(output) == []


def test_doctor_tracker_connector_reports_one_line_per_provider_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path,
        monkeypatch,
        MIXED,
        role_extra={
            "backend": (
                "    fallback:\n      provider: claude\n      binary: fake-claude\n"
            ),
        },
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _tracker_lines(output)
    assert len(lines) == 2, output
    providers = [line.split(":", 2)[1].strip() for line in lines]
    assert providers == ["claude", "codex"]
    assert lines[0].endswith(
        "(covers backend fallback, frontend, auditor, validator, finalizer)"
    )
    assert lines[1].endswith("(covers planner, backend)")


def test_doctor_tracker_connector_never_prints_config_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path,
        monkeypatch,
        MIXED,
        claude_mcp_config="config/linear-mcp.json",
        role_extra={"finalizer": "    unsafe_bypass: true\n"},
    )
    entry = {
        "command": "npx",
        "env": {"LINEAR_API_KEY": SECRET_SENTINEL},
        "headers": {"Authorization": f"Bearer {SECRET_SENTINEL}"},
    }
    _write_json(repo / "config" / "linear-mcp.json", {"mcpServers": {"linear": entry}})
    _write_json(
        tmp_path / "home" / ".claude.json",
        {"mcpServers": {"other": entry}, "projects": {str(repo): {"mcpServers": {}}}},
    )
    _write_text(
        tmp_path / "home" / ".codex" / "config.toml",
        f'[mcp_servers.linear]\ncommand = "npx"\n'
        f'env = {{ LINEAR_API_KEY = "{SECRET_SENTINEL}" }}\n',
    )
    # Invalid JSON whose text holds the sentinel: the parse error must not echo it.
    _write_text(
        repo / ".mcp.json",
        '{"mcpServers": {"linear": {"env": {"T": "' + SECRET_SENTINEL + '"}}',
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert len(_tracker_lines(output)) == 3, output
    assert SECRET_SENTINEL not in output
    assert "sentinel" not in output.lower()


def test_doctor_tracker_connector_runs_no_provider_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch, MIXED)
    _write_json(repo / ".mcp.json", _servers())
    real_run = subprocess.run
    calls: list[str] = []

    def recording_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        first = command[0] if isinstance(command, (list, tuple)) else command
        calls.append(Path(str(first)).stem.lower())
        return real_run(*args, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", recording_run)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert len(_tracker_lines(output)) == 2, output
    assert "git" in calls
    provider_binaries = {"claude", "codex", "gemini", "agy", "antigravity"}
    for name in calls:
        assert name.removeprefix("fake-") not in provider_binaries, calls


# --- Tracker probe (--probe-tracker) ----------------------------------------

PROBE_CHECK = "tracker probe"
CONFIGURED_STATES = ["Todo", "In Progress", "In Review"]


def _probe_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if f" {PROBE_CHECK}:" in line]


def _probe_line(output: str) -> str:
    lines = _probe_lines(output)
    assert len(lines) == 1, output
    return lines[0]


def _envelope(
    structured: object = None,
    *,
    issue_found: bool = True,
    team_states: list[str] | None = None,
    error: str = "",
    is_error: bool = False,
) -> str:
    if structured is None:
        structured = {
            "issue_found": issue_found,
            "team_states": (
                ["Backlog", *CONFIGURED_STATES, "Done"]
                if team_states is None
                else team_states
            ),
            "error": error,
        }
    return json.dumps(
        {"type": "result", "is_error": is_error, "structured_output": structured}
    )


class _FakeProbeRunner:
    """Stands in for ``doctor._run_logged_agent_command``; launches nothing."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self.result = _ProcessResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    def __call__(
        self, agent_name: str, command: list[str], **kwargs: object
    ) -> _ProcessResult:
        self.calls.append({"agent_name": agent_name, "command": command, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.result


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, runner: _FakeProbeRunner | None = None, **kw
) -> _FakeProbeRunner:
    runner = runner or _FakeProbeRunner(**kw)
    monkeypatch.setattr(doctor, "_run_logged_agent_command", runner)
    return runner


def _run_probe(
    repo: Path, capsys: pytest.CaptureFixture[str], issue: str = "ENG-123"
) -> tuple[int, str]:
    exit_code = main_module.main(
        ["doctor", "--repo-root", str(repo), "--probe-tracker", issue]
    )
    return exit_code, capsys.readouterr().out


def _option_value(command: list[str], flag: str) -> str:
    assert command.count(flag) == 1, command
    return command[command.index(flag) + 1]


def test_doctor_without_probe_flag_prints_no_probe_line_and_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    runner = _install_runner(monkeypatch, raises=AssertionError("no launch"))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert PROBE_CHECK not in output
    assert runner.calls == []


def test_doctor_probe_fails_in_vanilla_mode_without_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None, commit=False)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "--probe-tracker needs tracker.provider: linear" in line
    assert runner.calls == []


def test_doctor_probe_prints_nothing_when_the_config_does_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _add_config(repo, "  not_a_key: 1\n")
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    assert output.startswith("FAIL config:")
    assert _probe_lines(output) == []
    assert runner.calls == []


@pytest.mark.parametrize(
    "issue",
    ["qq 7", "QQ-0", "QQ-", "1QQ-5", "ABCDEFGHIJK-1", "QQ-1234567890", "Q-1;x"],
)
def test_doctor_probe_rejects_invalid_identifier_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, issue: str
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys, issue)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "Linear issue identifier such as ENG-123" in line
    assert issue not in line
    assert runner.calls == []


def test_doctor_probe_accepts_identifier_with_surrounding_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys, "  eng-42 ")

    assert exit_code == 0, output
    assert "issue eng-42" in _probe_line(output)
    assert "eng-42" in _option_value(runner.calls[0]["command"], "-p")


def test_doctor_probe_fails_without_a_claude_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch, {role: "codex" for role in ROLES})
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "runs through the Claude CLI" in line
    assert "no configured role or fallback" in line
    assert runner.calls == []


def test_doctor_probe_line_follows_the_tracker_connector_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 0, output
    lines = output.splitlines()
    probe_index = lines.index(_probe_line(output))
    connector_indexes = [
        index for index, line in enumerate(lines) if f" {TRACKER_CHECK}:" in line
    ]
    assert connector_indexes and probe_index == connector_indexes[-1] + 1


def test_doctor_probe_argv_without_claude_mcp_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 0, output
    assert len(runner.calls) == 1
    call = runner.calls[0]
    command = call["command"]
    assert command[0] == "fake-claude"
    assert _option_value(command, "--permission-mode") == "dontAsk"
    assert _option_value(command, "--tools") == ""
    assert _option_value(command, "--allowedTools").split(",") == [
        "mcp__linear__get_issue",
        "mcp__linear__list_issue_statuses",
    ]
    disallowed = _option_value(command, "--disallowedTools").split(",")
    for tool in (
        *config_module.CLAUDE_DISALLOWED_TOOLS,
        "mcp__linear__save_issue",
        "mcp__linear__save_comment",
        "mcp__linear__create_issue",
        "mcp__linear__update_issue",
        "mcp__linear__create_comment",
    ):
        assert tool in disallowed
    # Project and local settings (and their hooks) are skipped; hooks off.
    assert _option_value(command, "--setting-sources") == "user"
    assert json.loads(_option_value(command, "--settings")) == {
        "disableAllHooks": True
    }
    assert "--bare" not in command
    assert "--strict-mcp-config" not in command
    assert "--mcp-config" not in command
    assert _option_value(command, "--model") == config_module.CLAUDE_MODEL
    assert _option_value(command, "--output-format") == "json"
    schema = json.loads(_option_value(command, "--json-schema"))
    assert schema == doctor.TRACKER_PROBE_JSON_SCHEMA
    assert set(schema["required"]) == {"issue_found", "team_states", "error"}
    assert schema["additionalProperties"] is False
    assert "--no-session-persistence" in command
    assert command[-2] == "-p"
    prompt = command[-1]
    assert "ENG-123" in prompt and "ENG" in prompt
    assert "get_issue" in prompt and "list_issue_statuses" in prompt
    assert "Platform" not in prompt and "In Review" not in prompt
    assert prompt.isascii()
    assert call["cwd"] == repo.resolve() or call["cwd"] == repo
    assert call["timeout_ms"] == doctor._TRACKER_PROBE_TIMEOUT_MS == 180_000
    assert call["log"] is None
    assert isinstance(call["env"], dict)


def test_doctor_probe_never_denies_whole_tracker_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # Deny rules win over allow rules, so a server-wide deny would also block
    # the two read tools; the probe keeps a per-tool write denylist instead.
    repo = _linear_repo(tmp_path, monkeypatch)
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 0, output
    command = runner.calls[0]["command"]
    read_tools = {"mcp__linear__get_issue", "mcp__linear__list_issue_statuses"}
    allowed = _option_value(command, "--allowedTools").split(",")
    disallowed = _option_value(command, "--disallowedTools").split(",")
    for tool in allowed:
        if tool.startswith("mcp__linear__"):
            assert tool in read_tools
    for server_wide in ("mcp__linear", "mcp__linear__*"):
        assert server_wide not in allowed
        assert server_wide not in disallowed
    assert not read_tools & set(disallowed)


def test_doctor_probe_argv_with_claude_mcp_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(
        tmp_path, monkeypatch, claude_mcp_config="config/linear-mcp.json"
    )
    _write_json(repo / "config" / "linear-mcp.json", _servers())
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 0, output
    command = runner.calls[0]["command"]
    assert _option_value(command, "--mcp-config") == str(
        (repo / "config" / "linear-mcp.json").resolve()
    )
    assert command.count("--strict-mcp-config") == 1
    assert _option_value(command, "--setting-sources") == "user"
    assert json.loads(_option_value(command, "--settings")) == {
        "disableAllHooks": True
    }
    assert _option_value(command, "--tools") == ""
    assert "mcp__linear__save_issue" in _option_value(command, "--disallowedTools")


def test_doctor_probe_uses_the_first_claude_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    providers = {**NO_GEMINI, "planner": "codex", "backend": "claude"}
    repo = _linear_repo(
        tmp_path,
        monkeypatch,
        providers,
        role_extra={
            "backend": (
                "    model: backend-probe-model\n"
                "    unsafe_bypass: true\n"
                "    permissions_flag: --some-flag\n"
            ),
            "frontend": "    model: frontend-model\n",
        },
    )
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 0, output
    call = runner.calls[0]
    command = call["command"]
    assert call["agent_name"] == "backend"
    assert command[0] == "fake-claude"
    assert _option_value(command, "--model") == "backend-probe-model"
    # unsafe_bypass and permissions_flag are ignored by the probe.
    assert "--some-flag" not in command
    assert "--dangerously-skip-permissions" not in command
    assert _option_value(command, "--permission-mode") == "dontAsk"
    line = _probe_line(output)
    assert line.startswith(f"PASS {PROBE_CHECK}:")
    assert "launch backend" in line


def test_doctor_probe_passes_when_every_state_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(
        monkeypatch,
        stdout=_envelope(team_states=[" Todo ", "In Progress", "In Review"]),
    )

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 0, output
    line = _probe_line(output)
    assert line.startswith(f"PASS {PROBE_CHECK}:")
    assert "ENG-123" in line
    assert "launch planner" in line
    assert "3 of 3 configured state names" in line
    assert output.strip().splitlines()[-1].startswith("Doctor PASS: 0 error(s)")


def test_doctor_probe_checks_blocked_state_only_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(monkeypatch, stdout=_envelope(team_states=CONFIGURED_STATES))
    exit_code, output = _run_probe(repo, capsys)
    assert exit_code == 0, output
    assert "3 of 3" in _probe_line(output)

    _add_config(repo, "  blocked_state: Blocked\n")
    exit_code, output = _run_probe(repo, capsys)
    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "`Blocked`" in line
    assert "`Todo`" not in line

    _install_runner(
        monkeypatch, stdout=_envelope(team_states=[*CONFIGURED_STATES, "Blocked"])
    )
    exit_code, output = _run_probe(repo, capsys)
    assert exit_code == 0, output
    assert "4 of 4" in _probe_line(output)


def test_doctor_probe_lists_only_missing_configured_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(
        monkeypatch, stdout=_envelope(team_states=["Todo", "Backlog", "Canceled"])
    )

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "`In Progress`" in line and "`In Review`" in line
    assert "`Todo`" not in line
    assert "Backlog" not in line and "Canceled" not in line
    assert output.strip().splitlines()[-1].startswith("Doctor FAIL: 1 error(s)")


def test_doctor_probe_fails_when_the_binary_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _stub_which(monkeypatch, {"fake-codex"})
    runner = _install_runner(monkeypatch, stdout=_envelope())

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "`fake-claude`" in line
    assert "could not start the probe" in line
    assert runner.calls == []


def test_doctor_probe_fails_on_launch_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(monkeypatch, raises=OSError("boom"))

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "`fake-claude`" in line
    assert "could not start the probe" in line
    assert "Traceback" not in output


def test_doctor_probe_fails_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(
        monkeypatch, stderr="Process timed out after 180 seconds.", exit_code=1
    )

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert "`fake-claude`" in line
    assert "could not start the probe" in line
    assert "timed out" in line


def test_doctor_probe_fails_on_nonzero_exit_with_escaped_truncated_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    long_line = "caf\u00e9  \u2603\t" + "x" * 400
    _install_runner(
        monkeypatch,
        stdout=_envelope(),
        stderr=f"\n   \n{long_line}\nsecond line\n",
        exit_code=3,
    )

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "exited with code 3" in line
    assert line.isascii()
    assert "caf\\xe9 \\u2603 x" in line
    # "cafe" (4) + space + snowman + space leaves 193 of the 200 characters.
    assert "x" * 193 in line and "x" * 194 not in line
    assert "second line" not in line


CONTROL_TEXT = "\x1b[2K\x1b[1Ared\x07bell\x08back\x7fdel\x00nul\u200eltr"
CONTROL_ESCAPED = (
    "\\x1b[2K\\x1b[1Ared\\x07bell\\x08back\\x7fdel\\x00nul\\u200eltr"
)


def _assert_no_control_bytes(line: str) -> None:
    assert line.isascii()
    assert all(0x20 <= ord(char) < 0x7F for char in line), repr(line)


def test_doctor_probe_escapes_control_bytes_in_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(monkeypatch, stderr=f"{CONTROL_TEXT}\n", exit_code=2)

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert "exited with code 2" in line
    assert CONTROL_ESCAPED in line
    _assert_no_control_bytes(line)
    assert "\x1b" not in output and "\x07" not in output


def test_doctor_probe_escapes_control_bytes_in_the_error_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(
        monkeypatch,
        stdout=_envelope(issue_found=False, team_states=[], error=CONTROL_TEXT),
    )

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert "could not read issue ENG-123" in line
    assert CONTROL_ESCAPED in line
    _assert_no_control_bytes(line)
    assert "\x1b" not in output and "\x08" not in output


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        json.dumps(["structured_output"]),
        _envelope(is_error=True),
        json.dumps({"is_error": False, "result": "done"}),
        _envelope(structured="text"),
        _envelope(structured={"issue_found": True, "team_states": []}),
        _envelope(
            structured={
                "issue_found": True,
                "team_states": CONFIGURED_STATES,
                "error": "",
                "extra": 1,
            }
        ),
        _envelope(
            structured={"issue_found": "yes", "team_states": [], "error": ""}
        ),
        _envelope(
            structured={"issue_found": True, "team_states": [1], "error": ""}
        ),
    ],
)
def test_doctor_probe_fails_without_a_usable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, stdout: str
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(monkeypatch, stdout=stdout)

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "returned no usable probe result" in line


def test_doctor_probe_fails_when_the_issue_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    reason = "get_issue was refused\n\u00fcber " + "y" * 300
    _install_runner(
        monkeypatch, stdout=_envelope(issue_found=False, team_states=[], error=reason)
    )

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 1, output
    line = _probe_line(output)
    assert line.startswith(f"FAIL {PROBE_CHECK}:")
    assert "could not read issue ENG-123" in line
    assert "get_issue was refused \\xfcber y" in line
    assert line.isascii()
    # The collapsed 27-character prefix leaves 173 of the 200 characters.
    assert "y" * 173 in line and "y" * 174 not in line


def test_doctor_probe_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _linear_repo(tmp_path, monkeypatch)
    _install_runner(monkeypatch, stdout=_envelope())
    before = sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts)

    exit_code, output = _run_probe(repo, capsys)

    assert exit_code == 0, output
    assert sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts) == before


# --- Interpreter grants ------------------------------------------------------

INTERP_CHECK = "interpreter grants"
SAFE_CLAUDE_TOOLS = '["Read", "Edit", "Bash(git status:*)", "Bash(git diff:*)"]'


def _interp_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if f" {INTERP_CHECK}:" in line]


def _write_agents(repo: Path, blocks: dict[str, str], extra: str = "") -> None:
    lines = ["agents:"]
    for role in ROLES:
        lines.append(f"  {role}:")
        lines.extend(f"    {line}" for line in blocks[role].strip().splitlines())
    (repo / "dispatch_config.yaml").write_text(
        "\n".join(lines) + "\n" + extra, encoding="utf-8"
    )


def _interp_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocks: dict[str, str],
    extra: str = "",
) -> Path:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None, commit=False)
    _write_agents(repo, blocks, extra)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _stub_which(monkeypatch, {"fake-agy", "fake-codex", "fake-claude"})
    return repo


def _codex_block() -> str:
    return "provider: codex\nbinary: fake-codex"


def _safe_claude_block() -> str:
    return f"provider: claude\nbinary: fake-claude\nallowed_tools: {SAFE_CLAUDE_TOOLS}"


INTERP_PASS_LINE = (
    f"PASS {INTERP_CHECK}: no role has an interpreter grant for `python`, "
    "`python3`, `pytest`, bare `Bash`, or `Bash(*)`; only these are "
    "checked, so review any other interpreter rule (for example "
    "`Bash(node:*)` or `Bash(sh:*)`) by hand"
)
INTERP_FLAG_CAUSE = (
    "; cause: interpreter_grants: true adds the python, python3, and pytest "
    "grants to the default lists"
)


def test_doctor_interpreter_grants_pass_on_the_default_claude_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _interp_lines(output) == [INTERP_PASS_LINE]


def test_doctor_interpreter_grants_warn_on_the_opt_in_default_claude_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None, commit=False)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "interpreter_grants: true\n",
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    claude_roles = [role for role in ROLES if NO_GEMINI[role] == "claude"]
    assert len(lines) == len(claude_roles)
    for role, line in zip(claude_roles, lines, strict=True):
        assert line == (
            f"WARN {INTERP_CHECK}: {role} (provider claude) allows "
            "`Bash(python:*)`, `Bash(python3:*)`, `Bash(pytest:*)`; an "
            "interpreter can run any command, including `git push`, past the "
            "allowlist, so the allowlist is a guardrail, not a sandbox"
            + INTERP_FLAG_CAUSE
        )
    assert not any(line.startswith(f"WARN {INTERP_CHECK}: backend") for line in lines)


def test_doctor_interpreter_grants_explicit_bash_warn_names_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["auditor"] = (
        'provider: claude\nbinary: fake-claude\nallowed_tools: ["Read", "Bash"]'
    )
    repo = _interp_repo(tmp_path, monkeypatch, blocks, "interpreter_grants: true\n")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(
        f"WARN {INTERP_CHECK}: auditor (provider claude) allows `Bash`; "
    )
    assert "interpreter_grants" not in lines[0]


def test_doctor_interpreter_grants_explicit_python_rule_names_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # An explicit allowed_tools list is unaffected by the flag, so turning the
    # flag off would not remove the grant; the doctor must not blame it.
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["auditor"] = (
        "provider: claude\nbinary: fake-claude\n"
        'allowed_tools: ["Read", "Bash(python:*)"]'
    )
    repo = _interp_repo(tmp_path, monkeypatch, blocks, "interpreter_grants: true\n")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert lines == [
        f"WARN {INTERP_CHECK}: auditor (provider claude) allows "
        "`Bash(python:*)`; an interpreter can run any command, including "
        "`git push`, past the allowlist, so the allowlist is a guardrail, not "
        "a sandbox"
    ]


def test_doctor_interpreter_grants_linear_default_list_names_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["auditor"] = "provider: claude\nbinary: fake-claude"
    repo = _interp_repo(
        tmp_path, monkeypatch, blocks, LINEAR_TRACKER + "interpreter_grants: true\n"
    )

    _exit_code, output = _run(repo, capsys)

    lines = _interp_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(
        f"WARN {INTERP_CHECK}: auditor (provider claude) allows "
        "`Bash(python:*)`, `Bash(python3:*)`, `Bash(pytest:*)`; "
    )
    assert lines[0].endswith(INTERP_FLAG_CAUSE)


def test_doctor_interpreter_grants_linear_explicit_list_names_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["auditor"] = (
        "provider: claude\nbinary: fake-claude\n"
        'allowed_tools: ["Read", "Bash(python:*)"]'
    )
    repo = _interp_repo(
        tmp_path, monkeypatch, blocks, LINEAR_TRACKER + "interpreter_grants: true\n"
    )

    _exit_code, output = _run(repo, capsys)

    lines = _interp_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(
        f"WARN {INTERP_CHECK}: auditor (provider claude) allows `Bash(python:*)`; "
    )
    assert "interpreter_grants" not in lines[0]


def test_doctor_interpreter_grants_pass_when_claude_drops_interpreters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    repo = _interp_repo(tmp_path, monkeypatch, blocks)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _interp_lines(output) == [
        f"PASS {INTERP_CHECK}: no role has an interpreter grant for `python`, "
        "`python3`, `pytest`, bare `Bash`, or `Bash(*)`; only these are "
        "checked, so review any other interpreter rule (for example "
        "`Bash(node:*)` or `Bash(sh:*)`) by hand"
    ]


def test_doctor_interpreter_grants_pass_names_only_the_checked_interpreters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["auditor"] = (
        "provider: claude\nbinary: fake-claude\n"
        'allowed_tools: ["Read", "Bash(node:*)", "Bash(sh:*)"]'
    )
    repo = _interp_repo(tmp_path, monkeypatch, blocks)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(f"PASS {INTERP_CHECK}: ")
    assert "only these are checked" in lines[0]
    assert "by hand" in lines[0]
    assert lines[0] != f"PASS {INTERP_CHECK}: no role has an interpreter grant"


def test_doctor_interpreter_grants_warn_on_bare_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["auditor"] = (
        'provider: claude\nbinary: fake-claude\nallowed_tools: ["Read", "Bash"]'
    )
    blocks["validator"] = (
        "provider: claude\nbinary: fake-claude\n"
        'allowed_tools: ["Read", "Bash(*)", "Bash(git log:*)", "Bash(python)"]'
    )
    repo = _interp_repo(tmp_path, monkeypatch, blocks)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert len(lines) == 2
    assert lines[0].startswith(
        f"WARN {INTERP_CHECK}: auditor (provider claude) allows `Bash`; "
    )
    assert lines[1].startswith(
        f"WARN {INTERP_CHECK}: validator (provider claude) allows `Bash(*)`, "
        "`Bash(python)`; "
    )
    assert "PASS" not in " ".join(lines)


def test_doctor_interpreter_grants_label_claude_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = (
        "provider: codex\nbinary: fake-codex\n"
        "fallback:\n  provider: claude\n  binary: fake-claude"
    )
    repo = _interp_repo(tmp_path, monkeypatch, blocks, "interpreter_grants: true\n")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(
        f"WARN {INTERP_CHECK}: backend fallback (provider claude) allows "
        "`Bash(python:*)`, `Bash(python3:*)`, `Bash(pytest:*)`; "
    )
    assert lines[0].endswith(INTERP_FLAG_CAUSE)


def test_doctor_interpreter_grants_pass_on_default_claude_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = (
        "provider: codex\nbinary: fake-codex\n"
        "fallback:\n  provider: claude\n  binary: fake-claude"
    )
    repo = _interp_repo(tmp_path, monkeypatch, blocks)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _interp_lines(output) == [INTERP_PASS_LINE]


def test_doctor_interpreter_grants_pass_on_antigravity_roles_with_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["planner"] = "provider: antigravity\nbinary: fake-agy"
    repo = _interp_repo(tmp_path, monkeypatch, blocks)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _interp_lines(output) == [INTERP_PASS_LINE]


def test_doctor_interpreter_grants_warn_on_antigravity_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["planner"] = "provider: antigravity\nbinary: fake-agy"
    repo = _interp_repo(tmp_path, monkeypatch, blocks, "interpreter_grants: true\n")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(f"WARN {INTERP_CHECK}: planner (provider antigravity) ")
    expected = [
        "`command(regex:^python( .*)?$)`",
        "`command(regex:^python3( .*)?$)`",
        "`command(regex:^pytest( .*)?$)`",
    ]
    for entry in expected:
        assert entry in line
    assert line.index(expected[0]) < line.index(expected[1]) < line.index(expected[2])
    assert "git status" not in line
    assert "including `git push`" in line
    assert "guardrail, not a sandbox" in line
    assert "user-wide" in line
    assert line.endswith(INTERP_FLAG_CAUSE)


@pytest.mark.parametrize("opt_in", [False, True])
def test_doctor_interpreter_grants_name_antigravity_verification_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, opt_in: bool
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = (
        "provider: codex\nbinary: fake-codex\n"
        "fallback:\n  provider: antigravity\n  binary: fake-agy"
    )
    repo = _interp_repo(
        tmp_path,
        monkeypatch,
        blocks,
        extra='verification_allowed_prefixes: ["python -m pytest", "npm test"]\n'
        + ("interpreter_grants: true\n" if opt_in else ""),
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    if not opt_in:
        # The python prefix gets no agy grant with the flag off.
        assert lines == [INTERP_PASS_LINE]
        return
    assert len(lines) == 1
    assert lines[0].startswith(
        f"WARN {INTERP_CHECK}: backend fallback (provider antigravity) "
    )
    assert "`command(regex:^python -m pytest( .*)?$)`" in lines[0]
    assert "npm test" not in lines[0]
    assert lines[0].endswith(INTERP_FLAG_CAUSE)


AGY_INTERP_ENTRIES = (
    _grant("python"),
    _grant("python3"),
    _grant("pytest"),
    _grant("python -m pytest"),
)
AGY_INTERP_TAIL = (
    "; an interpreter can run any command, including `git push`, past the "
    "allowlist, so the allowlist is a guardrail, not a sandbox, and these "
    "grants are user-wide, so they apply to every agy run for this user"
    + INTERP_FLAG_CAUSE
)
AGY_SEE_CHECK = "see the `antigravity permissions` check"


def _listed(entries: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"`{entry}`" for entry in entries)


def _agy_interp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Planner on agy, backend with an agy fallback, flag on, python prefix."""
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["planner"] = "provider: antigravity\nbinary: fake-agy"
    blocks["backend"] = (
        "provider: codex\nbinary: fake-codex\n"
        "fallback:\n  provider: antigravity\n  binary: fake-agy"
    )
    return _interp_repo(
        tmp_path,
        monkeypatch,
        blocks,
        extra='verification_allowed_prefixes: ["python -m pytest", "npm test"]\n'
        "interpreter_grants: true\n",
    )


def _agy_interp_lines(body: str) -> list[str]:
    return [
        f"WARN {INTERP_CHECK}: {label} (provider antigravity) {body}"
        + AGY_INTERP_TAIL
        for label in ("planner", "backend fallback")
    ]


def test_doctor_interpreter_grants_agy_all_present_says_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_interp_repo(tmp_path, monkeypatch)
    # Listed out of order on purpose: the warning keeps the generated order.
    _write_agy_settings(
        tmp_path, _complete_agy_settings(tuple(reversed(AGY_INTERP_ENTRIES)))
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert lines == _agy_interp_lines(
        f"uses the agy allow entries {_listed(AGY_INTERP_ENTRIES)}"
    )
    assert "needs" not in " ".join(lines)
    assert "missing" not in " ".join(lines)


def test_doctor_interpreter_grants_agy_partial_names_only_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_interp_repo(tmp_path, monkeypatch)
    present = (AGY_INTERP_ENTRIES[0], AGY_INTERP_ENTRIES[3])
    missing = (AGY_INTERP_ENTRIES[1], AGY_INTERP_ENTRIES[2])
    _write_agy_settings(tmp_path, _complete_agy_settings(present))

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _interp_lines(output) == _agy_interp_lines(
        f"uses the agy allow entries {_listed(present)} and needs the agy "
        f"allow entries {_listed(missing)}, which "
        f"{doctor.ANTIGRAVITY_SETTINGS_PATH} permissions.allow is missing "
        f"({AGY_SEE_CHECK})",
    )


def test_doctor_interpreter_grants_agy_none_present_names_all_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_interp_repo(tmp_path, monkeypatch)
    _write_agy_settings(tmp_path, _complete_agy_settings())

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert lines == _agy_interp_lines(
        f"needs the agy allow entries {_listed(AGY_INTERP_ENTRIES)}, which "
        f"{doctor.ANTIGRAVITY_SETTINGS_PATH} permissions.allow is missing "
        f"({AGY_SEE_CHECK})",
    )
    assert "uses the agy" not in " ".join(lines)


def test_doctor_interpreter_grants_agy_absent_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_interp_repo(tmp_path, monkeypatch)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert lines == _agy_interp_lines(
        f"needs the agy allow entries {_listed(AGY_INTERP_ENTRIES)}; "
        f"{doctor.ANTIGRAVITY_SETTINGS_PATH} was not found, so none is granted "
        f"yet ({AGY_SEE_CHECK})",
    )
    assert not _agy_settings_path(tmp_path).exists()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        [1, 2],
        {"permissions": []},
        {"permissions": {"allow": "command(regex:^python( .*)?$)"}},
        {"permissions": {"allow": [1]}},
    ],
    ids=["malformed", "not-object", "no-permissions", "allow-string", "allow-ints"],
)
def test_doctor_interpreter_grants_agy_bad_settings_claim_no_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, content: object
) -> None:
    repo = _agy_interp_repo(tmp_path, monkeypatch)
    path = _write_agy_settings(tmp_path, content)
    before = path.read_bytes()

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert lines == _agy_interp_lines(
        f"needs the agy allow entries {_listed(AGY_INTERP_ENTRIES)}; "
        f"{doctor.ANTIGRAVITY_SETTINGS_PATH} could not be read as settings, so "
        f"the doctor did not check them ({AGY_SEE_CHECK} for details)",
    )
    assert "uses the agy" not in " ".join(lines)
    assert path.read_bytes() == before


def test_doctor_interpreter_grants_agy_unreadable_settings_claim_no_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_interp_repo(tmp_path, monkeypatch)
    # A directory at the settings path raises an OSError other than
    # FileNotFoundError on read, which the reader reports as unreadable.
    _agy_settings_path(tmp_path).mkdir(parents=True)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _interp_lines(output)
    assert lines == _agy_interp_lines(
        f"needs the agy allow entries {_listed(AGY_INTERP_ENTRIES)}; "
        f"{doctor.ANTIGRAVITY_SETTINGS_PATH} could not be read as settings, so "
        f"the doctor did not check them ({AGY_SEE_CHECK} for details)",
    )
    agy_lines = _agy_lines(output)
    assert agy_lines and agy_lines[0].startswith(
        f"WARN {AGY_CHECK}: {doctor.ANTIGRAVITY_SETTINGS_PATH} could not be read ("
    )


def test_doctor_interpreter_grants_agy_unsafe_bypass_skips_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = _codex_block()
    blocks["planner"] = "provider: antigravity\nbinary: fake-agy\nunsafe_bypass: true"
    repo = _interp_repo(tmp_path, monkeypatch, blocks, "interpreter_grants: true\n")
    _write_agy_settings(tmp_path, "{not json")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _interp_lines(output) == [
        f"WARN {INTERP_CHECK}: planner (provider antigravity) sets "
        "unsafe_bypass: true, so the launch has no shell allowlist at all"
    ]


def test_doctor_interpreter_grants_warn_on_unsafe_bypass_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _safe_claude_block() for role in ROLES}
    blocks["backend"] = "provider: codex\nbinary: fake-codex\nunsafe_bypass: true"
    blocks["auditor"] = "provider: claude\nbinary: fake-claude\nunsafe_bypass: true"
    repo = _interp_repo(tmp_path, monkeypatch, blocks)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _interp_lines(output) == [
        f"WARN {INTERP_CHECK}: backend (provider codex) sets unsafe_bypass: "
        "true, so the launch has no shell allowlist at all",
        f"WARN {INTERP_CHECK}: auditor (provider claude) sets unsafe_bypass: "
        "true, so the launch has no shell allowlist at all",
    ]


def test_doctor_interpreter_grants_absent_for_codex_only_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {role: _codex_block() for role in ROLES}
    repo = _interp_repo(tmp_path, monkeypatch, blocks)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert INTERP_CHECK not in output


def test_doctor_interpreter_grant_warnings_keep_exit_code_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    blocks = {
        role: "provider: claude\nbinary: fake-claude" for role in ROLES
    }
    blocks["backend"] = "provider: codex\nbinary: fake-codex\nunsafe_bypass: true"
    blocks["planner"] = "provider: antigravity\nbinary: fake-agy"
    repo = _interp_repo(tmp_path, monkeypatch, blocks, "interpreter_grants: true\n")

    report = doctor.run_doctor(repo_root=repo)

    interp = [check for check in report.checks if check.name == INTERP_CHECK]
    assert len(interp) == len(ROLES)
    assert {check.status for check in interp} == {"WARN"}
    assert [check for check in report.checks if check.status == "FAIL"] == []
    assert report.exit_code == 0


# ---------------------------------------------------------------------------
# Verification preflight (check_verification_preflight)
# ---------------------------------------------------------------------------

PREFLIGHT = doctor.VERIFICATION_PREFLIGHT_CHECK_NAME
CLAUDE_BLOCK = "provider: claude\nbinary: fake-claude"


def _preflight_handoff(
    commands: tuple[str, ...], next_agent: str = "backend"
) -> str:
    command_lines = "".join(f"  - {command}\n" for command in commands)
    return (
        "---\n"
        f"next_agent: {next_agent}\n"
        'reason: "Planner assigns the story."\n'
        'epic_id: "E1"\n'
        'story_id: "S1"\n'
        'story_title: "First story"\n'
        "producer: planner\n"
        "---\n\n"
        "# Handoff: S1 First story\n\n"
        "## Work Packet\n\n"
        "- **Objective:** Add a small feature.\n"
        "- **Files in scope:** `src/app.py`\n"
        "- **Files out of bounds:** none\n"
        "- **Context:** None.\n"
        "- **Verification command:**\n"
        f"{command_lines}"
        "- **Expected next route:** auditor\n"
    )


def _preflight_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend_block: str,
    *,
    extra: str = "",
    available: tuple[str, ...] = ("python", "git"),
) -> config_module.DispatchConfig:
    repo = tmp_path / "preflight"
    repo.mkdir(exist_ok=True)
    blocks = {role: CLAUDE_BLOCK for role in ROLES}
    blocks["backend"] = backend_block
    _write_agents(repo, blocks, extra)
    _stub_which(
        monkeypatch, {"fake-claude", "fake-codex", "fake-agy", *available}
    )
    return config_module.load_dispatch_config(repo_root=repo)


def _preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend_block: str,
    commands: tuple[str, ...] = ("python -m pytest tests -q",),
    *,
    extra: str = "",
    available: tuple[str, ...] = ("python", "git"),
    next_agent: str = "backend",
) -> list[doctor.DoctorCheck]:
    config = _preflight_config(
        tmp_path, monkeypatch, backend_block, extra=extra, available=available
    )
    checks = doctor.check_verification_preflight(
        config, _preflight_handoff(commands, next_agent)
    )
    assert all(check.name == PREFLIGHT for check in checks)
    assert "FAIL" not in {check.status for check in checks}
    return checks


def _warns(checks: list[doctor.DoctorCheck]) -> list[str]:
    return [check.message for check in checks if check.status == "WARN"]


def test_preflight_warns_when_default_claude_list_denies_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = _preflight(tmp_path, monkeypatch, CLAUDE_BLOCK)

    warns = _warns(checks)
    assert len(warns) == 1, warns
    assert warns[0].startswith(
        "backend (claude) cannot run `python` (`python -m pytest tests -q`): "
        "denied provider grant"
    )
    assert "set interpreter_grants: true or an allowed_tools rule" in warns[0]
    assert doctor.DoctorCheck("WARN", PREFLIGHT, warns[0]).format().startswith(
        "WARN verification preflight: backend (claude) cannot run `python`"
    )


def test_preflight_passes_default_claude_list_with_interpreter_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = _preflight(
        tmp_path, monkeypatch, CLAUDE_BLOCK, extra="interpreter_grants: true\n"
    )

    assert [check.status for check in checks] == ["PASS"]
    assert checks[0].message.startswith(
        "backend (claude) can run the 1 Verification command(s); static check"
    )


def test_preflight_explicit_exact_claude_rule_covers_only_that_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = (
        CLAUDE_BLOCK + '\nallowed_tools: ["Read", "Bash(python -m pytest tests -q)"]'
    )
    checks = _preflight(
        tmp_path,
        monkeypatch,
        block,
        ("python -m pytest tests -q", "python -m pytest tests/test_doctor.py -q"),
        extra="interpreter_grants: true\n",
    )

    warns = _warns(checks)
    assert len(warns) == 1, warns
    assert "`python -m pytest tests/test_doctor.py -q`" in warns[0]
    assert "denied provider grant" in warns[0]
    assert "`Bash(python -m pytest tests -q)` do not cover this command" in warns[0]
    assert "to this role's allowed_tools" in warns[0]
    # The flag is on, yet it does not change an explicit list, and the remedy
    # does not suggest setting it.
    assert "interpreter_grants does not change an explicit allowed_tools" in warns[0]
    assert "set interpreter_grants: true" not in warns[0]


def test_preflight_explicit_prefix_claude_rule_respects_word_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = CLAUDE_BLOCK + '\nallowed_tools: ["Bash(git diff:*)"]'
    checks = _preflight(
        tmp_path,
        monkeypatch,
        block,
        ("git diff --check", "git diff", "git diffx", "git status"),
    )

    warns = _warns(checks)
    assert len(warns) == 2, warns
    assert "(`git diffx`)" in warns[0]
    assert "(`git status`)" in warns[1]
    assert all("denied provider grant" in warn for warn in warns)


def test_preflight_claude_remedy_escapes_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = _preflight(
        tmp_path,
        monkeypatch,
        CLAUDE_BLOCK,
        ("python -m pytest \x1b[2Ktests -q", "npm test \x07--watch"),
        available=("python", "npm"),
    )

    warns = _warns(checks)
    assert len(warns) == 2, warns
    output = "\n".join(warns)
    assert "\x1b" not in output and "\x07" not in output
    assert "`Bash(python -m pytest \\x1b[2Ktests -q)`" in warns[0]
    assert "`Bash(npm test \\x07--watch)`" in warns[1]


def test_claude_denial_remedy_escapes_the_executable() -> None:
    agent_config = config_module.AgentConfig(provider="claude", binary="claude")
    for tools in ((), ("Bash(to\x1bol:*)",)):
        remedy = doctor._claude_denial_remedy(
            agent_config, "to\x1bol --run", "to\x1bol", tools
        )
        assert "\x1b" not in remedy
        assert "`Bash(to\\x1bol:*)`" in remedy
        assert "`Bash(to\\x1bol --run)`" in remedy


@pytest.mark.parametrize(
    ("rule", "command", "allowed"),
    [
        ("Bash", "anything at all", True),
        ("Bash(*)", "rm -rf build", True),
        ("Bash(python:*)", "python -m pytest", True),
        ("Bash(python:*)", "python", True),
        ("Bash(python:*)", "python3 -m pytest", False),
        ("Bash(python *)", "python -c 1", True),
        ("Bash(npm test)", "npm test", True),
        ("Bash(npm test)", "npm test --watch", False),
        ("Bash(git * main)", "git push origin main", True),
        ("Bash(git * main)", "git push origin dev", False),
        ("Read", "python", False),
    ],
)
def test_preflight_claude_rule_matching(rule: str, command: str, allowed: bool) -> None:
    assert doctor._claude_bash_rule_allows(rule, command) is allowed


def test_preflight_antigravity_needs_interpreter_grants_for_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = "provider: antigravity\nbinary: fake-agy"
    checks = _preflight(
        tmp_path,
        monkeypatch,
        block,
        ("python -m pytest tests -q", "git diff --check"),
        extra='verification_allowed_prefixes: ["python -m pytest", "git diff"]\n',
    )

    warns = _warns(checks)
    assert len(warns) == 1, warns
    assert warns[0].startswith("backend (antigravity) cannot run `python`")
    assert "denied provider grant" in warns[0]
    assert "set interpreter_grants: true" in warns[0]


def test_preflight_antigravity_passes_with_interpreter_grants_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = "provider: antigravity\nbinary: fake-agy"
    checks = _preflight(
        tmp_path,
        monkeypatch,
        block,
        ("python -m pytest tests -q", "git diff --check"),
        extra=(
            "interpreter_grants: true\n"
            'verification_allowed_prefixes: ["python -m pytest", "git diff"]\n'
        ),
    )

    assert [check.status for check in checks] == ["PASS"], checks


def test_preflight_antigravity_names_the_missing_grant_and_the_push_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = "provider: antigravity\nbinary: fake-agy"
    checks = _preflight(
        tmp_path,
        monkeypatch,
        block,
        ("make test", "git push origin main"),
        available=("make", "git"),
    )

    warns = _warns(checks)
    assert len(warns) == 2, warns
    assert warns[0].startswith("backend (antigravity) cannot run `make`")
    assert "add a verification_allowed_prefixes entry" in warns[0]
    assert agy_perms.antigravity_command_grant("make test") in warns[0]
    assert "required permissions.deny entry" in warns[1]


def test_preflight_codex_checks_only_executable_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = "provider: codex\nbinary: fake-codex"
    present = _preflight(
        tmp_path, monkeypatch, block, ("python -m pytest tests -q", "make test"),
        available=("python", "make"),
    )
    assert [check.status for check in present] == ["PASS"]
    assert present[0].message.startswith("backend (codex) can run the 2")

    missing = _preflight(
        tmp_path, monkeypatch, block, ("python -m pytest tests -q", "make test"),
        available=("python",),
    )
    warns = _warns(missing)
    assert len(warns) == 1, warns
    assert warns[0].startswith(
        "backend (codex) cannot run `make` (`make test`): missing executable"
    )
    assert "denied provider grant" not in warns[0]


def test_preflight_labels_primary_and_fallback_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = (
        CLAUDE_BLOCK
        + "\nfallback:\n  provider: antigravity\n  binary: fake-agy"
    )
    checks = _preflight(tmp_path, monkeypatch, block)

    warns = _warns(checks)
    assert len(warns) == 2, warns
    assert warns[0].startswith("backend (claude) cannot run `python`")
    assert warns[1].startswith("backend fallback (antigravity) cannot run `python`")


def test_preflight_checks_only_the_routed_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # frontend (claude, default list) would be denied python; backend (codex)
    # is the routed role and can run it.
    checks = _preflight(tmp_path, monkeypatch, "provider: codex\nbinary: fake-codex")
    assert [check.status for check in checks] == ["PASS"]

    frontend = _preflight(
        tmp_path,
        monkeypatch,
        "provider: codex\nbinary: fake-codex",
        next_agent="frontend",
    )
    warns = _warns(frontend)
    assert len(warns) == 1 and warns[0].startswith("frontend (claude)"), warns


def test_preflight_warns_on_gate_prefix_rejection_only_with_the_gate_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = "provider: codex\nbinary: fake-codex"
    prefixes = 'verification_allowed_prefixes: ["python -m pytest"]\n'
    checks = _preflight(
        tmp_path, monkeypatch, block, ("python -m pytest -q", "git diff --check"),
        extra=prefixes,
    )

    warns = _warns(checks)
    assert len(warns) == 1, warns
    assert warns[0].startswith(
        "backend Verification command rejected by gate prefix: "
        "Verification command `git diff --check` does not start with any"
    )
    assert "add a matching verification_allowed_prefixes entry" in warns[0]

    gate_off = _preflight(
        tmp_path, monkeypatch, block, ("python -m pytest -q", "git diff --check"),
        extra=prefixes
        + "enforce_work_packet: false\n"
        + 'work_packet_opt_out_reason: "Testing the gate-off path."\n',
    )
    assert [check.status for check in gate_off] == ["PASS"]


def test_preflight_unsafe_bypass_skips_grants_but_not_executables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = CLAUDE_BLOCK + "\nunsafe_bypass: true"
    checks = _preflight(tmp_path, monkeypatch, block)
    assert [check.status for check in checks] == ["PASS"]

    missing = _preflight(tmp_path, monkeypatch, block, available=())
    warns = _warns(missing)
    assert len(warns) == 1, warns
    assert "missing executable" in warns[0]


@pytest.mark.parametrize("next_agent", ["planner", "auditor", "user"])
def test_preflight_skips_non_implementer_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, next_agent: str
) -> None:
    checks = _preflight(tmp_path, monkeypatch, CLAUDE_BLOCK, next_agent=next_agent)
    assert checks == []


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not a handoff at all",
        "---\nnext_agent: [\n---\n",
        HANDOFF_WITHOUT_WORK_PACKET,
        _preflight_handoff((), "backend"),
    ],
)
def test_preflight_ignores_missing_or_malformed_work_packets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    config = _preflight_config(tmp_path, monkeypatch, CLAUDE_BLOCK)
    assert doctor.check_verification_preflight(config, content) == []


def test_preflight_warns_when_a_command_has_no_plain_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = _preflight(
        tmp_path,
        monkeypatch,
        "provider: codex\nbinary: fake-codex",
        ("X=1 python -m pytest",),
    )
    warns = _warns(checks)
    assert len(warns) == 1, warns
    assert "does not start with a plain program name" in warns[0]


def test_doctor_preflight_warnings_keep_exit_code_and_run_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    marker = tmp_path / "verification-ran.txt"
    command = f"python -c \"open(r'{marker}', 'w').close()\""
    repo = _make_repo(
        tmp_path, NO_GEMINI, handoff=_preflight_handoff((command, "make test"))
    )
    config_before = (repo / "dispatch_config.yaml").read_bytes()
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff_before = handoff_path.read_bytes()
    _stub_which(monkeypatch, {"fake-codex", "fake-claude", "python"})
    real_run = subprocess.run
    calls: list[object] = []

    def recording_run(args: object, *rest: object, **kwargs: object) -> object:
        calls.append(args)
        return real_run(args, *rest, **kwargs)

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"doctor ran a command: {args!r}")

    monkeypatch.setattr(subprocess, "run", recording_run)
    monkeypatch.setattr("llm_handoff.work_claim.run_shell_command", refuse)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    preflight = [line for line in output.splitlines() if PREFLIGHT in line]
    assert preflight == [
        "WARN verification preflight: backend (codex) cannot run `make` "
        "(`make test`): missing executable, `make` was not found on PATH; "
        "install it or change the Verification command"
    ], output
    assert not marker.exists()
    for args in calls:
        assert isinstance(args, list) and args[0] == "git", args
    assert (repo / "dispatch_config.yaml").read_bytes() == config_before
    assert handoff_path.read_bytes() == handoff_before
