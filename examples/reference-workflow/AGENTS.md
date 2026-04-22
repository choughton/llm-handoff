# Repository Agent Instructions

These instructions apply to this repository when it is using `llm-handoff`.

## Operating Model

This repo uses a file-based dispatch loop:

- `docs/handoff/HANDOFF.md` is the live handoff state.
- `PROJECT_STATE.md` is the durable project state file.
- Git commit SHAs are the durable record of completed work.
- Prompts are advisory; validators are authoritative.
- Ambiguous routing fails closed and routes to `validator` or `user`.

## Roles

Use these public role names in handoffs:

- `planner`: scopes work, sequences stories, and writes assignments.
- `backend`: implements backend, data, CLI, integration, and test work.
- `frontend`: implements UI, browser behavior, frontend tests, and visual checks.
- `auditor`: reviews completed work and routes approved or blocked results.
- `validator`: validates malformed, stale, or ambiguous handoff state.
- `finalizer`: updates durable project state after approved epic-level closeout.
- `user`: human decision required.

Provider names such as Codex, Gemini, and Claude are adapter details. Do not
use provider names as the public workflow roles unless the local dispatcher
config explicitly maps them.

## Provider-Native Helpers

Provider-native subagents, skills, and agent files are internal helper
mechanisms, not additional dispatcher roles. See
`docs/ARCHITECTURE.md#provider-native-subagents` in the `llm-handoff` source
checkout for the boundary: only the active dispatcher role writes
`HANDOFF.md`.

## Handoff Rules

Every write to `docs/handoff/HANDOFF.md` must begin with YAML frontmatter:

```yaml
---
next_agent: <planner | backend | frontend | auditor | validator | finalizer | user>
reason: "<short routing reason>"
epic_id: <optional epic id>
story_id: <optional story id>
story_title: <optional story title>
remaining_stories:
  - <optional remaining story id/title>
status: <optional canonical status>
bounce_count: <optional retry count>
evidence_present: <optional boolean>
scope_sha: <7-40 hex SHA when close_type is set>
close_type: <story | epic | omit>
prior_sha: <optional prior SHA>
producer: <planner | backend | frontend | auditor | validator | finalizer | user>
---
```

Rules:

- Quote every `reason` value.
- Do not write `scope_sha: HEAD`; run `git rev-parse HEAD` and write the
  concrete SHA.
- Use `close_type: story` only for approved story-level completion.
- Use `close_type: epic` only for approved final scope that should route to
  `finalizer`.
- Route unclear or unsafe state to `validator` or `user`; do not guess.
- Use the canonical status enum: `ready_for_review`, `verified_pass`,
  `verified_fail`, `blocked_missing_context`,
  `blocked_implementation_failure`, or `escalate_to_user`.
- Completion-class statuses require a `## Verification Evidence` block with
  commands, output summary, commit SHA, files, and unresolved concerns.

## Git Discipline

Local commits are allowed when the active role completed a coherent change and
the repository instructions require a commit. Remote pushes are not automatic.
Only push when a human or this repository's explicit policy authorizes it.

## Boundaries

- `planner` does not implement code.
- `backend` does not take frontend-only work.
- `frontend` does not modify backend routes, persistence, or API contracts.
- `auditor` reports findings and routes; it does not silently fix defects.
- `validator` reports malformed state; it does not implement fixes.
- `finalizer` records approved closeout state; it does not audit or implement.

When role ownership is unclear, update the handoff with a specific question and
route to `user`.
