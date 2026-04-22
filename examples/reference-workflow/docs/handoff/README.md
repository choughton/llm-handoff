# Handoff Prompt Files

This directory contains the reference prompt files used by `llm-handoff` plus the single live handoff state file, `HANDOFF.md`.

The handoff file is the mutex and the debugger: every transition is visible as text, and the dispatcher only advances when the route parses, validates, and maps to a known role.

## Files

| File | Role | Purpose |
|---|---|---|
| `HANDOFF.md` | shared | Live state file read and overwritten by agents |
| `HANDBOOK.md` | all agents | Shared operating rules for status, evidence, escalation, and failure modes |
| `SHARED_REPO_INIT_PROMPT.md` | all agents | Common bootstrap instructions for fresh sessions |
| `PLANNER_INITIAL_PROMPT.md` | planner | First planner session prompt |
| `PLANNER_HANDOFF_PROMPT.md` | planner | Ongoing planner dispatch prompt |
| `BACKEND_HANDOFF_PROMPT.md` | backend | Backend implementation prompt; Codex is one adapter example |
| `FRONTEND_HANDOFF_PROMPT.md` | frontend | Frontend implementation prompt; Gemini is one adapter example |
| `AUDITOR_HANDOFF_PROMPT.md` | auditor | Audit prompt; Claude is one adapter example |
| `FINALIZER_HANDOFF_PROMPT.md` | finalizer | Project-state and handoff-close prompt |

## Required Frontmatter

Every `HANDOFF.md` write must begin with YAML frontmatter. The dispatcher treats the YAML block as authoritative; prose sections are context for humans and agents.

```yaml
---
next_agent: <enum>       # required: planner | backend | frontend | auditor | validator | finalizer | user
reason: <string>         # required: quote every `reason` value
epic_id: <string>        # optional active epic identifier
story_id: <string>       # optional active story identifier
story_title: <string>    # optional short active story title
remaining_stories:       # optional remaining story IDs/titles
  - <story id/title>
status: <enum>           # required for completion-class handoffs; see below
bounce_count: 0          # optional dispatcher-maintained story retry count
evidence_present: true   # optional validator hint for evidence-aware handoffs
scope_sha: <git SHA>     # required when close_type is story or epic
close_type: <enum>       # optional: story | epic
prior_sha: <git SHA>     # optional prior verified SHA
producer: <string>       # required: planner | backend | frontend | auditor | validator | finalizer | user
---
```

## Status Values

Use exactly one canonical `status` value when the handoff claims completion,
audit outcome, or blocking state.

| Status | Meaning | Typical emitter |
|---|---|---|
| `ready_for_review` | Implementation is complete and needs audit. | backend, frontend |
| `verified_pass` | Auditor verified the assignment and quality gates. | auditor |
| `verified_fail` | Auditor found a defect and routes back to implementation. | auditor |
| `blocked_missing_context` | The role cannot proceed without more information. | any role |
| `blocked_implementation_failure` | Implementation was attempted but structurally failed. | backend, frontend |
| `escalate_to_user` | Human decision is required. | any role |

Legacy handoffs without `status` are still parsed during extraction, but new
handoffs should emit the enum verbatim. Do not invent synonyms such as `done`,
`approved`, or `blocked`.

## Verification Evidence

The `## Verification Evidence` block is required when `status` is
`ready_for_review`, `verified_pass`, or `verified_fail`. Use this exact
five-field shape:

```markdown
## Verification Evidence

- **Commands run:** verbatim command lines
- **Output summary:** one line per command with exit codes
- **Commit SHA verified:** concrete 7-40 character Git SHA; never `HEAD`
- **Files changed or reviewed:** relative paths
- **Unresolved concerns:** list or `none`
```

Evidence must be fresh for the current handoff. Results from a previous session,
assumptions, and stale command output do not satisfy the contract.

## Work Packet

Planner handoffs to `backend` or `frontend` should include a `## Work Packet`
block with these six fields:

```markdown
## Work Packet

- **Objective:** one bounded result
- **Files in scope:** relative paths
- **Files out of bounds:** relative paths or `none`
- **Context:** required reading or background
- **Verification command:** exact command to run
- **Expected next route:** role after success
```

Do not omit `Files out of bounds`, even when the value is `none`.

## Rules

- Use canonical role values in `next_agent`; provider names are adapter examples only.
- Do not write `scope_sha: HEAD`. Run `git rev-parse HEAD` and write the concrete 7-40 character hex SHA.
- Agents are not authorized to push unless the consuming repository explicitly grants that role permission.
- If routing is ambiguous, route to `validator` or `user`; do not guess.
