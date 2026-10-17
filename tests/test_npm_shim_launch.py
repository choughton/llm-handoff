from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from llm_handoff import agent_process


_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="exercises .cmd shim launches, which only exist on Windows",
)
_NEEDS_NODE = pytest.mark.skipif(shutil.which("node") is None, reason="needs node on PATH")

# The argument shape that broke the Codex security reviewer: inner quotes
# plus the characters cmd.exe treats as redirection, pipes, and expansion.
HOSTILE_PROMPT = (
    'Answer with JSON: {"summary": "<one line>", "status": "pass" | "fail"} '
    "and never run a > b & c % d ^ e."
)

# Byte-for-byte the launcher npm's cmd-shim writes for a node package.
_NPM_SHIM_TEMPLATE = (
    "@ECHO off\r\n"
    "GOTO start\r\n"
    ":find_dp0\r\n"
    "SET dp0=%~dp0\r\n"
    "EXIT /b\r\n"
    ":start\r\n"
    "SETLOCAL\r\n"
    "CALL :find_dp0\r\n"
    "\r\n"
    'IF EXIST "%dp0%\\node.exe" (\r\n'
    '  SET "_prog=%dp0%\\node.exe"\r\n'
    ") ELSE (\r\n"
    '  SET "_prog=node"\r\n'
    "  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n"
    ")\r\n"
    "\r\n"
    "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "
    '"%_prog%"  "%dp0%\\node_modules\\{package}\\bin\\{entry}" %*\r\n'
)


def _write_npm_shim(prefix: Path, name: str, *, package: str, entry: str) -> Path:
    script = prefix / "node_modules" / Path(package) / "bin" / entry
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "process.stdout.write(JSON.stringify(process.argv.slice(2)));\n",
        encoding="utf-8",
    )
    shim = prefix / f"{name}.cmd"
    shim.write_text(
        _NPM_SHIM_TEMPLATE.format(package=package.replace("/", "\\"), entry=entry),
        encoding="utf-8",
    )
    return shim


def _target_script(prefix: Path) -> Path:
    return prefix / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"


def test_npm_shim_is_replaced_by_its_node_target(tmp_path: Path) -> None:
    shim = _write_npm_shim(tmp_path, "codex", package="@openai/codex", entry="codex.js")
    local_node = tmp_path / "node.exe"
    local_node.write_text("", encoding="utf-8")

    argv = agent_process._resolve_command_argv([str(shim), "exec", HOSTILE_PROMPT])

    assert argv == [str(local_node), str(_target_script(tmp_path)), "exec", HOSTILE_PROMPT]


def test_npm_shim_falls_back_to_node_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = _write_npm_shim(tmp_path, "codex", package="@openai/codex", entry="codex.js")
    fake_node = tmp_path / "elsewhere" / "node.exe"
    fake_node.parent.mkdir()
    fake_node.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        agent_process.shutil,
        "which",
        lambda name: str(fake_node) if name == "node" else None,
    )

    argv = agent_process._resolve_command_argv([str(shim), "--version"])

    assert argv == [str(fake_node), str(_target_script(tmp_path)), "--version"]


def test_npm_shim_without_a_program_on_path_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = _write_npm_shim(tmp_path, "codex", package="@openai/codex", entry="codex.js")
    monkeypatch.setattr(agent_process.shutil, "which", lambda name: None)

    assert agent_process._resolve_command_argv([str(shim), "--version"]) == [
        str(shim),
        "--version",
    ]


def test_npm_shim_whose_program_resolves_to_a_script_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With .JS in PATHEXT, shutil.which("node") can return a node.js script
    # instead of node.exe; the shim strips .JS first, so the bypass must not
    # hand that script to Popen.
    shim = _write_npm_shim(tmp_path, "codex", package="@openai/codex", entry="codex.js")
    stray_script = tmp_path / "elsewhere" / "node.js"
    stray_script.parent.mkdir()
    stray_script.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        agent_process.shutil,
        "which",
        lambda name: str(stray_script) if name == "node" else None,
    )

    assert agent_process._resolve_command_argv([str(shim), "--version"]) == [
        str(shim),
        "--version",
    ]


def test_npm_shim_with_interpreter_options_is_left_alone(tmp_path: Path) -> None:
    # cmd-shim writes shebang options between the program and the target
    # (`#!/usr/bin/env -S node --loader x`); bypassing would drop them.
    shim = _write_npm_shim(tmp_path, "codex", package="@openai/codex", entry="codex.js")
    text = shim.read_text(encoding="utf-8").replace(
        '"%_prog%"  "%dp0%', '"%_prog%" --loader ./hooks.mjs "%dp0%'
    )
    assert "--loader" in text
    shim.write_text(text, encoding="utf-8")
    (tmp_path / "node.exe").write_text("", encoding="utf-8")

    assert agent_process._resolve_command_argv([str(shim), "--version"]) == [
        str(shim),
        "--version",
    ]


def test_npm_shim_with_environment_assignments_is_left_alone(tmp_path: Path) -> None:
    # cmd-shim writes `#!/usr/bin/env NODE_OPTIONS=... node` as `@SET` lines.
    shim = _write_npm_shim(tmp_path, "codex", package="@openai/codex", entry="codex.js")
    text = _NPM_SHIM_TEMPLATE.format(package="@openai\\codex", entry="codex.js")
    text = text.replace("@ECHO off", "@ECHO off\r\n@SET NODE_OPTIONS=--max-old-space-size=4096", 1)
    assert "@SET NODE_OPTIONS" in text
    shim.write_text(text, encoding="utf-8")
    (tmp_path / "node.exe").write_text("", encoding="utf-8")

    assert agent_process._resolve_command_argv([str(shim), "--version"]) == [
        str(shim),
        "--version",
    ]


def test_hand_written_cmd_wrapper_is_left_alone(tmp_path: Path) -> None:
    shim = tmp_path / "agent.cmd"
    shim.write_text('@echo off\r\n"%~dp0\\tool.exe" %*\r\n', encoding="utf-8")

    assert agent_process._resolve_command_argv([str(shim), "--version"]) == [
        str(shim),
        "--version",
    ]


def test_npm_shim_whose_target_is_missing_is_left_alone(tmp_path: Path) -> None:
    shim = tmp_path / "codex.cmd"
    shim.write_text(
        _NPM_SHIM_TEMPLATE.format(package="@openai\\codex", entry="codex.js"),
        encoding="utf-8",
    )

    assert agent_process._resolve_command_argv([str(shim)]) == [str(shim)]


def test_non_shim_binaries_are_untouched(tmp_path: Path) -> None:
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")

    assert agent_process._resolve_command_argv([str(exe), HOSTILE_PROMPT]) == [
        str(exe),
        HOSTILE_PROMPT,
    ]
    assert agent_process._resolve_command_argv([]) == []


@_WINDOWS_ONLY
@_NEEDS_NODE
def test_streaming_launch_of_npm_shim_keeps_hostile_arguments_intact(
    tmp_path: Path,
) -> None:
    # A fake npm-installed CLI that echoes its argv as JSON, launched the way
    # the dispatcher launches the Codex security reviewer.
    shim = _write_npm_shim(tmp_path, "codex", package="@openai/codex", entry="codex.js")
    command = [
        str(shim),
        "exec",
        "--json",
        "-c",
        'web_search="disabled"',
        HOSTILE_PROMPT,
    ]

    result = agent_process._run_command_streaming(
        command,
        cwd=tmp_path,
        timeout_ms=30_000,
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == command[1:]


@_WINDOWS_ONLY
@_NEEDS_NODE
def test_cmd_exe_shim_launch_still_fails_on_hostile_arguments(tmp_path: Path) -> None:
    # Documents the failure mode the bypass avoids: handing the .cmd shim
    # itself to Popen lets cmd.exe reparse the prompt and read `<one` as an
    # input redirection from a missing file.
    shim = _write_npm_shim(tmp_path, "codex", package="@openai/codex", entry="codex.js")

    completed = subprocess.run(
        [str(shim), HOSTILE_PROMPT],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "cannot find the file specified" in completed.stderr
