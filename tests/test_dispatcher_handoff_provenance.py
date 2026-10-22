"""TRANS-3 / PR #251 Finding 1: SHA-256 provenance for dispatcher handoffs.

The dispatcher records the SHA-256 of each handoff it writes itself in
`logs/dispatch/dispatcher-handoff.json` and clears the record when an agent
dispatch returns. Diagnostics trust a validator verdict only while
HANDOFF.md still matches that record.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from llm_handoff import config as config_module
from llm_handoff import orchestrator, validator
from llm_handoff.agent_types import DispatchResult, SubagentResult


HANDOFF = """---
next_agent: backend
reason: "Work-claim gate failed."
status: verified_fail
producer: validator
---

# Dispatcher bounce
"""


def _record_path(repo_root: Path) -> Path:
    return repo_root / config_module.DISPATCHER_HANDOFF_STATE_PATH


def _config(repo_root: Path) -> config_module.DispatchConfig:
    return config_module.load_dispatch_config(repo_root=repo_root)


def _make_link(link: Path, target: Path) -> None:
    """A directory symlink, or a junction on Windows; skip when neither works."""

    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import _winapi

        try:
            _winapi.CreateJunction(str(target), str(link))
            return
        except OSError:
            pass
    pytest.skip("cannot create a directory link here")


def test_record_and_match(tmp_path: Path) -> None:
    assert validator.record_dispatcher_handoff(tmp_path, HANDOFF)

    assert validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF)
    # A leading byte-order mark does not change the digest.
    assert validator.is_recorded_dispatcher_handoff(tmp_path, "﻿" + HANDOFF)
    assert not validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF + "x")


def test_no_record_is_untrusted(tmp_path: Path) -> None:
    assert not validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"version": 2, "sha256": "%s"}',
        '{"version": 1, "sha256": "ABC"}',
        '{"version": true, "sha256": "%s"}',
    ],
)
def test_malformed_record_is_untrusted(tmp_path: Path, payload: str) -> None:
    record = _record_path(tmp_path)
    record.parent.mkdir(parents=True)
    digest = validator.dispatcher_handoff_digest(HANDOFF)
    record.write_text(payload.replace("%s", digest), encoding="utf-8")

    assert not validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF)


def test_clear_removes_the_record(tmp_path: Path) -> None:
    assert validator.record_dispatcher_handoff(tmp_path, HANDOFF)

    assert validator.clear_dispatcher_handoff(tmp_path)
    assert not _record_path(tmp_path).exists()
    assert not validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF)
    # Clearing again (or with no logs directory) is not an error.
    assert validator.clear_dispatcher_handoff(tmp_path)
    assert validator.clear_dispatcher_handoff(tmp_path / "missing")


def test_clear_removes_a_read_only_record(tmp_path: Path) -> None:
    assert validator.record_dispatcher_handoff(tmp_path, HANDOFF)
    os.chmod(_record_path(tmp_path), 0o444)

    assert validator.clear_dispatcher_handoff(tmp_path)
    assert not _record_path(tmp_path).exists()


@pytest.mark.parametrize("linked", ["logs", "logs/dispatch"])
def test_linked_directory_is_refused(tmp_path: Path, linked: str) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    target = outside / "dispatch" if linked == "logs" else outside
    target.mkdir(parents=True)
    if linked == "logs/dispatch":
        (repo_root / "logs").mkdir()
    _make_link(repo_root / linked, outside)
    # A record reachable only through the link.
    (target / "dispatcher-handoff.json").write_text(
        '{"version": 1, "sha256": "%s"}' % validator.dispatcher_handoff_digest(HANDOFF),
        encoding="utf-8",
    )

    assert not validator.is_recorded_dispatcher_handoff(repo_root, HANDOFF)
    assert not validator.record_dispatcher_handoff(repo_root, HANDOFF)
    assert not validator.clear_dispatcher_handoff(repo_root)
    # Nothing outside the repository was written or deleted.
    assert sorted(path.name for path in target.iterdir()) == ["dispatcher-handoff.json"]


def test_linked_record_file_is_refused(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    record = _record_path(repo_root)
    record.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(
        '{"version": 1, "sha256": "%s"}' % validator.dispatcher_handoff_digest(HANDOFF),
        encoding="utf-8",
    )
    try:
        os.symlink(outside, record)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a file symlink here")

    assert not validator.is_recorded_dispatcher_handoff(repo_root, HANDOFF)
    assert not validator.record_dispatcher_handoff(repo_root, HANDOFF)


def test_hard_linked_record_file_is_refused(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    record = _record_path(repo_root)
    record.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(
        '{"version": 1, "sha256": "%s"}' % validator.dispatcher_handoff_digest(HANDOFF),
        encoding="utf-8",
    )
    try:
        os.link(outside, record)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a hard link here")

    assert not validator.is_recorded_dispatcher_handoff(repo_root, HANDOFF)


# Orchestrator wiring


def test_dispatcher_writer_records_what_a_reader_sees(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.handoff_full_path.parent.mkdir(parents=True)

    orchestrator._write_dispatcher_handoff(config, HANDOFF)

    on_disk = orchestrator._read_required_text(config.handoff_full_path)
    assert on_disk == HANDOFF
    assert validator.is_recorded_dispatcher_handoff(tmp_path, on_disk)


def test_writer_warns_when_the_record_cannot_be_written(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.handoff_full_path.parent.mkdir(parents=True)
    # A file where the logs directory should be.
    (tmp_path / "logs").write_text("not a directory\n", encoding="utf-8")
    messages: list[tuple[str, str]] = []

    orchestrator._write_dispatcher_handoff(
        config, HANDOFF, lambda level, message: messages.append((level, message))
    )

    assert config.handoff_full_path.read_text(encoding="utf-8") == HANDOFF
    assert [level for level, _ in messages] == ["WARN"]
    assert not validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF)


def test_protected_file_escalation_is_recorded(tmp_path: Path) -> None:
    config = _config(tmp_path)

    orchestrator._write_protected_file_escalation(
        config,
        role_label="backend",
        violations=(".claude/settings.json",),
        error="changed protected files",
    )

    content = orchestrator._read_required_text(config.handoff_full_path)
    assert content is not None and "producer: validator" in content
    assert validator.is_recorded_dispatcher_handoff(tmp_path, content)


def test_clear_after_dispatch_drops_a_planted_record(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.handoff_full_path.parent.mkdir(parents=True)
    config.handoff_full_path.write_text(HANDOFF, encoding="utf-8")
    # The agent planted a record matching its own handoff.
    assert validator.record_dispatcher_handoff(tmp_path, HANDOFF)

    orchestrator._clear_dispatcher_handoff_after_dispatch(config, None)

    assert not validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF)


def test_clear_after_dispatch_keeps_an_untouched_dispatcher_handoff(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.handoff_full_path.parent.mkdir(parents=True)
    orchestrator._write_dispatcher_handoff(config, HANDOFF)
    trusted = orchestrator._recorded_dispatcher_handoff_digest(config, HANDOFF)
    assert trusted is not None

    orchestrator._clear_dispatcher_handoff_after_dispatch(
        config, None, trusted_digest=trusted
    )

    assert validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF)


def test_clear_after_dispatch_drops_trust_when_the_agent_rewrote(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.handoff_full_path.parent.mkdir(parents=True)
    orchestrator._write_dispatcher_handoff(config, HANDOFF)
    trusted = orchestrator._recorded_dispatcher_handoff_digest(config, HANDOFF)
    rewritten = HANDOFF.replace("# Dispatcher bounce", "# Agent rewrite")
    config.handoff_full_path.write_text(rewritten, encoding="utf-8")
    # The agent also planted a record for its rewrite.
    assert validator.record_dispatcher_handoff(tmp_path, rewritten)

    orchestrator._clear_dispatcher_handoff_after_dispatch(
        config, None, trusted_digest=trusted
    )

    assert not validator.is_recorded_dispatcher_handoff(tmp_path, rewritten)
    assert not validator.is_recorded_dispatcher_handoff(tmp_path, HANDOFF)


def test_support_dispatch_clears_a_planted_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def planting_role(*_args: object, **_kwargs: object) -> SubagentResult:
        assert validator.record_dispatcher_handoff(tmp_path, HANDOFF)
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.0)

    monkeypatch.setattr(orchestrator, "invoke_support_role", planting_role)

    orchestrator._invoke_support_callable("auditor", "prompt", repo_root=tmp_path)

    assert not _record_path(tmp_path).exists()


def test_role_dispatch_clears_a_planted_record_even_when_it_raises(
    tmp_path: Path,
) -> None:
    def planting_role(_handoff_path: Path, **_kwargs: object) -> DispatchResult:
        assert validator.record_dispatcher_handoff(tmp_path, HANDOFF)
        raise RuntimeError("role crashed")

    with pytest.raises(RuntimeError):
        orchestrator._invoke_dispatch_callable(
            planting_role, tmp_path / "HANDOFF.md", repo_root=tmp_path
        )

    assert not _record_path(tmp_path).exists()


def test_no_scratch_files_are_left(tmp_path: Path) -> None:
    assert validator.record_dispatcher_handoff(tmp_path, HANDOFF)
    assert validator.record_dispatcher_handoff(tmp_path, HANDOFF + "again")

    names = sorted(path.name for path in _record_path(tmp_path).parent.iterdir())
    assert names == ["dispatcher-handoff.json"]
    shutil.rmtree(tmp_path / "logs")
