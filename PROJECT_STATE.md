# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

## Current Status

- **Active Epic:** GEMINI-REMOVAL (next; GEM-1 not started).
  LINEAR-WORKFLOW, LINEAR-FOUNDATIONS, CONFIG-CLEANUP, SCANNERS,
  PAUSE-RECOVERY, PUBLISH-RESUME-NITS, AUDIT-COST, and ANTIGRAVITY-ADAPTER
  closed.
- **Current Blocker:** None.
- **Active Branch:** main (story branches `llm-handoff/*`, one pull request per
  story).

## Backlog

Epics run in the order listed. The planner takes the next story of the active
epic, or the first story of the next epic when the active one closes. Each
story is one Work Packet; the planner writes its Files in scope, Verification
command, and acceptance criteria from the code. A story too large for one
Work Packet may be split. Re-order, add, or drop epics by editing this list.

Epics 6 (CONFIG-CLEANUP), 7 (LINEAR-FOUNDATIONS), and 8 (LINEAR-WORKFLOW) are
closed; see the Completed Scope Ledger.

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
- LIN-9c: The validator support agent (each shipped provider's handoff-validator
  helper file) gains a Linear evidence check row, and the dispatcher's validator
  output handling acts on it, so a validation-only turn reports evidence-comment
  mismatches end to end. From the LIN-9a review (PR #91, Finding 1).
- LIN-9d: Apply the LIN-9c `LINEAR-EVIDENCE` item and row to
  `.claude/agents/handoff-validator.md` and
  `examples/reference-workflow/.claude/agents/handoff-validator.md`. Decide
  how that Claude subagent reaches the Linear read tools, since its `tools:`
  line currently lists only Read, Grep, Glob, and Bash. The story routes to
  `user` because Claude roles cannot write `.claude/` paths.
- LIN-10: `docs/TESTING.md` runbook for a manual acceptance run against a
  sandbox Linear team.

### 9. GEMINI-REMOVAL - move Google-vendor paths to agy, drop Gemini CLI

Gemini CLI stopped serving personal Google accounts on 2026-06-18 (sign-in
fails with `IneligibleTierError`). Antigravity's `agy` replaces it, and the
`antigravity` role adapter already ships (ANTIGRAVITY-ADAPTER). Gemini CLI
still backs `planner` and `frontend` in the default mapping, the
`init --provider gemini` mapping, the `gemini` security reviewer, and the
`gemini` normalizer (API and CLI paths). Replace each with `agy` first, then
remove the `gemini` provider. After removal, a config value or flag that names
`gemini` fails at load with a message that points to `antigravity`, like the
`auto_push` removal (`_drop_removed_auto_push` in `llm_handoff/config.py`).
Only the rejection code and its tests, `CHANGELOG.md`, and prose may name the
old provider. Keep the strings that belong to `agy` itself: its model ids
(`gemini-3.8-flash-low`, ...), its settings path
`~/.gemini/antigravity-cli/settings.json`, and its `GEMINI.md` rules file.

`agy` facts checked live on 2026-10-17 (agy 1.3.5, Windows, signed in):

- Without an agent tools list, print mode offers about 55 tools, including
  file writes, `run_command`, browser control, MCP calls, and subagents.
  User-wide `permissions.allow` grants (for example the AGY-4
  `command(git commit)` and `command(python)` entries) apply to every run.
- A workspace agent file `.agents/agents/<name>.md` with a `tools:` list
  limits the tools the model gets. With
  `tools: [view_file, list_dir, grep_search, find_by_name]`, requests to call
  `write_to_file` and `run_command` were refused as unavailable and no file
  was created. The stream-json `init` event still lists every tool, so it
  cannot confirm the restriction; `step_update` events name each tool call.
- `tools: [finish]` with `--output-format json --json-schema <schema>`
  returns the answer in the result's `structured_output` field
  (`{"normalized": "backend"}`, one turn, about 8 seconds), and an injected
  `run_command` request did not run. `tools: []` also removes `finish`, and
  `--json-schema` then yields no `structured_output`.

- GEM-1: `antigravity` normalizer. Add `antigravity` to the normalizer
  providers. Each call runs `agy -p` from a private temp directory that holds
  only a dispatcher-written agent file
  (`.agents/agents/llm-handoff-normalizer.md`, `tools: [finish]`,
  `commandExecutionPolicy: off`), with `--agent llm-handoff-normalizer
  --output-format json --json-schema <next_agent schema> --print-timeout
  <timeout_ms>`, the Antigravity role environment allowlist, and arguments as
  a list with no shell. The answer comes only from `structured_output`. A
  status other than `SUCCESS`, a missing or invalid `structured_output`, a
  nonzero exit, a timeout, a sign-in failure, or any tool call other than
  `finish` in the output fails closed and leaves the handoff unchanged. Check
  whether a global agent of the same name (`~/.gemini/config/agents/`) can
  shadow the workspace file, and whether the raw `next_agent` text can travel
  on stdin instead of the command line. Default model: a flash id from
  `agy models`. `doctor` passes the normalizer line when `agy` is on `PATH`.
- GEM-2: `antigravity` security reviewer. `audit.security_provider` accepts
  `antigravity`, and the automatic order becomes `claude`, `codex`,
  `antigravity`. The reviewer's agent allows only read tools (`view_file`,
  `list_dir`, `grep_search`, `find_by_name`, plus `finish` if the verdict uses
  `--json-schema`): no `run_command`, because user-wide grants would apply to
  it, and no write, browser, MCP, or subagent tools. It cannot run `git`, so
  the dispatcher passes in the reviewed diff (in the prompt or in a file
  outside the repository). The agent definition comes from the dispatcher
  (written at launch, or shipped under the protected `.agents/**`), never from
  a file an implementer can edit. A tool call outside the allowed list fails
  the review closed, in addition to the existing handoff hash check. Update
  Reviewer Security Posture in `docs/ARCHITECTURE.md`.
- GEM-3: `agy` as the default Google vendor. `DEFAULT_AGENT_PROVIDERS`,
  `dispatch_config.example.yaml`, and
  `examples/reference-workflow/dispatch_config.yaml` map `planner` and
  `frontend` to `antigravity`. `init --provider antigravity` writes an all-agy
  mapping (every role on `agy`, `audit.security_provider: antigravity`,
  `normalizer.provider: antigravity`) and is chosen automatically when `agy`
  is the only provider CLI on `PATH`; `init` provider detection looks for
  `agy` instead of `gemini`. Every role already has an `.agents/agents/` file
  (AGY-3); confirm the all-agy mapping uses them.
- GEM-4: Remove the `gemini` provider from the runtime and reject the old
  names. Delete `llm_handoff/agent_providers/gemini.py`,
  `llm_handoff/normalizer_providers/gemini.py` (API and CLI paths), and the
  `google-genai` dependency. Remove the Gemini sandbox checks in
  `orchestrator.py`, the Gemini security reviewer and its extension check in
  `audit_review.py` (closes AUDIT-COST followup (b)), the Gemini checks in
  `doctor.py`, the Gemini environment allowlist and API key rule in
  `agent_process.py`, the Gemini sign-in patterns in `provider_auth.py`, the
  `gemini` entry in `roles.py`, the Gemini re-exports in `agents.py`,
  `init --provider gemini`, and `--use-planner-api-key-env`. Each of these
  fails at load or parse with a message naming `antigravity`:
  `provider: gemini` on any role, `normalizer.provider: gemini`,
  `audit.security_provider: gemini`, `init --provider gemini`, and
  `--use-planner-api-key-env`. Suggested text: "The gemini provider was
  removed: Gemini CLI no longer serves personal Google accounts. Use
  provider: antigravity (the agy CLI)." Delete or rewrite the Gemini tests and
  add rejection tests. Split into runtime removal and rejection if it is too
  large for one Work Packet.
- GEM-5: Remove the shipped Gemini files and docs. Delete `.gemini/agents/`,
  `.gemini/policies/`, and their copies under `examples/reference-workflow/`.
  Delete both `.geminiignore` files unless `agy` reads them (check its docs).
  `init` stops writing these into target repositories and leaves existing
  copies alone. Drop `.gemini/agents/` from the supervisor repair allowlist
  (`llm_handoff/supervisor.py`). Rewrite `README.md`, `CONFIGURATION.md`,
  `INSTALL.md`, `QUICKSTART.md`, `WALKTHROUGH.md`, `SECURITY.md`, `docs/`, and
  the handoff prompts to use `antigravity`, keeping one short note that Gemini
  CLI support was removed and why. Finish with a grep of `llm_handoff/`,
  `tests/`, `examples/`, and the config files for `gemini`: it may find only
  rejection code and tests, agy model ids, the agy settings path, and
  `GEMINI.md`.

### 10. LINEAR-SWITCH - mode switching and onboarding

- LIN-11: `init --tracker linear` and `init --tracker none` switch an
  initialized repository between modes, only between assignments and never
  while `.dispatch.lock` exists; `--dry-run`; `--force` reports what is left
  behind; a round trip is byte-identical.
- LIN-12: After `init --tracker linear`, print connector setup steps, the
  `doctor` command, and the probe command; add `docs/LINEAR.md`.

### 11. RECOVERY - interrupted-run recovery

A Ctrl+C, a closed window, or a crash at any point must leave the loop able
to continue from a record it wrote, without the operator cleaning the working
tree by hand. The dispatcher resumes deterministically; it does not guess
whose edits are whose. Today a Ctrl+C on Windows also reaches the agent
(children share the console process group), and a restart treats the agent's
partial edits as operator edits that are never committed, so the story
bounces until the three-fix circuit breaker escalates.

- RECOVERY-1: Graceful Ctrl+C. Start agent processes in their own process
  group on Windows (`CREATE_NEW_PROCESS_GROUP`, like POSIX
  `start_new_session`) so a console Ctrl+C reaches only the dispatcher. The
  first Ctrl+C requests the same stop as `llm-handoff stop` and prints `Stop
  requested; finishing the current <role> run. Press Ctrl+C again to stop
  now.`; the second stops at once and takes down the agent process tree.
- RECOVERY-2: In-flight record and implementer recovery. Before each step
  that can leave partial state, write `logs/dispatch/in-flight.json` (cycle,
  role, story key, phase, HEAD, a copy of the pre-dispatch `HANDOFF.md`, and
  fingerprints of the files dirty before the dispatch); clear it when the
  cycle completes. At startup, an implementer phase found in the record means
  files that were clean when the record was written and are changed now are
  that agent's partial work: carry them over, restore a half-written
  `HANDOFF.md`, and re-dispatch the same role with a note that its previous
  run was interrupted and its partial changes are in the tree. Files that
  were already dirty in the record stay operator-owned even if they changed
  (a fingerprint shows a change but cannot separate the operator's bytes from
  the agent's): they are never carried over or staged, and the dispatcher
  pauses for the operator, naming them. Interrupted after the handback but
  before the dispatcher commit or the gates: resume at the commit.
- RECOVERY-3: The other phases. Planner and finalizer: restore an invalid
  `HANDOFF.md` and re-run. Audit fan-out: restore the handoff and re-run the
  read-only reviewers. Work-claim gate and scanners: re-run. Publish: make
  each step safe to repeat (push again, reuse the open pull request, skip a
  merge that already happened), using `story.json`. A `.git/index.lock` left
  by a killed `git` pauses with its path and the check to run; it is never
  deleted automatically.
- RECOVERY-4: No-record fallback. When there is no in-flight record (a run
  from before RECOVERY-2, or a lost record) and files are dirty, in scope for
  the story's Work Packet, and clean at the story's last commit, ask `Treat
  these as <role>'s interrupted <story> work? [y/N]`; yes carries them over,
  no leaves them as operator edits. Never decide this without the operator.

### 12. CHAT - talk to the agents while the loop runs

The operator types at any time; a message holds the loop and is answered by
the agent it is about, from that agent's own session, read-only. Chat never
changes routing on its own: only a confirmed action re-dispatches a role.
Console only, TTY only; without a TTY the loop behaves exactly as today. No new
dependencies; Windows (`msvcrt`) and POSIX.

- CHAT-1: Console chat line. During a run, a prompt line stays at the bottom of
  the console (`[<role> running] chat> `) and is redrawn around log lines.
  Agent processes start with `stdin=DEVNULL` so they never read the operator's
  keystrokes.
- CHAT-2: A sent message holds the loop at the next safe point: after the
  current dispatch, its dispatcher commit, and its gates, and before the next
  dispatch. The graceful-stop check at the loop top is not enough, because
  publishing runs inside the auditor's cycle: add explicit hold checks right
  before `publish_story` in the cycle and before `resume_paused_publish` at
  startup, so no push or merge happens while a message is pending or held. A
  message about the running agent prints `Question queued until after the
  current <role> run completes.` While held, print `Press Enter to continue`;
  the loop never resumes on its own after a message. Queued messages are saved
  under `logs/dispatch/` and survive `llm-handoff stop`; a saved message found
  at startup holds the loop before the publish resume.
- CHAT-3: Dispatch record and read-only answers. Save each dispatch in the run
  (role, story, provider, session id, time) under `logs/dispatch/`. A question
  resumes that agent's own session and prints the answer; follow-ups stay in
  the same thread. Each answer runs behind a read-only boundary the provider
  enforces, not a prompt-level mode: Claude `--resume` with only read tools
  (Read, Grep, Glob, read-only `git`); Codex `exec resume` in the read-only
  sandbox. agy plan mode is not a boundary, so agy answers only once the
  story establishes an enforced boundary for it (for example `--conversation`
  with an agent whose `tools:` list holds only read tools, as in
  GEMINI-REMOVAL); until then it says it cannot answer and offers the
  planner. After each answer the dispatcher also checks that `HANDOFF.md` and
  the Git state are unchanged and pauses with a warning if not; this check is
  a second layer, not the boundary.
- CHAT-4: Router and action path. `@role <message>` targets that role. Otherwise
  the normalizer model classifies the message against the dispatch record
  (target, question or action, confidence) and prints its pick before anything
  runs, for example `-> asking auditor (LIN-9b audit, session <id>)`; a
  low-confidence pick shows a numbered list instead. Questions about the loop
  as a whole go to the planner. An action asks `Re-dispatch <role> with this
  guidance? [y/N]`; yes uses the existing operator role dispatch, and the
  handoff and pull request record the result as operator-influenced (closes
  PAUSE-RECOVERY followup (d)).

### 13. WORK-INTAKE - turn a plain request into epics and stories

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

- From the LINEAR-WORKFLOW close (info, LIN-10):
  - (a) `docs/TESTING.md` (~line 104) Preflight expects one tracker connector
    line per CLI, but `doctor` groups Claude launches as `claude strict` and
    `claude` (`llm_handoff/doctor.py` ~lines 1206-1212), so two Claude lines
    can print; reword it and match `CONFIGURATION.md` (~lines 1192-1194).
  - (b) planner guidance: do not suggest a failing Verification command as the
    forced audit failure; the work-claim gate stops it as `verification_failed`
    before any reviewer runs (`docs/TESTING.md` Scenario B).
  - (c) `docs/TESTING.md` (~line 16) Current State full-suite counts are stale
    (1344 passed, 18 skipped; actual 2275 passed, 20 skipped); update them or
    drop the hard-coded numbers.

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
- **LINEAR-WORKFLOW** - agent-side Linear workflow. LIN-7 planner lease rebuild
  (PR #89 merge `5a7493aed9812fed177d8696ef51d56a8f9e06b9`); LIN-8 planner
  selection rules and issue template (PR #90 merge
  `717c4e30e7151a16568679d357e2d3282037a012`); LIN-9a (PR #91 merge
  `4b298e0b9f909aed1b21362170f9a78202f45a28`); LIN-9b (PR #92 merge
  `f9373a4243f21c78bcbcd485a3598e2f418b678b`); LIN-9c validator Linear
  evidence check (PR #94 merge `f5f2b9737b3e6a1f52133729b12ecd0429a74393`);
  LIN-9d Claude validator helper evidence row (PR #105 merge
  `59e2533a568d06cff84e9798ff0da8da87b96002`); LIN-9e post-verdict Linear
  evidence dispatch (PR #106 merge
  `d012a8e6830e86f363d829c09cd81787e92c649c`); LIN-10 acceptance runbook in
  `docs/TESTING.md` (PR #107 merge
  `f4dc618e2bd52b417460ba6135708c1ff0ebaa84`, audited at that SHA). Audit
  approved with info findings only (see Open Followups). Verification: full
  test suite, 2275 passed, 20 skipped.

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file. The finalizer appends to the
  ledger and advances Active Epic on each epic close.
- Git history is the durable record of completed work.
