"""Answer chat messages one at a time on a background thread.

``ChatAnswerWorker.submit`` fits ``ChatInput``'s ``on_submit``: it reads the
running role's ``ChatContext``, queues the message, and returns the console
notice. A daemon thread answers queued messages in submission order with the
CHAT-17 side session (``run_chat_session``), resuming the saved session for
that provider on follow-ups. After each answer it writes the answer lines,
records the files the answer changed (CHAT-21 ``record_chat_changes``), and
then asks for a takeover when ``HANDOFF.md`` changed (CHAT-27
``request_takeover_if_handoff_changed``). The change record is written before
the takeover request, so the loop's takeover cleanup already sees the
chat-owned paths.

Known limit: the takeover check compares the handoff's content digest from
before the answer with the digest after it. A role's own ``HANDOFF.md`` write
that lands while an answer runs therefore also counts as a chat change to
``HANDOFF.md`` and requests a takeover. No path filtering is done for it.

Chat keeps the role's own permissions; this module adds no read-only
boundary.

Nothing in the loop calls this module yet; CHAT-30 wires it in.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any

from llm_handoff import config
from llm_handoff.chat_changes import record_chat_changes
from llm_handoff.chat_side_session import (
    SUPPORTED_CHAT_PROVIDERS,
    ChatSessionResult,
    run_chat_session,
)
from llm_handoff.chat_takeover import (
    handoff_content_sha,
    request_takeover_if_handoff_changed,
)

__all__ = [
    "CHAT_ANSWER_PREFIX",
    "CHAT_ERROR_PREFIX",
    "CHAT_NO_ROLE_MESSAGE",
    "ChatAnswerWorker",
    "ChatContext",
]

CHAT_ANSWER_PREFIX = "chat: "
CHAT_ERROR_PREFIX = "chat error: "
CHAT_NO_ROLE_MESSAGE = "No role is running, so the chat message was not sent."
CHAT_BLANK_MESSAGE = "The chat message was blank, so nothing was sent."
CHAT_SENT_MESSAGE = "Chat message sent to the chat session."


@dataclass(frozen=True)
class ChatContext:
    """The running role a chat message is answered for."""

    role: str
    provider: str
    story_id: str | None
    log_path: Path | str | None
    agent_config: config.AgentConfig | None


def chat_unavailable_message(provider: str) -> str:
    return f"Chat is not available for {provider} yet."


def _plural(count: int) -> str:
    return "message is" if count == 1 else "messages are"


class ChatAnswerWorker:
    """Queue chat messages and answer them in order on one daemon thread."""

    def __init__(
        self,
        *,
        repo_root: Path,
        handoff_path: Path,
        changes_path: Path,
        context: Callable[[], ChatContext | None],
        write_line: Callable[[str], None],
        run_session: Callable[..., ChatSessionResult] = run_chat_session,
        record_changes: Callable[..., Any] = record_chat_changes,
        request_takeover: Callable[..., Any] = request_takeover_if_handoff_changed,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._handoff_path = Path(handoff_path)
        self._changes_path = Path(changes_path)
        self._context = context
        self._write_line = write_line
        self._run_session = run_session
        self._record_changes = record_changes
        self._request_takeover = request_takeover
        self._cond = threading.Condition()
        self._queue: deque[tuple[str, ChatContext]] = deque()
        self._active = 0
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._sessions: dict[str, str] = {}

    # -- submission side -------------------------------------------------

    def submit(self, text: str) -> str:
        """Queue one message for the running role and return the notice."""

        message = text.strip() if isinstance(text, str) else ""
        if not message:
            return CHAT_BLANK_MESSAGE
        context = self._context()
        if context is None:
            return CHAT_NO_ROLE_MESSAGE
        if context.provider not in SUPPORTED_CHAT_PROVIDERS:
            return chat_unavailable_message(context.provider)
        with self._cond:
            # Messages ahead: the ones waiting plus the one being answered.
            ahead = len(self._queue) + self._active
            self._queue.append((message, context))
            self._ensure_thread_locked()
            self._cond.notify_all()
        if ahead:
            return f"{CHAT_SENT_MESSAGE} {ahead} {_plural(ahead)} ahead of it."
        return CHAT_SENT_MESSAGE

    def busy(self) -> bool:
        with self._cond:
            return self._busy_locked()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no message is queued or answered; False on timeout."""

        with self._cond:
            return self._cond.wait_for(
                lambda: not self._busy_locked(), timeout=timeout
            )

    def stop(self) -> None:
        """Finish the running answer, drop the rest, and join the thread."""

        with self._cond:
            thread = self._thread
            stop_event = self._stop_event
            if thread is None or stop_event is None:
                return
            stop_event.set()
            dropped = len(self._queue)
            self._queue.clear()
            self._thread = None
            self._stop_event = None
            self._cond.notify_all()
        if dropped:
            self._safe_write(
                f"Chat stopped: {dropped} queued chat "
                f"{'message was' if dropped == 1 else 'messages were'} "
                "not answered."
            )
        if thread is not threading.current_thread():
            thread.join()

    # -- worker side -----------------------------------------------------

    def _busy_locked(self) -> bool:
        return bool(self._queue) or self._active > 0

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(stop_event,),
            name="llm-handoff-chat-answers",
            daemon=True,
        )
        self._thread = thread
        self._stop_event = stop_event
        thread.start()

    def _run(self, stop_event: threading.Event) -> None:
        while True:
            with self._cond:
                while not self._queue and not stop_event.is_set():
                    self._cond.wait()
                if stop_event.is_set():
                    return
                message, context = self._queue.popleft()
                self._active += 1
            try:
                self._answer_guarded(message, context)
            finally:
                with self._cond:
                    self._active -= 1
                    self._cond.notify_all()

    def _answer_guarded(self, message: str, context: ChatContext) -> None:
        try:
            self._answer(message, context)
        except Exception as exc:  # noqa: BLE001 - the thread must keep running
            self._write_error(f"{type(exc).__name__}: {exc}")

    def _answer(self, message: str, context: ChatContext) -> None:
        handoff_sha_before = handoff_content_sha(self._handoff_path)
        try:
            result = self._call_session(message, context)
            if result is not None:
                self._write_result(result)
                self._record(message, context, result)
        finally:
            # The session may have rewritten HANDOFF.md even when it failed.
            self._request_takeover(
                handoff_path=self._handoff_path,
                handoff_sha_before=handoff_sha_before,
            )

    def _call_session(
        self, message: str, context: ChatContext
    ) -> ChatSessionResult | None:
        provider = context.provider
        try:
            result = self._run_session(
                provider,
                message,
                repo_root=self._repo_root,
                role=context.role,
                story_id=context.story_id,
                handoff_path=self._handoff_path,
                log_path=context.log_path,
                session_id=self._sessions.get(provider),
                agent_config=context.agent_config,
            )
        except (ValueError, OSError) as exc:
            self._write_error(str(exc) or type(exc).__name__)
            return None
        session_id = getattr(result, "session_id", None)
        if isinstance(session_id, str) and session_id.strip():
            self._sessions[provider] = session_id
        return result

    def _write_result(self, result: ChatSessionResult) -> None:
        if result.error:
            self._write_error(result.error)
            return
        lines = result.text.splitlines() or [""]
        for line in lines:
            self._safe_write(f"{CHAT_ANSWER_PREFIX}{line}")

    def _record(
        self, message: str, context: ChatContext, result: ChatSessionResult
    ) -> None:
        try:
            self._record_changes(
                self._changes_path,
                repo_root=self._repo_root,
                result=result,
                message=message,
                role=context.role,
                story_id=context.story_id,
                handoff_path=self._handoff_path,
            )
        except OSError as exc:
            self._write_error(
                f"could not save the chat change record {self._changes_path}: {exc}"
            )

    def _write_error(self, detail: str) -> None:
        self._safe_write(f"{CHAT_ERROR_PREFIX}{detail}")

    def _safe_write(self, line: str) -> None:
        # A console write failure must not skip the change record or takeover.
        try:
            self._write_line(line)
        except Exception:  # noqa: BLE001
            return
