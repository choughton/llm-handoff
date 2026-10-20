"""RECOVERY-9: find files an implementer may have left with no record on disk.

An implementer interrupted with neither an in-flight record nor a carry-over
record left behind (a run from before those records existed, or a record that
was lost) leaves its uncommitted files looking like operator edits. This
module picks the files that could be that implementer's work so the
dispatcher can ask the operator about them. It never decides on its own: the
caller asks the operator and only a confirmed answer carries the files over.

The selection is pure apart from read-only Git calls. Any Git failure means
no candidates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import subprocess

from llm_handoff.roles import normalize_next_agent_value
from llm_handoff.router import HandoffFrontmatterError, parse_handoff_frontmatter_text
from llm_handoff.work_claim import (
    WorkPacketError,
    carry_over_key,
    git_dirty_files,
    normalize_repo_path,
    parse_work_packet,
    path_is_in_scope,
)


IMPLEMENTER_ROLES = frozenset({"backend", "frontend"})
DISPATCH_LOG_DIRECTORY = "logs/dispatch"
# How many commits back from HEAD the story's last commit is searched for.
STORY_COMMIT_SEARCH_LIMIT = 500
GIT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class NoRecordCandidates:
    """Files that may be an interrupted implementer's work.

    ``offered`` are the dirty files the operator is asked about, sorted.
    ``left_out`` are the other dirty files (apart from the handoff and the
    dispatcher's own ``logs/dispatch/`` files); they stay operator edits.
    """

    role: str
    story_key: str
    offered: tuple[str, ...]
    left_out: tuple[str, ...]


def find_no_record_candidates(
    repo_root: Path,
    handoff_content: str,
    handoff_path: Path | str,
    protected_paths: tuple[str, ...],
    *,
    protected_match: Callable[[str, tuple[str, ...]], bool],
) -> NoRecordCandidates | None:
    """Return the files to offer as the routed implementer's interrupted work.

    None when the handoff does not route to ``backend`` or ``frontend``, has
    no carry-over key or no enforceable Work Packet, a Git call fails, or no
    dirty file qualifies. A dirty file qualifies when the Work Packet's
    Files in scope cover it, ``protected_match(path, protected_paths)`` is
    false, and no commit after the story's last commit changed it.
    """

    try:
        frontmatter = parse_handoff_frontmatter_text(handoff_content)
    except HandoffFrontmatterError:
        return None
    if frontmatter is None:
        return None
    role = normalize_next_agent_value(frontmatter.next_agent)
    if role not in IMPLEMENTER_ROLES:
        return None
    story_key = carry_over_key(handoff_content)
    if story_key is None:
        return None
    try:
        packet = parse_work_packet(handoff_content)
    except WorkPacketError:
        return None

    dirty_files = git_dirty_files(repo_root)
    if dirty_files is None:
        return None
    handoff_rel_path = normalize_repo_path(Path(handoff_path).as_posix())
    dirty = tuple(
        path
        for path in dirty_files
        if path != handoff_rel_path and not _is_dispatch_log_path(path)
    )
    if not dirty:
        return None

    eligible = tuple(
        path
        for path in dirty
        if path_is_in_scope(path, packet)
        and not protected_match(path, protected_paths)
    )
    offered: tuple[str, ...] = ()
    if eligible:
        changed_since = _changed_since_story_commit(
            repo_root, frontmatter.story_id, eligible
        )
        if changed_since is None:
            return None
        offered = tuple(sorted(path for path in eligible if path not in changed_since))
    if not offered:
        return None
    left_out = tuple(sorted(path for path in dirty if path not in offered))
    return NoRecordCandidates(
        role=role, story_key=story_key, offered=offered, left_out=left_out
    )


def story_last_commit(repo_root: Path, story_id: str) -> str | None:
    """The newest commit reachable from HEAD that the dispatcher made for the story.

    The dispatcher's commit subject is ``<story_id> <story_title>``, or just
    ``<story_id>``. Searches at most :data:`STORY_COMMIT_SEARCH_LIMIT`
    commits. Returns the SHA, ``""`` when none matches, or None when Git
    fails.
    """

    entries = _git_nul_entries(
        repo_root,
        [
            "log",
            "-z",
            f"--max-count={STORY_COMMIT_SEARCH_LIMIT}",
            "--format=%H %s",
            "HEAD",
            "--",
        ],
    )
    if entries is None:
        return None
    prefix = f"{story_id} "
    for entry in entries:
        sha, _separator, subject = entry.strip("\n").partition(" ")
        if subject == story_id or subject.startswith(prefix):
            return sha
    return ""


def _changed_since_story_commit(
    repo_root: Path, story_id: str | None, paths: tuple[str, ...]
) -> frozenset[str] | None:
    """Paths a commit after the story's last commit changed; None on Git failure.

    Without a ``story_id``, or with no commit for it, HEAD is the story's last
    commit and nothing has changed since.

    The result is the union of the paths every commit in ``<base>..HEAD``
    touched, so a path changed and later restored still counts, plus the
    endpoint diff ``<base> HEAD``. Merge commits are diffed against each of
    their parents, so a change made only in a merge (for example in its
    conflict resolution) counts even when a later merge restores it. A merge
    diff against a parent from another branch can list a path no commit in
    the range changed; that only keeps the path from being offered, which is
    the safe direction.
    """

    if not story_id:
        return frozenset()
    base = story_last_commit(repo_root, story_id)
    if base is None:
        return None
    if not base:
        return frozenset()
    touched = _git_nul_entries(
        repo_root,
        [
            "--literal-pathspecs",
            "log",
            "-z",
            # Walk every commit in the range, even a merge that is TREESAME
            # to one parent, and list each merge's changes against every
            # parent so a change made only in a merge counts.
            "--full-history",
            "-m",
            "--name-only",
            "--no-renames",
            "--format=",
            f"{base}..HEAD",
            "--",
            *paths,
        ],
    )
    if touched is None:
        return None
    entries = _git_nul_entries(
        repo_root,
        [
            "--literal-pathspecs",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            base,
            "HEAD",
            "--",
            *paths,
        ],
    )
    if entries is None:
        return None
    return frozenset(
        normalize_repo_path(entry) for entry in (*touched, *entries)
    )


def _git_nul_entries(repo_root: Path, args: list[str]) -> tuple[str, ...] | None:
    """Run a read-only Git command and split its NUL-separated output.

    None when Git cannot run, times out, or exits non-zero.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return tuple(entry for entry in completed.stdout.split("\0") if entry)


def _is_dispatch_log_path(path: str) -> bool:
    return path == DISPATCH_LOG_DIRECTORY or path.startswith(
        DISPATCH_LOG_DIRECTORY + "/"
    )
