# Configuration

`llm-handoff` is expected to use a YAML config file named
`dispatch_config.yaml` in the target repository.

This document describes the planned public configuration surface. The current
code scaffold still needs to be wired to this schema.

## Minimal Shape

```yaml
handoff_path: docs/handoff/HANDOFF.md
project_state_path: PROJECT_STATE.md
auto_push: false

agents:
  planner:
    provider: gemini
    binary: gemini
    resume: true
    timeout_ms: 1200000

  implementer:
    provider: codex
    binary: codex
    skill_name: llm-handoff
    resume: true
    timeout_ms: 1200000

  auditor:
    provider: claude
    binary: claude
    model: claude-opus-4-7
    resume: false
    timeout_ms: 900000
```

## Handoff Path

Default:

```yaml
handoff_path: docs/handoff/HANDOFF.md
```

Flat alternative:

```yaml
handoff_path: HANDOFF.md
```

The nested path is the recommended default because it leaves room for handoff
examples, archive files, and prompt handoff helpers. The flat path is useful for
small repos that prefer fewer directories.

## Project State Path

Recommended default:

```yaml
project_state_path: PROJECT_STATE.md
```

This file is an optional durable project-status document. Consumers can point it
at an existing file if their workflow already uses one.

## Auto Push

Default:

```yaml
auto_push: false
```

Remote pushes should be explicit. The dispatcher may create local commits as
part of a finalizer workflow, but it should not publish to a remote unless a
user explicitly opts in.

## Agent Roles

Public docs and templates should use generic role names:

- `planner`
- `implementer`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Provider names are adapter details.

Example:

```yaml
agents:
  implementer:
    provider: codex
    binary: codex
    skill_name: llm-handoff
    resume: true
```

## Provider Fields

Common fields:

- `provider`: adapter family such as `codex`, `gemini`, or `claude`.
- `binary`: command name or absolute executable path.
- `resume`: whether the adapter may reuse a prior session.
- `timeout_ms`: process timeout for a single dispatch.
- `retries`: provider retry count where supported.

Codex-specific fields:

- `skill_name`: Codex skill to invoke in the target repo.

Claude-specific fields:

- `model`: Claude model for subagent or validator calls.
- `permissions_flag`: optional CLI permission flag.

Gemini-specific fields:

- `mention`: optional agent mention used by Gemini agent files.
- `use_api_key_env`: whether to preserve Gemini API key environment variables.

## Prompt Templates

Prompt templates should be bundled with the package and overrideable from the
target repo.

Planned config:

```yaml
prompts_dir: .llm-handoff/prompts
```

If omitted, package defaults should be used.

## Frontmatter Contract

A handoff begins with YAML frontmatter:

```markdown
---
next_agent: planner
reason: Scope the first implementation task.
producer: user
---
```

Expected keys:

- `next_agent`: required role name.
- `reason`: required short routing reason.
- `producer`: role or actor that wrote the handoff.
- `scope_sha`: optional commit SHA related to the completed work.
- `prior_sha`: optional prior handoff or work commit.
- `close_type`: optional workflow close marker.

The dispatcher should fail closed on invalid or ambiguous frontmatter.
