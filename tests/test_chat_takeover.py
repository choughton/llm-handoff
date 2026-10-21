from __future__ import annotations

import hashlib
from pathlib import Path
import threading
from typing import Iterator

import pytest

from llm_handoff import chat_takeover
from llm_handoff.chat_takeover import (
    ChatTakeover,
    ChatTakeoverController,
    clear_takeover,
    handoff_content_sha,
    request_takeover,
    request_takeover_if_handoff_changed,
    takeover_requested,
)


@pytest.fixture
def shared_controller() -> Iterator[ChatTakeoverController]:
    chat_takeover.CONTROLLER.clear()
    try:
        yield chat_takeover.CONTROLLER
    finally:
        chat_takeover.CONTROLLER.clear()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_chat_takeover_is_not_an_exception() -> None:
    # The fail-closed ``except Exception`` handlers must let it through.
    assert issubclass(ChatTakeover, BaseException)
    assert not issubclass(ChatTakeover, Exception)


def test_fresh_controller_has_no_request() -> None:
    controller = ChatTakeoverController()
    assert controller.requested() is False
    assert controller.handoff_sha() is None


def test_controller_request_and_clear() -> None:
    controller = ChatTakeoverController()
    controller.request("abc123")
    assert controller.requested() is True
    assert controller.handoff_sha() == "abc123"

    controller.request("def456")
    assert controller.handoff_sha() == "def456"

    controller.clear()
    assert controller.requested() is False
    assert controller.handoff_sha() is None


def test_controller_request_with_no_sha_is_still_a_request() -> None:
    controller = ChatTakeoverController()
    controller.request(None)
    assert controller.requested() is True
    assert controller.handoff_sha() is None


def test_request_from_worker_thread_is_seen_on_main_thread() -> None:
    controller = ChatTakeoverController()
    worker = threading.Thread(target=controller.request, args=("feed",))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert controller.requested() is True
    assert controller.handoff_sha() == "feed"


def test_module_wrappers_use_the_shared_controller(
    shared_controller: ChatTakeoverController,
) -> None:
    assert takeover_requested() is False
    request_takeover("cafe")
    assert takeover_requested() is True
    assert shared_controller.handoff_sha() == "cafe"
    clear_takeover()
    assert takeover_requested() is False
    assert shared_controller.handoff_sha() is None


def test_handoff_content_sha_of_a_file(tmp_path: Path) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_bytes(b"---\nnext_agent: auditor\n---\r\n")
    assert handoff_content_sha(handoff) == _sha(b"---\nnext_agent: auditor\n---\r\n")


def test_handoff_content_sha_of_a_missing_path(tmp_path: Path) -> None:
    assert handoff_content_sha(tmp_path / "missing.md") is None


def test_handoff_content_sha_of_a_directory(tmp_path: Path) -> None:
    assert handoff_content_sha(tmp_path) is None


def test_handoff_content_sha_returns_none_on_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_bytes(b"x")

    def failing_read(_self: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", failing_read)
    assert handoff_content_sha(handoff) is None


def test_unchanged_handoff_requests_nothing(tmp_path: Path) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_bytes(b"same")
    (tmp_path / "other.py").write_text("changed", encoding="utf-8")
    controller = ChatTakeoverController()

    changed = request_takeover_if_handoff_changed(
        handoff_path=handoff,
        handoff_sha_before=_sha(b"same"),
        controller=controller,
    )

    assert changed is False
    assert controller.requested() is False


def test_changed_handoff_requests_takeover_with_new_sha(tmp_path: Path) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_bytes(b"before")
    before = handoff_content_sha(handoff)
    handoff.write_bytes(b"after")
    controller = ChatTakeoverController()

    changed = request_takeover_if_handoff_changed(
        handoff_path=handoff,
        handoff_sha_before=before,
        controller=controller,
    )

    assert changed is True
    assert controller.requested() is True
    assert controller.handoff_sha() == _sha(b"after")


def test_deleted_handoff_requests_takeover(tmp_path: Path) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_bytes(b"before")
    before = handoff_content_sha(handoff)
    handoff.unlink()
    controller = ChatTakeoverController()

    changed = request_takeover_if_handoff_changed(
        handoff_path=handoff,
        handoff_sha_before=before,
        controller=controller,
    )

    assert changed is True
    assert controller.requested() is True
    assert controller.handoff_sha() is None


def test_changed_handoff_defaults_to_the_shared_controller(
    tmp_path: Path, shared_controller: ChatTakeoverController
) -> None:
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_bytes(b"after")

    changed = request_takeover_if_handoff_changed(
        handoff_path=handoff,
        handoff_sha_before=_sha(b"before"),
    )

    assert changed is True
    assert takeover_requested() is True
    assert shared_controller.handoff_sha() == _sha(b"after")
