# Reference Workflow

This directory contains the copyable written protocol for a target repository
that wants to use `llm-handoff`.

The dispatcher is not just Python code. It depends on agent instructions,
handoff conventions, ignore rules, and a config file that all agree on the same
role names.

## Included Files

```text
AGENTS.md
PROJECT_STATE.md
dispatch_config.yaml
docs/handoff/HANDOFF.md
docs/handoff/*.md
.geminiignore
.gemini/agents/planner.md
.gemini/agents/frontend.md
.gemini/policies/planner_guardrails.toml
.codex/skills/llm-handoff/SKILL.md
.codex/skills/llm-handoff/agents/openai.yaml
.claude/agents/auditor.md
.claude/agents/handoff-router.md
.claude/agents/handoff-validator.md
.claude/agents/ledger-updater.md
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

## Usage Notes

From the `llm-handoff` source checkout, initialize a target repository with:

```bash
python -m llm_handoff init path/to/your-project --template reference-workflow --dry-run
python -m llm_handoff init path/to/your-project --template reference-workflow
```

The initializer copies this directory's contents except this README. It skips
identical files and refuses to overwrite changed files unless `--force` is
passed.

After copying, edit `dispatch_config.yaml`, `PROJECT_STATE.md`, and
`docs/handoff/HANDOFF.md` for the target repo's first assignment.
