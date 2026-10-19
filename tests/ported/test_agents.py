from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import ANY, Mock, call

import pytest

import llm_handoff.agents as agents
import llm_handoff.ledger as ledger
from llm_handoff import config
from llm_handoff.agent_providers.claude import claude_shell_guidance
from llm_handoff.validator import parse_validation_output


HANDOFF_PATH = Path("docs/handoff/HANDOFF.md")
ABSOLUTE_HANDOFF_PATH = (Path.cwd() / HANDOFF_PATH).resolve()
FIXTURES_PATH = Path(__file__).with_name("fixtures")


def _completed_process(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _process_result(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    session_id: str | None = None,
) -> agents._ProcessResult:
    return agents._ProcessResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        session_id=session_id,
    )


def _codex_command_result(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    thread_id: str | None = None,
    final_message: str = "",
) -> agents._CodexCommandResult:
    return agents._CodexCommandResult(
        result=_process_result(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        ),
        thread_id=thread_id,
        final_message=final_message,
    )


def _read_fixture(name: str) -> str:
    return (FIXTURES_PATH / name).read_text(encoding="utf-8")


def test_config_exposes_story_2_constants() -> None:
    assert config.CODEX_SKILL_NAME == "llm-handoff"
    assert config.CLAUDE_PERMISSIONS_FLAG == "--dangerously-skip-permissions"
    assert config.CLAUDE_MODEL == "claude-opus-5-5"
    assert config.AGENT_TIMEOUT_MS == 1_200_000
    assert config.SUBAGENT_TIMEOUT_MS == 900_000
    assert config.PLANNER_RESUME_DEFAULT is True

    if os.name == "nt":
        assert config.CODEX_BINARY == "codex.cmd"
        assert config.CLAUDE_BINARY == "claude.cmd"
    else:
        assert config.CODEX_BINARY == "codex"
        assert config.CLAUDE_BINARY == "claude"


def test_dispatch_config_coerces_string_inputs(tmp_path: Path) -> None:
    dispatch_config = config.DispatchConfig(
        repo_root=str(tmp_path),
        poll_interval_seconds="0",
        max_consecutive_failures="2",
    )

    assert dispatch_config.repo_root == tmp_path.resolve()
    assert dispatch_config.poll_interval_seconds == 0
    assert dispatch_config.max_consecutive_failures == 2


def test_resolve_command_binary_falls_back_from_cmd_wrapper_to_exe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_path = r"C:\Tools\agent-clis\claude.exe"
    original_which = shutil.which

    def fake_which(command_name: str) -> str | None:
        if command_name == "claude.cmd":
            return None
        if command_name == "claude":
            return resolved_path
        return original_which(command_name)

    monkeypatch.setattr(agents.shutil, "which", fake_which)

    assert agents._resolve_command_binary("claude.cmd") == resolved_path


def test_run_logged_agent_command_streams_filtered_codex_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for chunk in (
            "codex\n",
            "The Story 7 work is committed and the handoff is committed.\n",
            "exec\n",
            "\"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -Command 'git status --short --branch' in C:\\Workspace\\target-repo\n",
            "succeeded in 322ms. diff --git a/scripts/dispatch-loop.ps1 b/scripts/dispatch-loop.ps1\n",
            "index 1234567..89abcde 100644\n",
            "@@ -1,4 +1,4 @@\n",
            "+new diff line\n",
            "VALID: YES\n",
        ):
            kwargs["on_stdout"](chunk)
        return _process_result(stdout="captured stdout")

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_logged_agent_command(
        "Codex",
        [config.CODEX_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        log=log_mock,
    )

    assert result.stdout == "captured stdout"
    assert log_mock.call_args_list == [
        call("AGENT", "Codex: codex"),
        call(
            "AGENT",
            "Codex: The Story 7 work is committed and the handoff is committed.",
        ),
        call("AGENT", "Codex: exec"),
        call(
            "AGENT",
            "Codex: \"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -Command 'git status --short --branch' in C:\\Workspace\\target-repo",
        ),
        call("AGENT", "Codex: succeeded in 322ms."),
        call("AGENT", "Codex: VALID: YES"),
    ]


def test_run_logged_agent_command_streams_claude_stderr_as_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        kwargs["on_stderr"]("Transient Claude warning\n")
        return _process_result(stderr="Transient Claude warning\n", exit_code=1)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_logged_agent_command(
        "Claude auditor",
        [config.CLAUDE_BINARY, "-p", "prompt"],
        cwd=Path.cwd(),
        timeout_ms=config.SUBAGENT_TIMEOUT_MS,
        log=log_mock,
    )

    assert result.exit_code == 1
    assert result.stderr == "Transient Claude warning\n"
    assert log_mock.call_args_list == [
        call("WARN", "Claude auditor stderr: Transient Claude warning")
    ]


def test_run_logged_agent_command_skips_live_monitor_when_logging_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_command_streaming(command, **kwargs):
        del command
        assert kwargs["on_stdout"] is None
        assert kwargs["on_stderr"] is None
        return _process_result(stdout="captured stdout")

    monkeypatch.setattr(
        agents,
        "_LiveAgentOutputMonitor",
        Mock(side_effect=AssertionError("live monitor should not be created")),
    )
    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_logged_agent_command(
        "Codex",
        [config.CODEX_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        log=None,
    )

    assert result.stdout == "captured stdout"


def test_run_logged_agent_command_promotes_codex_stderr_progress_and_suppresses_tool_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for chunk in (
            "OpenAI Codex v0.121.0 (research preview)\n",
            "--------\n",
            "workdir: C:\\Workspace\\target-repo\n",
            "model: gpt-5.4\n",
            "user\n",
            "Use the repo skill $llm-handoff.\n",
            "deprecated: `[features].web_search_request` is deprecated because web search is enabled by default.\n",
            "codex\n",
            "Using the `llm-handoff` skill for this repo.\n",
            "exec\n",
            '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -Command "Get-Content -Raw \'.\\docs\\PROJECT_SPEC.md\'" in C:\\Workspace\\target-repo\n',
            "# llm-handoff - Product Requirements Document (PRD)\n",
            "**Version:** 2.0\n",
            "exec\n",
            "\"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -Command 'git status --short --branch' in C:\\Workspace\\target-repo\n",
            "succeeded in 322ms.\n",
            "codex\n",
            "I found strong existing coverage in `tests/api/test_api_export.py`.\n",
            "Error: failed to parse config\n",
        ):
            kwargs["on_stderr"](chunk)
        return _process_result(stderr="captured codex stderr")

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_logged_agent_command(
        "Codex",
        [config.CODEX_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        log=log_mock,
        stderr_mode="codex",
    )

    assert result.stderr == "captured codex stderr"
    assert log_mock.call_args_list == [
        call("AGENT", "Codex: OpenAI Codex v0.121.0 (research preview)"),
        call("AGENT", "Codex: --------"),
        call("AGENT", "Codex: workdir: C:\\Workspace\\target-repo"),
        call("AGENT", "Codex: model: gpt-5.4"),
        call("AGENT", "Codex: user"),
        call("AGENT", "Codex: Use the repo skill $llm-handoff."),
        call(
            "AGENT",
            "Codex: deprecated: `[features].web_search_request` is deprecated because web search is enabled by default.",
        ),
        call("AGENT", "Codex: codex"),
        call("AGENT", "Codex: Using the `llm-handoff` skill for this repo."),
        call("AGENT", "Codex: exec"),
        call(
            "AGENT",
            'Codex: "C:\\Program Files\\PowerShell\\7\\pwsh.exe" -Command "Get-Content -Raw \'.\\docs\\PROJECT_SPEC.md\'" in C:\\Workspace\\target-repo',
        ),
        call("AGENT", "Codex: exec"),
        call(
            "AGENT",
            "Codex: \"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -Command 'git status --short --branch' in C:\\Workspace\\target-repo",
        ),
        call("AGENT", "Codex: succeeded in 322ms."),
        call("AGENT", "Codex: codex"),
        call(
            "AGENT",
            "Codex: I found strong existing coverage in `tests/api/test_api_export.py`.",
        ),
        call("WARN", "Codex stderr: Error: failed to parse config"),
    ]


def test_run_codex_json_command_streams_agent_progress_and_suppresses_command_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_last_message_path = tmp_path / "last-message.json"
    output_last_message_path.write_text(
        '{"status":"completed","summary":"Final structured summary"}',
        encoding="utf-8",
    )
    log_mock = Mock()
    stream = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"I\\u2019m checking the repo state now."}}',
            '{"type":"item.started","item":{"id":"item_2","type":"command_execution","command":"\\"C:\\\\\\\\Program Files\\\\\\\\PowerShell\\\\\\\\7\\\\\\\\pwsh.exe\\" -Command \'git status --short --branch\'","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
            '{"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"\\"C:\\\\\\\\Program Files\\\\\\\\PowerShell\\\\\\\\7\\\\\\\\pwsh.exe\\" -Command \'git status --short --branch\'","aggregated_output":"## main...origin/main\\nM docs/handoff/HANDOFF.md\\n","exit_code":0,"status":"completed"}}',
            '{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"{\\"status\\":\\"completed\\",\\"summary\\":\\"Final structured summary\\"}"}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":3}}',
            "",
        ]
    )

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(stdout=stream)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_codex_json_command(
        "Codex",
        [config.CODEX_BINARY, "--yolo", "exec", "--json", "prompt"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        output_last_message_path=output_last_message_path,
        log=log_mock,
    )

    assert result.thread_id == "thread-123"
    assert result.final_message == (
        '{"status":"completed","summary":"Final structured summary"}'
    )
    assert result.result.stdout == stream
    assert log_mock.call_args_list[0:3] == [
        call("AGENT", "Codex: Codex session thread-123 started"),
        call("AGENT", "Codex: I’m checking the repo state now."),
        call("AGENT", "Codex: exec"),
    ]
    assert log_mock.call_args_list[3].args[0] == "AGENT"
    assert "git status --short --branch" in log_mock.call_args_list[3].args[1]
    assert "HANDOFF.md" not in log_mock.call_args_list[3].args[1]
    assert log_mock.call_args_list[4] == call(
        "DEBUG",
        "usage: Codex provider=codex input_tokens=10 output_tokens=3 "
        "cached_input_tokens=2",
    )


def test_run_codex_json_command_treats_noninteractive_stderr_as_agent_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_last_message_path = tmp_path / "last-message.json"
    log_mock = Mock()
    stream = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-456"}',
            '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"\\"C:\\\\\\\\Program Files\\\\\\\\PowerShell\\\\\\\\7\\\\\\\\pwsh.exe\\" -Command \'git add src/app/routers/export.py && git commit -m \\\\\\"feat: demo\\\\\\"\''  # noqa: E501
            '","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
            '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"\\"C:\\\\\\\\Program Files\\\\\\\\PowerShell\\\\\\\\7\\\\\\\\pwsh.exe\\" -Command \'git add src/app/routers/export.py && git commit -m \\\\\\"feat: demo\\\\\\"\''  # noqa: E501
            '","aggregated_output":"","exit_code":1,"status":"completed"}}',
            "",
        ]
    )

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        for chunk in (
            "2026-04-18T19:11:59.006195Z ERROR codex_core::tools::router: error=Exit code: 1\n",  # noqa: E501
            "Wall time: 1.9 seconds\n",
            "Output:\n",
            "ruff (legacy alias)......................................................Passed\n",  # noqa: E501
            "ruff format..............................................................Failed\n",  # noqa: E501
            "- hook id: ruff-format\n",
            "- files were modified by this hook\n",
            "1 file reformatted\n",
        ):
            kwargs["on_stderr"](chunk)
        return _process_result(stdout=stream, stderr="captured codex stderr")

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_codex_json_command(
        "Codex",
        [config.CODEX_BINARY, "--yolo", "exec", "--json", "prompt"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        output_last_message_path=output_last_message_path,
        log=log_mock,
    )

    assert result.thread_id == "thread-456"
    assert result.result.stderr == "captured codex stderr"
    logged = [entry.args for entry in log_mock.call_args_list]
    assert logged[0] == ("AGENT", "Codex: Codex session thread-456 started")
    assert logged[1] == ("AGENT", "Codex: exec")
    assert logged[2][0] == "AGENT"
    assert "git add src/app/routers/export.py" in logged[2][1]
    assert logged[3][0] == "AGENT"
    assert "command failed with exit code 1" in logged[3][1]
    assert logged[4:] == [
        (
            "AGENT",
            "Codex: 2026-04-18T19:11:59.006195Z ERROR codex_core::tools::router: error=Exit code: 1",  # noqa: E501
        ),
        ("AGENT", "Codex: Wall time: 1.9 seconds"),
        ("AGENT", "Codex: Output:"),
        (
            "AGENT",
            "Codex: ruff (legacy alias)......................................................Passed",  # noqa: E501
        ),
        (
            "AGENT",
            "Codex: ruff format..............................................................Failed",  # noqa: E501
        ),
        ("AGENT", "Codex: - hook id: ruff-format"),
        ("AGENT", "Codex: - files were modified by this hook"),
        ("AGENT", "Codex: 1 file reformatted"),
    ]


def test_cleanup_codex_output_artifacts_removes_last_message_but_keeps_session_state(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    artifact_paths = agents._codex_artifact_paths(repo_root)
    artifact_paths.output_directory.mkdir(parents=True, exist_ok=True)
    artifact_paths.output_last_message_path.write_text("stale", encoding="utf-8")
    artifact_paths.session_state_path.write_text(
        '{"thread_id":"thread-123"}',
        encoding="utf-8",
    )

    agents._cleanup_codex_output_artifacts(artifact_paths)

    assert not artifact_paths.output_last_message_path.exists()
    assert artifact_paths.session_state_path.read_text(encoding="utf-8") == (
        '{"thread_id":"thread-123"}'
    )


def test_run_logged_agent_command_streams_all_non_diff_claude_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for chunk in (
            "thinking through the audit scope\n",
            "collecting validation evidence\n",
            "diff --git a/foo b/foo\n",
            "@@ -1 +1 @@\n",
            "+suppressed diff line\n",
            "final audit verdict incoming\n",
        ):
            kwargs["on_stdout"](chunk)
        return _process_result(stdout="captured claude stdout")

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_logged_agent_command(
        "Claude auditor",
        [config.CLAUDE_BINARY, "-p", "prompt"],
        cwd=Path.cwd(),
        timeout_ms=config.SUBAGENT_TIMEOUT_MS,
        log=log_mock,
        stream_all_stdout=True,
    )

    assert result.stdout == "captured claude stdout"
    assert log_mock.call_args_list == [
        call("AGENT", "Claude auditor: thinking through the audit scope"),
        call("AGENT", "Claude auditor: collecting validation evidence"),
        call("AGENT", "Claude auditor: final audit verdict incoming"),
    ]


def test_invoke_codex_uses_repo_skill_prompt_and_json_artifacts_when_stateless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        return_value=_codex_command_result(
            stdout="codex jsonl",
            stderr="codex stderr",
            thread_id="thread-stateless",
            final_message='{"status":"completed","summary":"done"}',
        )
    )
    cleanup_mock = Mock()
    store_mock = Mock()
    session_state_mock = Mock(return_value=None)

    monkeypatch.setattr(agents, "_run_codex_json_command", run_mock)
    monkeypatch.setattr(agents, "_cleanup_codex_output_artifacts", cleanup_mock)
    monkeypatch.setattr(agents, "_read_codex_session_state", session_state_mock)
    monkeypatch.setattr(agents, "_write_codex_session_state", store_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[30.0, 31.5]))

    result = agents.invoke_codex(HANDOFF_PATH)

    expected_prompt = (
        f"Use the repo skill ${config.CODEX_SKILL_NAME}. Read {HANDOFF_PATH} and treat it as the live task file. "
        "Work statelessly and do not assume prior session memory. "
        "The skill replaces any legacy shared bootstrap or Codex handoff prompt files for this repo. "
        "Follow the skill's repo operating procedure, read/write/follow HANDOFF.md as needed, "
        "and execute the current backend assignment."
    )
    artifact_paths = agents._codex_artifact_paths(Path.cwd())

    assert result.exit_code == 0
    assert result.stdout == '{"status":"completed","summary":"done"}'
    assert result.stderr == "codex stderr"
    assert result.elapsed_seconds == pytest.approx(1.5)
    cleanup_mock.assert_called_once_with(artifact_paths)
    session_state_mock.assert_not_called()
    store_mock.assert_not_called()
    run_mock.assert_called_once_with(
        "Codex",
        [
            config.CODEX_BINARY,
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "-c",
            f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
            "--output-schema",
            str((Path.cwd() / config.CODEX_OUTPUT_SCHEMA_PATH).resolve()),
            "--output-last-message",
            str((Path.cwd() / config.CODEX_OUTPUT_LAST_MESSAGE_PATH).resolve()),
            expected_prompt,
        ],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        idle_timeout_ms=None,
        log=None,
        output_last_message_path=(
            Path.cwd() / config.CODEX_OUTPUT_LAST_MESSAGE_PATH
        ).resolve(),
        env=ANY,
    )


def test_invoke_codex_resumes_managed_session_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        return_value=_codex_command_result(
            stdout="codex jsonl",
            stderr="",
            thread_id="thread-resumed",
            final_message='{"status":"completed","summary":"resumed"}',
        )
    )
    cleanup_mock = Mock()
    store_mock = Mock()
    session_state_mock = Mock(return_value="thread-resumed")

    monkeypatch.setattr(agents, "_run_codex_json_command", run_mock)
    monkeypatch.setattr(agents, "_cleanup_codex_output_artifacts", cleanup_mock)
    monkeypatch.setattr(agents, "_read_codex_session_state", session_state_mock)
    monkeypatch.setattr(agents, "_write_codex_session_state", store_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[40.0, 42.0]))

    result = agents.invoke_codex(HANDOFF_PATH, use_resume=True)

    expected_prompt = (
        f"Continue the existing Codex dispatch session for this repo. Re-read {HANDOFF_PATH} and treat it as the live task file. "
        f"Follow the repo skill ${config.CODEX_SKILL_NAME} as the governing workflow. "
        "Use prior session context where it helps, but treat HANDOFF.md and the repo source-of-truth docs as authoritative for the current assignment."
    )
    artifact_paths = agents._codex_artifact_paths(Path.cwd())

    assert result.exit_code == 0
    assert result.stdout == '{"status":"completed","summary":"resumed"}'
    assert result.stderr == ""
    assert result.elapsed_seconds == pytest.approx(2.0)
    cleanup_mock.assert_called_once_with(artifact_paths)
    session_state_mock.assert_called_once_with(artifact_paths)
    store_mock.assert_called_once_with(artifact_paths, "thread-resumed")
    run_mock.assert_called_once_with(
        "Codex",
        [
            config.CODEX_BINARY,
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "exec",
            "resume",
            "--json",
            "-c",
            f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
            "--output-last-message",
            str((Path.cwd() / config.CODEX_OUTPUT_LAST_MESSAGE_PATH).resolve()),
            "thread-resumed",
            expected_prompt,
        ],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        idle_timeout_ms=None,
        log=None,
        output_last_message_path=(
            Path.cwd() / config.CODEX_OUTPUT_LAST_MESSAGE_PATH
        ).resolve(),
        env=ANY,
    )


def test_invoke_codex_bootstraps_new_managed_session_when_no_resume_state_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        return_value=_codex_command_result(
            thread_id="thread-new",
            final_message='{"status":"completed","summary":"bootstrapped"}',
        )
    )
    cleanup_mock = Mock()
    store_mock = Mock()
    session_state_mock = Mock(return_value=None)

    monkeypatch.setattr(agents, "_run_codex_json_command", run_mock)
    monkeypatch.setattr(agents, "_cleanup_codex_output_artifacts", cleanup_mock)
    monkeypatch.setattr(agents, "_read_codex_session_state", session_state_mock)
    monkeypatch.setattr(agents, "_write_codex_session_state", store_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[50.0, 53.0]))

    result = agents.invoke_codex(HANDOFF_PATH, use_resume=True)

    expected_prompt = (
        f"Use the repo skill ${config.CODEX_SKILL_NAME}. This is the first managed Codex dispatch session for this repo. "
        f"Read {Path('docs/handoff/SHARED_REPO_INIT_PROMPT.md')} and use it as the bootstrap checklist for repo context loading, but do not stop after the startup report. "
        f"After bootstrapping, read {HANDOFF_PATH} and treat it as the live task file. "
        "Follow HANDOFF.md as needed, update it when you finish, and execute the current backend assignment."
    )

    assert result.exit_code == 0
    assert result.stdout == '{"status":"completed","summary":"bootstrapped"}'
    session_state_mock.assert_called_once()
    store_mock.assert_called_once_with(
        agents._codex_artifact_paths(Path.cwd()), "thread-new"
    )
    assert run_mock.call_args.args[1][-1] == expected_prompt


def test_invoke_codex_retries_with_new_session_when_resume_state_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        side_effect=[
            _codex_command_result(
                exit_code=1,
                stdout="",
                stderr="error: session not found",
                thread_id=None,
                final_message="",
            ),
            _codex_command_result(
                stdout="codex jsonl",
                stderr="",
                thread_id="thread-recreated",
                final_message='{"status":"completed","summary":"fresh session"}',
            ),
        ]
    )
    cleanup_mock = Mock()
    clear_mock = Mock()
    store_mock = Mock()
    session_state_mock = Mock(return_value="thread-stale")
    log_mock = Mock()

    monkeypatch.setattr(agents, "_run_codex_json_command", run_mock)
    monkeypatch.setattr(agents, "_cleanup_codex_output_artifacts", cleanup_mock)
    monkeypatch.setattr(agents, "_clear_codex_session_state", clear_mock)
    monkeypatch.setattr(agents, "_read_codex_session_state", session_state_mock)
    monkeypatch.setattr(agents, "_write_codex_session_state", store_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[60.0, 61.5]))

    result = agents.invoke_codex(HANDOFF_PATH, use_resume=True, log=log_mock)

    assert result.exit_code == 0
    assert result.stdout == '{"status":"completed","summary":"fresh session"}'
    cleanup_mock.assert_called_once()
    clear_mock.assert_called_once_with(agents._codex_artifact_paths(Path.cwd()))
    store_mock.assert_called_once_with(
        agents._codex_artifact_paths(Path.cwd()),
        "thread-recreated",
    )
    assert log_mock.call_args_list == [
        call(
            "WARN",
            "Codex managed session thread-stale could not be resumed. Clearing the saved session and starting a fresh Codex session for this dispatch.",
        )
    ]
    resume_command = run_mock.call_args_list[0].args[1]
    assert "--output-schema" not in resume_command
    assert resume_command[-2:] == [
        "thread-stale",
        agents._build_codex_resume_prompt(HANDOFF_PATH),
    ]
    assert run_mock.call_args_list[1].args[1][0:6] == [
        config.CODEX_BINARY,
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "exec",
    ]


def test_invoke_manual_frontend_waits_for_manual_continue_and_logs_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_mock = Mock()
    log_mock = Mock()

    monkeypatch.setattr(agents, "_wait_for_manual_continue", wait_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[70.0, 74.5]))

    result = agents.invoke_manual_frontend(HANDOFF_PATH, log=log_mock)

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.elapsed_seconds == pytest.approx(4.5)
    wait_mock.assert_called_once_with()
    assert log_mock.call_args_list == [
        call(
            "PAUSE",
            "manual frontend is a manual GUI step. Complete the frontend work using "
            f"{ABSOLUTE_HANDOFF_PATH}, then press any key to continue dispatch.",
        )
    ]


def test_invoke_codex_returns_timeout_result(monkeypatch: pytest.MonkeyPatch) -> None:
    run_mock = Mock(
        return_value=_codex_command_result(
            exit_code=1,
            stdout="codex jsonl",
            stderr="Process timed out after 1200 seconds.\npartial stderr",
            final_message='{"status":"interrupted","summary":"partial stdout"}',
        )
    )

    monkeypatch.setattr(agents, "_run_codex_json_command", run_mock)
    monkeypatch.setattr(agents, "_cleanup_codex_output_artifacts", Mock())
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[40.0, 43.0]))

    result = agents.invoke_codex(HANDOFF_PATH)

    assert result.exit_code == 1
    assert result.stdout == '{"status":"interrupted","summary":"partial stdout"}'
    assert "timed out" in result.stderr.lower()
    assert "partial stderr" in result.stderr
    assert result.elapsed_seconds == pytest.approx(3.0)


def test_invoke_claude_subagent_cleans_anthropic_env_and_pins_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        return_value=_process_result(
            exit_code=7,
            stdout="claude stdout",
            stderr="claude stderr",
        )
    )

    monkeypatch.setattr(agents, "_run_claude_stream_json_command", run_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[50.0, 52.0]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "top-secret")

    result = agents.invoke_claude_subagent(
        subagent_name="handoff-validator",
        prompt="Return ONLY the structured output format.",
    )

    assert result.exit_code == 7
    assert result.stdout == "claude stdout"
    assert result.stderr == "claude stderr"
    assert result.elapsed_seconds == pytest.approx(2.0)

    call_args = run_mock.call_args
    assert call_args.args[0] == "Claude handoff-validator"
    assert call_args.args[1] == [
        config.CLAUDE_BINARY,
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
        claude_shell_guidance(config.CLAUDE_DEFAULT_ALLOWED_TOOLS),
        "--model",
        config.CLAUDE_MODEL,
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
        "Return ONLY the structured output format.",
    ]
    assert call_args.kwargs["cwd"] == Path.cwd()
    assert call_args.kwargs["timeout_ms"] == config.SUBAGENT_TIMEOUT_MS
    assert "ANTHROPIC_API_KEY" not in call_args.kwargs["env"]
    assert call_args.kwargs["log"] is None
    assert os.environ["ANTHROPIC_API_KEY"] == "top-secret"


def test_invoke_claude_subagent_returns_timeout_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        return_value=_process_result(
            exit_code=1,
            stdout="partial claude stdout",
            stderr="Process timed out after 900 seconds.\npartial claude stderr",
        )
    )

    monkeypatch.setattr(agents, "_run_claude_stream_json_command", run_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[60.0, 66.0]))

    result = agents.invoke_claude_subagent(
        subagent_name="ledger-updater",
        prompt="Update the ledger and report the new commit SHA.",
    )

    assert result.exit_code == 1
    assert result.stdout == "partial claude stdout"
    assert "timed out" in result.stderr.lower()
    assert "partial claude stderr" in result.stderr
    assert result.elapsed_seconds == pytest.approx(6.0)


def test_invoke_claude_subagent_extracts_plain_text_from_stream_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json = _read_fixture("claude_stream_auditor.jsonl")
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream_json.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(stdout=stream_json)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[70.0, 72.0]))

    result = agents.invoke_claude_subagent(
        subagent_name="auditor",
        prompt="Audit the handoff.",
        log=log_mock,
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == (
        "## Audit Verdict: APPROVED (Story-Close)\n\n"
        "**Scope:** Dispatch runtime hardening in `llm_handoff/agents.py`.\n\n"
        "**Gate:**\n"
        "- pytest: PASS\n"
        "- dispatch: PASS\n"
    )


def test_invoke_claude_subagent_logs_tool_use_and_pairs_tool_results_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json = _read_fixture("claude_stream_auditor.jsonl")
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream_json.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(stdout=stream_json)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[80.0, 82.0]))

    agents.invoke_claude_subagent(
        subagent_name="auditor",
        prompt="Audit the handoff.",
        log=log_mock,
    )

    assert log_mock.call_args_list[:6] == [
        call(
            "AGENT",
            "Claude auditor: Claude session session-auditor started (model: claude-opus-4-7, tools: 4)",
        ),
        call(
            "AGENT",
            "Claude auditor: Reading C:\\repo\\docs\\handoff\\HANDOFF.md",
        ),
        call(
            "AGENT",
            "Claude auditor: Running: git diff --stat HEAD~1..HEAD",
        ),
        call(
            "AGENT",
            "Claude auditor: Delegating to subagent: handoff-validator",
        ),
        call(
            "AGENT",
            "Claude auditor: ## Audit Verdict: APPROVED (Story-Close)",
        ),
        call(
            "AGENT",
            "Claude auditor: **Scope:** Dispatch runtime hardening in `llm_handoff/agents.py`.",
        ),
    ]
    assert all(
        not logged.args[1].endswith("succeeded") for logged in log_mock.call_args_list
    )


def test_format_claude_tool_use_label_keeps_long_command_context() -> None:
    command = (
        'git log --all --oneline | head -5 && echo "---" && git rev-parse HEAD '
        '&& echo "=====" && git status --short --branch'
    )

    label = agents._format_claude_tool_use_label(
        "Bash",
        {"command": command},
    )

    assert label == f"Running: {command}"


def test_invoke_claude_subagent_suppresses_thinking_from_logs_and_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json = _read_fixture("claude_stream_validator.jsonl")
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream_json.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(stdout=stream_json)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[90.0, 91.0]))

    result = agents.invoke_claude_subagent(
        subagent_name="handoff-validator",
        prompt="Validate the handoff.",
        log=log_mock,
    )

    assert "internal thoughts" not in result.stdout
    assert all(
        "internal thoughts" not in logged.args[1] for logged in log_mock.call_args_list
    )


def test_invoke_claude_subagent_preserves_validation_parser_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json = _read_fixture("claude_stream_validator.jsonl")

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream_json.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(stdout=stream_json)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[100.0, 101.0]))

    result = agents.invoke_claude_subagent(
        subagent_name="handoff-validator",
        prompt="Validate the handoff.",
    )
    parsed = parse_validation_output(result.stdout)

    assert parsed.verdict == "NO"
    assert parsed.errors == [
        "routing: HANDOFF does not provide a dispatchable next step."
    ]
    assert parsed.warnings == [
        "sha-present: Planner handoff does not yet include a git commit SHA."
    ]


def test_invoke_claude_subagent_preserves_ledger_parser_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json = _read_fixture("claude_stream_ledger.jsonl")

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream_json.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(stdout=stream_json)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[110.0, 111.0]))

    result = agents.invoke_claude_subagent(
        subagent_name="ledger-updater",
        prompt="Update the ledger.",
    )
    parsed = ledger._parse_subagent_output(result.stdout)

    assert parsed.ledger_updated is True
    assert parsed.project_state_updated is True
    assert parsed.handoff_rewritten is True
    assert parsed.epic_closed == "Dispatch Loop Runtime Hardening"
    assert parsed.next_epic == "Web Search During Analysis"
    assert parsed.audit_sha == "abc1234"
    assert parsed.commit_sha == "0123456789abcdef0123456789abcdef01234567"
    assert parsed.push_status == "PUSHED"
    assert parsed.push_detail == "origin/main now matches HEAD."


def test_invoke_claude_subagent_returns_partial_text_when_stream_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json = _read_fixture("claude_stream_partial.jsonl")

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream_json.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(
            stdout=stream_json,
            stderr="Process timed out after 900 seconds.",
            exit_code=1,
        )

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[120.0, 125.0]))

    result = agents.invoke_claude_subagent(
        subagent_name="auditor",
        prompt="Audit the handoff.",
    )

    assert result.exit_code == 1
    assert result.stdout == "## Audit Verdict: IN PROGRESS\n\n- pytest: still running"
    assert "timed out" in result.stderr.lower()


def test_invoke_claude_subagent_falls_back_to_legacy_text_when_stream_json_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()
    stream_json_mock = Mock(
        return_value=_process_result(
            exit_code=1,
            stdout="",
            stderr=_read_fixture("claude_stream_fallback_error.txt"),
        )
    )
    legacy_mock = Mock(
        return_value=_process_result(
            stdout="VALID: YES\nCHECKS:\n  ROUTING: PASS - Routed to Codex.\n",
            stderr="",
        )
    )

    monkeypatch.setattr(agents, "_run_claude_stream_json_command", stream_json_mock)
    monkeypatch.setattr(agents, "_run_logged_agent_command", legacy_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[130.0, 132.0]))

    result = agents.invoke_claude_subagent(
        subagent_name="handoff-validator",
        prompt="Validate the handoff.",
        log=log_mock,
    )

    assert result.exit_code == 0
    assert result.stdout == "VALID: YES\nCHECKS:\n  ROUTING: PASS - Routed to Codex.\n"
    assert log_mock.call_args_list == [
        call(
            "WARN",
            "Claude stream-json output is not supported by the installed Claude CLI. Falling back to buffered text mode; upgrade Claude Code to restore live subagent streaming.",
        )
    ]
    assert legacy_mock.call_args.args[0] == "Claude handoff-validator"


def test_claude_stream_json_logs_tool_failure_without_dumping_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json = "\n".join(
        [
            '{"type":"system","subtype":"init","session_id":"session-failure","model":"claude-opus-4-7","tools":["Bash"]}',
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_fail","name":"Bash","input":{"command":"pytest","description":"Run tests"}}]}}',
            '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_fail","content":"first failure line\\n'
            + ("x" * 5000)
            + '","is_error":true}]}}',
            '{"type":"result","subtype":"error","is_error":true,"result":"","stop_reason":"error"}',
            "",
        ]
    )
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream_json.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(stdout=stream_json, exit_code=1)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[140.0, 141.0]))

    agents.invoke_claude_subagent(
        subagent_name="auditor",
        prompt="Audit the handoff.",
        log=log_mock,
    )

    assert (
        call(
            "ERROR",
            "Claude auditor: Running: pytest failed: first failure line",
        )
        in log_mock.call_args_list
    )
    assert all("x" * 100 not in logged.args[1] for logged in log_mock.call_args_list)


def test_claude_stream_json_tool_failure_does_not_force_abort_when_agent_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json = "\n".join(
        [
            '{"type":"system","subtype":"init","session_id":"session-recover","model":"claude-opus-4-7","tools":["Bash"]}',
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_fail","name":"Bash","input":{"command":"python3 -c \\"bad path\\""}}]}}',
            '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_fail","content":"Exit code: 1","is_error":true}]}}',
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"VALID: YES\\nCHECKS:\\n  ROUTING: PASS - Routed to Codex.\\n"}]}}',
            '{"type":"result","subtype":"success","is_error":false,"result":"VALID: YES\\nCHECKS:\\n  ROUTING: PASS - Routed to Codex.\\n","stop_reason":"end_turn"}',
            "",
        ]
    )
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for line in stream_json.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        return _process_result(stdout=stream_json, exit_code=0)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[145.0, 146.0]))

    result = agents.invoke_claude_subagent(
        subagent_name="handoff-validator",
        prompt="Validate the handoff.",
        log=log_mock,
    )

    assert result.exit_code == 0
    assert result.stdout.startswith("VALID: YES")
    assert (
        call(
            "ERROR",
            'Claude handoff-validator: Running: python3 -c "bad path" failed: Exit code: 1',
        )
        in log_mock.call_args_list
    )


def test_run_command_streaming_uses_utf8_decoding_for_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen_kwargs: dict[str, object] = {}

    class FakeStream:
        def readline(self) -> str:
            return ""

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = FakeStream()
            self.stderr = FakeStream()
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    def fake_popen(command, **kwargs):
        del command
        popen_kwargs.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(agents.subprocess, "Popen", fake_popen)

    result = agents._run_command_streaming(
        [config.CLAUDE_BINARY, "--version"],
        cwd=Path.cwd(),
        timeout_ms=config.SUBAGENT_TIMEOUT_MS,
    )

    assert result.exit_code == 0
    assert popen_kwargs["encoding"] == "utf-8"
    assert popen_kwargs["errors"] == "replace"


def test_run_command_streaming_does_not_join_reader_threads_forever_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import llm_handoff.agent_process as agent_process

    join_timeouts: list[float | None] = []

    class FakeStream:
        def readline(self) -> str:
            return ""

        def close(self) -> None:
            return None

    class FakeThread:
        def __init__(self, *, target, args, daemon: bool) -> None:
            del target, args
            assert daemon is True

        def start(self) -> None:
            return None

        def join(self, timeout: float | None = None) -> None:
            join_timeouts.append(timeout)

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = FakeStream()
            self.stderr = FakeStream()
            self.returncode = 1
            self.killed = False

        def poll(self) -> int | None:
            return 1 if self.killed else None

        def kill(self) -> None:
            self.killed = True

        def wait(self) -> int:
            return 1

    monotonic_values = iter([0.0, 1.0])

    monkeypatch.setattr(agent_process.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        agent_process.subprocess,
        "Popen",
        lambda _command, **_kwargs: FakeProcess(),
    )
    # The fake has no real pid or process tree; keep the timeout path on the
    # plain kill so the test stays about reader-thread joins.
    monkeypatch.setattr(
        agent_process,
        "_kill_process_tree",
        lambda process: process.kill(),
    )
    monkeypatch.setattr(
        agent_process.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    result = agents._run_command_streaming(
        [config.CLAUDE_BINARY, "--version"],
        cwd=Path.cwd(),
        timeout_ms=1,
    )

    assert result.exit_code == 1
    assert "Process timed out after" in result.stderr
    assert join_timeouts == [
        agent_process.PROCESS_READER_JOIN_TIMEOUT_SECONDS,
        agent_process.PROCESS_READER_JOIN_TIMEOUT_SECONDS,
    ]
