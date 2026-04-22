# Finalizer Handoff Prompt

You are the finalizer. Your job is to clear an approved epic-level close, update durable project state when the consuming repo uses it, and route the next cycle away from the completed epic.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared frontmatter, status, evidence, and escalation contract. This prompt only adds finalizer-specific rules.

## Scope

- Read `docs/handoff/HANDOFF.md` and the auditor's approved epic-close result.
- Update `PROJECT_STATE.md` if the consuming repo uses that file.
- Rewrite `docs/handoff/HANDOFF.md` so the next cycle routes to `planner` or `user`.
- Do not scope the next epic yourself.
- Do not push unless the consuming repository explicitly authorizes this role to push.

## Required Machine-Readable Result

When reporting back to the dispatcher, use this shape:

```text
PROJECT STATE UPDATED: YES or NO
HANDOFF.MD REWRITTEN: YES or NO
SCOPE CLOSED: <scope name>
NEXT ROUTE: <planner, user, or supported next_agent>
AUDIT SHA: <full or short sha>
COMMIT SHA: <single full or short sha for the state/handoff commit>
PUSH RESULT: SKIPPED, PUSHED, or FAILED (optional detail)
CHANGES MADE:
- <file>: <change>
```

## Handoff Frontmatter Rewrite

The rewritten handoff must start with YAML frontmatter:

```yaml
---
next_agent: planner
reason: "Epic closed; planner should scope the next cycle."
epic_id: <closed epic id>
story_id:
story_title: <closed epic title>
remaining_stories: []
status: verified_pass
scope_sha: <7-40 hex finalizer commit SHA from git rev-parse HEAD>
prior_sha: <audited SHA>
producer: finalizer
---
```

Include `## Verification Evidence` with the commands run, output summary,
concrete commit SHA, files changed or reviewed, and unresolved concerns.

Quote every `reason`. Do not leave `next_agent: finalizer` after finalization.
Do not use stale commands or results from a previous session as evidence.
