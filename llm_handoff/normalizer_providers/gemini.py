from __future__ import annotations

import os

from llm_handoff.normalizer_models import NormalizedNextAgent, normalizer_prompt


def normalize_next_agent_with_gemini(
    raw_value: str,
    *,
    model: str,
    timeout_ms: int,
    api_key: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
) -> str:
    del max_retries
    resolved_client = client or _build_gemini_api_client(
        api_key=api_key,
        timeout_ms=timeout_ms,
    )
    return _normalize_next_agent_with_gemini_api(
        raw_value,
        client=resolved_client,
        model=model,
    )


def _normalize_next_agent_with_gemini_api(
    raw_value: str,
    *,
    client: object,
    model: str,
) -> str:
    response = client.models.generate_content(
        model=model,
        contents=normalizer_prompt(raw_value),
        config=_gemini_generation_config(),
    )
    return _coerce_gemini_normalization_output(response)


def _build_gemini_api_client(
    *,
    api_key: str | None = None,
    timeout_ms: int,
) -> object:
    effective_api_key = (
        api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    if not effective_api_key:
        raise RuntimeError(
            "Gemini next_agent normalization requires GEMINI_API_KEY or GOOGLE_API_KEY."
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "Gemini next_agent normalization requires the google-genai package."
        ) from exc

    return genai.Client(
        api_key=effective_api_key,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )


def _gemini_generation_config() -> object:
    try:
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "Gemini next_agent normalization requires the google-genai package."
        ) from exc

    return types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=64,
        response_mime_type="application/json",
        response_schema=NormalizedNextAgent,
    )


def _coerce_gemini_normalization_output(response: object) -> str:
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        return NormalizedNextAgent.model_validate(parsed).normalized

    text = getattr(response, "text", None)
    if isinstance(text, str):
        return NormalizedNextAgent.model_validate_json(text).normalized

    raise RuntimeError("Gemini normalizer returned no structured output.")
