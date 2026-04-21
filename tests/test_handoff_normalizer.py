from __future__ import annotations

import pytest

from llm_handoff import handoff_normalizer
from llm_handoff.normalizer_models import NormalizedNextAgent
from llm_handoff.normalizer_providers import claude as claude_normalizer
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

    def fail_cli(raw_value: str, *, model: str, timeout_ms: int) -> str:
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

    def fake_cli(raw_value: str, *, model: str, timeout_ms: int) -> str:
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
        handoff_normalizer.normalize_next_agent("implementer", provider="codex")
