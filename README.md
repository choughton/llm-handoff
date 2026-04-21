# llm-handoff

`llm-handoff` is a reference implementation of a file-based dispatch loop for
multi-CLI AI coding workflows.

The design principle is simple:

> Prompts are advisory. Validators are authoritative.

Agents can write prose, but the dispatcher only advances when the handoff state
parses, routes, and validates. A single Markdown file is the shared state file.
Git commit SHAs are the durable record of completed work. When routing is
ambiguous or unsafe, the dispatcher fails closed and pauses instead of guessing.

## Status

This repository is in pre-release extraction. The dispatcher is being
genericized from a project-specific implementation into a public reference
workflow. Expect names, configuration, and examples to change until the first
tagged release.

## What This Is

- A Python CLI for serially dispatching work between AI coding CLIs.
- A file-based operating protocol centered on `HANDOFF.md`.
- A deterministic router with validator-backed failure handling.
- A reference workflow that users can inspect, copy, fork, or adapt.

## What This Is Not

- Not a general multi-agent framework.
- Not parallel execution across branches or worktrees.
- Not a hosted service.
- Not a replacement for Codex, Gemini, Claude, or any other agent CLI.
- Not self-healing. It detects, validates, and pauses.
- Not production-certified for arbitrary repositories.

## Core Mechanism

`llm-handoff` treats model output as untrusted input.

1. The dispatcher reads a handoff file.
2. YAML frontmatter declares the next role.
3. A deterministic router proposes the next dispatch.
4. The configured provider CLI runs that role.
5. The updated handoff is validated.
6. The loop continues, pauses, or escalates to the user.

The default handoff path is:

```text
docs/handoff/HANDOFF.md
```

A flat root-level handoff is also supported through configuration:

```yaml
handoff_path: HANDOFF.md
```

## Why A File?

The handoff file is the mutex and the debugger. If the loop gets stuck, inspect:

```text
docs/handoff/HANDOFF.md
logs/dispatch/
git log --oneline
```

There is no hidden queue, database, or dashboard required to understand the
current state.

## Planned Quick Start

The intended source workflow is:

```bash
git clone https://github.com/choughton/llm-handoff.git
cd llm-handoff
python -m venv .venv
python -m pip install -e ".[dev]"
llm-handoff --help
```

The intended target-repo workflow is:

```bash
cd path/to/your-project
llm-handoff init --template reference-workflow
llm-handoff --config dispatch_config.yaml --dry-run
llm-handoff --config dispatch_config.yaml
```

The `init` command is planned as part of the public extraction. Until then, the
reference workflow templates will live under `examples/reference-workflow/`.

## Reference Workflow Shape

The public template should include:

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

These files are part of the mechanism. The dispatcher is not just Python code;
it is Python code plus a written protocol that tells agents how to cooperate.

## Handoff Frontmatter

A minimal handoff starts like this:

```markdown
---
next_agent: planner
reason: Scope the first implementation task.
producer: user
---

# Task Assignment

## Objective

Inspect this repository and propose the first small implementation task.

## Acceptance Criteria

- Identify the repo structure.
- Write a concrete handoff for the implementer.
- Include clear next_agent frontmatter.
```

Canonical public role names are expected to be:

- `planner`
- `implementer`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Provider CLIs are implementation details configured separately.

## Configuration Direction

The planned config shape is:

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

`auto_push` should remain `false` by default. The dispatcher may help create
local commits, but publishing to a remote should be an explicit user decision.

## Design Constraints

- Serial execution is intentional.
- Git is a required part of the state model.
- Handoff validation should fail closed on parse errors.
- Ambiguous routing should pause or dispatch a validator, not guess.
- The public workflow must not depend on project-specific names or documents.

## Known Limitations

- The reference implementation has been validated primarily in one workflow.
- Provider CLI behavior can change underneath the dispatcher.
- Validator calls optimize for correctness over token cost.
- Dual-run protection and semantic SHA checks are required before public launch.
- The current extraction still contains project-specific names that must be
  removed before release.

## License

Apache License 2.0. See [LICENSE](LICENSE).
