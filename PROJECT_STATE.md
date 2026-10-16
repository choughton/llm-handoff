# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

## Current Status

- **Active Epic:** ANTIGRAVITY-ADAPTER (AGY-1 in progress).
- **Current Blocker:** None.
- **Active Branch:** main (story branches `llm-handoff/*`, one pull request per
  story).

## Backlog

Epics run in the order listed. The planner takes the next story of the active
epic, or the first story of the next epic when the active one closes. Each
story is one Work Packet; the planner writes its Files in scope, Verification
command, and acceptance criteria from the code. A story too large for one
Work Packet may be split. Re-order, add, or drop epics by editing this list.

### 1. ANTIGRAVITY-ADAPTER - provider adapter for the Antigravity `agy` CLI

- AGY-1: Adapter core: `provider: antigravity`, binary `agy`, config
  registration, launch arguments, environment handling, stream output parsing,
  tests with a fake binary. (In progress.)
- AGY-2: Detect `agy` soft-denies (a refused shell command that still exits 0)
  and sign-in failures in `llm_handoff/provider_auth.py`; report a soft-deny as
  a failure that names the `permissions.allow` entry to add.
- AGY-3: Antigravity agent files under `.agents/agents/` (repository and
  `examples/reference-workflow/`), and `.agents/**` in
  `DEFAULT_PROTECTED_PATHS`.
- AGY-4: `init` prints, and `doctor` checks, the `permissions.allow` and
  `permissions.deny` entries each Antigravity-mapped role needs. Never edit the
  user's settings file.

### 2. AUDIT-COST - audit cost controls

- COST-1: Optional `audit.security_model`. When set, the security reviewer uses
  it (`--model`); when unset, keep borrowing the first matching role's model and
  log once at startup which model the security reviewer uses.
- COST-2: Save each reviewer's raw output (`quality.output.json`,
  `security.output.json`) and a `usage.json` with per-reviewer tokens and cost
  in the cycle's `logs/dispatch/audit/` folder.
- COST-3: A reviewer that fails to start (argument error, missing binary,
  sign-in failure) stops the loop with exit 1 after one attempt, logging its
  stderr, instead of a failed dispatch that re-runs the whole audit. Still fail
  closed: a missing verdict is never a pass.

### 3. PUBLISH-RESUME-NITS - sub-threshold findings from the PUBLISH-RESUME audit

- NITS-1: One story for all five:
  - give the resume path list its own limit instead of reusing
    `_RESUME_LISTED_COMMITS` (`llm_handoff/publish.py`);
  - say that parent order matters in the swapped-parent pause message;
  - document that custom merge drivers (for example `union`) can make
    `merge-tree` disagree with the operator's merge and pause;
  - list which conflicted files changed in the resume pause message;
  - document that `merge-tree` runs configured custom merge drivers during the
    resume check.

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

- None outside the backlog.

## Completed Scope Ledger

- **Phase 0 bootstrap (W0.1)** - pinned dispatcher took its first assignment
  to `verified_pass` against the development checkout. SHA `d74a3e0`.
- **SELFHOST-P1** - portable install, `init` merges, `doctor`, all-Claude and
  all-Codex role files, security review and work-claim gate on by default,
  restricted Verification commands. Closed at `01212e3`.
- **PUBLISH-RESUME** - a resumed publish pauses when the branch gained anything
  but merges, including edits carried inside an operator merge commit. PR3
  `e90ce96`, merged as `717b963`. Verification: full test suite.

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file. The finalizer appends to the
  ledger and advances Active Epic on each epic close.
- Git history is the durable record of completed work.
