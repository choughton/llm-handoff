# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

## Current Status

- **Active Epic:** LINEAR-WORKFLOW (next; LIN-7 not started).
  LINEAR-FOUNDATIONS, CONFIG-CLEANUP, SCANNERS, PAUSE-RECOVERY,
  PUBLISH-RESUME-NITS, AUDIT-COST, and ANTIGRAVITY-ADAPTER closed.
- **Current Blocker:** None.
- **Active Branch:** main (story branches `llm-handoff/*`, one pull request per
  story).

## Backlog

Epics run in the order listed. The planner takes the next story of the active
epic, or the first story of the next epic when the active one closes. Each
story is one Work Packet; the planner writes its Files in scope, Verification
command, and acceptance criteria from the code. A story too large for one
Work Packet may be split. Re-order, add, or drop epics by editing this list.

Epics 6 (CONFIG-CLEANUP) and 7 (LINEAR-FOUNDATIONS) are closed; see the
Completed Scope Ledger.

### 7. LINEAR-FOUNDATIONS - optional Linear mode, groundwork

Hybrid model: agents read and write Linear through their own CLI's Linear MCP
connector. The dispatcher holds no Linear client or credential and never calls
Linear. Linear mode is off by default and requires `enforce_work_packet: true`.

- LIN-1: `tracker` config block (`provider: none | linear`, team, exactly one of
  project or label, workflow state names, `mcp_server`, optional
  `claude_mcp_config`), validated at load. Nothing reads it yet.
- LIN-2: Lease fields `tracker_issue`, `tracker_url`, and `selection_basis`:
  required for implementer and auditor routes in Linear mode (`story_id` equals
  `tracker_issue`), accepted with a warning in vanilla mode, preserved by
  handoff repair.
- LIN-3: In Linear mode, each Claude role launches with its Linear tools (read
  tools for all; comments for planner, implementers, auditor; issue updates for
  planner and auditor only). With `claude_mcp_config` set, load only that MCP
  config. The security reviewer gets no Linear tools.
- LIN-4: Package-internal Linear prompt templates: a shared `AGENTS.md`
  section, one section per role, and the YAML evidence-comment contract.
- LIN-5: `init --tracker linear` on a fresh repository writes the tracker block
  and renders the Linear sections as managed blocks.
- LIN-6: `doctor` reports each mapped CLI's Linear connector; opt-in
  `--probe-tracker <issue>` confirms read access and state names, read-only.

### 8. LINEAR-WORKFLOW - agent-side Linear workflow

- LIN-7: In Linear mode, a missing or empty `HANDOFF.md` routes one planner
  cycle to rebuild the lease from the pool; a second miss pauses. Vanilla mode
  is unchanged.
- LIN-8: Planner selection rules (priority, then manual order, then creation
  date; every pick made without a priority is called out) and a Linear issue
  template whose labels match the Work Packet parser.
- LIN-9: Auditor pass and fail transitions and user-attention steps; the
  validator checks the latest evidence comment against `HANDOFF.md`, read-only.
- LIN-10: `docs/TESTING.md` runbook for a manual acceptance run against a
  sandbox Linear team.

### 9. LINEAR-SWITCH - mode switching and onboarding

- LIN-11: `init --tracker linear` and `init --tracker none` switch an
  initialized repository between modes, only between assignments and never
  while `.dispatch.lock` exists; `--dry-run`; `--force` reports what is left
  behind; a round trip is byte-identical.
- LIN-12: After `init --tracker linear`, print connector setup steps, the
  `doctor` command, and the probe command; add `docs/LINEAR.md`.

### 10. WORK-INTAKE - turn a plain request into epics and stories

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

## Open Followups

- From the AUDIT-COST close (low, `llm_handoff/audit_review.py`):
  - (a) the start-failure ERROR line logs up to 2000 characters of raw reviewer
    stderr without redaction and with newlines intact (~line 1273); redact it or
    pass it through `_single_line`.
  - (b) when `gemini extensions list` itself fails, the error still says the
    extension is not installed (~line 1444); give the check failure its own
    message.
  - (c) when the extension check exits 0 but the extension is missing, the
    start-failure line shows exit code 0 and empty stderr (~line 1275); show
    `run.error` for preflight failures.
- From the PAUSE-RECOVERY close (low, `llm_handoff/orchestrator.py`):
  - (a) the log line "scheduling a auditor dispatch" has an article error
    (~line 3278); use a neutral form and update the test that asserts it.
  - (b) no WARN or counter when the same role is targeted again on an unchanged
    handoff SHA, since `operator_role` cycles skip the stale-route check
    (~line 679).
  - (c) the pause reason and operator text are not delimited in
    `OPERATOR_ROLE_INSTRUCTION_TEMPLATE` (~line 3268); wrap them in labelled
    delimiters.
  - (d) an operator message appended to the `@auditor` reviewer prompts can
    steer the audit (~line 1540); log the instruction with the audit result or
    mark the verdict as operator-influenced.
- From the SCANNERS close (low/info, SCAN-1):
  - (a) `CHANGELOG.md` line 19 is over-long; rewrap it.
  - (b) a scanner command that starts with a placeholder gets the generic
    "does not start with a plain program name" WARN
    (`llm_handoff/doctor.py` ~line 806); mention the placeholder case.
  - (c) on Windows, `shutil.which` may find a program in the current directory
    instead of on `PATH`, in both doctor (`llm_handoff/doctor.py` ~line 817)
    and the gate (`llm_handoff/work_claim.py` ~line 1155); pass an explicit
    `path=` in both or document it.
  - (d) `CONFIGURATION.md` (~line 674) does not recommend pinning scanner
    versions; add one line.

- From the CONFIG-CLEANUP close (CLEAN-4, low/info):
  - (a) the planner, backend, frontend, auditor, validator, and router helper
    files still name `PROJECT_STATE.md` literally; give them the same deferral
    wording if wanted.
  - (b) `project_state_path` and `handoff_path` are not validated in
    `llm_handoff/config.py` (~line 785); reject absolute paths, `..` segments,
    and control characters.

- From the LINEAR-FOUNDATIONS close (low/info, LIN-6b):
  - (a) low: non-ASCII literals in `tests/test_doctor.py` (~lines 2561, 2582,
    2671), including an invisible U+200E; replace with `\u` escapes.
  - (b) low: the probe's isolation (`llm_handoff/doctor.py` ~line 1237) has
    not been checked against a live Claude CLI; an operator should run
    `llm-handoff doctor --probe-tracker <ISSUE>` once in a repo with a project
    hook and confirm the hook does not fire.
  - (c) low: the probe write denylist names only five tools
    (`llm_handoff/doctor.py` ~line 1217); deny every tracker-server tool except
    the two read tools, or keep the documented risk and recommend
    `claude_mcp_config`.
  - (d) low: `logs/dispatch/carry-over.json` is trusted at startup
    (`llm_handoff/orchestrator.py` ~line 3171); bind it to the handoff SHA and
    lock holder, or check it against git state.
  - (e) info: a stop request without `target_pid` is honored
    (`llm_handoff/stop_request.py` ~line 146); document it or require the pid.

## Completed Scope Ledger

- **Phase 0 bootstrap (W0.1)** - pinned dispatcher took its first assignment
  to `verified_pass` against the development checkout. SHA `d74a3e0`.
- **SELFHOST-P1** - portable install, `init` merges, `doctor`, all-Claude and
  all-Codex role files, security review and work-claim gate on by default,
  restricted Verification commands. Closed at `01212e3`.
- **PUBLISH-RESUME** - a resumed publish pauses when the branch gained anything
  but merges, including edits carried inside an operator merge commit. PR3
  `e90ce96`, merged as `717b963`. Verification: full test suite.
- **ANTIGRAVITY-ADAPTER** - `agy` provider adapter (AGY-1 core adapter; AGY-2
  soft-deny and sign-in detection, PR #60 merge
  `54cc38c0dc343fb07bb0b578bfdd243fe809d254`; AGY-3 agent files and protected
  `.agents/**`, PR #61 merge `223053d5a1dac89d96c5273da674f42299b3b7de`; AGY-4
  `init` prints and `doctor` checks `permissions.allow`/`deny`, audited at
  `ba0a672c86213932a280bbe1f2b37748cb573336`, PR #62 merge
  `8d8a4c08cc84487a995086943347ff26fa9bc3de`). Audit approved with sub-threshold
  findings only (doctor output escaping, push-grant detection breadth, deny/ask
  shadowing heuristic not checked against a live `agy`).
- **AUDIT-COST** - audit cost controls. COST-1 optional `audit.security_model`
  (PR #63 merge `b1443a4`); COST-2a provider adapters return usage (PR #64
  merge `44afa59`); COST-2b `usage.json` and raw reviewer output per cycle
  (PR #65 merge `3c924b8`); COST-3 a reviewer that fails to start stops the loop
  with exit 1 after one attempt (PR #66 merge
  `2e9a99c6a7950cc227bf351edc2b8503234d8122`, audited at that SHA). Audit
  approved with low findings only (see Open Followups). Verification: full test
  suite, 1615 passed, 18 skipped.
- **PUBLISH-RESUME-NITS** - NITS-1 resume check nits: separate path list limit,
  parent-order and conflicted-file text in resume pause messages, merge driver
  docs. Audited at `e8973ed3e8d7673af0ab17dc0acad928272cd72e`, PR #68 merge
  `cdb607a`. Audit approved with sub-threshold findings only (conflicted-files
  listing truncation untested; pause-message path escaping). Verification: full
  test suite.
- **PAUSE-RECOVERY** - bounded planner recovery at pauses, then an operator
  prompt. PAUSE-1 `pause_recovery` config and one planner recovery per pause
  streak (PR #69 merge `62c5f72`); PAUSE-2a interactive operator prompt at
  pauses (PR #70 merge `cde6212`); PAUSE-2b operator prompt role targeting
  `@backend` / `@frontend` / `@auditor` (PR #71 merge
  `b8511aafc6564dd3aa8f5bcaf89ea8520510b535`, audited at that SHA). Audit
  approved with sub-threshold findings only (see Open Followups). Verification:
  full test suite, 1718 passed, 18 skipped.
- **SCANNERS** - make scanner setup explicit. SCAN-1 `doctor` reports each
  configured scanner (PASS, FAIL, or WARN per scanner, lookup only, never runs
  one); example configs say a scanner runs only when listed and installed, and
  the active `semgrep --config auto` entry is removed (PR #72 merge
  `d1bbd817a0e5f0c279d205befcf0a7ef511333c7`, audited at that SHA). Audit
  approved with sub-threshold findings only (see Open Followups). Verification:
  full test suite, 1726 passed, 18 skipped.
- **CONFIG-CLEANUP** - portability review gaps. CLEAN-1 `auto_push` config;
  CLEAN-2 finalizer prompt strings use `project_state_path`; CLEAN-3 generic
  supervisor default repair allowlist (`e275de4347d404baa3f256754b451800ec5ef006`,
  PR #75 merge `d5cda0437401b32b151ca9cbda84ce4a76230dee`); CLEAN-4 shipped
  finalizer helper files and the stale-finalizer recovery instruction defer to
  the configured `project_state_path` (`STALE_FINALIZER_RECOVERY_INSTRUCTION`
  became `STALE_FINALIZER_RECOVERY_INSTRUCTION_TEMPLATE` plus
  `stale_finalizer_recovery_instruction()`), audited at
  `b871bdce278510ab6a5622980d8544353513767f`, PR #77 merge
  `92fa431018de65cc2b1c2aef64cfca832fb8b3ee`. Audit approved with
  sub-threshold findings only (see Open Followups). Verification: full test
  suite, 1739 passed, 18 skipped.
- **LINEAR-FOUNDATIONS** - optional Linear mode, groundwork (hybrid model; no
  Linear client in the dispatcher; off by default). LIN-1 `tracker` config
  (PR #79 merge `00db136`); LIN-2a/2b lease fields (PRs #80 `d6f5e83`, #81
  `4fca24d`); LIN-3 Claude role Linear tools (PR #82 merge `0916264`); LIN-4
  prompt templates (PR #83 merge `470842f`); LIN-5 `init --tracker` (PR #84
  merge `3d0154e`); LIN-6a `doctor` connector report (PR #85 merge `e212077`);
  LIN-6b `doctor --probe-tracker` (PR #88 merge
  `cf49bf40a061c2c26c335c4e6f6ca02aa5a3b213`, audited at that SHA). Audit
  approved with sub-threshold findings only (see Open Followups). Verification:
  full test suite, 2126 passed, 20 skipped.

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file. The finalizer appends to the
  ledger and advances Active Epic on each epic close.
- Git history is the durable record of completed work.
