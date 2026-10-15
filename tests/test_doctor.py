from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from llm_handoff import __main__ as main_module
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
