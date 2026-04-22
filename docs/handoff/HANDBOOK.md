# Handoff Handbook

Read this before acting on a `docs/handoff/HANDOFF.md` assignment. Role prompts
add role-specific instructions; this handbook contains the shared protocol.

## How HANDOFF.md Works

`HANDOFF.md` is the live state file. Only the active dispatcher role owns the
file during its turn. Provider-native subagents, skills, or helper agents are
internal support machinery and must not independently rewrite the handoff.

Every handoff has two layers:

- YAML frontmatter for machine routing.
- Markdown body for human-readable context, evidence, findings, and work
  packets.

## Frontmatter Schema Reference

Every handoff write starts with YAML frontmatter containing `next_agent`,
`reason`, and `producer`. Use the optional scope fields when known:
`epic_id`, `story_id`, `story_title`, `remaining_stories`, `scope_sha`,
`close_type`, `prior_sha`, `status`, `bounce_count`, and `evidence_present`.

Quote every `reason`. Run `git rev-parse HEAD` for concrete SHAs. Never write
`scope_sha: HEAD`.

## Status Enum Reference

Use exactly one of these values when status is needed:

- `ready_for_review`
- `verified_pass`
- `verified_fail`
- `blocked_missing_context`
- `blocked_implementation_failure`
- `escalate_to_user`

Do not invent synonyms or change capitalization.

## Evidence Block Reference

When `status` is `ready_for_review`, `verified_pass`, or `verified_fail`, include
`## Verification Evidence` with these five fields:

- `Commands run`
- `Output summary`
- `Commit SHA verified`
- `Files changed or reviewed`
- `Unresolved concerns`

Evidence must come from the current turn. Prior output, assumptions, and model
confidence are not evidence.

## When To Flag Uncertainty

Stop and route to `planner`, `validator`, or `user` when scope, ownership,
routing, tests, or Git state are ambiguous. Do not widen your role boundary to
avoid asking.

## Escalation Protocol

Use `status: escalate_to_user` and `next_agent: user` when human input is
required. Use `blocked_missing_context` when the missing input is specific and
the next human question is clear. Use `blocked_implementation_failure` when an
implementation path failed structurally and needs re-scoping.

## Common Failure Modes

- Missing or malformed YAML frontmatter.
- Provider names used as public workflow roles.
- `scope_sha: HEAD` instead of a concrete SHA.
- Completion claims without `## Verification Evidence`.
- Planner assignments without a concrete work packet.
- Auditor approvals that skip spec compliance.
- Repeated implementer/auditor bounces on the same story.
