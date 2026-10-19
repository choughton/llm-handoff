"""Mask secret-shaped values in provider output before it is logged.

Provider CLIs sometimes echo environment values, request headers, or API keys
on stderr when they fail. The dispatcher prints stderr into its log and into
reviewer error and pause messages, so every such path routes the text through
``redact_secrets`` (or ``redact_stderr_summary`` for one-line summaries).

The patterns are deliberately conservative: they mask well-known token
prefixes and ``NAME=value`` pairs whose name marks a secret. They are a safety
net, not a guarantee that arbitrary secrets are caught. Raw stderr stored on
result objects and in per-run stderr files is left unchanged.
"""

from __future__ import annotations

import re


REDACTED = "[REDACTED]"

# Token shapes masked in full.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI and Anthropic style keys: sk-..., sk-proj-..., sk-ant-...
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"),
    # Google API keys.
    re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{30,}"),
    # GitHub personal, OAuth, server, and user tokens.
    re.compile(r"(?<![A-Za-z0-9_])(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{16,}"),
)

# ``Bearer <token>``: the word stays, the token is masked.
_BEARER_RE = re.compile(r"\b(Bearer)([ \t]+)[^\s'\"]+", re.IGNORECASE)

# ``NAME=value`` / ``NAME: value`` where NAME ends in _KEY, _TOKEN, _SECRET.
# The name may be quoted, as in JSON or a Python mapping
# (``{"NAME": "value"}``). The name, separator, and any quotes around the
# value stay. A quoted value is masked through its matching closing quote on
# the same line, so a value with spaces (``NAME="alpha beta"``) is masked in
# full; backslash escapes inside it do not end it. An unquoted value, or a
# quote with no closing match on the line, ends at whitespace or a quote.
_NAMED_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<name>[A-Za-z0-9_]*(?:_KEY|_TOKEN|_SECRET))"
    r"(?P<sep>[\"']?[ \t]*[=:][ \t]*)"
    r"(?:"
    r"(?P<quote>[\"'])(?:\\[^\r\n]|(?!(?P=quote))[^\\\r\n])+(?=(?P=quote))"
    r"|"
    r"(?P<bare>[\"']?)[^\s\"']+"
    r")",
    re.IGNORECASE,
)


def _mask_named_secret(match: re.Match[str]) -> str:
    opening = match.group("quote") or match.group("bare") or ""
    return f"{match.group('name')}{match.group('sep')}{opening}{REDACTED}"


def redact_secrets(text: str) -> str:
    """Return ``text`` with secret-shaped values replaced by ``[REDACTED]``.

    Text that matches no pattern is returned unchanged.
    """

    if not text:
        return text
    redacted = text
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    redacted = _BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", redacted)
    redacted = _NAMED_SECRET_RE.sub(_mask_named_secret, redacted)
    return redacted


def redact_stderr_summary(text: str, limit: int = 300) -> str:
    """Return a redacted, single-line summary of ``text`` of at most ``limit``
    characters.

    Whitespace collapses to single spaces. When the result is longer than
    ``limit``, the tail is kept (the end of stderr usually holds the error)
    and prefixed with ``...``. Redaction runs before the cap, so the cap can
    never leave an unmasked fragment of a secret. A ``limit`` of three or
    less leaves no room for a tail, so a cut result is the first ``limit``
    characters of ``...``; a negative ``limit`` is treated as zero.
    """

    limit = max(limit, 0)
    collapsed = " ".join(redact_secrets(str(text or "")).split())
    if len(collapsed) <= limit:
        return collapsed
    marker = "..."
    if limit <= len(marker):
        # No room for any tail: emit as much of the marker as fits so the
        # result still never exceeds ``limit``.
        return marker[:limit]
    return marker + collapsed[len(collapsed) - (limit - len(marker)) :]
