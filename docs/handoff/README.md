# Handoff Prompt Files

This directory contains the reference prompt files used by `llm-handoff` plus the single live handoff state file, `HANDOFF.md`.

The handoff file is the mutex and the debugger: every transition is visible as text, and the dispatcher only advances when the route parses, validates, and maps to a known role.

## Files

| File | Role | Purpose |
|---|---|---|
| `HANDOFF.md` | shared | Live state file read and overwritten by agents |
| `SHARED_REPO_INIT_PROMPT.md` | all agents | Common bootstrap instructions for fresh sessions |
| `PLANNER_INITIAL_PROMPT.md` | planner | First planner session prompt |
| `PLANNER_HANDOFF_PROMPT.md` | planner | Ongoing planner dispatch prompt |
| `BACKEND_HANDOFF_PROMPT.md` | backend | Backend/Codex implementation prompt |
| `FRONTEND_HANDOFF_PROMPT.md` | frontend | Frontend/Gemini implementation prompt |
| `AUDITOR_HANDOFF_PROMPT.md` | auditor | Claude audit prompt |
| `FINALIZER_HANDOFF_PROMPT.md` | finalizer | Project-state and handoff-close prompt |

## Required Frontmatter

Every `HANDOFF.md` write must begin with YAML frontmatter. The dispatcher treats the YAML block as authoritative; prose sections are context for humans and agents.

```yaml
---
next_agent: <enum>       # required: planner | backend | frontend | auditor | validator | finalizer | user
reason: <string>         # required: quote every `reason` value
epic_id: <string>        # optional active epic identifier
story_id: <string>       # optional active story identifier
story_title: <string>    # optional short active story title
remaining_stories:       # optional remaining story IDs/titles
  - <story id/title>
scope_sha: <git SHA>     # required when close_type is story or epic
close_type: <enum>       # optional: story | epic
prior_sha: <git SHA>     # optional prior verified SHA
producer: <string>       # required: planner | backend | frontend | auditor | validator | finalizer | user
---
```

## Rules

- Use canonical role values in `next_agent`; provider names are adapter examples only.
- Do not write `scope_sha: HEAD`. Run `git rev-parse HEAD` and write the concrete 7-40 character hex SHA.
- Agents are not authorized to push unless the consuming repository explicitly grants that role permission.
- If routing is ambiguous, route to `validator` or `user`; do not guess.
