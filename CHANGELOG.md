# Changelog

All notable changes to `llm-handoff` will be documented here.

This project follows a simple pre-release changelog until the first public tag.

## Unreleased

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
  story's implementer as a `verified_fail` handoff; the fix goes to the same
  pull request with an `@codex review` request, for up to
  `max_review_rounds` rounds. A review that does not finish within
  `review_timeout_seconds` stops the loop with the pull request left open.
- In `commit_mode: dispatcher`, the planner is now told not to commit the
  handoff file. Committing it put live dispatch state on the base branch and,
  in the branch publish modes, into every story pull request.
- Review findings are now closed the way a person would close them: the
  implementer writes a `## Review Responses` entry per finding, and on the
  next push the dispatcher posts each one as the reply on that finding's
  review thread and resolves it. `pr` mode only auto-merges a pull request
  whose review threads are all resolved; otherwise the loop stops and leaves
  it open.

## 0.0.0

- No public release tag has been published yet.
