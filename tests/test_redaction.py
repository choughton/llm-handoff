from __future__ import annotations

from unittest.mock import Mock, call

import pytest

from llm_handoff.agent_providers.claude import _ClaudeStreamJsonMonitor
from llm_handoff.agent_providers.codex import _CodexJsonMonitor
from llm_handoff.redaction import REDACTED, redact_secrets, redact_stderr_summary


# Planted values are built at runtime so the test file itself holds no
# literal secret-shaped token.
OPENAI_KEY = "sk-proj-" + "Qw3rT" * 8
ANTHROPIC_KEY = "sk-ant-" + "Yu7iO" * 4
GOOGLE_KEY = "AIza" + "Pl5kJ" * 7
GITHUB_KEYS = [prefix + "Hg2fD" * 7 + "S" for prefix in ("ghp_", "gho_", "ghs_", "ghu_")]
GITHUB_PAT = "github_pat_" + "Mn6bV" * 6 + "_" + "Cx1zA" * 4


@pytest.mark.parametrize(
    ("text", "planted", "expected"),
    [
        (
            f"auth failed for {OPENAI_KEY} today",
            OPENAI_KEY,
            f"auth failed for {REDACTED} today",
        ),
        (
            f"auth failed for {ANTHROPIC_KEY} today",
            ANTHROPIC_KEY,
            f"auth failed for {REDACTED} today",
        ),
        (
            f"google said key={GOOGLE_KEY} is invalid",
            GOOGLE_KEY,
            f"google said key={REDACTED} is invalid",
        ),
        (
            "header Authorization: Bearer abc.def.ghi was rejected",
            "abc.def.ghi",
            f"header Authorization: Bearer {REDACTED} was rejected",
        ),
        (
            "header authorization: bearer abc.def.ghi was rejected",
            "abc.def.ghi",
            f"header authorization: bearer {REDACTED} was rejected",
        ),
        (
            f"clone with {GITHUB_PAT} failed",
            GITHUB_PAT,
            f"clone with {REDACTED} failed",
        ),
        (
            f"env OPENAI_API_KEY={OPENAI_KEY} loaded",
            OPENAI_KEY,
            f"env OPENAI_API_KEY={REDACTED} loaded",
        ),
        (
            "config MY_SERVICE_TOKEN: xyz123 loaded",
            "xyz123",
            f"config MY_SERVICE_TOKEN: {REDACTED} loaded",
        ),
        (
            'config my_app_secret="hunter2pass" loaded',
            "hunter2pass",
            f'config my_app_secret="{REDACTED}" loaded',
        ),
        (
            "config DEPLOY_KEY='abc123def' loaded",
            "abc123def",
            f"config DEPLOY_KEY='{REDACTED}' loaded",
        ),
    ],
)
def test_redact_secrets_masks_each_secret_shape(
    text: str, planted: str, expected: str
) -> None:
    redacted = redact_secrets(text)

    assert planted not in redacted
    assert redacted == expected


@pytest.mark.parametrize("token", GITHUB_KEYS)
def test_redact_secrets_masks_github_token_prefixes(token: str) -> None:
    redacted = redact_secrets(f"git push with {token} denied")

    assert token not in redacted
    assert redacted == f"git push with {REDACTED} denied"


def test_redact_secrets_masks_every_planted_value_in_one_blob() -> None:
    blob = "\n".join(
        [
            f"Error: OPENAI_API_KEY={OPENAI_KEY}",
            f"google key {GOOGLE_KEY}",
            "Authorization: Bearer abc.def.ghi",
            f"github {GITHUB_KEYS[0]}",
            "MY_SERVICE_TOKEN: xyz123",
            "exit code 2",
        ]
    )

    redacted = redact_secrets(blob)

    for planted in (OPENAI_KEY, GOOGLE_KEY, "abc.def.ghi", GITHUB_KEYS[0], "xyz123"):
        assert planted not in redacted
    for word in ("Error:", "OPENAI_API_KEY=", "google key", "Bearer", "github",
                 "MY_SERVICE_TOKEN:", "exit code 2"):
        assert word in redacted


@pytest.mark.parametrize(
    "text",
    [
        "exit code 2",
        r"C:\Users\someone\repo\llm_handoff\audit_review.py",
        "/home/someone/repo/tests/test_redaction.py:12",
        "key=value",
        "mode=strict timeout=30",
        "MONKEY=banana",
        "sk-short",
        "tasks-in-progress-and-pending-list",
        "the token was missing",
        "ghp_ alone is not a token",
        "",
    ],
)
def test_redact_secrets_leaves_ordinary_text_unchanged(text: str) -> None:
    assert redact_secrets(text) == text


def test_redact_stderr_summary_collapses_whitespace() -> None:
    assert redact_stderr_summary("line one\n\n  line\ttwo\r\n") == "line one line two"
    assert redact_stderr_summary("   \n ") == ""


def test_redact_stderr_summary_caps_keeping_the_tail() -> None:
    text = "x" * 500 + "THE ERROR"

    summary = redact_stderr_summary(text)

    assert len(summary) == 300
    assert summary.startswith("...")
    assert summary.endswith("THE ERROR")
    assert redact_stderr_summary(text, limit=20) == "..." + text[-17:]


def test_redact_stderr_summary_leaves_short_text_uncut() -> None:
    assert redact_stderr_summary("short error", limit=11) == "short error"


def test_redact_stderr_summary_never_emits_partial_secret() -> None:
    # Sweep the cap across every position the raw key occupied: a cut on the
    # unredacted text would leave a fragment of the key; redacting first
    # must not.
    tail = " final error line"
    text = "y" * 400 + " " + OPENAI_KEY + tail
    for limit in range(len(tail) + 3, len(tail) + len(OPENAI_KEY) + 5):
        summary = redact_stderr_summary(text, limit=limit)

        assert len(summary) <= limit
        assert "Qw3rT" not in summary
        assert "sk-proj" not in summary
        assert summary.endswith(tail)


def test_redact_stderr_summary_redacts_default_cap() -> None:
    text = "noise " * 100 + f"OPENAI_API_KEY={OPENAI_KEY}"

    summary = redact_stderr_summary(text)

    assert len(summary) == 300
    assert summary.endswith(f"OPENAI_API_KEY={REDACTED}")
    assert "Qw3rT" not in summary


def test_claude_stream_monitor_redacts_stderr_lines() -> None:
    log = Mock()
    monitor = _ClaudeStreamJsonMonitor(agent_name="Claude", log=log)

    monitor.consume_stderr(f"warning: OPENAI_API_KEY={OPENAI_KEY}\n")

    assert log.call_args_list == [
        call("INFO", f"Claude stderr: warning: OPENAI_API_KEY={REDACTED}")
    ]


def test_codex_json_monitor_redacts_stderr_lines() -> None:
    log = Mock()
    monitor = _CodexJsonMonitor(agent_name="Codex", log=log)

    monitor.consume_stderr(f"Error: bad key {GOOGLE_KEY}\n")
    monitor.consume_stderr(f"Retrying with Bearer {OPENAI_KEY}\n")

    messages = [entry.args for entry in log.call_args_list]
    assert ("WARN", f"Codex stderr: Error: bad key {REDACTED}") in messages
    assert all(GOOGLE_KEY not in text and OPENAI_KEY not in text for _level, text in messages)
