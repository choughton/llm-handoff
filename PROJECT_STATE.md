# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

It holds current work only. Closed epics, their ledger lines, and older
status notes are in `COMPLETED_WORK.md`; read that file only when you need
history.

## Current Status

- **Active Epic:** LOOSE-ENDS (epic 22). LOOSE-1 (config resume settings
  work without CLI flags) is approved at `0585fb4`
  (`0585fb4d19a04354a52601470672567ccf8fb21b`) and merged in PR #284
  (`9a94f35`, `9a94f3599cda149aec769c69b16789a925902b11`). LOOSE-2
  (dispatcher log lines show on the console while the chat line is up) is
  implemented and awaits audit; LOOSE-3 and LOOSE-4 remain, in that order. TIDY (epic 19) closed with TIDY-13,
  approved at `7816ae6` and merged in PR #272 (`4e380f9`).
- **Parked Epic:** ACTIVE-PLANNER (epic 18). ACTIVE-1 (gate-verified
  handback) is approved at `9197913`. ACTIVE-2 (the same contract in the
  non-Claude helper files) is approved at `9486d3c`. ACTIVE-3 (verification
  preflight in `doctor`) is approved at `2de1b44`. ACTIVE-4 (verification
  preflight in the dispatcher) is approved at `d77b61f` and merged in PR #276
  (`f7625f8`). ACTIVE-5 (test scope policy) is approved at `98c3118` and
  merged in PR #280 (`0953fc3`). ACTIVE-6 (environment refusal bullet and
  validator warning) is approved at `527b1bb` and merged in PR #282
  (`383a1b2`). ACTIVE-7 (no pointless re-dispatch, planner side) is approved
  at `7b1eb86` (`7b1eb86f8f33111d1206c7dfcb66748bcd69980e`) and merged in PR
  #283 (`3702b46`, `3702b469678bf5842d7a86ff979c599ff2cab89d`). The epic is
  parked at its remaining story, ACTIVE-8: Operator edit of the Claude helper
  files. It waits on the operator applying and committing the
  `.claude/agents/` edits (mirroring ACTIVE-1 and ACTIVE-6 into the root and
  `examples/reference-workflow/` `backend.md` and `frontend.md`, ACTIVE-5
  and ACTIVE-7 into both `planner.md` copies, and checking `auditor.md`),
  once the loop is ready to pause for the operator and writes the
  `logs/dispatch/operator/ACTIVE-8/` artifact.
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
  the existing validator and gate behavior is retained. Progress: approved
  2026-10-22 at `9197913ef76de7aeb9b2143d393442b6fbfbb21e`.
- ACTIVE-2 (was part of ACTIVE-1): Gate-verified handback in the non-Claude
  helper files: the `llm-handoff` and `llm-handoff-frontend` Codex skills
  and `backend.md` and `frontend.md` under `.agents/agents/` (root and
  `examples/reference-workflow/` copies) describe the ACTIVE-1 wording.
  Protected paths; list each in Files in scope. Progress: approved
  2026-10-22 at `9486d3c31b63fde1839feee6dde0543ff50b5bde`; the Codex
  skills' temporary-directory paragraph now follows the conditional
  gate-verified rule.
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
  Progress: approved 2026-10-22 at
  `2de1b447b30e3b283b67d316548a8e776724e26f` as
  `doctor.check_verification_preflight`.
- ACTIVE-4 (was part of ACTIVE-2): Verification preflight in the
  dispatcher. Call the ACTIVE-3 check at startup (`run_loop`,
  `llm_handoff/orchestrator.py` ~line 456, next to the pre-launch checks
  there) and in every cycle that dispatches an implementer, next to
  `capture_pre_dispatch_state` (~line 1170), because a Work Packet the
  planner writes mid-run never passes startup. It logs the same WARN and
  never blocks, so the planner can re-scope. Startup and per-cycle tests in
  `tests/ported/test_orchestrator.py` with `--dry-run`. Progress:
  implemented 2026-10-22, awaiting audit; startup checks the handoff on disk,
  each backend or frontend cycle (dry-run preview included) checks the
  assignment it dispatches, and the first cycle skips content startup
  already checked. PR #276 review round 1: the cycle check now covers the
  role the cycle dispatches (operator `@backend`/`@frontend` overrides
  included), the startup skip applies only to the same role and content,
  and manual frontend runs are not checked. Approved 2026-10-22 at
  `d77b61f36fbc03295bb7601878e06629c4a367d1` and merged in PR #276
  (`f7625f838f14d320c530f9ac65bd87bfeac5ee02`).
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
  phrase. Grooming (2026-10-22): the rule is mirrored into the four
  `examples/reference-workflow/` copies (handbook, planner prompt, Codex
  planner skill, `.agents/agents/planner.md`); the existing parser test
  (`test_parse_work_packet_extracts_scope_and_command`, already `tests/`) and
  dispatcher-commit test for new files under a directory entry are reused,
  and one `verify_work_claim` case adds a new test under `tests/`. The
  pattern stays advisory with existing status gating; no assignment-time
  detector activation. Progress: implemented 2026-10-22; the handbook
  carries a Tests In Files In Scope section. Approved 2026-10-22 at
  `98c311812f777ad558706381316ebe84122f38b6` and merged in PR #280
  (`0953fc32804a6b4b1adc1d27e53f7c91b5a0ece4`).
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
  supplies it. Progress: implemented 2026-10-22; the
  validator takes the handoff the planner was dispatched on as
  `dispatched_handoff_content`, separate from the story-list baseline, and
  the orchestrator passes it on post-dispatch validation and producer repair.
  Approved 2026-10-22 at `527b1bbf272c8dedc57a1d405df9527f18ae741d` and
  merged in PR #282 (`383a1b23eb43a59dfc9e95cb664ed7f1e345ef77`).
- ACTIVE-7 (was part of ACTIVE-4): No pointless re-dispatch, planner side.
  When the planner receives `blocked_implementation_failure` whose handback
  carries the ACTIVE-6 bullet, it must not re-dispatch the same role with an
  unchanged Work Packet: it either applies ACTIVE-1 (re-dispatch with an
  instruction to hand back gate-verified) or routes to `user` with the exact
  config line to add, such as `interpreter_grants: true`. Encode this in the
  planner prompt's handback review, the handbook's Escalation Protocol
  (`docs/handoff/HANDBOOK.md` ~lines 125-142), and the ACTIVE-5 planner
  helper files outside `.claude/`. Grooming (2026-10-22): the rule is
  mirrored into the four `examples/reference-workflow/` copies (handbook,
  planner prompt, Codex planner skill, `.agents/agents/planner.md`), and
  verification focuses on both decision paths: the gate-verified path
  revises the Work Packet's Context or Objective with the ACTIVE-1 handback
  instruction and keeps the Verification command, and the `user` path names
  a remedy matching the refusal category. The ACTIVE-6 warning stays
  advisory and is reused. Progress: implemented 2026-10-22;
  the handbook's Escalation Protocol carries the shared rule, each planner
  file a Handback Review section, `tests/ported/test_handoff_docs.py` checks
  all eight files and both paths, and `tests/ported/test_validator.py` shows
  the revised Work Packet validates without `planner_redispatch_after_refusal`
  while the unchanged one still warns. Approved 2026-10-22 at
  `7b1eb86f8f33111d1206c7dfcb66748bcd69980e` and merged in PR #283
  (`3702b469678bf5842d7a86ff979c599ff2cab89d`).
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

### 22. LOOSE-ENDS - security followups and loop papercuts

Fit: unblocked, independent, and needs no operator input. A good epic to
work while ACTIVE-PLANNER is parked at ACTIVE-8 (its operator story), and
after ACTIVE-PLANNER closes. Operator direction (2026-10-22): queue work the
loop can finish overnight without a person at the console.

Two medium security findings from epic-close audits, promoted from Open
Followups, and two operator-reported papercuts from the 2026-10-21/22 runs.
Each story is one behavior change with its own tests.

- LOOSE-1: Config resume settings work without CLI flags. In
  `llm_handoff/__main__.py` the `--use-backend-resume/--no-backend-resume`
  and `--use-planner-resume/--no-planner-resume` options default to `True`
  and `_run_dispatch` always passes them to `load_dispatch_config`
  (`llm_handoff/config.py`), which overwrites the config file's
  `backend_resume` and `planner_resume`. So `planner_resume: false` in the
  file still prints `Planner session: MANAGED RESUME` unless the operator
  also passes `--no-planner-resume` (seen live 2026-10-21). Make both options
  tri-state (default `None`) so the file wins unless a flag is passed, keep
  the flag names, and say in CONFIGURATION.md and the CLI help which one
  applies. Decided (planner, 2026-10-22): for both `backend` and `planner`
  the precedence is the CLI flag (or a non-None `load_dispatch_config`
  caller override; source `--use-<role>-resume` / `--no-<role>-resume`) >
  an explicit top-level `<role>_resume` (source `<role>_resume`) > an
  explicit `agents.<role>.resume` read before provider defaults are merged,
  or in the `model_fields_set` of a caller-supplied `AgentConfig` (source
  `agents.<role>.resume`) > `True` (source `default`). Provider defaults
  never count as explicit, and `AgentConfig.resume` is not rewritten from
  the top-level setting. `DispatchConfig` stores `backend_resume_source`
  and `planner_resume_source`, and both startup banner session lines end
  `source: <source>`. Tests: file false and no flag gives stateless; file
  false and `--use-planner-resume` resumes; no key and no flag keeps the
  current default. Grooming (2026-10-22): scope is
  `llm_handoff/__main__.py`, `llm_handoff/config.py`,
  `llm_handoff/orchestrator.py`, `CONFIGURATION.md`, this file, and tests in
  `tests/test_cli.py`, `tests/test_config.py`, and
  `tests/ported/test_orchestrator.py`; verify both roles, both flag values,
  top-level file settings without flags, the `agents.<role>.resume`
  fallback, the top-level key winning over it, provider-default `resume`
  falling through to `True` (`default`), and the banner `source:` output.
  Progress: implemented 2026-10-23; `load_dispatch_config`
  and a `DispatchConfig` before-validator share one resolver, a config
  that sets `<role>_resume_source` fails to load, and CLI help and
  CONFIGURATION.md (Runtime Controls, Provider Fields) state the precedence.
  Approved at `0585fb4` and merged in PR #284 (`9a94f35`).
- LOOSE-2: Dispatcher log lines show on the console while the chat line is
  up. On 2026-10-22 the epic-close pre-audit verification logged `Dispatcher
  verification (epic close): running Work Packet Verification command ...`
  and `... exited 0 after 397.1s` to the dispatch log file, but the operator's
  console showed nothing between `Dispatching audit fan-out` and the
  reviewers starting about seven minutes later, so the run looked frozen.
  Find why these lines did not reach the console while the `[auditor
  running] chat>` line was drawn (the console chat line renderer and the
  dispatch logger), make every logged line print above the chat line, and
  add a test that logs while the chat line is attached. Grooming
  (2026-10-22): `llm_handoff/logging_util.py` filters steady-state `INFO`
  and `DEBUG` before console rendering, so inspect that filter along with
  `llm_handoff/chat_line.py`; redrawing the prompt alone cannot recover a
  filtered message. Progress: implemented 2026-10-23, awaiting audit;
  `DispatchLogger._should_write_console` lets every level through while a
  chat line is attached, and the steady-state filter still governs the
  direct console when none is attached, including after detachment.
- LOOSE-3: Windows `HANDOFF.md` replacement keeps the file's ACL. From the
  CHAT-26 audit (medium, security reviewer): on Windows
  `_write_handoff_durably` (`llm_handoff/orchestrator.py`) replaces
  `HANDOFF.md` with a temporary file that takes the directory's inherited
  ACL, so an explicit DACL on the old handoff is not kept. Copy the old
  file's security descriptor onto the temporary file before the replace and
  fail closed (keep the old file, log an ERROR) when it cannot be copied.
  POSIX behavior is unchanged; Windows-only tests are skipped elsewhere.
  Grooming (2026-10-22): `_write_handoff_durably` already copies POSIX
  metadata before the replace; keep its cleanup and failure path when
  adding Windows security descriptor preservation.
- LOOSE-4: Report-only validator launches deny Linear writes explicitly.
  From the TIDY-15 audit (medium, security reviewer): removing the Linear
  write tools from Claude `--allowedTools` for report-only validator
  launches does not deny writes that inherited user or project permissions
  already allow. Add explicit `--disallowedTools` entries for the configured
  Linear MCP server's `save_comment` and `save_issue` on those launches,
  covering fallback launches and keeping the existing denials. Tests cover
  the primary and fallback launch arguments. Grooming (2026-10-22): the
  Claude permission builder in `llm_handoff/agent_providers/claude.py`
  emits the shared disallowed-tools list; keep those denials and add the
  report-only `save_comment` and `save_issue` denials at primary and
  fallback validator launches.

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

- The CHAT-26 audit finding (Windows `HANDOFF.md` ACL) is LOOSE-3.

- The TIDY-15 audit finding (report-only validator Linear writes) is LOOSE-4.

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
