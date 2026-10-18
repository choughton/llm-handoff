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
bounce_count: 0          # optional informational retry count; the dispatcher keeps its own in logs/dispatch/
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

Each status implies a route, and `next_agent` must name the same one:
`ready_for_review` goes to `auditor`; `verified_pass` goes to `planner`, or to
`finalizer` with `close_type: epic`; `verified_fail` goes to `backend`,
`frontend`, or `planner`; `blocked_implementation_failure` goes to `planner`;
`blocked_missing_context` and `escalate_to_user` go to `user`. When they disagree, the router and the post-dispatch validator reject
the frontmatter. A handoff whose `next_agent` is not `user` is sent to the
planner to repair it, and one whose `next_agent` is `user` pauses the loop.

Legacy handoffs without `status` are still parsed during extraction, but new
handoffs should emit the enum verbatim. Do not invent synonyms such as `done`,
`approved`, or `blocked`.

## Verification Evidence

The `## Verification Evidence` block is required when `status` is
`ready_for_review`, `verified_pass`, or `verified_fail`, except on the
finalizer's `verified_pass` epic-close handoff, which carries the auditor's
approval forward. Use this exact five-field shape:

```markdown
## Verification Evidence

- **Commands run:** verbatim command lines
- **Output summary:** one line per command with exit codes
- **Commit SHA verified:** concrete 7-40 character Git SHA; never `HEAD`
- **Files changed or reviewed:** relative paths
- **Unresolved concerns:** list or `none`
```

A field's value may continue on indented lines below the label, such as
nested `- ...` bullets under `Commands run`. A label may carry one
parenthetical note before its colon, as in `- **Commands run (this turn):**`.
Write the `Commit SHA verified` SHA on the label line itself.

Evidence must be fresh for the current handoff. Results from a previous session,
assumptions, and stale command output do not satisfy the contract.

## Work Packet

Planner handoffs to `backend` or `frontend` must include a `## Work Packet`
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

Do not omit `Files out of bounds`, even when the value is `none`. Do not list
the same path under both fields; the validator rejects that packet as
`work_packet_unparseable`. To limit edits within a file, keep it in `Files in
scope` and state the limit in the Objective.

The validator enforces this contract on every planner handoff that is not
routed to `user`:

- a planner handoff with no `## Work Packet` section fails with
  `scope_claim_mismatch`;
- a missing or empty Work Packet field is reported as
  `acceptance_coverage_unclear`;
- the pre-extraction `## Task Assignment` block with `Objective` and
  `Acceptance Criteria` headings is still accepted as a legacy alias, but it
  emits a `task_assignment_deprecated` warning.

Keep the paths concrete and the command runnable from the repository root.
Separate multiple paths with commas; wrap a path that contains spaces in
backticks, for example `` `docs/user guide.md` ``.
Write more than one verification command as one command per nested list
item under `Verification command`, with no prose such as `then` or `and`
between them; every line runs as its own command.
When the dispatcher runs with `enforce_work_packet: true`, it parses exactly
these fields, diffs the implementer's commits against `Files in scope` and
`Files out of bounds`, and runs `Verification command` itself before the
auditor is dispatched. Every handoff that routes work to `backend` or
`frontend` must then carry a parseable Work Packet, including `verified_fail`
audit results, so implementer handbacks should preserve the section verbatim
and auditors should carry it forward.
`Files in scope` also controls edits to protected control files such as
`AGENTS.md`, the handoff prompts, `dispatch_config.yaml`, and the `.claude/`,
`.codex/`, and `.gemini/` directories. A role that changes one of these
without the path listed under `Files in scope` fails closed: the dispatcher
rewrites the handoff to route to `user` with a `protected_file_modified:`
error. List the exact path, or a directory ending in `/`, when a role is
meant to edit a control file.

## Rules

- Use canonical role values in `next_agent`; provider names are adapter examples only.
- Do not write `scope_sha: HEAD`. Run `git rev-parse HEAD` and write the concrete 7-40 character hex SHA.
- Agents are not authorized to push unless the consuming repository explicitly grants that role permission.
- If routing is ambiguous, route to `validator` or `user`; do not guess.
