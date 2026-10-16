# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

## Current Status

- **Active Epic:** PAUSE-RECOVERY (next; PAUSE-1 not started).
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

### 4. PAUSE-RECOVERY - bounded planner recovery at pauses, then an operator prompt

- PAUSE-1: Config `pause_recovery: planner | off` (default `planner`). At every
  pause in `run_loop` except `next_agent: user` (unknown route, stale route,
  low confidence, unparseable finalizer output, missing route after planner,
  self-loops), schedule one planner recovery through the existing
  `PendingPlannerRecovery` path with the validator's diagnosis. One automatic
  attempt per pause streak; the streak resets only after a normal dispatch
  passes validation. Skip the stale-route check for recovery cycles. Tests that
  build `DispatchConfig` directly need `pause_recovery="off"`.
- PAUSE-2: When recovery is used up or the route is `user`, show an
  interactive prompt (TTY only; `--no-interactive-pause` disables it): Enter
  sends the planner "best judgment", text is guidance for the planner,
  `@backend` / `@frontend` / `@auditor <message>` targets a role, plus `retry`
  and `quit`. Editing `HANDOFF.md` still resumes. Read stdin on a daemon thread
  while polling the file.
- Docs call this bounded planner recovery, not self-healing.

### 5. SCANNERS - make scanner setup explicit

- SCAN-1: `doctor` checks that each configured scanner's binary is on `PATH`
  and reports which scanners will run. The example configs say plainly that a
  scanner runs only when it is listed and installed, and recommend `bandit`,
  `pip-audit`, and `gitleaks` for Python repositories (`semgrep` only with a
  pinned local ruleset).

### 6. CONFIG-CLEANUP - known gaps from the portability review

- CLEAN-1: `auto_push` is declared in config but never read. Remove it or wire
  it to the documented behavior, and update the tests and examples that set it.
- CLEAN-2: The finalizer prompt strings in `llm_handoff/ledger.py` hard-code
  `PROJECT_STATE.md`; use the configured `project_state_path`.
- CLEAN-3: The supervisor's default repair allowlist
  (`llm_handoff/supervisor.py`) assumes this repository's layout; make the
  default generic for target repositories.

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

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file. The finalizer appends to the
  ledger and advances Active Epic on each epic close.
- Git history is the durable record of completed work.
