# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

It holds current work only. Closed epics, their ledger lines, and older
status notes are in `COMPLETED_WORK.md`; read that file only when you need
history.

## Current Status

- **Active Epic:** REFUSAL-REDACTION (epic 23), opened after operator approval
  of the Work Intake proposal. The tpm groomed it with REDACT-1 and REDACT-2
  kept in order and scope, no splits or additions. REDACT-1 (redaction wording
  across the non-Claude protocol, prompt, and helper copies, with tests) is
  implemented and awaiting audit; REDACT-2, the `.claude/agents/` operator
  story, is pending. WORK-INTAKE (epic 14) closed with INTAKE-6 approved at
  `989f2902c44a62fdff96ec31bdb6e0c96744f159` and merged in PR #304
  (`e5a6ea4`).
- **Parked Epic:** None.
- **Other Open Epics:** None.
- **Epic Awaiting Close:** None.
- **Current Blocker:** None.
- **Active Branch:** main (story branches `llm-handoff/*`, one pull request per
  story).

## Backlog

The planner chooses the next epic by fit, not by position in this list.
Numbers are labels, not priorities. When the active epic closes, or when its
next story waits on something outside the loop, the planner reads every open
epic's intro and `Fit:` line and picks the one whose first story is unblocked,
whose dependencies are closed, and whose result the loop needs soonest. A
story that unblocks a parked epic, fixes what the last run broke, or changes
the planner's own protocol goes before a feature; a cheap story that fills a
wait goes before a long one that would hold it. The planner states the choice
and the reason in the handoff `reason`, and has the next Work Packet record it
in Current Status.

Parking. An epic whose next story waits on the operator (a live check, a
decision, a merge) is parked, not closed. Instead of routing to `user` and
pausing the loop, the planner picks another epic and has that epic's first
Work Packet record the parked epic in Current Status: its name, its remaining
stories, and the exact operator action it waits for, so the ask survives the
next handoff rewrite. One epic is active at a time. The planner returns to a
parked epic when the operator reports the blocker cleared, in this file or at
the operator prompt. Route to `user` only when no open epic has an unblocked
story.

Each story is one Work Packet; the role that assigns it writes its Files in
scope, Verification command, and acceptance criteria from the code. With a
tpm configured, that is the tpm; without one, it is the planner. Add, drop,
or re-shape epics by editing this list; that stays with the planner, which
also writes each epic's intro, `Fit:` line, and story outline.

Story size and IDs (operator rule, 2026-10-19). With a tpm configured, the
tpm sizes, splits, and renumbers the stories inside the open epic under these
rules; without one, the planner does. A story is one behavior
change: about four source modules or fewer plus their tests and docs, one
verification focus, and one role. A story that would edit `.claude/` does
not mix that edit with code: Claude roles cannot write `.claude/`, so those
edits go in one operator story that routes to `user`, at the end of the
epic unless a later story needs those files first.
Story IDs are flat: `EPIC-N` only. When a story turns out too large, replace
it with new stories numbered after the epic's highest number and add
`(was EPIC-3)` to each; never use letter or nested suffixes such as `3a` or
`3b1`. Merged stories keep their IDs. Stories not started yet may be
renumbered in execution order.

Closed epics, with their full intros and story lists, are in
`COMPLETED_WORK.md`; the open epics below keep their numbers.

### 23. REFUSAL-REDACTION - redact sensitive values in environment-refusal diagnostics

Intro: From the ACTIVE-6 and ACTIVE-7 security audits (medium, non-blocking):
implementer `Environment refusal` bullets and gate-deferred `Commands run`
entries quote raw refusal text, which can copy credentials, tokens, or
sensitive command arguments into `HANDOFF.md`, where the handbook and planner
prompts also instruct agents to quote it.

Fit: Unblocked, self-contained security hardening, and the only actionable item
remaining in `PROJECT_STATE.md`.

Goal: Require redacting credentials, tokens, and sensitive command arguments in
quoted `Environment refusal` bullets and `deferred to the work-claim gate:`
refusal diagnostics across the handbook, implementer prompts, planner prompts,
and helper/template copies (root and `examples/reference-workflow/`), while
preserving the refusal category (`permission rule`, `missing binary`,
`no TTY`) and a sanitized diagnostic; remove the closed followup from
`PROJECT_STATE.md`.

Boundaries:

- Keep the existing `- **Environment refusal:** <permission rule | missing binary | no TTY>: <...>`
  and `deferred to the work-claim gate: <...>` syntax so validator parsing
  (`_environment_refusal`, `planner_redispatch_after_refusal`) and existing
  valid handoffs continue to work.
- Update root and `examples/reference-workflow/` copies of protocol docs, role
  prompts, `.agents/agents/`, and `.codex/skills/` together with
  `tests/test_reference_workflow.py` and `tests/ported/test_handoff_docs.py`.
- Keep `.claude/agents/` edits (`backend.md`, `frontend.md`, `planner.md` in
  root and `examples/reference-workflow/`) in a separate final operator story
  routed to `user` with an operator artifact under `logs/dispatch/operator/`.

Stories:

- REDACT-1: Require redacting credentials and sensitive arguments in
  `Environment refusal` bullets and `deferred to the work-claim gate:`
  diagnostics across `docs/handoff/` (`HANDBOOK.md`, `README.md`,
  `BACKEND_HANDOFF_PROMPT.md`, `FRONTEND_HANDOFF_PROMPT.md`,
  `AUDITOR_HANDOFF_PROMPT.md`, `PLANNER_HANDOFF_PROMPT.md`,
  `PLANNER_INITIAL_PROMPT.md`), `.agents/agents/{backend,frontend,planner}.md`,
  `.codex/skills/{llm-handoff,llm-handoff-frontend,llm-handoff-planner}/SKILL.md`,
  and their `examples/reference-workflow/` copies; update
  `tests/test_reference_workflow.py`, `tests/ported/test_handoff_docs.py`,
  `PROJECT_STATE.md`, and `CHANGELOG.md`.
- REDACT-2: Operator story synchronizing
  `.claude/agents/{backend,frontend,planner}.md` and
  `examples/reference-workflow/.claude/agents/{backend,frontend,planner}.md`
  with the `REDACT-1` redaction wording.

## Open Followups

Promoted to HARDENING stories and removed below: AUDIT-COST (a),
PAUSE-RECOVERY (c) and (d), CONFIG-CLEANUP (b), and LINEAR-FOUNDATIONS (d).
AUDIT-COST (b) and (c) closed by GEM-4b. On 2026-10-19 every other open item
was promoted to epic 19 (TIDY) except the operator check and the two design
notes below; the remaining letters are unchanged.

- From the LINEAR-FOUNDATIONS close (low, LIN-6b), an operator action:
  - (b) the probe's isolation (`llm_handoff/doctor.py`,
    `_TRACKER_PROBE_SETTINGS` ~line 1490) has not been checked against a live
    Claude CLI; an operator should run
    `llm-handoff doctor --probe-tracker <ISSUE>` once in a repo with a project
    hook and confirm the hook does not fire.

- From the source-dispatcher comparison (2026-10-18, info), design notes:
  - (c) Design choice, not a gap: the source project removed the Work Packet
    from the handoff and keeps the issue body in Linear only. This repo keeps
    the Work Packet in `HANDOFF.md` because the work-claim gate, scanners,
    protected paths, and dispatcher commit mode parse it; revisit only if a
    thin-lease mode is wanted for Linear repositories.
  - (d) Design choice, not a gap: the source project routes on a three-level
    hierarchy (initiative, project, issue) with project-open and project-close
    gates. This repo has epic and story in `remaining_stories` plus this file;
    the TPM-ROLE epic scopes the picker to one epic for that reason.

- From the ACTIVE-6 audit (medium, security, non-blocking): an implementer's
  `Environment refusal` bullet quotes the refusal text, which can carry
  credentials or sensitive command arguments into `HANDOFF.md`. The ACTIVE-7
  audit widened the scope: the planner instructions and handbook copies
  (`docs/handoff/HANDBOOK.md`, `docs/handoff/PLANNER_HANDOFF_PROMPT.md`,
  their `examples/reference-workflow/` copies, and the planner helpers) also
  ask agents to quote that refusal text. A future bounded change must
  require redaction in the implementer prompts, the planner instructions,
  the handbook, and their helper and template copies, while keeping the
  refusal category and a sanitized diagnostic. Tracked by epic 23
  (REFUSAL-REDACTION): REDACT-1 covers the non-Claude surfaces and REDACT-2
  the `.claude/agents/` copies; remove this item when REDACT-2 completes.

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file and holds current work only.
  On each epic close the finalizer moves the closed epic and its status notes
  to `COMPLETED_WORK.md`, appends its ledger line there, and advances Active
  Epic here.
- Git history is the durable record of completed work.
