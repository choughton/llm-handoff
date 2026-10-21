from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_handoff import chat_changes
from llm_handoff.chat_changes import (
    ChatChange,
    ChatChangeEntry,
    chat_changes_path,
    chat_owned_paths,
    clear_chat_changes,
    load_chat_changes,
    record_chat_changes,
)
from llm_handoff.chat_side_session import ChatSessionResult
from llm_handoff.config import CHAT_CHANGES_STATE_PATH

HANDOFF = "docs/handoff/HANDOFF.md"
GOOD_SHA = "a" * 64


def _result(*paths: str, exit_code: int = 0, error: str | None = None) -> ChatSessionResult:
    return ChatSessionResult(
        text="done" if error is None else "",
        session_id="session-1",
        exit_code=exit_code,
        error=error,
        edited_paths=tuple(paths),
    )


def _clock(second: int):
    return lambda: datetime(2026, 10, 20, 12, 0, second, 500, tzinfo=timezone.utc)


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _record(repo: Path, *paths: str, message: str = "please fix", **kwargs):
    return record_chat_changes(
        chat_changes_path(repo),
        repo_root=repo,
        result=kwargs.pop("result", None) or _result(*paths),
        message=message,
        role=kwargs.pop("role", "backend"),
        story_id=kwargs.pop("story_id", "S1"),
        handoff_path=kwargs.pop("handoff_path", HANDOFF),
        now=kwargs.pop("now", _clock(1)),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_path_helper_uses_config_constant(tmp_path: Path) -> None:
    assert chat_changes_path(tmp_path) == tmp_path / CHAT_CHANGES_STATE_PATH
    assert CHAT_CHANGES_STATE_PATH == Path("logs") / "dispatch" / "chat-changes.json"


def test_round_trip_two_entries(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "a")
    _write(tmp_path, "src/b.py", "b")
    first = _record(tmp_path, "src/a.py", message="one", now=_clock(1))
    second = _record(
        tmp_path, "src/b.py", message="two", role="auditor", story_id=None, now=_clock(2)
    )

    loaded = load_chat_changes(chat_changes_path(tmp_path))

    assert loaded == [first, second]
    assert first == ChatChangeEntry(
        role="backend",
        story_id="S1",
        recorded_at="2026-10-20T12:00:01+00:00",
        message_sha256=_sha("one"),
        changes=(ChatChange(path="src/a.py", fingerprint=_sha("a")),),
    )
    assert second.story_id is None
    assert second.role == "auditor"
    assert second.recorded_at == "2026-10-20T12:00:02+00:00"


def test_normalizes_drops_handoff_and_duplicates(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "a")
    _write(tmp_path, "src/b.py", "b")
    _write(tmp_path, HANDOFF, "handoff")

    entry = _record(
        tmp_path,
        "src\\b.py",
        "./docs/handoff/HANDOFF.md",
        "./src/a.py",
        "src/b.py",
        "docs\\handoff\\HANDOFF.md",
        "src/a.py",
    )

    assert entry is not None
    assert [change.path for change in entry.changes] == ["src/b.py", "src/a.py"]


def test_absolute_handoff_path_is_dropped(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "a")
    entry = _record(
        tmp_path, HANDOFF, "src/a.py", handoff_path=tmp_path / "docs" / "handoff" / "HANDOFF.md"
    )

    assert entry is not None
    assert [change.path for change in entry.changes] == ["src/a.py"]


def test_empty_result_writes_nothing(tmp_path: Path) -> None:
    assert _record(tmp_path, result=_result()) is None
    assert _record(tmp_path, HANDOFF, "./" + HANDOFF) is None
    assert not chat_changes_path(tmp_path).exists()


def test_message_text_is_not_saved(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "a")
    message = "secret operator instruction xyzzy"

    _record(tmp_path, "src/a.py", message=message)

    raw = chat_changes_path(tmp_path).read_text(encoding="utf-8")
    assert "xyzzy" not in raw
    assert message not in raw
    payload = json.loads(raw)
    assert payload["version"] == 1
    assert payload["entries"][0]["message_sha256"] == _sha(message)


def test_failed_result_is_recorded(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "a")

    entry = _record(
        tmp_path, result=_result("src/a.py", exit_code=1, error="provider exited 1")
    )

    assert entry is not None
    assert load_chat_changes(chat_changes_path(tmp_path)) == [entry]


def test_deleted_file_records_absent(tmp_path: Path) -> None:
    entry = _record(tmp_path, "src/gone.py")

    assert entry is not None
    assert entry.changes == (ChatChange(path="src/gone.py", fingerprint="<absent>"),)


def _valid_entry(**overrides) -> dict:
    entry = {
        "role": "backend",
        "story_id": "S1",
        "recorded_at": "2026-10-20T12:00:01+00:00",
        "message_sha256": GOOD_SHA,
        "changes": [{"path": "src/a.py", "fingerprint": "f" * 64}],
    }
    entry.update(overrides)
    return entry


def _save(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps("string"),
        json.dumps({"version": 2, "entries": [_valid_entry()]}),
        json.dumps({"version": "1", "entries": [_valid_entry()]}),
        json.dumps({"version": True, "entries": [_valid_entry()]}),
        json.dumps({"entries": [_valid_entry()]}),
        json.dumps({"version": 1, "entries": {"a": 1}}),
    ],
)
def test_load_rejects_unusable_file(tmp_path: Path, text: str) -> None:
    path = chat_changes_path(tmp_path)
    _save(path, text)

    assert load_chat_changes(path) == []


def test_load_missing_file(tmp_path: Path) -> None:
    assert load_chat_changes(chat_changes_path(tmp_path)) == []


def test_load_unreadable_file(tmp_path: Path) -> None:
    path = chat_changes_path(tmp_path)
    path.mkdir(parents=True)

    assert load_chat_changes(path) == []


@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        _valid_entry(role=""),
        _valid_entry(role="   "),
        _valid_entry(role=None),
        _valid_entry(story_id=5),
        _valid_entry(recorded_at=None),
        _valid_entry(recorded_at=123),
        _valid_entry(message_sha256="A" * 64),
        _valid_entry(message_sha256="a" * 63),
        _valid_entry(message_sha256="g" * 64),
        _valid_entry(message_sha256=None),
        _valid_entry(changes="src/a.py"),
        _valid_entry(changes=None),
        _valid_entry(changes=["src/a.py"]),
        _valid_entry(changes=[{"path": "", "fingerprint": "x"}]),
        _valid_entry(changes=[{"path": 1, "fingerprint": "x"}]),
        _valid_entry(changes=[{"path": "src/a.py", "fingerprint": ""}]),
        _valid_entry(changes=[{"path": "src/a.py"}]),
        _valid_entry(
            changes=[
                {"path": "src/a.py", "fingerprint": "x"},
                {"path": "src/b.py", "fingerprint": None},
            ]
        ),
    ],
)
def test_load_skips_malformed_entry(tmp_path: Path, bad) -> None:
    path = chat_changes_path(tmp_path)
    good = _valid_entry(role="frontend")
    _save(path, json.dumps({"version": 1, "entries": [bad, good]}))

    loaded = load_chat_changes(path)

    assert [entry.role for entry in loaded] == ["frontend"]


def test_clear_missing_and_present(tmp_path: Path) -> None:
    path = chat_changes_path(tmp_path)
    clear_chat_changes(path)
    assert not path.exists()

    _write(tmp_path, "src/a.py", "a")
    _record(tmp_path, "src/a.py")
    assert path.exists()

    clear_chat_changes(path)
    assert not path.exists()


def test_chat_owned_paths_tracks_current_fingerprints(tmp_path: Path) -> None:
    _write(tmp_path, "src/keep.py", "keep")
    _write(tmp_path, "src/changed.py", "before")
    _record(tmp_path, "src/keep.py", "src/changed.py", "src/deleted.py")
    _write(tmp_path, "src/changed.py", "role edit")

    entries = load_chat_changes(chat_changes_path(tmp_path))

    assert chat_owned_paths(tmp_path, entries) == ("src/keep.py", "src/deleted.py")

    _write(tmp_path, "src/deleted.py", "recreated")
    assert chat_owned_paths(tmp_path, entries) == ("src/keep.py",)


def test_chat_owned_paths_uses_later_fingerprint(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "first")
    _write(tmp_path, "src/b.py", "b")
    _record(tmp_path, "src/a.py", now=_clock(1))
    _record(tmp_path, "src/b.py", "src/a.py", now=_clock(2))
    entries = load_chat_changes(chat_changes_path(tmp_path))
    assert chat_owned_paths(tmp_path, entries) == ("src/a.py", "src/b.py")

    _write(tmp_path, "src/a.py", "second")
    _record(tmp_path, "src/a.py", now=_clock(3))
    entries = load_chat_changes(chat_changes_path(tmp_path))

    assert chat_owned_paths(tmp_path, entries) == ("src/a.py", "src/b.py")

    _write(tmp_path, "src/a.py", "first")
    assert chat_owned_paths(tmp_path, entries) == ("src/b.py",)


def test_write_failure_propagates_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "src/a.py", "a")
    first = _record(tmp_path, "src/a.py", now=_clock(1))
    path = chat_changes_path(tmp_path)

    def _fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(chat_changes.os, "replace", _fail)

    with pytest.raises(OSError, match="disk full"):
        _record(tmp_path, "src/a.py", now=_clock(2))

    leftovers = [item.name for item in path.parent.iterdir() if item.name != path.name]
    assert leftovers == []
    assert load_chat_changes(path) == [first]


def test_write_failure_on_first_write_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "src/a.py", "a")

    def _fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _fail)

    with pytest.raises(OSError):
        _record(tmp_path, "src/a.py")

    assert list(chat_changes_path(tmp_path).parent.iterdir()) == []
