---
name: "llm-handoff"
description: "Use for backend/Codex work in repositories that use llm-handoff."
---

# llm-handoff Backend Skill

Use this skill when Codex is acting as the `backend` role in an `llm-handoff` workflow.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared status enum and Verification Evidence contract.

## Read First

1. `docs/handoff/HANDBOOK.md`
2. `AGENTS.md`
3. `PROJECT_STATE.md` if present
4. `docs/handoff/HANDOFF.md`
5. `docs/handoff/README.md`
6. `README.md`
7. Repository-specific architecture or requirements docs referenced by the handoff

## Role Boundary

The backend role owns backend code, tests, data contracts, persistence, command-line glue, and integration wiring. It does not own frontend-only work, planning, audit verdicts, or finalizer state updates.

If the task is misrouted, rewrite `docs/handoff/HANDOFF.md` and route to `planner`, `validator`, or `user` instead of expanding scope.

Do not modify `PROJECT_STATE.md` unless the current handoff explicitly assigns state-maintenance work to this role.

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
producer: backend
---
```

When implementation is complete, route to `auditor`:

```yaml
---
next_agent: auditor
reason: "Backend work complete; audit requested."
epic_id: <string>
story_id: <string>
story_title: <string>
remaining_stories:
  - <story id/title>
status: ready_for_review
scope_sha: <7-40 hex implementation SHA from git rev-parse HEAD>
close_type: story
producer: backend
---
```

Run `git rev-parse HEAD` and use the concrete 7-40 character hex SHA. Do not write `scope_sha: HEAD`, a branch name, or a placeholder.

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

- Verify the requested backend scope is complete.
- Run the focused tests or checks that cover the change.
- Commit the backend change only if the repository protocol asks agents to commit locally and you are not sandboxed; in dispatcher commit mode, leave it uncommitted.
- Rewrite `docs/handoff/HANDOFF.md` with concrete files changed, checks run, blockers if any, and the next role.
- Preserve the assignment's `## Work Packet` section in the handback; the auditor and the dispatcher's work-claim gate read it.
- Keep `epic_id`, `story_id`, `story_title`, and `remaining_stories` from the assignment unchanged in the handback. If the assignment has `remaining_stories: []`, write `remaining_stories: []`; do not drop it or replace it with the template's placeholder item. The auditor reads that explicit empty list as the epic's last story.
- Never run `git push`; remote pushes are the operator's call.
