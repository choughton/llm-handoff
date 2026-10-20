"""Graceful stop requests for a running dispatcher.

``llm-handoff stop`` writes a small file under ``logs/dispatch/``. The
dispatch loop checks for it before each cycle and while it waits at a pause,
so it finishes the cycle in flight (dispatch, commit, validation, work-claim
gate) and exits with code 0 before it starts the next dispatch. Deleting the
file before the dispatcher sees it cancels the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path

DISPATCH_LOCK_FILENAME = ".dispatch.lock"
STOP_REQUEST_PATH = Path("logs") / "dispatch" / "stop-request"


@dataclass(frozen=True, slots=True)
class DispatchLockInfo:
    path: Path
    pid: int | None
    created_at: str | None


def dispatch_lock_path(repo_root: Path) -> Path:
    return Path(repo_root).resolve() / DISPATCH_LOCK_FILENAME


def stop_request_path(repo_root: Path) -> Path:
    return Path(repo_root).resolve() / STOP_REQUEST_PATH


def read_dispatch_lock(repo_root: Path) -> DispatchLockInfo | None:
    """Return the pid and start time recorded in ``.dispatch.lock``, if any."""

    path = dispatch_lock_path(repo_root)
    if not path.exists():
        return None
    fields = _read_fields(path)
    raw_pid = fields.get("pid", "")
    return DispatchLockInfo(
        path=path,
        pid=int(raw_pid) if raw_pid.isdigit() else None,
        created_at=fields.get("created_at") or None,
    )


def _read_fields(path: Path) -> dict[str, str]:
    """Parse ``key: value`` lines; an unreadable or missing file has none."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields.setdefault(key.strip(), value.strip())
    return fields


def process_is_running(pid: int) -> bool:
    # Imported here so the dispatch loop does not load the supervisor module.
    from llm_handoff.supervisor import _is_process_running

    return _is_process_running(pid)


def process_check_command(pid: int) -> str:
    if os.name == "nt":
        return f"Get-Process -Id {pid}"
    return f"ps -p {pid}"


def describe_lock_holder(lock: DispatchLockInfo) -> str:
    """Name the pid in ``.dispatch.lock`` and say how to check it."""

    if lock.pid is None:
        return f"{lock.path} does not record a pid."
    started = f", started {lock.created_at}" if lock.created_at else ""
    if process_is_running(lock.pid):
        state = "is running"
    else:
        state = (
            "is not running on this machine, so the lock may be stale "
            "(a killed dispatcher cannot remove it)"
        )
    return (
        f"{lock.path} names pid {lock.pid}{started}; that pid {state}. "
        f"Check it with `{process_check_command(lock.pid)}`."
    )


def stop_request_is_ignored(repo_root: Path) -> bool:
    """True when Git ignores the stop-request file.

    An untracked stop request that Git does not ignore would show up as an
    uncommitted file in the work-claim gate of the dispatch in flight.
    """

    from llm_handoff.work_claim import git_path_is_ignored

    return git_path_is_ignored(Path(repo_root).resolve(), STOP_REQUEST_PATH.as_posix())


def request_stop(repo_root: Path, *, target_pid: int | None = None) -> Path:
    """Write a stop request, for the dispatcher with ``target_pid`` when given.

    A request without a target pid, such as a file created by hand, applies to
    whichever dispatcher sees it.
    """

    path = stop_request_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = f"target_pid: {target_pid}\n" if target_pid is not None else ""
    path.write_text(
        f"requested_at: {datetime.now().isoformat(timespec='seconds')}\n"
        f"requested_by_pid: {os.getpid()}\n"
        f"{target}",
        encoding="utf-8",
    )
    return path


def stop_request_target_pid(repo_root: Path) -> int | None:
    """The pid a stop request names; None when it names none or is missing."""

    raw_pid = _read_fields(stop_request_path(repo_root)).get("target_pid", "")
    return int(raw_pid) if raw_pid.isdigit() else None


def stop_requested(repo_root: Path, *, pid: int | None = None) -> bool:
    """True when a stop request applies to the dispatcher with ``pid``.

    A request for another pid does not apply. Without ``pid`` any request does.
    A request that names no pid applies to any pid by design: only one
    dispatcher can hold ``.dispatch.lock`` in a repository, so it can only
    mean the dispatcher running there.
    """

    if not stop_request_path(repo_root).is_file():
        return False
    if pid is None:
        return True
    target_pid = stop_request_target_pid(repo_root)
    return target_pid is None or target_pid == pid


def clear_stale_stop_request(repo_root: Path, *, pid: int) -> bool:
    """At startup, delete a stop request that is not for this dispatcher.

    ``llm-handoff stop`` may accept this run's lock before startup gets here,
    so a request naming this pid is kept. True when a request was deleted.
    """

    if not stop_request_path(repo_root).is_file():
        return False
    if stop_request_target_pid(repo_root) == pid:
        return False
    return clear_stop_request(repo_root)


def clear_stop_request(repo_root: Path) -> bool:
    """Delete the stop request; True when there was one."""

    try:
        stop_request_path(repo_root).unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
