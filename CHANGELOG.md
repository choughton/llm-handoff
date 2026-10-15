# Changelog

All notable changes to `llm-handoff` will be documented here.

This project follows a simple pre-release changelog until the first public tag.

## Unreleased

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

## 0.0.0

- No public release tag has been published yet.
