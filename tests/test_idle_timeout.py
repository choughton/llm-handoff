from __future__ import annotations

import io
from pathlib import Path
import queue
import sys
import time
from typing import Any

from pydantic import ValidationError
import pytest

from llm_handoff import agent_process
from llm_handoff.agent_providers import claude as claude_provider
from llm_handoff.agent_providers import codex as codex_provider
from llm_handoff.agent_types import _ProcessResult
from llm_handoff.config import AgentConfig


def _python_command(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def test_idle_timeout_kills_a_silent_process_before_the_full_timeout(
    tmp_path: Path,
) -> None:
    start = time.monotonic()

    result = agent_process._run_command_streaming(
        _python_command("import time; print('started', flush=True); time.sleep(60)"),
        cwd=tmp_path,
        timeout_ms=60_000,
        idle_timeout_ms=1_000,
    )

    assert time.monotonic() - start < 30
    assert result.exit_code == 1
    assert result.stdout.strip() == "started"
    assert result.stderr.startswith(
        "Process timed out after 1 seconds without output (idle_timeout_ms)."
    )


def test_idle_timeout_resets_while_the_process_keeps_writing(
    tmp_path: Path,
) -> None:
    # Total runtime (about 2 seconds) is well past the 1 second idle limit,
    # but no single gap between lines reaches it.
    result = agent_process._run_command_streaming(
        _python_command(
            "import time\n"
            "for i in range(8):\n"
            "    print(f'line {i}', flush=True)\n"
            "    time.sleep(0.25)\n"
        ),
        cwd=tmp_path,
        timeout_ms=60_000,
        idle_timeout_ms=1_000,
    )

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [f"line {i}" for i in range(8)]
    assert "timed out" not in result.stderr


def test_idle_timeout_counts_output_written_without_a_newline(
    tmp_path: Path,
) -> None:
    # Token streams and progress output flush partial lines; they are
    # activity even though no complete line arrives for about 2 seconds.
    result = agent_process._run_command_streaming(
        _python_command(
            "import sys, time\n"
            "for i in range(8):\n"
            "    sys.stdout.write(f'tok{i} ')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.25)\n"
        ),
        cwd=tmp_path,
        timeout_ms=60_000,
        idle_timeout_ms=1_000,
    )

    assert result.exit_code == 0
    assert result.stdout == "".join(f"tok{i} " for i in range(8))
    assert "timed out" not in result.stderr


def test_reader_queues_the_same_lines_as_text_mode_readline() -> None:
    data = "one\r\ntwo\rthree\nnon-ascii é\npartial".encode("utf-8")
    expected = list(
        iter(io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").readline, "")
    )
    stream_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    agent_process._read_process_stream(
        io.TextIOWrapper(
            io.BufferedReader(io.BytesIO(data)),
            encoding="utf-8",
            errors="replace",
        ),
        "stdout",
        stream_queue,
    )

    chunks = [chunk for _, chunk in stream_queue.queue if chunk is not None]
    assert chunks == expected == ["one\n", "two\n", "three\n", "non-ascii é\n", "partial"]


def test_without_idle_timeout_a_silent_process_runs_to_completion(
    tmp_path: Path,
) -> None:
    result = agent_process._run_command_streaming(
        _python_command("import time; time.sleep(1.5); print('done', flush=True)"),
        cwd=tmp_path,
        timeout_ms=60_000,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "done"


def test_idle_timeout_message_is_classified_as_a_failover_timeout() -> None:
    failover = pytest.importorskip("llm_handoff.failover")
    stderr = "Process timed out after 300 seconds without output (idle_timeout_ms)."

    assert (
        failover.classify_fatal_failure(exit_code=1, stdout="", stderr=stderr)
        == "timeout"
    )


def test_agent_config_idle_timeout_defaults_off_and_rejects_non_positive() -> None:
    assert AgentConfig(provider="codex").idle_timeout_ms is None
    assert AgentConfig(provider="codex", idle_timeout_ms=1).idle_timeout_ms == 1
    with pytest.raises(ValidationError, match="idle_timeout_ms must be at least 1"):
        AgentConfig(provider="codex", idle_timeout_ms=0)


def _capturing_runner(calls: list[dict[str, Any]]):
    def fake_run_command_streaming(command: list[str], **kwargs: Any) -> _ProcessResult:
        calls.append(kwargs)
        return _ProcessResult(stdout="", stderr="", exit_code=0)

    return fake_run_command_streaming


def test_claude_adapter_passes_idle_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_provider, "_run_command_streaming", _capturing_runner(calls)
    )

    claude_provider.invoke_claude_subagent(
        "auditor",
        "prompt",
        repo_root=tmp_path,
        agent_config=AgentConfig(provider="claude", idle_timeout_ms=4_000),
    )

    assert [call["idle_timeout_ms"] for call in calls] == [4_000]


def test_codex_adapter_passes_idle_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        codex_provider, "_run_command_streaming", _capturing_runner(calls)
    )

    codex_provider.invoke_codex(
        Path("docs/handoff/HANDOFF.md"),
        repo_root=tmp_path,
        agent_config=AgentConfig(provider="codex", idle_timeout_ms=5_000),
    )

    assert [call["idle_timeout_ms"] for call in calls] == [5_000]


def test_adapters_leave_idle_timeout_off_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        codex_provider, "_run_command_streaming", _capturing_runner(calls)
    )

    codex_provider.invoke_codex(Path("docs/handoff/HANDOFF.md"), repo_root=tmp_path)

    assert [call["idle_timeout_ms"] for call in calls] == [None]
