# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

It holds current work only. Closed epics, their ledger lines, and older
status notes are in `COMPLETED_WORK.md`; read that file only when you need
history.

## Current Status

- **Active Epic:** WORK-INTAKE (epic 14), on INTAKE-1 (bounded proposal or
  escalation previews at user pauses). The planner chose it by fit after
  ESCALATION-RECONCILE (epic 16) closed with RECON-2 approved at `0427989`
  and merged in PR #295 (`3b37cb7`); the pre-INTAKE-1 design check is
  resolved by the planner-approved redesign below.
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

### 14. WORK-INTAKE - turn a plain request into epics and stories

Intro: turn a plain-language request typed at the operator prompt during a
user pause into reviewed epics and story outlines, record the approved plan
in `PROJECT_STATE.md` (or create Linear issues in Linear mode), and open the
first epic by fit without requiring the user to edit `HANDOFF.md`,
`PROJECT_STATE.md`, or Work Packets by hand.

Fit: unblocked and ready to open. The pre-INTAKE-1 design check is resolved,
TPM-ROLE and ESCALATION-RECONCILE are closed, and no other open epic remains
in the backlog.

Goal: let an operator type a plain-language request at a user pause, read the
planner's `## Proposed Backlog` (or an escalation's question) directly in the
terminal, revise it by typing changes or approve it by pressing Enter, have
the planner record the approved epics in `PROJECT_STATE.md` (with the
dispatcher committing `PROJECT_STATE.md` only in `commit_mode: dispatcher`,
or creating Linear issues in Linear mode) and open the first epic by fit.

Boundaries:

- Do not add a `request` CLI subcommand; requests arrive through the existing
  operator prompt at a pause.
- Proposal handoffs (`## Proposed Backlog`) contain epics (intro, goal,
  boundaries, `Fit:` line) and story outlines only, never Work Packets, and do
  not edit `PROJECT_STATE.md` or create Linear issues until the operator
  approves.
- Terminal display of a user escalation's question or `## Proposed Backlog`
  at a user pause must be bounded in length so the terminal stays readable
  without opening `HANDOFF.md`.
- At a proposal review (`## Proposed Backlog` on a user pause), the operator
  prompt line tells the operator to press Enter to approve/start the plan or
  type changes to revise it. Pressing Enter sends an explicit approval
  instruction to the planner (not `OPERATOR_BEST_JUDGMENT_REQUEST`) that
  permits editing `PROJECT_STATE.md` (`config.project_state_path`). Typed text
  sends revision guidance back to the planner, which revises
  `## Proposed Backlog` and routes to `user` again (no automatic loop bound).
- In `commit_mode: dispatcher`, after the approval turn, the dispatcher
  commits `config.project_state_path` (`PROJECT_STATE.md`) and only that file
  (same local, ledger-only commit shape as a finalizer epic-close commit,
  published with the next story's pull request). If the approval turn leaves
  any other file (besides `HANDOFF.md`) changed, the dispatcher pauses
  instead of committing.
- On approval, the planner opens the first epic by fit: it routes to `tpm`
  when `agents.tpm` is configured (so the `tpm` sizes stories and writes Work
  Packets) and otherwise writes the first Work Packet itself. If a request
  was proposed and approved while another epic was open, the approved epics
  are added to the backlog and the planner keeps choosing the next epic by
  fit.
- In Linear mode, the planner creates the Linear issues at approval, never
  during proposal/revision drafts.
- Follow the `.claude/` rule above: Claude roles cannot write `.claude/`, so
  any `.claude/` agent/template edits needed by prompt stories are split into
  an operator story routed to `user` at the end of the epic rather than mixed
  with code or Claude-dispatched packets.

Stories, in execution order (INTAKE-2 was split into INTAKE-5 and INTAKE-6 to
separate planner protocol changes from operator-only Claude file edits):

- INTAKE-1: Bounded proposal or escalation previews at user pauses: print
  the current handoff's nonempty `## Proposed Backlog`, or its escalation
  body, once per user-route pause, capped by lines and characters with
  control characters escaped.
- INTAKE-5 (was INTAKE-2): Planner intake/revision protocol in the handbook,
  planner initial/handoff prompts, `.agents/agents/planner.md`,
  `.codex/skills/llm-handoff-planner/SKILL.md`, and corresponding
  reference-workflow copies. When no epic has an unblocked story, invite a
  request at the operator prompt. Recognize plain-language new-work requests,
  propose epics with intro, goal, boundaries, Fit and story outlines under
  `## Proposed Backlog`, and revise on guidance. Drafts route to user without
  Work Packets, project-state edits, or Linear mutations. Approval requires
  the explicit instruction supplied by INTAKE-3. Verification focus:
  prompt/template parity and draft mutation boundaries. Excludes `.claude/`;
  INTAKE-6 carries those copies.
- INTAKE-3: Proposal approval transaction in `operator_prompt.py`,
  `orchestrator.py`, and `dispatcher_commit.py` with tests and supporting
  docs. Distinguish Enter approval from typed revision and explicitly
  authorize the planner to record the approved backlog in
  `config.project_state_path`. Preserve ordinary pause behavior. Open the
  first epic by fit, routing to tpm when configured, otherwise assigning its
  Work Packet; preserve existing open work when adding approved epics. In
  dispatcher commit mode commit only the configured project-state file,
  excluding HANDOFF.md; pause without committing if any other file changed.
  Reuse the finalizer ledger-only commit pattern without permitting
  completed-work edits. Verification focus: the approval transaction
  authorizes exactly the project-state change; drafts/revisions never
  authorize it. Approval and its commit guard stay together as one behavior.
- INTAKE-4: Linear approval integration through `llm_handoff/init_workflow.py`,
  `llm_handoff/prompt_templates/linear/`, generated instruction sections,
  tests and docs. Drafts and revisions remain in HANDOFF.md without Linear
  calls; approval creates the approved epics/stories in Linear and starts the
  first issue under the existing Linear workflow. Verification focus:
  generated Linear planner instructions and approval-only mutation. Any
  `.claude/` copy edits belong to INTAKE-6.
- INTAKE-6 (was INTAKE-2): Final operator story synchronizing
  `.claude/agents/planner.md` and
  `examples/reference-workflow/.claude/agents/planner.md` with completed
  intake/revision/approval rules, including applicable Linear parity from
  INTAKE-4. Prepare complete file artifacts or an applyable patch under
  `logs/dispatch/operator/INTAKE-6/` and route to user under the handbook
  Operator Stories contract. No runtime changes. Verification focus: Claude
  planner copy parity after the operator applies the artifact. This story
  stays last; full provider parity is not complete until it is applied.

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
  refusal category and a sanitized diagnostic. Not yet scheduled; no epic is
  opened for it here.

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file and holds current work only.
  On each epic close the finalizer moves the closed epic and its status notes
  to `COMPLETED_WORK.md`, appends its ledger line there, and advances Active
  Epic here.
- Git history is the durable record of completed work.
