"""Scanner executable lookups never search the current directory.

These tests use the real filesystem and no ``shutil.which`` stub.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from llm_handoff.config import ScannerConfig
from llm_handoff.work_claim import (
    CommandRun,
    ScannerContext,
    bind_command_executable,
    find_scanner_executable,
    run_scanner,
    run_shell_command,
    shell_quote,
)


def _plant_fake_scanner(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        planted = directory / "fakescan.bat"
        planted.write_text("@exit /b 0\r\n", encoding="utf-8", newline="")
    else:
        planted = directory / "fakescan"
        planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        planted.chmod(0o755)
    return planted


@pytest.fixture
def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    planted_file = _plant_fake_scanner(tmp_path / "cwd")
    monkeypatch.chdir(planted_file.parent)
    return planted_file


def _refusing_runner(command: str, **_kwargs: Any) -> CommandRun:
    raise AssertionError(f"the gate ran a missing scanner: {command}")


def _context(tmp_path: Path) -> ScannerContext:
    return ScannerContext(
        repo_root=tmp_path,
        diff_range="aaa..bbb",
        changed_files=("src/app.py",),
        changed_py_files=("src/app.py",),
        changed_js_files=(),
    )


def test_helper_ignores_executable_only_in_current_directory(
    tmp_path: Path, planted: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()

    assert find_scanner_executable("fakescan", path=str(other)) is None


def test_helper_finds_executable_in_absolute_path_entry(
    tmp_path: Path, planted: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    search = str(other) + os.pathsep + str(planted.parent)

    found = find_scanner_executable("fakescan", path=search)

    assert found is not None
    assert Path(found) == planted


def test_helper_skips_dot_relative_and_empty_path_entries(planted: Path) -> None:
    search = os.pathsep.join([".", "", "bin", os.curdir])

    assert find_scanner_executable("fakescan", path=search) is None


def test_helper_defaults_to_the_process_path(
    tmp_path: Path, planted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("PATH", str(other))

    assert find_scanner_executable("fakescan") is None

    monkeypatch.setenv("PATH", str(planted.parent))

    assert find_scanner_executable("fakescan") is not None


def test_helper_accepts_absolute_path_and_rejects_relative_path(
    planted: Path,
) -> None:
    assert find_scanner_executable(str(planted), path="") == str(planted)
    if os.name == "nt":
        # An absolute path without the extension gets PATHEXT appended.
        bare = str(planted.with_suffix(""))
        found = find_scanner_executable(bare, path="")
        assert found is not None
        assert os.path.normcase(found) == os.path.normcase(str(planted))
    assert find_scanner_executable("./fakescan", path=str(planted.parent)) is None
    assert find_scanner_executable(f"./{planted.name}", path="") is None


def test_helper_has_no_cmd_stem_fallback(tmp_path: Path) -> None:
    # Looking up `bandit.cmd` never falls back to a plain `bandit`.
    plain = tmp_path / "plain"
    plain.mkdir()
    program = plain / "bandit"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)

    assert find_scanner_executable("bandit.cmd", path=str(plain)) is None


def test_gate_reports_scanner_only_in_current_directory_missing(
    tmp_path: Path, planted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("PATH", str(other) + os.pathsep + os.environ.get("PATH", ""))

    scanner_run, error = run_scanner(
        ScannerConfig(name="fakescan", command="fakescan {changed_files}"),
        _context(tmp_path),
        runner=_refusing_runner,
    )

    assert error is not None
    assert error.startswith("scanner_missing:fakescan")
    assert scanner_run.missing is True
    assert scanner_run.run is None


def test_gate_runs_scanner_on_an_absolute_path_entry(
    tmp_path: Path, planted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "PATH", str(planted.parent) + os.pathsep + os.environ.get("PATH", "")
    )
    calls: list[str] = []

    def recording_runner(command: str, **kwargs: Any) -> CommandRun:
        calls.append(command)
        return CommandRun(
            command=command,
            exit_code=0,
            elapsed_seconds=0.0,
            output_tail="",
            timeout_seconds=kwargs["timeout_seconds"],
        )

    scanner_run, error = run_scanner(
        ScannerConfig(name="fakescan", command="fakescan {changed_files}"),
        _context(tmp_path),
        runner=recording_runner,
    )

    assert error is None
    assert scanner_run.missing is False
    assert scanner_run.outcome == "PASS"
    assert len(calls) == 1
    # The runner gets the resolved absolute path, not the bare name.
    resolved = find_scanner_executable("fakescan")
    assert resolved is not None
    assert Path(resolved) == planted
    assert calls[0] == f"{shell_quote(resolved)} {shell_quote('src/app.py')}"


@pytest.mark.parametrize(
    ("command", "expected_rest"),
    [
        ("fakescan", ""),
        ("fakescan --flag {x}", " --flag {x}"),
        ("  fakescan\t-q", "\t-q"),
        ('"fake scan" -q', " -q"),
        ("'fakescan' -q", " -q"),
    ],
)
def test_bind_command_executable_replaces_only_the_leading_token(
    command: str, expected_rest: str
) -> None:
    resolved = os.path.join(os.path.abspath(os.sep), "tools dir", "fakescan")

    assert (
        bind_command_executable(command, resolved)
        == shell_quote(resolved) + expected_rest
    )


def _plant_echo_scanner(directory: Path, message: str, exit_code: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        planted = directory / "fakescan.bat"
        planted.write_text(
            f"@echo {message}\r\n@exit /b {exit_code}\r\n",
            encoding="utf-8",
            newline="",
        )
    else:
        planted = directory / "fakescan"
        planted.write_text(
            f"#!/bin/sh\necho {message}\nexit {exit_code}\n", encoding="utf-8"
        )
        planted.chmod(0o755)
    return planted


def test_gate_runs_trusted_path_scanner_not_repository_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A same-named program in the repository must not shadow the trusted
    # scanner on PATH: cmd.exe would otherwise check the working directory
    # first when it resolves a bare name.
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    trusted = _plant_echo_scanner(tmp_path / "trusted", "trusted-scanner", 0)
    repo_root = tmp_path / "repo"
    _plant_echo_scanner(repo_root, "hijacked-scanner", 1)
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv(
        "PATH", str(trusted.parent) + os.pathsep + os.environ.get("PATH", "")
    )

    scanner_run, error = run_scanner(
        ScannerConfig(name="fakescan", command="fakescan {changed_files}"),
        ScannerContext(
            repo_root=repo_root,
            diff_range="aaa..bbb",
            changed_files=("src/app.py",),
            changed_py_files=("src/app.py",),
            changed_js_files=(),
        ),
        runner=run_shell_command,
    )

    assert error is None, scanner_run.detail
    assert scanner_run.outcome == "PASS"
    assert scanner_run.run is not None
    assert "trusted-scanner" in scanner_run.run.output_tail
    assert "hijacked-scanner" not in scanner_run.run.output_tail


def test_gate_uses_the_gate_environment_path(
    tmp_path: Path, planted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A gate env without PATH finds nothing, even with the scanner in cwd.
    monkeypatch.setattr(
        "llm_handoff.work_claim.build_work_claim_env",
        lambda passthrough=(), **_kwargs: {"SYSTEMROOT": "x"},
    )

    _scanner_run, error = run_scanner(
        ScannerConfig(name="fakescan", command="fakescan {changed_files}"),
        _context(tmp_path),
        runner=_refusing_runner,
    )

    assert error is not None
    assert error.startswith("scanner_missing:fakescan")


def test_gate_reads_path_case_insensitively(
    tmp_path: Path, planted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "llm_handoff.work_claim.build_work_claim_env",
        lambda passthrough=(), **_kwargs: {"Path": str(planted.parent)},
    )

    scanner_run, error = run_scanner(
        ScannerConfig(name="fakescan", command="fakescan {changed_files}"),
        _context(tmp_path),
        runner=lambda command, **kwargs: CommandRun(
            command=command,
            exit_code=0,
            elapsed_seconds=0.0,
            output_tail="",
            timeout_seconds=kwargs["timeout_seconds"],
        ),
    )

    assert error is None
    assert scanner_run.outcome == "PASS"
