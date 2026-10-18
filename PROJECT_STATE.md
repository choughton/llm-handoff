# Project State

This file is the durable state pointer for `llm-handoff`, which develops itself
through its own dispatch loop. Keep it short. Detailed implementation notes
belong in commits, handoffs, and pull requests.

## Current Status

- **Active Epic:** GEMINI-REMOVAL (GEM-0b, GEM-0c, and GEM-0d merged, GEM-0d
  in PR #122; GEM-0e1 merged in PR #123; GEM-0f merged in PR #124; GEM-0g
  merged in PR #125; GEM-0h in progress).
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
still backs `planner` and `frontend` in the default mapping, the `init
--provider gemini` mapping, the `gemini` security reviewer, and the `gemini`
normalizer (API and CLI paths). GEM-0 first makes `agy` roles work (the first
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
  split out as GEM-0e1.
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
  (AGY-3); confirm the all-agy mapping uses them. Prerequisite: do not start
  GEM-3 until a person has run the GEM-0 live check (an operator-run `agy`
  planner turn that writes a valid `HANDOFF.md` and runs
  `git log --oneline -5` and `python -m pytest --version` without a
  soft-deny) and reported pass. Deferred by the user on 2026-10-18.
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
  Delete both `.geminiignore` files; `agy` does not read them (see the facts
  above), so nothing changes for `agy` roles. Check whether `agy` honors
  `.gitignore`, and say in `SECURITY.md` which of those paths `agy` roles
  can read.
  `init` stops writing these into target repositories and leaves existing
  copies alone. Drop `.gemini/agents/` from the supervisor repair allowlist
  (`llm_handoff/supervisor.py`). Rewrite `README.md`, `CONFIGURATION.md`,
  `INSTALL.md`, `QUICKSTART.md`, `WALKTHROUGH.md`, `SECURITY.md`, `docs/`, and
  the handoff prompts to use `antigravity`, keeping one short note that Gemini
  CLI support was removed and why. Finish with a grep of `llm_handoff/`,
  `tests/`, `examples/`, and the config files for `gemini`: it may find only
  rejection code and tests, agy model ids, the agy settings path, and
  `GEMINI.md`. `.claude/agents/handoff-router.md` and its example copy name
  Gemini, and Claude roles cannot write `.claude/` paths, so route those
  edits to `user` (as in LIN-9d).

### 10. HARDENING - security followups

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
  key.
- HARD-2: Delimit operator text in operator-role prompts (closes
  PAUSE-RECOVERY followup (c)). `OPERATOR_ROLE_INSTRUCTION_TEMPLATE` and
  `OPERATOR_ROLE_AUDIT_FAN_OUT_INSTRUCTION_TEMPLATE` in
  `llm_handoff/orchestrator.py` put the pause reason and the operator's
  message (`Operator guidance: <text>`) inline in the instruction. Wrap each
  in labelled delimiters and neutralize a closing delimiter that appears
  inside the text, so neither can pass as dispatcher instructions. Test with
  text that contains the delimiter.
- HARD-3: Record operator influence on audits (closes PAUSE-RECOVERY
  followup (d)). An `@auditor` dispatch with an operator message tells the
  reviewers to apply that message within the review, and nothing records it:
  `audit_review.py` has no operator marker. Mark the audit result as
  operator-influenced in the audit record under `logs/dispatch/audit/`, in
  the handoff the auditor writes, and in the pull request body when the
  story publishes. Record only the marker, the time, and a SHA-256 digest of
  the message, never its text: an operator may type private context or a
  credential at the console, and `SECURITY.md` keeps secrets out of
  `HANDOFF.md`, logs, and anything published. CHAT-4 reuses this marker.
- HARD-4: Validate `handoff_path` and `project_state_path` (closes
  CONFIG-CLEANUP followup (b)). The validator in `llm_handoff/config.py`
  (`_normalize_relative_path`) accepts any value, and the dispatcher builds
  `repo_root / path`, so an absolute path or a `..` segment points outside
  the repository. Reject absolute paths, drive-letter and UNC paths, `..`
  segments, and control characters at config load, and check that the
  resolved path stays under `repo_root`. Name the key and the rule in the
  error.
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
  same rule.

### 11. LINEAR-SWITCH - mode switching and onboarding

- LIN-11: `init --tracker linear` and `init --tracker none` switch an
  initialized repository between modes, only between assignments and never
  while `.dispatch.lock` exists; `--dry-run`; `--force` reports what is left
  behind; a round trip is byte-identical.
- LIN-12: After `init --tracker linear`, print connector setup steps, the
  `doctor` command, and the probe command; add `docs/LINEAR.md`.

### 12. RECOVERY - interrupted-run recovery

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

### 13. CHAT - talk to the agents while the loop runs

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
  handoff and pull request record the result as operator-influenced (reuses
  the HARD-3 operator-influenced marker).

### 14. WORK-INTAKE - turn a plain request into epics and stories

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

The source project's dispatcher separates product and boundary judgment (its
planner lane) from routine sequencing (a TPM lane that picks the next ready
item inside an approved scope, writes the lease, and may route only to an
implementer, the auditor, the planner, or the user). That lane was added
eight days after this extraction was seeded, so it was never ported: here the
planner does both jobs, every `verified_pass` and every finalizer close route
back to the planner, and the validator cannot constrain a picker because no
picker role exists. Add an optional `tpm` role. The name is generic on
purpose: `AGENTS.md` forbids provider names in roles and
`llm_handoff/roles.py` drops any label that contains one. The role is
optional so that every existing `dispatch_config.yaml` keeps working: without
`agents.tpm`, routing is unchanged.

- TPM-1: Role enum and config. Add `tpm` to `DispatchRole`,
  `CANONICAL_DISPATCH_ROLES`, and `ROLE_DISPLAY_NAMES` (`llm_handoff/roles.py`),
  `AgentRole` and `LINEAR_ROLE_TOOLS` (`llm_handoff/config.py`), `NextAgent`
  (`llm_handoff/router.py`), and the normalizer's canonical set.
  `DEFAULT_AGENT_PROVIDERS` and `REQUIRED_AGENT_ROLES` stay at six roles; a
  config without `agents.tpm` loads byte-for-byte as before, and `doctor`
  prints `tpm: not configured (planner picks stories)`. With `agents.tpm`
  present the role gets the same provider, launch-flag, timeout, fallback, and
  resume handling as the other roles (`llm_handoff/agent_roles.py`,
  `llm_handoff/agent_process.py`). `dispatch_config.example.yaml` maps `tpm`
  to codex with `skill_name: llm-handoff-tpm`, commented out, with a note that
  the planner picks stories when it is absent. Add `tpm:` patterns to
  `llm_handoff/rationalization_patterns.yaml`.
- TPM-2: Validator rules for `producer: tpm` (`llm_handoff/validator.py`).
  `next_agent` must be one of `backend`, `frontend`, `auditor`, `planner`, or
  `user`; `finalizer` and `validator` are rejected as
  `frontmatter_routing_rule`.
  `status` may be omitted (an assignment) or one of `blocked_missing_context`,
  `blocked_implementation_failure`, or `escalate_to_user`; `ready_for_review`,
  `verified_pass`, and `verified_fail` are rejected as
  `frontmatter_status_producer_rule`. `close_type: epic` is rejected: a tpm
  that reaches the end of its `remaining_stories` routes to `planner` with the
  reason, never to the auditor with an epic close. A tpm assignment to
  `backend` or `frontend` must carry a `## Work Packet` exactly as a planner
  assignment must (`scope_claim_mismatch`, `acceptance_coverage_unclear`).
  `agent_self_loop` applies. When `next_agent: tpm` is read and `agents.tpm`
  is not configured, the orchestrator treats the handoff as not dispatchable
  and schedules the existing planner repair with the instruction `tpm is not
  configured; route to planner or an implementer` instead of pausing.
- TPM-3: Routing when the role is configured (`llm_handoff/orchestrator.py`
  plus the prompts under `docs/handoff/`, which are protected paths and must
  be in Files in scope). The auditor's story-level `verified_pass` routes to
  `tpm`; the epic-level `verified_pass` with `close_type: epic` still routes
  to `finalizer`; the finalizer routes to `tpm` after an epic close, or to
  `planner` when `PROJECT_STATE.md` lists no next epic. The planner opens an
  epic by writing `epic_id` and `remaining_stories`, then routes to `tpm`
  instead of assigning the first story itself. Pause recovery
  (`pause_recovery: planner`), the tracker lease rebuild, the planner
  self-loop path, and operator-prompt planner dispatch stay on the planner;
  the operator prompt accepts `@tpm <message>` like the other roles. New
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
  describes both configurations (tpm present, tpm absent).
- TPM-4: Provider adapters and the reference workflow. Add
  `.codex/skills/llm-handoff-tpm/SKILL.md` with `agents/openai.yaml`,
  `.claude/agents/tpm.md`, `.gemini/agents/tpm.md`, and `.agents/agents/tpm.md`
  to the source checkout and to `examples/reference-workflow/`; `init`
  copies them and `init --tracker linear` renders the tpm section.
  Codex artifacts are keyed per role: today every Codex role shares
  `logs/dispatch/codex/last-message.json` and `session.json`
  (`llm_handoff/agent_providers/codex.py` ~line 410), so a Codex tpm and a
  Codex backend, or a Codex auditor, would overwrite each other's saved
  session; use `logs/dispatch/codex-<role>/` for every role other than
  `backend`, which keeps its current path. Claude and agy already run
  per-role agent files. `docs/INVENTORY.md` lists the new files.
- TPM-5: Linear mode. Move selection rules 1 to 7 and the user-attention
  steps from `llm_handoff/prompt_templates/linear/roles/planner.md` into a
  new `roles/tpm.md`; the planner section keeps only epic-level duties and a
  one-line pointer. `LINEAR_PROMPT_ROLES` gains `tpm`;
  `LINEAR_EVIDENCE_EVENTS` allows `lease_opened`, `user_attention_requested`,
  and `user_attention_cleared` from `tpm` as well as `planner`; `tpm` gets the
  planner's Linear tool set. When `agents.tpm` is absent, `init --tracker
  linear` renders the selection rules into the planner section as today, so
  a six-role Linear repository is unchanged; a test renders both shapes. Add
  two hygiene rules to the tpm section: a leased issue already in
  `tracker.success_state` is stale, so post nothing, release the lease, and
  route to `planner`; an eligible issue with no priority is reported in the
  `lease_opened` comment as picked without one (rule 5 already covers the
  wording). The handoff-validator's `LINEAR-EVIDENCE` check accepts `tpm` as
  the lease producer.
- TPM-6: Documentation and changelog. `README.md` role table and loop diagram,
  `docs/ARCHITECTURE.md` (role list, "planner picks stories unless tpm is
  configured"), `CONFIGURATION.md` (`agents.tpm` reference and the per-role
  Codex artifact paths), `docs/TESTING.md` (a tpm scenario in the acceptance
  run), `docs/SUPERVISED_DISPATCH.md` if the repair role list changes,
  `CHANGELOG.md`. Each of TPM-1 to TPM-5 updates the docs its code touches;
  this story is the sweep that checks the set reads as one.

### 16. ESCALATION-RECONCILE - a stale user escalation gets one planner pass

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
  `producer: finalizer` and the previous route was `finalizer`. `producer:
  finalizer` may route only to `planner`, `user`, or `tpm` when configured,
  and never with `close_type` set. `producer: validator` may route anywhere
  except `finalizer` and may not emit a completion status. The post-dispatch
  validation already knows the previous route; a `producer` that names a
  role other than the one just dispatched is rejected as
  `frontmatter_producer_mismatch` unless the handoff is a dispatcher-written
  escalation.
- TRANS-3: One table, two readers. Write the producer-to-route-and-status
  table once in `docs/handoff/HANDBOOK.md` under `## Transition Table`, and
  add a test that parses that table and asserts it matches the validator's
  rule set, so a prompt edit and a validator edit cannot drift apart. The
  `handoff-validator` helper prompts and `.codex/skills/llm-handoff-validator`
  cite the table instead of restating it.

## Open Followups

Promoted to HARDENING stories and removed below: AUDIT-COST (a),
PAUSE-RECOVERY (c) and (d), CONFIG-CLEANUP (b), and LINEAR-FOUNDATIONS (d).
The remaining letters are unchanged.

- From the AUDIT-COST close (low, `llm_handoff/audit_review.py`):
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

- From the source-dispatcher comparison (2026-10-18, info; not filed as epics
  because each is one Work Packet or a design choice to confirm first):
  - (a) Linear mode: the finalizer posts nothing (`roles/finalizer.md`), so an
    epic close leaves no evidence comment; add an `epic_closed` event to
    `LINEAR_EVIDENCE_EVENTS` and `EVIDENCE_COMMENT.md`, posted on the last
    story's issue, if the operator wants a Linear-side record of closes.
  - (b) Linear mode: a validator or planner repair of a leased handoff posts
    nothing; a `handoff_repaired` event would let the `LINEAR-EVIDENCE` check
    accept a repaired lease whose producer changed.
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

## Notes

- `docs/handoff/HANDOFF.md` is the live state file and stays uncommitted in
  this repository; the committed copy is the public template.
- `PROJECT_STATE.md` is the durable status file. The finalizer appends to the
  ledger and advances Active Epic on each epic close.
- Git history is the durable record of completed work.
