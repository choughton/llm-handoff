# Architecture

`llm-handoff` is built around one operational choice: a Markdown handoff file is
the shared state between agents.

The dispatcher is intentionally serial. One role runs at a time, writes the next
handoff, and exits. The dispatcher validates the result before the next role can
run.

## Design Principles

- Prompts are advisory.
- Validators are authoritative.
- Git is the durable state machine.
- Ambiguous routing fails closed.
- Human-readable state is preferred over hidden orchestration state.

## Core Flow

```text
Read HANDOFF.md
Parse frontmatter
Route next_agent
Invoke provider CLI
Capture output and logs
Validate updated HANDOFF.md
Continue, pause, or escalate
```

## Module Map

- `llm_handoff.__main__`: CLI entry point.
- `llm_handoff.config`: dispatch configuration and repo-root detection.
- `llm_handoff.init_workflow`: target-repo reference workflow initialization.
- `llm_handoff.router`: handoff parsing and route selection.
- `llm_handoff.validator`: post-dispatch handoff validation.
- `llm_handoff.orchestrator`: dispatch loop and failure-mode handling.
- `llm_handoff.agents`: compatibility facade for agent invocation imports.
- `llm_handoff.agent_types`: shared dispatch result types.
- `llm_handoff.agent_process`: subprocess execution and path helpers.
- `llm_handoff.agent_streams`: shared CLI stream filtering.
- `llm_handoff.agent_providers.*`: concrete provider CLI adapters.
- `llm_handoff.agent_roles`: generic role-to-adapter invocation wrappers.
- `llm_handoff.handoff_normalizer`: frontmatter `next_agent` normalization
  control flow.
- `llm_handoff.normalizer_models`: normalizer schema and prompt contract.
- `llm_handoff.normalizer_providers.*`: model-provider normalizer adapters.
- `llm_handoff.ledger`: finalizer-style close flow for durable state updates.
- `llm_handoff.logging_util`: dispatch log writer.
- `llm_handoff.text_io`: robust text decoding for handoff files.

The current scaffold has the core config loader, router, validator,
normalizer, provider invocation layer, target-repo initializer, and prompt
templates. The remaining architectural work is to make every role-to-provider
choice configurable without changing the public role names.

## Handoff File

Default path:

```text
docs/handoff/HANDOFF.md
```

Supported flat path:

```text
HANDOFF.md
```

The handoff file contains two layers:

- YAML frontmatter for machine routing.
- Markdown body for human and agent context.

## Next-Agent Normalizer

The router treats canonical frontmatter as the authority. Supported public
roles are `planner`, `backend`, `frontend`, `auditor`, `validator`,
`finalizer`, and `user`.

If `next_agent` is not an exact enum match, the dispatcher can invoke an
internal next-agent normalizer backed by a configured small model. The current
scaffold uses Claude Haiku as the default implementation, but the public
configuration should allow equivalent low-latency models from other providers.
This is not a workflow role and not a general reasoning step. It is a
constrained resolver for obvious freeform values.

The normalizer has two execution paths. If a provider API key is available, it
uses a structured API call with the Pydantic `NormalizedNextAgent` schema. If
no API key is available, it can fall back to the configured provider CLI
session. Once the API path is selected, API errors fail closed instead of
silently switching to local OAuth state.

The first scaffold implements the Claude API and Claude CLI normalizer paths.
Provider adapters for other model families should keep the same schema and
failure contract.

Normalizer outcomes:

- canonical role: rewrite `next_agent` and continue through deterministic
  routing;
- `unknown`: fail closed and send the handoff to validation or user
  intervention;
- invalid model output: fail closed rather than inventing a route.

## Git As State

The dispatcher expects Git commit SHAs to appear in handoffs that claim
completed work. SHA validation should be both syntactic and semantic before the
public release:

- syntax: 7 to 40 hexadecimal characters;
- semantics: `git cat-file -e <sha>` succeeds in the target repo.

Git is not an interchangeable backend in this design. It is part of the
mechanism.

## Failure Modes

The dispatcher should name and isolate failure modes rather than guessing:

- unknown route;
- low-confidence route;
- stale handoff;
- handoff hygiene failure after dispatch;
- planner self-loop;
- agent self-loop;
- missing route after dispatch;
- malformed validator output;
- malformed close/finalizer output.

When a failure mode is detected, the safe default is to pause or dispatch a
validator role with a scoped prompt.

For narrow post-dispatch handoff hygiene failures, the dispatcher has a bounded
repair lane. The producer gets one repair-only dispatch constrained to
`docs/handoff/HANDOFF.md`; the repair must create a commit, change the handoff
hash, touch only the handoff file, preserve critical frontmatter, leave dirty
state unchanged except for the handoff, and pass validation. If that producer
repair fails, the planner gets one cleanup-only attempt. If the planner cannot
operationalize the handoff, the dispatcher aborts to the user.

## Provider CLIs

Provider CLIs are adapters, not workflow roles. A public config should map
generic roles such as `planner`, `backend`, or `frontend` to concrete providers
such as Gemini, Codex, or Claude.

The dispatcher does not sandbox these tools and does not manage their auth.

## Prompt And Agent Templates

The written protocol is part of the runtime surface. The source checkout ships:

- `docs/handoff/*.md` for shared handoff prompts and the live handoff template;
- `.codex/skills/llm-handoff/SKILL.md` for the backend role when served by
  Codex;
- `.gemini/agents/*.md` for planner and frontend role templates;
- `.claude/agents/*.md` for auditor, router, validator, and finalizer support.

These files are templates, not hidden state. A consuming repository can copy
them, fork them, or replace them as long as the frontmatter contract remains
valid.

## Non-Goals

- Parallel execution.
- Worktree orchestration.
- Pull request automation as a core requirement.
- Non-Git state backends.
- Hosted service mode.
- General plugin framework.
