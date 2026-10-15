from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from llm_handoff import handoff_normalizer
from llm_handoff import orchestrator
from llm_handoff.config import NormalizerConfig
from llm_handoff.normalizer_models import NormalizedNextAgent
from llm_handoff.normalizer_providers import claude as claude_normalizer
from llm_handoff.normalizer_providers import codex as codex_normalizer
from llm_handoff.normalizer_providers import gemini as gemini_normalizer
from llm_handoff.normalizer_providers import openai as openai_normalizer


def test_normalizer_uses_structured_api_path_when_api_key_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[object] = []
    api_keys: list[str | None] = []

    def fake_anthropic(*, api_key: str | None = None) -> object:
        client = object()
        clients.append(client)
        api_keys.append(api_key)
        return client

    def fake_api_normalizer(
        raw_value: str,
        *,
        client: object,
        model: str,
        max_retries: int,
    ) -> str:
        assert raw_value == "implementer"
        assert client is clients[0]
        assert model == "claude-haiku-test"
        assert max_retries == 2
        return "backend"

    def fail_cli(
        raw_value: str,
        *,
        model: str,
        timeout_ms: int,
        repo_root: object = None,
    ) -> str:
        raise AssertionError("CLI fallback should not run when an API key is present.")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")
    monkeypatch.setattr(claude_normalizer, "Anthropic", fake_anthropic)
    monkeypatch.setattr(
        claude_normalizer,
        "_normalize_next_agent_with_instructor",
        fake_api_normalizer,
    )
    monkeypatch.setattr(
        claude_normalizer,
        "_normalize_next_agent_with_claude_cli",
        fail_cli,
    )

    assert (
        handoff_normalizer.normalize_next_agent(
            "implementer",
            model="claude-haiku-test",
        )
        == "backend"
    )
    assert api_keys == ["test-api-key"]


def test_normalizer_uses_cli_path_when_api_key_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_api(*args: object, **kwargs: object) -> str:
        raise AssertionError("API path should not run without an API key.")

    def fake_cli(
        raw_value: str,
        *,
        model: str,
        timeout_ms: int,
        repo_root: object = None,
    ) -> str:
        assert repo_root is None
        assert raw_value == "implementer"
        assert model == "claude-haiku-test"
        assert timeout_ms == 12345
        return "backend"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        claude_normalizer,
        "_normalize_next_agent_with_instructor",
        fail_api,
    )
    monkeypatch.setattr(
        claude_normalizer,
        "_normalize_next_agent_with_claude_cli",
        fake_cli,
    )

    assert (
        handoff_normalizer.normalize_next_agent(
            "implementer",
            model="claude-haiku-test",
            timeout_ms=12345,
        )
        == "backend"
    )


class _FakeGeminiModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return type(
            "GeminiResponse",
            (),
            {"parsed": NormalizedNextAgent(normalized="backend")},
        )()


class _FakeGeminiClient:
    def __init__(self) -> None:
        self.models = _FakeGeminiModels()


def test_normalizer_dispatches_to_configured_gemini_provider() -> None:
    client = _FakeGeminiClient()

    assert (
        handoff_normalizer.normalize_next_agent(
            "implementer",
            provider="gemini",
            model="gemini-flash-test",
            client=client,
        )
        == "backend"
    )
    assert client.models.calls[0]["model"] == "gemini-flash-test"
    assert "implementer" in client.models.calls[0]["contents"]


class _FakeOpenAIResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return type(
            "OpenAIResponse",
            (),
            {"output_parsed": NormalizedNextAgent(normalized="auditor")},
        )()


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = _FakeOpenAIResponses()


def test_normalizer_dispatches_to_configured_openai_provider() -> None:
    client = _FakeOpenAIClient()

    assert (
        handoff_normalizer.normalize_next_agent(
            "review gate",
            provider="openai",
            model="gpt-test",
            client=client,
        )
        == "auditor"
    )
    assert client.responses.calls[0]["model"] == "gpt-test"
    assert client.responses.calls[0]["text_format"] is NormalizedNextAgent
    assert "review gate" in client.responses.calls[0]["input"]


def test_gemini_normalizer_requires_api_key_when_client_is_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="requires GEMINI_API_KEY"):
        gemini_normalizer.normalize_next_agent_with_gemini(
            "implementer",
            model="gemini-flash-test",
            timeout_ms=1000,
        )


def test_openai_normalizer_requires_api_key_when_client_is_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="requires OPENAI_API_KEY"):
        openai_normalizer.normalize_next_agent_with_openai(
            "implementer",
            model="gpt-test",
            timeout_ms=1000,
        )


def test_normalizer_rejects_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported next_agent normalizer provider"):
        handoff_normalizer.normalize_next_agent("implementer", provider="mistral")


class _FakeCodexRun:
    """Stands in for `codex exec`, writing the last message like the CLI."""

    def __init__(
        self,
        *,
        last_message: str = '{"normalized": "backend"}',
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.last_message = last_message
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.schema: dict[str, object] | None = None

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        schema_path = Path(command[command.index("--output-schema") + 1])
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        last_message_path = Path(command[command.index("--output-last-message") + 1])
        last_message_path.write_text(self.last_message, encoding="utf-8")
        return subprocess.CompletedProcess(
            command, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def test_codex_normalizer_runs_read_only_codex_exec_without_openai_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run = _FakeCodexRun()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(codex_normalizer, "_run_codex_process", fake_run)

    assert (
        handoff_normalizer.normalize_next_agent(
            "implementer",
            provider="codex",
            model="gpt-codex-test",
            timeout_ms=12345,
            repo_root=tmp_path,
        )
        == "backend"
    )

    assert len(fake_run.calls) == 1
    command, kwargs = fake_run.calls[0]
    assert command[1:4] == ["exec", "--sandbox", "read-only"]
    assert "--skip-git-repo-check" in command
    assert command[command.index("--model") + 1] == "gpt-codex-test"
    # The raw value travels on stdin, never on the command line.
    assert command[-1] == "-"
    assert not any("implementer" in arg for arg in command)
    assert "implementer" in str(kwargs["input"])
    assert kwargs["timeout"] == pytest.approx(12.345)
    # The turn runs from the private temp directory that holds the schema,
    # never from the target repository.
    cwd = Path(str(kwargs["cwd"]))
    assert cwd != tmp_path
    assert tmp_path not in cwd.parents
    schema_path = Path(command[command.index("--output-schema") + 1])
    assert schema_path.parent == cwd
    assert kwargs.get("shell", False) is False
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert fake_run.schema is not None
    assert fake_run.schema["additionalProperties"] is False
    # The schema and last-message files live outside the target repo.
    assert list(tmp_path.iterdir()) == []


def test_codex_normalizer_withholds_openai_key_from_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run = _FakeCodexRun(last_message='{"normalized": "auditor"}')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-pass")
    monkeypatch.setattr(codex_normalizer, "_run_codex_process", fake_run)

    assert (
        codex_normalizer.normalize_next_agent_with_codex(
            "review gate",
            model="gpt-codex-test",
            timeout_ms=1000,
            api_key="sk-explicit-should-not-pass",
        )
        == "auditor"
    )
    env = fake_run.calls[0][1]["env"]
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in {name.upper() for name in env}
    assert "sk-should-not-pass" not in env.values()
    assert "sk-explicit-should-not-pass" not in env.values()


def test_codex_normalizer_falls_back_to_stdout_when_last_message_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run = _FakeCodexRun(last_message="", stdout='{"normalized": "planner"}\n')
    monkeypatch.setattr(codex_normalizer, "_run_codex_process", fake_run)

    assert (
        codex_normalizer.normalize_next_agent_with_codex(
            "plan it", model="gpt-codex-test", timeout_ms=1000
        )
        == "planner"
    )


@pytest.mark.parametrize(
    "fake_run",
    [
        _FakeCodexRun(last_message="backend, probably"),
        _FakeCodexRun(last_message='{"normalized": "implementer"}'),
        _FakeCodexRun(returncode=1, stderr="not logged in"),
    ],
    ids=["non-json", "off-enum", "nonzero-exit"],
)
def test_codex_normalizer_raises_on_unusable_cli_result(
    fake_run: _FakeCodexRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_normalizer, "_run_codex_process", fake_run)

    with pytest.raises(RuntimeError, match="Codex CLI"):
        codex_normalizer.normalize_next_agent_with_codex(
            "implementer", model="gpt-codex-test", timeout_ms=1000
        )


def test_codex_normalizer_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout_run(command: list[str], **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(codex_normalizer, "_run_codex_process", timeout_run)

    with pytest.raises(RuntimeError, match="Codex CLI next_agent normalization failed"):
        codex_normalizer.normalize_next_agent_with_codex(
            "implementer", model="gpt-codex-test", timeout_ms=1
        )


class _HangingPopen:
    """A Popen stand-in whose first communicate() call times out."""

    instances: list["_HangingPopen"] = []

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = command
        self.kwargs = kwargs
        self.communicate_timeouts: list[object] = []
        self.returncode: int | None = None
        _HangingPopen.instances.append(self)

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        self.communicate_timeouts.append(timeout)
        if len(self.communicate_timeouts) == 1:
            raise subprocess.TimeoutExpired(self.command, timeout or 0)
        return "", ""


def test_codex_process_timeout_kills_the_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[object] = []
    _HangingPopen.instances = []
    monkeypatch.setattr(codex_normalizer.subprocess, "Popen", _HangingPopen)
    monkeypatch.setattr(codex_normalizer, "_kill_process_tree", killed.append)

    with pytest.raises(subprocess.TimeoutExpired):
        codex_normalizer._run_codex_process(
            ["codex", "exec", "-"],
            cwd=".",
            env={},
            input="prompt",
            timeout=2.5,
        )

    (process,) = _HangingPopen.instances
    assert killed == [process]
    assert process.kwargs.get("shell", False) is False
    for key, value in codex_normalizer._process_group_popen_kwargs().items():
        assert process.kwargs[key] == value
    # The post-kill drain is bounded, so the call cannot outlive the deadline
    # by more than the drain window.
    assert process.communicate_timeouts == [
        2.5,
        codex_normalizer._POST_KILL_DRAIN_TIMEOUT_SECONDS,
    ]


def test_codex_process_timeout_bounds_a_real_hung_child() -> None:
    script = "import sys, time; sys.stdin.read(); time.sleep(60)"
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        codex_normalizer._run_codex_process(
            [sys.executable, "-c", script],
            cwd=".",
            env=dict(os.environ),
            input="prompt",
            timeout=0.5,
        )

    assert time.monotonic() - started < 30


_FREEFORM_HANDOFF ="""---
next_agent: the implementer please
reason: "Work assigned."
producer: planner
---

# Handoff
"""


def _assert_codex_failure_fails_closed(tmp_path: Path) -> None:
    handoff_path = tmp_path / "HANDOFF.md"
    handoff_path.write_text(_FREEFORM_HANDOFF, encoding="utf-8")
    logs: list[tuple[str, str]] = []

    result = orchestrator._normalize_handoff_next_agent_file(
        handoff_path,
        _FREEFORM_HANDOFF,
        NormalizerConfig(provider="codex", timeout_ms=1000),
        lambda level, message: logs.append((level, message)),
        repo_root=tmp_path,
    )

    assert result == _FREEFORM_HANDOFF
    assert handoff_path.read_text(encoding="utf-8") == _FREEFORM_HANDOFF
    assert any(
        level == "WARN" and "next_agent normalization failed" in message
        for level, message in logs
    ), logs


def test_codex_normalizer_missing_binary_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real subprocess launch of a binary that does not exist.
    monkeypatch.setattr(
        codex_normalizer.config,
        "CODEX_BINARY",
        "llm-handoff-test-missing-codex-binary",
    )

    with pytest.raises(RuntimeError, match="Codex CLI next_agent normalization failed"):
        codex_normalizer.normalize_next_agent_with_codex(
            "implementer", model="gpt-codex-test", timeout_ms=5000
        )
    _assert_codex_failure_fails_closed(tmp_path)


def test_codex_normalizer_unparseable_output_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_normalizer,
        "_run_codex_process",
        _FakeCodexRun(last_message="I think the backend role."),
    )

    _assert_codex_failure_fails_closed(tmp_path)
