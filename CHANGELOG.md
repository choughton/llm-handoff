# Changelog

All notable changes to `llm-handoff` will be documented here.

This project follows a simple pre-release changelog until the first public tag.

## Unreleased

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
