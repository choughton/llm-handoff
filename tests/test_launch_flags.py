from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff import agent_roles, config, orchestrator
from llm_handoff.agent_providers import claude, codex, gemini
from llm_handoff.agent_types import _ProcessResult
from llm_handoff.config import AgentConfig, DispatchConfig, load_dispatch_config


REAL_DETECT_GEMINI_SANDBOX = gemini.detect_gemini_sandbox_runtime
REPO_ROOT = Path(__file__).resolve().parents[1]


class _CommandRecorder:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        self.commands.append(list(command))
        return _ProcessResult(stdout="", stderr="", exit_code=0)


# Claude Code


def test_claude_default_launch_uses_dont_ask_with_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(claude, "_run_command_streaming", recorder)

    claude.invoke_claude_subagent("auditor", "audit", repo_root=Path.cwd())

    command = recorder.commands[0]
    assert "--dangerously-skip-permissions" not in command
    assert command[1:10] == [
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Edit,Write,Grep,Glob,Agent,Bash",
        "--strict-mcp-config",
        "--allowedTools",
        ",".join(config.CLAUDE_DEFAULT_ALLOWED_TOOLS),
        "--disallowedTools",
        "WebFetch,WebSearch",
    ]


def test_claude_default_allowlist_covers_the_documented_tools() -> None:
    allowed = config.CLAUDE_DEFAULT_ALLOWED_TOOLS

    for rule in (
        "Read",
        "Edit",
        "Write",
        "Grep",
        "Glob",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git log:*)",
        "Bash(git add:*)",
        "Bash(git commit:*)",
        "Bash(git rev-parse:*)",
        "Bash(python:*)",
        "Bash(pytest:*)",
    ):
        assert rule in allowed
    assert not any("push" in rule for rule in allowed)
    assert not any(rule in {"Bash", "Bash(*)"} for rule in allowed)


def test_claude_role_allowed_tools_override_reaches_command() -> None:
    args = claude.claude_permission_args(
        AgentConfig(provider="claude", allowed_tools=("Read", "Bash(git diff:*)"))
    )

    assert args == [
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Bash",
        "--strict-mcp-config",
        "--allowedTools",
        "Read,Bash(git diff:*)",
        "--disallowedTools",
        "WebFetch,WebSearch",
        "--append-system-prompt",
        claude.claude_shell_guidance(("Read", "Bash(git diff:*)")),
    ]


def test_claude_tools_flag_limits_availability_not_just_approval() -> None:
    # --allowedTools only pre-approves; settings could approve other tools.
    # --tools removes every built-in tool outside the role's allowlist.
    args = claude.claude_permission_args(
        AgentConfig(provider="claude", allowed_tools=("Grep", "Bash(git log:*)"))
    )

    assert args[args.index("--tools") + 1] == "Grep,Bash"
    assert "--strict-mcp-config" in args


def test_claude_mcp_rule_in_allowlist_keeps_mcp_servers_loadable() -> None:
    args = claude.claude_permission_args(
        AgentConfig(provider="claude", allowed_tools=("Read", "mcp__docs__search"))
    )

    assert args[args.index("--tools") + 1] == "Read"
    assert "--strict-mcp-config" not in args


def test_claude_empty_allowed_tools_omits_allowlist_flag() -> None:
    args = claude.claude_permission_args(
        AgentConfig(provider="claude", allowed_tools=())
    )

    assert "--allowedTools" not in args
    assert args[:5] == [
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--strict-mcp-config",
    ]


def test_claude_unsafe_bypass_restores_previous_flag_in_both_launch_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_recorder = _CommandRecorder()
    fallback_commands: list[list[str]] = []

    def unsupported_stream(command, **_kwargs):
        stream_recorder.commands.append(list(command))
        return _ProcessResult(stdout="", stderr="unknown option", exit_code=1)

    def fallback(agent_name, command, **_kwargs):
        fallback_commands.append(list(command))
        return _ProcessResult(stdout="ok", stderr="", exit_code=0)

    monkeypatch.setattr(claude, "_run_command_streaming", unsupported_stream)
    monkeypatch.setattr(claude, "_run_logged_agent_command", fallback)

    claude.invoke_claude_subagent(
        "auditor",
        "audit",
        repo_root=Path.cwd(),
        agent_config=AgentConfig(provider="claude", unsafe_bypass=True),
    )

    assert stream_recorder.commands[0][1] == "--dangerously-skip-permissions"
    assert fallback_commands[0][1] == "--dangerously-skip-permissions"
    assert "--permission-mode" not in stream_recorder.commands[0]
    assert "--permission-mode" not in fallback_commands[0]


def test_permissions_flag_requires_unsafe_bypass() -> None:
    with pytest.raises(ValueError, match="unsafe_bypass"):
        AgentConfig(
            provider="claude", permissions_flag="--dangerously-skip-permissions"
        )


def test_allowed_tools_only_applies_to_claude() -> None:
    with pytest.raises(ValueError, match="claude roles"):
        AgentConfig(provider="codex", allowed_tools=("Read",))


def test_allowed_tools_rejects_commas() -> None:
    with pytest.raises(ValueError, match="without commas"):
        AgentConfig(provider="claude", allowed_tools=("Read,Edit",))


# Codex


def test_codex_launch_uses_package_schema_for_external_repo(tmp_path: Path) -> None:
    assert not (tmp_path / "llm_handoff").exists()
    paths = codex._codex_artifact_paths(tmp_path)
    command = codex._build_codex_exec_command("implement", artifact_paths=paths)

    assert command.count("--output-schema") == 1
    schema_path = Path(command[command.index("--output-schema") + 1])
    assert schema_path.is_absolute()
    assert schema_path.is_file()
    assert schema_path.is_relative_to(Path(config.__file__).resolve().parent)
    assert not schema_path.is_relative_to(tmp_path.resolve())
    assert paths.output_directory == (tmp_path / "logs/dispatch/codex").resolve()
    assert paths.output_last_message_path == paths.output_directory / "last-message.json"
    assert paths.session_state_path == paths.output_directory / "session.json"
    assert command[command.index("--output-last-message") + 1] == str(
        paths.output_last_message_path
    )


def test_codex_default_launch_uses_workspace_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(codex, "_run_command_streaming", recorder)

    codex.invoke_codex(tmp_path / "HANDOFF.md", repo_root=tmp_path)

    command = recorder.commands[0]
    assert "--yolo" not in command
    assert command[1:6] == [
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "exec",
    ]


def test_codex_resume_launch_uses_workspace_sandbox(tmp_path: Path) -> None:
    command = codex._build_codex_resume_command(
        tmp_path / "HANDOFF.md",
        artifact_paths=codex._codex_artifact_paths(tmp_path),
        thread_id="thread-1",
    )

    assert command[1:7] == [
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "exec",
        "resume",
    ]


def test_codex_unsafe_bypass_from_role_config_restores_yolo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(codex, "_run_command_streaming", recorder)

    agent_roles.invoke_role(
        "backend",
        tmp_path / "HANDOFF.md",
        agent_config=AgentConfig(provider="codex", unsafe_bypass=True),
        repo_root=tmp_path,
    )

    command = recorder.commands[0]
    assert command[1:3] == ["--yolo", "exec"]
    assert "--sandbox" not in command


# Gemini CLI


def test_gemini_default_launch_keeps_container_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(gemini, "_run_command_streaming", recorder)

    gemini.invoke_gemini("planner", tmp_path / "HANDOFF.md", repo_root=tmp_path)

    command = recorder.commands[0]
    assert "--sandbox=false" not in command
    assert command[1:5] == ["--output-format", "stream-json", "--yolo", "--sandbox"]


def test_gemini_unsafe_bypass_restores_previous_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(gemini, "_run_command_streaming", recorder)
    monkeypatch.setattr(gemini, "detect_gemini_sandbox_runtime", lambda: None)

    result = gemini.invoke_gemini(
        "planner",
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        agent_config=AgentConfig(provider="gemini", unsafe_bypass=True),
    )

    assert result.exit_code == 0
    assert recorder.commands[0][3:5] == ["--yolo", "--sandbox=false"]


def test_gemini_fails_closed_without_sandbox_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _CommandRecorder()
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(gemini, "_run_command_streaming", recorder)
    monkeypatch.setattr(gemini, "detect_gemini_sandbox_runtime", lambda: None)

    result = gemini.invoke_gemini(
        "Planner",
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        log=lambda level, message: logs.append((level, message)),
    )

    assert result.exit_code == 1
    assert recorder.commands == []
    assert "agents.planner.unsafe_bypass: true" in result.stderr
    assert logs == [("ERROR", result.stderr)]


@pytest.mark.parametrize(
    ("platform", "configured", "available", "expected"),
    [
        ("win32", "", {"docker"}, "docker"),
        ("linux", "", {"podman"}, "podman"),
        ("linux", "", set(), None),
        ("darwin", "", {"sandbox-exec"}, "sandbox-exec"),
        ("linux", "podman", {"docker"}, None),
        ("linux", "true", {"docker"}, "docker"),
    ],
)
def test_detect_gemini_sandbox_runtime(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    configured: str,
    available: set[str],
    expected: str | None,
) -> None:
    monkeypatch.setattr(gemini.sys, "platform", platform)
    monkeypatch.setenv("GEMINI_SANDBOX", configured)
    monkeypatch.setattr(
        gemini.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in available else None,
    )

    assert REAL_DETECT_GEMINI_SANDBOX() == expected


# Orchestrator startup


def _write_planner_handoff(repo: Path) -> None:
    handoff = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text(
        '---\nnext_agent: planner\nreason: "Scope work."\nproducer: user\n---\n',
        encoding="utf-8",
    )


def test_run_loop_fails_closed_at_startup_without_gemini_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_planner_handoff(tmp_path)
    monkeypatch.setattr(gemini, "detect_gemini_sandbox_runtime", lambda: None)
    dispatched: list[object] = []
    monkeypatch.setattr(
        orchestrator,
        "invoke_planner_role",
        lambda *args, **kwargs: dispatched.append(args),
    )
    logs: list[tuple[str, str]] = []

    exit_code = orchestrator.run_loop(
        DispatchConfig(repo_root=tmp_path, poll_interval_seconds=0),
        max_cycles=1,
        log=lambda level, message: logs.append((level, message)),
    )

    assert exit_code == 1
    assert dispatched == []
    errors = [message for level, message in logs if level == "ERROR"]
    assert any("agents.planner.unsafe_bypass: true" in m for m in errors)
    assert any("agents.frontend.unsafe_bypass: true" in m for m in errors)


def test_startup_banner_shows_launch_posture_and_warns_on_unsafe_bypass(
    tmp_path: Path,
) -> None:
    agents = config._default_agent_configs()
    agents["backend"] = AgentConfig(provider="codex", unsafe_bypass=True)
    logs: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(
        DispatchConfig(repo_root=tmp_path, agents=agents),
        lambda level, message: logs.append((level, message)),
    )

    info = [message for level, message in logs if level == "INFO"]
    warnings = [message for level, message in logs if level == "WARN"]
    assert any(
        m.startswith("Launch planner:") and "--yolo --sandbox" in m for m in info
    )
    assert any(m.startswith("Launch auditor:") and "dontAsk" in m for m in info)
    assert any(m.startswith("Launch backend:") and "--yolo" in m for m in info)
    assert warnings == [
        "UNSAFE BYPASS: backend (codex) launches with --yolo; approvals and "
        "sandboxing are disabled. Remove agents.backend.unsafe_bypass to "
        "restore the safe default."
    ]


def test_startup_banner_has_no_unsafe_warning_by_default(tmp_path: Path) -> None:
    logs: list[tuple[str, str]] = []

    orchestrator._log_startup_banner(
        DispatchConfig(repo_root=tmp_path),
        lambda level, message: logs.append((level, message)),
    )

    assert [m for level, m in logs if level == "WARN"] == []


def test_config_file_reads_unsafe_bypass_and_allowed_tools(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """agents:
  backend:
    provider: codex
    unsafe_bypass: true
  auditor:
    provider: claude
    allowed_tools: [Read, Grep, "Bash(git diff:*)"]
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.agents["backend"].unsafe_bypass is True
    assert loaded.agents["planner"].unsafe_bypass is False
    assert loaded.agents["auditor"].allowed_tools == (
        "Read",
        "Grep",
        "Bash(git diff:*)",
    )
    assert loaded.agents["auditor"].permissions_flag is None


def test_example_config_loads_with_safe_defaults() -> None:
    loaded = load_dispatch_config(
        repo_root=REPO_ROOT,
        config_path=REPO_ROOT / "dispatch_config.example.yaml",
    )

    assert not any(agent.unsafe_bypass for agent in loaded.agents.values())


def test_claude_shell_guidance_lists_the_allowed_prefixes() -> None:
    guidance = claude.claude_shell_guidance(
        ("Read", "Bash(git diff:*)", "Bash(git log *)", "Bash(python:*)")
    )

    assert guidance is not None
    assert "`git diff`, `git log`, `python`" in guidance
    assert "one command per Bash call" in guidance
    assert "`git -C <path>`" in guidance
    assert claude.claude_shell_guidance(("Read", "Grep")) is None


def test_claude_launch_without_bash_gets_no_shell_guidance() -> None:
    args = claude.claude_permission_args(
        AgentConfig(provider="claude", allowed_tools=("Read", "Grep"))
    )

    assert "--append-system-prompt" not in args


def test_claude_role_files_explain_the_shell_allowlist() -> None:
    root = Path(__file__).resolve().parents[1]
    for agents_dir in (
        root / ".claude" / "agents",
        root / "examples" / "reference-workflow" / ".claude" / "agents",
    ):
        for role_file in sorted(agents_dir.glob("*.md")):
            text = role_file.read_text(encoding="utf-8")
            assert "## Shell Commands" in text, role_file
            assert "one command per Bash call" in text, role_file



def _stream_tool_call(command: str, result_text: str, *, is_error: bool) -> str:
    import json

    use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": command},
                }
            ]
        },
    }
    result = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "is_error": is_error,
                    "content": result_text,
                }
            ]
        },
    }
    return json.dumps(use) + "\n" + json.dumps(result) + "\n"


def test_a_refused_command_logs_a_short_warning_not_an_error() -> None:
    logs: list[tuple[str, str]] = []
    monitor = claude._ClaudeStreamJsonMonitor(
        agent_name="Claude backend",
        log=lambda level, message: logs.append((level, message)),
    )
    command = "find logs -type f && git status --porcelain && " + "cat x && " * 40

    monitor.consume_stdout(
        _stream_tool_call(
            command,
            "Permission to use Bash has been denied because Claude Code is "
            "running in don't ask mode. IMPORTANT: You *may* attempt ...",
            is_error=True,
        )
    )

    levels = [level for level, _message in logs]
    assert "ERROR" not in levels
    [warning] = [message for level, message in logs if level == "WARN"]
    assert warning.startswith("Claude backend: refused by the permission rules: ")
    assert warning.endswith("...")
    assert len(warning) < 260
    assert "IMPORTANT" not in warning


def test_other_tool_failures_still_log_errors() -> None:
    logs: list[tuple[str, str]] = []
    monitor = claude._ClaudeStreamJsonMonitor(
        agent_name="Claude backend",
        log=lambda level, message: logs.append((level, message)),
    )

    monitor.consume_stdout(
        _stream_tool_call("python -m ruff check x.py", "Exit code 1", is_error=True)
    )

    assert any(
        level == "ERROR" and "failed: Exit code 1" in message for level, message in logs
    )


def test_claude_shell_guidance_lists_exact_bash_rules() -> None:
    guidance = claude.claude_shell_guidance(("Bash(npm run build)", "Bash(git log:*)"))

    assert guidance is not None
    assert "by prefix: `git log`" in guidance
    assert "as exact commands: `npm run build`" in guidance
    assert claude.claude_shell_guidance(("Bash(make test)",)) is not None


def test_claude_shell_guidance_only_recommends_available_file_tools() -> None:
    guidance = claude.claude_shell_guidance(("Grep", "Bash(git log:*)"))

    assert guidance is not None
    assert "Use the Grep tool instead of" in guidance
    for missing in ("Read", "Glob", "Write", "Edit"):
        assert f"{missing} " not in guidance and f"{missing}," not in guidance
