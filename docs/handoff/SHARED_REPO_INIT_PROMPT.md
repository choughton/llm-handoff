# Shared Repo Init Prompt

Use this prompt when a fresh agent session needs to load repository context before handling a specific `HANDOFF.md` assignment.

## Bootstrap

1. Read `AGENTS.md`.
2. Read `PROJECT_STATE.md` if present.
3. Read `docs/handoff/HANDOFF.md`.
4. Read `README.md`, `CONFIGURATION.md`, and `docs/ARCHITECTURE.md` as needed.
5. Read only the extra files needed for the active assignment.

## State Model

Agents do not share memory. Treat version-controlled files as the source of truth:

- `docs/handoff/HANDOFF.md` is the live routing state.
- `PROJECT_STATE.md` is the durable project state file when the consuming repo chooses to use one.
- Git history is the durable execution record.

## Required Handoff Frontmatter

Every handoff write must begin with YAML frontmatter:

```yaml
---
next_agent: <enum>       # required: planner | backend | frontend | auditor | validator | finalizer | user
reason: <string>         # required: quote every `reason` value
epic_id: <string>        # optional
story_id: <string>       # optional
story_title: <string>    # optional
remaining_stories:       # optional
  - <story id/title>
scope_sha: <git SHA>     # required when close_type is story or epic
close_type: <enum>       # optional: story | epic
prior_sha: <git SHA>     # optional
producer: <string>       # required: planner | backend | frontend | auditor | validator | finalizer | user
---
```

`scope_sha` must be a concrete 7-40 character hex Git SHA. Do not write `scope_sha: HEAD`.

## Operating Rule

Prompts are advisory. Validators and the dispatcher are authoritative. If the prompt conflicts with parsed frontmatter, Git state, or repository instructions, stop and report the conflict.
