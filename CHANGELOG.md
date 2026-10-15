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
- Preserved Apache-2.0 licensing.
- Added opt-in audit fan-out (`audit.reviewers: [quality, security]`): a
  read-only quality reviewer and security reviewer run concurrently on the
  implementer's commit range, and the dispatcher writes `HANDOFF.md` from
  their structured verdicts.

## 0.0.0

- No public release tag has been published yet.
