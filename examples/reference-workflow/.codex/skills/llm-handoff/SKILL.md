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
7. `CONFIGURATION.md`
8. `docs/ARCHITECTURE.md`

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

## Handling Audit Feedback

Before acting on auditor feedback, verify the finding against the changed files,
run or inspect the focused command that proves it, and check the fix against
repository architecture and role boundaries. If feedback conflicts with the
contract, document the contradiction and route with
`status: blocked_implementation_failure`.

## Completion Checklist

- Verify the requested backend scope is complete.
- Run the focused tests or checks that cover the change.
- Commit the backend change if the repository protocol asks agents to commit locally.
- Rewrite `docs/handoff/HANDOFF.md` with concrete files changed, checks run, blockers if any, and the next role.
- Leave `git push` to the repository's authorization rules.
