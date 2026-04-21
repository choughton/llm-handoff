# Installation

`llm-handoff` is in pre-release extraction. These instructions describe the
source-checkout workflow and the target-repo initialization path.

For a shorter first-run path, see [QUICKSTART.md](QUICKSTART.md). For a more
detailed source-checkout and target-repo explanation, see
[WALKTHROUGH.md](WALKTHROUGH.md).

## Requirements

- Python 3.11 or newer.
- Git.
- One or more provider CLIs, depending on your workflow:
  - Codex CLI for backend-style dispatches.
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

When a later command is shown as `python -m ...`, Windows PowerShell users who
have not activated the environment should run it as
`.\.venv\Scripts\python.exe -m ...` instead. If you activate the environment
yourself, `python` is fine.

macOS or Linux:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
```

## Install Local Dependencies

Source-checkout dependency install:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

macOS or Linux:

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

From the `llm-handoff` source checkout, preview the reference workflow files
that would be copied into a target repository:

```bash
python -m llm_handoff init path/to/your-project --template reference-workflow --dry-run
```

Then initialize the target repository:

```bash
python -m llm_handoff init path/to/your-project --template reference-workflow
```

The initializer copies:

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

It does not copy the template README into the target repo. Existing identical
files are skipped. Existing changed files abort initialization unless you pass
`--force` after reviewing what would be overwritten.

You can still copy or fork the files under
[examples/reference-workflow](examples/reference-workflow/README.md) manually
if you want a custom protocol layout.

## Dry Run

```bash
python -m llm_handoff --config dispatch_config.yaml --dry-run
```

A dry run parses the handoff, resolves the configured role, and reports the
provider CLI it would invoke without launching the agent.

## Run The Loop

```bash
python -m llm_handoff --config dispatch_config.yaml
```

The dispatcher reads the handoff file, invokes one role, validates the updated
handoff, then continues or pauses.

When running against a separate target repository before packaging support
exists, run with the source checkout on `PYTHONPATH` or from the source checkout
itself while pointing the config paths at the target repo.

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

The current scaffold tests describe the genericization contract. See
[docs/TESTING.md](docs/TESTING.md).
