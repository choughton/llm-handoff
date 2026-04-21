from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock, call

import pytest

import llm_handoff.agents as agents
import llm_handoff.ledger as ledger
from llm_handoff import config
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
    assert config.GEMINI_PLANNER_MENTION == "@planner"
    assert config.GEMINI_FRONTEND_MENTION == "@frontend"
    assert config.CODEX_SKILL_NAME == "llm-handoff"
    assert config.CLAUDE_PERMISSIONS_FLAG == "--dangerously-skip-permissions"
    assert config.CLAUDE_MODEL == "claude-opus-4-7"
    assert config.AGENT_TIMEOUT_MS == 1_200_000
    assert config.SUBAGENT_TIMEOUT_MS == 900_000
    assert config.GEMINI_MAX_RETRIES == 3
    assert config.GEMINI_RETRY_BASE_SECONDS == 60
    assert config.GEMINI_RESUME_DEFAULT is True

    if os.name == "nt":
        assert config.GEMINI_BINARY == "gemini.cmd"
        assert config.CODEX_BINARY == "codex.cmd"
        assert config.CLAUDE_BINARY == "claude.cmd"
    else:
        assert config.GEMINI_BINARY == "gemini"
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


def test_invoke_gemini_planner_uses_expected_prompt_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        return_value=_process_result(
            stdout="gemini stdout",
            stderr="gemini stderr",
        )
    )

    monkeypatch.setattr(agents, "_run_gemini_command", run_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[100.0, 101.25]))
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")

    result = agents.invoke_gemini("Planner", HANDOFF_PATH)

    assert result.exit_code == 0
    assert result.stdout == "gemini stdout"
    assert result.stderr == "gemini stderr"
    assert result.elapsed_seconds == pytest.approx(1.25)
    call_args = run_mock.call_args
    assert call_args.args[0] == [
        config.GEMINI_BINARY,
        "--output-format",
        "stream-json",
        "--yolo",
        "--sandbox=false",
        "-p",
        (
            f"{config.GEMINI_PLANNER_MENTION} Execute your assigned task based on the "
            f"provided handoff document. @{ABSOLUTE_HANDOFF_PATH}"
        ),
    ]
    assert call_args.kwargs["cwd"] == Path.cwd()
    assert call_args.kwargs["timeout_ms"] == config.AGENT_TIMEOUT_MS
    assert "GOOGLE_API_KEY" not in call_args.kwargs["env"]
    assert "GEMINI_API_KEY" not in call_args.kwargs["env"]
    assert call_args.kwargs["attempt_number"] == 1
    assert call_args.kwargs["max_attempts"] == config.GEMINI_MAX_RETRIES + 1
    assert call_args.kwargs["log"] is None
    assert os.environ["GOOGLE_API_KEY"] == "google-secret"
    assert os.environ["GEMINI_API_KEY"] == "gemini-secret"


def test_invoke_gemini_appends_additional_instruction_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        return_value=_process_result(
            stdout="gemini stdout",
            stderr="gemini stderr",
        )
    )

    monkeypatch.setattr(agents, "_run_gemini_command", run_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[100.0, 101.0]))

    result = agents.invoke_gemini(
        "Planner",
        HANDOFF_PATH,
        additional_instruction="Scope the next epic instead of repeating finalizer.",
    )

    assert result.exit_code == 0
    assert run_mock.call_args.args[0] == [
        config.GEMINI_BINARY,
        "--output-format",
        "stream-json",
        "--yolo",
        "--sandbox=false",
        "-p",
        (
            f"{config.GEMINI_PLANNER_MENTION} Execute your assigned task based on the "
            f"provided handoff document. @{ABSOLUTE_HANDOFF_PATH} Additional instruction: "
            "Scope the next epic instead of repeating finalizer."
        ),
    ]


def test_invoke_gemini_planner_resume_uses_uuid_and_sha_invalidation_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "3b0f6421-8846-40ac-b8f0-34d14a6778ec"
    run_mock = Mock(
        return_value=_process_result(
            stdout="gemini stdout",
            stderr="",
            session_id=session_id,
        )
    )

    monkeypatch.setattr(agents, "_run_gemini_command", run_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[300.0, 302.0]))

    result = agents.invoke_gemini(
        "Planner",
        HANDOFF_PATH,
        use_resume=True,
        session_id=session_id,
        previous_handoff_sha="previous-sha",
        current_handoff_sha="current-sha",
    )

    assert result.exit_code == 0
    assert result.session_id == session_id
    command = run_mock.call_args.args[0]
    assert command[:6] == [
        config.GEMINI_BINARY,
        "--resume",
        session_id,
        "--output-format",
        "stream-json",
        "--yolo",
    ]
    prompt = command[-1]
    assert prompt.startswith(
        f"{config.GEMINI_PLANNER_MENTION} Continue your assigned task"
    )
    assert f"@{ABSOLUTE_HANDOFF_PATH}" not in prompt
    assert str(ABSOLUTE_HANDOFF_PATH) in prompt
    assert "HANDOFF.md SHA at your prior turn was previous-sha" in prompt
    assert "the current SHA is current-sha" in prompt
    assert "If the SHA has changed" in prompt


def test_invoke_gemini_resume_failure_falls_back_to_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh_session_id = "8b2f3ec0-1f24-47f0-a890-622cd4348d3a"
    run_mock = Mock(
        side_effect=[
            _process_result(
                exit_code=1,
                stderr='Error resuming session: Invalid session identifier "stale-id".',
            ),
            _process_result(
                stdout="fresh ok",
                stderr="",
                session_id=fresh_session_id,
            ),
        ]
    )
    log_mock = Mock()

    monkeypatch.setattr(agents, "_run_gemini_command", run_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[400.0, 405.0]))

    result = agents.invoke_gemini(
        "Planner",
        HANDOFF_PATH,
        use_resume=True,
        session_id="stale-id",
        previous_handoff_sha="old",
        current_handoff_sha="new",
        log=log_mock,
    )

    assert result.exit_code == 0
    assert result.stdout == "fresh ok"
    assert result.session_id == fresh_session_id
    assert result.session_invalidated is True
    resume_command = run_mock.call_args_list[0].args[0]
    fresh_command = run_mock.call_args_list[1].args[0]
    assert resume_command[1:3] == ["--resume", "stale-id"]
    assert "--resume" not in fresh_command
    assert f"@{ABSOLUTE_HANDOFF_PATH}" in fresh_command[-1]
    assert log_mock.call_args_list == [
        call(
            "WARN",
            "Gemini planner managed session stale-id could not be resumed. Clearing the in-memory session and starting a fresh Gemini planner session for this dispatch.",
        )
    ]


def test_gemini_stream_json_monitor_parses_valid_events() -> None:
    log_mock = Mock()
    monitor = agents._GeminiStreamJsonMonitor(
        agent_name="Gemini planner",
        log=log_mock,
        attempt_number=1,
        max_attempts=4,
    )

    monitor.consume_stdout(
        '{"type":"init","session_id":"session-123","model":"gemini-3.1-pro-preview"}\n'
    )
    monitor.consume_stdout(
        '{"type":"message","role":"assistant","content":"I will now read the handoff.","delta":true}\n'
    )
    monitor.consume_stdout(
        '{"type":"tool_use","tool_name":"read_file","tool_id":"tool-1","parameters":{"file_path":"docs/handoff/HANDOFF.md"}}\n'
    )
    monitor.consume_stdout(
        '{"type":"tool_result","tool_id":"tool-1","status":"success","output":"ok"}\n'
    )
    monitor.consume_stdout(
        '{"type":"result","status":"success","stats":{"inputTokens":12,"outputTokens":4}}\n'
    )
    finalized = monitor.finalize(_process_result(stdout="", stderr="", exit_code=0))

    assert finalized.stdout == "I will now read the handoff."
    assert finalized.session_id == "session-123"
    messages = [entry.args[1] for entry in log_mock.call_args_list]
    assert (
        "Gemini planner: Gemini session session-123 started (model: gemini-3.1-pro-preview)"
        in messages
    )
    assert "Gemini planner: I will now read the handoff." in messages
    assert "Gemini planner: Reading docs/handoff/HANDOFF.md" in messages
    assert (
        "Gemini planner: Gemini result (status=success, tokens=12 input / 4 output tokens)"
        in messages
    )


def test_gemini_stream_json_monitor_skips_rate_limit_monitor_without_log() -> None:
    monitor = agents._GeminiStreamJsonMonitor(
        agent_name="Gemini planner",
        log=None,
        attempt_number=1,
        max_attempts=4,
    )

    assert monitor._rate_limit_monitor is None


def test_gemini_live_rate_limit_monitor_skips_agent_output_monitor_without_log() -> (
    None
):
    monitor = agents._GeminiLiveRateLimitMonitor(
        log=None,
        attempt_number=1,
        max_attempts=4,
    )

    assert monitor._agent_output_monitor is None


def test_extract_gemini_stream_text_uses_text_key_fallback() -> None:
    assert (
        agents._extract_gemini_stream_text({"text": "fallback text"}) == "fallback text"
    )


def test_extract_gemini_stream_text_uses_message_key_fallback() -> None:
    assert (
        agents._extract_gemini_stream_text(
            {"content": "", "message": "fallback message"}
        )
        == "fallback message"
    )


def test_extract_gemini_stream_text_uses_output_key_fallback() -> None:
    assert (
        agents._extract_gemini_stream_text(
            {"content": "", "text": "", "message": "", "output": "fallback output"}
        )
        == "fallback output"
    )


def test_format_gemini_result_tokens_includes_units() -> None:
    assert (
        agents._format_gemini_result_tokens({"inputTokens": 12, "outputTokens": 4})
        == "12 input / 4 output tokens"
    )


def test_gemini_stream_json_monitor_handles_malformed_json_without_crashing() -> None:
    log_mock = Mock()
    monitor = agents._GeminiStreamJsonMonitor(
        agent_name="Gemini planner",
        log=log_mock,
        attempt_number=1,
        max_attempts=4,
    )

    monitor.consume_stdout(
        '{"type":"message","role":"assistant","content":"Valid progress line.","delta":true}\n'
    )
    monitor.consume_stdout('{"type":"tool_use","tool_name":"read_file"\n')
    finalized = monitor.finalize(_process_result(stdout="", stderr="", exit_code=0))

    assert finalized.stdout == "Valid progress line."
    assert log_mock.call_args_list == [
        call("AGENT", "Gemini planner: Valid progress line."),
        call(
            "WARN",
            'Gemini planner stream-json parse failed: {"type":"tool_use","tool_name":"read_file"',
        ),
    ]


def test_gemini_stream_json_monitor_ignores_pretty_json_429_blocks_on_stderr() -> None:
    log_mock = Mock()
    monitor = agents._GeminiStreamJsonMonitor(
        agent_name="Gemini planner",
        log=log_mock,
        attempt_number=1,
        max_attempts=4,
    )

    stderr = """Attempt 1 failed with status 429. Retrying with backoff...
{
  "status": "RESOURCE_EXHAUSTED",
  "details": [
    {
      "reason": "MODEL_CAPACITY_EXHAUSTED",
      "metadata": {
        "model": "gemini-3.1-pro-preview"
      }
    }
  ]
}
"""

    monitor.consume_stderr(stderr)
    monitor.finalize(_process_result(stdout="", stderr=stderr, exit_code=0))

    messages = [call.args[1] for call in log_mock.call_args_list]
    assert not any("stream-json parse failed" in message for message in messages)
    assert any(
        "Gemini CLI emitted an internal 429 on CLI attempt 1" in message
        for message in messages
    )


def test_invoke_gemini_opt_in_preserves_api_key_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        return_value=_process_result(
            stdout="gemini stdout",
            stderr="gemini stderr",
        )
    )

    monkeypatch.setattr(agents, "_run_gemini_command", run_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[200.0, 201.0]))
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")

    result = agents.invoke_gemini("Frontend", HANDOFF_PATH, use_api_key_env=True)

    assert result.exit_code == 0
    call_args = run_mock.call_args
    assert call_args.kwargs["env"]["GEMINI_API_KEY"] == "gemini-secret"
    assert "GOOGLE_API_KEY" not in call_args.kwargs["env"]
    assert os.environ["GOOGLE_API_KEY"] == "google-secret"
    assert os.environ["GEMINI_API_KEY"] == "gemini-secret"


def test_invoke_gemini_frontend_retries_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        side_effect=[
            _process_result(exit_code=1, stderr="first"),
            _process_result(exit_code=2, stderr="second"),
            _process_result(
                exit_code=0,
                stdout="frontend ok",
                stderr="frontend warn",
            ),
        ]
    )
    sleep_mock = Mock()

    monkeypatch.setattr(agents, "_run_gemini_command", run_mock)
    monkeypatch.setattr(agents.time, "sleep", sleep_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[5.0, 15.0]))

    result = agents.invoke_gemini("Frontend", HANDOFF_PATH)

    assert result.exit_code == 0
    assert result.stdout == "frontend ok"
    assert result.stderr == "frontend warn"
    assert result.elapsed_seconds == pytest.approx(10.0)
    assert run_mock.call_count == 3
    sleep_mock.assert_has_calls(
        [
            call(config.GEMINI_RETRY_BASE_SECONDS),
            call(config.GEMINI_RETRY_BASE_SECONDS * 2),
        ]
    )


def test_invoke_gemini_logs_retry_wait_before_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        side_effect=[
            _process_result(exit_code=1, stderr="transient failure"),
            _process_result(exit_code=0, stdout="gemini ok"),
        ]
    )
    sleep_mock = Mock()
    log_mock = Mock()

    monkeypatch.setattr(agents, "_run_gemini_command", run_mock)
    monkeypatch.setattr(agents.time, "sleep", sleep_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[30.0, 32.0]))

    result = agents.invoke_gemini("Planner", HANDOFF_PATH, log=log_mock)

    assert result.exit_code == 0
    assert (
        call(
            "WARN",
            "Gemini CLI dispatch attempt 1/4 failed; retrying in 60s.",
        )
        in log_mock.call_args_list
    )
    sleep_mock.assert_called_once_with(config.GEMINI_RETRY_BASE_SECONDS)


def test_run_gemini_command_logs_429_detail_and_warns_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = """Request failed with status 429.
{
  "code": 429,
  "status": "RESOURCE_EXHAUSTED",
  "reason": "rateLimitExceeded",
  "details": [
    {
      "@type": "type.googleapis.com/google.rpc.ErrorInfo",
      "reason": "MODEL_CAPACITY_EXHAUSTED",
      "metadata": {
        "model": "gemini-3.1-pro-preview"
      }
    }
  ]
}"""
    log_mock = Mock()

    monkeypatch.setattr(
        agents,
        "_run_command_streaming",
        Mock(return_value=_process_result(exit_code=1, stderr=stderr)),
    )

    result = agents._run_gemini_command(
        [config.GEMINI_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        env={},
        attempt_number=1,
        max_attempts=4,
        log=log_mock,
    )

    assert result.exit_code == 1
    assert result.stderr == stderr
    assert log_mock.call_count == 2
    assert log_mock.call_args_list[0].args[0] == "INFO"
    assert (
        "Gemini CLI 429 detail during dispatch attempt 1/4 (stderr)"
        in log_mock.call_args_list[0].args[1]
    )
    assert '"status": "RESOURCE_EXHAUSTED"' in log_mock.call_args_list[0].args[1]
    assert '"reason": "MODEL_CAPACITY_EXHAUSTED"' in log_mock.call_args_list[0].args[1]
    assert '"model": "gemini-3.1-pro-preview"' in log_mock.call_args_list[0].args[1]
    assert log_mock.call_args_list[1] == call(
        "WARN",
        "Gemini CLI emitted a 429 on attempt 1/4 (status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, model=gemini-3.1-pro-preview); retrying after backoff. Full stderr captured in the dispatch log.",
    )


def test_run_gemini_command_logs_final_429_warning_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = """{
  "code": 429,
  "status": "RESOURCE_EXHAUSTED",
  "details": [
    {
      "reason": "MODEL_CAPACITY_EXHAUSTED",
      "metadata": {
        "model": "gemini-3.1-pro-preview"
      }
    }
  ]
}"""
    log_mock = Mock()

    monkeypatch.setattr(
        agents,
        "_run_command_streaming",
        Mock(return_value=_process_result(exit_code=9, stderr=stderr)),
    )

    result = agents._run_gemini_command(
        [config.GEMINI_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        env={},
        attempt_number=4,
        max_attempts=4,
        log=log_mock,
    )

    assert result.exit_code == 9
    assert result.stderr == stderr
    assert (
        call(
            "WARN",
            "Gemini CLI emitted a 429 on attempt 4/4 (status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, model=gemini-3.1-pro-preview); no retries remain. Full stderr captured in the dispatch log.",
        )
        in log_mock.call_args_list
    )


def test_run_gemini_command_logs_each_internal_429_when_cli_eventually_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = """Attempt 1 failed with status 429. Retrying with backoff...
{
  "status": "RESOURCE_EXHAUSTED",
  "details": [
    {
      "reason": "MODEL_CAPACITY_EXHAUSTED",
      "metadata": {
        "model": "gemini-3.1-pro-preview"
      }
    }
  ]
}
Attempt 2 failed with status 429. Retrying with backoff...
{
  "status": "RESOURCE_EXHAUSTED",
  "details": [
    {
      "reason": "MODEL_CAPACITY_EXHAUSTED",
      "metadata": {
        "model": "gemini-3.1-pro-preview"
      }
    }
  ]
}"""
    log_mock = Mock()

    monkeypatch.setattr(
        agents,
        "_run_command_streaming",
        Mock(
            return_value=_process_result(
                exit_code=0,
                stdout="agent completed successfully",
                stderr=stderr,
            )
        ),
    )

    result = agents._run_gemini_command(
        [config.GEMINI_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        env={},
        attempt_number=1,
        max_attempts=4,
        log=log_mock,
    )

    assert result.exit_code == 0
    assert result.stdout == "agent completed successfully"
    assert result.stderr == stderr
    assert log_mock.call_count == 4
    assert log_mock.call_args_list[0].args[0] == "INFO"
    assert "Attempt 1 failed with status 429" in log_mock.call_args_list[0].args[1]
    assert log_mock.call_args_list[1].args[0] == "INFO"
    assert "Attempt 2 failed with status 429" in log_mock.call_args_list[1].args[1]
    assert log_mock.call_args_list[2] == call(
        "WARN",
        "Gemini CLI emitted an internal 429 on CLI attempt 1 (status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, model=gemini-3.1-pro-preview); command eventually completed successfully. Full retry output captured in the dispatch log.",
    )
    assert log_mock.call_args_list[3] == call(
        "WARN",
        "Gemini CLI emitted an internal 429 on CLI attempt 2 (status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, model=gemini-3.1-pro-preview); command eventually completed successfully. Full retry output captured in the dispatch log.",
    )


def test_invoke_gemini_stops_after_three_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_mock = Mock(
        side_effect=[
            _process_result(exit_code=9, stderr="attempt 1"),
            _process_result(exit_code=9, stderr="attempt 2"),
            _process_result(exit_code=9, stderr="attempt 3"),
            _process_result(exit_code=9, stderr="attempt 4"),
        ]
    )
    sleep_mock = Mock()

    monkeypatch.setattr(agents, "_run_gemini_command", run_mock)
    monkeypatch.setattr(agents.time, "sleep", sleep_mock)
    monkeypatch.setattr(agents.time, "monotonic", Mock(side_effect=[20.0, 25.0]))

    result = agents.invoke_gemini("Planner", HANDOFF_PATH)

    assert result.exit_code == 9
    assert result.stdout == ""
    assert result.stderr == "attempt 4"
    assert result.elapsed_seconds == pytest.approx(5.0)
    assert run_mock.call_count == config.GEMINI_MAX_RETRIES + 1
    sleep_mock.assert_has_calls([call(60), call(120), call(240)])


def test_run_gemini_command_falls_back_when_stream_json_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_json_mock = Mock(
        return_value=_process_result(
            exit_code=1,
            stdout="",
            stderr="error: unknown flag '--output-format'",
        )
    )
    legacy_mock = Mock(
        return_value=_process_result(
            exit_code=0,
            stdout="Fallback success",
            stderr="",
        )
    )
    log_mock = Mock()

    monkeypatch.setattr(agents, "_run_gemini_stream_json_command", stream_json_mock)
    monkeypatch.setattr(agents, "_run_gemini_plain_text_command", legacy_mock)

    result = agents._run_gemini_command(
        [
            config.GEMINI_BINARY,
            "--output-format",
            "stream-json",
            "--yolo",
            "--sandbox=false",
            "-p",
            "prompt",
        ],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        env={"PATH": os.environ.get("PATH", "")},
        attempt_number=1,
        max_attempts=4,
        log=log_mock,
    )

    assert result.exit_code == 0
    assert result.stdout == "Fallback success"
    assert stream_json_mock.call_count == 1
    assert legacy_mock.call_count == 1
    assert legacy_mock.call_args.args[0] == [
        config.GEMINI_BINARY,
        "--yolo",
        "--sandbox=false",
        "-p",
        "prompt",
    ]
    assert log_mock.call_args_list == [
        call(
            "WARN",
            "Gemini stream-json output is not supported by the installed Gemini CLI. Falling back to mixed text mode; upgrade Gemini CLI to restore structured dispatch streaming.",
        )
    ]


def test_run_gemini_command_emits_live_429_warning_before_process_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamed_chunks = [
        "Attempt 1 failed with status 429. Retrying with backoff...\n",
        "{\n",
        '  "status": "RESOURCE_EXHAUSTED",\n',
        '  "details": [\n',
        "    {\n",
        '      "reason": "MODEL_CAPACITY_EXHAUSTED",\n',
        '      "metadata": {\n',
        '        "model": "gemini-3.1-pro-preview"\n',
    ]
    full_stderr = "".join(
        streamed_chunks
        + [
            "      }\n",
            "    }\n",
            "  ]\n",
            "}\n",
        ]
    )
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for chunk in streamed_chunks:
            kwargs["on_stderr"](chunk)
        assert log_mock.call_args_list == [
            call(
                "WARN",
                "Gemini CLI emitted an internal 429 on CLI attempt 1 "
                "(status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, "
                "model=gemini-3.1-pro-preview) during dispatch attempt 1/4; "
                "Gemini CLI is still retrying. Completed retry blocks are "
                "logged to the dispatch log as they finish.",
            )
        ]
        return _process_result(
            exit_code=0,
            stdout="done",
            stderr=full_stderr,
        )

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_gemini_command(
        [config.GEMINI_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        env={"PATH": os.environ.get("PATH", "")},
        attempt_number=1,
        max_attempts=4,
        log=log_mock,
    )

    assert result.exit_code == 0
    assert result.stdout == "done"
    assert result.stderr == full_stderr
    assert log_mock.call_args_list[1].args[0] == "INFO"
    assert (
        "Gemini CLI internal 429 detail for CLI attempt 1 during dispatch attempt 1/4"
        in log_mock.call_args_list[1].args[1]
    )


def test_run_gemini_command_emits_each_live_internal_429_block_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_stderr = """Attempt 1 failed with status 429. Retrying with backoff...
{
  "status": "RESOURCE_EXHAUSTED",
  "details": [
    {
      "reason": "MODEL_CAPACITY_EXHAUSTED",
      "metadata": {
        "model": "gemini-3.1-pro-preview"
      }
    }
  ]
}
Attempt 1 failed with status 429. Retrying with backoff...
{
  "status": "RESOURCE_EXHAUSTED",
  "details": [
    {
      "reason": "MODEL_CAPACITY_EXHAUSTED",
      "metadata": {
        "model": "gemini-3.1-pro-preview"
      }
    }
  ]
}"""
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for chunk in full_stderr.splitlines(keepends=True):
            kwargs["on_stderr"](chunk)
        assert log_mock.call_args_list == [
            call(
                "WARN",
                "Gemini CLI emitted an internal 429 on CLI attempt 1 "
                "(status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, "
                "model=gemini-3.1-pro-preview) during dispatch attempt 1/4; "
                "Gemini CLI is still retrying. Completed retry blocks are "
                "logged to the dispatch log as they finish.",
            ),
            call(
                "INFO",
                "Gemini CLI internal 429 detail for CLI attempt 1 during "
                "dispatch attempt 1/4 (stderr):\n"
                "Attempt 1 failed with status 429. Retrying with backoff...\n"
                "{\n"
                '  "status": "RESOURCE_EXHAUSTED",\n'
                '  "details": [\n'
                "    {\n"
                '      "reason": "MODEL_CAPACITY_EXHAUSTED",\n'
                '      "metadata": {\n'
                '        "model": "gemini-3.1-pro-preview"\n'
                "      }\n"
                "    }\n"
                "  ]\n"
                "}",
            ),
            call(
                "WARN",
                "Gemini CLI emitted an internal 429 on CLI attempt 1 "
                "(status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, "
                "model=gemini-3.1-pro-preview) during dispatch attempt 1/4; "
                "Gemini CLI is still retrying. Completed retry blocks are "
                "logged to the dispatch log as they finish.",
            ),
        ]
        return _process_result(
            exit_code=0,
            stderr=full_stderr,
        )

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    agents._run_gemini_command(
        [config.GEMINI_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        env={"PATH": os.environ.get("PATH", "")},
        attempt_number=1,
        max_attempts=4,
        log=log_mock,
    )

    assert log_mock.call_count == 4
    assert log_mock.call_args_list[3].args[0] == "INFO"


def test_run_gemini_command_logs_completed_retry_block_before_process_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamed_chunks = [
        "Attempt 1 failed with status 429. Retrying with backoff...\n",
        "{\n",
        '  "status": "RESOURCE_EXHAUSTED",\n',
        '  "details": [\n',
        "    {\n",
        '      "reason": "MODEL_CAPACITY_EXHAUSTED",\n',
        '      "metadata": {\n',
        '        "model": "gemini-3.1-pro-preview"\n',
        "      }\n",
        "    }\n",
        "  ]\n",
        "}\n",
        "Gemini resumed work and scanned HANDOFF.md before the next retry.\n",
        "Attempt 2 failed with status 429. Retrying with backoff...\n",
    ]
    full_stderr = "".join(
        streamed_chunks
        + [
            "{\n",
            '  "status": "RESOURCE_EXHAUSTED",\n',
            '  "details": [\n',
            "    {\n",
            '      "reason": "MODEL_CAPACITY_EXHAUSTED",\n',
            '      "metadata": {\n',
            '        "model": "gemini-3.1-pro-preview"\n',
            "      }\n",
            "    }\n",
            "  ]\n",
            "}\n",
        ]
    )
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        for chunk in streamed_chunks:
            kwargs["on_stderr"](chunk)
        assert log_mock.call_args_list == [
            call(
                "WARN",
                "Gemini CLI emitted an internal 429 on CLI attempt 1 "
                "(status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, "
                "model=gemini-3.1-pro-preview) during dispatch attempt 1/4; "
                "Gemini CLI is still retrying. Completed retry blocks are "
                "logged to the dispatch log as they finish.",
            ),
            call(
                "INFO",
                "Gemini CLI internal 429 detail for CLI attempt 1 during "
                "dispatch attempt 1/4 (stderr):\n"
                "Attempt 1 failed with status 429. Retrying with backoff...\n"
                "{\n"
                '  "status": "RESOURCE_EXHAUSTED",\n'
                '  "details": [\n'
                "    {\n"
                '      "reason": "MODEL_CAPACITY_EXHAUSTED",\n'
                '      "metadata": {\n'
                '        "model": "gemini-3.1-pro-preview"\n'
                "      }\n"
                "    }\n"
                "  ]\n"
                "}\n"
                "Gemini resumed work and scanned HANDOFF.md before the next retry.",
            ),
        ]
        return _process_result(exit_code=130, stderr=full_stderr)

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_gemini_command(
        [config.GEMINI_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        env={"PATH": os.environ.get("PATH", "")},
        attempt_number=1,
        max_attempts=4,
        log=log_mock,
    )

    assert result.exit_code == 130
    assert result.stderr == full_stderr
    assert log_mock.call_args_list == [
        call(
            "WARN",
            "Gemini CLI emitted an internal 429 on CLI attempt 1 "
            "(status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, "
            "model=gemini-3.1-pro-preview) during dispatch attempt 1/4; "
            "Gemini CLI is still retrying. Completed retry blocks are "
            "logged to the dispatch log as they finish.",
        ),
        call(
            "INFO",
            "Gemini CLI internal 429 detail for CLI attempt 1 during "
            "dispatch attempt 1/4 (stderr):\n"
            "Attempt 1 failed with status 429. Retrying with backoff...\n"
            "{\n"
            '  "status": "RESOURCE_EXHAUSTED",\n'
            '  "details": [\n'
            "    {\n"
            '      "reason": "MODEL_CAPACITY_EXHAUSTED",\n'
            '      "metadata": {\n'
            '        "model": "gemini-3.1-pro-preview"\n'
            "      }\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "Gemini resumed work and scanned HANDOFF.md before the next retry.",
        ),
        call(
            "INFO",
            "Gemini CLI internal 429 detail for CLI attempt 2 during "
            "dispatch attempt 1/4 (stderr):\n"
            "Attempt 2 failed with status 429. Retrying with backoff...\n"
            "{\n"
            '  "status": "RESOURCE_EXHAUSTED",\n'
            '  "details": [\n'
            "    {\n"
            '      "reason": "MODEL_CAPACITY_EXHAUSTED",\n'
            '      "metadata": {\n'
            '        "model": "gemini-3.1-pro-preview"\n'
            "      }\n"
            "    }\n"
            "  ]\n"
            "}",
        ),
        call(
            "WARN",
            "Gemini CLI emitted a 429 on CLI attempt 2 "
            "(status=RESOURCE_EXHAUSTED, reason=MODEL_CAPACITY_EXHAUSTED, "
            "model=gemini-3.1-pro-preview) during dispatch attempt 1/4; "
            "retrying after backoff. Full stderr captured in the dispatch log.",
        ),
    ]


def test_run_gemini_command_streams_live_stdout_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_mock = Mock()

    def fake_run_command_streaming(command, **kwargs):
        del command
        kwargs["on_stdout"](
            "Gemini scoped the next epic and is rewriting HANDOFF.md.\n"
        )
        return _process_result(
            exit_code=0,
            stdout="Gemini scoped the next epic and is rewriting HANDOFF.md.\n",
        )

    monkeypatch.setattr(agents, "_run_command_streaming", fake_run_command_streaming)

    result = agents._run_gemini_command(
        [config.GEMINI_BINARY, "--yolo"],
        cwd=Path.cwd(),
        timeout_ms=config.AGENT_TIMEOUT_MS,
        env={"PATH": os.environ.get("PATH", "")},
        attempt_number=1,
        max_attempts=4,
        log=log_mock,
    )

    assert result.exit_code == 0
    assert result.stdout == "Gemini scoped the next epic and is rewriting HANDOFF.md.\n"
    assert log_mock.call_args_list == [
        call(
            "AGENT",
            "Gemini CLI: Gemini scoped the next epic and is rewriting HANDOFF.md.",
        )
    ]


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
        "INFO",
        "Codex: turn completed (tokens=10/3, cached=2)",
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
            "--yolo",
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
        log=None,
        output_last_message_path=(
            Path.cwd() / config.CODEX_OUTPUT_LAST_MESSAGE_PATH
        ).resolve(),
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
            "--yolo",
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
        log=None,
        output_last_message_path=(
            Path.cwd() / config.CODEX_OUTPUT_LAST_MESSAGE_PATH
        ).resolve(),
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
    assert run_mock.call_args_list[1].args[1][0:3] == [
        config.CODEX_BINARY,
        "--yolo",
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
        config.CLAUDE_PERMISSIONS_FLAG,
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
