from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

ValidAgent = Literal[
    "auditor",
    "backend",
    "finalizer",
    "frontend",
    "planner",
    "validator",
    "user",
    "unknown",
]


class NormalizedNextAgent(BaseModel):
    normalized: ValidAgent = Field(
        description=(
            "Canonical enum value for the given freeform next_agent input. "
            "Return 'unknown' if the input does not plausibly match any valid "
            "agent; do not guess when ambiguous."
        )
    )


def normalizer_prompt(raw_value: str) -> str:
    return (
        "Given this freeform `next_agent` value from a HANDOFF.md "
        f"frontmatter block: {raw_value!r}\n\n"
        "Return exactly one canonical enum value that best matches the intended "
        "receiving agent. Valid values are: "
        f"{', '.join(get_args(ValidAgent))}. Return 'unknown' if nothing "
        "plausibly matches or the value is ambiguous."
    )
