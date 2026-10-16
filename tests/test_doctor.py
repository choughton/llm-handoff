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
    monkeypatch.delenv("GEMINI_SANDBOX", raising=False)
    # The default claude normalizer is runnable with an Anthropic key; tests
    # of the normalizer check clear it and choose their own provider inputs.
    for name in NORMALIZER_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "doctor-test-key")


def _run(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    exit_code = main_module.main(["doctor", "--repo-root", str(repo)])
    return exit_code, capsys.readouterr().out


MIXED = {
    "planner": "gemini",
    "backend": "codex",
    "frontend": "gemini",
    "auditor": "claude",
    "validator": "claude",
    "finalizer": "claude",
}
NO_GEMINI = {role: ("codex" if role == "backend" else "claude") for role in ROLES}


def test_doctor_happy_path_passes_every_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, MIXED)
    _stub_which(monkeypatch, {"fake-gemini", "fake-codex", "fake-claude", "docker"})

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
        "PASS container runtime:",
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


def test_doctor_skips_container_runtime_without_gemini_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI)
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "container runtime" not in output


def test_doctor_requires_container_runtime_for_gemini_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, MIXED)
    _stub_which(monkeypatch, {"fake-gemini", "fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 1
    runtime_lines = [
        line for line in output.splitlines() if line.startswith("FAIL container runtime:")
    ]
    assert len(runtime_lines) == 2
    for line in runtime_lines:
        assert "Docker" in line
        assert "Podman" in line
        assert "sandbox-exec" in line
    assert "agents.planner.unsafe_bypass: true" in output
    assert "agents.frontend.unsafe_bypass: true" in output


def test_doctor_container_runtime_skips_unsafe_bypass_gemini_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, commit=False)
    config_path = repo / "dispatch_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  planner:\n    provider: claude\n    binary: fake-claude\n",
            "  planner:\n    provider: gemini\n    binary: fake-gemini\n"
            "    unsafe_bypass: true\n",
        ),
        encoding="utf-8",
    )
    _stub_which(monkeypatch, {"fake-gemini", "fake-codex", "fake-claude"})

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "PASS role planner: provider gemini" in output
    assert "container runtime" not in output


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


def test_doctor_warns_on_dirty_tree_and_falls_back_to_default_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, handoff=None, commit=False)
    (repo / "dispatch_config.yaml").unlink()
    (repo / "notes.txt").write_text("dirty\n", encoding="utf-8")
    _stub_which(
        monkeypatch,
        {
            doctor._command_binary_name("codex"),
            doctor._command_binary_name("claude"),
            doctor._command_binary_name("gemini"),
            "podman",
        },
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert "PASS config: no dispatch_config.yaml found" in output
    assert "WARN git working tree: working tree is dirty" in output
    assert "PASS container runtime: Gemini CLI sandbox runtime `podman`" in output


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


def test_doctor_passes_gemini_normalizer_with_google_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _make_repo(tmp_path, NO_GEMINI, normalizer_provider="gemini")
    _stub_which(monkeypatch, {"fake-codex", "fake-claude"})
    monkeypatch.setenv("GOOGLE_API_KEY", "doctor-test-key")

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    assert _normalizer_lines(output) == [
        "PASS normalizer: provider gemini can use GOOGLE_API_KEY (auth not verified)"
    ]


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
    monkeypatch.delenv("GEMINI_SANDBOX", raising=False)
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
    _stub_which(monkeypatch, {"fake-gemini", "fake-codex", "fake-claude", "docker"})
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


@pytest.mark.parametrize(
    "grant", ["command(git push)", "command(git push origin main)", "unsandboxed(git push)"]
)
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
    allow.remove("command(git diff)")
    allow.append("command(regex:^npm test$)")
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(f"WARN {AGY_CHECK}:")
    assert "permissions.allow is missing command(git diff), command(npm test)" in line
    assert "regex grants" in line
    assert "(covers planner, backend fallback)" in line


def test_doctor_antigravity_warns_on_missing_deny_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _agy_repo(tmp_path, monkeypatch, backend_fallback=False)
    settings = _complete_agy_settings()
    settings["permissions"]["deny"] = ["command(git push)"]  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert lines == [
        f"WARN {AGY_CHECK}: ~/.gemini/antigravity-cli/settings.json "
        "permissions.deny is missing unsandboxed(git push) (covers planner)"
    ]


@pytest.mark.parametrize(
    ("rule", "shadowed"),
    [
        ("command(*)", "command(git status), command(git diff)"),
        ("command(git)", "command(git status), command(git diff)"),
        ("command(git *)", "command(git status), command(git diff)"),
        ("command(regex:^py)", "command(python), command(python3), command(pytest)"),
        ("command(git status)", "command(git status)"),
        ("command(npm test)", "command(npm test)"),
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
    settings = _complete_agy_settings(extra_allow=("command(npm test)",))
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
    settings["permissions"]["deny"].append("command(pytest)")  # type: ignore[index]
    _write_agy_settings(tmp_path, settings)

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1, output
    assert lines[0].startswith(f"WARN {AGY_CHECK}:")
    assert "permissions.deny has rules that override required allow entries" in lines[0]
    assert "`command(pytest)` shadows command(pytest)" in lines[0]


@pytest.mark.parametrize(
    "ask",
    [
        ["command(npm publish)", "command(regex:^curl)", "unsandboxed(*)"],
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
        tmp_path, _complete_agy_settings(extra_allow=("command(npm test)",))
    )

    exit_code, output = _run(repo, capsys)

    assert exit_code == 0, output
    lines = _agy_lines(output)
    assert len(lines) == 1
    assert lines[0].startswith(f"PASS {AGY_CHECK}:")
    assert "(covers planner, backend fallback)" in lines[0]


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
