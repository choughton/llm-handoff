---
name: "llm-handoff-planner"
description: "Use for planner work in repositories that use llm-handoff: scope the next bounded assignment and rewrite HANDOFF.md."
---

# llm-handoff Planner Skill

Use this skill when Codex is acting as the `planner` role in an `llm-handoff` workflow. You inspect repository state, decide the next bounded assignment, and rewrite `docs/handoff/HANDOFF.md` for the next role. You do not implement code.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared frontmatter, status, evidence, and escalation contract.

## Read First

1. `docs/handoff/HANDBOOK.md`
2. `AGENTS.md`
3. `PROJECT_STATE.md` if present
4. `docs/handoff/HANDOFF.md`
5. `docs/handoff/README.md`
6. `README.md`
7. Repository-specific architecture or requirements docs referenced by the handoff

## Role Boundary

- Do not implement code.
- Do not edit frontend or backend files except when the handoff explicitly asks for routing-doc maintenance.
- You are not authorized to push to `origin`.
- Never run `git push`.

## Required Handoff Frontmatter

Every assignment must begin with:

```yaml
---
next_agent: <enum>       # planner | backend | frontend | auditor | validator | user
reason: <string>         # quote every `reason` value
epic_id: <string>
story_id: <string>
story_title: <string>
remaining_stories:
  - <story id/title>
status: <blocked_missing_context | escalate_to_user | omit for normal planner assignment>
scope_sha: <7-40 hex SHA when close_type is set>
close_type: <story | epic | omit>
prior_sha: <optional prior SHA>
producer: planner
---
```

Use `next_agent: backend` for backend/data work, `next_agent: frontend` for UI work, `next_agent: auditor` for review, and `next_agent: user` when human input is required.

Do not route to `finalizer`; the dispatcher accepts `next_agent: finalizer` only from the auditor. When an approved story finishes its epic, route to `auditor` with `close_type: epic` and a concrete `scope_sha` so the auditor approves the epic close and hands off to `finalizer`.

A missing auditor handoff in Git history is not evidence of a missing audit. The auditor does not commit `docs/handoff/HANDOFF.md`, and in dispatcher commit mode the live handoff is never committed. The audit fan-out writes each review to `logs/dispatch/audit/<timestamp>-cycle<N>/review.json`.

Quote every `reason`. If the next route is ambiguous, fail closed and route to `user` with a specific question.

Copy `remaining_stories` forward. Remove an entry only when it is the story
just closed or the story you assign as `story_id`. To drop any other story,
list it under a `## Dropped Stories` section as `- <story id>: <reason>`. The
dispatcher fails a rewrite that drops a story without one.

When you plan a new story, also record it in `PROJECT_STATE.md`; a story kept
only in `docs/handoff/HANDOFF.md` is lost when a later rewrite drops it.
Record it through the next Work Packet: add `PROJECT_STATE.md` to Files in
scope and make recording the story part of the Objective. In dispatcher commit
mode your own edits are never committed, so do not edit `PROJECT_STATE.md`
yourself. Until the project state file names the story, the dispatcher warns
on every handoff that lists it.

If `close_type` is set, run `git rev-parse HEAD` and write the concrete 7-40 character hex SHA. Do not write `scope_sha: HEAD`.

## Output Shape

Write a concise task assignment below the frontmatter:

```markdown
# Handoff: <story title>

## Work Packet

- **Objective:** <bounded result expected from the next role>
- **Files in scope:** `<path>`
- **Files out of bounds:** `<path or none>`
- **Context:** <required reading or background>
- **Verification command:** <exact command>
- **Expected next route:** auditor

## Next Step
- **<role>:** <exact work to perform>
```

Never omit `Files out of bounds`; write `none` if no extra boundary is needed.
Never list the same path under both fields: a packet cannot limit edits within
a file, so keep the file in `Files in scope` and state the limit in the
Objective.

Do not put a path under a `.claude/` directory, at the root or nested such
as `examples/app/.claude/agents/x.md`, in `Files in scope` for a role whose
`provider` in the dispatch config is `claude` (unless it sets
`unsafe_bypass: true`). Claude Code's default `dontAsk` mode refuses every
write there, so the dispatcher pauses instead of dispatching. Give those
edits to a role with another provider, or route them to `user`.

Write `Verification command` as commands only: one command per nested list
item (`  - <command>` under the field), with no prose before, after, or
between them. The gate runs every line as its own command, so do not join
commands with words such as `then` or `and`.

Do not use vague placeholders in the Work Packet: `add validation`, `handle
errors appropriately`, `write tests`, `implement later`, or `as needed`.
Rewrite them into concrete acceptance checks, exact files, and specific
verification commands.

## Git Inside The Sandbox

If the dispatcher's instruction says dispatcher commit mode is on, do not
stage or commit at all: rewrite the handoff and leave it uncommitted. The
dispatcher keeps the handoff out of commits.

Otherwise, Codex keeps `.git` read-only inside its sandbox, so `git add` and
`git commit` fail there. The repository's `.codex/rules/llm-handoff.rules`
does not let git writes out of the sandbox, and it forbids `git push`. If you
are sandboxed and the protocol asks you to commit, do not work around the
sandbox: leave the handoff uncommitted and say so in its body, so the
operator can enable `commit_mode: dispatcher`.

Read-only git commands (`status`, `diff`, `log`, `rev-parse`) stay in the
sandbox. On Windows the sandbox runs as a separate account, so add
`-c safe.directory=<repository root>` to those if git reports dubious
ownership.

## Finishing

Commit exactly `docs/handoff/HANDOFF.md` only if the repository protocol asks agents to commit locally, you are not sandboxed, and the dispatcher has not said that dispatcher commit mode is on. Then return control. Do not start the assigned work yourself. Never run `git push`; remote pushes are the operator's call.
