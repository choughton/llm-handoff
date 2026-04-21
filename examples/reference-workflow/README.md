# Reference Workflow

This directory will contain the copyable written protocol for a target
repository that wants to use `llm-handoff`.

The dispatcher is not just Python code. It depends on agent instructions,
handoff conventions, ignore rules, and a config file that all agree on the same
role names.

## Planned Files

```text
AGENTS.md
PROJECT_STATE.md
dispatch_config.yaml
docs/handoff/HANDOFF.md
.geminiignore
.gemini/agents/planner.md
.gemini/agents/frontend.md
.codex/skills/llm-handoff/SKILL.md
.claude/agents/auditor.md
.claude/agents/handoff-validator.md
.claude/agents/finalizer.md
```

## Role Contract

Public role names:

- `planner`
- `backend`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Provider CLIs are configured separately. For example, a repo can map
`planner` to Gemini and `backend` to Codex without making those provider
names part of the handoff protocol.

## Handoff Contract

A handoff starts with YAML frontmatter:

```markdown
---
next_agent: planner
reason: Scope the first implementation task.
producer: user
---
```

The body should be clear enough for the next role to act without relying on
hidden state.

## Default Path

Recommended:

```text
docs/handoff/HANDOFF.md
```

Supported:

```text
HANDOFF.md
```

The nested path is recommended for non-trivial repos because it leaves room for
handoff examples, archive files, and helper prompts.

## Extraction Notes

The template files themselves still need to be generated from the genericized
workflow. Do not copy source-project prompt text directly into this example.
