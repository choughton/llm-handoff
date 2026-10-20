from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_handoff import chat_queue
from llm_handoff.chat_queue import (
    ChatMessage,
    append_chat_messages,
    chat_queue_path,
    clear_chat_messages,
    load_chat_messages,
    queued_message_notice,
)
from llm_handoff.config import CHAT_QUEUE_STATE_PATH


def _queue(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "dispatch" / "chat-queue.json"


def _write_raw(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _entry(text: str = "hello", role: object = "backend", received_at: object = "2026-10-20T12:00:00+00:00") -> dict:
    return {"text": text, "role": role, "received_at": received_at}


def test_public_names_are_exported() -> None:
    assert set(chat_queue.__all__) == {
        "ChatMessage",
        "append_chat_messages",
        "chat_queue_path",
        "clear_chat_messages",
        "load_chat_messages",
        "queued_message_notice",
    }


def test_chat_message_is_frozen() -> None:
    message = ChatMessage(text="hi", role=None, received_at="t")
    with pytest.raises(AttributeError):
        message.text = "other"  # type: ignore[misc]


def test_chat_queue_path_joins_under_logs_dispatch(tmp_path: Path) -> None:
    assert CHAT_QUEUE_STATE_PATH == Path("logs") / "dispatch" / "chat-queue.json"
    assert chat_queue_path(tmp_path) == tmp_path / "logs" / "dispatch" / "chat-queue.json"


def test_append_then_load_round_trip(tmp_path: Path) -> None:
    path = _queue(tmp_path)
    messages = [
        ChatMessage(text="why did the audit fail?", role="auditor", received_at="2026-10-20T12:00:00+00:00"),
        ChatMessage(text="café — 日本語", role=None, received_at="2026-10-20T12:01:00+00:00"),
    ]

    append_chat_messages(path, messages)

    assert load_chat_messages(path) == messages
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw) == {
        "version": 1,
        "messages": [
            {"text": m.text, "role": m.role, "received_at": m.received_at}
            for m in messages
        ],
    }
    assert sorted(p.name for p in path.parent.iterdir()) == ["chat-queue.json"]


def test_append_does_not_follow_link_at_predictable_temporary_name(tmp_path: Path) -> None:
    path = _queue(tmp_path)
    path.parent.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    planted = path.with_name("chat-queue.json.tmp")
    try:
        planted.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available here")

    append_chat_messages(path, [ChatMessage(text="hi", role=None, received_at="t")])

    assert victim.read_text(encoding="utf-8") == "keep me"
    assert planted.is_symlink()
    assert load_chat_messages(path) == [ChatMessage(text="hi", role=None, received_at="t")]


def test_append_uses_fresh_temporary_name_each_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _queue(tmp_path)
    seen: list[str] = []
    real_replace = chat_queue.os.replace

    def recording_replace(source: object, target: object) -> None:
        seen.append(Path(str(source)).name)
        real_replace(source, target)

    monkeypatch.setattr(chat_queue.os, "replace", recording_replace)

    append_chat_messages(path, [ChatMessage(text="one", role=None, received_at="t1")])
    append_chat_messages(path, [ChatMessage(text="two", role=None, received_at="t2")])

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert all(name.startswith(".chat-queue.json.") and name.endswith(".tmp") for name in seen)


def test_replace_failure_removes_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _queue(tmp_path)

    def failing_replace(source: object, target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(chat_queue.os, "replace", failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        append_chat_messages(path, [ChatMessage(text="hi", role=None, received_at="t")])

    assert list(path.parent.iterdir()) == []


def test_second_append_keeps_earlier_messages_first(tmp_path: Path) -> None:
    path = _queue(tmp_path)
    first = ChatMessage(text="first", role="backend", received_at="t1")
    second = ChatMessage(text="second", role="planner", received_at="t2")
    third = ChatMessage(text="third", role=None, received_at="t3")

    append_chat_messages(path, [first])
    append_chat_messages(path, [second, third])

    assert load_chat_messages(path) == [first, second, third]


def test_empty_append_creates_no_file(tmp_path: Path) -> None:
    path = _queue(tmp_path)

    append_chat_messages(path, [])

    assert not path.exists()
    assert not path.parent.exists()


def test_missing_file_loads_nothing(tmp_path: Path) -> None:
    assert load_chat_messages(_queue(tmp_path)) == []


def test_unreadable_path_loads_nothing(tmp_path: Path) -> None:
    path = _queue(tmp_path)
    path.mkdir(parents=True)  # reading a directory raises OSError

    assert load_chat_messages(path) == []


def test_invalid_json_loads_nothing(tmp_path: Path) -> None:
    path = _queue(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert load_chat_messages(path) == []


@pytest.mark.parametrize(
    "payload",
    [
        [{"version": 1, "messages": []}],
        "text",
        None,
        {"messages": [_entry()]},
        {"version": 2, "messages": [_entry()]},
        {"version": "1", "messages": [_entry()]},
        {"version": True, "messages": [_entry()]},
        {"version": 1.0, "messages": [_entry()]},
        {"version": 1},
        {"version": 1, "messages": {"text": "hello"}},
        {"version": 1, "messages": "hello"},
    ],
    ids=[
        "top-level-list",
        "top-level-string",
        "top-level-null",
        "missing-version",
        "version-2",
        "version-string",
        "version-bool",
        "version-float",
        "missing-messages",
        "messages-object",
        "messages-string",
    ],
)
def test_whole_file_failures_load_nothing(tmp_path: Path, payload: object) -> None:
    path = _queue(tmp_path)
    _write_raw(path, payload)

    assert load_chat_messages(path) == []


@pytest.mark.parametrize(
    "bad_entry",
    [
        "just text",
        None,
        ["hello"],
        _entry(text=5),  # type: ignore[arg-type]
        {"role": "backend", "received_at": "t"},
        _entry(text=""),
        _entry(text="   \n\t"),
        _entry(role=7),
        _entry(role=["backend"]),
        _entry(received_at=None),
        _entry(received_at=1700000000),
        {"text": "hello", "role": "backend"},
    ],
    ids=[
        "entry-string",
        "entry-null",
        "entry-list",
        "text-not-string",
        "text-missing",
        "text-empty",
        "text-blank",
        "role-int",
        "role-list",
        "received-at-null",
        "received-at-int",
        "received-at-missing",
    ],
)
def test_bad_entry_is_skipped_and_neighbors_kept(tmp_path: Path, bad_entry: object) -> None:
    path = _queue(tmp_path)
    _write_raw(
        path,
        {
            "version": 1,
            "messages": [
                _entry(text="before", role="backend", received_at="t1"),
                bad_entry,
                _entry(text="after", role=None, received_at="t2"),
            ],
        },
    )

    assert load_chat_messages(path) == [
        ChatMessage(text="before", role="backend", received_at="t1"),
        ChatMessage(text="after", role=None, received_at="t2"),
    ]


def test_append_drops_invalid_saved_entries(tmp_path: Path) -> None:
    path = _queue(tmp_path)
    _write_raw(path, {"version": 1, "messages": [_entry(text="kept", received_at="t1"), 42]})
    new = ChatMessage(text="new", role=None, received_at="t2")

    append_chat_messages(path, [new])

    assert load_chat_messages(path) == [
        ChatMessage(text="kept", role="backend", received_at="t1"),
        new,
    ]
    assert len(json.loads(path.read_text(encoding="utf-8"))["messages"]) == 2


def test_clear_removes_file(tmp_path: Path) -> None:
    path = _queue(tmp_path)
    append_chat_messages(path, [ChatMessage(text="hi", role=None, received_at="t")])
    assert path.exists()

    clear_chat_messages(path)

    assert not path.exists()
    assert load_chat_messages(path) == []


def test_clear_missing_file_is_noop(tmp_path: Path) -> None:
    path = _queue(tmp_path)

    clear_chat_messages(path)

    assert not path.exists()


def test_clear_propagates_other_os_errors(tmp_path: Path) -> None:
    path = _queue(tmp_path)
    path.mkdir(parents=True)  # unlinking a directory raises a non-missing OSError

    with pytest.raises(OSError):
        clear_chat_messages(path)


def test_write_failure_raises_os_error(tmp_path: Path) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory", encoding="utf-8")
    path = blocker / "dispatch" / "chat-queue.json"

    with pytest.raises(OSError):
        append_chat_messages(path, [ChatMessage(text="hi", role=None, received_at="t")])


def test_queued_message_notice_text() -> None:
    assert (
        queued_message_notice("backend")
        == "Question queued until after the current backend run completes."
    )
    assert (
        queued_message_notice("auditor")
        == "Question queued until after the current auditor run completes."
    )
