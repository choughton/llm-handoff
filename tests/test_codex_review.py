from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from llm_handoff.codex_review import (
    LAST_MESSAGE_PATH,
    LocalFinding,
    build_review_command,
    parse_review_message,
    run_codex_review,
)


DASH = "\u2014"

# The layout `codex exec review` printed for a two-finding review.
TWO_FINDINGS = f"""The patch drops the empty-input guard and miscounts the slice.

Full review comments:

- [P2] Restore the empty-input guard in mean {DASH} {{root}}/src/stats.py:2-2
  Calling `mean([])` now raises `ZeroDivisionError`.
  Restore the guard.

- [P1] Correct the slice start in last_n {DASH} {{root}}/src/stats.py:7-9
  `last_n([1, 2, 3], 1)` returns `[2, 3]`.
"""

# A clean review is the overall explanation alone.
CLEAN = (
    "The change escapes untrusted command text in diagnostic remedies. No "
    "actionable regressions were identified."
)

# Writes the final message, like `codex exec review --output-last-message`.
# argv[1] is a JSON state file: {"replies": [{"exit", "message", "stderr"}]};
# each run takes the next reply and records its arguments.
FAKE_CODEX = r"""
import json, sys
state_path, args = sys.argv[1], sys.argv[2:]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
state.setdefault("runs", []).append(args)
replies = state.get("replies") or [{}]
reply = replies.pop(0) if len(replies) > 1 else replies[0]
with open(state_path, "w", encoding="utf-8") as handle:
    json.dump(state, handle)
message = reply.get("message", "")
if reply.get("write_last_message", True) and message:
    path = args[args.index("--output-last-message") + 1]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(message)
print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
print(json.dumps({"type": "item.completed",
                  "item": {"type": "agent_message", "text": message}}))
sys.stderr.write(reply.get("stderr", ""))
sys.exit(reply.get("exit", 0))
"""


def fake_codex(tmp_path: Path, *replies: dict) -> tuple[tuple[str, ...], Path]:
    """The command prefix for a fake `codex`, and its state file."""

    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    state = tmp_path / "codex_state.json"
    state.write_text(json.dumps({"replies": list(replies)}), encoding="utf-8")
    return (sys.executable, str(script), str(state)), state


def codex_runs(state: Path) -> list[list[str]]:
    return json.loads(state.read_text(encoding="utf-8")).get("runs", [])


def test_parse_reads_each_finding_relative_to_the_repository(tmp_path: Path) -> None:
    root = tmp_path.resolve().as_posix()

    review = parse_review_message(TWO_FINDINGS.format(root=root), tmp_path)

    assert review.explanation == (
        "The patch drops the empty-input guard and miscounts the slice."
    )
    assert review.findings == (
        LocalFinding(
            priority=2,
            title="Restore the empty-input guard in mean",
            body="Calling `mean([])` now raises `ZeroDivisionError`.\nRestore the guard.",
            path="src/stats.py",
            line=2,
        ),
        LocalFinding(
            priority=1,
            title="Correct the slice start in last_n",
            body="`last_n([1, 2, 3], 1)` returns `[2, 3]`.",
            path="src/stats.py",
            line=9,
        ),
    )


def test_parse_accepts_windows_paths_and_a_single_review_comment(
    tmp_path: Path,
) -> None:
    root = str(tmp_path.resolve()).replace("/", "\\").upper()
    text = (
        "One problem.\r\n\r\nReview comment:\r\n\r\n"
        f"- [P0] Guard the secret {DASH} {root}\\src\\app.py:4\r\n"
        "  The token is logged.\r\n"
    )

    review = parse_review_message(text, tmp_path)

    [finding] = review.findings
    assert finding.path == "src/app.py"
    assert finding.line == 4
    assert finding.location() == "src/app.py:4"


def test_parse_keeps_a_finding_without_a_location(tmp_path: Path) -> None:
    text = "Problems.\n\nFull review comments:\n\n- [P1] Missing migration\n  Add one.\n"

    [finding] = parse_review_message(text, tmp_path).findings

    assert (finding.path, finding.line) == (None, None)
    assert finding.location() == "(no file)"
    assert finding.body == "Add one."


def test_parse_treats_an_explanation_alone_as_clean(tmp_path: Path) -> None:
    review = parse_review_message(CLEAN, tmp_path)

    assert review.findings == ()
    assert review.explanation == CLEAN


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  \n",
        "Problems.\n\nFull review comments:\n\n* a finding in a layout we do not know\n",
    ],
)
def test_parse_never_reads_an_unknown_message_as_clean(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(ValueError):
        parse_review_message(text, tmp_path)


def test_review_command_is_read_only_against_the_base_ref(tmp_path: Path) -> None:
    command = build_review_command(("codex",), "origin/main", tmp_path / "last.txt")

    assert command[:5] == ["codex", "exec", "--sandbox", "read-only", "review"]
    assert command[command.index("--base") + 1] == "origin/main"
    assert command[command.index("--output-last-message") + 1] == str(
        tmp_path / "last.txt"
    )
    assert "--ephemeral" in command
    assert "--output-schema" not in command


def test_run_returns_the_parsed_review(tmp_path: Path) -> None:
    root = tmp_path.resolve().as_posix()
    prefix, state = fake_codex(
        tmp_path, {"message": TWO_FINDINGS.format(root=root)}
    )

    run = run_codex_review(
        command_prefix=prefix,
        repo_root=tmp_path,
        base_ref="origin/main",
        timeout_ms=60000,
    )

    assert run.error == ""
    assert run.review is not None
    assert [finding.location() for finding in run.review.findings] == [
        "src/stats.py:2",
        "src/stats.py:9",
    ]
    [args] = codex_runs(state)
    assert args[args.index("--base") + 1] == "origin/main"
    assert (tmp_path / LAST_MESSAGE_PATH).is_file()


def test_run_falls_back_to_the_last_agent_message(tmp_path: Path) -> None:
    prefix, _state = fake_codex(
        tmp_path, {"message": CLEAN, "write_last_message": False}
    )

    run = run_codex_review(
        command_prefix=prefix,
        repo_root=tmp_path,
        base_ref="origin/main",
        timeout_ms=60000,
    )

    assert run.review is not None and run.review.explanation == CLEAN


def test_run_does_not_reuse_an_earlier_last_message(tmp_path: Path) -> None:
    stale = tmp_path / LAST_MESSAGE_PATH
    stale.parent.mkdir(parents=True)
    stale.write_text(CLEAN, encoding="utf-8")
    prefix, _state = fake_codex(tmp_path, {"message": ""})

    run = run_codex_review(
        command_prefix=prefix,
        repo_root=tmp_path,
        base_ref="origin/main",
        timeout_ms=60000,
    )

    assert run.review is None
    assert "no final message" in run.error


def test_run_reports_a_usage_limit(tmp_path: Path) -> None:
    prefix, _state = fake_codex(
        tmp_path,
        {"exit": 1, "stderr": "ERROR: You've hit your usage limit. Try again later."},
    )

    run = run_codex_review(
        command_prefix=prefix,
        repo_root=tmp_path,
        base_ref="origin/main",
        timeout_ms=60000,
    )

    assert run.review is None
    assert run.usage_limited is True
    assert "exited with code 1" in run.error
