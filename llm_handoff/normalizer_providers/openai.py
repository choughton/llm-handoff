from __future__ import annotations

import os

from llm_handoff.normalizer_models import NormalizedNextAgent, normalizer_prompt


def normalize_next_agent_with_openai(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
    api_key: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
) -> str:
    resolved_client = client or _build_openai_api_client(
        api_key=api_key,
        timeout_ms=timeout_ms,
        max_retries=max_retries,
    )
    return _normalize_next_agent_with_openai_api(
        raw_value,
        client=resolved_client,
        model=model,
    )


def _normalize_next_agent_with_openai_api(
    raw_value: str,
    *,
    client: object,
    model: str,
) -> str:
    response = client.responses.parse(
        model=model,
        input=normalizer_prompt(raw_value),
        text_format=NormalizedNextAgent,
        max_output_tokens=64,
        temperature=0,
    )
    return _coerce_openai_normalization_output(response)


def _build_openai_api_client(
    *,
    api_key: str | None = None,
    timeout_ms: int,
    max_retries: int,
) -> object:
    effective_api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not effective_api_key:
        raise RuntimeError("OpenAI next_agent normalization requires OPENAI_API_KEY.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI next_agent normalization requires the openai package."
        ) from exc

    return OpenAI(
        api_key=effective_api_key,
        timeout=timeout_ms / 1000,
        max_retries=max_retries,
    )


def _coerce_openai_normalization_output(response: object) -> str:
    parsed = getattr(response, "output_parsed", None)
    if parsed is not None:
        return NormalizedNextAgent.model_validate(parsed).normalized

    output = getattr(response, "output", None)
    if isinstance(output, list):
        for item in output:
            content = getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for part in content:
                parsed_part = getattr(part, "parsed", None)
                if parsed_part is not None:
                    return NormalizedNextAgent.model_validate(parsed_part).normalized
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    return NormalizedNextAgent.model_validate_json(text).normalized

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return NormalizedNextAgent.model_validate_json(output_text).normalized

    raise RuntimeError("OpenAI normalizer returned no structured output.")
