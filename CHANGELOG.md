# Changelog

All notable changes to `llm-handoff` will be documented here.

This project follows a simple pre-release changelog until the first public tag.

## Unreleased

- Interrupted handback recovery: once a gated implementer dispatch returns,
  the dispatcher rewrites `logs/dispatch/in-flight.json` as a `handback`
  record (the handoff the implementer left and HEAD at that moment) and
  deletes it when the cycle finishes, instead of deleting it right after the
  dispatch. A `handback` record found at the next start means the cycle was
  interrupted before its dispatcher commit, validation, gate, or publish
  finished. When HEAD and the handoff are unchanged and the operator confirms
  `Resume <role>'s interrupted <story> handback at the dispatcher commit? [y/N]`
  at a terminal, the first cycle resumes at the dispatcher commit and runs
  validation and the work-claim gate without dispatching the role again. The
  assignment shown before that question is the exact text the resumed commit
  and gate use, and the start does not repair or normalize the confirmed
  handback first (the resumed cycle's post-dispatch repair does), so a
  handback such as `next_agent: AUDITOR` still resumes. A moved HEAD or a changed handoff discards the record with a `WARN` line and
  restores nothing; declining, no terminal, or `interactive_pause: false`
  discards it and the files stay operator edits; a changed operator file
  pauses with an `INTERRUPTED HANDBACK:` reason; a dry run only logs it. When
  the handback rewrite fails the record is deleted, and the run stops only
  when that delete fails too. The record also keeps the pre-dispatch
  protected-file digests, and the resumed cycle checks protected files
  against them and the pre-dispatch HEAD instead of a snapshot taken after
  the restart, so a protected file the implementer changed before the
  interrupt still routes the handoff to `user`. With `protected_paths` set, a
  `handback` record without those digests is discarded with a `WARN` line.
- A deeply nested `in-flight.json` that makes the JSON parser raise
  `RecursionError` is now treated as an invalid record (deleted with a `WARN`
  line, kept in a dry run) instead of crashing startup.
- Interrupted implementer recovery: the dispatcher now writes
  `logs/dispatch/in-flight.json` right before each gated `backend` or
  `frontend` dispatch and deletes it once the dispatch returns. A record found
  at the next start means that implementer was interrupted mid-dispatch (a
  second Ctrl+C, a closed window, or a crash). After the operator confirms
  `Recover <role>'s interrupted <story> work? [y/N]` at a terminal, the
  agent's partial files carry over to the story, a half-written handoff is
  restored, and the same role is dispatched again with a note that its run
  was interrupted. Declining, no terminal, or `interactive_pause: false`
  discards the record and the files stay operator edits. When an operator
  file changed in the meantime, the first cycle pauses with an
  `INTERRUPTED DISPATCH:` reason instead. A dry run only logs the record. The
  record uses the carry-over record's race-safe writer and link checks, is
  read through the same link-refusing directory walk (only a single-link
  regular file of at most 8 MiB is accepted), and is not written when Git
  does not ignore it. The half-written handoff is restored through the same
  link-refusing writer, and a handoff path that passes through a symlink or
  junction discards the record instead of writing outside the repository.
  A record that cannot be deleted after the dispatch returns stops the
  dispatcher before its commit and gates, a read that fails with a sharing,
  permission, or I/O error keeps the record for a later start, and an
  interrupted operator `@backend`/`@frontend` dispatch that continued the
  saved story is recovered as a continuation of that story. Because a role
  can rewrite the record, the dispatcher does not treat it as authenticated:
  before the question it logs the assignment a confirmation would restore
  (Files in scope, Files out of bounds, Verification commands, and a short
  SHA-256, escaped), and confirming approves that assignment.
- The interrupted-dispatch recovery and carry-over prompts now also escape
  Unicode format controls (bidi overrides and isolates) and line or paragraph
  separators, not only control characters.

- An audit verdict whose strings hold unescaped backslashes, such as a Codex
  quality reviewer's `rg -n '12\.'` in `commands_run`, now parses instead of
  failing `quality_verdict_malformed`. Each lone backslash is read as a
  literal one; valid JSON escapes keep their meaning.
- Behavior change: a console Ctrl+C during a role's dispatch cycle now asks
  for a graceful stop instead of killing the agent. Agent processes, the
  handoff normalizers, the work-claim verification command, and the
  supervised dispatch child now start with `CREATE_NEW_PROCESS_GROUP` on
  Windows (POSIX keeps `start_new_session`), so Ctrl+C reaches only the
  dispatcher; `taskkill /T /F` remains the Windows tree kill. The first
  Ctrl+C while a role's cycle runs prints
  `Stop requested; finishing the current <role> run. Press Ctrl+C again to
  stop now.`, sets an in-process stop flag, and the dispatcher finishes the
  cycle (dispatcher commit, gates, publish) and exits with code 0 before the
  next dispatch, as `llm-handoff stop` does. A second Ctrl+C kills the
  running agent's process tree and exits with code 130, as before; agent runs
  on the audit fan-out's worker threads stop as well. Ctrl+C outside a role's
  cycle (startup, between cycles, a pause wait, the operator prompt) still
  stops at once. Under supervised dispatch Ctrl+C reaches only the
  supervisor; use `llm-handoff stop` there. `git` commands the dispatcher
  runs itself still receive Ctrl+C. The supervisor now kills the child
  dispatcher's process tree, and the work-claim gate kills the verification
  command's process tree, when either is interrupted. A Ctrl+C that arrives
  while one of these children is being created is held until the child
  handle exists, so a stop-now press then kills the new child too. The
  supervisor installs the same Ctrl+C handler with no active role, so its
  Ctrl+C still stops at once but is held the same way while it starts the
  child dispatcher. The handoff normalizers do not use this guard yet.
- The operator prompt at a pause is now one line: "Press Enter to have the
  planner fix this, or type instructions for it and press Enter." The five
  help lines are gone; `retry`, `quit`, and `@backend|@frontend|@auditor`
  still work and are documented in
  [Operator Prompt](CONFIGURATION.md#operator-prompt). Once the planner has
  tried in the current pause streak, a second line offers the manual
  HANDOFF.md edit. The reason line above the prompt drops the generic
  "Update the handoff; dispatch will resume after the file changes." tail.
- Behavior change: a carry-over record loaded from
  `logs/dispatch/carry-over.json` at startup no longer applies on its own,
  because agent roles can write under `logs/` and a forged record could claim
  operator edits as implementer work. After dropping files that no longer
  match the record, the dispatcher asks
  `Carry these over as <story>'s work? [y/N]` when `interactive_pause` is on
  and it runs at a terminal with console input; `y` or `yes` applies the
  record. Any other answer, end of input, no terminal, or
  `interactive_pause: false` discards it with a `WARN` line, deletes the file,
  and leaves its files as operator edits. A dry run logs the record, keeps the
  file, and does not apply it. Control characters in the story key and paths
  are escaped before display. Previously the record applied without asking.
  The in-memory record kept between cycles of one run is unchanged. When the
  record path or a directory above it is a symlink or junction, the
  dispatcher no longer reads, writes, or deletes the record (previously it
  followed the link and could delete a matching record file outside the
  repository); it logs a `WARN` line instead. Saving the record no longer
  writes through the fixed `carry-over.json.tmp` name, which followed a
  symlink planted there; it creates a fresh, randomly named temporary file
  exclusively and atomically replaces the record. On POSIX it opens each
  directory under the repository without following links and writes,
  renames, and deletes the record relative to those directory handles. On
  Windows it holds `logs` and `logs/dispatch` open without delete sharing,
  rejecting either one if it is a junction or symlink, for the whole write or
  delete, so neither directory can be renamed, deleted, or replaced by a
  junction until the record is written or deleted. Each directory is opened
  relative to its parent's already-open handle (the repository root first),
  not by its full path, so turning an empty `logs` into a mount point while
  `logs/dispatch` is being opened cannot redirect it outside the repository.
  Because the pin cannot stop an empty directory from being turned into a
  mount point in place, the Windows writer and delete also check the pinned
  directory handles for a reparse point, open the record relative to the
  pinned `logs/dispatch` handle, refuse it unless the file handle is inside
  that directory, and then write, rename (relative to the directory handle),
  or delete through that handle instead of looking the path up again. Where
  neither mechanism is
  available, the record stays in memory only: the dispatcher does not read,
  write, or delete `carry-over.json` and logs a `WARN` line instead.
- Behavior change: in the audit fan-out, a security reviewer `fail` verdict
  whose findings are all below `audit.block_severity` no longer blocks the
  audit. `block_severity` decides, as the Codex Security scanner path
  already did. The audit passes with those findings under
  `## Security Notes` (repeated in the pull request body), the
  `## Merge Decision` section marks the security verdict non-blocking, the
  unresolved concerns name it, and the log records a `WARN`. A quality
  `fail` still always blocks, and so does a security `fail` with no
  findings or with a finding at or above `block_severity`. Before this, a
  security reviewer that rated its only finding `medium` but returned
  `fail` sent the story back to the implementer each round until the
  three-fix circuit breaker stopped it.
- Behavior change: with `wait_for_reviewers: [codex]`, a Codex review that
  Codex reports as failed no longer stops the loop at once. The dispatcher
  posts one `@codex review` pull request comment and waits for the same head
  commit again, recording the retry in `logs/dispatch/publish/story.json` so
  a restart does not ask twice and resumes that wait at startup while the
  story branch is still at that commit and `HANDOFF.md` is still the story's
  `verified_pass`. When the retried review fails too, a usage
  limit failed it (no automatic retry), or no review finishes in time, an
  operator prompt at a terminal offers `merge` (merge without the Codex
  review, with a `WARN` and a pull request comment recording the decision), `retry` (ask
  Codex once more and wait), or `quit` (the earlier stop with the pull
  request left open). Without a terminal the loop stops as before. A
  completed review with findings is unchanged and never gets a review
  request.
- In `pr` mode with `publish.base_branch` set, a start on a story branch
  whose pull request is already merged no longer stops at the next story
  ("Story branch not ready: still on story branch ...") after a full planner
  cycle. Before the first dispatch, when the checkout is on a
  `publish.branch_prefix` branch with no saved story state, its HEAD is on
  `<remote>/<base_branch>`, the local base branch has nothing that remote
  does not, and the only uncommitted file is the handoff, the dispatcher
  fast-forwards the base branch to the remote, switches to it, deletes the
  local story branch, and logs what it did. Nothing is pushed. Any other
  case logs a warning with the reason and keeps the previous stop. This
  covers an operator who merged a paused story's pull request but did not
  run `git switch <base>` and `git pull`.
- Behavior change: config load now rejects a `handoff_path` or
  `project_state_path` that does not stay inside the repository. An empty
  value, control characters, an absolute, UNC, or drive-letter path, a `..`
  segment, a value that names the repository root (such as `.`), and a value
  that resolves outside the repository through a symlink each fail with an
  error naming the key, the quoted value, and the rule. Previously these
  values were accepted and could point the dispatcher outside the repository.
- An audit fan-out dispatched with `@auditor <message>` at the operator
  prompt is now marked as operator-influenced in three places: an
  `operator_influence` key in the audit's `review.json` under
  `logs/dispatch/audit/`, a `## Operator Influence` section in the audit
  handoff (after `## Merge Decision`), and a `### Operator influence`
  subsection under `## Reviewer notes` in the pull request body. Only the UTC
  time the message was accepted and the SHA-256 digest of the raw message are
  kept; the message text is never recorded. An `@auditor` dispatch without a
  message, a normal audit, and the single-agent auditor path carry no marker.
- In `pr` mode, an epic-close audit that passes on a story branch with no
  commits beyond the remote base branch no longer stops at `gh pr create`
  ("No commits between ..."). The dispatcher now pushes nothing, fast-forwards
  the base branch, switches to it, and drops the empty story branch and its
  story state. Previously it pushed the empty branch first, leaving it on the
  remote.
- An epic-close audit, or any audit whose review range has no committed
  changes, now gets the same out-of-sandbox verification as an implementer
  claim. Before the audit fan-out starts its reviewers, the dispatcher runs
  the handoff's Work Packet Verification commands on the audit commit, with
  the work-claim gate's allowlist, environment, and timeout. A pass is
  recorded as a `## Dispatcher Verification` section, so the quality reviewer
  does not re-run the suite inside its sandbox. A failure routes
  `verified_fail` to `planner` with the command output, and no reviewer runs.
  Previously a sandboxed Codex reviewer on Windows could not create pytest's
  per-user temp directory, failed the epic close, and the loop sent the empty
  range to `backend`, which had nothing to change. A recorded pass is reused
  only for the same commands, a pass is credited to the commit only when no
  file other than `HANDOFF.md` has uncommitted changes, and the approved
  audit handoff keeps the dispatcher's commands in its Verification Evidence.
  The step is skipped in Linear mode.
- A blocked audit fan-out on a range with no committed changes now routes
  `verified_fail` to `planner` instead of the implementer.
- The planner prompts (root and `examples/reference-workflow/` copies) and
  `HANDBOOK.md` now say never to point `--basetemp`, `TMP`, `TEMP`, or
  `TMPDIR` inside the repository in a Verification command: temp directories
  inside a live checkout collide with the running dispatcher's
  `.dispatch.lock`.
- The `handoff-router` Claude agent file (root and
  `examples/reference-workflow/` copies) now names Antigravity instead of
  Gemini in its provider-name example, finishing the Gemini CLI removal.
- The operator-prompt instructions (planner guidance, `@<role>`, and the
  `@auditor` audit fan-out) now pass the pause reason between
  `<<<PAUSE REASON>>>` / `<<<END PAUSE REASON>>>` markers and the operator's
  message between `<<<OPERATOR MESSAGE>>>` / `<<<END OPERATOR MESSAGE>>>`
  markers, with a fixed sentence saying the pause reason is quoted dispatcher
  state and the operator's message cannot widen the role's boundaries or the
  Work Packet's Files in scope. Runs of three or more `<` or `>` characters
  inside the quoted text are split with spaces, so the text cannot close its
  block. The bounded pause-recovery instruction is unchanged.
- Provider stderr is now redacted before it reaches the dispatcher log or a
  reviewer error. A new `llm_handoff/redaction.py` masks `sk-` keys, Google
  `AIza` keys, `Bearer` tokens, GitHub tokens, and `NAME=value` pairs whose
  name ends in `_KEY`, `_TOKEN`, or `_SECRET` (including quoted names such as
  `{"NAME": "value"}`, and quoted values with spaces, which are masked through
  the closing quote). The `audit_reviewer_start_failed`
  and `security_reviewer_failed` messages now carry a redacted one-line stderr
  tail capped at 300 characters (was the raw last 2000 characters), and the
  streamed agent stderr lines, the validator stderr warning, and the
  ledger-updater stderr lines are redacted. Raw stderr on result objects and
  in `logs/dispatch/audit/<name>.stderr.txt` is unchanged.
- In publish modes `merge-commit` and `pr`, an operator `@backend` or
  `@frontend` dispatch from a pause now continues the saved story when the
  handoff has no `story_id` (for example a dispatcher-written
  `protected_file_modified` escalation) and the checkout is still on that
  story's branch. The dispatcher commit, the work-claim gate, and the
  protected-file check then read the saved story's Work Packet from
  `logs/dispatch/publish/story.json`, since the escalation only fences it.
  When the story branch is still not ready, the dispatcher logs the error and
  pauses at the operator prompt again instead of exiting 1.
- `docs/` (`ARCHITECTURE.md`, `TESTING.md`, `SUPERVISED_DISPATCH.md`, and the
  `docs/handoff/` prompts and README), `examples/reference-workflow/README.md`,
  both `AGENTS.md` copies, the `dispatch_config` audit comments, and the
  provider-name sentence in the `.agents/agents/handoff-router.md` and
  `llm-handoff-validator` skill helpers now name Antigravity (`agy`) instead of
  Gemini CLI. The reference workflow README no longer lists the removed
  `.gemini/` files or `.geminiignore`.
- The top-level docs (`README.md`, `CONFIGURATION.md`, `INSTALL.md`,
  `QUICKSTART.md`, `WALKTHROUGH.md`, `SECURITY.md`) no longer describe Gemini
  CLI, and `CONFIGURATION.md` now documents the `mention` and `retries` role
  fields as accepted and ignored.
- Removed the shipped Gemini CLI instruction files: `.gemini/agents/`,
  `.gemini/policies/planner_guardrails.toml`, and `.geminiignore`, both at
  the repository root and in `examples/reference-workflow/`. `llm-handoff
  init` no longer writes any `.gemini/` path or a `.geminiignore` (its
  `.geminiignore` managed-block merge is gone), and it leaves existing copies
  of those files in a target repository unchanged. `.gemini/agents/` is no
  longer in the default supervisor repair allowlist. `.gemini/**` stays a
  protected path.
- Removed the `gemini` role provider (`llm_handoff/agent_providers/gemini.py`,
  its Gemini CLI sandbox and retry handling, its sign-in failure markers, and
  its `GEMINI_*` and Google Cloud environment allowlist). `provider: gemini`
  on any `agents.<role>` or `agents.<role>.fallback` now fails config load
  with "The gemini provider was removed: Gemini CLI no longer serves personal
  Google accounts. Use provider: antigravity (the agy CLI)." Dispatcher
  startup and `llm-handoff doctor` no longer check for a container runtime,
  and doctor's Linear-mode tracker connector check no longer has a `gemini`
  line. `GEMINI_API_KEY` and `GOOGLE_API_KEY` are still stripped from every
  launch and still rejected in `env_passthrough`. The `tenacity` dependency,
  used only by the removed adapter, is dropped from `pyproject.toml` and
  `requirements-dev.txt`.
- Removed the top-level `planner_api_key_env` config key and the
  `--use-planner-api-key-env` flag; only the gemini adapter read them.
  `planner_api_key_env: false` is still accepted and ignored. Behavior
  change: a config with `planner_api_key_env: true` (or any value other than
  `false`) now fails to load with "planner_api_key_env was removed with the
  gemini provider: Gemini CLI no longer serves personal Google accounts.
  Delete the planner_api_key_env key; a Codex role uses use_api_key_env: true
  for OPENAI_API_KEY." Passing `--use-planner-api-key-env` now prints
  "--use-planner-api-key-env was removed with the gemini provider: Gemini CLI
  no longer serves personal Google accounts. Use provider: antigravity (the
  agy CLI)." to stderr and exits with code 2 before any config loads. The
  startup banner no longer prints a `Planner API env:` line.
- Removed the Gemini CLI security reviewer and its `gemini extensions list`
  checks, both at dispatcher startup and in `llm-handoff doctor` (the
  `security extension` line is gone). `audit: {security_provider: gemini}`
  now fails config load with "The gemini provider was removed: Gemini CLI no
  longer serves personal Google accounts. Use security_provider: antigravity
  (the agy CLI)." The automatic security reviewer choice is unchanged; it
  never picked `gemini`. The Gemini role provider is unchanged.
- Removed the `gemini` next-agent normalizer (its Gemini CLI and API paths,
  `llm_handoff/normalizer_providers/gemini.py`) and the all-Gemini `init`
  mapping. `normalizer: {provider: gemini}` now fails config load with "The
  gemini provider was removed: Gemini CLI no longer serves personal Google
  accounts. Use provider: antigravity (the agy CLI)."
  `llm-handoff init --provider gemini` now exits nonzero before writing any
  file, with "The gemini provider was removed: Gemini CLI no longer serves
  personal Google accounts. Use --provider antigravity (the agy CLI)." The
  `google-genai` dependency is dropped from `pyproject.toml` and
  `requirements-dev.txt`. `llm-handoff doctor` no longer has Gemini entries
  in its normalizer check. The Gemini role provider is unchanged.
- The default provider for `planner` and `frontend` is now `antigravity`
  (`binary: agy`) instead of `gemini`. `DEFAULT_AGENT_PROVIDERS`,
  `dispatch_config.example.yaml`, and
  `examples/reference-workflow/dispatch_config.yaml` all change; the two
  stanzas drop their `mention:` lines and keep their `resume` and
  `timeout_ms` values. `backend`, `auditor`, `validator`, and `finalizer`
  are unchanged. `llm-handoff init --tracker linear` with the reference
  mapping now writes `planner` and `frontend` on `provider: claude`
  (`binary: claude`, `resume: false`, `timeout_ms: 1200000`) with a comment
  above `planner`, because Linear mode does not support `antigravity`
  roles; `init --tracker linear --provider antigravity` is still refused.
  In Linear mode `init` also appends each role's
  `mcp__<tracker.mcp_server>__*` tools to the `tools:` line of the copied
  `.claude/agents/planner.md` and `.claude/agents/frontend.md`, as it
  already did for `handoff-validator.md`, because a Claude subagent's
  `tools:` list is an allowlist that otherwise blocks the Linear MCP tools.
  Behavior change: a hand-written Linear-mode config that omits `planner` or
  `frontend` now gets the `antigravity` default for that role and fails at
  load with the existing `tracker.provider: linear does not support
  provider: antigravity` message. Map both roles to `claude` or `codex`.
  A config without an `agents:` block now needs `agy` on `PATH` and the
  `agy` user settings entries for those two roles.
- Supervised repair now works with an `antigravity` repair role. The
  supervisor already launched the repair turn as
  `agy --agent supervisor-repair`, but no agent file had that name, so `agy`
  rejected the launch. The reference workflow now ships
  `.agents/agents/supervisor-repair.md` (root and
  `examples/reference-workflow/` copies, with the full eight-tool list and
  repair-only instructions). The repair launch also drops an `agent_name`
  override on an `antigravity` repair role and on an `antigravity` fallback,
  so it always passes `--agent supervisor-repair`; before, a role override
  launched the role's normal agent instead. Behavior change: the default
  supervised repair scope now admits `.agents/agents/`, in the order `docs/`,
  `.claude/agents/`, `.codex/skills/llm-handoff/`, `.agents/agents/`,
  `.gemini/agents/`.
- The work-claim gate now reports on the console while it runs. In steady
  state the console showed only `Running deterministic work-claim gate` and
  the PASSED or FAILED line, with everything else in the log file, so a
  long Verification command looked like a hang. The console now also shows
  the gate's `work-claim:` lines: the pre-dispatch and current HEAD and
  `scope_sha`, the changed files, each scanner and the Verification command
  as it starts, each check's PASS, FAIL, or SKIP with its detail, each
  scanner's outcome (a new line), and each Verification command's exit code
  and elapsed time. Command output tails (`work-claim | ...`) stay in the
  log file. Console lines quote repository-derived text (Verification
  commands, file names, agent messages), so the console writer now escapes
  terminal control and format characters (ANSI and OSC sequences, carriage
  returns, line feeds, bidi overrides, surrogates) as `\xNN` or `\uNNNN`
  before printing; the log file keeps the exact text.
- `llm-handoff init --provider antigravity` writes a new all-Antigravity
  mapping: every role on `provider: antigravity` and `binary: agy` with the
  all-Claude timeouts and no `unsafe_bypass`, `audit.security_provider:
  antigravity`, and `normalizer.provider: antigravity` with the default
  model. Each role resolves to a shipped `.agents/agents/` file; validator
  and finalizer keep the `handoff-validator`, `handoff-router`, and
  `ledger-updater` names the dispatcher passes. Behavior change: `init`
  provider auto-detect now looks for `claude`, `codex`, and `agy` instead of
  `gemini`. An `agy`-only machine gets the all-Antigravity mapping, and a
  `gemini`-only machine now gets the reference mapping; `--provider gemini`
  still writes the all-Gemini mapping. `init --tracker linear` with the
  all-Antigravity mapping, explicit or detected, now fails before writing any
  file, because Linear mode does not support `provider: antigravity`. The
  antigravity security reviewer passed an operator-run live check on
  2026-10-18 with `agy` 1.4.2: it returned `pass` on a real range and `fail`
  with a `high` finding on an injected range, using only `view_file`. The
  model declined the injected request, so the check does not show `agy`
  refusing a forced call to a tool outside the agent file's `tools` list.
- `audit.security_provider: antigravity` is now a read-only security
  reviewer that runs `agy`, and the automatic security reviewer order is
  `claude`, `codex`, `antigravity`. `gemini` left the automatic order; an
  explicit `audit.security_provider: gemini` still works. Each review runs
  `agy -p <fixed instruction> --agent <name> --output-format stream-json
  --json-schema <schema> --print-timeout <N>s` from a new private temp
  directory outside the repository that holds a copy of the scope diff, the
  verdict schema, and a dispatcher-written agent file named
  `llm-handoff-security-<random hex>` with
  `tools: [view_file, list_dir, grep_search, find_by_name, finish]` and
  `commandExecutionPolicy: off`. If the system temp directory (`TMPDIR`,
  `TEMP`, or `TMP`) resolves inside the repository, the review fails closed
  before writing any file. `--model` is added only when the first
  `antigravity` role sets `model`; the unsafe-bypass argument is never added.
  Only `user_input`, `agent_response`, `finish`, and allowed-tool steps and
  exactly one `SUCCESS` result are accepted, the verdict comes only from
  `structured_output`, and error text never quotes `agy` output. The
  user-wide `agy` `permissions.allow` grants still apply, and the step checks
  run only after `agy` exits. `audit.security_model` is now rejected for
  every provider other than `claude`. The antigravity reviewer has not yet
  passed an operator-run live check.
- Breaking default change: interpreter grants are now an operator opt-in.
  `CLAUDE_DEFAULT_ALLOWED_TOOLS` no longer holds `Bash(python:*)`,
  `Bash(python3:*)`, or `Bash(pytest:*)`, and the generated `agy` allow
  entries (`ANTIGRAVITY_REQUIRED_ALLOW`, the list `init` prints, and the list
  `doctor` checks) no longer hold the `python`, `python3`, or `pytest`
  grants or a grant for a `verification_allowed_prefixes` entry that starts
  with one of them. A new top-level `interpreter_grants: true` restores the
  previous default lists exactly. Any interpreter grant lets an agent run any
  command, `git push` included, past the allowlist, so role allowlists are a
  guardrail, not a sandbox; the docs now recommend server-side branch
  protection. A role's own `allowed_tools` list, `unsafe_bypass` launches,
  Codex launches, and the work-claim gate (which runs the Verification
  command itself) are unchanged.
  - `antigravity_allowed_commands()` and `antigravity_allow_entries()` take a
    required keyword-only `interpreter_grants` argument; the new constants
    are `CLAUDE_INTERPRETER_ALLOWED_TOOLS` and
    `ANTIGRAVITY_INTERPRETER_COMMANDS`.
  - `llm-handoff init --verification-command` with a `python`, `python3`, or
    `pytest` executable adds no rule and writes no `allowed_tools` list, and
    prints one line explaining the opt-in. `init` never writes
    `interpreter_grants`.
  - `llm-handoff doctor` reports `interpreter grants` as PASS for the default
    lists with the key off, names `interpreter_grants: true` as the cause in
    each warning about a grant the key adds, and warns (with the key off)
    about any `agy` settings `permissions.allow` entry that still grants an
    interpreter, since that grant is user-wide.
- `normalizer.provider: antigravity` is now a registered next-agent
  normalizer (default model `gemini-3.8-flash-low`; the default provider
  stays `claude`). Each call runs `agy` with `--output-format stream-json`
  and a `--json-schema` file from a new private temp directory that holds a
  one-off agent file named `llm-handoff-normalizer-<random hex>` with
  `tools: [finish]` and `commandExecutionPolicy: off`. The raw `next_agent`
  value goes only in the agent file body, never on the command line. Only
  `user_input`, `agent_response`, and `finish` steps and exactly one
  `SUCCESS` result are accepted, and the answer comes only from
  `structured_output`, never `response`; anything else, including any `tool`
  step, fails closed and leaves the handoff unchanged. `llm-handoff doctor`
  checks for the `agy` binary for this provider. `antigravity` still cannot
  be the `audit.security_provider`.
  - A sign-in failure or permission auto-deny from the `antigravity`
    normalizer now raises fixed error text and never copies `agy`'s stderr,
    which can carry tokens or command arguments.
  - `llm-handoff doctor` prints a `normalizer boundary` WARN line when the
    normalizer provider is `antigravity`: the only pre-execution boundary is
    agy's enforcement of `tools: [finish]`, the user-wide
    `permissions.allow` grants still apply, and the post-run checks cannot
    undo a command that already ran.
- An `agy` deny-rule refusal line no longer includes the refused command line
  or `agy`'s error message, which repeats the command; best-effort redaction
  could not cover every secret shape, such as
  `-c http.extraHeader="X-Api-Key: value"`. The stderr line and the `ERROR`
  log line now read `Antigravity deny rule refused a command at step <N>`
  followed by `(matched deny rule <entry>)` when the error message names one
  of the required deny entries, or `(matched a user-configured deny rule)`
  otherwise; `<N>` is `unknown` when the step has no integer index.
  `redact_credentials()` is removed. The marker, the nonzero exit code, and
  the no-fresh-rerun behavior on resume are unchanged.
- `llm-handoff doctor` no longer decides whether a regex `agy` allow entry is
  push-safe only by testing three example `git push` lines. A regex allow
  entry that equals a generated required grant passes unless its command can
  push (`git` alone, `git` followed by an option such as `-c x=y`, or a
  command with a `push` word); any other regex allow entry whose pattern
  names `git` now FAILs because the doctor cannot verify it, so entries such
  as `command(regex:^git push --force$)` and `command(regex:^git status$)`
  fail. Patterns that do not name `git` keep the example-line check as a
  backstop. Each allow entry yields at most one push FAIL. No grant or default
  grant list changed.
- The work-claim gate now matches Verification commands against
  `verification_allowed_prefixes` literally instead of word by word. After
  surrounding spaces and tabs are stripped, a command must equal a prefix or
  start with a prefix followed by one space, and a command containing a tab,
  other non-space whitespace, or a run of two or more spaces fails
  `verification_not_allowed` with a `whitespace` reason. This is the rule the
  `agy` grant `command(regex:^<prefix>( .*)?$)` applies, so the gate no longer
  allows a command such as `python  -m pytest` that the grant would refuse.
  Config load rejects a prefix without single spaces between words, and
  `llm-handoff init --verification-command` rejects such a command. No grant
  or default grant list changed.
- `llm-handoff doctor` now reports `interpreter grants` lines per role or
  fallback launch: a WARN for each Claude launch whose effective
  `allowed_tools` list has bare `Bash`, `Bash(*)`, or a `python`, `python3`,
  or `pytest` rule; a WARN for each Antigravity launch naming the required
  user-wide `agy` allow entries for those interpreters (including ones built
  from `verification_allowed_prefixes`); and a WARN for each `unsafe_bypass:
  true` launch, which has no shell allowlist at all. An interpreter can run
  any command, including `git push`, past the allowlist. When none apply and a
  Claude or Antigravity launch exists, one PASS line is printed that names
  the interpreters checked; other interpreter rules such as `Bash(node:*)`
  are not detected and need a manual review. These lines
  are warnings only and do not change the doctor's exit code; no default grant
  list changed.
- An `agy` deny-rule refusal now fails the dispatch. `agy` reports a refused
  command as a `run_command` tool error inside a `SUCCESS` result with exit
  code 0, so a role turn could end normally after a `git push` was refused.
  The adapter now records each tool step in `ERROR` state whose message
  contains `Matches user-configured deny rule` (once per step), sets a
  nonzero exit code even when the role recovers, and adds one stderr line and
  one `ERROR` log line per refusal, starting with the new
  `ANTIGRAVITY_DENY_RULE_MARKER` and naming the command line and message.
  Both fields pass through the new `redact_credentials()` first, which
  replaces URL userinfo, authorization headers, bearer tokens, secret-named
  `NAME=value` pairs and `--flag value` arguments, and common token prefixes
  with `[REDACTED]`, so a refused `git push https://user:TOKEN@host/...` does
  not write the token to the console or dispatch log. A resumed run that fails this way is not rerun in a fresh conversation. Other
  tool errors, and commands that run and fail on their own, still pass.
- The post-dispatch gate now fails a handoff rewrite that silently drops
  planned stories. It compares the new `remaining_stories` with the handoff
  the dispatch started from, matching stories by their leading ID token (a
  token with a digit; title-only entries match by full text, and duplicate
  keys are counted). An entry may leave the list only when it is the prior
  or the new `story_id`, or when the rewrite lists it under
  `## Dropped Stories` with a reason; otherwise validation fails with
  `remaining_stories_dropped`, naming the entries. The rule applies only
  within one epic. The failure is repairable through the existing producer
  repair, and planner recovery after it is checked against the dropped list,
  not the rewrite that lost it. A new `remaining_story_not_in_project_state`
  warning names story IDs, or titles, that the configured
  `project_state_path` does not list (skipped in Linear mode).
  The handbook, the planner and auditor prompts, and their agent files now
  say to copy the list forward and to record new stories in
  `PROJECT_STATE.md` through the next Work Packet. The finalizer prompt adds
  any listed story the project state file is missing. Before this, an auditor
  rewrite without `remaining_stories` lost a story that existed only in the
  handoff, and the next planner rebuilt the list without it.
- The `agy` permission grants now cover arguments. The exact-match
  `command(<cmd>)` allow entries refused argument-bearing commands such as
  `git rev-parse HEAD` and `git commit -m ...`; each allow entry is now an
  anchored regex grant, `command(regex:^<cmd>( .*)?$)`, built by the new
  `antigravity_command_grant()` from the new `ANTIGRAVITY_ALLOWED_COMMANDS`
  list (verification commands get the same form, with regex metacharacters
  escaped). The `git push` deny entries are now
  `command(regex:^git( .*)? push( .*)?$)` and
  `unsandboxed(regex:^git( .*)? push( .*)?$)`, which also refuse
  option-prefixed pushes such as `git -C . push`, and any other `git` command
  line containing ` push`. `llm-handoff init` prints the new entries.
  `llm-handoff doctor` tests deny and ask rules against the plain commands
  (from the new `antigravity_allowed_commands()`), fails on a regex allow
  entry that matches a `git push` command line, says that legacy exact grants
  do not cover arguments, and prints regex backslashes without doubling them.
  `CONFIGURATION.md` lists the entries and the live `agy` 1.4.2 results
  behind them, the deny breadth, and that `python( .*)?` allows
  `python -c`. The `python` and
  `python3` grants still let a role run `git push` from a subprocess, the
  same known gap as Claude's `Bash(python:*)`, tracked as GEM-0e.
- Every shipped `agy` agent file under `.agents/agents/` (and its copy in
  `examples/reference-workflow/`) now declares a `tools:` list. Without one,
  `agy` gives the agent only read tools and it cannot write files. The role
  files list `view_file`, `list_dir`, `grep_search`, `find_by_name`,
  `write_to_file`, `replace_file_content`, `multi_replace_file_content`, and
  `run_command`; the read-only `handoff-validator` file leaves out the three
  write tools. The lists come from the new `ANTIGRAVITY_AGENT_TOOLS` and
  `ANTIGRAVITY_AGENT_WRITE_TOOLS` constants in
  `llm_handoff/antigravity_permissions.py`, and tests check every file
  against them. The `handoff-validator` file keeps `run_command` for its git
  checks, its prompt limits it to read-only git commands, and the docs state
  that this limit is a guardrail, not a shell boundary.
- `pr` mode no longer strands a pull request whose review finding was
  deferred. The review bounce now says that deferring every finding is still
  a `ready_for_review` handback with no code change (the work-claim gate
  counts the story's earlier commits), and that a `blocked_*` handback skips
  the gate, the audit, the reply, and the planner's pass. A blocked
  handback that answers every open finding with `Deferred:` and changes no
  story file now counts as a deferral. A planner handoff with
  `## Deferred Finding Decisions` written outside that pass used to be
  ignored silently. Now it is acted on when every unresolved finding has a
  decision and the pull request head is the commit Codex reviewed (plus
  only clean base-branch merges); otherwise the dispatcher pauses and keeps
  the story state. Before each non-planner cycle, a handoff that moves on
  from a saved story requires that story's pull request to be merged:
  - a merged story is cleaned up;
  - an open one on its reviewed head gets one planner pass, with the moved-on
    handoff saved to `logs/dispatch/publish/displaced-handoff.md` for the
    planner to restore;
  - anything else, including a `gh` failure, pauses naming the pull request
    and the recovery steps.
- Behavior change: in `pr` mode, saved `## Review Responses` now last for the
  whole correction round. A later claim in the round merges its responses
  into the saved ones by finding number, and a claim without responses keeps
  them. Previously a claim without responses, such as the implementer's
  handback after an audit bounce, deleted the saved ones, so the fix's
  answer was never posted and the merge stopped. Saved responses are dropped
  when a new correction round starts. A saved Codex finding left without a
  response now goes to the planner's one deferred-findings pass, with its
  thread still open and the story state kept, when it and deferred findings
  are the only unresolved threads. Previously the dispatcher paused and
  cleared the story state, so a restart could not recover the open pull
  request. A person's comment or any other unresolved thread still pauses.

## Pre-release snapshot - 2026-10-19

Condensed history from the start of the extraction (2026-04-20) through
2026-10-19. Each entry states the behavior at the snapshot date; features
that were added and later replaced or removed in that window appear in their
final form. Mechanism detail lives in the pull requests, `PROJECT_STATE.md`,
and `CONFIGURATION.md`.

### Added

- Extraction scaffold: the `llm_handoff` module, public README and repository
  instructions, the documentation split, generic handoff prompts and agent
  templates, and the ported dispatcher tests as the full-suite gate.
- Packaging: `pyproject.toml` (hatchling) installs from a Git checkout with
  `pip install .` and adds the `llm-handoff` console command. The wheel
  bundles `examples/reference-workflow/`. Not published to PyPI.
- `llm-handoff init` copies the reference workflow into a target repository,
  keeps user content through managed sections, merges Claude settings, and
  refuses linked or hard-linked destinations, even with `--force`.
- `init --provider claude|codex|antigravity` puts every role on one CLI; with
  no `--provider`, a machine with only one provider CLI gets that mapping. The
  reference workflow ships Claude, Codex, and `agy` helper files for each role.
- `init` lists candidate build and test commands it finds (CI, `package.json`,
  `pyproject.toml`, `Makefile`, Cargo, Go) without running them;
  `--verification-command` confirms one into `verification_allowed_prefixes`.
- `init --tracker linear --tracker-team <key>` with `--tracker-project` or
  `--tracker-label` sets up a fresh repository in Linear mode: a validated
  `tracker` block plus Linear blocks in `AGENTS.md` and the handbook.
- `llm-handoff doctor` checks a checkout before a run, one `PASS`, `FAIL`, or
  `WARN` line per check, exits 1 on any error, and writes no files. The
  supervisor's `--doctor` mode shares its checks.
- Doctor checks cover Git history, provider binaries, the handoff file, the
  normalizer, scanners, the work-claim gate and Verification allowlist,
  interpreter grants per launch, `agy` settings, and Linear tracker connectors.
- `llm-handoff doctor --probe-tracker <ISSUE>` (Linear mode, opt-in) launches
  one locked-down Claude role to check that the issue and the configured
  workflow states can be read.
- `llm-handoff stop` asks a running dispatcher to finish the cycle in flight
  and exit 0. It refuses when no live dispatcher holds `.dispatch.lock` or
  Git does not ignore `logs/dispatch/`. Ctrl+C still aborts at once.
- Operator prompt at pauses (`interactive_pause`, on by default): at a TTY,
  Enter, free-text planner guidance, `retry`, `quit`, or `@backend`,
  `@frontend`, or `@auditor` with an optional message.
- `pause_recovery` (`planner` by default, or `"off"`): the six non-user pauses
  get one bounded planner recovery that carries the pause reason and the
  validator's diagnosis. A `user` route still waits.
- Deterministic work-claim gate (`enforce_work_packet`) checks
  `ready_for_review` claims against the Work Packet before the auditor runs; a
  pass writes `## Dispatcher Verification` so reviewers do not re-run it.
- Work-claim `scanners` (gitleaks, semgrep, bandit, pip-audit, or any tool)
  with diff-scoped placeholders, per-scanner timeouts, and `required`.
- `verification_allowed_prefixes`: the gate runs only Verification commands
  that match a listed prefix and contain no shell metacharacters; others fail
  `verification_not_allowed`. An empty list logs `WARN` at every start.
- `commit_mode: dispatcher`: implementers leave changes uncommitted and the
  dispatcher commits only in-scope files, recording `scope_sha`. Requires the
  work-claim gate.
- In dispatcher commit mode, uncommitted implementer work carries into the
  next dispatch for the same story, and the record is saved to
  `logs/dispatch/carry-over.json` so a restart keeps it.
- `publish.mode` (`local`, `merge-commit`, `pr`): branch modes put each story
  on an `llm-handoff/<story>` branch and merge it with a report commit or a
  pull request. Agents never branch, push, or merge. Default `local`.
- `publish.wait_for_reviewers: [codex]` (`pr` mode with `auto_merge`) waits
  for Codex's GitHub review of the pushed commit; findings bounce to the
  implementer for one correction round (`max_review_rounds: 1`).
- Review responses: each `## Review Responses` entry is posted as the reply on
  its review thread and the thread is resolved. `pr` mode auto-merges only
  when every thread is resolved.
- Deferred findings: a `Deferred:` response keeps its thread open, and a pull
  request held only by deferred findings gets one planner pass to widen the
  packet, accept them (`## Deferred Finding Decisions`), or route to `user`.
- Audit fan-out (`audit.reviewers: [quality, security]`): read-only quality
  and security reviewers run in parallel on the story range. Each cycle writes
  `usage.json` with per-reviewer tokens and cost.
- `audit.security_model` sets the Claude security reviewer's model and is
  rejected for other providers. The `Audit reviewers:` start line names the
  model and its source.
- Role `fallback`: a timeout, rate or usage limit, sign-in failure, or launch
  failure reruns the dispatch once on the fallback. `idle_timeout_ms` (off by
  default) kills an agent that prints nothing for that long.
- `protected_paths`: a dispatch that changes control files (`AGENTS.md`,
  handoff prompts, dispatch config, provider agent directories) outside its
  Work Packet routes to `user` with `protected_file_modified:`.
- `antigravity` provider (binary `agy`): print mode with stream-json output,
  `--print-timeout`, optional `--model`, and `--conversation` resume. It gets
  no provider environment variables or API key.
- The `antigravity` adapter fails a run whose stderr reports a print-mode
  permission auto-deny, naming the `permissions.allow` entry to add, and
  treats `authentication required` as a sign-in failure.
- `agy` agent files under `.agents/agents/` for every role and for
  `supervisor-repair`, each with a `tools:` list (`handoff-validator` gets no
  write tools). `.agents/**` is a default protected path.
- `agy` permission entries (`llm_handoff/antigravity_permissions.py`): `init`
  prints them for `~/.gemini/antigravity-cli/settings.json`, and `doctor`
  checks that file. Neither command writes it.
- `audit.security_provider: antigravity`, a read-only `agy` security reviewer.
  The automatic order is `claude`, `codex`, `antigravity`. It passed an
  operator live check on 2026-10-18 with `agy` 1.4.2.
- Next-agent normalizers `antigravity` (default model `gemini-3.8-flash-low`)
  and `codex` (`codex exec --sandbox read-only`, CLI login, no API key). Both
  fail closed on unusable output; the default stays `claude`.
- Linear mode (`tracker.provider: linear`, off by default): a validated
  `tracker` block, handoff lease fields (`tracker_issue`, `tracker_url`,
  `selection_basis`) kept in story state, and shipped Linear prompt templates.
- Linear-mode Claude launches get the role's Linear MCP tools, and
  `tracker.claude_mcp_config` passes `--mcp-config <path> --strict-mcp-config`.
  Without it, startup warns that every user MCP server loads.
- Linear-mode prompts state the planner selection rules, the issue-state
  transitions and evidence comments, and a read-only validator evidence check.
  `ISSUE_TEMPLATE.md` gives an example issue. Issue text is untrusted data.
- `handoff-validator` helpers report a `LINEAR-EVIDENCE` row; in Linear mode a
  failed, missing, or unparsed row blocks automatic planner pause recovery.
- `docs/TESTING.md` `## Linear Mode Acceptance Run`: a manual operator
  runbook against a sandbox Linear team. The test suite does not run it.
- `remaining_stories_dropped`: validation fails a rewrite that drops planned
  stories not listed under `## Dropped Stories` with a reason. A
  `remaining_story_not_in_project_state` warning flags unknown stories.
- An audit dispatched with `@auditor <message>` records the acceptance time
  and the message's SHA-256 (never its text) in `review.json`, the audit
  handoff, and the pull request body.
- Integration tests (`tests/integration/`, marker `integration`) install the
  wheel offline and run `init`, `doctor`, and `--dry-run` on throwaway Node
  and Go repositories.

### Changed

- Behavior change: the work-claim gate is on by default. Opting out takes
  `enforce_work_packet: false` plus `work_packet_opt_out_reason`, and logs a
  `WARN`. Planner assignments now need a parseable `## Work Packet`.
- Behavior change: the security reviewer runs by default. Opting out takes
  `audit.reviewers: [quality]` plus `audit.security_opt_out_reason`. Startup
  fails with `security_reviewer_failed:` when the reviewer CLI is missing.
- With `audit.security_provider` unset, the reviewer is the first installed
  CLI that differs from the implementer's provider, else the implementer's
  own vendor with a `WARN` that the review is not independent.
- Behavior change: claims are checked against Git. `ready_for_review` needs
  `scope_sha`, an unknown `Commit SHA verified` fails `evidence_sha_unknown:`,
  and the gate adds `evidence_sha_mismatch` and `no_changes` in both modes.
- Behavior change: `planner` and `frontend` default to `antigravity`
  (`binary: agy`). A config with no `agents:` block needs `agy`; a Linear
  config that omits either role fails to load, so map both to claude or codex.
- Behavior change: `init` provider auto-detect looks for `claude`, `codex`,
  and `agy`; a `gemini`-only machine gets the reference mapping.
  `init --tracker linear` refuses the all-Antigravity mapping.
- Behavior change: the default supervised repair scope is `docs/`,
  `.claude/agents/`, `.codex/skills/llm-handoff/`, and `.agents/agents/`.
  Other paths need `--repair-allow-path`.
- Behavior change: a security `fail` whose findings are all below
  `audit.block_severity` passes the audit, listing them in `## Security Notes`.
  A quality `fail`, or a security `fail` with no findings, still blocks.
- Behavior change: a failed Codex review gets one `@codex review` retry (none
  after a usage limit). If that fails or times out, a terminal prompt offers
  `merge`, `retry`, or `quit`; without a terminal the loop stops.
- Linear mode rejects `provider: antigravity` on any role or fallback at
  config load (`agy` agent files give roles no Linear MCP access), and
  `init --tracker linear` maps `planner` and `frontend` to `claude`.
- In Linear mode, a missing or empty `HANDOFF.md` dispatches one planner
  lease rebuild (`tracker_lease_rebuild`) instead of exiting 1; a second empty
  read pauses.
- Default Claude models: `claude-opus-5-5` for roles and `claude-haiku-5-5`
  for the normalizer. Explicit `model` values are unchanged.
- `pr` mode merges the base branch into the story branch before each push, and
  once more if `gh pr merge` reports it not mergeable. A conflict pauses with
  the files named and the story state kept.
- A resumed `pr` publish pushes only when every commit added since the pause
  is a clean merge of the base branch (checked with `git merge-tree`).
  Anything else pauses and pushes nothing.
- Resume pause messages explain merge parent order and name the conflicted
  files a merge changed. `CONFIGURATION.md` explains that merge drivers such as
  `union` run during the resume check and can make it pause.
- A handoff that moves on from a saved story needs that story's pull request
  merged; an open one on its reviewed head gets one planner pass, and anything
  else pauses. This replaces the `Story branch not ready` exit.
- The pull request and merge-commit report is a short page: a Summary of up to
  three sentences and Reviewer notes (`### Story`, `### Acceptance checks`,
  `### Audit`). Changes, Test plan, Work Packet, and token usage are gone.
- The audit range starts at the story's first implementer dispatch, and in
  branch publish modes the audit and gate base survive a restart through
  `logs/dispatch/publish/story.json`.
- An audit whose range has no committed changes, such as an epic close, runs
  the Work Packet Verification first. A failure, or a blocked audit on such a
  range, routes `verified_fail` to `planner`.
- A handoff whose `status` contradicts its `next_agent` fails validation
  (`frontmatter_status_route_conflict`); prompts pair each status with its
  route.
- A post-dispatch validation failure logs an `ERROR` naming the errors and
  pauses instead of exiting 1 silently. Claims the gate has not checked never
  get automatic planner recovery.
- `EVIDENCE_MISSING` errors, including `Commit SHA verified: HEAD`, go
  through the one-shot producer hygiene repair instead of stopping the run.
- Verification Evidence fields accept indented continuation lines and a
  parenthetical note in the label.
- An audit reviewer that fails to start stops the run after one attempt with
  an `audit_reviewer_start_failed:` line. A missing verdict is never a pass.
- A provider sign-in failure stops the run at once with that CLI's sign-in
  steps instead of retrying, unless the role's `fallback` takes over.
- The console shows the work-claim gate's `work-claim:` progress lines;
  command output tails stay in the log.
- Token usage and cost moved from the console to `DEBUG` `usage:` log lines.
- A `.dispatch.lock` refusal names the lock's pid, its start time, and whether
  it is running. The lock is never removed automatically.
- Claude roles are told their shell allowlist up front; a refused command
  logs a one-line `WARN`.
- The dispatcher pauses before sending a Claude `backend` or `frontend` in
  `dontAsk` mode a Work Packet that scopes a `.claude` path Claude Code would
  refuse to write.
- In dispatcher commit mode, planner and finalizer commits that hold only the
  handoff are undone with `git reset --soft`; other planner commits or branch
  changes stop the loop.
- Prompts: planners route a finished epic to the auditor with
  `close_type: epic`, the auditor routes an approved last story to
  `finalizer`, and implementers keep the assignment's story fields unchanged.
- Planner prompts ask for one Verification command per list item with no
  prose, and never a temp directory inside the repository.
- The finalizer prompt and helpers name the configured `project_state_path`.
- Documentation: the README is organized around getting started, the
  repository map moved to `docs/INVENTORY.md`, each first-run guide has one
  job, and copied files no longer link to llm-handoff's own docs.
- The test suite runs on 8 `pytest-xdist` workers, and an autouse fixture
  fails any test that launches a real provider CLI.
- Package source may name the Antigravity (`agy`) CLI; `antigravity` is still
  rejected as a `next_agent` value.
- Removed `auto_push`, which was never read. `auto_push: false` is ignored;
  any other value fails at load and points to `publish.mode`.
- Removed Gemini CLI support (it no longer serves personal Google accounts):
  the `gemini` provider, security reviewer, normalizer, `init` mapping, and
  `.gemini/` files. A `gemini` value fails at load, pointing to `antigravity`.
- With the Gemini removal, `tenacity` and `google-genai` are dropped, and
  `doctor` no longer checks a container runtime. `GEMINI_API_KEY` and
  `GOOGLE_API_KEY` are still stripped from every launch.
- Behavior change: `planner_api_key_env` and `--use-planner-api-key-env` are
  removed. `planner_api_key_env: false` is ignored, any other value fails to
  load, and the flag exits 2 before any config loads.

### Fixed

- Prose in a Work Packet no longer breaks the gate: a prose-only or
  `:`-ending Verification line is `work_packet_unparseable`, and a repeat
  failure of the same command bounces to the planner.
- A path in both `Files in scope` and `Files out of bounds` is
  `work_packet_unparseable` instead of looping the implementer; a new change
  to a disallowed file is a `scope_violation` to revert.
- Path lists written as prose no longer create stray out-of-bounds entries.
  The narrowest matching entry decides, in the gate and the protected-file
  check.
- Branch publish modes refresh the saved Work Packet on each implementer
  dispatch, so a later planner revision is no longer lost.
- `pr` mode saves `## Review Responses` from the handoff it sends to the
  auditor, so a hand-fixed correction round no longer loses them.
- A resumed `pr` publish that conflicts again at the merge step keeps the
  merge-step marker and says the pull request was left open.
- A refused base-branch merge names git's `error:` or `fatal:` line and any
  blocking local files. The dispatcher never stashes, discards, or commits them.
- A start on a story branch whose pull request already merged fast-forwards
  and switches to the base branch when it is safe, instead of stopping after a
  planner cycle. Nothing is pushed.
- A passing epic-close audit with no story branch, or with nothing beyond the
  remote base, publishes nothing and continues to the finalizer.
- A restart that routes straight to the auditor, or a correction round with no
  new commit, now reviews the whole story instead of one commit or nothing.
- An operator `@backend` or `@frontend` on a handoff with no `story_id`
  continues the saved story; a branch that is not ready pauses again.
- Audit verdict JSON with raw newlines or tabs inside strings parses instead
  of failing `quality_verdict_malformed`.
- `doctor` and the supervisor preflight accept the finalizer's `verified_pass`
  epic-close handoff instead of reporting `EVIDENCE_MISSING`.
- `init --provider codex` writes `commit_mode: dispatcher`, because Codex's
  sandbox keeps `.git` read-only and agent mode stalled.
- On Windows, npm-installed CLI shims launch through their `node <entry>.js`
  target, so arguments with `"`, `<`, `>`, `|`, `&`, `%`, or `^` arrive intact.
- Codex launches resolve the output schema from the installed package, and a
  Codex validator or finalizer report is read from its JSON `summary`.
- The security reviewer's verdict schema no longer has a `$schema` key, which
  Claude Code's `--json-schema` rejected.
- The one-shot handoff repair reads `git status` correctly and leaves the
  handoff uncommitted in dispatcher commit mode.
- Recording the commit SHA replaces only the `pending-dispatcher-commit`
  marker, and a blocked handback with the marker gets a `sha_pending` warning
  instead of a `sha_missing` error.
- A Work Packet field written as nested bullets no longer warns that it is
  empty, and an empty Evidence field no longer absorbs the next one.
- A scheduled planner recovery dropped because `HANDOFF.md` changed logs a
  `WARN`.
- Agent launches keep `USERNAME`, `USER`, and `LOGNAME`, so pytest no longer
  uses a shared `pytest-of-unknown` temp directory.
- Supervised child imports are isolated from the target working directory.
- Supervised repair with an `antigravity` role launches the shipped
  `supervisor-repair` agent and ignores an `agent_name` override.
- Codex usage-limit stops name the limit; the pull request is left open.
- Wording and ASCII fixes in the config templates, the finalizer skill,
  `CONFIGURATION.md`, and `tests/test_cli.py`.

### Security

- Provider CLIs launch with an allowlisted environment. `env_passthrough` adds
  names, `env_mode: inherit` restores the copy, and Codex roles need
  `use_api_key_env: true` for `OPENAI_API_KEY`. Security reviewers too.
- Behavior change: the gate's Verification command and scanners also run with
  an allowlisted environment; vendor API keys never reach them. A command
  that needs an inherited variable must list it in `work_claim_env_passthrough`.
- Safer launch flags: Claude Code runs `--permission-mode dontAsk` with an
  `allowed_tools` list and web tools denied; Codex runs `workspace-write`.
  `unsafe_bypass: true` restores the old flags with a startup warning.
- The reference workflow ships `.claude/settings.json` deny rules with a
  `PreToolUse` hook, and `.codex/rules/llm-handoff.rules`, which forbids
  `git push`. `SECURITY.md` has a Threat Model section.
- Breaking default change: interpreter grants are an opt-in. Default Claude
  and `agy` grants omit `python`, `python3`, and `pytest`, and
  `interpreter_grants: true` restores them. Role allowlists are a guardrail.
- `agy` allow entries are anchored regex grants that cover arguments, and the
  deny entries refuse any `git` command line with ` push`. `doctor` fails a
  regex allow entry naming `git` that it cannot prove push-safe.
- An `agy` deny-rule refusal fails the dispatch even when the role recovers.
  The log line names the step and the matched rule, never the command line.
- The work-claim gate matches Verification commands literally (single spaces
  only), the same rule the `agy` grant applies.
- Provider stderr is redacted (`llm_handoff/redaction.py`) before it reaches
  the log or a reviewer error. Raw stderr files are unchanged.
- The `antigravity` normalizer and security reviewer run from private temp
  directories with fixed tool lists and `commandExecutionPolicy: off`, accept
  only `structured_output`, and never echo `agy` output in errors.
- Console lines escape terminal control and format characters before
  printing; the log keeps the exact text.
- Operator-prompt instructions quote the pause reason and operator message
  between fixed markers that the quoted text cannot close.
- Behavior change: config load rejects a `handoff_path` or
  `project_state_path` that leaves the repository (absolute, UNC, `..`, the
  root itself, or a symlink escape). These were accepted before.
- Behavior change: a carry-over record loaded at startup applies only after a
  `Carry these over as <story>'s work? [y/N]` prompt at a terminal. Any other
  answer discards and deletes it with a `WARN`; a dry run keeps the file.
- Behavior change: the dispatcher no longer follows a symlink or junction at
  or above the carry-over record. It writes and deletes the record through
  pinned directory handles, or keeps it in memory where it cannot.

## 0.0.0

- No public release tag has been published yet.
