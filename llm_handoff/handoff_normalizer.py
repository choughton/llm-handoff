from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Protocol

from llm_handoff import config
from llm_handoff.normalizer_models import NormalizedNextAgent, normalizer_prompt
from llm_handoff.normalizer_providers.claude import normalize_next_agent_with_claude
from llm_handoff.roles import (
    CANONICAL_NEXT_AGENT_ROLES,
    is_legacy_next_agent_alias,
    normalize_next_agent_value,
)


DEFAULT_NORMALIZER_PROVIDER = config.NORMALIZER_PROVIDER
DEFAULT_NORMALIZER_MODEL = config.NORMALIZER_MODEL
CANONICAL_NEXT_AGENTS = tuple(agent for agent in CANONICAL_NEXT_AGENT_ROLES)
CANONICAL_NEXT_AGENT_SET = frozenset(CANONICAL_NEXT_AGENTS)
_NEXT_AGENT_LINE_RE = re.compile(r"^(\s*next_agent\s*:\s*).*$")


class NormalizerAdapter(Protocol):
    def __call__(
        self,
        raw_value: str,
        *,
        model: str,
        timeout_ms: int,
        api_key: str | None = None,
        client: object | None = None,
        max_retries: int = 2,
    ) -> str: ...


_NORMALIZER_PROVIDER_ADAPTERS: dict[str, NormalizerAdapter] = {
    "claude": normalize_next_agent_with_claude,
}
_normalizer_prompt = normalizer_prompt

__all__ = [
    "HandoffNextAgentNormalization",
    "NormalizedNextAgent",
    "normalize_handoff_next_agent_text",
    "normalize_next_agent",
]


@dataclass(frozen=True)
class HandoffNextAgentNormalization:
    content: str
    original: str | None = None
    normalized: str | None = None
    rewritten: bool = False
    unknown: bool = False


def normalize_next_agent(
    freeform: str,
    *,
    provider: str = DEFAULT_NORMALIZER_PROVIDER,
    model: str = DEFAULT_NORMALIZER_MODEL,
    timeout_ms: int = config.NORMALIZER_TIMEOUT_MS,
    api_key: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
) -> str:
    """Return a canonical next_agent enum value, or 'unknown' if no match."""

    raw_value = freeform.strip()
    if not raw_value:
        return "unknown"
    if raw_value in CANONICAL_NEXT_AGENT_SET:
        return raw_value
    if is_legacy_next_agent_alias(raw_value):
        normalized = normalize_next_agent_value(raw_value)
        if normalized is not None:
            return normalized

    adapter = _NORMALIZER_PROVIDER_ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"Unsupported next_agent normalizer provider `{provider}`.")

    return adapter(
        raw_value,
        model=model,
        timeout_ms=timeout_ms,
        api_key=api_key,
        client=client,
        max_retries=max_retries,
    )


def normalize_handoff_next_agent_text(
    handoff_content: str,
    *,
    normalizer: Callable[[str], str] = normalize_next_agent,
) -> HandoffNextAgentNormalization:
    lines = handoff_content.splitlines()
    if not lines or lines[0].strip() != "---":
        return HandoffNextAgentNormalization(content=handoff_content)

    end_index = _frontmatter_end_index(lines)
    if end_index is None:
        return HandoffNextAgentNormalization(content=handoff_content)

    next_agent_index, original = _next_agent_line(lines, end_index)
    if next_agent_index is None or original is None:
        return HandoffNextAgentNormalization(content=handoff_content)
    if original in CANONICAL_NEXT_AGENT_SET:
        return HandoffNextAgentNormalization(
            content=handoff_content,
            original=original,
            normalized=original,
        )

    normalized = normalizer(original)
    if normalized == "unknown":
        return HandoffNextAgentNormalization(
            content=handoff_content,
            original=original,
            normalized=normalized,
            unknown=True,
        )
    if normalized not in CANONICAL_NEXT_AGENT_SET:
        raise ValueError(
            f"next_agent normalizer returned unsupported value `{normalized}`."
        )

    rewritten_lines = [*lines]
    match = _NEXT_AGENT_LINE_RE.match(rewritten_lines[next_agent_index])
    if match is None:
        return HandoffNextAgentNormalization(content=handoff_content)
    rewritten_lines[next_agent_index] = f"{match.group(1)}{normalized}"
    rewritten_content = "\n".join(rewritten_lines)
    if handoff_content.endswith("\n"):
        rewritten_content += "\n"

    return HandoffNextAgentNormalization(
        content=rewritten_content,
        original=original,
        normalized=normalized,
        rewritten=True,
    )


def _frontmatter_end_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return index
    return None


def _next_agent_line(
    lines: list[str],
    frontmatter_end_index: int,
) -> tuple[int | None, str | None]:
    for index in range(1, frontmatter_end_index):
        line = lines[index]
        match = _NEXT_AGENT_LINE_RE.match(line)
        if match is None:
            continue
        return index, line.split(":", 1)[1].strip().strip("'\"")
    return None, None
