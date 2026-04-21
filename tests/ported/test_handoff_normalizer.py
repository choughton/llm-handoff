from __future__ import annotations

import json
import subprocess

import pytest
from pydantic import ValidationError

import llm_handoff.handoff_normalizer as handoff_normalizer


class _FakeMessages:
    def __init__(self, normalized: str) -> None:
        self.normalized = normalized
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object):
        self.calls.append(kwargs)
        response_model = kwargs["response_model"]
        return response_model(normalized=self.normalized)


class _FakeInstructedClient:
    def __init__(self, normalized: str) -> None:
        self.messages = _FakeMessages(normalized)


def test_normalize_next_agent_passes_exact_canonical_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_from_anthropic(_client: object) -> object:
        raise AssertionError("exact canonical next_agent should not call Instructor")

    monkeypatch.setattr(
        handoff_normalizer.instructor, "from_anthropic", fail_from_anthropic
    )

    assert handoff_normalizer.normalize_next_agent("auditor") == "auditor"
    assert handoff_normalizer.normalize_next_agent("claude-audit") == "auditor"
    assert handoff_normalizer.normalize_next_agent("Codex") == "backend"


@pytest.mark.parametrize(
        ("freeform", "canonical"),
        [
            ("Claude Code (Auditor)", "auditor"),
            ("gemini pe", "planner"),
            ("frontend role", "frontend"),
            ("ledger", "finalizer"),
    ],
)
def test_normalize_next_agent_uses_instructor_for_freeform_values(
    monkeypatch: pytest.MonkeyPatch,
    freeform: str,
    canonical: str,
) -> None:
    fake_instructed = _FakeInstructedClient(canonical)

    monkeypatch.setattr(
        handoff_normalizer.instructor,
        "from_anthropic",
        lambda _client: fake_instructed,
    )

    assert (
        handoff_normalizer.normalize_next_agent(
            freeform,
            client=object(),
            max_retries=3,
        )
        == canonical
    )
    [call] = fake_instructed.messages.calls
    assert call["model"] == "claude-haiku-4-5"
    assert call["response_model"] is handoff_normalizer.NormalizedNextAgent
    assert call["max_retries"] == 3
    assert freeform in call["messages"][0]["content"]


@pytest.mark.parametrize("freeform", ["", "   "])
def test_normalize_next_agent_returns_unknown_for_blank_without_llm(
    monkeypatch: pytest.MonkeyPatch,
    freeform: str,
) -> None:
    def fail_from_anthropic(_client: object) -> object:
        raise AssertionError("blank next_agent should not call Instructor")

    monkeypatch.setattr(
        handoff_normalizer.instructor, "from_anthropic", fail_from_anthropic
    )

    assert handoff_normalizer.normalize_next_agent(freeform) == "unknown"


@pytest.mark.parametrize("freeform", ["banana", "not a thing"])
def test_normalize_next_agent_can_return_unknown_from_instructor(
    monkeypatch: pytest.MonkeyPatch,
    freeform: str,
) -> None:
    fake_instructed = _FakeInstructedClient("unknown")
    monkeypatch.setattr(
        handoff_normalizer.instructor,
        "from_anthropic",
        lambda _client: fake_instructed,
    )

    assert (
        handoff_normalizer.normalize_next_agent(freeform, client=object()) == "unknown"
    )


def test_normalize_next_agent_uses_claude_cli_oauth_without_sdk_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_from_anthropic(_client: object) -> object:
        raise AssertionError("missing SDK credentials should use Claude CLI OAuth")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append({"command": command, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "result": "Done. The normalized agent value is `auditor`.",
                    "structured_output": {"normalized": "auditor"},
                }
            ),
            stderr="",
        )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        handoff_normalizer.instructor, "from_anthropic", fake_from_anthropic
    )
    monkeypatch.setattr(handoff_normalizer.subprocess, "run", fake_run)

    assert handoff_normalizer.normalize_next_agent("Claude Code (Auditor)") == "auditor"
    [call] = calls
    command = call["command"]
    kwargs = call["kwargs"]
    assert isinstance(command, list)
    assert "--model" in command
    assert "claude-haiku-4-5" in command
    assert "--json-schema" in command
    assert "--bare" not in command
    assert isinstance(kwargs["env"], dict)
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in kwargs["env"]


def test_normalize_next_agent_falls_back_to_claude_cli_when_sdk_auth_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class _FailingMessages:
        @staticmethod
        def create(**_kwargs: object) -> object:
            raise RuntimeError("Could not resolve authentication method.")

    class _FailingInstructedClient:
        messages = _FailingMessages()

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"result": json.dumps({"normalized": "auditor"})}),
            stderr="",
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-key")
    monkeypatch.setattr(
        handoff_normalizer.instructor,
        "from_anthropic",
        lambda _client: _FailingInstructedClient(),
    )
    monkeypatch.setattr(handoff_normalizer.subprocess, "run", fake_run)

    assert (
        handoff_normalizer.normalize_next_agent("Claude Code (Auditor)") == "auditor"
    )
    assert len(calls) == 1


def test_normalized_next_agent_schema_rejects_values_outside_literal_enum() -> None:
    with pytest.raises(ValidationError):
        handoff_normalizer.NormalizedNextAgent(normalized="Claude Code")
