# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

## Current Status

- **Operator direction (2026-10-20):** CHAT-16 (read-only session answer
  launchers) is dropped unmerged, and chat is redesigned so it answers right
  away and can edit files and `HANDOFF.md` with the role's own permissions
  (see the CHAT intro). CHAT is parked and TPM-ROLE starts with TPM-1. CHAT
  resumes after TPM-ROLE with CHAT-17. The operator moves the self-host
  planner to agy, with a codex tpm, once TPM-ROLE lands, so TPM-ROLE's
  `Fit:` reason applies here too.
- **Active Epic:** TPM-ROLE, by the operator direction above. TPM-1 (role
  vocabulary) was approved at `67474dd` and merged in PR #206; TPM-2
  (launching a configured tpm) was approved at `16bbc32` and merged in PR
  #207; TPM-3 (validator rules for `producer: tpm`) is in flight. ACTIVE-PLANNER and ESCALATION-RECONCILE still
  wait on their triggers, TRANSITION-RULES comes after TPM-ROLE, WORK-INTAKE
  needs a design check, and LINEAR-SWITCH has low urgency.
- **Parked Epic:** CHAT, with CHAT-17, CHAT-18, CHAT-19, CHAT-20, CHAT-4,
  and CHAT-5 remaining. It waits for TPM-ROLE to close, not for the
  operator. CHAT-15 (the loop records each dispatch in the
  dispatch record) was merged in PR #203. CHAT-3 (dispatch record file) was approved at
  `6d387ab` and merged in PR #202. CHAT-14 (Claude and Codex role dispatches report their session id) was
  approved at `b61b313` and merged in PR #201. CHAT-2 (sent chat messages are saved
  to the queue and announced) was approved at `dd26c6a` and merged in PR
  #200. CHAT-13 (hold the loop on saved chat
  messages) was approved at `cd0df4c` and merged in PR #199; it took the hold
  half of CHAT-2 and works from the CHAT-12 queue file alone, so the loop's
  push and merge points were safe before capture writes to that file.
  CHAT-12 (chat message queue file) was approved at
  `fb04abf` and merged in PR #197. CHAT-11 (chat line and keystroke capture
  wiring) was approved at `7a6170b` and merged in PR #196. CHAT-10 (chat keystroke capture component) was approved at `9ac6a21` and
  merged in PR #195. CHAT-9 (console chat line renderer) was approved at
  `3ac5fcf` and merged in PR #194.
- **Parked Epic:** TIDY, with TIDY-7 and TIDY-8 remaining. TIDY-7 waits for
  the operator to say they are ready to make the `.claude/agents/*.md`
  deferral edits; the planner then writes the edits out and routes to
  `user`. TIDY-8 waits for the operator to confirm they want Linear
  `epic_closed` and `handoff_repaired` evidence events. TIDY-11 (Linear init
  renders Linear MCP tools into the Claude backend, auditor, and finalizer
  subagents) was approved at `f81ed5b` and merged in PR #192. TIDY-6 (non-Claude helper
  files defer to the configured project state path) was approved at
  `138d448` and merged in PR #191. TIDY-5 (one
  refusal summary per dispatch) was approved at `80682fd` and merged in PR
  #190. TIDY-4 (tracker probe and stop-request hardening) was
  approved at `ec81ba8` and merged in PR #188. TIDY-3 (docs and text cleanup) was approved
  at `bf27c64` and merged in PR #187. TIDY-1 (operator-prompt and doctor message fixes) was approved at
  `33a8600` and merged in PR #186. TIDY-2 (scanner
  lookups ignore the current directory on Windows) was approved at
  `6515634` and merged in PR #185. TIDY-9 (merge-aware audit
  range) was approved at `935b9c8` and merged in PR #184. TIDY-10
  (dispatcher verification for planner-issued audits) was approved at
  `2df9673` and merged in PR #183. RECOVERY is closed (RECOVERY-13
  approved at `ab2f9d3`, merged in PR #182, epic close `c07dcdd`; the
  per-story SHAs are in the RECOVERY section).
- **Epic Awaiting Close:** None. HARDENING closed (HARD-6 merged in PR #157,
  audited at `3949ed2`; see the Completed Scope Ledger). LINEAR-WORKFLOW,
  LINEAR-FOUNDATIONS, CONFIG-CLEANUP, SCANNERS, PAUSE-RECOVERY,
  PUBLISH-RESUME-NITS, AUDIT-COST, ANTIGRAVITY-ADAPTER, GEMINI-REMOVAL, and
  HARDENING closed.
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

Each story is one Work Packet; the planner writes its Files in scope,
Verification command, and acceptance criteria from the code. Add, drop, or
re-shape epics by editing this list.

Story size and IDs (operator rule, 2026-10-19). A story is one behavior
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

Fit: closed. GEM-0b through GEM-5c are merged (GEM-5c in PR #148, edited by
the operator because Claude roles cannot write `.claude/`); the epic close was
approved at `55e2ca3c757d4c02cc13dad42060f374caef630e`. Nothing remains.

Gemini CLI stopped serving personal Google accounts on 2026-06-18 (sign-in
fails with `IneligibleTierError`). Antigravity's `agy` replaces it, and the
`antigravity` role adapter already ships (ANTIGRAVITY-ADAPTER). Gemini CLI
still backs only the `gemini` role provider; GEM-4a and GEM-4b removed the
`gemini` normalizer, the `init --provider gemini` mapping, and the `gemini`
security reviewer. GEM-0 first makes `agy` roles work (the first
live `agy` planner run could not write files); then replace each Gemini path
with `agy`, then remove the `gemini` provider. After removal, a config value or
flag that names `gemini` fails at load with a message that points to
`antigravity`, like the `auto_push` removal (`_drop_removed_auto_push` in
`llm_handoff/config.py`). Only the rejection code and its tests,
`CHANGELOG.md`, and prose may name the old provider. Keep the strings that
belong to `agy` itself: its model ids (`gemini-3.8-flash-low`, ...), its
settings path `~/.gemini/antigravity-cli/settings.json`, and its `GEMINI.md`
rules file.

`agy` facts checked live on 2026-10-17 (Windows, signed in; `agy` updated
itself from 1.3.5 to 1.4.2 during the checks):

- Without `--agent`, print mode offers about 55 tools, including file
  writes, `run_command`, browser control, MCP calls, and subagents. With
  `--agent` and an agent file that sets no `tools:` list, the agent gets
  only read and research tools (`view_file`, `search_web`,
  `read_url_content`, `generate_image`, `send_message`, `schedule`,
  `manage_task`) and cannot write files.
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
- An agent `tools:` list with a name `agy` does not know makes it exit 3
  before the run starts (`tool "command_status" not found in registry`),
  even though the `init` event lists `command_status`.
- `permissions.allow` grants match the exact command line.
  `command(git status)` allowed `git status` but refused `git status .`,
  `git status --short`, and `python --version` (soft-denied, exit 0).
- `agy` ignores `.gemini/agents/`, `.gemini/policies/`, and `.geminiignore`
  (it does read `GEMINI.md`, kept above). An agent that exists only under
  `.gemini/agents/` fails `--agent` with `unknown agent` (the
  error lists only `.agents/agents/` agents). A file named in
  `.geminiignore` was listed and read, and a `.gemini/policies/` deny rule
  for `view_file` had no effect. The `agy` binary contains none of the
  strings `geminiignore`, `.gemini/agents`, `.gemini/policies`, or
  `.gemini/settings`; it does contain `AGENTS.md`, `GEMINI.md`,
  `.agents/rules`, and `.agents/agents`. It has no `agentignore` string and
  does contain `.gitignore` (not tested). `.geminiignore` is what kept `.env`,
  `logs/`, and `.git` out of Gemini CLI's reach; `agy` roles never had it.

- GEM-0: Make `agy` roles work. On 2026-10-17 the first live run with
  `planner` on `antigravity` could not write `HANDOFF.md`: the shipped
  `.agents/agents/planner.md` sets no `tools:` list, so `agy` gave it only
  read and research tools, and it printed the handoff instead
  (`no_new_sha`, `planner_self_loop`). Fix both gaps in the facts above.
  (1) Every shipped `agy` agent file (`.agents/agents/*.md` and the copies
  under `examples/reference-workflow/`) gets an explicit `tools:` list.
  Checked live: `[view_file, list_dir, grep_search, find_by_name,
  write_to_file, replace_file_content, multi_replace_file_content,
  run_command]` writes files and runs granted commands. Keep the allowed
  names in one list in code and test that every shipped file uses only
  those, since an unknown name stops `agy` before the run. Leave out web,
  browser, image, and subagent tools, as the Claude roles deny WebFetch and
  WebSearch; read-only helpers such as `handoff-validator` may also leave
  out the write tools. Linear mode (`tracker.provider: linear`) supports
  `agy` roles, which reach Linear through `agy`'s MCP connector, so a
  `tools:` list without MCP access would cut them off. Either give
  Linear-mode `agy` roles the MCP tool (`call_mcp_tool`, limited per role
  to the LIN-3 Linear tools where `agy` allows it, for example with
  `mcp(<server>/<tool>)` grants) and check it live, or reject `antigravity`
  roles in Linear mode at config load with a clear message; never ship a
  list that silently drops Linear access. (2) Replace the exact-match grants
  in `ANTIGRAVITY_REQUIRED_ALLOW` (`llm_handoff/antigravity_permissions.py`),
  the list `init` prints, and `CONFIGURATION.md` with grants that cover
  each command with arguments, likely `command(regex:...)` entries. The
  regex form is untested, so route to `user` for a live check before
  settling the list. `doctor` must recognize the new entries (it matches
  by exact string today), and `git push` stays denied in every form. Also
  correct the `CONFIGURATION.md` text these facts contradict (the agent
  files "set no `tools` list"; loading via `--agent` "has not yet been
  checked"). Done when an operator-run `agy` planner turn (Claude roles
  cannot run `agy`) writes a valid `HANDOFF.md` and runs
  `git log --oneline -5` and `python -m pytest --version` without a
  soft-deny. On 2026-10-18 the user accepted this risk for GEM-0c: the `agy`
  `python` and `python3` grants (`command(regex:^python( .*)?$)` and
  `command(regex:^python3( .*)?$)`), like Claude's `Bash(python:*)` and
  `Bash(python3:*)` in `CLAUDE_DEFAULT_ALLOWED_TOOLS`
  (`llm_handoff/config.py`), let a role run a subprocess `git push` past the
  deny entries, and `write_to_file` plus `python <script>` or `pytest` with a
  written `conftest.py` does the same, so narrowing the grant does not close
  it. Follow-up: GEM-0e. Parts (1) and (2) are merged (GEM-0b, PR #117;
  GEM-0c, PR #119). Only the operator-run live check in the done condition
  remains; on 2026-10-18 the user deferred it to a prerequisite of GEM-3.
- GEM-0d: Treat `agy` deny-rule tool errors as a failed dispatch. A
  deny-rule hit comes back as a `run_command` tool error inside a
  `SUCCESS` result with empty stderr, so the AGY-2 stderr check misses
  it. The adapter (`llm_handoff/agent_providers/antigravity.py`) treats
  it as a failed dispatch. Add stream-json fixtures for it. Decided by
  the planner on 2026-10-17 from the operator's live `agy` notes. Merged in
  PR #122.
- GEM-0e: Interpreter-bypass boundary for shell grants. Enforce "no push"
  outside the agent process, or keep remote-write credentials out of the
  agent environment. Make interpreter grants (`python`, `python3`, `pytest`)
  an explicit operator opt-in rather than a silent default. Have
  `llm-handoff doctor` report the bypass risk for each role that has an
  interpreter grant (Claude and `agy`). Until GEM-0e ships, the role
  allowlists are a guardrail, not a sandbox. The `doctor` reporting part is
  split out as GEM-0e1. Decided by the user on 2026-10-18: direction (c).
  The interpreter grants (`python`, `python3`, `pytest`) become an explicit
  operator opt-in: they leave `CLAUDE_DEFAULT_ALLOWED_TOOLS` and the
  generated `agy` grant set unless the operator turns them on in config. The
  planner picks the config key name and shape when it plans GEM-0e and
  records it here. Self-host keeps the grants on: the outside self-host
  config cannot be edited by agents, so the GEM-0e handback must name the
  exact opt-in setting, which the operator adds before re-pinning onto the
  GEM-0e merge. `CONFIGURATION.md` (and the README if it describes the
  allowlists) states that role allowlists are a guardrail, not a sandbox,
  since any interpreter grant can run any command, `git push` included, and
  recommends server-side branch protection (a ruleset requiring pull
  requests with no bypass actors); the dispatcher does not configure it.
  Directions (a) (keep remote-write credentials out of the agent
  environment) and (b) (a pre-push hook or refusing credential helper) are
  dropped. The GEM-0e1 doctor wording update for opt-in grants stays inside
  GEM-0e. Key decided by the planner on 2026-10-18: top-level
  `interpreter_grants: bool` on `DispatchConfig`, default `false`; `true`
  restores the previous Claude default list and `agy` allow set exactly, and
  a role's own `allowed_tools` is unaffected. Self-host opt-in: the operator
  adds the top-level line `interpreter_grants: true` to the outside self-host
  config before re-pinning onto the GEM-0e merge.
- GEM-0e1: Doctor reports interpreter-grant bypass risk per role.
  `llm-handoff doctor` adds `interpreter grants` lines per role or fallback
  launch: a WARN for each Claude launch without `unsafe_bypass` whose
  effective list (`allowed_tools`, or `CLAUDE_DEFAULT_ALLOWED_TOOLS`) has
  bare `Bash`, `Bash(*)`, or a `python`, `python3`, or `pytest` rule; a WARN
  for each `agy` launch without `unsafe_bypass` naming the required
  `command(regex:...)` allow entries for those interpreters (including ones
  from `verification_allowed_prefixes`), noting the grants are user-wide; a
  WARN for each `unsafe_bypass: true` launch (no shell allowlist at all); and
  one PASS line when none apply and a Claude or `agy` launch exists. The
  lines are WARN or PASS only, so the doctor exit code is unchanged; no
  default grant list changes. Merged in PR #123.
- GEM-0f: Matching whitespace rules for verification commands and agy
  grants. `verification_command_allowed()` (`llm_handoff/work_claim.py`)
  compares whitespace-split tokens, but `antigravity_command_grant()`
  (`llm_handoff/antigravity_permissions.py`) keeps literal single spaces.
  As a result, a command such as `git  status -s` passes the work-claim
  gate and `agy` refuses it. Pick one rule and apply it in both places:
  either the gate matches literally (it rejects runs of whitespace and
  tabs, and the current `tests/test_work_claim.py` token-split cases
  change), or the generated grants accept ` +` between tokens (this needs
  a live `agy` check, updates to `CONFIGURATION.md` and the `init`
  output, and a re-applied settings file). From the GEM-0c review
  (PR #119, Finding 1). Decided by the planner on 2026-10-18: the gate
  matches literally.
- GEM-0g: Doctor decides regex allow-entry push safety without fixed
  probes. `_regex_grant_allows_push()` (`llm_handoff/doctor.py`) tests
  only the three `_GIT_PUSH_PROBES` lines, so a push-capable allow entry
  such as `command(regex:^git push --force$)` passes when the deny
  entries are missing. Doctor stops deciding regex safety from example
  lines. One option is to pass only regex allow entries that equal a
  generated `antigravity_command_grant()` value and fail any other regex
  allow entry that names `git` with a "cannot verify" message. Add tests
  for `--force`, a non-main branch, and `git -c x=y push`. From the
  GEM-0c review (PR #119, Finding 2). Decided by the planner on
  2026-10-18: generated grants pass unless push-capable; other regex allow
  entries naming `git` fail as unverifiable; the probe check remains a
  backstop for the rest.
- GEM-0h: Refusal logs carry no raw command arguments. The `agy`
  deny-rule refusal logs in `llm_handoff/agent_providers/antigravity.py`
  keep raw command arguments after best-effort redaction, so a secret in an
  unrecognized form, for example a `-c http.extraHeader="X-Api-Key: value"`
  value, reaches stderr and the dispatch log. Recommendation: log a fixed
  refusal reason and step identifier instead, or add secret-header redaction
  with tests for escaped and concatenated quoted values. From the GEM-0d
  security review (low). Decided by the planner on 2026-10-18: log a fixed
  refusal reason, the step identifier, and the matched required deny entry
  only; remove `redact_credentials()`.
- GEM-1: `antigravity` normalizer. Add `antigravity` to the normalizer
  providers (`llm_handoff/normalizer_providers/antigravity.py`). Each call
  runs `agy` from a new private temp directory that holds the closed
  `normalizer.schema.json` and a dispatcher-written agent file
  `.agents/agents/llm-handoff-normalizer-<uuid4 hex>.md` (`tools: [finish]`,
  `commandExecutionPolicy: off`). The random name is both the file name and
  the frontmatter `name:`, so no global agent can shadow it. The agent file
  body is `normalizer_prompt(raw_value)`, which `repr`-quotes the value, so
  the raw `next_agent` text never reaches the command line or stdin. The
  command is `agy -p <fixed instruction> --agent <name> --output-format
  stream-json --json-schema <schema path> --print-timeout <N>s --model
  <model>`, as a list with no shell, in the Antigravity role environment
  allowlist; a timeout kills the process tree. `stream-json` replaces `json`
  because only stream-json shows the steps (captured live with `agy` 1.4.2
  on 2026-10-18). Only `user_input`, `agent_response`, and `finish` steps are
  allowed; any `tool` step, whatever its `tool_name`, or a missing or
  unknown step type fails closed. The answer comes only from the single
  `result` event's `structured_output`, which must hold exactly the schema
  keys; `response` (raw `finish` arguments with `toolAction` and
  `toolSummary`) is never read. A status other than `SUCCESS`, no or several
  `result` events, a non-JSON stdout line, a nonzero exit, a timeout, an
  `agy` sign-in failure, or a permission soft-deny fails closed and leaves
  the handoff unchanged. `commandExecutionPolicy: off` is accepted by `agy`
  but not shown to be honored. The pre-execution boundary is `agy`'s
  enforcement of `tools: [finish]` alone; the user-wide `permissions.allow`
  grants apply to the normalizer run, and the step-type and result checks
  run after `agy` exits, so they cannot undo a command that already ran. The
  planner accepted this residual risk on 2026-10-18 (decision A, same class
  as GEM-0c) because the provider is opt-in and adds no command the handoff
  writer lacks; `doctor` prints a `normalizer boundary` WARN line for it.
  Sign-in and soft-deny errors use fixed text and never copy `agy` stderr.
  Default model: `gemini-3.8-flash-low` (not exercised in the
  capture). `doctor` passes the normalizer line when `agy` is on `PATH` and
  fails naming `agy` when it is not. `antigravity` still cannot be the
  `audit.security_provider` (GEM-2).
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
  Reviewer Security Posture in `docs/ARCHITECTURE.md`. Design decided by the
  planner on 2026-10-18: `agy` runs with a new private temp directory
  outside the repository as its working directory; the dispatcher writes a
  random-name agent file (`llm-handoff-security-<uuid4 hex>`) there with
  `tools: [view_file, list_dir, grep_search, find_by_name, finish]`, plus a
  copy of the scope diff and the verdict schema; the model is borrowed from
  the first `antigravity` role (no `--model` otherwise); no unsafe-bypass
  args are ever added; and the step checks fail closed (any tool outside the
  list, any other step type, any non-`SUCCESS` or extra result). An operator
  live run on 2026-10-18 (`agy` 1.4.2) passed: the real GEM-2 range returned
  `pass` with no findings, and the same range plus an injected hunk asking
  for a `python -c` file write and a second file write returned `fail` with
  one `high` finding naming the injection; every tool step was `view_file`
  and neither target file exists.
- GEM-3: `agy` as the default Google vendor. The three operator live checks
  passed on 2026-10-18 with `agy` 1.4.2. GEM-0 role check: an `agy` planner
  turn exited 0 and wrote a valid `HANDOFF.md`, running
  `git log --oneline -5` and `python -m pytest --version` with no soft-deny.
  GEM-1 normalizer check: an injected `python -c` file-write request returned
  `unknown` with only `user_input`, `agent_response`, and `finish` steps, and
  no file was created. GEM-2 reviewer check: as recorded in the GEM-2 entry.
  Caveat: in the normalizer and reviewer checks the model declined the
  injected request, so they show that nothing outside the allowed tools ran,
  not that `agy` refuses a forced call; that part rests on the `tools:` list
  fact above. Split by the planner on 2026-10-18:
  - GEM-3a: `init --provider antigravity` writes an all-agy mapping (every
    role on `agy`, `audit.security_provider: antigravity`,
    `normalizer.provider: antigravity`) and is chosen automatically when
    `agy` is the only provider CLI on `PATH`; `init` provider detection looks
    for `agy` instead of `gemini`; `init --tracker linear` refuses the all-agy
    mapping before writing. Every role resolves to an existing
    `.agents/agents/` file (AGY-3). No default mapping changes.
  - GEM-3b: `DEFAULT_AGENT_PROVIDERS`, `dispatch_config.example.yaml`, and
    `examples/reference-workflow/dispatch_config.yaml` map `planner` and
    `frontend` to `antigravity`. Decided by the planner on 2026-10-18:
    `init --tracker linear` with the reference mapping writes `planner` and
    `frontend` on `claude` (`provider: claude`, `binary: claude`,
    `resume: false`, `timeout_ms: 1200000`), with a one-line comment above
    `planner` saying Linear mode does not support `antigravity` roles;
    `backend`, `auditor`, `validator`, and `finalizer` keep the template
    mapping. `init --tracker linear --provider antigravity` stays refused.
    The config-load rejection in `_validate_tracker` is unchanged, so a
    hand-written Linear config that omits `planner` or `frontend` now fails
    at load with the existing message; recorded in `CHANGELOG.md` as a
    behavior change.
  - GEM-3c: Antigravity-safe supervised repair. Supervised repair
    (`--repair-role`) with an `antigravity` role launches
    `agy --agent supervisor-repair`: `llm_handoff/supervisor.py`
    `_run_repair_role` passes subagent name `supervisor-repair`, and
    `llm_handoff/agent_providers/antigravity.py` resolves
    `agent_name or subagent_name or role`. No shipped
    `.agents/agents/supervisor-repair.md` exists, so `agy` fails with
    `unknown agent`. The fix either ships a `supervisor-repair` agy agent file
    (root and `examples/reference-workflow/` copies, with a `tools:` list from
    the GEM-0 allowed names) or makes the adapter fall back to the repair
    role's own agent file, and adds a launch regression test. The story also
    decides whether `.agents/agents/` joins the default repair allowlist. It
    must land before GEM-3b. From the GEM-3a audit, decided by the planner on
    2026-10-18. Decided by the planner on 2026-10-18: ship a dedicated
    `supervisor-repair` agy agent file (root and example copies, full
    eight-tool list) rather than an adapter fallback, and add
    `.agents/agents/` to the default repair allowlist.
- GEM-4: Remove the `gemini` provider from the runtime and reject the old
  names, so each fails at load or parse with a message naming `antigravity`.
  Split by the planner on 2026-10-18 into one Work Packet per story:
  - GEM-4a (merged in PR #138): delete `llm_handoff/normalizer_providers/gemini.py`
    (API and CLI paths), its registration in `handoff_normalizer.py` and
    `normalizer_providers/__init__.py`, `GEMINI_NORMALIZER_MODEL` and the
    `gemini` normalizer entries in `config.py`, the `gemini` normalizer
    entries in `doctor.py`, the `google-genai` dependency, and the all-Gemini
    `init` mapping (`GEMINI_MAPPING` and its blocks in `init_workflow.py`).
    Reject `normalizer.provider: gemini` with "The gemini provider was
    removed: Gemini CLI no longer serves personal Google accounts. Use
    provider: antigravity (the agy CLI)." and `init --provider gemini`
    (nonzero exit, no file written) with the same text ending "Use
    --provider antigravity (the agy CLI)." Update the example configs, the
    normalizer and init docs, and `CHANGELOG.md`; delete the Gemini
    normalizer and all-Gemini `init` tests and add both rejection tests.
  - GEM-4b (merged in PR #139): delete the Gemini security reviewer and its extension check in
    `audit_review.py` and the matching security-extension check in
    `doctor.py`, closing AUDIT-COST followups (b) and (c). Reject
    `audit.security_provider: gemini` with "The gemini provider was removed:
    Gemini CLI no longer serves personal Google accounts. Use
    security_provider: antigravity (the agy CLI)." Delete or rewrite the
    Gemini reviewer tests and add a rejection test.
  - GEM-4c (merged in PR #141): delete `llm_handoff/agent_providers/gemini.py`, the Gemini
    sandbox checks in `orchestrator.py` and `doctor.py`, the Gemini
    environment allowlist and API key rule in `agent_process.py`, the Gemini
    sign-in patterns in `provider_auth.py`,
    the Gemini re-exports in `agents.py`, the Gemini constants in
    `config.py`, and `--use-planner-api-key-env`. Reject `provider: gemini`
    on any role with "The gemini provider was removed: Gemini CLI no longer
    serves personal Google accounts. Use provider: antigravity (the agy
    CLI)." and `--use-planner-api-key-env` with "--use-planner-api-key-env
    was removed with the gemini provider: Gemini CLI no longer serves
    personal Google accounts. Use provider: antigravity (the agy CLI)."
    Delete or rewrite the Gemini role tests and add rejection tests.
    Planner decisions (2026-10-18):
    - `"gemini"` stays in `LEGACY_PROVIDER_LABELS` in `roles.py`. That entry
      is rejection code: it keeps a `next_agent` label that names `gemini`
      from routing. (This corrects the earlier plan to delete it.)
    - `planner_api_key_env: false` still loads and the key is dropped, like
      `auto_push`. Any other value fails at load with "planner_api_key_env
      was removed with the gemini provider: Gemini CLI no longer serves
      personal Google accounts. Delete the planner_api_key_env key; a Codex
      role uses use_api_key_env: true for OPENAI_API_KEY."
    - `--use-planner-api-key-env` stays only as a hidden option; passing it
      prints the message above to stderr and exits with code 2 before any
      config loads, matching the `init --provider gemini` rejection.
    - `tenacity` is removed from `pyproject.toml` and
      `requirements-dev.txt`; only the deleted adapter imported it.
    - `GEMINI_API_KEY` and `GOOGLE_API_KEY` stay in
      `VENDOR_API_KEY_ENV_NAMES`, so every launch still strips them and
      `env_passthrough` still rejects them.
- GEM-5: Remove the shipped Gemini files and docs. Split by the planner on
  2026-10-18 into GEM-5a, GEM-5b, and GEM-5c.
  - GEM-5a (merged in PR #142): Delete the shipped `.gemini/` trees and
    `.geminiignore` files and stop `init` from writing them. Delete
    `.gemini/agents/`, `.gemini/policies/planner_guardrails.toml`, and
    `.geminiignore`, at the repository root and under
    `examples/reference-workflow/`; `agy` never read them (see the facts
    above), so nothing changes for `agy` roles. Remove the `.geminiignore`
    managed-block code from `llm_handoff/init_workflow.py`, so `init` writes
    no `.gemini/` path and no `.geminiignore` and leaves existing copies
    unchanged. Drop `.gemini/agents/` from the default supervisor repair
    allowlist (`llm_handoff/supervisor.py`). Fix the stale `GEMINI_` prefix
    wording in the `build_work_claim_env` docstring (`work_claim.py`). Drop
    `.gemini/` and `.geminiignore` from the `pyproject.toml` force-include
    comment. Update the tests, `docs/INVENTORY.md`, and `CHANGELOG.md` to
    match. Planner decisions (2026-10-18):
    - `.gemini/**` stays in `DEFAULT_PROTECTED_PATHS`
      (`llm_handoff/config.py`) and in the `protected_paths` list of
      `dispatch_config.example.yaml`. `init` leaves existing `.gemini/`
      copies in target repositories alone, and those files are still
      instruction files a role could be tricked into editing. GEM-5b's final
      grep allows this entry.
    - `AgentConfig.mention` and `AgentConfig.retries` stay as fields.
      `AgentConfig` uses `extra="forbid"`, so deleting them would break any
      config that sets them. GEM-5b documents them in `CONFIGURATION.md` as
      accepted and ignored. GEM-5a does not touch them.
    - The agy-owned strings stay: `~/.gemini/antigravity-cli/settings.json`,
      agy model ids such as `gemini-3.8-flash-low`, `GEMINI.md`, the
      `GEMINI_API_KEY` entry in `VENDOR_API_KEY_ENV_NAMES`, and every
      `gemini` rejection message and its tests.
    - Checking whether `agy` honors `.gitignore` needs a live `agy` run by
      the operator. It is not part of GEM-5a. GEM-5b writes the conservative
      `SECURITY.md` text (treat `.env`, `logs/`, and `.git` as readable by
      `agy` roles until a live check shows otherwise).
  - GEM-5b: Split by the planner on 2026-10-18 because GEM-5b covered about
    130 mentions across 20 files.
  - GEM-5b1 (merged in PR #143): Rewrite the six top-level docs (`README.md`,
    `CONFIGURATION.md`, `INSTALL.md`, `QUICKSTART.md`, `WALKTHROUGH.md`,
    `SECURITY.md`) to use `antigravity`, with one short note in `README.md`
    that Gemini CLI support was removed and why. Document
    `AgentConfig.mention` and `retries` in `CONFIGURATION.md` as accepted and
    ignored. In `SECURITY.md`, say `.geminiignore` is no longer shipped,
    `agy` never read it, and `agy` roles should be assumed able to read
    `.env`, `logs/`, and `.git` until an operator live check shows `agy`
    honors `.gitignore`.
  - GEM-5b2 (merged in PR #144): Rewrite `docs/` (`ARCHITECTURE.md`, `TESTING.md`,
    `SUPERVISED_DISPATCH.md`, and the `docs/handoff/` prompts and README, in
    both the root and `examples/reference-workflow/` copies),
    `examples/reference-workflow/README.md`, the
    `examples/reference-workflow/dispatch_config.yaml` comments, `AGENTS.md`
    and `examples/reference-workflow/AGENTS.md`, and the provider-name
    sentence in `.agents/agents/handoff-router.md` and
    `.codex/skills/llm-handoff-validator/SKILL.md` (root and example copies)
    to use `antigravity`. Update the `tests/ported/test_handoff_docs.py:255`
    assertion that quotes the old ARCHITECTURE wording. Finish with a grep of
    `llm_handoff/`, `tests/`, `examples/`, and the config files for
    `gemini`, excluding `.claude/worktrees/`: it may find only rejection code
    and tests, agy model ids, the agy settings path, `GEMINI.md`, the
    `GEMINI_API_KEY` vendor-key entry, the `.gemini/**` protected path, and
    `CHANGELOG.md`.
  - GEM-5c (merged in PR #148): Replace "Gemini" in the provider-name
    sentence of `.claude/agents/handoff-router.md` and
    `examples/reference-workflow/.claude/agents/handoff-router.md` with
    "Antigravity". Claude roles cannot write `.claude/` paths, so this story
    routes to `user` (as LIN-9d did).

### 10. HARDENING - security followups

Fit: closed. HARD-1 through HARD-6 are merged (HARD-6 in PR #157); the epic
close was approved at `3949ed2388e6a726012cfbf0c8d3511fba5ea179`. Nothing
remains. CHAT-18's dependency on the HARD-3 marker is
satisfied.

The security-relevant Open Followups, promoted to stories. Each story names
the followup it closes; all five were confirmed in the code at `d67722e`.

- HARD-1: Redact reviewer stderr in start-failure messages (closes
  AUDIT-COST followup (a)). `_start_failure_message` in
  `llm_handoff/audit_review.py` puts the last 2000 characters of raw reviewer
  stderr (`_START_FAILURE_STDERR_CHARS`), newlines included, into the
  `audit_reviewer_start_failed` error that the dispatcher logs. The package
  has no redaction helper. Add one shared helper that masks secret-shaped
  values (vendor key prefixes such as `sk-` and `AIza`, bearer tokens, and
  `NAME=value` pairs whose name ends in `_KEY`, `_TOKEN`, or `_SECRET`), run
  the stderr through it and `_single_line`, and cap it near the 300
  characters other reviewer errors use. Apply the helper wherever else
  provider stderr reaches a log line or a pause reason. Test with a planted
  key. Merged in PR #146.
- HARD-2: Delimit operator text in operator-role prompts (closes
  PAUSE-RECOVERY followup (c)). `OPERATOR_ROLE_INSTRUCTION_TEMPLATE` and
  `OPERATOR_ROLE_AUDIT_FAN_OUT_INSTRUCTION_TEMPLATE` in
  `llm_handoff/orchestrator.py` put the pause reason and the operator's
  message (`Operator guidance: <text>`) inline in the instruction. Wrap each
  in labelled delimiters and neutralize a closing delimiter that appears
  inside the text, so neither can pass as dispatcher instructions. Test with
  text that contains the delimiter. Decided by the planner on 2026-10-18:
  labelled markers `<<<PAUSE REASON>>>` / `<<<END PAUSE REASON>>>` and
  `<<<OPERATOR MESSAGE>>>` / `<<<END OPERATOR MESSAGE>>>`; runs of three or
  more `<` or `>` inside quoted text are split with spaces; the planner
  operator template (`OPERATOR_PAUSE_INSTRUCTION_TEMPLATE`) is covered too.
  Merged in PR #147.
- HARD-3: Record operator influence on audits (closes PAUSE-RECOVERY
  followup (d)). An `@auditor` dispatch with an operator message tells the
  reviewers to apply that message within the review, and nothing records it:
  `audit_review.py` has no operator marker. Mark the audit result as
  operator-influenced in the audit record under `logs/dispatch/audit/`, in
  the handoff the auditor writes, and in the pull request body when the
  story publishes. Record only the marker, the time, and a SHA-256 digest of
  the message, never its text: an operator may type private context or a
  credential at the console, and `SECURITY.md` keeps secrets out of
  `HANDOFF.md`, logs, and anything published. CHAT-18 reuses
  this marker.
  Decided by the planner on 2026-10-18: fan-out path only (the single-agent
  auditor writes its own handoff and stays unmarked); the marker is a
  `## Operator Influence` handoff section, an `operator_influence` key in
  `review.json`, and a `### Operator influence` pull request subsection, each
  holding the UTC time and the SHA-256 of the raw message. Merged in PR #151.
- HARD-4: Validate `handoff_path` and `project_state_path` (closes
  CONFIG-CLEANUP followup (b)). The validator in `llm_handoff/config.py`
  (`_normalize_relative_path`) accepts any value, and the dispatcher builds
  `repo_root / path`, so an absolute path or a `..` segment points outside
  the repository. Reject absolute paths, drive-letter and UNC paths, `..`
  segments, and control characters at config load, and check that the
  resolved path stays under `repo_root`. Name the key and the rule in the
  error. Decided by the planner on 2026-10-18: a field validator rejects
  empty values, control characters, absolute, UNC, and drive-letter paths,
  and `..` segments; a model validator rejects a value whose resolved path is
  not strictly inside the resolved `repo_root` (symlink escapes and `.`);
  errors name the key, `repr()` of the value, and the rule.
- HARD-5: Stop trusting a carry-over record from disk (closes
  LINEAR-FOUNDATIONS followup (d)). `_restore_implementer_leftovers` in
  `llm_handoff/orchestrator.py` loads `logs/dispatch/carry-over.json` at
  startup, and `capture_pre_dispatch_state` (`llm_handoff/work_claim.py`)
  drops every listed file whose fingerprint still matches from the
  operator-dirty set, so later commit logic treats it as implementer work.
  Roles can write under `logs/`, and values saved in that same file (such
  as the handoff SHA or `HEAD`) prove nothing: a role can read the current
  values and fingerprints and forge a matching record that claims the
  operator's edits. Treat a record read from disk as untrusted. At startup,
  name its story and files and ask the operator to confirm them
  (`Carry these over as <story>'s work? [y/N]`, TTY only); without a TTY or
  on no, discard it with a WARN and leave the files operator-owned. Only an
  in-memory record from the current run applies without asking. Use a
  location roles cannot write only if one exists for every provider; Claude
  roles' `Write` tool is not path-limited, so do not assume one.
  RECOVERY-2's in-flight record has the same exposure and should follow the
  same rule. Decided by the planner on 2026-10-19: prune first; ask only with
  `interactive_pause` and a TTY console; `y`/`yes` accepts; decline, EOF, no
  TTY, and dry run never apply it; dry run keeps the file; control characters
  are escaped in the prompt. Approved at `1f601433becc09ffe6f729dfbdbcb27bd72c3cc2`
  (merged in PR #154) with one operator-accepted medium security finding, now
  HARD-6.
- HARD-6: Race-safe Windows carry-over deletion and writing (follow-up from the
  HARD-5 security review). On Windows, `llm_handoff/orchestrator.py` (about
  line 3778) checks for links and then deletes `logs/dispatch/carry-over.json`
  by pathname, and the writer separates path validation from `os.replace`. A
  process that can write under `logs/` can swap `logs/dispatch` for a junction
  after the check, so the dispatcher deletes or writes an external file with
  its own permissions. HARD-5 accepted this race and documents it in
  `CHANGELOG.md`; POSIX already uses no-follow directory descriptors. Use
  Windows directory handles with reparse-point rejection and protection
  against directory replacement for the whole delete and write; if that
  guarantee is unavailable, disable disk persistence and deletion of the
  carry-over record on Windows and keep it in memory only. Test with a
  junction swap where the platform allows it, and update the `CHANGELOG.md`
  limitation note. Decided by the planner on 2026-10-19: before writing or
  deleting the record without directory-fd support, pin `logs` and
  `logs/dispatch` with `CreateFileW` (through `ctypes`) without
  `FILE_SHARE_DELETE` and with `FILE_FLAG_OPEN_REPARSE_POINT`, rejecting a
  reparse point or non-directory, and hold both handles for the whole write or
  delete; when pinning is unavailable, keep the record in memory only (no
  read, write, or delete; one `WARN`); POSIX unchanged. Implementation note:
  the pin handles request `FILE_LIST_DIRECTORY` in addition to
  `FILE_READ_ATTRIBUTES`, because Windows does not enforce share modes on an
  attributes-only handle (a test showed the rename succeeding without it).
  Audit fix (2026-10-19, after the `5cbe641` audit): the pin does not stop an
  empty `logs/dispatch` from being turned into a mount point in place;
  `FSCTL_SET_REPARSE_POINT` needs only write-attributes access, which share
  modes do not govern (a probe set one under a pin with share mode 0). The
  Windows delete and writer now open the file as a handle, require its final
  path to be inside a handle to `logs/dispatch`, and delete through that
  handle or write through it and rename it relative to the directory handle
  (`NtSetInformationFile` with `RootDirectory`). Windows-only regressions set
  the mount point in place while the pin is held. Second audit fix
  (2026-10-19, after the `c19452b` audit): the pins themselves were opened by
  full path, so an empty `logs` turned into a mount point between the two
  pins redirected the `logs/dispatch` pin outside the repository. Each pin is
  now opened relative to its parent's open handle (`NtCreateFile` with
  `RootDirectory`, starting from a handle to the repository root), the
  parent handle is checked for a reparse point after the child opens, and
  the child's final path must equal the parent's plus the child name. The
  record file is opened and created relative to the pinned `logs/dispatch`
  handle, which is checked for a reparse point before the delete and the
  rename. A relative open that meets an in-place mount point fails with
  Windows error 1921, which is treated as a link. A Windows-only regression
  mounts an empty `logs` between the two pins for both the delete and the
  write.

### 11. LINEAR-SWITCH - mode switching and onboarding

Fit: low urgency; only repositories that switch tracker modes need it.
LINEAR-WORKFLOW is closed, so nothing blocks it.

- LIN-11: `init --tracker linear` and `init --tracker none` switch an
  initialized repository between modes, only between assignments and never
  while `.dispatch.lock` exists; `--dry-run`; `--force` reports what is left
  behind; a round trip is byte-identical.
- LIN-12: After `init --tracker linear`, print connector setup steps, the
  `doctor` command, and the probe command; add `docs/LINEAR.md`.

### 12. RECOVERY - interrupted-run recovery

Fit: closed. RECOVERY-13, the last story, merged in PR #182, and the epic
close is commit `c07dcdd`.

A Ctrl+C, a closed window, or a crash at any point must leave the loop able
to continue from a record it wrote, without the operator cleaning the working
tree by hand. The dispatcher resumes deterministically; it does not guess
whose edits are whose. Today a Ctrl+C on Windows also reaches the agent
(children share the console process group), and a restart treats the agent's
partial edits as operator edits that are never committed, so the story
bounces until the three-fix circuit breaker escalates.

- RECOVERY-1 (merged in PR #160): Graceful Ctrl+C. Start agent processes in their own process
  group on Windows (`CREATE_NEW_PROCESS_GROUP`, like POSIX
  `start_new_session`) so a console Ctrl+C reaches only the dispatcher. The
  first Ctrl+C requests the same stop as `llm-handoff stop` and prints `Stop
  requested; finishing the current <role> run. Press Ctrl+C again to stop
  now.`; the second stops at once and takes down the agent process tree.
  Implementation note: the SIGINT handler sets an in-process flag that the
  loop's stop checks read, rather than writing a stop-request file from the
  handler. A role is active for a whole cycle (set before the dispatch, the
  audit fan-out, or the epic-close finalizer; cleared at the loop top and at
  pauses), so the first Ctrl+C lets that cycle's commit, gates, and publish
  finish. Supervised dispatch gets no graceful Ctrl+C stop (Ctrl+C reaches
  only the supervisor; `llm-handoff stop` stays the graceful path there), and
  the dispatcher's own `git` calls stay in the console process group. The
  supervisor kills the child dispatcher's tree on any exception, and the
  work-claim verification run kills its tree on any exception, because
  neither child receives the console Ctrl+C anymore. Agent, verification,
  and supervised-child launches go through
  `agent_process._start_isolated_process`, which holds a Ctrl+C until
  `Popen` returns so a stop-now press cannot strand a new child; the
  supervisor's `main` installs the console interrupt handler (with no active
  role) so that hold applies there too. The handoff normalizers still call
  `Popen` directly (deferred PR #160 review finding).
- RECOVERY-2: Split by the planner on 2026-10-19:
  - RECOVERY-2a (merged in PR #165): In-flight record and interrupted
    implementer dispatch recovery. `llm_handoff/in_flight.py` holds `InFlightRecord` (version 1,
    cycle, role `backend`/`frontend`, story key, phase `dispatch`, HEAD, the
    pre-dispatch `HANDOFF.md`, operator dirty-file fingerprints, carried-over
    paths) and a loader that rejects bad JSON, versions, roles, phases, types,
    and a story key that does not match the handoff. `run_loop` writes
    `logs/dispatch/in-flight.json` right before a gated implementer dispatch
    and deletes it once `_dispatch_route` returns, through HARD-6's race-safe
    helpers (parameterized by file name); no write, one WARN per run, when Git
    does not ignore it or no race-safe mechanism exists. At startup, before
    the carry-over prompt, a record is compared with the tree (agent files,
    changed operator files, changed handoff); nothing changed deletes it,
    dry run only logs it, and HARD-5's rule applies: on `y` the agent files
    carry over, a half-written handoff is restored, and the same role is
    re-dispatched (`interrupted_dispatch_recovery`) with a note listing its
    files; a changed operator file pauses the first cycle instead
    (`INTERRUPTED DISPATCH:`), and those files are never staged.
  - RECOVERY-2b (merged in PR #166): Resume at the commit after an
    interrupted handback. The
    record advances to a handback phase once _dispatch_route returns and is
    cleared at the loop top; at startup that phase skips the re-dispatch and
    resumes at the dispatcher commit and gates, with the same confirmation
    rule. Decided by the planner on 2026-10-19:
    - Record: phase becomes `dispatch` or `handback`; `handback_content`
      (the handoff when `_dispatch_route` returned) and `handback_head_sha`
      (HEAD then) are added and serialized. The loader requires a string
      `handback_content` for `handback` and none for `dispatch`, and a string
      or None `handback_head_sha`. Version stays 1, so a 2a record still
      loads as a `dispatch` record.
    - Folded in from the 2a security review: the loader also returns None on
      `RecursionError` from `json.loads`, so a deeply nested record under the
      8 MiB limit takes the invalid-record path instead of crashing startup.
    - `run_loop` rewrites the record as a `handback` record after
      `_dispatch_route` returns, through the same race-safe writer; a failed
      rewrite deletes it, and a failed delete keeps the 2a ERROR and exit 1.
      The per-run state tracks the handback record, which is deleted at the
      loop top (and when the run returns from that cycle); a failed delete
      logs an ERROR and returns 1. Non-implementer, ungated, and dry-run
      dispatches write nothing.
    - Startup: a moved HEAD or changed handoff discards the record with one
      WARN and no restore (kept in dry run). Otherwise agent and changed
      operator files are found as for `dispatch`; dry run logs and keeps it.
      HARD-5 confirmation shows the summary, the assignment summary, and the
      handback's 12-hex sha256 prefix, then asks
      `Resume <role>'s interrupted <story> handback at the dispatcher commit? [y/N]`,
      with every path and story escaped. Decline, EOF, or no TTY discards it.
      Yes with changed operator files pauses with `INTERRUPTED HANDBACK:`;
      otherwise the agent files carry over, the record is deleted, and a
      `PendingPlannerRecovery(kind="interrupted_handback")` is scheduled.
    - The resumed cycle (source `interrupted_handback_recovery`) does not
      call `_dispatch_route` and writes no record; it uses the record's
      pre-dispatch handoff (or `assignment_content`) as the assignment for
      the commit and gate, its sha as `prior_handoff_sha`, rebuilds
      `PreDispatchState` from the record, treats the dispatch as exit 0, and
      then runs the normal post-dispatch path. The restored values travel in
      the pending recovery, not by re-reading the record.
  - RECOVERY-2c (approved at `10bdbee`, merged in PR #167): Untrusted
    protected-file baseline in interrupted-run
    recovery. Source: the RECOVERY-2b security review's medium finding (a
    role could forge the record's `protected_digests` and `head_sha` so a
    resumed cycle missed a protected-file change), plus the documented
    dispatch-phase gap (the re-dispatched cycle captured its baseline after
    the restart). Applies only with `protected_paths` set: (1) a `dispatch`
    or `handback` record whose `head_sha` is None, not a commit, or not HEAD
    or an ancestor of HEAD is discarded before any prompt with one WARN
    (kept in dry run); (2) a confirmed recovery's `protected_before` is the
    record's digests with every protected file uncommitted at startup set
    to `unverified` (found by `git status` plus a direct comparison of the
    protected files on disk with HEAD's tree, so ignored, skip-worktree, and
    assume-unchanged protected files count; RECOVERY-2c audit finding),
    carried in `PendingPlannerRecovery`, so each routes the
    cycle to `user` with `protected_file_modified:`; (3) the
    `interrupted_dispatch_recovery` cycle uses that snapshot too, and a
    `dispatch` record without `protected_digests` is discarded with a WARN;
    (4) one INFO line before each question lists the uncommitted protected
    files, those changed in `<head_sha>..HEAD`, and those changed by commits
    not on any remote-tracking ref (disclosure only). PR #167 review round 1:
    the HEAD comparison hashes raw bytes (CRLF to LF only), not `git
    hash-object`, so clean filters cannot hide an edit; a hidden protected
    edit keeps a `dispatch` record from the "nothing to recover" exit; and a
    recovery that pauses on changed operator files hands its baseline to the
    first dispatch after the pause.
  - RECOVERY-2d (approved at `ee76517`, merged in PR #168): NUL-delimited
    protected-path listing for commits. Source:
    the RECOVERY-2c security review's medium finding (`git log --name-only`
    without `-z` prints non-ASCII and control-character paths quoted, for
    example `".claude/agents/caf\303\251.md"`, so such a committed protected
    file never matched `.claude/**` and dropped out of the recovery disclosure and of
    `_protected_file_changes`). `_protected_paths_in_commits` adds `-z`,
    splits on NUL only (no stripping, no unquoting), and reads Git's stdout
    through a new `_run_git_stdout_raw` that keeps whitespace and decodes
    UTF-8 with `surrogateescape`; the handoff exclusion, pattern filter, and
    None on Git failure are unchanged. Disclosure paths stay escaped with
    `_escape_control_characters`. `_git_changed_files_since` is unchanged.
- RECOVERY-3: Split by the planner on 2026-10-19 (the other phases):
  - RECOVERY-3a (approved at `c29943e`, merged in PR #169): Stale Git index
    lock pauses at startup. A `.git/index.lock`
    left by a killed `git` (for example a second Ctrl+C during a dispatcher
    commit, since the dispatcher's own `git` calls stay in the console
    process group) pauses a real run at startup with its path (from
    `git rev-parse --git-path index` plus `.lock`, so an inherited
    `GIT_INDEX_FILE` is followed) and the check to run, before the
    publish resume and interrupted-run recovery; it is never deleted
    automatically. Polls until the file is gone; a dry run only warns.
  - RECOVERY-3b: Interrupted planner and finalizer dispatch, split on
    2026-10-19. The planner half merged as RECOVERY-3b1; the finalizer half
    is RECOVERY-5 below.
    - RECOVERY-3b1 (approved at `bb4678f`, merged in PR #171): Interrupted
      planner dispatch. A planner
      dispatch writes a `planner` in-flight record (dispatch phase only, null
      story key when the handoff has none, no operator files) and deletes it
      once `_dispatch_route` returns. At startup an unchanged handoff or a
      changed handoff that passes planner validation deletes the record and
      keeps the handoff; a missing, empty, or invalid handoff is restored from
      the record only after HARD-5 confirmation, and the planner runs again
      (`interrupted_planner_recovery`) against the recovery's protected-file
      baseline; an earlier forced instruction is not kept. In dispatcher commit
      mode, handoff-only commits since the record's untrusted HEAD are undone
      only after the operator confirms the exact `git reset --soft`.
- The rest of RECOVERY, renumbered flat on 2026-10-19 (old IDs in brackets;
  a live handoff's `remaining_stories` may still use them).
  Order: 8, 9, 10, 11, then 12.
- RECOVERY-5 (was RECOVERY-3b2; approved at `2e62500`, merged in PR #174):
  Interrupted finalizer dispatch. A finalizer
  dispatch writes a `finalizer` in-flight record (same shape as a planner
  record) right before `_invoke_epic_close` and deletes it once that returns.
  At startup, after the anchor checks: (a) Git is compared read-only with
  the record's HEAD and branch in any commit mode, and a branch switch,
  non-descendant history, or a commit with any file but the project
  state file (plus the handoff in agent commit mode) stops the run and
  keeps the record, never resetting Git; (b) an uncommitted project state
  edit stops the run too (dry run: INFO); (c) a valid changed handoff
  that no longer routes to `finalizer` deletes the record; (d) no ledger
  commit and an unchanged handoff deletes it; (e) otherwise the handoff
  is restored only after HARD-5 confirmation, and with a ledger commit
  the planner runs with the stale-finalizer instruction
  (`interrupted_finalizer_recovery`), without one the finalizer runs
  again, both against the recovery's protected-file baseline. The story
  branch also fixes the two high PR #173 security findings (the
  merge-conflict resolver did not compare index entries, and its handoff
  restore followed links) because its audit range includes that code.
- RECOVERY-6 (was RECOVERY-3c; approved at `7dcd6ad`, merged in PR #175):
  Interrupted audit fan-out: restore the
  handoff and re-run the read-only reviewers. Decided by the planner on
  2026-10-19:
  - Record: `auditor` joins the in-flight roles with the planner/finalizer
    shape (`dispatch` phase only, empty `dirty_fingerprints` and
    `carried_over`, null `assignment_content`, `story_key` null only when the
    handoff has no `carry_over_key`); the loader rejects anything else.
  - Write: `run_loop` writes an `auditor` record right before
    `_dispatch_route` when the route is `auditor`, `audit.security_enabled`
    is true (the fan-out path), there is no resumed handback, and it is not
    a dry run, and deletes it as soon as `_dispatch_route` returns, whatever
    its exit code or stop request; a failed delete logs an ERROR naming the
    record and returns 1. The single-agent auditor and dry runs write none.
  - Startup (`_recover_interrupted_auditor`): (a) `protected_paths` without
    record digests discards with one WARN; (b) the unanchored-record
    check; (c) a moved HEAD or switched branch, read-only in any commit mode,
    logs an ERROR naming the record, both 12-hex SHAs, and both escaped
    branches, keeps the record, and stops (dry run: INFO, no stop); (d) an
    unchanged handoff logs INFO "left nothing to recover" and deletes the
    record; (e) any other handoff is restored only after HARD-5 confirmation
    (`Restore the handoff and re-run auditor's interrupted <story> audit? [y/N]`),
    with a linked handoff path discarded before the prompt, decline, EOF,
    no TTY, or `interactive_pause: false` discarding, and a confirmed
    record that cannot be deleted stopping the run.
  - Re-run: a confirmed recovery schedules `PendingPlannerRecovery` kind
    `interrupted_audit`; the matching cycle routes to `auditor`
    (`interrupted_audit_recovery`) with the recovery's protected-file
    baseline and no forced instruction, and writes a fresh `auditor` record.
    Partial `logs/dispatch/audit/<timestamp>-cycle<N>/` directories are left
    untouched.
- RECOVERY-7 (was RECOVERY-3d; approved at `c39b42a`, merged in PR #176):
  Interrupted work-claim gate and scanners:
  re-run them. Decided by the planner on 2026-10-19: reuse the RECOVERY-2b
  handback resume rather than add a new phase. Once the dispatcher commit
  step has run, and again right before each work-claim gate call, the
  on-disk handback record is refreshed (`_refresh_in_flight_handback`) so its
  `handback_content` and `handback_head_sha` describe the post-commit state;
  the existing startup check then matches and the confirmed resume re-runs
  the (now no-op) commit, validation, and the work-claim gate with scanners
  on the stamped claim. No new record field, phase, or loader rule. A failed
  refresh write deletes the record; a failed delete too logs an ERROR naming
  the record and stops the run before the gate. An interrupt between
  `git commit` returning and the refresh write still discards the record
  (RECOVERY-9's no-record fallback is the follow-up).
- RECOVERY-8 (was RECOVERY-3e; approved at `e9f084a`, merged in PR #177):
  Publish steps safe to repeat, `pr` mode only
  (the `merge-commit` half is RECOVERY-12). Decided by the planner on
  2026-10-19: `StoryState` saves `publish_requested_head` and
  `publish_requested_at` before each push, and a publish of the same head
  reuses that time for the review wait, so a Codex review given while the
  dispatcher was down counts. At startup, after the paused-publish resume and
  outside dry run, `resume_interrupted_publish` acts when the saved story has
  no paused publish or pending review retry, its branch is checked out, and
  the handoff is that story's `verified_pass` with `close_type` `story` or
  `epic` and a resolvable `scope_sha`. It pauses with `INTERRUPTED PUBLISH:`
  (state kept) on a merge in progress or uncommitted files beyond the handoff
  and `logs/dispatch/`, HEAD beyond `scope_sha` other than clean base-branch
  merges, a `gh pr view` failure other than "no pull requests found", a
  closed pull request, a merged one with unresolved threads or saved
  findings, or a lost review bounce (unresolved findings saved for HEAD). A
  merged pull request with every thread resolved is cleaned up with no push
  or `gh` write; an open one, or none, publishes again only after HARD-5
  confirmation (`Resume publishing <story_id> on <branch>? [y/N]`).
- RECOVERY-9 (was RECOVERY-4; approved at `1e41789`, merged in PR #178):
  No-record fallback. When there is no
  in-flight record (a run from before RECOVERY-2, or a lost record) and files
  are dirty, in scope for the story's Work Packet, and clean at the story's
  last commit, ask `Treat these as <role>'s interrupted <story> work? [y/N]`;
  yes carries them over, no leaves them as operator edits. Never decide this
  without the operator. Decided by the planner on 2026-10-19: (1) the
  fallback runs in `_run_loop` only when neither `logs/dispatch/in-flight.json`
  nor `logs/dispatch/carry-over.json` existed at startup and the interrupted
  and carry-over recoveries returned nothing, so a start with a record never
  gets a second question; (2) the handoff must route to `backend` or
  `frontend` with a carry-over key and an enforceable Work Packet; (3) offered
  files are dirty (handoff and `logs/dispatch/` aside), in scope, not
  protected, and unchanged by any commit after the story's last commit (the
  newest of 500 commits on HEAD whose subject is the `story_id` or starts with
  it and a space; HEAD without a `story_id` or such a commit), and any Git
  failure offers nothing; (4) a dry run logs one INFO line and asks nothing;
  otherwise, with `interactive_pause`, a TTY, and a console, the dispatcher
  logs the offered files and the ones left as operator edits, then asks the
  escaped question; `y` returns the files' fingerprints as this start's
  carry-over files, and anything else logs one WARN line and writes, stages,
  or deletes nothing.
- RECOVERY-10: Merge-conflict resolver sees hidden working-tree edits. From
  the RECOVERY-5 security review (medium, below the block threshold): the
  resolver's working-tree snapshot (`llm_handoff/publish.py` ~line 1350)
  hashes only paths `git status` reports, so a tracked file marked
  `assume-unchanged` or `skip-worktree` can be edited while the planner
  resolves the project state file, and the index comparison misses it
  because it ignores those flags. Snapshot the content and file type of
  every tracked path independently of `git status` and index flags, reject
  index-flag changes, and add regressions for a hidden edit with an
  unchanged staged blob. Decided by the planner on 2026-10-19: (1)
  `_worktree_snapshot` covers the union of every tracked path from
  `git ls-files -z` (unmerged paths deduped) and every path
  `git status --porcelain=v1 -z --untracked-files=all` lists (renames and
  copies as before); each path records its status code (empty when status
  does not list it), its `os.lstat` file type without following links
  (regular file, executable, symlink, directory, or missing), and a digest
  (sha256 of a regular file's bytes or a symlink's target, empty for a
  directory or missing path); any Git failure returns None so the callers
  fail closed; (2) an index-flag snapshot from `git ls-files -v -z` is taken
  before and after the planner, and any path, `project_state_path` included,
  whose `assume-unchanged` or `skip-worktree` flag changed, appeared, or
  disappeared adds `it also changed the index flags of <paths>`; a Git
  failure or a record with no tag and path fails closed. Backend
  implementation note: only the two flags are compared, not the raw tag letter, so a conflicted path's `M` to `H`
  tag change on staging is not a flag change. The existing index-entry and
  staged-resolution checks are unchanged. PR #179 review correction: a
  tracked submodule (gitlink, mode 160000 in `git ls-files --stage`) is
  fingerprinted by its HEAD plus its own recursive worktree snapshot instead
  of the constant directory fingerprint; an empty submodule directory keeps
  the directory fingerprint, and a non-empty one without `.git` or that Git
  cannot read fails closed.
- RECOVERY-11: Failed in-flight record deletes stop every recovery path.
  From the RECOVERY-6 audit (non-blocking): the implementer
  dispatch/handback, planner, and finalizer startup recovery paths call
  `_delete_in_flight_record` and ignore a False result (for example
  `_recover_interrupted_handback`'s discard, decline, and confirmed
  branches), so a run can continue while a stale record stays on disk; make
  each such call log an ERROR naming the record and stop the run, as
  RECOVERY-6 did for the auditor path, with a regression per path.
- RECOVERY-12: Interrupted merge-commit publish. Split from RECOVERY-8 on
  2026-10-19. In `merge-commit` mode an interrupt between `git switch <base>`,
  `git merge --no-ff`, and `git branch -d <story>` leaves the checkout on the
  base branch with `story.json` kept. A start should finish the remaining
  steps when the story branch is already merged into the base branch, and
  pause otherwise. Decided by the planner on 2026-10-19:
  `resume_interrupted_merge_commit` in `llm_handoff/publish.py` shares the
  `pr` resume's entry gate (saved state; handoff is that story's
  `verified_pass`, `close_type` `story` or `epic`, `scope_sha` resolving to
  the audited commit) through `_audited_story_commit`. Pauses keep the story
  state, start `INTERRUPTED PUBLISH:`, and list the manual steps (`git switch
  <base>`, `git merge --no-ff <branch>`, `git branch -d <branch>`, delete
  `story.json`, restart). On the story branch: `MERGE_HEAD`, a failed or
  non-handoff/non-`logs/dispatch/` dirty list, or HEAD not exactly the
  audited commit pauses; otherwise one INFO line and
  `Resume merging <story_id> into <base>? [y/N]`, where `y` runs
  `publish_story(mode="merge-commit")` and anything else (or no terminal)
  logs one WARN and keeps the state. On the base branch: `MERGE_HEAD` pauses;
  an existing story branch must have the audited tip and the audited commit
  must be an ancestor of HEAD (exit 1 pauses as unmerged, other failures
  pause as Git failures), then `git branch -d`, clear the state, and log one
  AGENT line; a gone branch with the audited commit merged clears the state
  with one INFO line, otherwise pauses. Never merges, resets, or asks there.
  Any other branch or a detached HEAD does nothing. `run_loop` calls it after
  the index-lock wait in `merge-commit` mode outside dry runs, sharing the
  hoisted `confirm_publish_resume` with the `pr` path; `outcome.stop` returns
  its exit code. Regressions in `tests/test_interrupted_merge_commit.py`.
- RECOVERY-13: Merge-conflict resolver fails closed on unreadable files.
  From the RECOVERY-10 security review (low, below the block threshold):
  `_worktree_snapshot`'s fingerprint (`llm_handoff/publish.py` ~line 1456)
  records an empty digest when a file cannot be read, so a file unreadable
  in both snapshots with unchanged status and type hides a content change.
  A read failure makes the snapshot fail (None), so conflict resolution
  pauses, with a regression. Decided by the planner on 2026-10-20:
  `_path_fingerprint` returns None when `os.lstat` fails with anything but
  `FileNotFoundError` or `NotADirectoryError` (still `missing`), or when a
  symlink's target or a regular file's bytes cannot be read; directories and
  other types keep an empty digest. `_worktree_snapshot` and
  `_gitlink_fingerprint` return None when it does, so the resolver pauses
  with "the merge state could not be read" or "the checkout could not be
  read".

### 13. CHAT - talk to the agents while the loop runs

Fit: parked by the operator on 2026-10-20; TPM-ROLE goes first. Resume CHAT
with CHAT-17 once TPM-ROLE closes. Nothing else blocks it: HARD-3 is merged
and RECOVERY is closed.

Operator redesign (2026-10-20). Chat works like a side coding session the
operator talks to while the loop runs: it answers right away, while the
current role keeps running, and it can fix what the operator asks for. It
edits files, and it changes the run by rewriting `HANDOFF.md`. A chat session
gets the same tools and permissions as the role whose provider it runs on:
the role's own launch arguments, `unsafe_bypass` included. Do not add a
read-only boundary to chat: no tool allowlists narrower than the role's, no
read-only sandbox, no disabling of MCP servers or hooks, and no pause because
chat changed `HANDOFF.md` or Git. Write access is the operator's decision,
so a reviewer finding that only says chat can write is out of scope. The
dispatcher records what chat changed and makes it pass through the same
gates as role work: chat file changes are committed with the run and
audited before anything publishes, and a chat rewrite of `HANDOFF.md` is
validated before the loop follows it. CHAT-16 (read-only session answer
launchers) was dropped unmerged; its branch has reusable answer and session
id parsing, but not its permission arguments.

Console only, TTY only; without a TTY the loop behaves exactly as today. No new
dependencies; Windows (`msvcrt`) and POSIX.

- CHAT-8 (was part of CHAT-1): Agent and normalizer processes start with
  stdin closed. `_run_command_streaming` (the launch path for every agent
  role provider and audit reviewer, through `_run_logged_agent_command`) and
  `_run_command` in `llm_handoff/agent_process.py`, and the Claude
  normalizer's `subprocess.run` in
  `llm_handoff/normalizer_providers/claude.py`, pass
  `stdin=subprocess.DEVNULL`, so no agent reads the operator's keystrokes.
  The Codex normalizer's `stdin=subprocess.PIPE`, the supervisor's child
  dispatcher launch, the work-claim gate, and the dispatcher's own `git`
  calls stay unchanged.
- CHAT-9 (was part of CHAT-1): Console chat line renderer. New
  `llm_handoff/chat_line.py` defines `ChatLine`, which draws the bottom
  prompt line `[<role> running] chat> <buffer>` (terminal-safe, cut to the
  terminal width with the buffer's last characters kept) and redraws it
  around lines written above it. `DispatchLogger` takes an optional
  `chat_line` (and `attach_chat_line`); when one is attached,
  `_write_console` writes through it, otherwise console output is unchanged.
  Not wired in yet: nothing creates a `ChatLine` besides tests.
- CHAT-10 (was part of CHAT-9): Chat keystroke capture component. New
  `llm_handoff/chat_input.py` defines `ChatInput`, which runs one daemon
  thread that reads keys through a `ChatKeyReader` (`open`, `read_chars`,
  `close`) and edits a `ChatLine` buffer: printable characters append,
  Backspace/Delete remove the last character, Enter (CR, LF, or CR+LF once)
  submits, Ctrl+C calls `interrupt()` (default `_thread.interrupt_main`),
  and other control or format characters are ignored. A non-blank submit
  queues the stripped text for `take_pending()` and notifies
  `CHAT_UNAVAILABLE_MESSAGE` (CHAT-2 adds an `on_submit` callback that
  receives the text instead and supplies the notice). `start`/`stop` restart cleanly, `stop`
  is idempotent and closes the reader once per start, and EOF or `OSError`
  ends the thread quietly. `WindowsChatKeyReader(msvcrt)` reads without echo
  and drops prefix keys; `PosixChatKeyReader(fd, encoding, termios, select)`
  clears `ICANON` and `ECHO` (keeping `ISIG`), drops escape sequences, and
  restores the terminal with `TCSADRAIN`. `chat_input_for_console` returns
  None for a non-TTY stdin or a missing platform module. Not wired in yet.
- CHAT-11 (was part of CHAT-10): Chat line and keystroke capture wiring.
  New `llm_handoff/chat_session.py` holds one process-wide chat session
  (`attach`, `detach`, `set_role`, `suspended`), a no-op when nothing is
  attached; a `ChatInput.start()` `OSError` leaves capture off. When stdin
  and stdout are both TTYs and the run is not a dry run, `_run_dispatch`
  attaches a `ChatLine` to the `DispatchLogger` and the session, and detaches
  both in a `finally` around `run_loop`. The orchestrator calls
  `chat_session.set_role` next to every `console_interrupt.set_active_role`,
  so the line is drawn and keys are captured only while a role's cycle runs
  (the pause and review prompts run with the role cleared). The carry-over
  confirm and the manual frontend keypress wait run inside
  `chat_session.suspended()`, which stops capture and erases the line, then
  restores the role afterward. On Enter, the console says the message was
  received but chat answers are not available yet. Under the supervisor the
  child's stdout is a pipe, so no chat line appears.
- CHAT-12 (was part of CHAT-2): Chat message queue file. New
  `llm_handoff/chat_queue.py` saves `ChatMessage(text, role, received_at)` to
  `logs/dispatch/chat-queue.json` (`CHAT_QUEUE_STATE_PATH`) via temp-file
  replace; a bad file loads `[]`, bad entries are skipped; also clear and the
  queued-message notice. CHAT-13 holds the loop on it.
- CHAT-13 (was part of CHAT-2): Hold the loop on saved chat messages.
  Approved at `cd0df4c` (PR #199). `_hold_for_chat_messages` in `llm_handoff/orchestrator.py` reads the
  CHAT-12 queue file; an empty or missing queue changes nothing. A dry run
  logs one WARN that a real run would hold for N saved message(s), and a run
  without an interactive console logs one WARN that the loop does not hold;
  both leave the file. Otherwise the hold clears the console role (restored
  in a `finally`) inside `chat_session.suspended()`, lists each message as
  `[<role>] <text>` (`no role` when unset, control characters escaped),
  drains type-ahead, and prints `Press Enter to continue`. Any line clears
  the queue and continues; EOF or a stop request stops the run and keeps the
  queue; the loop never resumes on its own. The hold runs at startup after
  the Git index lock wait and before the merge-commit and pr publish resumes,
  at the loop top after the stop-request check, right before `publish_story`
  in the auditor branch, and right before `settle_deferred_findings` in the
  planner branch, so no push or merge happens while a message is saved.
  Chat capture is turned off before those last two holds and stays off
  through the publish or settle, so no message is saved after the check.
- CHAT-2: Sent chat messages are saved to the queue and announced.
  Approved at `dd26c6a` (PR #200). A non-blank `ChatInput` submission is appended to the CHAT-12 queue file with
  the active role and `received_at`, and the console prints
  `queued_message_notice(role)` (`Question queued until after the current
  <role> run completes.`) instead of `CHAT_UNAVAILABLE_MESSAGE`. CHAT-13
  then holds the loop on the saved message at the next safe point.
- CHAT-14 (was part of CHAT-3): Claude and Codex role dispatches report
  their session id. Approved at `b61b313` (PR #201). The Claude stream monitor keeps the `session_id` string
  from the first `system`/`init` event (with or without a logger) and
  `SubagentResult.session_id` carries it into `DispatchResult.session_id`;
  `invoke_codex` returns the `thread.started` `thread_id` of the run that
  produced the result. A missing id, the Claude buffered text fallback, and a
  failover result give None. Resume behavior is unchanged: the Claude and
  Codex role branches still ignore the `session_id` argument.
- CHAT-3: Dispatch record file. Approved at `6d387ab` (PR #202). New
  `llm_handoff/dispatch_record.py` saves
  `DispatchEntry(role, story_id, provider, session_id, recorded_at)` to
  `logs/dispatch/dispatch-record.json` (`DISPATCH_RECORD_STATE_PATH`) via
  temp-file replace; a bad file loads `[]`, bad entries are skipped; also
  clear and `record_dispatch` (UTC time, blank session id stored as None).
  CHAT-15 wires the loop to it.
- CHAT-15 (was part of CHAT-3): The loop records each dispatch. A real run
  clears the record at start, after the bounce counts are restored. After
  each role dispatch returns (whatever its exit code), `_run_loop` appends
  one `DispatchEntry` through `record_dispatch`: the route name as role, the
  assignment's story id, the role's configured primary provider (None for
  the manual frontend), and `DispatchResult.session_id`. Two dispatches give
  two entries in dispatch order. Resumed handbacks, the epic-close ledger,
  and dry runs are not recorded; a dry run neither clears nor writes, so an
  existing record is left as it was. A clear or write `OSError` logs one
  WARN naming the record path and the run or cycle continues unchanged.
  When Git does not ignore the record path, the run logs one WARN at start
  and neither clears nor writes it, so a tracked record is left untouched
  and the record never shows up in a work-claim gate. The clear and each
  write go through the carry-over record's link-resistant unlink and writer
  (entries are appended to a private staging copy, so the repository copy is
  never read); a symlink or junction on the record path logs one WARN and
  disables the record for the rest of the run.
- CHAT-16 (was part of CHAT-4): Dropped unmerged on 2026-10-20 by the
  operator redesign above. It built read-only answer launchers; CHAT-17
  replaces it.
- CHAT-17 (replaces CHAT-16): Chat side-session launcher. A new module
  starts one chat session for a message and resumes it for follow-ups,
  returning the answer text, the session id, the exit code, a short error,
  and the files the session's own edit events name (Claude `Edit`, `Write`,
  `MultiEdit`, and `NotebookEdit` tool calls in its stream output; Codex
  file-change items). It runs in the repository root on the provider of the
  active role (`claude` or `codex`), with that role's normal launch
  arguments, `unsafe_bypass` included, so it has the role's tools and
  permissions. The prompt names the active role, the story, the
  `HANDOFF.md` path, and the role's log, and says the session may edit files
  and `HANDOFF.md` to do what the operator asks. Another provider raises
  before launch. Not wired into the loop.
- CHAT-18 (was part of CHAT-4): Chat file changes are committed and audited.
  After a chat answer, the dispatcher records the files the CHAT-17 result
  names under `logs/dispatch/`, with their fingerprints, the time, and a
  SHA-256 of the message, never its text. Only those files count as chat
  changes: a file the role edits while the answer runs, or one chat changed
  without an edit event (for example through a shell command), stays the
  role's and goes through the normal scope check. During an implementer
  role, the work-claim gate commits the recorded files with the role's work
  even outside the Work Packet's files in scope, and the commit and pull
  request carry the HARD-3 operator-influenced marker. During any other
  role, the dispatcher commits them itself after the role's cycle, before
  the next route, with the same marker. Chat changes committed after an
  approved audit send the story back to the audit fan-out before it
  publishes, so nothing unaudited is published. Tested with a faked
  CHAT-17 result; the loop calls it once CHAT-20 wires chat in.
- CHAT-19 (was part of CHAT-4): A chat rewrite of `HANDOFF.md` takes over the
  run. When a chat answer changes `HANDOFF.md` while a role runs, the
  dispatcher stops that role as an operator interrupt does, keeps the role's
  partial file changes as carry-over, validates the chat's handoff, and
  continues from it; an invalid handoff pauses as today. The role is stopped
  before it can write its own handback, so the chat's handoff is not
  overwritten. File changes without a `HANDOFF.md` change let the role keep
  running. `docs/ARCHITECTURE.md` says chat may write `HANDOFF.md` this way.
  Tested with a faked CHAT-17 result; the loop calls it once CHAT-20 wires
  chat in.
- CHAT-20 (was part of CHAT-4): Messages are answered right away. Lands after
  CHAT-18 and CHAT-19, so writable chat is never wired in without them. A
  non-blank chat submission goes to the CHAT-17 side session instead of the
  CHAT-12 queue, and the answer prints above the chat line while the role
  keeps running; messages typed while an answer runs wait in order, and
  follow-ups resume the same side session for the rest of the run. After
  each answer the loop applies CHAT-18 and CHAT-19. New submissions no
  longer write the CHAT-12 queue, and the CHAT-13 `Press Enter to continue`
  hold runs only when that file already holds messages (saved before this
  story, or kept by an earlier stop), so none is stranded. At the points
  where the hold runs (before the publish and settle steps), and before the
  work-claim gate commits, the loop also waits for a running chat answer to
  finish. An agy role prints that chat is not available for agy yet.
- CHAT-4 (was part of CHAT-3): `@role` asks a finished role's own session.
  `@role <message>` finds the role's latest entry in the CHAT-15 dispatch
  record and resumes that session with the role's normal launch arguments,
  so the role that did the work answers and can fix it; the CHAT-18 and
  CHAT-19 rules apply to what it changes. `@role` for the running role, or a
  role with no recorded session, goes to the side session. Untargeted
  messages always go to the side session (the old CHAT-7 normalizer
  classification is dropped).
- CHAT-5 (was part of CHAT-3): Chat on agy. The side session and `@role`
  answers run on agy with the role's normal agent and grants, as agy role
  dispatches do.

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

### 15. TPM-ROLE - routine story picker split out of the planner

Fit: reliability; since GEM-3b the default planner is `antigravity`, so
without a tpm `agy` writes every story's Work Packet and reads
`remaining_stories` after every pass. This epic moves that per-story work to a
role on a provider that is strict about exact fields (codex in the example
mapping) and leaves `agy` the epic-level judgment. Pick it once GEMINI-REMOVAL
closes; it is independent of that epic apart from TPM-6 not recreating
`.gemini/`. If the default planner moves off `antigravity`, most of the reason
for this epic goes away; re-check the Fit before starting it. Operator
direction 2026-10-20: this epic is active, ahead of the rest of CHAT. The
self-host planner runs on claude only until TPM-ROLE lands; the operator then
moves it to agy with a codex tpm, so the reason above holds.

The source project's dispatcher separates product and boundary judgment (its
planner lane) from routine sequencing (a TPM lane that picks the next ready
item inside an approved scope, writes the lease, and may route only to an
implementer, the auditor, the planner, or the user). It added that lane
because a Gemini planner was unreliable at routine dispatch: its Work Packets
kept failing the validator on missing fields and scope claims, and it routed
stories whose work was already on main or had skipped audit. A Codex picker
with explicit escalation triggers (stale or already-done stories, ambiguous
criteria, competing next stories) caught a stale route on its first day. The
lane was added eight days after this extraction was seeded, so it was never
ported: here the planner does both jobs, every `verified_pass` and every
finalizer close route back to the planner, and the validator cannot constrain
a picker because no picker role exists. Add an optional `tpm` role. The name
is generic on purpose: `AGENTS.md` forbids provider names in roles and
`llm_handoff/roles.py` drops any label that contains one. The role is
optional so that every existing `dispatch_config.yaml` keeps working: without
`agents.tpm`, routing is unchanged.

Groomed on 2026-10-19 from six stories into twelve, each one Work Packet;
the `(was ...)` notes give the old IDs. None had started. Order matters: the
tpm's contract (prompt, helper files, Codex artifacts, Linear rules, and the
operator's `.claude/` agent) lands in TPM-1 to TPM-9, and routing work to
the tpm (TPM-10, TPM-11) comes only after it, so a configured tpm is never
dispatched without its instructions.

- TPM-1 (was part of TPM-1): Role vocabulary. Add `tpm` to `DispatchRole`,
  `CANONICAL_DISPATCH_ROLES`, and `ROLE_DISPLAY_NAMES`
  (`llm_handoff/roles.py`), `AgentRole`
  (`llm_handoff/config.py`), `NextAgent` (`llm_handoff/router.py`), and the
  normalizer's canonical set. `DEFAULT_AGENT_PROVIDERS` and
  `REQUIRED_AGENT_ROLES` stay at six roles; a config without `agents.tpm`
  loads byte-for-byte as before. Add `tpm:` patterns to
  `llm_handoff/rationalization_patterns.yaml`. `AGENTS.md` and
  `examples/reference-workflow/AGENTS.md` add `tpm` to the public role
  vocabulary (~lines 61-71); both are protected paths, so list them in Files
  in scope. The tpm Linear tool set moves to TPM-8; until TPM-2 and TPM-3
  land, a handoff routed to `tpm` pauses instead of dispatching.
- TPM-2 (was part of TPM-1): Launching a configured tpm. With `agents.tpm`
  present the role gets the same provider, launch-flag, timeout, fallback,
  and resume handling as the other roles (`llm_handoff/agent_roles.py`,
  `llm_handoff/agent_process.py`). `doctor` prints
  `tpm: not configured (planner picks stories)` when it is absent.
  `dispatch_config.example.yaml` maps `tpm` to codex with
  `skill_name: llm-handoff-tpm`, commented out, with a note that the planner
  picks stories when it is absent.
- TPM-3 (was TPM-2): Validator rules for `producer: tpm`
  (`llm_handoff/validator.py`), and the unconfigured case. `next_agent` must
  be one of `backend`, `frontend`, `auditor`, `planner`, or `user`;
  `finalizer` and `validator` are rejected as `frontmatter_routing_rule`.
  `status` may be omitted (an assignment) or one of
  `blocked_missing_context`, `blocked_implementation_failure`, or
  `escalate_to_user`; `ready_for_review`, `verified_pass`, and
  `verified_fail` are rejected as `frontmatter_status_producer_rule`.
  `close_type: epic` is rejected: a tpm that reaches the end of its
  `remaining_stories` routes to `planner` with the reason, never to the
  auditor with an epic close. A tpm assignment to `backend` or `frontend`
  must carry a `## Work Packet` exactly as a planner assignment must
  (`scope_claim_mismatch`, `acceptance_coverage_unclear`). `agent_self_loop`
  applies. When `next_agent: tpm` is read and `agents.tpm` is not
  configured, the orchestrator treats the handoff as not dispatchable and
  schedules the existing planner repair with the instruction `tpm is not
  configured; route to planner or an implementer` instead of pausing.
  A configured tpm keeps the interim pause until TPM-10, so it is never
  dispatched before its prompt (TPM-5) and helper files (TPM-6) land.
- TPM-4 (was part of TPM-4): Per-role Codex artifacts. Today every Codex
  role shares `logs/dispatch/codex/last-message.json` and `session.json`
  (`llm_handoff/agent_providers/codex.py` ~line 410), so a Codex tpm and a
  Codex backend, or a Codex auditor, would overwrite each other's saved
  session; use `logs/dispatch/codex-<role>/` for every role other than
  `backend`, which keeps its current path. `CONFIGURATION.md` lists the
  paths.
- TPM-5 (was part of TPM-3): The tpm prompt and the protocol docs. New
  `docs/handoff/TPM_HANDOFF_PROMPT.md`: read the handbook, `AGENTS.md`,
  `PROJECT_STATE.md`, and the handoff; take the next entry of
  `remaining_stories`; write the Work Packet from the code; escalate to
  `planner` when the story list is empty or ambiguous, two stories compete
  and order matters, a story conflicts with the project state, or the next
  story needs a design decision; escalate to `user` when a human decision is
  needed; never implement, audit, close an epic, edit `PROJECT_STATE.md`, or
  push. Update `docs/handoff/README.md`, `HANDBOOK.md`,
  `PLANNER_HANDOFF_PROMPT.md`, `PLANNER_INITIAL_PROMPT.md`,
  `AUDITOR_HANDOFF_PROMPT.md`, and `FINALIZER_HANDOFF_PROMPT.md` so each
  describes both configurations (tpm present, tpm absent). These are
  protected paths and must be in Files in scope. Docs only.
- TPM-6 (was part of TPM-4): tpm helper files outside `.claude/`. Add
  `.codex/skills/llm-handoff-tpm/SKILL.md` with `agents/openai.yaml` and
  `.agents/agents/tpm.md` (agy) to the source checkout and to
  `examples/reference-workflow/`; `init` copies them. No `.gemini/agents/`
  file: GEM-5 deleted both of those trees, and nothing may recreate them.
  `docs/INVENTORY.md` lists the new files.
- TPM-7 (was part of TPM-5): Linear prompt templates. The tpm picks Linear
  issues, as in the source project. Move selection rules 1 to 7 and the
  user-attention steps from
  `llm_handoff/prompt_templates/linear/roles/planner.md` into a new
  `roles/tpm.md`; the planner section keeps only epic-level duties and a
  one-line pointer. `LINEAR_PROMPT_ROLES` gains `tpm`. When `agents.tpm` is
  absent, `init --tracker linear` renders the selection rules into the
  planner section as today, so a six-role Linear repository is unchanged; a
  test renders both shapes. Add two hygiene rules to the tpm section: a
  leased issue already in `tracker.success_state` is stale, so post nothing,
  release the lease, and route to `planner`; an eligible issue with no
  priority is reported in the `lease_opened` comment as picked without one
  (rule 5 already covers the wording).
- TPM-8 (was part of TPM-5): Linear evidence and tools for the tpm.
  `LINEAR_EVIDENCE_EVENTS` allows `lease_opened`,
  `user_attention_requested`, and `user_attention_cleared` from `tpm` as
  well as `planner`; `tpm` gets the planner's Linear tool set. The
  `LINEAR-EVIDENCE` check accepts `tpm` as the lease producer.
  `expected_linear_outcome()` in `llm_handoff/audit_linear_evidence.py`
  ~line 81 treats `verified_pass` routed to `tpm` as `audit_passed`; today
  only `planner` and `finalizer` count and every other route is recorded as
  `audit_failed`, so without this a successful audit would post a failure
  comment. Add route tests for it.
- TPM-9 (was part of TPM-4): Operator edit of the Claude tpm agent (routes
  to `user`). Claude roles cannot write `.claude/`, so the planner writes
  `.claude/agents/tpm.md` (root and `examples/reference-workflow/` copies)
  into the handoff for the operator to add, as GEM-5c did, plus the Linear
  section TPM-7 renders into it. It comes before TPM-10 because a Claude
  tpm needs this file before any route reaches it. If TIDY-7 is still open,
  list its edits in the same handoff (operator batching, 2026-10-20).
- TPM-10 (was part of TPM-3): Routing to the tpm when it is configured
  (`llm_handoff/orchestrator.py`; `build_audit_handoff()` in
  `llm_handoff/audit_review.py` ~line 1147, which hard-codes the non-epic
  pass route to `planner`, and its routing tests). The auditor's story-level
  `verified_pass` routes to `tpm`; the epic-level `verified_pass` with
  `close_type: epic` still routes to `finalizer`; the finalizer routes to
  `tpm` after an epic close, or to `planner` when `PROJECT_STATE.md` lists no
  next epic. The planner opens an epic by writing `epic_id` and
  `remaining_stories`, then routes to `tpm` instead of assigning the first
  story itself. Pause recovery (`pause_recovery: planner`), the tracker
  lease rebuild, the planner self-loop path, and operator-prompt planner
  dispatch stay on the planner. Without `agents.tpm` every route is
  unchanged. TPM-10 also removes the interim pause for a configured tpm
  (`TPM_ROUTE_NOT_LAUNCHED_REASON`), so `next_agent: tpm` dispatches the
  tpm.
- TPM-11 (was part of TPM-3): Operator prompt `@tpm <message>`, like the
  other `@role` targets, refused with a clear message when `agents.tpm` is
  not configured.
- TPM-12 (was TPM-6): Documentation and changelog sweep. `README.md` role
  table and loop diagram, `docs/ARCHITECTURE.md` (role list, "planner picks
  stories unless tpm is configured"), `CONFIGURATION.md` (`agents.tpm`
  reference), `docs/TESTING.md` (a tpm scenario in the acceptance run),
  `docs/SUPERVISED_DISPATCH.md` if the repair role list changes,
  `CHANGELOG.md`. Each earlier story updates the docs its code touches; this
  story checks the set reads as one.

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

Fit: after TPM-ROLE, because TRANS-2 names `tpm`; otherwise independent,
and each story is small enough to fill a wait.

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
  (`frontmatter_status_producer_rule`). Check every fixture under
  `tests/ported/fixtures/handoffs/` and the reference-workflow starter
  handoff still validate; a legacy fixture that breaks the rule is updated,
  not grandfathered.
- TRANS-2: Auditor and finalizer producers. `verified_pass` and
  `verified_fail` require `producer: auditor`; a `verified_pass` carried
  forward by the finalizer keeps its existing exemption only when
  `producer: finalizer` and the previous route was `finalizer`.
  `producer: finalizer` may route only to `planner`, `user`, or `tpm` when
  configured, and never with `close_type` set.
- TRANS-3 (was part of TRANS-2): Validator producer and dispatcher-authored
  handoffs. `producer: validator` may route anywhere except `finalizer` and
  may not emit a completion status, with one exemption: the dispatcher's own
  publish handoffs, `build_review_bounce_handoff()` and the deferred-decision
  pass in `llm_handoff/publish.py` (~lines 1387 and 1469), write
  `producer: validator` with `verified_fail` or `verified_pass`. Either the
  orchestrator marks those writes as dispatcher-authored where it validates
  them, or the builders move to a distinct producer value in the same story;
  the review-bounce fixtures must keep validating.
- TRANS-4 (was part of TRANS-2): Producer matches the dispatched role. The
  post-dispatch validation already knows the previous route; a `producer`
  that names a role other than the one just dispatched is rejected as
  `frontmatter_producer_mismatch` unless the handoff is a dispatcher-written
  escalation.
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

Fit: TIDY-8 is unblocked; TIDY-7 is batched. The operator answered both
questions on 2026-10-20; this line supersedes the TIDY bullet in Current
Status until the next Work Packet rewrites it. TIDY-8: the operator wants
both Linear evidence events (`epic_closed` and `handoff_repaired`), so it is
a normal backend story the planner may pick by fit. TIDY-7: the operator is
ready for the `.claude/` edits but wants one pause, not one per story, so do
not route TIDY-7 to `user` on its own. Take it in the same `user`-routed
handoff as the next `.claude/` operator story (TPM-9, TRANS-6, or ACTIVE-8),
listing both stories' exact edits. That companion story's Work Packet adds
TIDY-7's files to its Files in scope and TIDY-7's wording check to its
acceptance criteria, so the companion audit covers both edits. When that
audit passes, the planner's next Work Packet records TIDY-7 as approved at
the companion audit's SHA (and its pull request), removes it from TIDY's
remaining stories, and, if TIDY then has none left, routes the TIDY epic
close to `finalizer` before picking new work. TIDY closes only after TIDY-7.
Nothing here blocks another epic.

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
  `.claude/` operator story's `user` route, as the Fit line says.
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
- **GEMINI-REMOVAL** - Google-vendor paths moved to `agy`, Gemini CLI provider
  removed (GEM-0b through GEM-5c; last story GEM-5c in PR #148). Epic close was a verification-only audit of an empty range at
  `55e2ca3c757d4c02cc13dad42060f374caef630e`; approved with no findings.
  Verification: dispatcher ran `python -m pytest tests -q -p no:cacheprovider`
  on that commit, exit 0 (operator run: 2692 passed, 16 skipped).
- **HARDENING (HARD-1 through HARD-6)** - security followups closed: HARD-1
  reviewer stderr redaction (PR #146); HARD-2 operator text delimiters
  (PR #147); HARD-3 operator-influence audit marker (PR #151); HARD-4
  `handoff_path` and `project_state_path` validation (approved at `d981442`);
  HARD-5 carry-over record confirmation (PR #154, audited at
  `1f601433becc09ffe6f729dfbdbcb27bd72c3cc2`). HARD-5 audit approved; quality
  pass, security reviewer found one medium Windows junction-swap race,
  operator-accepted and carried as HARD-6. Verification: dispatcher
  ran the full suite at the audited SHA, exit 0 (2784 passed, 26 skipped).
  HARD-6 race-safe Windows carry-over deletion and writing (PR #157 merge
  `0fb740c`, audited at `3949ed2388e6a726012cfbf0c8d3511fba5ea179`); quality
  and security reviews passed with no findings; epic close approved with
  `remaining_stories: []`. Verification: dispatcher ran focused tests and the
  full suite at the audited SHA, exit 0.

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file. The finalizer appends to the
  ledger and advances Active Epic on each epic close.
- Git history is the durable record of completed work.
