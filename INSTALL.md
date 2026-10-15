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
- Docker or Podman on `PATH` for any role mapped to Gemini CLI (macOS can use
  `sandbox-exec`). Gemini roles run in its container sandbox and the
  dispatcher refuses to start without one.
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

## Install The Package From A Git Checkout

The repository ships a `pyproject.toml`, so you can install it as a package
from a checkout or straight from Git. It is not yet published to PyPI.

From a local checkout:

```bash
python -m pip install .
```

Or straight from Git:

```bash
python -m pip install git+https://github.com/choughton/llm-handoff.git
```

Either install puts an `llm-handoff` console command on `PATH` for that
environment. `llm-handoff ...` and `python -m llm_handoff ...` run the same
CLI. The wheel bundles the `examples/reference-workflow/` template, including
its hidden `.claude/`, `.codex/`, `.gemini/`, `.geminiignore`, and `.gitignore`
paths, so `llm-handoff init` works outside the source checkout. Run from source
instead, `init` reads the template from `examples/reference-workflow/`.

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

## Safety Defaults

Agents launch with an allowlisted environment, permission checks and sandboxes
on, and protected control files that stop the loop when a role edits them
outside its Work Packet. Codex users who authenticate with an API key instead
of `codex login` must set `use_api_key_env: true` on the Codex roles.

The per-role opt-outs are `unsafe_bypass: true`, `env_mode: inherit`, and the
repo-wide `protected_paths: []`. Each one removes a protection. See
[Agent Safety Defaults](README.md#agent-safety-defaults) and
[CONFIGURATION.md](CONFIGURATION.md#launch-permissions-and-sandboxing).

On Windows, run the dispatcher inside WSL2 or a container that has no push
credential. Claude Code's Bash sandbox needs WSL2, and the Codex native
Windows sandbox is experimental. See the
[threat model](SECURITY.md#threat-model).

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

The initializer recursively copies the reference workflow template from
[`examples/reference-workflow/`](examples/reference-workflow/). That directory
is the authoritative inventory of copied files. It includes the target-repo
`AGENTS.md`, `PROJECT_STATE.md`, `dispatch_config.yaml`, handoff prompt
templates, provider agent templates, provider policy files, and provider ignore
files.

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

If `--config` is omitted, the dispatcher looks for `dispatch_config.yaml` in
the detected repo root.

## Run The Loop

```bash
python -m llm_handoff --config dispatch_config.yaml
```

The dispatcher reads the handoff file, invokes one role, validates the updated
handoff, then continues or pauses.

To run against a separate target repository, either install the package (see
above) and run `llm-handoff` from anywhere, or run from the source checkout
while pointing `--repo-root` and `--config` at the target repo.

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
