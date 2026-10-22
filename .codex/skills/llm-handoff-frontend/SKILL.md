---
name: "llm-handoff-frontend"
description: "Use for frontend work in repositories that use llm-handoff: implement the assigned UI Work Packet, verify it, and hand back to the auditor."
---

# llm-handoff Frontend Skill

Use this skill when Codex is acting as the `frontend` role in an `llm-handoff` workflow. You own UI implementation, browser behavior, frontend tests, and frontend build verification.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared status enum and Verification Evidence contract.

## Read First

1. `docs/handoff/HANDBOOK.md`
2. `AGENTS.md`
3. The project state file (`PROJECT_STATE.md` unless the dispatcher prompt or `AGENTS.md` names another path), if present
4. `docs/handoff/HANDOFF.md`
5. `docs/handoff/README.md`
6. The frontend source and test files named in the Work Packet

Do not read `CHANGELOG.md` whole; it is long. To add an entry, read only its
first 40 lines (`head -n 40 CHANGELOG.md` in a POSIX shell,
`Get-Content CHANGELOG.md -TotalCount 40` in PowerShell) and add the bullet
first under `## Unreleased`.

## Role Boundary

- Stay inside the Work Packet's `Files in scope`, and never touch `Files out of bounds`.
- Do not modify backend routes, persistence, or API contracts.
- If the task requires backend work, route to `planner` or `backend`.
- You are not authorized to push to `origin`. Never run `git push`.

## Required Handoff Frontmatter

Every handoff write must begin with a YAML frontmatter block:

```yaml
---
next_agent: <enum>       # planner | backend | frontend | auditor | validator | finalizer | user
reason: <string>         # quote every `reason` value
epic_id: <string>
story_id: <string>
story_title: <string>
remaining_stories:
  - <story id/title>
status: <ready_for_review | blocked_missing_context | blocked_implementation_failure | escalate_to_user>
scope_sha: <7-40 hex SHA when close_type is set>
close_type: <story | epic | omit>
prior_sha: <optional prior SHA>
producer: frontend
---
```

When implementation is complete, route to `auditor`:

```yaml
---
next_agent: auditor
reason: "Frontend work complete; audit requested."
epic_id: <string>
story_id: <string>
story_title: <string>
remaining_stories:
  - <story id/title>
status: ready_for_review
scope_sha: <7-40 hex implementation SHA from git rev-parse HEAD>
close_type: story
prior_sha: <optional prior SHA>
producer: frontend
---
```

Run `git rev-parse HEAD` and use the concrete 7-40 character hex SHA. Do not write `scope_sha: HEAD`, branch names, or placeholders.

Quote every `reason`. Use `status: blocked_missing_context`, `blocked_implementation_failure`, or `escalate_to_user` instead of `ready_for_review` when the work cannot be completed. Pair `blocked_missing_context` and `escalate_to_user` with `next_agent: user`, and `blocked_implementation_failure` with `next_agent: planner`.

Include the `## Verification Evidence` block from `docs/handoff/README.md`.
Do not use stale command output from a previous session.

## Git Inside The Sandbox

If the dispatcher's instruction says dispatcher commit mode is on, do not
stage or commit at all: leave your changes in the working tree, leave
`scope_sha` empty, and write `pending-dispatcher-commit` as the
`Commit SHA verified` value. The dispatcher commits the in-scope files for you.

Otherwise, Codex keeps `.git` read-only inside its sandbox, so `git add` and
`git commit` fail there. The repository's `.codex/rules/llm-handoff.rules`
does not let git writes out of the sandbox, and it forbids `git push`. If you
are sandboxed and the protocol asks you to commit, do not work around the
sandbox: leave the changes uncommitted and hand back as
`status: blocked_implementation_failure` explaining that the operator should
enable `commit_mode: dispatcher`.

Read-only git commands (`status`, `diff`, `log`, `rev-parse`) stay in the
sandbox. On Windows the sandbox runs as a separate account, so add
`-c safe.directory=<repository root>` to those if git reports dubious
ownership.

If a test needs temporary directories the sandbox cannot create (for example
pytest's `tmp_path` on Windows), do not move them into the repository. Run
what you can, record the sandbox error under `Unresolved concerns`, and hand
back as `ready_for_review` once the change is committed: the dispatcher's
work-claim gate re-runs the Work Packet's Verification command outside the
sandbox before any audit.

## Handling Audit Feedback

Before acting on auditor feedback, verify the finding against the changed files,
run or inspect the focused command that proves it, and check the fix against
repository architecture and role boundaries. If feedback conflicts with the
contract, document the contradiction and route to `planner` with
`status: blocked_implementation_failure`.

## Completion Checklist

- Verify the requested frontend scope is complete.
- Run the Work Packet's Verification command and the focused frontend checks for the changed surface.
- Commit the frontend change only if the repository protocol asks agents to commit locally and you are not sandboxed; in dispatcher commit mode, leave it uncommitted.
- Rewrite `docs/handoff/HANDOFF.md` with concrete files changed, checks run, screenshots or browser verification when relevant, and the next role.
- Preserve the assignment's `## Work Packet` section in the handback; the auditor and the dispatcher's work-claim gate read it.
- Keep `epic_id`, `story_id`, `story_title`, and `remaining_stories` from the assignment unchanged in the handback. If the assignment has `remaining_stories: []`, write `remaining_stories: []`; do not drop it or replace it with the template's placeholder item. The auditor reads that explicit empty list as the epic's last story.
- Never run `git push`; remote pushes are the operator's call.
