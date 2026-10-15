# Changelog

All notable changes to `llm-handoff` will be documented here.

This project follows a simple pre-release changelog until the first public tag.

## Unreleased

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
