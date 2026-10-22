# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

It holds current work only. Closed epics, their ledger lines, and older
status notes are in `COMPLETED_WORK.md`; read that file only when you need
history.

## Current Status

- **Active Epic:** ACTIVE-PLANNER (epic 18). ACTIVE-1 (gate-verified
  handback) is implemented and awaits audit; ACTIVE-2 through ACTIVE-8
  remain. TIDY (epic 19) closed with TIDY-13, approved at `7816ae6` and
  merged in PR #272 (`4e380f9`).
- **Parked Epic:** None.
- **Other Open Epics:** ESCALATION-RECONCILE still waits on its trigger,
  WORK-INTAKE needs a design check, and LINEAR-SWITCH has low urgency.
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

### 11. LINEAR-SWITCH - mode switching and onboarding

Fit: low urgency; only repositories that switch tracker modes need it.
LINEAR-WORKFLOW is closed, so nothing blocks it.

- LIN-11: `init --tracker linear` and `init --tracker none` switch an
  initialized repository between modes, only between assignments and never
  while `.dispatch.lock` exists; `--dry-run`; `--force` reports what is left
  behind; a round trip is byte-identical.
- LIN-12: After `init --tracker linear`, print connector setup steps, the
  `doctor` command, and the probe command; add `docs/LINEAR.md`.

### 14. WORK-INTAKE - turn a plain request into epics and stories

Fit: needs a user design check before INTAKE-1. INTAKE-2 rewrites how the
planner turns a request into this file's epics, so take TPM-ROLE first and
write the intake rules once for the planner/tpm split.

Before scoping INTAKE-1, route to `user` for a short design check.

- INTAKE-1: A command that takes a plain-language request ("build me a web page
  about puppies") when no work is queued, writes it into `HANDOFF.md` as a
  request, and routes to `planner`. Refuse while an assignment is active or
  `.dispatch.lock` exists.
- INTAKE-2: Planner intake rules: architect the request, break it into epics
  and stories, add them to this file's Backlog (vanilla mode), then start the
  first story. Users never write Work Packets by hand.
- INTAKE-3: In Linear mode, the planner creates the epics and stories as Linear
  issues instead (after LINEAR-WORKFLOW).

### 16. ESCALATION-RECONCILE - a stale user escalation gets one planner pass

Fit: small (two stories) and independent. Pick it right after a run has
paused on a user escalation that turned out to be stale.

A `next_agent: user` handoff can be stale by the time a run starts: the
operator may have answered the question in a commit, merged the blocking pull
request, or edited `PROJECT_STATE.md` without touching the handoff. The source
project gives such a handoff one planner reconciliation pass before pausing.
Here the loop pauses at once (`llm_handoff/orchestrator.py` ~line 857) and
`pause_recovery` is defined as covering every pause except `next_agent: user`.
Add the pass as an explicit, bounded opt-in. It never loops: a planner that
confirms the blocker rewrites the handoff to `user` and the run pauses
normally with no second pass.

- RECON-1: Config and first-cycle pass. New top-level
  `reconcile_user_escalations: false` (the source project defaults to on;
  this reference pauses by default, so the operator turns it on). When on,
  the first cycle of a run that reads `next_agent: user` with
  `status: escalate_to_user` or `blocked_missing_context` from a producer
  other than `planner` dispatches the planner once with an instruction that
  names the pause reason, says the handoff may be stale, asks it to check the
  handoff against `PROJECT_STATE.md`, Git state, and open pull requests, and
  tells it to rewrite to `user` with the same question when the blocker
  still holds, or to the next role when it does not. The dispatch reuses the
  bounded-recovery path (`_schedule_pause_recovery`), counts in telemetry as
  `user_escalation_reconciliations`, and logs `Reconciling a user escalation
  from <producer> before pausing`. `--dry-run` prints the decision and
  dispatches nothing. A planner-produced user route on the first cycle pauses
  as today.
- RECON-2: Later cycles and the no-loop guard. On later cycles only a
  non-planner producer's user route gets the pass, once per handoff SHA. A
  user route the planner wrote during the pass, or any planner-produced user
  route, pauses with no further pass in the same run; the pause reason says
  `reconciled by planner`. In Linear mode the planner's instruction adds:
  when advancing from a leased issue whose latest evidence comment is
  `user_attention_requested`, post `user_attention_cleared` first; when
  reconfirming, post nothing. `CONFIGURATION.md` documents the key,
  `docs/handoff/PLANNER_HANDOFF_PROMPT.md` documents the pass, and the
  handbook's escalation section says a user escalation may be reconciled once
  when the key is on.

### 18. ACTIVE-PLANNER - finish stories without a person at the console

Fit: pick it right after a run has paused on an implementer environment
refusal (a permission rule, a missing binary) rather than on a question. The
stories run in order; ACTIVE-5 and ACTIVE-7 change the planner's own
protocol, so the epic goes before a feature. ACTIVE-8 is the operator's
`.claude/` edit and goes last. Does not overlap ESCALATION-RECONCILE,
which re-checks a stale `user` route; this epic keeps the loop from reaching
`user` for a config line it could have named.

On 2026-10-18 one story took three implementer dispatches and two planner
passes before the auditor saw it, with no code problem: the backend's
allowlist refused `python -m pytest`, so it could not post the fresh
`Commands run` the handbook demands, handed back
`blocked_implementation_failure`, and the planner re-dispatched the same
Work Packet. The work-claim gate runs the Verification command itself
whenever it applies (`verify_work_claim`, `llm_handoff/work_claim.py` ~line
1498; `_work_claim_gate_applies`, `llm_handoff/orchestrator.py` ~line
3174), so the implementer's own run is redundant evidence when the gate is
on. The validator checks only that each evidence field is non-empty
(`llm_handoff/validator.py` ~lines 646-669); the "fresh commands" rule
lives in the handbook (`docs/handoff/HANDBOOK.md` ~lines 62-77), the
implementer prompts (`docs/handoff/BACKEND_HANDOFF_PROMPT.md` ~line 61,
`FRONTEND_HANDOFF_PROMPT.md` ~line 68), and the auditor. Nothing checks,
before a dispatch, that the role can run the command it was given.

Groomed on 2026-10-19 from four stories into eight, each one Work Packet;
the `(was ...)` notes give the old IDs. None had started.

- ACTIVE-1 (was part of ACTIVE-1): Gate-verified handback. When an
  implementer cannot run the Work Packet's Verification command (a
  permission refusal, a missing tool), it may still hand back
  `status: ready_for_review` with a `## Verification Evidence` block whose
  `Commands run` reads `deferred to the work-claim gate: <the refusal,
  quoted>` and whose `Output summary` names the refusal; the other three
  fields stay as today. This is legal only when `enforce_work_packet: true`;
  without the gate the current rule stands. The validator accepts it as it
  does now (the field is non-empty). The gate runs the command as it does
  today and, on a pass, `record_passed_verification`
  (`llm_handoff/work_claim.py` ~line 2390, called at
  `llm_handoff/orchestrator.py` ~line 3367) writes the
  `## Dispatcher Verification` section the auditor helper files already tell
  the auditor to trust; that section is the evidence the auditor reads.
  Update the handbook's Evidence Block Reference, both implementer prompts,
  and the auditor prompt under `docs/handoff/` (a deferred `Commands run`
  with a PASS section is not a finding). The backend and frontend
  `verification deferred` patterns in
  `llm_handoff/rationalization_patterns.yaml` (~lines 10-12 and 20-22) must
  not match the new wording. Tests: the validator accepts the block, the
  gate still runs the command on such a claim, and an auditor fixture with a
  deferred `Commands run` plus a PASS section validates. Grooming
  (2026-10-22): the story also mirrors the four protocol files into
  `examples/reference-workflow/docs/handoff/` and adds detector regressions;
  the existing validator and gate behavior is retained. Progress: implemented
  2026-10-22, awaiting audit.
- ACTIVE-2 (was part of ACTIVE-1): Gate-verified handback in the non-Claude
  helper files: the `llm-handoff` and `llm-handoff-frontend` Codex skills
  and `backend.md` and `frontend.md` under `.agents/agents/` (root and
  `examples/reference-workflow/` copies) describe the ACTIVE-1 wording.
  Protected paths; list each in Files in scope.
- ACTIVE-3 (was part of ACTIVE-2): Verification preflight in `doctor`.
  Parse the live Work Packet when the handoff routes to an implementer
  (`doctor._check_handoff`, `llm_handoff/doctor.py` ~line 1873, already reads
  and routes it) and check that each command's leading executable
  (`command_executable`, `llm_handoff/work_claim.py` ~line 1455) is allowed
  by that role's launch, fallback included (`_role_launches`,
  `llm_handoff/doctor.py` ~line 370): for Claude, the `Bash(prefix:*)` rules
  of `claude_allowed_tools` (`llm_handoff/config.py` ~line 340, built from
  `claude_default_allowed_tools` ~line 65 and per-role `allowed_tools` ~line
  300, parsed as `_BASH_RULE_RE` does in
  `llm_handoff/agent_providers/claude.py` ~line 22); for agy, the grants
  `antigravity_allowed_commands` builds
  (`llm_handoff/antigravity_permissions.py` ~line 111); for Codex, which runs
  in a workspace-write sandbox with no command allowlist
  (`codex_permission_args`, `llm_handoff/agent_providers/codex.py` ~line
  294), only that the executable resolves. Also run
  `verification_command_allowed` (`llm_handoff/work_claim.py` ~line 1833)
  against the same commands. Report `WARN verification preflight: backend
  (claude) cannot run python; set interpreter_grants: true or an
  allowed_tools rule`, never FAIL. Put the check in one function the
  dispatcher can call in ACTIVE-4. Tests in `tests/test_doctor.py`.
- ACTIVE-4 (was part of ACTIVE-2): Verification preflight in the
  dispatcher. Call the ACTIVE-3 check at startup (`run_loop`,
  `llm_handoff/orchestrator.py` ~line 456, next to the pre-launch checks
  there) and in every cycle that dispatches an implementer, next to
  `capture_pre_dispatch_state` (~line 1170), because a Work Packet the
  planner writes mid-run never passes startup. It logs the same WARN and
  never blocks, so the planner can re-scope. Startup and per-cycle tests in
  `tests/ported/test_orchestrator.py` with `--dry-run`.
- ACTIVE-5 (was ACTIVE-3): Test scope policy. In
  `docs/handoff/PLANNER_HANDOFF_PROMPT.md` (the Work Packet section, ~lines
  78-150) and the handbook: for a story that changes behaviour, tests that
  assert the old behaviour are in scope by definition. The planner lists
  `tests/` (a directory, which `path_matches_entry` in
  `llm_handoff/work_claim.py` ~line 618 already supports) or the specific
  test files, and never writes "stop if a test outside Files in scope must
  change" or a variant. With `interpreter_grants: true` the planner's default
  allowlist includes `pytest` (`claude_default_allowed_tools`,
  `llm_handoff/config.py` ~line 65), so the prompt tells it to run the
  relevant tests read-only while scoping, to find which files assert the old
  behaviour. Add a `planner` pattern for the forbidden phrase in
  `llm_handoff/rationalization_patterns.yaml` (~line 24; the validator
  surfaces it through `_rationalization_warnings`,
  `llm_handoff/validator.py` ~line 747) and mirror the rule in
  `.codex/skills/llm-handoff-planner/SKILL.md` and `.agents/agents/planner.md`
  (not `.gemini/agents/`, which GEM-5 deleted). Tests: `parse_work_packet`
  accepts `tests/` as a Files in scope entry and the gate treats a new file
  under it as in scope (`tests/test_work_claim.py`, next to the parser tests
  at ~line 301), and `tests/test_rationalization_detector.py` catches the
  phrase.
- ACTIVE-6 (was part of ACTIVE-4): Environment refusal bullet and validator
  warning. An implementer that hands back `blocked_implementation_failure`
  for an environment reason writes one bullet in the handback body,
  `- **Environment refusal:** <permission rule | missing binary | no TTY>:
  <the refusal text, quoted>`; both implementer prompts under
  `docs/handoff/` and the ACTIVE-2 helper files add it, and
  `_bullet_field_values` (`llm_handoff/validator.py` ~line 834) already
  parses bullets of that shape. The dispatcher's own refusal log lines
  (`llm_handoff/agent_providers/claude.py` ~lines 203-208, `antigravity.py`
  ~line 35) are not handoff content and cover only permission rules, so the
  validator does not key on them. Add a validator warning
  `planner_redispatch_after_refusal` when a planner handoff routes to the
  implementer that produced the previous handoff, the Work Packet section
  (`extract_work_packet_section`, `llm_handoff/work_claim.py` ~line 452) is
  unchanged, and the previous handoff carries that bullet.
  `validate_handoff_text` already takes `previous_route` and
  `prior_handoff_content` (`llm_handoff/validator.py` ~lines 376-385); the
  orchestrator passes the story baseline there (~line 1468), so the story
  confirms that baseline is the implementer handback the planner read or
  passes the handback separately. The `agent_self_loop` check (~line 466)
  does not cover this case, since planner to backend is a different role.
  Tests in `tests/ported/test_validator.py`. Grooming (2026-10-22): the
  validator argument is `previous_agent`, not the stale `previous_route`
  reference above; the story still checks that the prior content is the
  actual implementer handback rather than assuming the story baseline
  supplies it.
- ACTIVE-7 (was part of ACTIVE-4): No pointless re-dispatch, planner side.
  When the planner receives `blocked_implementation_failure` whose handback
  carries the ACTIVE-6 bullet, it must not re-dispatch the same role with an
  unchanged Work Packet: it either applies ACTIVE-1 (re-dispatch with an
  instruction to hand back gate-verified) or routes to `user` with the exact
  config line to add, such as `interpreter_grants: true`. Encode this in the
  planner prompt's handback review, the handbook's Escalation Protocol
  (`docs/handoff/HANDBOOK.md` ~lines 125-142), and the ACTIVE-5 planner
  helper files outside `.claude/`.
- ACTIVE-8: Operator edit of the Claude helper files (routes to `user`).
  Claude roles cannot write `.claude/`, so the planner writes the exact
  edits into the handoff, as GEM-5c did: the ACTIVE-1 and ACTIVE-6 wording in
  `backend.md` and `frontend.md`, and the ACTIVE-5 and ACTIVE-7 rules in
  `planner.md`, under `.claude/agents/` (root and
  `examples/reference-workflow/` copies). The auditor helper already trusts
  the PASS section (`.claude/agents/auditor.md` ~line 46), so it needs no
  change unless ACTIVE-1's auditor prompt wording must be mirrored. If
  TIDY-7 is still open, list its edits in the same handoff (operator
  batching, 2026-10-20). Grooming (2026-10-22): use the handbook's Operator
  Stories artifact contract under `logs/dispatch/operator/ACTIVE-8/`, with
  complete files or an applyable patch and an apply command; prose-only
  edits in the handoff are insufficient. TIDY-7 is already closed per the
  planner, so there is no TIDY-7 batching work. The story stays last and
  operator-owned.

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

- From the CHAT-26 audit (medium, security reviewer): on Windows
  `_write_handoff_durably` replaces `HANDOFF.md` with a temporary file that
  takes the directory's inherited ACL, so an explicit DACL on the old
  handoff is not kept. Fixing it means copying the security descriptor
  before the replace and failing closed when it cannot be copied.

- From the TIDY-15 audit (medium, security reviewer; kept in Open Followups
  by the planner for a future hardening pass): removing the Linear write tools from Claude
  `--allowedTools` for report-only validator launches does not deny writes
  that inherited user or project permissions already allow. The reviewer
  recommends explicit `--disallowedTools` entries for the configured
  server's `save_comment` and `save_issue`, covering fallback launches and
  keeping existing denials.

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file and holds current work only.
  On each epic close the finalizer moves the closed epic and its status notes
  to `COMPLETED_WORK.md`, appends its ledger line there, and advances Active
  Epic here.
- Git history is the durable record of completed work.
