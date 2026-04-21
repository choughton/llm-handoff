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

The current scaffold is not a release-ready package yet. See
[CHANGELOG.md](CHANGELOG.md) for the extraction state.

## Table Of Contents

### This README

- [What This Is](#what-this-is)
- [What This Is Not](#what-this-is-not)
- [How It Works](#how-it-works)
- [Documentation Files](#documentation-files)
- [Known Limitations](#known-limitations)
- [License](#license)

### Documentation Files

| File | Purpose |
| --- | --- |
| [README.md](README.md) | Public front door and project positioning. |
| [INSTALL.md](INSTALL.md) | Install paths, provider CLI checks, and first run. |
| [CONFIGURATION.md](CONFIGURATION.md) | Planned `dispatch_config.yaml` surface. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map and design choices. |
| [docs/TESTING.md](docs/TESTING.md) | Test strategy and current scaffold state. |
| [examples/reference-workflow/README.md](examples/reference-workflow/README.md) | Copyable workflow protocol plan. |
| [AGENTS.md](AGENTS.md) | Instructions for coding agents working in this repo. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution scope and project boundaries. |
| [SECURITY.md](SECURITY.md) | Safe usage and vulnerability reporting. |
| [CHANGELOG.md](CHANGELOG.md) | Release and extraction history. |

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

## How It Works

`llm-handoff` treats model output as untrusted input.

1. The dispatcher reads a handoff file.
2. YAML frontmatter declares the next role.
3. A deterministic router proposes the next dispatch.
4. The configured provider CLI runs that role.
5. The updated handoff is validated.
6. The loop continues, pauses, or escalates to the user.

The handoff file is the mutex and the debugger. There is no hidden queue,
database, or dashboard required to understand the current state.

## Documentation Files

The table of contents above links every Markdown document in this repository.
The most important next reads are [INSTALL.md](INSTALL.md),
[CONFIGURATION.md](CONFIGURATION.md), and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Known Limitations

- The reference implementation has been validated primarily in one workflow.
- Provider CLI behavior can change underneath the dispatcher.
- Validator calls optimize for correctness over token cost.
- Dual-run protection and semantic SHA checks are required before public launch.
- The current extraction still contains project-specific names that must be
  removed before release.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design constraints in
more detail.

## License

Apache License 2.0. See [LICENSE](LICENSE).
