# Configuration

`llm-handoff` is expected to use a YAML config file named
`dispatch_config.yaml` in the target repository.

This document describes the public configuration surface currently parsed by
the source checkout. Full role-to-provider adapter wiring is still being
ported. See
[dispatch_config.example.yaml](dispatch_config.example.yaml) for a copyable
example shape.

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

  backend:
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
- `backend`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Provider names are adapter details.

Example:

```yaml
agents:
  backend:
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

## Next-Agent Normalizer

The dispatcher has an internal next-agent normalizer for non-canonical
`next_agent` values. The deterministic enum check runs first. If the value is
not a supported role, the dispatcher may make a small model call to normalize
obvious freeform values to one of the public roles.

The normalizer is intentionally constrained:

- it can only return a canonical role such as `backend`, `frontend`, or
  `auditor`;
- it should return `unknown` when intent is ambiguous;
- `unknown` fails closed and routes to validation or user intervention instead
  of guessing.

Config shape:

```yaml
normalizer:
  provider: claude
  model: claude-haiku-4-5
  timeout_ms: 60000
  on_unknown: fail_closed
```

The public contract should not require Claude Haiku. A target repository should
be able to choose a small, low-latency model from an available provider, such
as Gemini Flash or an OpenAI mini model, as long as the adapter enforces the
same canonical-role-or-unknown output contract.

The current code scaffold implements the Claude API and Claude CLI paths first.
Provider adapters for Gemini or OpenAI normalizer calls are planned adapter
work.

The normalizer has two runtime auth paths:

- API key path: when the configured provider has an API key available, the
  dispatcher should call the provider API with a structured Pydantic output
  schema.
- CLI auth path: when no API key is available, the dispatcher may fall back to
  the configured provider CLI session, such as Claude Code OAuth.

The API path should not silently fall back to CLI auth after selecting an API
key. If the structured API call fails, the dispatcher should fail closed and
leave the handoff unchanged for validation.

There is intentionally no `enabled` switch. The normalizer is part of the
routing pipeline: exact enum matches remain deterministic, obvious freeform
values get one constrained normalization attempt, and ambiguity fails closed.

## Prompt Templates

Prompt templates should be stored in the source tree and overrideable from the
target repo.

Planned config:

```yaml
prompts_dir: .llm-handoff/prompts
```

If omitted, source-tree defaults should be used.

## Frontmatter Contract

A handoff begins with YAML frontmatter:

```markdown
---
next_agent: planner
reason: "Scope the first implementation task."
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
