"""Dispatcher-side commits for implementer handbacks.

With ``commit_mode: dispatcher`` an implementer never writes to ``.git``. It
leaves its edits in the working tree and hands back ``ready_for_review``. The
dispatcher then:

1. lists files changed since dispatch, skipping the handoff file and any file
   that was already dirty before dispatch;
2. stages only files inside the Work Packet's ``Files in scope`` and outside
   ``Files out of bounds``;
3. commits exactly those paths (``--only``, hooks skipped); and
4. records the new commit as ``scope_sha`` and as the Verification Evidence
   ``Commit SHA verified`` value in the handoff.

Anything left unstaged stays in the working tree, where the work-claim gate's
worktree check fails it. Sandboxed agents therefore never need write access
to ``.git``, and no agent can widen what gets committed.

Files a chat side session changed at the operator's request (the
authenticated ``chat_changes`` record) are the one exception: each one that
still matches its recorded fingerprint is committed too, even outside the
Work Packet scope or when it was dirty before dispatch, and the commit body
carries the operator marker (message time and SHA-256, never the text).
After a non-implementer role, ``commit_chat_changes`` commits those chat
paths on their own with the same marker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
from typing import TYPE_CHECKING

from llm_handoff.chat_changes import ChatChangeEntry, chat_owned_paths
from llm_handoff.router import HandoffFrontmatterError, parse_handoff_frontmatter_text
from llm_handoff.work_claim import (
    EVIDENCE_LABEL_NOTE,
    PreDispatchState,
    WorkPacketError,
    git_dirty_files,
    git_head,
    normalize_repo_path,
    parse_work_packet,
    path_is_in_scope,
)

if TYPE_CHECKING:
    from llm_handoff.audit_review import OperatorInfluence


COMMIT_MODE_INSTRUCTION = (
    "Dispatcher commit mode is on. Do not run `git add`, `git commit`, or any "
    "other command that writes to `.git`. Leave your changes uncommitted in the "
    "working tree and change only files listed under Files in scope. In your "
    "handback, leave `scope_sha` empty and write `pending-dispatcher-commit` as "
    "the `Commit SHA verified` value. When you hand back `ready_for_review`, the "
    "dispatcher commits exactly the in-scope files, records the commit SHA in "
    "the handoff, and runs the work-claim gate. Do not leave scratch or "
    "temporary files in the repository: anything outside Files in scope that is "
    "still in the working tree fails the gate."
)
# The handoff is live dispatch state. Committing it would put that state on
# the base branch and, with `publish.mode`, into every story branch and pull
# request.
PLANNER_COMMIT_MODE_INSTRUCTION = (
    "Dispatcher commit mode is on. Do not run `git add`, `git commit`, or any "
    "other command that writes to `.git`, even if the repository protocol asks "
    "agents to commit the handoff. Rewrite the handoff file and leave it "
    "uncommitted; the dispatcher keeps it out of commits."
)
PENDING_COMMIT_SHA = "pending-dispatcher-commit"
_GIT_TIMEOUT_SECONDS = 60
_FRONTMATTER_SCOPE_SHA_RE = re.compile(r"(?m)^scope_sha:[^\n]*$")
_FRONTMATTER_PRODUCER_RE = re.compile(r"(?m)^producer:[^\n]*$")
_EVIDENCE_SHA_RE = re.compile(
    rf"(?im)^(-[ \t]+\*\*Commit SHA verified{EVIDENCE_LABEL_NOTE}:\*\*)([^\n]*)$"
)
_PENDING_MARKER_RE = re.compile(rf"`?{re.escape(PENDING_COMMIT_SHA)}`?")


@dataclass(frozen=True)
class DispatcherCommitResult:
    """What the commit step did. ``sha`` is None when nothing was committed."""

    sha: str | None
    committed: tuple[str, ...] = ()
    left_uncommitted: tuple[str, ...] = ()
    skipped_reason: str | None = None
    error: str | None = None
    # True when a ready_for_review claim changed no in-scope file this dispatch.
    nothing_to_commit: bool = False
    # Chat-owned paths committed alongside the in-scope files, and the
    # operator marker of each chat entry that contributed one, in record order.
    chat_committed: tuple[str, ...] = ()
    chat_influences: tuple[OperatorInfluence, ...] = ()


def commit_implementer_changes(
    *,
    repo_root: Path,
    handoff_path: Path,
    assignment_content: str,
    claim_content: str,
    pre_dispatch_state: PreDispatchState,
    role: str,
    chat_entries: Sequence[ChatChangeEntry] = (),
) -> DispatcherCommitResult:
    """Commit an implementer's in-scope changes on its behalf.

    ``assignment_content`` is the handoff the implementer received, so the
    scope comes from the planner's Work Packet, not from the handback.
    ``chat_entries`` are authenticated chat change entries; each dirty path
    in ``chat_owned_paths`` other than the handoff is committed as well, after
    the in-scope files, whatever the Work Packet or pre-dispatch state says.
    """

    try:
        frontmatter = parse_handoff_frontmatter_text(claim_content)
    except HandoffFrontmatterError:
        frontmatter = None
    status = (frontmatter.status or "").strip() if frontmatter is not None else ""
    if status != "ready_for_review":
        return DispatcherCommitResult(
            sha=None,
            skipped_reason=f"handback status is `{status or 'unset'}`, not ready_for_review",
        )

    try:
        packet = parse_work_packet(assignment_content)
    except WorkPacketError as exc:
        return DispatcherCommitResult(
            sha=None,
            skipped_reason=f"the assignment's Work Packet did not parse ({exc.detail})",
        )

    dirty_now = git_dirty_files(repo_root)
    if dirty_now is None:
        return DispatcherCommitResult(
            sha=None,
            error="git status failed; the working tree could not be listed",
        )

    handoff_rel_path = normalize_repo_path(Path(handoff_path).as_posix())
    pre_existing = set(pre_dispatch_state.dirty_files)
    candidates = [
        path
        for path in dirty_now
        if path != handoff_rel_path and path not in pre_existing
    ]
    # A file that was dirty before dispatch holds edits the implementer did not
    # make, so it is never staged; if the implementer changed it further, the
    # gate's worktree check reports it.
    in_scope = tuple(path for path in candidates if path_is_in_scope(path, packet))
    # The operator asked chat to change these, so they bypass the scope and
    # pre-dispatch-dirty rules. A path the role changed after chat recorded it
    # no longer matches its fingerprint and is not in this list.
    dirty_set = set(dirty_now)
    owned = tuple(
        path
        for path in dict.fromkeys(
            normalize_repo_path(item)
            for item in (chat_owned_paths(repo_root, chat_entries) if chat_entries else ())
        )
        if path in dirty_set and path != handoff_rel_path
    )
    to_commit = in_scope + tuple(path for path in owned if path not in in_scope)
    owned_set = set(owned)
    chat_paths = tuple(path for path in to_commit if path in owned_set)
    left_uncommitted = tuple(path for path in candidates if path not in to_commit)
    if not to_commit:
        return DispatcherCommitResult(
            sha=None,
            left_uncommitted=left_uncommitted,
            skipped_reason="no in-scope files changed since dispatch",
            nothing_to_commit=True,
        )
    chat_influences = _chat_influences(chat_entries, chat_paths)

    story_id = frontmatter.story_id if frontmatter is not None else None
    story_title = frontmatter.story_title if frontmatter is not None else None
    subject = (
        " ".join(part for part in (story_id, story_title) if part)
        or "Implementer handback"
    )
    body = (
        f"Committed by the llm-handoff dispatcher from the {role} role's "
        "ready_for_review handback.\n\nFiles:\n"
        + "\n".join(f"- {path}" for path in to_commit)
    )
    if chat_paths:
        body += _chat_body(chat_paths, chat_influences)

    add = _git(repo_root, ["add", "--", *to_commit])
    if add.returncode != 0:
        return DispatcherCommitResult(
            sha=None,
            left_uncommitted=left_uncommitted,
            error=f"git add failed: {_first_line(add)}",
        )
    commit = _git(
        repo_root,
        [
            "commit",
            "--no-verify",
            "--only",
            "-m",
            subject,
            "-m",
            body,
            "--",
            *to_commit,
        ],
    )
    if commit.returncode != 0:
        return DispatcherCommitResult(
            sha=None,
            left_uncommitted=left_uncommitted,
            error=f"git commit failed: {_first_line(commit)}",
        )
    sha = git_head(repo_root)
    if sha is None:
        return DispatcherCommitResult(
            sha=None,
            left_uncommitted=left_uncommitted,
            error="git rev-parse HEAD failed after the commit",
        )
    return DispatcherCommitResult(
        sha=sha,
        committed=to_commit,
        left_uncommitted=left_uncommitted,
        chat_committed=chat_paths,
        chat_influences=chat_influences,
    )


def commit_chat_changes(
    *,
    repo_root: Path,
    handoff_path: Path,
    role: str,
    story_id: str | None,
    chat_entries: Sequence[ChatChangeEntry],
) -> DispatcherCommitResult:
    """Commit the chat-owned paths after a non-implementer role's cycle.

    Only paths in ``chat_owned_paths`` that are dirty now are committed, in
    record order, and never the handoff. A dirty file that is not chat-owned,
    or one the role changed after chat recorded it (so its fingerprint no
    longer matches), is never staged.
    """

    dirty_now = git_dirty_files(repo_root)
    if dirty_now is None:
        return DispatcherCommitResult(
            sha=None,
            error="git status failed; the working tree could not be listed",
        )
    handoff_rel_path = normalize_repo_path(Path(handoff_path).as_posix())
    dirty_set = set(dirty_now)
    owned = tuple(
        path
        for path in dict.fromkeys(
            normalize_repo_path(item)
            for item in (chat_owned_paths(repo_root, chat_entries) if chat_entries else ())
        )
        if path in dirty_set and path != handoff_rel_path
    )
    if not owned:
        return DispatcherCommitResult(
            sha=None,
            skipped_reason="no recorded chat change is still in the working tree",
            nothing_to_commit=True,
        )
    chat_influences = _chat_influences(chat_entries, owned)

    label = f"Chat changes after the {role} role"
    story = (story_id or "").strip()
    subject = f"{story} {label}" if story else label
    body = (
        f"Committed by the llm-handoff dispatcher after the {role} role's "
        "cycle.\n\nFiles:\n"
        + "\n".join(f"- {path}" for path in owned)
        + _chat_body(owned, chat_influences)
    )

    add = _git(repo_root, ["add", "--", *owned])
    if add.returncode != 0:
        return DispatcherCommitResult(
            sha=None, error=f"git add failed: {_first_line(add)}"
        )
    commit = _git(
        repo_root,
        ["commit", "--no-verify", "--only", "-m", subject, "-m", body, "--", *owned],
    )
    if commit.returncode != 0:
        return DispatcherCommitResult(
            sha=None, error=f"git commit failed: {_first_line(commit)}"
        )
    sha = git_head(repo_root)
    if sha is None:
        return DispatcherCommitResult(
            sha=None, error="git rev-parse HEAD failed after the commit"
        )
    return DispatcherCommitResult(
        sha=sha,
        committed=owned,
        chat_committed=owned,
        chat_influences=chat_influences,
    )


def _utc_marker_time(recorded_at: str) -> str | None:
    """``recorded_at`` as UTC ``YYYY-MM-DDTHH:MM:SSZ``; None when unparseable."""

    text = recorded_at.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chat_influences(
    entries: Sequence[ChatChangeEntry], chat_paths: tuple[str, ...]
) -> tuple[OperatorInfluence, ...]:
    """The marker of each entry whose recorded state of a path was committed.

    ``chat_owned_paths`` matches a path against its latest recorded
    fingerprint, so that latest entry is the one that contributed the path.
    """

    if not chat_paths:
        return ()
    from llm_handoff.audit_review import OperatorInfluence

    latest: dict[str, int] = {}
    for index, entry in enumerate(entries):
        for change in entry.changes:
            latest[normalize_repo_path(change.path)] = index
    committed = set(chat_paths)
    contributing = sorted(
        {index for path, index in latest.items() if path in committed}
    )
    influences: list[OperatorInfluence] = []
    for index in contributing:
        entry = entries[index]
        at = _utc_marker_time(entry.recorded_at)
        if at is None:
            continue
        influences.append(OperatorInfluence(at=at, message_sha256=entry.message_sha256))
    return tuple(influences)


def _chat_body(
    chat_paths: tuple[str, ...], influences: tuple[OperatorInfluence, ...]
) -> str:
    lines = ["", "", "Chat changes (operator-influenced):"]
    lines.extend(f"- {path}" for path in chat_paths)
    for influence in influences:
        lines.append(f"- Message time: {influence.at}")
        lines.append(f"- Message SHA-256: {influence.message_sha256}")
    return "\n".join(lines)


def stamp_commit_sha(handoff_content: str, sha: str) -> str:
    """Record ``sha`` as ``scope_sha`` and as the evidence commit SHA."""

    normalized = handoff_content.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return handoff_content
    end = normalized.find("\n---", 4)
    if end == -1:
        return handoff_content
    frontmatter, rest = normalized[: end + 1], normalized[end + 1 :]
    if _FRONTMATTER_SCOPE_SHA_RE.search(frontmatter):
        frontmatter = _FRONTMATTER_SCOPE_SHA_RE.sub(f"scope_sha: {sha}", frontmatter)
    elif _FRONTMATTER_PRODUCER_RE.search(frontmatter):
        frontmatter = _FRONTMATTER_PRODUCER_RE.sub(
            lambda match: f"scope_sha: {sha}\n{match.group(0)}",
            frontmatter,
            count=1,
        )
    else:
        frontmatter = f"{frontmatter}scope_sha: {sha}\n"
    rest = _EVIDENCE_SHA_RE.sub(lambda match: _stamp_evidence_line(match, sha), rest)
    return frontmatter + rest


def _stamp_evidence_line(match: re.Match[str], sha: str) -> str:
    label, value = match.group(1), match.group(2)
    # Replace only the marker, so a note after it, as in
    # "pending-dispatcher-commit (plus operator commit ...", keeps its text.
    if _PENDING_MARKER_RE.search(value):
        return label + _PENDING_MARKER_RE.sub(sha, value, count=1)
    return f"{label} {sha}"


@dataclass(frozen=True)
class GitPosition:
    head: str | None
    branch: str | None


@dataclass(frozen=True)
class PlannerCommitCheck:
    """``undone`` lists planner commits that were reset; ``error`` stops the loop."""

    undone: tuple[str, ...] = ()
    error: str | None = None


def git_position(repo_root: Path) -> GitPosition:
    head = _git(repo_root, ["rev-parse", "--verify", "HEAD"])
    branch = _git(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    return GitPosition(
        head=(head.stdout.strip() or None) if head.returncode == 0 else None,
        branch=(branch.stdout.strip() or None) if branch.returncode == 0 else None,
    )


def undo_planner_handoff_commits(
    repo_root: Path,
    handoff_path: Path,
    before: GitPosition,
    *,
    role: str = "planner",
    may_commit_other_files: bool = False,
) -> PlannerCommitCheck:
    """Enforce that ``role`` leaves ``.git`` alone in dispatcher commit mode.

    The planner and handoff-repair prompts say not to commit, but prompts are
    advisory. When the role added commits that change only the handoff file,
    they are reset with ``git reset --soft`` and the handoff is unstaged, so it
    stays in the working tree uncommitted. Anything else (other files, a branch
    switch, or rewritten history) is reported as an error for the operator.
    With ``may_commit_other_files`` (the finalizer, which commits project
    state), commits that leave the handoff out are accepted, and only commits
    that mix the handoff with other files are an error.
    """

    after = git_position(repo_root)
    if before.head is None or after.head == before.head:
        return PlannerCommitCheck()
    if after.branch != before.branch:
        return PlannerCommitCheck(
            error=(
                f"the {role} moved the checkout from `{before.branch or 'detached HEAD'}` "
                f"to `{after.branch or 'detached HEAD'}`"
            )
        )
    if after.head is None or _git(
        repo_root, ["merge-base", "--is-ancestor", before.head, after.head]
    ).returncode != 0:
        return PlannerCommitCheck(
            error=f"the {role} rewrote history on `{after.branch}` (HEAD was {before.head[:12]})"
        )
    commits = tuple(
        _git(repo_root, ["rev-list", "--reverse", f"{before.head}..{after.head}"])
        .stdout.split()
    )
    changed = _git(
        repo_root, ["diff", "--name-only", before.head, after.head]
    ).stdout.split()
    handoff = handoff_path.as_posix()
    others = sorted(set(changed) - {handoff})
    if may_commit_other_files and handoff not in changed:
        return PlannerCommitCheck()
    if others and may_commit_other_files:
        return PlannerCommitCheck(
            error=(
                f"the {role} committed {', '.join(c[:12] for c in commits)} with "
                f"the handoff {handoff} alongside: {', '.join(others)}"
            )
        )
    if others:
        return PlannerCommitCheck(
            error=(
                f"the {role} committed {', '.join(c[:12] for c in commits)} with "
                f"files other than the handoff: {', '.join(others)}"
            )
        )
    reset = _git(repo_root, ["reset", "--soft", before.head])
    if reset.returncode != 0:
        return PlannerCommitCheck(
            error=f"could not undo the {role}'s handoff commit: {_first_line(reset)}"
        )
    if changed:
        _git(repo_root, ["restore", "--staged", "--", handoff])
    return PlannerCommitCheck(undone=commits)


def _git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    for text in (result.stderr, result.stdout):
        for line in (text or "").splitlines():
            if line.strip():
                return line.strip()
    return f"exit {result.returncode}"
