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
- `llm_handoff.router`: handoff parsing and route selection.
- `llm_handoff.validator`: post-dispatch handoff validation.
- `llm_handoff.orchestrator`: dispatch loop and failure-mode handling.
- `llm_handoff.agents`: provider CLI invocation and stream handling.
- `llm_handoff.handoff_normalizer`: frontmatter `next_agent` normalization.
- `llm_handoff.ledger`: finalizer-style close flow from the source project.
- `llm_handoff.logging_util`: dispatch log writer.
- `llm_handoff.text_io`: robust text decoding for handoff files.

The current scaffold still contains source-project assumptions in several of
these modules. The next extraction pass should move those assumptions into
config, prompts, or examples.

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
- planner self-loop;
- agent self-loop;
- missing route after dispatch;
- malformed validator output;
- malformed close/finalizer output.

When a failure mode is detected, the safe default is to pause or dispatch a
validator role with a scoped prompt.

## Provider CLIs

Provider CLIs are adapters, not workflow roles. A public config should map
generic roles such as `planner` or `implementer` to concrete providers such as
Gemini, Codex, or Claude.

The dispatcher does not sandbox these tools and does not manage their auth.

## Non-Goals

- Parallel execution.
- Worktree orchestration.
- Pull request automation as a core requirement.
- Non-Git state backends.
- Hosted service mode.
- General plugin framework.
