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

``@<role> <message>`` goes to that role's own finished session instead, when
the ``role_session`` lookup returns one on a supported provider and that role
is not the one running. Role messages share the one FIFO queue with side
messages and resume the role's session with ``resumed_role=True``. Otherwise
the full text goes to the side session with a notice that says why.

The takeover check compares the handoff's content digest from before the
answer with the digest after it, so a role's own ``HANDOFF.md`` write that
lands while an answer runs also requests a takeover. The loop sorts that out
(CHAT-35): when the role returned and the handoff on disk still has the
requested digest and names that role as ``producer``, the request is cleared
and the role's handback goes through its normal post-dispatch path. Known
limit: a role's write while it is still running, when the poll then stops
it, is still a takeover, and a chat rewrite that names the running role as
``producer`` is taken as that role's handback.

Chat keeps the role's own permissions; this module adds no read-only
boundary.

``__main__._attach_console_chat`` wires in this worker (CHAT-32); CHAT-34
supplies the ``role_session`` lookup.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import threading
from typing import Any, get_args

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
    "CHAT_STOPPING_MESSAGE",
    "ChatAnswerWorker",
    "ChatContext",
    "RoleSession",
]

CHAT_ANSWER_PREFIX = "chat: "
CHAT_ERROR_PREFIX = "chat error: "
CHAT_NO_ROLE_MESSAGE = "No role is running, so the chat message was not sent."
CHAT_BLANK_MESSAGE = "The chat message was blank, so nothing was sent."
CHAT_SENT_MESSAGE = "Chat message sent to the chat session."
CHAT_STOPPING_MESSAGE = "Chat is stopping, so the chat message was not sent."

_ROLE_NAMES: frozenset[str] = frozenset(get_args(config.AgentRole))
_ROLE_MESSAGE_RE = re.compile(r"@(\S+)(?:\s+(.*))?", re.DOTALL)


@dataclass(frozen=True)
class ChatContext:
    """The running role a chat message is answered for."""

    role: str
    provider: str
    story_id: str | None
    log_path: Path | str | None
    agent_config: config.AgentConfig | None


@dataclass(frozen=True)
class RoleSession:
    """A finished role dispatch whose session an ``@role`` message resumes."""

    role: str
    provider: str
    story_id: str | None
    session_id: str
    agent_config: config.AgentConfig | None


@dataclass(frozen=True)
class _Queued:
    """One queued message; ``role_session`` is set for a role message."""

    message: str
    context: ChatContext
    role: str | None = None
    body: str | None = None
    role_session: RoleSession | None = None


def chat_unavailable_message(provider: str) -> str:
    return f"Chat is not available for {provider} yet."


def _plural(count: int) -> str:
    return "message is" if count == 1 else "messages are"


def _with_ahead(notice: str, ahead: int) -> str:
    if ahead:
        return f"{notice} {ahead} {_plural(ahead)} ahead of it."
    return notice


def _parse_role_message(message: str) -> tuple[str, str] | None:
    """Return ``(role, body)`` for ``@<role> <body>``, else None."""

    match = _ROLE_MESSAGE_RE.fullmatch(message)
    if match is None or match.group(1) not in _ROLE_NAMES:
        return None
    return match.group(1), (match.group(2) or "").strip()


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
        role_session: Callable[[str], RoleSession | None] | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._handoff_path = Path(handoff_path)
        self._changes_path = Path(changes_path)
        self._context = context
        self._write_line = write_line
        self._run_session = run_session
        self._record_changes = record_changes
        self._request_takeover = request_takeover
        self._role_session = role_session
        self._cond = threading.Condition()
        self._queue: deque[_Queued] = deque()
        self._active = 0
        # The worker thread keeps ownership of these until it exits (see
        # ``_run``), so no second thread starts while an answer still runs.
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        # Side sessions, keyed by provider.
        self._sessions: dict[str, str] = {}
        # Role sessions: recorded session id -> latest id an answer returned.
        self._role_resume_ids: dict[str, str] = {}

    # -- submission side -------------------------------------------------

    def submit(self, text: str) -> str:
        """Queue one message for the running role and return the notice."""

        message = text.strip() if isinstance(text, str) else ""
        if not message:
            return CHAT_BLANK_MESSAGE
        parsed = _parse_role_message(message)
        if parsed is not None and not parsed[1]:
            return CHAT_BLANK_MESSAGE
        context = self._context()
        if context is None:
            return CHAT_NO_ROLE_MESSAGE
        fallback_reason: str | None = None
        if parsed is not None:
            target, body = parsed
            if target == context.role:
                fallback_reason = (
                    f"The {target} role is running, so the side session answers."
                )
            else:
                session, fallback_reason = self._lookup_role_session(target)
                if session is not None:
                    return self._enqueue(
                        _Queued(
                            message=message,
                            context=context,
                            role=target,
                            body=body,
                            role_session=session,
                        ),
                        f"Chat message sent to the {target} session.",
                    )
        if context.provider not in SUPPORTED_CHAT_PROVIDERS:
            return chat_unavailable_message(context.provider)
        notice = CHAT_SENT_MESSAGE
        if fallback_reason is not None:
            notice = f"{fallback_reason} {CHAT_SENT_MESSAGE}"
        return self._enqueue(_Queued(message=message, context=context), notice)

    def _lookup_role_session(
        self, target: str
    ) -> tuple[RoleSession | None, str | None]:
        """Return the usable session, or None and the fallback sentence."""

        no_session = (
            f"No finished {target} session is recorded, so the side session answers."
        )
        if self._role_session is None:
            return None, no_session
        try:
            session = self._role_session(target)
        except Exception:  # noqa: BLE001 - a broken lookup means no session
            return None, no_session
        if not isinstance(session, RoleSession):
            return None, no_session
        session_id = session.session_id
        if not isinstance(session_id, str) or not session_id.strip():
            return None, no_session
        if session.provider not in SUPPORTED_CHAT_PROVIDERS:
            return None, (
                f"The {target} session runs on {session.provider}, which chat "
                "does not support yet, so the side session answers."
            )
        return session, None

    def _enqueue(self, item: _Queued, notice: str) -> str:
        with self._cond:
            if self._stop_event is not None and self._stop_event.is_set():
                # stop() is waiting for the running answer; starting another
                # thread now would answer two messages at once.
                return CHAT_STOPPING_MESSAGE
            # Messages ahead: the ones waiting plus the one being answered.
            ahead = len(self._queue) + self._active
            self._queue.append(item)
            self._ensure_thread_locked()
            self._cond.notify_all()
        return _with_ahead(notice, ahead)

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
        """Finish the running answer, drop the rest, and join the thread.

        While stop() waits for the running answer, ``submit`` queues nothing
        and returns ``CHAT_STOPPING_MESSAGE``; once stop() returns, the next
        ``submit`` starts a fresh thread.
        """

        with self._cond:
            thread = self._thread
            stop_event = self._stop_event
            if thread is None or stop_event is None:
                return
            dropped = 0
            if not stop_event.is_set():
                # Only the first stop() drops messages; a concurrent second
                # stop() just waits for the same thread below.
                stop_event.set()
                dropped = len(self._queue)
                self._queue.clear()
                self._cond.notify_all()
            # The thread handle stays set until the thread itself exits, so
            # submit() cannot start a replacement while an answer runs.
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
        try:
            self._run_loop(stop_event)
        finally:
            # Release ownership only once no answer can still be running.
            with self._cond:
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._stop_event = None
                self._cond.notify_all()

    def _run_loop(self, stop_event: threading.Event) -> None:
        while True:
            with self._cond:
                while not self._queue and not stop_event.is_set():
                    self._cond.wait()
                if stop_event.is_set():
                    return
                item = self._queue.popleft()
                self._active += 1
            try:
                self._answer_guarded(item)
            finally:
                with self._cond:
                    self._active -= 1
                    self._cond.notify_all()

    def _answer_guarded(self, item: _Queued) -> None:
        try:
            self._answer(item)
        except Exception as exc:  # noqa: BLE001 - the thread must keep running
            self._write_error(item, f"{type(exc).__name__}: {exc}")

    def _answer(self, item: _Queued) -> None:
        handoff_sha_before = handoff_content_sha(self._handoff_path)
        try:
            if item.role_session is not None:
                result = self._call_role_session(item)
            else:
                result = self._call_side_session(item)
            if result is not None:
                self._write_result(item, result)
                self._record(item, result)
        finally:
            # The session may have rewritten HANDOFF.md even when it failed.
            self._request_takeover(
                handoff_path=self._handoff_path,
                handoff_sha_before=handoff_sha_before,
            )

    def _call_side_session(self, item: _Queued) -> ChatSessionResult | None:
        context = item.context
        provider = context.provider
        try:
            result = self._run_session(
                provider,
                item.message,
                repo_root=self._repo_root,
                role=context.role,
                story_id=context.story_id,
                handoff_path=self._handoff_path,
                log_path=context.log_path,
                session_id=self._sessions.get(provider),
                agent_config=context.agent_config,
            )
        except (ValueError, OSError) as exc:
            self._write_error(item, str(exc) or type(exc).__name__)
            return None
        session_id = getattr(result, "session_id", None)
        if isinstance(session_id, str) and session_id.strip():
            self._sessions[provider] = session_id
        return result

    def _call_role_session(self, item: _Queued) -> ChatSessionResult | None:
        session = item.role_session
        if session is None:
            return None
        recorded_id = session.session_id
        try:
            result = self._run_session(
                session.provider,
                item.body or "",
                repo_root=self._repo_root,
                role=item.role or session.role,
                story_id=session.story_id,
                handoff_path=self._handoff_path,
                log_path=item.context.log_path,
                session_id=self._role_resume_ids.get(recorded_id, recorded_id),
                agent_config=session.agent_config,
                resumed_role=True,
            )
        except (ValueError, OSError) as exc:
            self._write_error(item, str(exc) or type(exc).__name__)
            return None
        session_id = getattr(result, "session_id", None)
        if isinstance(session_id, str) and session_id.strip():
            self._role_resume_ids[recorded_id] = session_id
        return result

    def _write_result(self, item: _Queued, result: ChatSessionResult) -> None:
        if result.error:
            self._write_error(item, result.error)
            return
        prefix = (
            CHAT_ANSWER_PREFIX if item.role is None else f"chat {item.role}: "
        )
        lines = result.text.splitlines() or [""]
        for line in lines:
            self._safe_write(f"{prefix}{line}")

    def _record(self, item: _Queued, result: ChatSessionResult) -> None:
        try:
            self._record_changes(
                self._changes_path,
                repo_root=self._repo_root,
                result=result,
                message=item.message,
                role=item.role if item.role is not None else item.context.role,
                # A role answer is committed by the running dispatch, so its
                # changes are recorded against the running story.
                story_id=item.context.story_id,
                handoff_path=self._handoff_path,
            )
        except OSError as exc:
            self._write_error(
                item,
                f"could not save the chat change record {self._changes_path}: {exc}",
            )

    def _write_error(self, item: _Queued, detail: str) -> None:
        prefix = CHAT_ERROR_PREFIX if item.role is None else f"chat {item.role} error: "
        self._safe_write(f"{prefix}{detail}")

    def _safe_write(self, line: str) -> None:
        # A console write failure must not skip the change record or takeover.
        try:
            self._write_line(line)
        except Exception:  # noqa: BLE001
            return
