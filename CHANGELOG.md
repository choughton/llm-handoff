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

## 0.0.0

- No public release tag has been published yet.
