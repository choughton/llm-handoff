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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

from llm_handoff.router import HandoffFrontmatterError, parse_handoff_frontmatter_text
from llm_handoff.work_claim import (
    PreDispatchState,
    WorkPacketError,
    git_dirty_files,
    git_head,
    normalize_repo_path,
    parse_work_packet,
    path_matches_entry,
)


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
_EVIDENCE_SHA_RE = re.compile(r"(?im)^(-\s+\*\*Commit SHA verified:\*\*)[^\n]*$")


@dataclass(frozen=True)
class DispatcherCommitResult:
    """What the commit step did. ``sha`` is None when nothing was committed."""

    sha: str | None
    committed: tuple[str, ...] = ()
    left_uncommitted: tuple[str, ...] = ()
    skipped_reason: str | None = None
    error: str | None = None


def commit_implementer_changes(
    *,
    repo_root: Path,
    handoff_path: Path,
    assignment_content: str,
    claim_content: str,
    pre_dispatch_state: PreDispatchState,
    role: str,
) -> DispatcherCommitResult:
    """Commit an implementer's in-scope changes on its behalf.

    ``assignment_content`` is the handoff the implementer received, so the
    scope comes from the planner's Work Packet, not from the handback.
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
    to_commit = tuple(
        path
        for path in candidates
        if any(path_matches_entry(path, entry) for entry in packet.files_in_scope)
        and not any(
            path_matches_entry(path, entry) for entry in packet.files_out_of_bounds
        )
    )
    left_uncommitted = tuple(path for path in candidates if path not in to_commit)
    if not to_commit:
        return DispatcherCommitResult(
            sha=None,
            left_uncommitted=left_uncommitted,
            skipped_reason="no in-scope files changed since dispatch",
        )

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
    )


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
    rest = _EVIDENCE_SHA_RE.sub(lambda match: f"{match.group(1)} {sha}", rest)
    return frontmatter + rest


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
