"""One log-file line per agent run with its token usage and cost.

Lines are logged at ``DEBUG``, which ``DispatchLogger`` writes to the log file
only once startup is over, so usage stays out of the console. The format is
``usage: <agent> provider=<p> key=value ...`` so a log can be summed with
``grep 'usage:'``. Fields a provider does not report are left out.
"""

from __future__ import annotations

from collections.abc import Mapping


USAGE_FIELD_ORDER = (
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_input_tokens",
    "thinking_tokens",
    "total_tokens",
    "cost_usd",
    "num_turns",
    "duration_ms",
)


def format_usage_line(
    agent_name: str,
    provider: str,
    fields: Mapping[str, object],
) -> str:
    """Return ``usage: <agent> provider=<p> ...`` with known fields first."""

    parts = [f"usage: {agent_name}", f"provider={provider}"]
    ordered = [key for key in USAGE_FIELD_ORDER if key in fields]
    ordered += sorted(key for key in fields if key not in USAGE_FIELD_ORDER)
    for key in ordered:
        value = fields[key]
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


def usage_fields_from_claude_result(event: Mapping[str, object]) -> dict[str, object]:
    """Pull usage and cost out of a Claude Code ``result`` event or JSON output."""

    fields: dict[str, object] = {}
    usage = event.get("usage")
    if isinstance(usage, Mapping):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int):
                fields[key] = value
    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        fields["cost_usd"] = cost
    for source, target in (("num_turns", "num_turns"), ("duration_ms", "duration_ms")):
        value = event.get(source)
        if isinstance(value, int):
            fields[target] = value
    return fields


def usage_fields_from_codex_turn(event: Mapping[str, object]) -> dict[str, object]:
    """Pull token counts out of a Codex ``turn.completed`` event (no cost)."""

    fields: dict[str, object] = {}
    usage = event.get("usage")
    if isinstance(usage, Mapping):
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                fields[key] = value
    return fields
