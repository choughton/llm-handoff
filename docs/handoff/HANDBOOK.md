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

## Pull Request Review Responses

When the dispatcher bounces pull request review findings back to you, add a
`## Review Responses` section to the handback with one `### Finding N`
subsection per finding: one or two sentences on what you changed, or why you
left it. The dispatcher posts each response as the reply on that finding's
review thread and resolves the thread.

If a finding is real but you leave it unfixed for later work, start its
response with `Deferred:`, for example
`Deferred: out of scope for this story; needs its own Work Packet.` The
dispatcher posts that reply but leaves the thread unresolved, so the pull
request is not merged until a person decides. Do not use `Deferred:` for a
finding you rejected as wrong; explain why it does not hold up instead.

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
- Completion claims whose `scope_sha` is not HEAD or whose diff leaves the
  Work Packet scope.

Verification-only work, such as re-running tests or checking an earlier
commit, belongs with the auditor, not in an implementer Work Packet. When the
work-claim gate runs, an implementer `ready_for_review` claim whose commits
since dispatch change no file other than the handoff fails `no_changes`, and
a claim whose `Commit SHA verified` names a different commit than `scope_sha`
fails `evidence_sha_mismatch`.
