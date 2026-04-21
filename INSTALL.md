# Installation

`llm-handoff` is in pre-release extraction. These instructions describe the
intended install and first-run workflow. Some commands will become active once
dependency setup and config loading are finalized.

## Requirements

- Python 3.11 or newer.
- Git.
- One or more provider CLIs, depending on your workflow:
  - Codex CLI for implementer-style dispatches.
  - Gemini CLI for planner or frontend-style dispatches.
  - Claude Code CLI for auditor, validator, or finalizer-style dispatches.
- Optional: GitHub CLI (`gh`) for repository checks and release workflow.

The dispatcher orchestrates provider CLIs. It does not install them, configure
their accounts, or manage their API keys.

## Clone From Source

```bash
git clone https://github.com/choughton/llm-handoff.git
cd llm-handoff
```

## Create A Virtual Environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

macOS or Linux:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
```

## Install Local Dependencies

Planned source-checkout dependency install:

```bash
python -m pip install -r requirements-dev.txt
```

No PyPI distribution is planned. This repository is intended to be cloned and
run from source. If a `pyproject.toml` is added later, it should support local
tooling and tests, not package publication.

## Check Provider CLIs

Run the commands for the providers you intend to use:

```bash
codex --version
gemini --version
claude --version
```

If you use GitHub workflows:

```bash
gh auth status
```

Provider auth must already be valid before the dispatcher invokes a provider
CLI.

## Initialize A Target Repository

Planned workflow:

```bash
cd path/to/your-project
python -m llm_handoff init --template reference-workflow
```

The init command should copy a reference protocol into the target repository,
including:

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

Until `init` exists, use `examples/reference-workflow/` as the source template
for the written protocol.

## Dry Run

Planned command:

```bash
python -m llm_handoff --config dispatch_config.yaml --dry-run
```

A dry run should parse the handoff, resolve the configured role, and report the
provider CLI it would invoke without launching the agent.

## Run The Loop

Planned command:

```bash
python -m llm_handoff --config dispatch_config.yaml
```

The dispatcher reads the handoff file, invokes one role, validates the updated
handoff, then continues or pauses.

## Debugging

When the loop pauses, inspect:

```text
docs/handoff/HANDOFF.md
PROJECT_STATE.md
logs/dispatch/
git log --oneline
```

The handoff file is the live state. Git is the durable history.

## Running Tests

```bash
python -m pytest tests -q
```

The current scaffold tests intentionally include failing tests that describe the
next genericization targets. See [docs/TESTING.md](docs/TESTING.md).
