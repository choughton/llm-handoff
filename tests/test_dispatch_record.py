from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_handoff import dispatch_record
from llm_handoff.config import DISPATCH_RECORD_STATE_PATH
from llm_handoff.dispatch_record import (
    DispatchEntry,
    append_dispatch_entry,
    clear_dispatch_record,
    dispatch_record_path,
    load_dispatch_record,
    record_dispatch,
)


def _record(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "dispatch" / "dispatch-record.json"


def _write_raw(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _raw_entry(**overrides: object) -> dict:
    entry: dict = {
        "role": "backend",
        "story_id": "CHAT-3",
        "provider": "claude",
        "session_id": "sess-1",
        "recorded_at": "2026-10-20T12:00:00+00:00",
    }
    entry.update(overrides)
    return entry


GOOD = DispatchEntry(
    role="backend",
    story_id="CHAT-3",
    provider="claude",
    session_id="sess-1",
    recorded_at="2026-10-20T12:00:00+00:00",
)


def test_public_names_are_exported() -> None:
    assert dispatch_record.__all__ == [
        "DispatchEntry",
        "append_dispatch_entry",
        "clear_dispatch_record",
        "dispatch_record_path",
        "load_dispatch_record",
        "record_dispatch",
    ]


def test_dispatch_entry_is_frozen_with_expected_fields() -> None:
    assert [field.name for field in dataclasses.fields(DispatchEntry)] == [
        "role",
        "story_id",
        "provider",
        "session_id",
        "recorded_at",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        GOOD.role = "auditor"  # type: ignore[misc]


def test_dispatch_record_path_joins_under_logs_dispatch(tmp_path: Path) -> None:
    assert DISPATCH_RECORD_STATE_PATH == Path("logs") / "dispatch" / "dispatch-record.json"
    assert dispatch_record_path(tmp_path) == tmp_path / DISPATCH_RECORD_STATE_PATH


def test_append_then_load_round_trip_keeps_order(tmp_path: Path) -> None:
    path = _record(tmp_path)
    second = DispatchEntry(
        role="auditor",
        story_id=None,
        provider=None,
        session_id=None,
        recorded_at="2026-10-20T12:05:00+00:00",
    )

    append_dispatch_entry(path, GOOD)
    append_dispatch_entry(path, second)

    assert load_dispatch_record(path) == [GOOD, second]
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw) == {
        "version": 1,
        "entries": [dataclasses.asdict(GOOD), dataclasses.asdict(second)],
    }
    assert sorted(p.name for p in path.parent.iterdir()) == ["dispatch-record.json"]


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_dispatch_record(_record(tmp_path)) == []


def test_load_unreadable_file_returns_empty(tmp_path: Path) -> None:
    path = _record(tmp_path)
    path.mkdir(parents=True)  # a directory cannot be read as text
    assert load_dispatch_record(path) == []


def test_load_invalid_json_returns_empty(tmp_path: Path) -> None:
    path = _record(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_dispatch_record(path) == []


def test_load_non_utf8_returns_empty(tmp_path: Path) -> None:
    path = _record(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00bad")
    assert load_dispatch_record(path) == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "text",
        None,
        {"entries": [_raw_entry()]},
        {"version": True, "entries": [_raw_entry()]},
        {"version": 2, "entries": [_raw_entry()]},
        {"version": "1", "entries": [_raw_entry()]},
        {"version": 1.0, "entries": [_raw_entry()]},
        {"version": 1},
        {"version": 1, "entries": {"0": _raw_entry()}},
        {"version": 1, "entries": "nope"},
    ],
    ids=[
        "list-payload",
        "string-payload",
        "null-payload",
        "missing-version",
        "bool-version",
        "wrong-version",
        "string-version",
        "float-version",
        "missing-entries",
        "dict-entries",
        "string-entries",
    ],
)
def test_load_rejected_payload_returns_empty(tmp_path: Path, payload: object) -> None:
    path = _record(tmp_path)
    _write_raw(path, payload)
    assert load_dispatch_record(path) == []


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-object",
        ["list"],
        _raw_entry(role=None),
        _raw_entry(role=""),
        _raw_entry(role="   "),
        _raw_entry(role=7),
        _raw_entry(story_id=3),
        _raw_entry(provider=["claude"]),
        _raw_entry(session_id={"id": "x"}),
        _raw_entry(recorded_at=None),
        _raw_entry(recorded_at=1700000000),
    ],
    ids=[
        "string-entry",
        "list-entry",
        "null-role",
        "empty-role",
        "blank-role",
        "int-role",
        "int-story-id",
        "list-provider",
        "dict-session-id",
        "null-recorded-at",
        "int-recorded-at",
    ],
)
def test_load_skips_bad_entry_and_keeps_good_one(tmp_path: Path, bad: object) -> None:
    path = _record(tmp_path)
    _write_raw(path, {"version": 1, "entries": [bad, dataclasses.asdict(GOOD)]})
    assert load_dispatch_record(path) == [GOOD]


def test_load_accepts_none_optional_fields(tmp_path: Path) -> None:
    path = _record(tmp_path)
    _write_raw(
        path,
        {
            "version": 1,
            "entries": [_raw_entry(story_id=None, provider=None, session_id=None)],
        },
    )
    assert load_dispatch_record(path) == [
        DispatchEntry(
            role="backend",
            story_id=None,
            provider=None,
            session_id=None,
            recorded_at="2026-10-20T12:00:00+00:00",
        )
    ]


def test_append_creates_parent_directories(tmp_path: Path) -> None:
    path = _record(tmp_path)
    assert not path.parent.exists()
    append_dispatch_entry(path, GOOD)
    assert load_dispatch_record(path) == [GOOD]


def test_append_replace_failure_propagates_and_leaves_prior_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _record(tmp_path)
    append_dispatch_entry(path, GOOD)
    before = path.read_bytes()

    def _fail_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _fail_replace)

    with pytest.raises(OSError, match="disk full"):
        append_dispatch_entry(
            path, dataclasses.replace(GOOD, role="auditor", session_id=None)
        )

    assert path.read_bytes() == before
    assert list(path.parent.glob(".dispatch-record.json.*.tmp")) == []
    assert sorted(p.name for p in path.parent.iterdir()) == ["dispatch-record.json"]


def test_clear_deletes_file(tmp_path: Path) -> None:
    path = _record(tmp_path)
    append_dispatch_entry(path, GOOD)
    clear_dispatch_record(path)
    assert not path.exists()
    assert load_dispatch_record(path) == []


def test_clear_missing_file_is_not_an_error(tmp_path: Path) -> None:
    path = _record(tmp_path)
    clear_dispatch_record(path)
    assert not path.exists()


def test_record_dispatch_stores_utc_time_from_non_utc_clock(tmp_path: Path) -> None:
    path = _record(tmp_path)
    plus_five = timezone(timedelta(hours=5))
    fixed = datetime(2026, 10, 20, 17, 30, 45, 123456, tzinfo=plus_five)

    entry = record_dispatch(
        path,
        role="backend",
        story_id="CHAT-3",
        provider="claude",
        session_id="sess-9",
        now=lambda: fixed,
    )

    assert entry == DispatchEntry(
        role="backend",
        story_id="CHAT-3",
        provider="claude",
        session_id="sess-9",
        recorded_at="2026-10-20T12:30:45+00:00",
    )
    assert load_dispatch_record(path) == [entry]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_record_dispatch_stores_blank_session_id_as_none(
    tmp_path: Path, blank: str
) -> None:
    path = _record(tmp_path)
    fixed = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    entry = record_dispatch(
        path,
        role="auditor",
        story_id=None,
        provider="codex",
        session_id=blank,
        now=lambda: fixed,
    )

    assert entry.session_id is None
    assert load_dispatch_record(path) == [entry]
    assert json.loads(path.read_text(encoding="utf-8"))["entries"][0]["session_id"] is None


def test_record_dispatch_appends_after_saved_entries(tmp_path: Path) -> None:
    path = _record(tmp_path)
    append_dispatch_entry(path, GOOD)

    entry = record_dispatch(
        path,
        role="auditor",
        story_id="CHAT-3",
        provider="codex",
        session_id=None,
        now=lambda: datetime(2026, 10, 20, 13, 0, 0, tzinfo=timezone.utc),
    )

    assert load_dispatch_record(path) == [GOOD, entry]


def test_record_dispatch_default_clock_is_utc(tmp_path: Path) -> None:
    path = _record(tmp_path)
    entry = record_dispatch(
        path, role="backend", story_id=None, provider=None, session_id=None
    )
    parsed = datetime.fromisoformat(entry.recorded_at)
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.microsecond == 0
