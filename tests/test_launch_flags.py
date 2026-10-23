from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff import agent_roles, config, orchestrator
from llm_handoff.agent_providers import claude, codex
from llm_handoff.agent_types import _ProcessResult
from llm_handoff.config import AgentConfig, DispatchConfig, load_dispatch_config


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
    ):
        assert rule in allowed
    assert not any("push" in rule for rule in allowed)
    assert not any(rule in {"Bash", "Bash(*)"} for rule in allowed)


# The default list before GEM-0e, which `interpreter_grants: true` restores.
_PRE_GEM_0E_DEFAULT_TOOLS = (
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    "Agent",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git cat-file:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git rev-parse:*)",
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(pytest:*)",
)
_INTERPRETER_RULES = ("Bash(python:*)", "Bash(python3:*)", "Bash(pytest:*)")


def _old_default_args() -> list[str]:
    return [
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Edit,Write,Grep,Glob,Agent,Bash",
        "--strict-mcp-config",
        "--allowedTools",
        ",".join(_PRE_GEM_0E_DEFAULT_TOOLS),
        "--disallowedTools",
        "WebFetch,WebSearch",
        "--append-system-prompt",
        claude.claude_shell_guidance(_PRE_GEM_0E_DEFAULT_TOOLS),
    ]


def test_claude_default_allowlist_has_no_interpreter_rule() -> None:
    assert config.CLAUDE_INTERPRETER_ALLOWED_TOOLS == _INTERPRETER_RULES
    for rule in _INTERPRETER_RULES:
        assert rule not in config.CLAUDE_DEFAULT_ALLOWED_TOOLS
    assert config.claude_default_allowed_tools(False) == (
        config.CLAUDE_DEFAULT_ALLOWED_TOOLS
    )
    assert config.claude_default_allowed_tools(True) == _PRE_GEM_0E_DEFAULT_TOOLS


def test_claude_default_launch_args_have_no_interpreter_rule(tmp_path: Path) -> None:
    loaded = DispatchConfig(repo_root=tmp_path)
    args = orchestrator._launch_args_for_role(loaded, "auditor")

    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert allowed == list(config.CLAUDE_DEFAULT_ALLOWED_TOOLS)
    for rule in _INTERPRETER_RULES:
        assert rule not in allowed
    guidance = args[args.index("--append-system-prompt") + 1]
    assert "`python`" not in guidance
    assert "`pytest`" not in guidance


def test_interpreter_grants_restore_the_previous_launch_args(tmp_path: Path) -> None:
    loaded = DispatchConfig(
        repo_root=tmp_path, agents=_all_claude_agents(), interpreter_grants=True
    )

    for role in _ROLES:
        assert loaded.agents[role].allowed_tools is None
        assert loaded.agents[role].interpreter_grants is True
        assert orchestrator._launch_args_for_role(loaded, role) == _old_default_args()


def test_interpreter_grants_reach_the_claude_launch_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(claude, "_run_command_streaming", recorder)
    loaded = DispatchConfig(repo_root=tmp_path, interpreter_grants=True)

    claude.invoke_claude_subagent(
        "auditor", "audit", repo_root=tmp_path, agent_config=loaded.agents["auditor"]
    )

    command = recorder.commands[0]
    assert command[1:12] == _old_default_args()


def test_interpreter_grants_reach_claude_fallbacks(tmp_path: Path) -> None:
    agents: dict[str, object] = dict(config._default_agent_configs())
    agents["backend"] = {"provider": "codex", "fallback": {"provider": "claude"}}
    for flag, expected in ((False, _VANILLA_DEFAULT_ARGS), (True, _old_default_args())):
        loaded = DispatchConfig(
            repo_root=tmp_path, agents=agents, interpreter_grants=flag
        )
        fallback = loaded.agents["backend"].fallback
        assert fallback is not None
        assert fallback.interpreter_grants is flag
        assert orchestrator._launch_args_for_agent(fallback) == expected


def test_interpreter_grants_leave_explicit_allowed_tools_as_written(
    tmp_path: Path,
) -> None:
    custom_tools = ("Read", "Bash(git diff:*)")
    results = []
    for flag in (False, True):
        loaded = DispatchConfig(
            repo_root=tmp_path,
            agents=_all_claude_agents(allowed_tools=custom_tools),
            interpreter_grants=flag,
        )
        assert loaded.agents["backend"].allowed_tools == custom_tools
        assert loaded.agents["backend"].claude_allowed_tools == custom_tools
        results.append(orchestrator._launch_args_for_role(loaded, "backend"))
    assert results[0] == results[1]
    assert ",".join(custom_tools) in results[1]


def test_interpreter_grants_leave_unsafe_bypass_and_codex_launches(
    tmp_path: Path,
) -> None:
    agents: dict[str, object] = dict(config._default_agent_configs())
    agents["auditor"] = {"provider": "claude", "unsafe_bypass": True}
    off = DispatchConfig(repo_root=tmp_path, agents=agents)
    on = DispatchConfig(repo_root=tmp_path, agents=agents, interpreter_grants=True)

    for role in ("auditor", "backend"):
        assert orchestrator._launch_args_for_role(on, role) == (
            orchestrator._launch_args_for_role(off, role)
        )
    assert orchestrator._launch_args_for_role(on, "auditor") == [
        "--dangerously-skip-permissions"
    ]


@pytest.mark.parametrize(("flag", "count"), [(False, 14), (True, 17)])
def test_startup_banner_counts_the_flag_aware_default_list(
    tmp_path: Path, flag: bool, count: int
) -> None:
    logs: list[tuple[str, str]] = []
    orchestrator._log_launch_posture(
        DispatchConfig(repo_root=tmp_path, interpreter_grants=flag),
        lambda level, message: logs.append((level, message)),
    )

    [auditor_line] = [m for _level, m in logs if m.startswith("Launch auditor:")]
    assert f"({count} allowed tool rules;" in auditor_line


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


def test_codex_backend_artifact_paths_are_unchanged(tmp_path: Path) -> None:
    default = codex._codex_artifact_paths(tmp_path)
    backend = codex._codex_artifact_paths(tmp_path, role="backend")

    assert default == backend
    assert backend.output_directory == (tmp_path / "logs/dispatch/codex").resolve()
    assert backend.output_last_message_path == (
        tmp_path / "logs/dispatch/codex/last-message.json"
    ).resolve()
    assert backend.session_state_path == (
        tmp_path / "logs/dispatch/codex/session.json"
    ).resolve()
    assert backend.output_directory == (tmp_path / config.CODEX_OUTPUT_DIRECTORY).resolve()


@pytest.mark.parametrize("role", ["tpm", "planner"])
def test_codex_non_backend_roles_get_their_own_artifact_directory(
    tmp_path: Path, role: str
) -> None:
    paths = codex._codex_artifact_paths(tmp_path, role=role)

    directory = (tmp_path / "logs" / "dispatch" / f"codex-{role}").resolve()
    assert paths.output_directory == directory
    assert paths.output_last_message_path == directory / "last-message.json"
    assert paths.session_state_path == directory / "session.json"
    assert paths.output_schema_path == config.CODEX_OUTPUT_SCHEMA_PATH.resolve()


@pytest.mark.parametrize("role", ["../x", "codex", "", "Backend"])
def test_codex_artifact_paths_reject_unknown_roles(tmp_path: Path, role: str) -> None:
    with pytest.raises(ValueError, match="Unknown Codex artifact role"):
        codex._codex_artifact_paths(tmp_path, role=role)


def _write_codex_session(path: Path, thread_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"thread_id": "{thread_id}"}}', encoding="utf-8")


def _resumed_thread_id(command: list[str]) -> str:
    assert command[command.index("exec") + 1] == "resume"
    return command[command.index("--output-last-message") + 2]


def test_codex_resume_reads_only_the_role_session_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(codex, "_run_command_streaming", recorder)
    _write_codex_session(
        tmp_path / "logs/dispatch/codex/session.json", "thread-backend"
    )
    _write_codex_session(
        tmp_path / "logs/dispatch/codex-tpm/session.json", "thread-tpm"
    )

    codex.invoke_codex(
        tmp_path / "HANDOFF.md", repo_root=tmp_path, role_name="tpm", use_resume=True
    )
    codex.invoke_codex(
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        role_name="backend",
        use_resume=True,
    )

    tpm_command, backend_command = recorder.commands
    assert _resumed_thread_id(tpm_command) == "thread-tpm"
    assert "thread-backend" not in tpm_command
    assert tpm_command[tpm_command.index("--output-last-message") + 1] == str(
        (tmp_path / "logs/dispatch/codex-tpm/last-message.json").resolve()
    )
    assert _resumed_thread_id(backend_command) == "thread-backend"
    assert "thread-tpm" not in backend_command


def test_codex_role_without_saved_session_ignores_backend_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(codex, "_run_command_streaming", recorder)
    _write_codex_session(
        tmp_path / "logs/dispatch/codex/session.json", "thread-backend"
    )

    codex.invoke_codex(
        tmp_path / "HANDOFF.md", repo_root=tmp_path, role_name="tpm", use_resume=True
    )

    [command] = recorder.commands
    assert "resume" not in command
    assert "thread-backend" not in command


def test_codex_auditor_last_message_lives_in_its_own_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _CommandRecorder()
    monkeypatch.setattr(codex, "_run_command_streaming", recorder)

    codex.invoke_codex(tmp_path / "HANDOFF.md", repo_root=tmp_path, role_name="auditor")

    [command] = recorder.commands
    last_message = Path(command[command.index("--output-last-message") + 1])
    auditor_directory = (tmp_path / "logs" / "dispatch" / "codex-auditor").resolve()
    assert last_message == auditor_directory / "last-message.json"
    assert auditor_directory.is_dir()
    assert not (tmp_path / "logs" / "dispatch" / "codex").exists()


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


# Antigravity


def test_antigravity_launch_args_follow_unsafe_bypass() -> None:
    assert orchestrator._launch_args_for_agent(AgentConfig(provider="antigravity")) == [
        "--mode",
        "accept-edits",
    ]
    assert orchestrator._launch_args_for_agent(
        AgentConfig(provider="antigravity", unsafe_bypass=True)
    ) == ["--dangerously-skip-permissions"]


def test_startup_banner_describes_antigravity_safe_default(tmp_path: Path) -> None:
    agents = config._default_agent_configs()
    agents["planner"] = AgentConfig(provider="antigravity")
    agents["frontend"] = AgentConfig(provider="antigravity")
    logs: list[tuple[str, str]] = []

    orchestrator._log_launch_posture(
        DispatchConfig(repo_root=tmp_path, agents=agents),
        lambda level, message: logs.append((level, message)),
    )

    [planner_line] = [
        m for level, m in logs if level == "INFO" and m.startswith("Launch planner:")
    ]
    assert "antigravity" in planner_line
    assert "--mode accept-edits" in planner_line
    assert "permissions.allow" in planner_line
    assert "agy user settings" in planner_line
    assert not any(level == "WARN" for level, _m in logs)


def test_startup_banner_warns_on_antigravity_unsafe_bypass(tmp_path: Path) -> None:
    agents = config._default_agent_configs()
    agents["planner"] = AgentConfig(provider="antigravity", unsafe_bypass=True)
    logs: list[tuple[str, str]] = []

    orchestrator._log_launch_posture(
        DispatchConfig(repo_root=tmp_path, agents=agents),
        lambda level, message: logs.append((level, message)),
    )

    assert [m for level, m in logs if level == "WARN"] == [
        "UNSAFE BYPASS: planner (antigravity) launches with "
        "--dangerously-skip-permissions; approvals and sandboxing are disabled. "
        "Remove agents.planner.unsafe_bypass to restore the safe default."
    ]


# Orchestrator startup


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
    warnings = [
        message
        for level, message in logs
        if level == "WARN" and not message.startswith("Verification allowlist:")
    ]
    assert any(
        m.startswith("Launch planner:")
        and "antigravity" in m
        and "permissions.allow" in m
        for m in info
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

    assert [
        m
        for level, m in logs
        if level == "WARN" and not m.startswith("Verification allowlist:")
    ] == []


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


def _stream_tool_call_with_id(
    tool_id: str,
    command: str,
    result_text: str,
    *,
    is_error: bool,
) -> str:
    return _stream_tool_call(command, result_text, is_error=is_error).replace(
        '"toolu_1"', f'"{tool_id}"'
    )


_REFUSAL_TEXT = (
    "Permission to use Bash has been denied because Claude Code is running "
    "in don't ask mode."
)


def test_refusals_log_one_summary_per_dispatch_without_arguments() -> None:
    logs: list[tuple[str, str]] = []
    monitor = claude._ClaudeStreamJsonMonitor(
        agent_name="Claude backend",
        log=lambda level, message: logs.append((level, message)),
    )

    for tool_id, command in (
        ("toolu_1", "git push origin main"),
        ("toolu_2", "git push origin main"),
        ("toolu_3", "cat secret.txt"),
    ):
        monitor.consume_stdout(
            _stream_tool_call_with_id(tool_id, command, _REFUSAL_TEXT, is_error=True)
        )
    before = list(logs)
    result = monitor.finalize(_ProcessResult(stdout="", stderr="err", exit_code=0))

    per_refusal = [
        message
        for level, message in before
        if level == "WARN" and "refused by the permission rules:" in message
    ]
    assert len(per_refusal) == 3
    summaries = [
        message
        for level, message in logs[len(before) :]
        if level == "WARN"
    ]
    assert summaries == [
        "Claude backend: permission rules refused 3 tool call(s) this dispatch "
        "(git push, cat)"
    ]
    assert "origin main" not in summaries[0]
    assert "secret.txt" not in summaries[0]
    assert result.stderr == "err"
    assert result.exit_code == 0


def test_refusals_are_counted_without_a_log() -> None:
    monitor = claude._ClaudeStreamJsonMonitor(agent_name="Claude backend", log=None)

    monitor.consume_stdout(
        _stream_tool_call("git push origin main", _REFUSAL_TEXT, is_error=True)
    )
    monitor.finalize(_ProcessResult(stdout="", stderr="", exit_code=0))

    assert monitor.refusal_count == 1
    assert monitor.refused_prefixes == ["git push"]


def test_unknown_tool_id_refusal_counts_as_tool() -> None:
    import json

    logs: list[tuple[str, str]] = []
    monitor = claude._ClaudeStreamJsonMonitor(
        agent_name="Claude backend",
        log=lambda level, message: logs.append((level, message)),
    )
    result_event = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_missing",
                    "is_error": True,
                    "content": _REFUSAL_TEXT,
                }
            ]
        },
    }

    monitor.consume_stdout(json.dumps(result_event) + "\n")
    monitor.finalize(_ProcessResult(stdout="", stderr="", exit_code=0))

    assert logs[-1] == (
        "WARN",
        "Claude backend: permission rules refused 1 tool call(s) this dispatch (Tool)",
    )


def test_a_run_without_refusals_logs_no_summary() -> None:
    logs: list[tuple[str, str]] = []
    monitor = claude._ClaudeStreamJsonMonitor(
        agent_name="Claude backend",
        log=lambda level, message: logs.append((level, message)),
    )

    monitor.consume_stdout(_stream_tool_call("git status", "clean", is_error=False))
    monitor.finalize(_ProcessResult(stdout="", stderr="", exit_code=0))

    assert not any("permission rules refused" in message for _level, message in logs)


@pytest.mark.parametrize(
    ("name", "input_payload", "expected"),
    [
        ("Bash", {"command": "git push origin main"}, "git push"),
        ("Bash", {"command": "git -C x status"}, "git"),
        ("PowerShell", {"command": '"C:\\Tools\\rg.exe" foo'}, "rg.exe"),
        ("bash", {"command": "/usr/bin/cat secret.txt"}, "cat"),
        ("Bash", {"command": "ca$t secret.txt"}, "<command>"),
        ("Bash", {"command": "   "}, "Bash"),
        ("Bash", {}, "Bash"),
        ("WebFetch", {"url": "https://example.com/secret"}, "WebFetch"),
        ("mcp__server__tool", {}, "mcp__server__tool"),
        ("Bad Tool!", {}, "Tool"),
        (None, None, "Tool"),
    ],
)
def test_refusal_prefix(name: object, input_payload: object, expected: str) -> None:
    assert claude._refusal_prefix(name, input_payload) == expected


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


# Linear mode Claude launches

_ROLES = ("planner", "backend", "frontend", "auditor", "validator", "finalizer")
_LINEAR_TRACKER: dict[str, object] = {
    "provider": "linear",
    "team": "ENG",
    "project": "Platform",
    "eligible_state": "Todo",
    "in_progress_state": "In Progress",
    "success_state": "In Review",
    "mcp_server": "linear",
}
_EXPECTED_LINEAR_TOOLS: dict[str, tuple[str, ...]] = {
    "planner": (
        "mcp__linear__get_issue",
        "mcp__linear__list_issues",
        "mcp__linear__list_comments",
        "mcp__linear__save_comment",
        "mcp__linear__save_issue",
    ),
    "backend": (
        "mcp__linear__get_issue",
        "mcp__linear__list_issues",
        "mcp__linear__list_comments",
        "mcp__linear__save_comment",
    ),
    "frontend": (
        "mcp__linear__get_issue",
        "mcp__linear__list_issues",
        "mcp__linear__list_comments",
        "mcp__linear__save_comment",
    ),
    "auditor": (
        "mcp__linear__get_issue",
        "mcp__linear__list_issues",
        "mcp__linear__list_comments",
        "mcp__linear__save_comment",
        "mcp__linear__save_issue",
    ),
    # Comment-only (TIDY-15): `save_comment` for handoff_repaired, never
    # `save_issue`.
    "validator": (
        "mcp__linear__get_issue",
        "mcp__linear__list_issues",
        "mcp__linear__list_comments",
        "mcp__linear__save_comment",
    ),
    "finalizer": (
        "mcp__linear__get_issue",
        "mcp__linear__list_issues",
        "mcp__linear__list_comments",
        "mcp__linear__save_comment",
    ),
}
_DEFAULT_GUIDANCE = claude.claude_shell_guidance(config.CLAUDE_DEFAULT_ALLOWED_TOOLS)
_VANILLA_DEFAULT_ARGS = [
    "--permission-mode",
    "dontAsk",
    "--tools",
    "Read,Edit,Write,Grep,Glob,Agent,Bash",
    "--strict-mcp-config",
    "--allowedTools",
    ",".join(config.CLAUDE_DEFAULT_ALLOWED_TOOLS),
    "--disallowedTools",
    "WebFetch,WebSearch",
    "--append-system-prompt",
    _DEFAULT_GUIDANCE,
]


def _all_claude_agents(**overrides: object) -> dict[str, dict[str, object]]:
    return {role: {"provider": "claude", **overrides} for role in _ROLES}


def _linear_config(
    tmp_path: Path, agents: dict[str, object], **tracker: object
) -> DispatchConfig:
    return DispatchConfig(
        repo_root=tmp_path, agents=agents, tracker={**_LINEAR_TRACKER, **tracker}
    )


def _mcp_file(tmp_path: Path) -> Path:
    path = tmp_path / "linear-mcp.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_vanilla_mode_claude_launch_args_are_unchanged(tmp_path: Path) -> None:
    default = DispatchConfig(repo_root=tmp_path)
    assert all(agent.allowed_tools is None for agent in default.agents.values())
    for role in ("auditor", "validator", "finalizer"):
        assert orchestrator._launch_args_for_role(default, role) == (
            _VANILLA_DEFAULT_ARGS
        )

    all_claude = DispatchConfig(repo_root=tmp_path, agents=_all_claude_agents())
    for role in _ROLES:
        assert all_claude.agents[role].allowed_tools is None
        assert all_claude.agents[role].claude_mcp_config_path is None
        assert orchestrator._launch_args_for_role(all_claude, role) == (
            _VANILLA_DEFAULT_ARGS
        )

    custom_tools = ("Read", "Bash(git diff:*)")
    custom = DispatchConfig(
        repo_root=tmp_path,
        agents=_all_claude_agents(allowed_tools=custom_tools),
    )
    for role in _ROLES:
        assert custom.agents[role].allowed_tools == custom_tools
        assert orchestrator._launch_args_for_role(custom, role) == [
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
            claude.claude_shell_guidance(custom_tools),
        ]


@pytest.mark.parametrize("role", _ROLES)
def test_linear_mode_without_mcp_config_adds_role_tools(
    tmp_path: Path, role: str
) -> None:
    loaded = _linear_config(tmp_path, _all_claude_agents())
    allowed = (*config.CLAUDE_DEFAULT_ALLOWED_TOOLS, *_EXPECTED_LINEAR_TOOLS[role])

    assert loaded.agents[role].allowed_tools == allowed
    assert loaded.agents[role].claude_mcp_config_path is None
    args = orchestrator._launch_args_for_role(loaded, role)
    assert args == [
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Edit,Write,Grep,Glob,Agent,Bash",
        "--allowedTools",
        ",".join(allowed),
        "--disallowedTools",
        "WebFetch,WebSearch",
        "--append-system-prompt",
        _DEFAULT_GUIDANCE,
    ]
    assert "--strict-mcp-config" not in args
    assert "--mcp-config" not in args


@pytest.mark.parametrize("flag", [False, True])
def test_linear_mode_appends_tools_to_the_flag_aware_default_list(
    tmp_path: Path, flag: bool
) -> None:
    loaded = DispatchConfig(
        repo_root=tmp_path,
        agents=_all_claude_agents(),
        tracker=_LINEAR_TRACKER,
        interpreter_grants=flag,
    )
    base = config.claude_default_allowed_tools(flag)

    for role in _ROLES:
        assert loaded.agents[role].allowed_tools == (
            *base,
            *_EXPECTED_LINEAR_TOOLS[role],
        )
        args = orchestrator._launch_args_for_role(loaded, role)
        assert args[args.index("--allowedTools") + 1] == ",".join(
            (*base, *_EXPECTED_LINEAR_TOOLS[role])
        )
    has_interpreters = "Bash(python:*)" in loaded.agents["backend"].allowed_tools
    assert has_interpreters is flag


@pytest.mark.parametrize("role", _ROLES)
def test_linear_mode_with_mcp_config_loads_only_that_config(
    tmp_path: Path, role: str
) -> None:
    mcp_file = _mcp_file(tmp_path)
    loaded = _linear_config(
        tmp_path, _all_claude_agents(), claude_mcp_config="linear-mcp.json"
    )
    allowed = (*config.CLAUDE_DEFAULT_ALLOWED_TOOLS, *_EXPECTED_LINEAR_TOOLS[role])

    args = orchestrator._launch_args_for_role(loaded, role)

    assert args == [
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Edit,Write,Grep,Glob,Agent,Bash",
        "--allowedTools",
        ",".join(allowed),
        "--disallowedTools",
        "WebFetch,WebSearch",
        "--append-system-prompt",
        _DEFAULT_GUIDANCE,
        "--mcp-config",
        str(mcp_file.resolve()),
        "--strict-mcp-config",
    ]
    assert not any(
        "mcp__" in name for name in args[args.index("--tools") + 1].split(",")
    )


def test_linear_mode_claude_launch_command_carries_mcp_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mcp_file = _mcp_file(tmp_path)
    loaded = _linear_config(
        tmp_path, _all_claude_agents(), claude_mcp_config=str(mcp_file)
    )
    recorder = _CommandRecorder()
    monkeypatch.setattr(claude, "_run_command_streaming", recorder)

    claude.invoke_claude_subagent(
        "backend",
        "work",
        repo_root=tmp_path,
        agent_config=loaded.agents["backend"],
    )

    command = recorder.commands[0]
    index = command.index("--mcp-config")
    assert command[index + 1 : index + 3] == [
        str(mcp_file.resolve()),
        "--strict-mcp-config",
    ]
    assert command[command.index("--allowedTools") + 1].endswith(
        "mcp__linear__save_comment"
    )


def test_linear_mode_custom_allowed_tools_skip_listed_linear_tools(
    tmp_path: Path,
) -> None:
    agents = _all_claude_agents()
    agents["auditor"] = {
        "provider": "claude",
        "allowed_tools": ["Read", "mcp__linear__list_issues"],
    }
    loaded = _linear_config(tmp_path, agents)

    assert loaded.agents["auditor"].allowed_tools == (
        "Read",
        "mcp__linear__list_issues",
        "mcp__linear__get_issue",
        "mcp__linear__list_comments",
        "mcp__linear__save_comment",
        "mcp__linear__save_issue",
    )


def test_linear_mode_uses_the_configured_mcp_server_name(tmp_path: Path) -> None:
    loaded = _linear_config(tmp_path, _all_claude_agents(), mcp_server="team-linear")

    assert loaded.agents["validator"].allowed_tools[-4:] == (
        "mcp__team-linear__get_issue",
        "mcp__team-linear__list_issues",
        "mcp__team-linear__list_comments",
        "mcp__team-linear__save_comment",
    )
    assert not any(
        tool.endswith("__save_issue")
        for tool in loaded.agents["validator"].allowed_tools
    )
    args = orchestrator._launch_args_for_role(loaded, "validator")
    assert "mcp__team-linear__save_comment" in args[args.index("--allowedTools") + 1]


def test_linear_mode_claude_fallback_gets_its_roles_tools(tmp_path: Path) -> None:
    mcp_file = _mcp_file(tmp_path)
    agents: dict[str, object] = dict(config._default_agent_configs())
    agents["planner"] = {"provider": "codex"}
    agents["frontend"] = {"provider": "codex"}
    agents["backend"] = {
        "provider": "codex",
        "fallback": {"provider": "claude", "allowed_tools": ["Read"]},
    }
    agents["auditor"] = {
        "provider": "claude",
        "model": "claude-opus-5-5",
        "fallback": {"provider": "claude", "model": "claude-sonnet-5"},
    }
    loaded = _linear_config(tmp_path, agents, claude_mcp_config="linear-mcp.json")

    backend = loaded.agents["backend"]
    assert backend.allowed_tools is None
    assert backend.claude_mcp_config_path is None
    assert backend.fallback is not None
    assert backend.fallback.allowed_tools == (
        "Read",
        *_EXPECTED_LINEAR_TOOLS["backend"],
    )
    fallback_args = orchestrator._launch_args_for_agent(backend.fallback)
    assert fallback_args[-3:] == [
        "--mcp-config",
        str(mcp_file.resolve()),
        "--strict-mcp-config",
    ]

    auditor = loaded.agents["auditor"]
    assert auditor.fallback is not None
    assert auditor.fallback.allowed_tools == (
        *config.CLAUDE_DEFAULT_ALLOWED_TOOLS,
        *_EXPECTED_LINEAR_TOOLS["auditor"],
    )
    assert auditor.claude_mcp_config_path == mcp_file.resolve()
    assert auditor.fallback.claude_mcp_config_path == mcp_file.resolve()


def test_linear_mode_leaves_non_claude_and_bypass_agents_unchanged(
    tmp_path: Path,
) -> None:
    _mcp_file(tmp_path)
    agents: dict[str, object] = dict(config._default_agent_configs())
    agents["planner"] = {"provider": "codex"}
    agents["frontend"] = {"provider": "codex"}
    agents["auditor"] = {"provider": "claude", "unsafe_bypass": True}
    vanilla =DispatchConfig(repo_root=tmp_path, agents=agents)
    loaded = _linear_config(tmp_path, agents, claude_mcp_config="linear-mcp.json")

    for role in ("planner", "backend", "frontend", "auditor"):
        assert loaded.agents[role] == vanilla.agents[role]
        assert loaded.agents[role].allowed_tools is None
        assert loaded.agents[role].claude_mcp_config_path is None
    assert orchestrator._launch_args_for_role(loaded, "auditor") == [
        "--dangerously-skip-permissions"
    ]
    assert orchestrator._launch_args_for_role(loaded, "backend") == (
        orchestrator._launch_args_for_role(vanilla, "backend")
    )


def _report_only_validator_config(tmp_path: Path, server: str) -> DispatchConfig:
    _mcp_file(tmp_path)
    agents: dict[str, object] = dict(config._default_agent_configs())
    agents["planner"] = {"provider": "codex"}
    agents["frontend"] = {"provider": "codex"}
    agents["validator"] = {
        "provider": "claude",
        "fallback": {"provider": "claude", "model": "claude-sonnet-5"},
    }
    return _linear_config(
        tmp_path, agents, claude_mcp_config="linear-mcp.json", mcp_server=server
    )


def _flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


@pytest.mark.parametrize("server", ["linear", "team-linear"])
def test_report_only_validator_primary_and_fallback_commands_deny_linear_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, server: str
) -> None:
    loaded = _report_only_validator_config(tmp_path, server)
    report_only = config.without_linear_write_tools(loaded.agents["validator"], server)
    commands: list[list[str]] = []

    def fake_stream(_name: str, command: list[str], **_kwargs: object):
        commands.append(list(command))
        if len(commands) == 1:
            # A timeout is fatal, so the fallback launches next.
            return _ProcessResult(
                stdout="", stderr="Process timed out after 900 seconds.", exit_code=1
            )
        return _ProcessResult(stdout="VALID: YES", stderr="", exit_code=0)

    monkeypatch.setattr(claude, "_run_claude_stream_json_command", fake_stream)

    result = agent_roles.invoke_support_role(
        "handoff-validator",
        "validate",
        role="validator",
        handoff_path=tmp_path / "HANDOFF.md",
        agent_config=report_only,
        repo_root=tmp_path,
    )

    assert result.exit_code == 0
    assert len(commands) == 2
    assert _flag_value(commands[1], "--model") == "claude-sonnet-5"
    denied = (
        f"WebFetch,WebSearch,mcp__{server}__save_comment,mcp__{server}__save_issue"
    )
    for command in commands:
        assert _flag_value(command, "--disallowedTools") == denied
        allowed = _flag_value(command, "--allowedTools").split(",")
        assert f"mcp__{server}__get_issue" in allowed
        assert f"mcp__{server}__list_comments" in allowed
        assert f"mcp__{server}__save_comment" not in allowed
        assert f"mcp__{server}__save_issue" not in allowed
        assert _flag_value(command, "--mcp-config") == str(
            (tmp_path / "linear-mcp.json").resolve()
        )
    # The routed repair launch keeps its comment grant and the shared denials.
    repair_args = orchestrator._launch_args_for_role(loaded, "validator")
    assert _flag_value(repair_args, "--disallowedTools") == "WebFetch,WebSearch"
    assert f"mcp__{server}__save_comment" in _flag_value(repair_args, "--allowedTools")


def test_report_only_validator_buffered_launch_keeps_the_denials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loaded = _report_only_validator_config(tmp_path, "linear")
    report_only = config.without_linear_write_tools(
        loaded.agents["validator"], "linear"
    )
    stream_commands: list[list[str]] = []
    buffered_commands: list[list[str]] = []

    def fake_stream(_name: str, command: list[str], **_kwargs: object):
        stream_commands.append(list(command))
        return _ProcessResult(
            stdout="", stderr="error: unknown option '--output-format'", exit_code=1
        )

    def fake_buffered(_name: str, command: list[str], **_kwargs: object):
        buffered_commands.append(list(command))
        return _ProcessResult(stdout="VALID: YES", stderr="", exit_code=0)

    monkeypatch.setattr(claude, "_run_claude_stream_json_command", fake_stream)
    monkeypatch.setattr(claude, "_run_logged_agent_command", fake_buffered)

    claude.invoke_claude_subagent(
        "handoff-validator", "validate", repo_root=tmp_path, agent_config=report_only
    )

    [stream_command] = stream_commands
    [buffered_command] = buffered_commands
    assert _flag_value(buffered_command, "--disallowedTools") == (
        "WebFetch,WebSearch,mcp__linear__save_comment,mcp__linear__save_issue"
    )
    permission_end = stream_command.index("--model")
    assert buffered_command[:permission_end] == stream_command[:permission_end]


def test_report_only_derivation_leaves_bypass_launch_unchanged() -> None:
    bypass = AgentConfig(provider="claude", unsafe_bypass=True)

    report_only = config.without_linear_write_tools(bypass, "linear")

    assert report_only is bypass
    assert claude.claude_permission_args(report_only) == [
        "--dangerously-skip-permissions"
    ]


@pytest.mark.parametrize("with_mcp_config", [False, True])
def test_linear_mode_security_reviewer_gets_no_linear_tools(
    tmp_path: Path, with_mcp_config: bool
) -> None:
    from llm_handoff import audit_review

    tracker: dict[str, object] = {}
    if with_mcp_config:
        _mcp_file(tmp_path)
        tracker["claude_mcp_config"] = "linear-mcp.json"
    loaded = _linear_config(tmp_path, _all_claude_agents(), **tracker)

    plan = audit_review.build_security_reviewer_plan(
        loaded,
        provider="claude",
        scope=audit_review.ReviewScope(
            base_sha="a" * 40,
            head_sha="b" * 40,
            files=("src/app.py",),
            diff_path=tmp_path / "scope.diff",
        ),
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )

    command = list(plan.command)
    assert not any("mcp__" in part for part in command)
    assert "--mcp-config" not in command
    assert "--strict-mcp-config" in command


def test_linear_mode_normalizer_launch_gets_no_linear_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from llm_handoff.normalizer_providers import claude as normalizer_claude

    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(normalizer_claude.subprocess, "run", fake_run)
    try:
        normalizer_claude._normalize_next_agent_with_claude_cli(
            "auditor", model="claude-haiku-5-5", timeout_ms=1000
        )
    except Exception:
        pass

    assert commands
    assert not any("mcp__" in part for part in commands[0])
    assert "--mcp-config" not in commands[0]
    assert "--strict-mcp-config" in commands[0]


def _posture_warnings(loaded: DispatchConfig) -> list[str]:
    logs: list[tuple[str, str]] = []
    orchestrator._log_launch_posture(
        loaded, lambda level, message: logs.append((level, message))
    )
    return [message for level, message in logs if level == "WARN"]


def test_linear_mode_without_mcp_config_warns_once_at_startup(
    tmp_path: Path,
) -> None:
    agents: dict[str, object] = dict(config._default_agent_configs())
    agents["planner"] = {"provider": "codex"}
    agents["frontend"] = {"provider": "codex"}
    agents["backend"] = {
        "provider": "codex",
        "fallback": {"provider": "claude"},
    }
    agents["finalizer"] = {"provider": "claude", "unsafe_bypass": True}

    warnings = _posture_warnings(_linear_config(tmp_path, agents))

    linear_warnings = [m for m in warnings if "tracker.claude_mcp_config" in m]
    assert linear_warnings == [
        "Linear mode without tracker.claude_mcp_config: Claude roles "
        "backend fallback, auditor, validator launch without "
        "--strict-mcp-config, so every MCP server in the user's Claude "
        "configuration loads and tools pre-approved in user or project "
        "settings can run. Set tracker.claude_mcp_config to load only the "
        "Linear MCP server."
    ]


def test_linear_mode_startup_warn_absent_with_mcp_config_or_vanilla(
    tmp_path: Path,
) -> None:
    _mcp_file(tmp_path)
    with_config = _linear_config(
        tmp_path, _all_claude_agents(), claude_mcp_config="linear-mcp.json"
    )
    assert _posture_warnings(with_config) == []
    assert _posture_warnings(DispatchConfig(repo_root=tmp_path)) == []
    assert (
        _posture_warnings(
            DispatchConfig(repo_root=tmp_path, agents=_all_claude_agents())
        )
        == []
    )


def test_linear_mode_startup_warn_absent_without_safe_claude_agents(
    tmp_path: Path,
) -> None:
    agents = _all_claude_agents(unsafe_bypass=True)

    warnings = _posture_warnings(_linear_config(tmp_path, agents))

    assert not any("tracker.claude_mcp_config" in m for m in warnings)
