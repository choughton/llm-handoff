from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from llm_handoff import agent_process, config
from llm_handoff.agent_providers import antigravity
from llm_handoff.agent_types import _ProcessResult
from llm_handoff.config import AgentConfig
from llm_handoff.provider_auth import find_auth_failure


def _event(**fields: object) -> str:
    """Encode one stream-json line in agy's documented envelope.

    ``type`` names the event; agy writes it as the top-level ``event``
    discriminator and nests the payload under that name, for example
    ``{"event": "result", "result": {...}}``.
    """
    name = fields.pop("type")
    return json.dumps({"event": name, name: fields})


def _flat_event(**fields: object) -> str:
    """Encode one stream-json line as a flat object with a ``type`` key."""
    return json.dumps(fields)


def _stream(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _success_stream(response: str = "Done.", conversation_id: str = "conv-9") -> str:
    return _stream(
        _event(type="init", cwd="/repo", permission_mode="default"),
        _event(
            type="step_update",
            conversation_id=conversation_id,
            step_index=0,
            step_type="tool",
            tool_name="read_file",
            state="ACTIVE",
        ),
        _event(
            type="step_update",
            conversation_id=conversation_id,
            step_index=1,
            step_type="agent_response",
            text_delta="Done",
        ),
        _event(
            type="result",
            conversation_id=conversation_id,
            status="SUCCESS",
            response=response,
            usage={"input_tokens": 10, "output_tokens": 4},
        ),
    )


class _Runner:
    """Stands in for _run_command_streaming and feeds recorded stdout."""

    def __init__(self, *results: _ProcessResult, stream: bool = True) -> None:
        self.results = list(results)
        self.stream = stream
        self.commands: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, command, *, on_stdout=None, on_stderr=None, **kwargs):
        self.commands.append(list(command))
        self.kwargs.append(kwargs)
        result = self.results.pop(0)
        if self.stream and on_stdout is not None and result.stdout:
            # Split mid-line to prove the monitor buffers partial lines.
            middle = len(result.stdout) // 2
            on_stdout(result.stdout[:middle])
            on_stdout(result.stdout[middle:])
        if on_stderr is not None and result.stderr:
            on_stderr(result.stderr)
        return result


def _ok(stdout: str | None = None) -> _ProcessResult:
    return _ProcessResult(
        stdout=_success_stream() if stdout is None else stdout,
        stderr="",
        exit_code=0,
    )


def _invoke(monkeypatch: pytest.MonkeyPatch, runner: _Runner, tmp_path: Path, **kwargs):
    monkeypatch.setattr(antigravity, "_run_command_streaming", runner)
    logs: list[tuple[str, str]] = []
    kwargs.setdefault("log", lambda level, message: logs.append((level, message)))
    result = antigravity.invoke_antigravity(
        kwargs.pop("role", "planner"),
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
        **kwargs,
    )
    return result, logs


# Command shape


def test_fresh_launch_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = _Runner(_ok())

    _invoke(monkeypatch, runner, tmp_path, role="backend")

    command = runner.commands[0]
    assert command[0] == config.ANTIGRAVITY_BINARY
    assert command[1] == "-p"
    assert command[3:] == [
        "--agent",
        "backend",
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{config.AGENT_TIMEOUT_MS // 1000}s",
    ]
    prompt = command[2]
    assert "backend role" in prompt
    assert str((tmp_path / "HANDOFF.md").resolve()) in prompt
    assert "repository instructions" in prompt
    assert "live task file" in prompt
    for provider_name in ("Antigravity", "agy", "Gemini", "Claude", "Codex"):
        assert provider_name not in prompt
    assert runner.kwargs[0]["timeout_ms"] == config.AGENT_TIMEOUT_MS
    assert runner.kwargs[0]["cwd"] == tmp_path


def test_resumed_launch_with_model_and_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ok())

    _invoke(
        monkeypatch,
        runner,
        tmp_path,
        binary="agy-beta",
        model="pro",
        timeout_ms=90_500,
        use_resume=True,
        session_id="conv-1",
        previous_handoff_sha="aaa111",
        current_handoff_sha="bbb222",
        additional_instruction="Keep it short.",
        agent_config=AgentConfig(
            provider="antigravity",
            agent_name="planner-agy",
            unsafe_bypass=True,
            idle_timeout_ms=5000,
        ),
    )

    command = runner.commands[0]
    assert command[0] == "agy-beta"
    assert command[3:] == [
        "--agent",
        "planner-agy",
        "--output-format",
        "stream-json",
        "--print-timeout",
        "90s",
        "--model",
        "pro",
        "--conversation",
        "conv-1",
        "--dangerously-skip-permissions",
    ]
    prompt = command[2]
    assert prompt.startswith("Continue your assigned task")
    assert "aaa111" in prompt and "bbb222" in prompt
    assert "@" not in prompt
    assert prompt.endswith("Additional instruction: Keep it short.")
    assert runner.kwargs[0]["timeout_ms"] == 90_500
    assert runner.kwargs[0]["idle_timeout_ms"] == 5000


def test_use_resume_without_session_id_launches_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ok())

    _invoke(monkeypatch, runner, tmp_path, use_resume=True, session_id=None)

    assert "--conversation" not in runner.commands[0]


def test_print_timeout_is_at_least_one_second(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ok())

    _invoke(monkeypatch, runner, tmp_path, timeout_ms=250)

    command = runner.commands[0]
    assert command[command.index("--print-timeout") + 1] == "1s"


def test_support_prompt_is_the_prompt_and_subagent_names_the_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ok())

    _invoke(
        monkeypatch,
        runner,
        tmp_path,
        role="validator",
        support_prompt="Return ONLY the validation output.",
        subagent_name="handoff-validator",
        additional_instruction="Be strict.",
    )

    command = runner.commands[0]
    assert command[2] == (
        "Return ONLY the validation output. Additional instruction: Be strict."
    )
    assert command[command.index("--agent") + 1] == "handoff-validator"


def test_permission_args() -> None:
    assert antigravity.antigravity_permission_args(unsafe_bypass=False) == []
    assert antigravity.antigravity_permission_args(unsafe_bypass=True) == [
        "--dangerously-skip-permissions"
    ]


# Stream parsing


def test_response_and_conversation_id_come_from_result_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ok(_success_stream(response="Final answer.")))

    result, logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 0
    assert result.stdout == "Final answer."
    assert result.session_id == "conv-9"
    assert result.session_invalidated is False
    agent_logs = [m for level, m in logs if level == "AGENT"]
    assert any("session started" in m for m in agent_logs)
    assert any("Using tool: read_file" in m for m in agent_logs)
    assert any(m.endswith(": Done") for m in agent_logs)
    assert any("status=SUCCESS" in m for m in agent_logs)
    [usage] = [m for level, m in logs if level == "DEBUG"]
    assert usage.startswith("usage: Antigravity planner provider=antigravity")
    assert "input_tokens=10" in usage and "output_tokens=4" in usage


def test_captured_stdout_is_parsed_when_runner_does_not_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ok(_success_stream(response="Captured.")), stream=False)

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.stdout == "Captured."
    assert result.session_id == "conv-9"


def test_text_deltas_are_joined_when_result_has_no_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(
        _event(type="step_update", conversation_id="c1", step_type="user_input",
               text_delta="prompt echo "),
        _event(type="step_update", conversation_id="c1", step_type="agent_response",
               text_delta="Hello "),
        _event(type="step_update", conversation_id="c2", step_type="agent_response",
               text_delta="world"),
        _event(type="result", status="SUCCESS", response=""),
    )
    runner = _Runner(_ok(stdout))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 0
    assert result.stdout == "Hello world"
    assert result.session_id == "c2"


def test_raw_stdout_is_returned_without_response_or_deltas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(_event(type="result", status="SUCCESS", conversation_id="c3"))
    runner = _Runner(_ok(stdout))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.stdout == stdout
    assert result.session_id == "c3"


def test_documented_nested_envelope_is_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(
        json.dumps({"event": "init", "init": {"permission_mode": "default"}}),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "nested-1",
                    "step_type": "agent_response",
                    "text_delta": "Working",
                },
            }
        ),
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "nested-1",
                    "status": "SUCCESS",
                    "response": "Nested done.",
                },
            }
        ),
    )
    runner = _Runner(_ok(stdout))

    result, logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "Nested done."
    assert result.session_id == "nested-1"
    agent_logs = [m for level, m in logs if level == "AGENT"]
    assert any("permission mode: default" in m for m in agent_logs)
    assert any(m.endswith(": Working") for m in agent_logs)


def test_nested_non_success_result_fails_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(
        json.dumps(
            {"event": "result", "result": {"status": "ERROR", "error": "denied"}}
        )
    )
    runner = _Runner(_ok(stdout))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 1
    assert result.stderr == "Antigravity result status=ERROR: denied"


def test_flat_type_events_are_still_accepted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(
        _flat_event(type="step_update", conversation_id="flat-1",
                    step_type="agent_response", text_delta="x"),
        _flat_event(type="result", conversation_id="flat-1", status="SUCCESS",
                    response="Flat done."),
    )
    runner = _Runner(_ok(stdout))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "Flat done."
    assert result.session_id == "flat-1"


def test_malformed_lines_are_ignored_with_one_warning_each(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = "Loading agy...\n{not json\n[1, 2]\n" + _success_stream()
    runner = _Runner(_ok(stdout))

    result, logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 0
    assert result.stdout == "Done."
    warnings = [m for level, m in logs if level == "WARN"]
    assert len(warnings) == 3
    assert "Loading agy..." in warnings[0]
    assert "{not json" in warnings[1]


# Failure mapping


@pytest.mark.parametrize(
    "status",
    ["ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"],
)
def test_non_success_status_fails_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
) -> None:
    stdout = _stream(
        _event(type="result", conversation_id="c4", status=status, error="boom")
    )
    runner = _Runner(_ProcessResult(stdout=stdout, stderr="agy notice", exit_code=0))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 1
    assert result.stderr == f"agy notice\nAntigravity result status={status}: boom"


def test_non_success_status_keeps_nonzero_agy_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(
        _event(type="result", status="ERROR", error={"message": "quota exceeded"})
    )
    runner = _Runner(_ProcessResult(stdout=stdout, stderr="", exit_code=3))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 3
    assert result.stderr == "Antigravity result status=ERROR: quota exceeded"


def test_missing_result_event_fails_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(
        _event(type="init"),
        _event(type="step_update", step_type="agent_response", text_delta="partial"),
    )
    runner = _Runner(_ProcessResult(stdout=stdout, stderr="", exit_code=0))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 1
    assert "no result event" in result.stderr


def test_nonzero_exit_without_result_is_returned_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ProcessResult(stdout="", stderr="agy: crashed", exit_code=7))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 7
    assert result.stderr == "agy: crashed"


# Permission soft-deny

AGY_SOFT_DENY = (
    'jetski: no output produced - a tool required the "command" permission '
    "that headless mode cannot prompt for, so it was auto-denied. Add an "
    "allow-rule under permissions.allow in settings.json (e.g. "
    "command(<target>))."
)


def _soft_deny_errors(logs: list[tuple[str, str]]) -> list[str]:
    return [m for level, m in logs if level == "ERROR"]


def test_soft_deny_with_success_result_fails_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(
        _ProcessResult(stdout=_success_stream(), stderr=AGY_SOFT_DENY, exit_code=0)
    )

    result, logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 1
    first, added = result.stderr.split("\n")
    assert first == AGY_SOFT_DENY
    assert '"command" permission' in added
    assert "`command(<target>)`" in added
    assert "permissions.allow" in added
    [error] = _soft_deny_errors(logs)
    assert error.endswith(added)
    assert error.startswith("Antigravity planner: ")


def test_soft_deny_without_result_event_fails_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ProcessResult(stdout="", stderr=AGY_SOFT_DENY, exit_code=0))

    result, logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 1
    assert "no result event" in result.stderr
    assert "`command(<target>)`" in result.stderr
    assert len(_soft_deny_errors(logs)) == 1


def test_soft_deny_with_non_success_result_keeps_agy_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(_event(type="result", status="ERROR", error="denied"))
    runner = _Runner(_ProcessResult(stdout=stdout, stderr=AGY_SOFT_DENY, exit_code=4))

    result, logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 4
    assert "Antigravity result status=ERROR: denied" in result.stderr
    assert result.stderr.rstrip().endswith(
        "the entry applies to every agy run for that user."
    )
    assert len(_soft_deny_errors(logs)) == 1


def test_clean_run_logs_no_soft_deny(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(
        _ProcessResult(stdout=_success_stream(), stderr="agy notice", exit_code=0)
    )

    result, logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 0
    assert result.stderr == "agy notice"
    assert _soft_deny_errors(logs) == []


# Resume


def test_failed_resume_reruns_once_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(
        _ProcessResult(stdout="", stderr="conversation not found", exit_code=1),
        _ok(_success_stream(conversation_id="conv-new")),
    )

    result, logs = _invoke(
        monkeypatch,
        runner,
        tmp_path,
        use_resume=True,
        session_id="conv-old",
        previous_handoff_sha="aaa",
        current_handoff_sha="bbb",
    )

    assert len(runner.commands) == 2
    assert runner.commands[0][-2:] == ["--conversation", "conv-old"]
    assert "--conversation" not in runner.commands[1]
    assert runner.commands[1][2].startswith("Act as the planner role")
    assert result.exit_code == 0
    assert result.session_id == "conv-new"
    assert result.session_invalidated is True
    assert any(level == "WARN" and "conv-old" in m for level, m in logs)


def test_failed_fresh_rerun_is_not_retried_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(
        _ProcessResult(stdout="", stderr="bad", exit_code=1),
        _ProcessResult(stdout="", stderr="still bad", exit_code=2),
    )

    result, _logs = _invoke(
        monkeypatch, runner, tmp_path, use_resume=True, session_id="conv-old"
    )

    assert len(runner.commands) == 2
    assert result.exit_code == 2
    assert result.session_invalidated is True


def test_fresh_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ProcessResult(stdout="", stderr="bad", exit_code=1))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert len(runner.commands) == 1
    assert result.exit_code == 1
    assert result.session_invalidated is False


def test_soft_deny_on_failed_resume_is_not_rerun_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(
        _ProcessResult(stdout=_success_stream(), stderr=AGY_SOFT_DENY, exit_code=0),
        _ok(),
    )

    result, logs = _invoke(
        monkeypatch, runner, tmp_path, use_resume=True, session_id="conv-old"
    )

    assert len(runner.commands) == 1
    assert result.exit_code == 1
    assert result.session_invalidated is False
    assert "permissions.allow" in result.stderr
    assert not any(level == "WARN" and "conv-old" in m for level, m in logs)


AGY_SIGNED_OUT = "Error: authentication required"


def test_sign_in_failure_gets_a_shared_auth_marker_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(_ProcessResult(stdout="", stderr=AGY_SIGNED_OUT, exit_code=1))

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 1
    first, added = result.stderr.split("\n")
    assert first == AGY_SIGNED_OUT
    assert added == f"agy is not authenticated: {AGY_SIGNED_OUT}"
    assert find_auth_failure(result.stdout, result.stderr) == added


def test_successful_run_mentioning_authentication_required_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(
        _ProcessResult(
            stdout=_success_stream(response="authentication required for deploy"),
            stderr="note: authentication required for deploy",
            exit_code=0,
        )
    )

    result, _logs = _invoke(monkeypatch, runner, tmp_path)

    assert result.exit_code == 0
    assert result.stderr == "note: authentication required for deploy"


def test_sign_in_failure_on_failed_resume_is_not_rerun_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _Runner(
        _ProcessResult(stdout="", stderr=AGY_SIGNED_OUT, exit_code=1),
        _ok(),
    )

    result, logs = _invoke(
        monkeypatch, runner, tmp_path, use_resume=True, session_id="conv-old"
    )

    assert len(runner.commands) == 1
    assert result.exit_code == 1
    assert result.session_invalidated is False
    assert find_auth_failure(result.stderr) is not None
    assert not any(level == "WARN" and "conv-old" in m for level, m in logs)


def test_resumed_success_without_new_id_keeps_the_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = _stream(_event(type="result", status="SUCCESS", response="ok"))
    runner = _Runner(_ok(stdout))

    result, _logs = _invoke(
        monkeypatch, runner, tmp_path, use_resume=True, session_id="conv-1"
    )

    assert result.session_id == "conv-1"


# Fake binary


def test_fake_agy_binary_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_agy.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'event': 'init', 'init': {'cwd': '.'}}))\n"
        "print(json.dumps({'event': 'step_update', 'step_update':"
        " {'conversation_id': 'fake-1', 'step_type': 'agent_response',"
        " 'text_delta': 'hi'}}))\n"
        "print(json.dumps({'event': 'result', 'result':"
        " {'conversation_id': 'fake-1', 'status': 'SUCCESS',"
        " 'response': ' '.join(sys.argv[1:3])}}))\n",
        encoding="utf-8",
    )
    real_run = agent_process._run_command_streaming
    launched: list[list[str]] = []

    def run_fake_agy(command, **kwargs):
        # Swap the agy executable for the interpreter plus the fake script,
        # keep every agy argument, and run it through the real process runner.
        launched.append(list(command))
        return real_run([sys.executable, str(script), *command[1:]], **kwargs)

    monkeypatch.setattr(antigravity, "_run_command_streaming", run_fake_agy)

    result = antigravity.invoke_antigravity(
        "backend",
        tmp_path / "HANDOFF.md",
        repo_root=tmp_path,
    )

    assert result.exit_code == 0, result.stderr
    assert result.session_id == "fake-1"
    assert result.stdout.startswith("-p Act as the backend role")
    assert launched[0][0] == config.ANTIGRAVITY_BINARY
