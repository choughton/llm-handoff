# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

It holds current work only. Closed epics, their ledger lines, and older
status notes are in `COMPLETED_WORK.md`; read that file only when you need
history.

## Current Status

- **Active Epic:** TRANSITION-RULES (epic 17), opened by the planner after
  GROOMING closed at `d4bdd04`; the tpm groomed it with all six stories kept
  in order. TRANS-1 (implementer producers: `backend` and `frontend` route
  only to `auditor`, `planner`, or `user`, and never emit `verified_pass`,
  `verified_fail`, or `close_type: epic`) was merged in PR #249 at
  `7e061a2`. TRANS-2 (auditor and finalizer producers, per the planner's
  2026-10-21 TRANS-2/TRANS-3 boundary decision recorded in epic 17) was
  approved at `bb5a247` and merged in PR #250 at `99c8bbd`. TRANS-3
  (validator producer: keyword-only `dispatcher_authored` flag on the three
  validation entry points; validator verdicts need it, and
  `ready_for_review`, `next_agent: finalizer`, and `close_type: epic` stay
  rejected; PR #251 Finding 1 fixed in-story with SHA-256 provenance in
  `logs/dispatch/dispatcher-handoff.json`, accepted by the planner as the
  final provenance mechanism) was approved at `9642d8a` and merged in PR
  #251 at `733d53f`. TRANS-4 (every dispatched role's handoff must name
  that role as `producer`, else `frontmatter_producer_mismatch`;
  dispatcher-authored handoffs skip only that check; a chat takeover
  rewrite may name `planner` or `user` through the `chat_takeover` flag) is
  implemented, with the chat-takeover fix, and awaiting audit. TRANS-5 and
  TRANS-6 remain.
- **Parked Epic:** TIDY, with TIDY-8, TIDY-12, and TIDY-13 remaining
  (TIDY-12 and TIDY-13 are wording nits filed by the operator on
  2026-10-21). TIDY-8 is a backend story the planner picks by fit (the
  operator confirmed both events on 2026-10-20).
- **Other Open Epics:** ACTIVE-PLANNER and ESCALATION-RECONCILE still wait
  on their triggers, WORK-INTAKE needs a design check, and LINEAR-SWITCH has
  low urgency.
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

### 17. TRANSITION-RULES - per-producer legality in the validator

Fit: unblocked; TPM-ROLE is closed, so TRANS-2's `tpm` dependency is met.
Next in line after GROOMING (operator direction 2026-10-21); otherwise
independent, and each story is small enough to fill a wait.

The validator checks that `status` implies `next_agent` and that `finalizer`
is reached only from the auditor with `close_type: epic`
(`llm_handoff/validator.py` ~lines 230-244). It does not check who may emit a
status or who an implementer may route to, so a backend handback with
`status: verified_pass` or a planner handoff to `finalizer` with a forged
`producer: auditor` is only caught by prompts. The source project enforces
a per-producer table. Make the table explicit here, enforce it, and keep the
handbook and the code from drifting.

- TRANS-1: Implementer producers. A handoff with `producer: backend` or
  `producer: frontend` may route only to `auditor`, `planner`, or `user`
  (`frontmatter_routing_rule`); `verified_pass`, `verified_fail`, and
  `close_type: epic` from an implementer are rejected
  (`frontmatter_status_producer_rule`). Inspect every fixture under
  `tests/ported/fixtures/handoffs/` and the reference-workflow starter
  handoff (`producer: user`, which stays accepted). Intentional legacy and
  negative routing cases (empty, malformed, frontmatter-free inputs) keep
  their expected router/validator results; a purportedly valid implementer
  fixture that violates the new rules is updated, not exempted or
  grandfathered.
- TRANS-2: Auditor and finalizer producers. `verified_pass` and
  `verified_fail` require `producer: auditor` (`frontmatter_status_producer_rule`
  for planner, user, implementer, tpm, and unrecognized producers);
  `producer: validator` keeps its existing behavior until TRANS-3.
  `producer: auditor` never emits `ready_for_review` and never routes to
  `auditor`. `producer: finalizer` routes only to `planner`, `tpm`, or
  `user` (when `agents.tpm` is unset, the existing TPM-3 check intercepts
  `next_agent: tpm`), never sets `close_type` (`story` or `epic`), and emits
  only `verified_pass`, `blocked_missing_context`, `escalate_to_user`, or no
  status. A finalizer `verified_pass` after a known non-finalizer dispatch
  is rejected, and the evidence exemption applies only when both the
  previous route and the normalized producer are `finalizer` (doctor and
  supervisor preflight pass the producer as the previous agent).
- TRANS-3 (was part of TRANS-2): Validator producer and dispatcher-authored
  handoffs. Decided 2026-10-21: the dispatcher's own builders
  (`build_review_bounce_handoff()` and `build_deferred_findings_handoff()`
  in `llm_handoff/publish.py`, `build_bounce_handoff()` in
  `llm_handoff/work_claim.py`, and `_write_protected_file_escalation()` in
  `llm_handoff/orchestrator.py`) keep `producer: validator`; no new
  `dispatcher` producer string is added. Instead `validate_handoff_frontmatter()`,
  `validate_handoff()`, and `validate_handoff_text()` gain a keyword-only
  `dispatcher_authored: bool = False`. By default `producer: validator` may
  route anywhere except `finalizer`, may not set `close_type: epic`, and may
  not emit `ready_for_review`, `verified_pass`, or `verified_fail`. With
  `dispatcher_authored=True` it may also emit `verified_fail` or
  `verified_pass`; `ready_for_review`, `next_agent: finalizer`, and
  `close_type: epic` stay rejected. The validator-producer regression cases
  are updated in this story, and the review-bounce fixtures must keep
  validating. Provenance (PR #251 Finding 1, fixed in-story): when the
  dispatcher writes one of these handoffs it records the handoff's
  normalized SHA-256 in `logs/dispatch/dispatcher-handoff.json`
  (`DISPATCHER_HANDOFF_STATE_PATH`), and it clears that record when any
  agent dispatch returns, before post-dispatch validation. `llm-handoff
  doctor` and supervisor preflight pass `dispatcher_authored=True` only
  while `HANDOFF.md` matches the record, so any edit drops the trust; the
  record is never read or written through a symlink or junction on `logs`,
  `logs/dispatch`, or the file. Post-dispatch, chat, repair, and
  interrupted-role validation keep `dispatcher_authored=False`. The
  provenance flag waives only the producer rule, so the two publish
  builders now write a `## Verification Evidence` block naming the
  reviewed story-branch head (the deferred-findings builder takes it as
  `head_sha`), and the doctor and supervisor regressions use the real
  builders' output. Residual (audit, medium): the record is an unkeyed
  digest in agent-writable `logs/`, so a record an agent plants during a
  dispatch the dispatcher never returns from is not cleared. Decided by
  the planner on 2026-10-21:
  accepted within the workspace-local reference architecture (AGENTS.md
  Scope Boundaries forbid out-of-repo state stores or key daemons,
  orchestrator post-dispatch validation never reads the record, and
  evidence, SHA, and finalizer/epic-close rules still apply).
- TRANS-4 (was part of TRANS-2): Producer matches the dispatched role. The
  post-dispatch validation already knows the previous route; a `producer`
  that names a role other than the one just dispatched is rejected as
  `frontmatter_producer_mismatch` unless the handoff is a dispatcher-written
  escalation. Decided 2026-10-21: generalize
  `_tpm_dispatch_producer_errors()` in `validate_handoff_text()` to every
  role when `not dispatcher_authored`. Chat-takeover fix (planner,
  2026-10-21, after the backend handback at `1b1ef6d`): `validate_handoff()`
  and `validate_handoff_text()` take a keyword-only `chat_takeover` flag,
  passed only by the `_follow_chat_takeover` call. With it the producer
  check accepts `planner` or `user`, and any other producer still fails
  `frontmatter_producer_mismatch`; nothing else is waived and
  `dispatcher_authored` stays false. Both chat prompt branches tell the chat
  to set `producer: user` when it rewrites HANDOFF.md. PR #257 review round
  1: in Linear mode a `producer: user` takeover that assigns an implementer
  still needs `selection_basis` (Finding 2, fixed); validating the successful
  finalizer handoff in `run_loop` is deferred to the planner (Finding 1,
  outside this packet's single orchestrator edit).
- TRANS-5 (was TRANS-3): One table, two readers. Write the
  producer-to-route-and-status table once in `docs/handoff/HANDBOOK.md` under
  `## Transition Table`, and add a test that parses that table and asserts it
  matches the validator's rule set, so a prompt edit and a validator edit
  cannot drift apart. The `handoff-validator` helper files under
  `.agents/agents/` and the `.codex/skills/llm-handoff-validator` skill (root
  and `examples/reference-workflow/` copies) cite the table instead of
  restating it.
- TRANS-6: Operator edit of the Claude validator helpers (routes to `user`).
  The planner writes the exact edit for `.claude/agents/handoff-validator.md`
  and its `examples/reference-workflow/` copy so they cite the TRANS-5 table,
  as GEM-5c did. If TIDY-7 is still open, list its edits in the same handoff
  (operator batching, 2026-10-20).

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
  deferred `Commands run` plus a PASS section validates.
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
  Tests in `tests/ported/test_validator.py`.
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
  batching, 2026-10-20).

### 19. TIDY - small fixes from epic-close audits

Fit: TIDY-7 is approved at `4a8f9fc` (PR #216, batched with TPM-9). TIDY-8,
TIDY-12, and TIDY-13 remain, all unblocked. TIDY-8: the operator wants both
Linear evidence events (`epic_closed` and `handoff_repaired`), so it is a
normal backend story. TIDY-12 and TIDY-13 are one-file wording nits the
operator filed on 2026-10-21 after switching the self-host planner to agy and
adding a codex tpm; each fills a wait. TIDY closes after all three. Nothing
here blocks another epic.

The Open Followups that were still open on 2026-10-19, promoted to stories.
Line numbers are from `105ea42`; re-check them when writing the Work Packet.

- TIDY-1: Operator-prompt and doctor message fixes. (a) The operator-prompt
  log line `scheduling a {role} dispatch` reads "a auditor" for the auditor
  (`llm_handoff/orchestrator.py` ~line 5317); use a form that needs no
  article and update the test that asserts it. (b) An `operator_role_dispatch`
  cycle (~line 710) skips the stale-route check, so targeting the same role
  again on an unchanged handoff SHA gives no signal; log one WARN naming the
  role and the unchanged SHA (no new counter or route change). (c) A scanner
  command that starts with a placeholder gets the generic "does not start
  with a plain program name" WARN (`llm_handoff/doctor.py` ~line 1130); name
  the placeholder case in that message. Files: `llm_handoff/orchestrator.py`,
  `llm_handoff/doctor.py`, `tests/`, `CHANGELOG.md`. Closes PAUSE-RECOVERY
  (a) and (b) and SCANNERS (b). Decided by the planner on 2026-10-20: both
  scheduling lines read "scheduling the <role> dispatch"; the repeat-target
  WARN fires only for an operator_role cycle whose route and handoff SHA
  match the previous cycle; the placeholder WARN names the leading
  placeholder token. Approved at `33a8600`, merged in PR #186.
- TIDY-2: Scanner lookups ignore the current directory on Windows. On
  Windows, `shutil.which` can also search the current directory, so doctor
  (`llm_handoff/doctor.py` ~line 1136) and the work-claim gate
  (`llm_handoff/work_claim.py` ~line 1394) can accept a scanner that is in the
  repository but not on `PATH`. Passing `path=` does not fix it: on Python
  3.11 (the `requires-python` floor) `shutil.which` prepends the current
  directory on Windows even with an explicit `path`, and later versions
  decide it per process. Add one shared helper that both call instead of
  `shutil.which` for scanner executables. It walks the `PATH` entries itself
  (`os.environ["PATH"]`, or the gate's allowlisted environment's `PATH` when
  the gate builds one), skips empty and relative entries such as `.`, tries
  each `PATHEXT` extension on Windows, and accepts a regular file that
  `os.access(..., os.X_OK)` allows. An absolute executable path is checked
  directly. Doctor and the gate then cannot disagree. Tests plant a fake
  executable (a `.bat` on Windows) in a temp working directory that is not on
  `PATH`, run from that directory, and expect both to report it missing; a
  second test puts the directory on `PATH` and expects both to find it.
  Files: `llm_handoff/doctor.py`, `llm_handoff/work_claim.py`, `tests/`,
  `CHANGELOG.md`. Closes SCANNERS (c). Approved at 6515634, merged in PR #185.
- TIDY-3: Docs and text cleanup. (a) Rewrap `CHANGELOG.md` lines over 100
  characters (one at ~line 445 on `105ea42`). (b) `CONFIGURATION.md` scanner
  section: add one line recommending pinned scanner versions. (c)
  `docs/TESTING.md` Preflight (~line 201) expects one `tracker connector` line
  per CLI, but doctor groups Claude launches as `claude strict` and `claude`
  (`llm_handoff/doctor.py` ~lines 1429-1448), so two Claude lines can print;
  reword it to match `CONFIGURATION.md`. (d) `docs/TESTING.md` Scenario B
  (~line 248): say not to force the audit failure with a failing Verification
  command, since the work-claim gate stops it as `verification_failed` before
  any reviewer runs. (e) `docs/TESTING.md` Current State (~line 16): drop the
  hard-coded suite counts. (f) Replace the non-ASCII literals in
  `tests/test_doctor.py` (~lines 3006, 3027, 3116, including an invisible
  U+200E) with `\u` escapes. Files: `CHANGELOG.md`, `CONFIGURATION.md`,
  `docs/TESTING.md`, `tests/test_doctor.py`. Closes SCANNERS (a) and (d),
  LINEAR-WORKFLOW (a) to (c), and LINEAR-FOUNDATIONS (a). Decided by the
  planner on 2026-10-20: (d) was already done at `1bde084`, so it needs no
  edit; (c) also corrects the `CONFIGURATION.md` sentence and drops the
  Antigravity connector text; a new `tests/test_text_hygiene.py` keeps (a)
  and (f) fixed. Approved at `bf27c64`, merged in PR #187.
- TIDY-4: Tracker probe and stop-request hardening. (a) The
  `doctor --probe-tracker` write denylist (`_TRACKER_PROBE_WRITE_TOOLS`,
  `llm_handoff/doctor.py` ~line 1469) names only five tools; deny every
  tracker-server tool except the two read tools in
  `_TRACKER_PROBE_READ_TOOLS`, for example by allowing only those two by name
  for the tracker server. If the CLI cannot express that, keep the denylist,
  document the residual risk in `CONFIGURATION.md`, and recommend
  `claude_mcp_config`. (b) `stop_requested` (`llm_handoff/stop_request.py`
  ~line 136) honors a stop request with no `target_pid`; document that in
  `CONFIGURATION.md` as intended (a manual stop file stops any running
  dispatcher in the repository) or require the pid, and say which in the
  handback. Files: `llm_handoff/doctor.py`, `llm_handoff/stop_request.py`,
  `tests/`, `CONFIGURATION.md`, `CHANGELOG.md`. Closes LINEAR-FOUNDATIONS (c)
  and (e). Decided by the planner on 2026-10-20: (a) takes the fallback,
  keeping the explicit `--allowedTools` and the write denylist, because
  Claude Code applies deny rules before allow rules, so a server-wide deny
  would also block the two read tools; a code comment, a test, and a
  corrected residual-risk note in `CONFIGURATION.md` record this. (b) keeps
  the pidless stop request as intended, because only one dispatcher can hold
  `.dispatch.lock` in a repository; only the `stop_requested` docstring and
  `CONFIGURATION.md` change. Approved at `ec81ba8`, merged in PR #188.
- TIDY-5: One refusal summary per dispatch. The Claude provider logs a WARN
  per refused command (`refused by the permission rules`,
  `llm_handoff/agent_providers/claude.py` ~line 208) and no total. Count
  refusals per dispatch in `_ClaudeStreamJsonMonitor` and print one summary
  line next to the `Claude result` line (~line 170), naming the count and the
  distinct refused command prefixes (no raw arguments, per GEM-0h). Give the
  agy provider (`llm_handoff/agent_providers/antigravity.py`, deny-rule
  handling ~lines 290 and 539) the same summary line. Keep the per-command
  WARN lines. Files: both provider modules, `tests/`, `CHANGELOG.md`. Closes
  the 2026-10-18 pause review followup. Decided by the planner on
  2026-10-20: one WARN summary per process run, logged from each monitor's
  `finalize` and never appended to stderr; Claude prefixes are the program
  basename (plus the `git` subcommand) or the tool name, with `<command>`
  for anything outside a safe character set; `agy` names the matched deny
  rules, since it keeps no command text (GEM-0h). Approved at `80682fd`,
  merged in PR #190.
- TIDY-6: Non-Claude helper files defer to the configured project state
  path. The planner, backend, frontend, auditor, validator, router, and
  ledger-updater helper files under `.agents/agents/` and the role skills
  under `.codex/skills/` (root and `examples/reference-workflow/` copies)
  name `PROJECT_STATE.md` literally. Give them the deferral wording CLEAN-4
  gave the finalizer helpers (for example line 19 of
  `.codex/skills/llm-handoff-finalizer/SKILL.md`: "The project state file
  named in the dispatcher prompt (`PROJECT_STATE.md` by default)"). These are
  protected paths, so list each file under Files in scope. Verification:
  `tests/test_reference_workflow.py` and the full suite. Closes
  CONFIG-CLEANUP (a) outside `.claude/`. Decided by the planner on
  2026-10-20: the wording is 'The project state file (`PROJECT_STATE.md`
  unless the dispatcher prompt or `AGENTS.md` names another path)', because
  only the finalizer's dispatcher prompt names the configured path; the
  planner story-recording paragraph keeps the literal name because
  `tests/test_story_list.py` asserts it across every planner prompt tree.
  Approved at `138d448`, merged in PR #191.
- TIDY-7: Operator edit of the Claude helper files (routes to `user`).
  Claude roles cannot write `.claude/`, so the planner writes the exact edits
  into the handoff and routes to `user`, as GEM-5c and LIN-9d did: (a) the
  same project state deferral wording as TIDY-6 in `.claude/agents/*.md` and
  `examples/reference-workflow/.claude/agents/*.md`. Part (b) moved to
  TIDY-11 on 2026-10-20: the GEM-3b render is `_LINEAR_AGENT_TOOLS` in
  `llm_handoff/init_workflow.py`, not a `.claude/` edit. Take it after TIDY-6
  so both trees use the same wording. Closes CONFIG-CLEANUP (a) inside
  `.claude/` only. Batched (operator, 2026-10-20): ride along with the next
  `.claude/` operator story's `user` route, as the Fit line says. Riding
  with TPM-9 (planner, 2026-10-20). Approved at `4a8f9fc`, merged in PR #216.
- TIDY-8: Linear evidence events for epic closes and handoff repairs
  (operator confirmed both events on 2026-10-20). (a) In Linear mode the finalizer posts
  nothing, so an epic close leaves no evidence comment; add an `epic_closed`
  event to `LINEAR_EVIDENCE_EVENTS` and the evidence comment template, posted
  on the last story's issue. (b) A validator or planner repair of a leased
  handoff posts nothing; add a `handoff_repaired` event so the
  `LINEAR-EVIDENCE` check accepts a repaired lease whose producer changed.
  Vanilla mode is unchanged. Closes source-dispatcher comparison (a) and (b).
- TIDY-9: Merge-aware audit range. Approved at `935b9c8`, merged in PR #184.
  When a story branch has merged its base
  branch after the saved story base, the audit fan-out range (base from
  `_story_start_sha` / `publish.saved_story_base`) includes the merged
  upstream files, and the quality reviewer fails phase 1 on scope
  (RECOVERY-2a cycles 7 and 9). Use
  `git merge-base HEAD <remote>/<base branch>` as the review base when it
  descends from the saved base. Files: `llm_handoff/orchestrator.py`,
  `llm_handoff/publish.py`, `tests/`, `CHANGELOG.md`. Decided by the planner
  on 2026-10-20: a new `publish.merge_aware_review_base` returns the full
  merge-base of the scope commit (or `HEAD`) and
  `refs/remotes/<remote>/<base branch>` only when the saved story state
  belongs to the checked-out story branch, the merge-base differs from the
  saved base and descends from it, and it is not the scope commit itself;
  otherwise, or on any git failure, it returns the saved base. The
  orchestrator applies it only to the `AuditFanOutContext` base on an
  auditor route outside `local` publish mode and logs one INFO line when the
  base moves; `gate_story_base`, `_story_start_sha`, `saved_story_base`, and
  the work-claim gate are unchanged, and `audit_review.py` is unchanged.
- TIDY-10: Dispatcher verification for planner-issued audits. Approved at
  `2df9673`, merged in PR #183.
  `audit_verification.pre_audit_trigger` fires only for an epic close or an
  empty review range, so a story audit the planner re-issues reaches a
  sandboxed reviewer with no `## Dispatcher Verification` PASS (RECOVERY-2a
  cycle 12). Also trigger it when the handoff's producer is not an
  implementer and has no PASS for the audit commit. Files:
  `llm_handoff/audit_verification.py`, `llm_handoff/orchestrator.py`,
  `tests/`, `CHANGELOG.md`. Decided by the planner on 2026-10-20: a third
  trigger in `pre_audit_trigger` for any non-implementer producer (`audit
  issued by <producer>`); the call site in `audit_review.py` is unchanged and
  `orchestrator.py` needs no edit.
- TIDY-11: Linear init renders Linear MCP tools into the Claude backend,
  auditor, and finalizer subagents. Add `.claude/agents/backend.md`
  (backend), `auditor.md` (auditor), and `ledger-updater.md` (finalizer) to
  `_LINEAR_AGENT_TOOLS` in `llm_handoff/init_workflow.py`, so
  `init --tracker linear` appends their role's tools as GEM-3b does for
  planner and frontend. Files: `llm_handoff/init_workflow.py`,
  `tests/test_init_tracker.py`, `CHANGELOG.md`. Closes the GEM-3b audit
  followup (was TIDY-7 (b)). Approved at `f81ed5b`, merged in PR #192.
- TIDY-12: Doctor's agy interpreter-grant line says what it found. For an
  `antigravity` role with `interpreter_grants: true`, `doctor` warns
  "<role> (provider antigravity) needs the agy allow entries
  `command(regex:^python( .*)?$)`, ..." (`llm_handoff/doctor.py` ~line 902)
  even when `~/.gemini/antigravity-cli/settings.json` already holds those
  entries, which read as a missing grant to the operator on 2026-10-21. When
  every entry is present in the agy user settings, word it like the Claude
  line ("uses the agy allow entries ..."), keeping the bypass-risk and
  user-wide text; when one is missing, say which are missing and point to the
  `antigravity permissions` check. Severity stays WARN. Files:
  `llm_handoff/doctor.py`, `tests/test_doctor.py`, `CHANGELOG.md`.
- TIDY-13: The example config's tpm comment matches TPM-10. The commented
  `tpm:` block in `dispatch_config.example.yaml` (~line 180) still says "The
  dispatcher does not route to it yet: a handoff with next_agent: tpm still
  pauses", which TPM-10 made wrong. Say instead that, when configured, the
  auditor's story-level passes and the planner's epic opens route to the tpm,
  the planner keeps epic choice, pause recovery, and repairs, and without it
  the planner picks every story. Check `CONFIGURATION.md` and the reference
  workflow's `dispatch_config.yaml` for the same stale sentence. Protected
  path; list it in Files in scope. Files: `dispatch_config.example.yaml`,
  `CHANGELOG.md`.

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

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file and holds current work only.
  On each epic close the finalizer moves the closed epic and its status notes
  to `COMPLETED_WORK.md`, appends its ledger line there, and advances Active
  Epic here.
- Git history is the durable record of completed work.
