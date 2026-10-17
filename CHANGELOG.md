# Changelog

All notable changes to `llm-handoff` will be documented here.

This project follows a simple pre-release changelog until the first public tag.

## Unreleased

- Handoff frontmatter accepts three tracker lease fields: `tracker_issue`,
  `tracker_url`, and `selection_basis` (`priority`, `manual_order`, or
  `created_at`). With `tracker.provider: linear`, handoffs routed to
  `backend`, `frontend`, or `auditor` must carry `tracker_issue` (equal to
  `story_id`) and `tracker_url`, planner assignments must carry
  `selection_basis`, and the dispatcher logs a WARN when the planner picked an
  issue without a priority. Repair prompts preserve the fields, and the audit
  fan-out handoff carries them forward. With `provider: none` the fields are
  accepted with a single `tracker_fields_ignored` warning.
- `dispatch_config.yaml` accepts an optional top-level `tracker` block for
  Linear mode, validated at load. It defaults to `provider: none`, which
  changes nothing and allows no other tracker key. `provider: linear`
  requires `team`, `eligible_state`, `in_progress_state`, `success_state`,
  `mcp_server`, exactly one of `project` / `label`, and
  `enforce_work_packet: true`; an optional `claude_mcp_config` must name an
  existing file. Nothing reads the block yet. The example and template
  configs carry it commented out. See `CONFIGURATION.md`.
- The shipped finalizer helper files and the stale-finalizer recovery
  instruction now defer to the configured `project_state_path` instead of
  always naming `PROJECT_STATE.md`.
- The dispatcher now pauses before dispatching `backend` or `frontend` when
  the role is `provider: claude` in the default `dontAsk` mode and the Work
  Packet's `Files in scope` names a path under a `.claude` directory, at the
  root or nested. Claude Code's documented writable exceptions under the
  root `.claude` folder (`.claude/worktrees/`, and Markdown files under
  `.claude/agent-memory/` or `.claude/agent-memory-local/`) do not pause.
  Claude Code denies the other writes in `dontAsk` mode, so the
  role could only hand back `blocked_missing_context`. The pause reason names
  the entries and says to apply the edits by hand or move them to a role with
  another provider; `--dry-run` logs it. The planner prompts and helper files
  now say not to scope `.claude/` paths to a Claude role.
- The supervisor's default repair allowlist is now `docs/` plus the provider
  helper folders `.claude/agents/`, `.codex/skills/llm-handoff/`, and
  `.gemini/agents/`. Dispatcher source, tests, top-level docs, and scripts
  are no longer allowed by default; pass `--repair-allow-path` for any other
  path a repair may change.
- The finalizer prompt now names the configured `project_state_path` when it
  tells the finalizer which file to update and commit, instead of always
  `PROJECT_STATE.md`. Its `COMMIT SHA` line now asks for the sha of the
  project state commit and no longer names `HANDOFF.md`.
- The `auto_push` config key has been removed; the dispatcher never read it.
  A config with `auto_push: false` still loads and the key is ignored.
  `auto_push: true`, or any value other than `false`, now fails at load with
  an error that says to delete the key and points to `publish.mode`, which
  controls whether the dispatcher pushes. `dispatch_config.example.yaml` and
  the reference workflow config no longer set it.
- `llm-handoff doctor` now reports configured scanners. With none
  configured it prints one `PASS` line. With scanners configured and the
  work-claim gate off it prints one `WARN` line saying none of them run.
  With the gate on it prints one line per scanner: `PASS` when the leading
  executable is on `PATH`, `FAIL` when it is missing and `required: true`
  (every claim the scanner applies to would fail with
  `scanner_missing:<name>`), and `WARN` when it is missing and
  `required: false` or when the command does not start with a plain program
  name. For a command that uses `{changed_files}` or `{changed_py_files}`,
  the `PASS` and `FAIL` messages say they apply only to claims where that
  list is nonempty, since the gate skips the scanner otherwise. Doctor uses the gate's executable lookup and never runs
  a scanner command. `dispatch_config.example.yaml`, the reference workflow
  config, `CONFIGURATION.md`, and `README.md` now say that a scanner runs
  only when it is listed and its executable is installed, recommend bandit,
  pip-audit, and gitleaks for Python repositories, and show semgrep with a
  pinned local ruleset. The example config's active `semgrep --config auto`
  entry is now a commented-out entry with a local ruleset path.
- The operator prompt now accepts `@backend`, `@frontend`, or `@auditor`
  with an optional message. The dispatcher runs that role directly on the
  current `HANDOFF.md`, with the pause reason and the operator's message as
  its instruction. The targeted cycle skips the stale-route check; the
  circuit breaker, work-claim gate, commits, and post-dispatch validation
  still apply. `@planner <message>` is planner guidance, and other targets
  are rejected with the list of accepted ones. On a terminal with no console
  input adapter, the prompt now logs a `WARN` line and waits for a
  `HANDOFF.md` edit instead of reading stdin on a thread.
- New operator prompt at dispatcher pauses (`interactive_pause`, on by
  default; `--no-interactive-pause` turns it off for one run). When stdin is
  a TTY, outside `--dry-run`, and with `poll_interval_seconds` above 0, a
  pause that waits for a `HANDOFF.md` edit also accepts Enter (planner
  dispatch with a best-judgment instruction), free text (planner guidance),
  `retry`, or `quit`. The dispatcher now also logs a `WARN` line when a
  scheduled planner recovery is dropped because `HANDOFF.md` changed.
- New `pause_recovery` config (`planner` by default, or `"off"`). At the six
  non-user pauses in the run loop (unknown route, stale route, low
  confidence, unparseable finalizer output, missing route after dispatch, and
  self-loop) the dispatcher now schedules one bounded planner recovery that
  carries the pause reason and the validator's diagnosis, instead of waiting
  at once. Each pause streak gets one attempt; the streak resets after a
  normal dispatch passes validation. A `user` route still always waits.
  `"off"` keeps the old behavior. The startup banner shows the mode.
- Resume check pause messages say more. A merge refused because its second
  parent is not on the base branch now explains that parent order matters
  (run `git merge <remote>/<base_branch>` on the story branch). A merge that
  changes files beyond the conflict resolution now also names the conflicted
  files it changed, which the check does not inspect, and asks you to review
  them. Changed-path listings use their own limit of five paths.
  `CONFIGURATION.md` now explains that `git merge-tree` runs the repository's
  configured merge drivers during the resume check and that a merge driver
  such as `union` can make the resume pause.
- In `merge-commit` and `pr` publish modes, a passing epic-close audit
  (`close_type: epic`) with no story branch no longer stops the loop with
  "the audited work is not on a story branch". The epic's stories are already
  merged, so the dispatcher publishes nothing and continues to the finalizer.
  It still stops when the checkout is not the base branch (or is a branch with
  `branch_prefix` when `base_branch` is null), and in `pr` mode when the
  branch has commits that are not on the remote.

- An audit fan-out reviewer that fails to start (missing binary, CLI argument
  error, sign-in failure, missing Gemini sandbox or security extension) now
  stops the run with exit 1 after one attempt and logs its stderr in an
  `audit_reviewer_start_failed:` line, instead of retrying until
  `max_consecutive_failures`. Other reviewer errors still count toward
  `max_consecutive_failures`. Either way no audit verdict is written, so a
  missing verdict is never a pass.

- Each audit fan-out cycle now writes `usage.json` next to `review.json` in
  `logs/dispatch/audit/<timestamp>-cycle<N>/`, with each reviewer's provider,
  token counts, and cost, plus a `total_cost_usd`. Reviewers that report no
  usage show `null`; a retried run reports only its last attempt.

- Provider adapters now return per-run token and cost usage on their result
  objects (`usage` on `DispatchResult` and `SubagentResult`), as groundwork
  for audit usage files. The `usage:` log lines are unchanged.

- Added an optional `audit.security_model` setting. When set, the claude
  security reviewer launches with `--model <security_model>`; when unset, it
  still borrows the model of the first `claude` role, else the built-in
  default. The setting applies only to the claude reviewer: a config that
  sets it with `audit.security_provider: gemini` or `codex`, or without the
  security reviewer, fails to load, and an auto-chosen gemini or codex
  reviewer logs a `WARN` per audit that it was not applied. Every dispatcher
  start logs the security reviewer's model and its source in the
  `Audit reviewers:` line.

- Added one shared list of the `agy` permission entries Antigravity-mapped
  roles need (`llm_handoff/antigravity_permissions.py`): the default
  `command(...)` allow entries, `command(<cmd>)` per Verification command,
  and deny entries for `command(git push)` and `unsandboxed(git push)`. When
  `agy` is on `PATH`, `llm-handoff init` prints the list and asks you to add
  it to `~/.gemini/antigravity-cli/settings.json`; `agy` does not change the
  provider mapping. `llm-handoff doctor` reads that file when a role or
  fallback runs on Antigravity without `unsafe_bypass` and reports an
  `antigravity permissions` result: FAIL for a `git push` allow grant, WARN
  for a missing, unreadable, or malformed file, missing entries, or a
  `permissions.deny` or `permissions.ask` rule that overrides a required allow
  entry (Antigravity applies Deny > Ask > Allow), PASS otherwise. Neither command writes the settings file.

- Added Antigravity agent files under `.agents/agents/` for each `--agent`
  name the `antigravity` adapter passes: `planner`, `backend`, `frontend`,
  `auditor`, `handoff-validator`, `handoff-router`, and `ledger-updater`. They
  ship in this repository and in the `reference-workflow` template. Each body
  is adapted from the matching Claude agent; the frontmatter carries only
  `name` and `description`, with no `tools` list, and the shell section
  explains that print-mode `agy` runs only commands a `permissions.allow`
  grant covers. Loading through `agy -p --agent <name>` has not been checked
  against a live CLI. `.agents/**` is now a default protected path.

- The `antigravity` adapter now detects two `agy` failure modes. A run whose
  stderr reports that print mode auto-denied a tool permission it cannot
  prompt for fails the dispatch even when `agy` exits 0; the adapter logs one
  ERROR line naming the permission and the `permissions.allow` entry to add
  in `~/.gemini/antigravity-cli/settings.json`, and a soft-denied resume is
  not rerun fresh. A failed `agy` run whose stderr says `authentication
  required` is a sign-in failure, so an unsigned `agy` stops the loop with
  `agy` sign-in steps and a signed-out resume is not rerun fresh. Only the
  Antigravity adapter checks that phrase; other providers' output is not
  matched against it. Sign-in messages for a role without a `binary` name the
  provider's default CLI (`agy` for `antigravity`).

- Added the `antigravity` provider adapter (binary `agy`). A role with
  `provider: antigravity` launches `agy -p` with stream-json output, an
  explicit `--print-timeout`, optional `--model`, and `--conversation` resume
  (one fresh rerun if a resumed run fails). A non-`SUCCESS` result status or a
  missing result event fails the dispatch. The launch gets no provider
  environment variables and no API key, and passes no permission flag unless
  `unsafe_bypass: true` adds `--dangerously-skip-permissions`. It is a runtime
  adapter only, not a normalizer or security reviewer. Antigravity agent files
  and soft-deny and sign-in failure detection follow in later stories.

- Package source may now name the Antigravity (`agy`) CLI. The extraction-era
  scrub test banned the word because the source dispatcher hard-coded it as a
  manual GUI frontend step, which is now the generic `use_manual_frontend`.
  That reason does not cover a real provider adapter. `antigravity` stays in
  `LEGACY_PROVIDER_LABELS`, written plainly, so it is still rejected as a
  `next_agent` value like `claude`, `codex`, and `gemini`.

- The backend and frontend implementer prompts now say to keep `epic_id`,
  `story_id`, `story_title`, and `remaining_stories` from the assignment
  unchanged in the handback, and to write `remaining_stories: []` when the
  assignment has it. An implementer that rewrote the frontmatter from its
  template could drop the explicit empty list, so the auditor could not
  close the epic on its last story and routed it back to the planner.

- The planner prompts no longer offer `next_agent: finalizer`. The validator
  and router accept that route only from `producer: auditor`, so a planner
  that followed the old "approved epic closeout" line was rejected and the run
  paused. The planner now routes a finished epic to `auditor` with
  `close_type: epic` and a concrete `scope_sha`, and is told that a missing
  auditor handoff in Git history is not evidence of a missing audit (audit
  fan-out reviews live under `logs/dispatch/audit/`). The auditor prompts and
  the audit fan-out prompt now route an approved story that finishes its epic
  (an explicit `remaining_stories: []`, a handoff that names it the last
  story, or a planner-sent `close_type: epic`) straight to `finalizer` with
  `close_type: epic` instead of back to the planner to decide. A fan-out
  handoff now keeps an explicit `remaining_stories: []` instead of dropping
  it, so the marker survives a blocked audit and the implementer's retry.

- The work-claim gate no longer runs a Verification command line written as
  prose. A line such as `` `cmd1`, then `` or `` `cmd1` and `cmd2` `` now runs
  each backticked command. A line with other text around a backticked command,
  or with `or` between two of them, is reported as prose
  (`work_packet_unparseable`) and nothing runs. Before this, the gate ran the
  whole line, failed a correct implementation with `verification_failed`, and
  the packet had to go back to the planner. The planner prompts and agent
  files now say to write one command per nested list item, with no prose.

- A resumed `pr` publish now also checks the content of each merge commit
  added since the pause. Each must be a two-parent merge that brings in the
  base branch, and its tree may differ from the automatic merge of its parents
  (`git merge-tree --write-tree`) only in files that merge left conflicted.
  An operator merge that carries other edits, a merge of another branch, an
  octopus merge, or a failed or unsupported `git merge-tree` pauses with the
  story state kept and pushes nothing; the message names the merge and up to
  five offending files. Edits inside conflicted files are not inspected.
- A handoff whose `status` contradicts its `next_agent` is now rejected as
  invalid frontmatter instead of silently following the status. Before, a
  backend handback with `next_agent: planner` and
  `status: blocked_missing_context` became an escalation pause while the
  post-dispatch validator still passed it. The router now reports the
  conflict, so a handoff that names a non-`user` agent goes to the planner for
  repair and one that names `user` pauses; the validator fails it with
  `frontmatter_status_route_conflict`. The handbook and the backend and
  frontend prompts now pair `blocked_missing_context` and `escalate_to_user`
  with `next_agent: user`, and `blocked_implementation_failure` (re-scope)
  with `next_agent: planner`. A passing audit fan-out now always writes
  `next_agent: planner` (or `finalizer` for an epic close) to match its
  `verified_pass` status, which is where it already routed, and the auditor
  and finalizer prompts pair each status with its route.

- A resumed `pr` publish now also checks the content of each merge commit
  added since the pause. Each must be a two-parent merge that brings in the
  base branch, and its tree may differ from the automatic merge of its parents
  (`git merge-tree --write-tree`) only in files that merge left conflicted.
  An operator merge that carries other edits, a merge of another branch, an
  octopus merge, or a failed or unsupported `git merge-tree` pauses with the
  story state kept and pushes nothing; the message names the merge and up to
  five offending files. Edits inside conflicted files are not inspected.

- A resumed `pr` publish now checks what was added to the story branch while
  publishing was paused. The pause records the story branch head, and on the
  next start the dispatcher fetches the base branch and pushes only when every
  commit added since the pause is a merge commit or is already on the base
  branch. A non-merge commit, a branch that was reset or rewritten, or a
  failed `git` check pauses again with the story state kept, pushes nothing,
  and makes no `gh` call; the message lists up to five offending commits. A
  story state saved by an older version has no recorded head, so it pauses
  instead of resuming.

- In dispatcher commit mode the live handoff no longer leaks into pull
  requests. The finalizer is told to commit only project state, and a
  finalizer commit that holds only the handoff is undone with
  `git reset --soft` (one that mixes the handoff with other files stops the
  loop). A new story branch is refused when the base branch has commits that
  are not on the remote and change the handoff file. After a pull request
  correction round, the audit now reviews the whole story branch instead of
  an empty range when the round made no new commit.

- Fixed a resumed `pr` publish that had paused at the merge step forgetting
  that origin when the base branch conflicted a second time. The second pause
  now says the pull request was left open and keeps the merge-step marker, so
  with `wait_for_reviewers` the next resume still leaves the operator's merge
  for a person to review instead of waiting for a review that never comes.

- `pr` publish mode now keeps the story branch up to date with the base
  branch. Before each push it fetches the base branch and merges it into the
  story branch, so a pull request that merged during the story no longer
  stops the loop at `gh pr merge`. When `gh pr merge` reports the pull
  request is not mergeable, the dispatcher merges the base branch once more,
  pushes, and retries the merge once. A conflicting merge is aborted and
  pauses the loop with the conflicting files named and the story state kept;
  after you merge and commit on the story branch, the next start resumes
  publishing. This repository's `.gitattributes` now merges `CHANGELOG.md`
  with `merge=union`, so two new bullets in the same section merge cleanly.

- The files `llm-handoff init` copies no longer point at llm-handoff's own
  `CONFIGURATION.md` or `docs/ARCHITECTURE.md`, which a target repository
  does not have. The planner, Gemini planner, and Codex backend prompts now
  read repository-specific architecture or requirements docs referenced by
  the handoff; the template `AGENTS.md` states the provider-native helper
  boundary directly; the template `dispatch_config.yaml` comments say
  `llm-handoff's CONFIGURATION.md`. The root copies of the edited prompts
  match the templates, and a new test guards every shipped template file.

- Fixed the protected-file check pausing a run when a protected file was
  listed under a Work Packet's Files in scope as a bullet list. The check
  now reads Files in scope and Files out of bounds with the same parser as
  the work-claim gate and dispatcher commit, so the narrowest entry decides.

- Wording and ASCII fixes: the normalizer comment in both config templates
  no longer says the model is a Claude model, so it stays true in the
  all-Codex `init` config; the finalizer skill and `CONFIGURATION.md` now say
  that `commit_mode: dispatcher` commits backend and frontend work and leaves
  the planner's handoff uncommitted; `tests/test_cli.py` uses a Python
  escape for U+2502 instead of the literal box-drawing character.

- Added integration tests in `tests/integration/` (marker `integration`).
  They build and install the wheel offline, then run the installed
  `init`, `doctor`, and `--dry-run` on a throwaway Node repository and a
  one-commit Go repository, each with only a fake `claude` or only a fake
  `codex` on `PATH`. They check the single-provider mapping, that existing
  `AGENTS.md` text and `.claude/settings.json` deny rules survive `init`,
  that a second `init` changes nothing, and that no other file changes.

- `llm-handoff doctor` now reports a `git history` check. It warns when the
  repository has no commits yet (commit before the first dispatcher run) or
  only one commit (the audit review base defaults to the parent of the
  reviewed commit, so audits need a second commit or an explicit base). Two
  or more commits pass. The check reads history with `git rev-list` only and
  never fails the doctor.

- Added an all-Codex mapping. The reference workflow now ships a Codex repo
  skill for every role (`llm-handoff-planner`, `llm-handoff-frontend`,
  `llm-handoff-auditor`, `llm-handoff-validator`, and
  `llm-handoff-finalizer`, with `llm-handoff` staying the backend skill).
  `llm-handoff init --provider codex` writes every role as `provider: codex`
  with its `skill_name`, `audit.security_provider: codex`, and
  `normalizer.provider: codex`. Without `--provider`, `init` picks this
  mapping when `codex` is the only provider CLI on `PATH`. Whether Codex's
  `workspace-write` sandbox lets a role commit is unverified;
  `commit_mode: dispatcher` avoids the question for the planner, backend, and
  frontend, but not for the finalizer, which still commits its own state
  update. When the validator or finalizer runs on Codex, the dispatcher
  parses the `summary` of Codex's `{status, summary}` JSON envelope, so their
  reports are read instead of being reported as unparseable.

- The pull request and merge-commit report is now a short, readable page
  for people, with two sections. The Summary is at most three sentences,
  taken from the start of the Work Packet's Objective. Before, it was the
  whole Objective with every line break removed, so numbered acceptance
  checks ran into one long paragraph. Reviewer notes have `### Story`,
  `### Acceptance checks` (the rest of the Objective, with its lists kept),
  and `### Audit`. The report no longer has the Changes, Test plan, Work
  Packet, or Token usage sections: the pull request's Files and Commits
  tabs, `HANDOFF.md`, and the dispatch log already hold that. The
  dispatcher no longer saves the gate result to
  `logs/dispatch/publish/gate.md` or a log offset in the story state. A
  story state saved by an older version still loads.

- In `publish.mode: pr`, a pull request held open only by deferred review
  findings now goes to the planner instead of pausing. The dispatcher
  rewrites `HANDOFF.md` to route to `planner` with each deferred finding, the
  implementer's `Deferred:` response, and the current Work Packet. The planner
  gets one pass per pull request. It can widen the Work Packet and send the
  findings back to the implementer on the same branch, or accept them as
  follow-up work with a `## Deferred Finding Decisions` section whose
  `### Finding N` entries start with `Follow-up:`. The dispatcher then posts
  each decision as the reply, resolves the thread, and merges. It can also
  route to `user`. The dispatcher still pauses when a finding is left
  undecided, is deferred again after the planner's pass, or sits next to
  another unresolved thread. Before, any deferred finding paused the merge
  for a person, so a finding whose fix was outside the story's Files in scope
  always stalled the loop.

- Added the optional `verification_allowed_prefixes` config key. When set,
  the work-claim gate runs a Work Packet Verification command only if its
  whitespace-split words start with a listed prefix's words and it contains
  none of `&`, `|`, `;`, `<`, `>`, `$`, backtick, `(`, `)`, `%`, `!`, or a
  line break (`%` and `!` block `cmd.exe` variable expansion on Windows).
  Otherwise no Verification command runs, the claim fails the new
  `verification_not_allowed` check, and the bounce routes to `planner`. An
  empty or whitespace-only entry, or an entry with one of those characters,
  fails config load. With the list empty, commands run as before, and every
  dispatcher start logs `WARN Verification allowlist: OFF` and
  `llm-handoff doctor` prints `WARN verification allowlist:`.
  `llm-handoff init --verification-command` writes the confirmed command into
  the list and now rejects a command with one of those characters. The
  dispatcher logs the exact text of each scanner and Verification command
  before it runs.

- Behavior change: the work-claim gate's Verification command and configured
  `scanners` now run with an allowlisted environment instead of the
  dispatcher's full inherited environment. They receive only the base and
  platform process variables provider CLIs also get (`PATH`, `HOME`,
  `USERPROFILE`, `SystemRoot`, `TEMP`, `COMSPEC`, `APPDATA`, and similar)
  plus the names listed in the new `work_claim_env_passthrough` config key.
  Vendor API keys and `CLAUDE_*`, `CODEX_*`, and `GEMINI_*` variables never
  reach them; a vendor API key name in `work_claim_env_passthrough` fails
  config load, as do empty entries and invalid variable names. A
  Verification command or scanner that relied on an inherited variable, such
  as a proxy setting or a scanner token, now needs that name listed in
  `work_claim_env_passthrough`.

- Behavior change: the work-claim gate is now on by default
  (`enforce_work_packet` defaults to `true`, and the shipped
  `dispatch_config.example.yaml` and reference workflow config set it to
  `true`). Opting out takes `enforce_work_packet: false` plus a non-empty
  `work_packet_opt_out_reason`; a config that turns the gate off without a
  reason, or with a whitespace-only one, no longer loads, and a reason set
  while the gate is on is rejected. `commit_mode: dispatcher` still requires
  the gate, with the same error as before, even when a reason is set. When the
  gate is off, every dispatcher start logs a WARN with
  `enforce_work_packet: false` and the reason instead of the old INFO line,
  and `llm-handoff doctor` prints `WARN work-claim gate:` with the reason; with
  the gate on it prints `PASS work-claim gate:`. The doctor's handoff
  validation now applies the same Work Packet requirement as the dispatch
  loop, so a planner assignment the dispatcher would reject for an
  unparseable Work Packet also fails `llm-handoff doctor`. Configs that omit
  `enforce_work_packet` now run the gate: planner assignments to `backend` or
  `frontend` must carry a parseable `## Work Packet`, and post-dispatch
  validation rejects them otherwise. To keep the old behavior, opt out with a
  reason.

- Behavior change: completion claims are now checked against Git.
  Post-dispatch validation fails a `ready_for_review` handback from `backend`
  or `frontend` that has no frontmatter `scope_sha`
  (`frontmatter_scope_sha_missing:`); before, only `close_type` required it.
  When status is `ready_for_review`, `verified_pass`, or `verified_fail`, a
  Verification Evidence `Commit SHA verified` value that does not name a
  commit in the repository fails `evidence_sha_unknown:`. In dispatcher commit
  mode the `pending-dispatcher-commit` marker is exempt. The `sha_missing`
  check now counts only hex tokens that resolve to commits. Outside a Git
  repository neither check resolves anything: `evidence_sha_unknown` is not
  reported and `sha_missing` counts any hex token, as before. The work-claim
  gate adds `evidence_sha_mismatch`, which fails a claim whose
  `Commit SHA verified` does not resolve to the same commit as `scope_sha` or
  to a handoff-only commit above it, and it now applies `no_changes` in both
  commit modes, not only in dispatcher commit mode: a claim whose commits
  since dispatch change no file other than the handoff fails. Both gate
  failures bounce to the implementer as `verified_fail`. The HANDBOOK now says
  verification-only work belongs with the auditor.

- Behavior change: the audit now runs the security reviewer next to the
  quality reviewer by default (`audit.reviewers` defaults to
  `[quality, security]`). Opting out takes `audit.reviewers: [quality]` plus a
  non-empty `audit.security_opt_out_reason`; a config that leaves out
  `security` without a reason no longer loads, and a reason set while
  `security` is listed is rejected. When security is opted out, every
  dispatcher start logs a WARN with the reason and `llm-handoff doctor`
  prints `WARN security review:` with it. With `audit.security_provider`
  unset, the reviewer is now the first of `claude`, `codex`, `gemini` whose
  CLI is on `PATH` and that differs from the implementer's provider, then the
  implementer's own vendor with a WARN that the review is not independent.
  At startup, before any agent launches, `run` now checks the reviewer for
  both `backend` and `frontend`, configured or automatic: its CLI must be on
  `PATH`, and a Gemini reviewer also needs its sandbox runtime and the Gemini
  CLI security extension installed and enabled for the repository (an
  extension that `gemini extensions list` shows as disabled fails the
  check). A failed check stops the run with a
  `security_reviewer_failed:` error. Before, only a configured Gemini
  reviewer's sandbox was checked, and the automatic choice did not check
  what was installed. Reviewer errors during an audit still fail closed.
  Configs that relied on the old quality-only default must either install a
  reviewer CLI or opt out with a reason.

- `llm-handoff init` now prints candidate build and verification commands it
  finds in the target repository (CI workflow `run:` steps, `package.json`
  scripts, pytest in `pyproject.toml`, `Makefile` test targets, Cargo, and
  Go), each with its source file. It runs none of them. The new
  `--verification-command "<cmd>"` option confirms one: its executable must be
  a bare program name, and `init` adds `Bash(<executable>:*)` to the
  `allowed_tools` of every Claude role in `dispatch_config.yaml`. Without the
  option the written config is unchanged.

- The work-claim gate's story base now survives a dispatcher restart in the
  `merge-commit` and `pr` publish modes. The base was kept only in memory, so
  after a restart mid-story it became the current HEAD. A re-claim with nothing
  new to commit, for example after a review correction round, then failed
  `no_changes` and post-dispatch validation failed
  `frontmatter_scope_sha_missing`. The gate now takes the base from the saved
  story state (`logs/dispatch/publish/story.json`) when it names the same
  `story_id` and its branch is checked out. The security audit range uses the
  same saved base for the story's first dispatch after a restart, so the audit
  still reviews the story's earlier commits. `local` mode and a story state for
  another story still use HEAD.

- Fixed a work-claim loop that prose in a Work Packet caused. A
  `Verification command` line that is only a connective (`and`, `or`, `then`,
  `plus`, `also`) or that ends in `:` is now `work_packet_unparseable`, so the
  planner's post-dispatch validation rejects it. Before, the gate ran it as a
  command. A claim whose Verification command fails again with the same command
  as the previous bounce, with no other failed check and no new commit changing
  a file other than the handoff, now bounces to the planner instead of the
  implementer. The gate runs the packet from the assignment, so the
  implementer could not fix it.

- A pull request review response that starts with `Deferred:` now keeps its
  finding's review thread open. The dispatcher still posts the reply, logs a
  `WARN` naming the deferred finding, and does not resolve the thread, so the
  unresolved-thread rule pauses the merge for a person. Before, any response
  resolved its thread, so a real finding the implementer left unfixed was
  closed silently and the pull request merged.

- Fixed a stale Work Packet in the branch publish modes. The story state kept
  the packet from when the story branch was created, so a planner revision
  made later was lost: the review correction handoff and the pull request
  report brought the old packet back, and the gate failed on the defect the
  planner had fixed. Each implementer dispatch on the story branch now
  refreshes the saved packet from the assignment.

- Added a `codex` next-agent normalizer provider. `normalizer.provider: codex`
  runs `codex exec --sandbox read-only` on the user's Codex CLI login, so an
  all-Codex setup does not need `OPENAI_API_KEY`; the adapter never passes
  that key. It runs from a private temp directory rather than the checkout,
  and a timeout terminates the whole Codex process tree. Launch failures and
  unusable output fail closed. `llm-handoff
  doctor` now prints a `normalizer` line that fails when the configured
  normalizer has no usable binary or API key, without calling the provider.

- Added an `llm-handoff doctor` subcommand that checks a checkout before a
  run: Git, each configured role's provider CLI binary, a container runtime
  when a Gemini CLI role runs sandboxed, and the current handoff file. It
  prints one `PASS`, `FAIL`, or `WARN` line per check, exits `1` on any
  error, and writes no files. The supervisor's `--doctor` mode now shares its
  PATH lookup.

- Fixed a work-claim loop that a contradictory Work Packet caused. A path
  listed under both `Files in scope` and `Files out of bounds` is now
  `work_packet_unparseable`, which routes to the planner; before, it resolved
  to out of bounds, the dispatcher would not commit it, and the gate told the
  implementer to commit it until the circuit breaker tripped. A new uncommitted
  change to a file the packet does not allow is now a `scope_violation` that
  says to revert it. With `commit_mode: dispatcher` the bounce no longer asks
  the implementer to commit, and when the dispatcher committed nothing, the
  dispatcher skips the handoff repair for the missing SHA and lets the gate
  report the claim. `src` and `src/` count as the same entry for the
  both-lists check. A retry that only reverts a forbidden file now passes
  `no_changes` on the story's earlier dispatcher commit, which the dispatcher
  records as `scope_sha`.

- The default Claude model for roles with no `model` set is now
  `claude-opus-5-5`, and the default Claude normalizer model is
  `claude-haiku-5-5`. The `init` template, example configs, and
  CONFIGURATION.md use the 5.5 models. Configs that set `model` explicitly
  are unchanged.

- Added a `pyproject.toml` (hatchling) so the dispatcher installs from a Git
  checkout with `pip install .` or `pip install git+https://...` and exposes an
  `llm-handoff` console command. The wheel bundles
  `examples/reference-workflow/`, hidden paths included, and `init` resolves the
  template through `importlib.resources`, falling back to the source layout.
  hatchling joins the dev requirements so the packaging test builds the wheel
  offline. Not yet published to PyPI.

- Roles accept an optional `fallback` agent. When the primary agent times
  out, hits a rate or usage limit, is not signed in, or cannot launch, the
  dispatcher reruns that dispatch once on the fallback. Ordinary failures and
  runs that already rewrote the handoff or changed a protected control file
  do not fail over.
- Roles accept an optional `idle_timeout_ms`. When set, an agent that writes
  no stdout or stderr output for that long is killed and reported as a
  timeout, so a hung agent fails over to its fallback before the full
  `timeout_ms`. Off by default, because some CLIs stay silent while they work.

- The audit fan-out range now starts at the story's first implementer
  dispatch. A retry after a work-claim or audit bounce no longer moves the
  base to the earlier attempt's commit, so the reviewers see every attempt.
  The base resets when the story changes (its `story_id`, or its Work Packet
  when there is no `story_id`) or the dispatcher accepts the story's
  approval; a `verified_pass` that fails validation or recovery keeps it.

- `init` preserves user content through managed sections in AGENTS.md and
  ignore files, merges Claude settings, and reports merge actions separately.
  Added a .gitignore template for dispatcher logs and the lock file.
  Identical unmarked templates migrate without duplication; linked destination
  files and directories are rejected as conflicts, including with --force.
  Windows reparse-point detection also protects junctions on Python 3.11.
  Hard-linked files and non-directory destination parents are also conflicts,
  including during dry runs and with --force.

- Work Packet path lists written as prose no longer turn words like "every",
  "file", or "than" into out-of-bounds entries, and entries after "other than"
  or "except" are not out of bounds. When a file matches both lists, the
  narrowest entry decides, so an in-scope file inside an out-of-bounds
  directory is committed instead of left in the working tree.
- With `commit_mode: dispatcher`, work an implementer dispatch left uncommitted
  is carried into the next dispatch for the same story, so a bounce can commit
  it. Before, it counted as an operator edit: it was never committed, and the
  gate could pass a claim whose `scope_sha` left it out.
- The one-shot handoff repair no longer misreads `git status` (the first path
  lost a character), and in dispatcher commit mode it leaves the handoff
  uncommitted instead of being required to commit it.
- A Work Packet field written as nested bullets under its label, such as
  `**Context:**`, no longer gets an "is empty" warning.

- With `commit_mode: dispatcher`, a blocked or escalated implementer handback
  that carries the `pending-dispatcher-commit` marker now gets a
  `sha_pending` warning instead of a `sha_missing` validation error.
- Supervisor preflight and `--doctor` now validate the handoff with the
  configured commit mode, so they accept the same blocked handbacks awaiting a
  dispatcher commit.
- Isolated supervised child imports from the target working directory while preserving source-checkout launches.
- Fixed Codex launches in target repositories by resolving the output schema from the installed package.
- Seeded the `llm_handoff` extraction scaffold.
- Added public repository instructions and README.
- Added documentation split for installation, configuration, architecture,
  testing, contribution scope, security, and reference workflow setup.
- Added generic handoff prompt files plus Codex, Gemini, and Claude agent
  templates.
- Added `python -m llm_handoff init` for copying the reference workflow into a
  target repository.
- Promoted the ported dispatcher tests into the public full-suite gate.
- Added an opt-in deterministic work-claim gate (`enforce_work_packet`) that
  checks `ready_for_review` claims against the Work Packet before dispatching
  the auditor and bounces failures with evidence.
- Added configurable `scanners` to the work-claim gate (gitleaks, semgrep,
  bandit, pip-audit, or any installed tool) with diff-scoped placeholders,
  per-scanner timeouts, and `required` handling for missing executables.
- Preserved Apache-2.0 licensing.
- Added opt-in audit fan-out (`audit.reviewers: [quality, security]`): a
  read-only quality reviewer and security reviewer run concurrently on the
  implementer's commit range, and the dispatcher writes `HANDOFF.md` from
  their structured verdicts.
- Provider CLIs now launch with an allowlisted environment instead of a copy
  of the dispatcher's environment. Per-role `env_passthrough` adds names and
  `env_mode: inherit` restores the old copy. Codex roles need
  `use_api_key_env: true` to receive `OPENAI_API_KEY`.
- Protected control files: a dispatch that changes `AGENTS.md`, handoff
  prompts, the dispatch config, or provider agent directories without listing
  them in the Work Packet now routes the handoff to `user` with a
  `protected_file_modified:` error. Configure with `protected_paths`.
- Safer launch flags by default: Claude Code runs with
  `--permission-mode dontAsk`, an `allowed_tools` list, and web tools denied;
  Codex runs with `--sandbox workspace-write --ask-for-approval never`; Gemini
  CLI keeps its container sandbox and refuses to start without Docker or
  Podman. Per-role `unsafe_bypass: true` restores the previous flags and logs
  a startup warning. `permissions_flag` now requires `unsafe_bypass`.
- The reference workflow's Gemini guardrails now use the policy engine's
  `[[rule]]` schema (the previous file matched no schema and enforced
  nothing), with user-tier install steps. It also ships a Claude Code
  `.claude/settings.json` with deny rules and a standard-library
  `PreToolUse` hook that blocks the same commands.
- SECURITY.md has a Threat Model section. `auto_push` is documented as unused.
- Audit security reviewers now launch with the hardened defaults. Both Codex
  reviewer paths (`codex-security scan` and `codex exec`) used to inherit the
  dispatcher's whole environment; every reviewer now uses its provider
  role's environment allowlist. The Claude reviewer also denies web tools.
- The Gemini audit security reviewer runs plan mode inside Gemini CLI's
  container sandbox instead of `--sandbox=false`, because headless plan mode
  auto-approves `exit_plan_mode` and continues in YOLO mode. With
  `audit.security_provider: gemini`, `run` refuses to start without Docker or
  Podman unless the Gemini role sets `unsafe_bypass: true`.
- The reference workflow ships Claude subagents for `planner`, `backend`, and
  `frontend`, so every role can run on Claude Code alone.
  `init --provider claude` writes an all-Claude mapping, and `init` picks it
  automatically when `claude` is the only provider CLI on `PATH`.
- When a provider CLI fails because it is not signed in (for example Claude
  Code's "OAuth session expired"), the dispatcher now stops at once and logs
  the sign-in steps for that CLI, instead of retrying until the failure limit.
  The supervisor recognizes the same sign-in failures.
- Gemini CLI's `IneligibleTierError` (Google no longer accepts personal Gemini
  Code Assist sign-ins) is treated as a sign-in failure: the dispatcher stops
  with steps to switch to an API key, and the Gemini adapter no longer waits
  out its retry backoff on any sign-in failure.
- Agent launches now keep the account name (`USERNAME`, `USER`, `LOGNAME`).
  Without it, pytest in an agent run fell back to a shared
  `pytest-of-unknown` temp directory that another account could own.
- The reference workflow ships `.codex/rules/llm-handoff.rules`, which
  forbids `git push` for Codex roles. It does not allow git writes outside
  the sandbox, because prefix rules cannot restrict trailing arguments such
  as `-a` or `--amend`; sandboxed Codex implementers use
  `commit_mode: dispatcher` instead of `unsafe_bypass`.
- Added `commit_mode: dispatcher`: implementers leave their changes
  uncommitted, and the dispatcher stages only files inside the Work Packet's
  scope, commits them with `--no-verify --only`, and records the commit as
  `scope_sha` before validation and the work-claim gate. In that mode the gate
  also fails claims with no new commit (`no_changes`). Requires
  `enforce_work_packet: true`.
- Token usage and cost moved off the console and into the log file. Each
  agent run writes one `DEBUG` line, `usage: <agent> provider=<p> model=...
  input_tokens=... output_tokens=... cache_*=... cost_usd=... num_turns=...`,
  for Claude roles, Codex and Gemini (tokens only; they report no price), and
  the Claude security reviewer. `grep 'usage:'` a dispatch log to total a run.
  The console keeps a short `Claude result (stop=...)` / `Gemini result
  (status=...)` line.
- The security reviewer's verdict schema no longer carries a `$schema` key,
  which Claude Code's `--json-schema` rejected at launch.
- The test suite runs on 8 `pytest-xdist` workers (about 28s instead of about
  158s). Four protected-file tests had been launching the real `claude` CLI
  for the validator role; they now stub it, and an autouse fixture fails any
  test that tries to launch a real provider CLI.
- A passing work-claim gate now writes a `## Dispatcher Verification` section
  (verdict, verified commit, checks, verification command) into the handoff.
  The auditor and the fan-out quality reviewer are told not to re-run a
  Verification command the dispatcher already passed on that commit.
- Added `publish.mode` (`local`, `merge-commit`, `pr`). In the branch modes
  the dispatcher puts each story on its own `llm-handoff/<story>` branch and,
  after the gate and audit pass, either merges it with a report commit or
  pushes it and opens a pull request whose body is the report (Work Packet,
  gate result, audit verdicts, token usage and cost), merging it when
  `auto_merge` is on. Agents never branch, push, or merge. Default stays
  `local`; the branch modes need `commit_mode: dispatcher`.
- Added `publish.wait_for_reviewers: [codex]` for `pr` mode with
  `auto_merge`. Before merging, the dispatcher waits for the Codex GitHub
  review of the pushed commit. No findings merges. Findings bounce to the
  story's implementer as a `verified_fail` handoff for one correction round
  (`max_review_rounds: 1`); the fix goes to the same pull request and merges
  without another review once its threads are resolved. The dispatcher never
  asks Codex for a review. A review that does not finish within
  `review_timeout_seconds` stops the loop with the pull request left open.
- Story pull requests and merge commits now follow the repository's pull
  request template (`.github/pull_request_template.md`): Summary, Changes,
  Reviewer notes, and Test plan, with the full reviews, Work Packet, and
  token usage folded underneath.
- Claude roles are told their shell allowlist up front (one command per
  call, no chaining, `git -C`, or `cat`/`find`; use file tools instead),
  through `--append-system-prompt` and a Shell Commands section in each role
  file, so subagents see it too. A command the allowlist refuses is logged as
  a one-line WARN instead of a red ERROR carrying the full refusal text.
- A blocked implementer handback in dispatcher commit mode may now add a
  note after the `pending-dispatcher-commit` marker (for example
  `pending-dispatcher-commit (nothing to commit)`); it still gets the
  `sha_pending` warning instead of the `sha_missing` error.
- When Codex runs out of code review usage, the review gate now says so
  when it stops: the bot's usage-limit comment on the pull request marks the
  review as failed (even before the summary flips to Failed), and the pause
  message names the limit. The pull request is still left open, never merged.
- In `commit_mode: dispatcher`, the planner is now told not to commit the
  handoff file, and the dispatcher enforces it: planner commits that change
  only the handoff are undone with `git reset --soft` (the handoff stays in
  the working tree), and any other planner commit, branch switch, or history
  rewrite stops the loop. Committing the handoff put live dispatch state on
  the base branch and, in the branch publish modes, into every story pull
  request.
- Review findings are now closed the way a person would close them: the
  implementer writes a `## Review Responses` entry per finding, and on the
  next push the dispatcher posts each one as the reply on that finding's
  review thread and resolves it. `pr` mode only auto-merges a pull request
  whose review threads are all resolved; otherwise the loop stops and leaves
  it open.

## 0.0.0

- No public release tag has been published yet.
