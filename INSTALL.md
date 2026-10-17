# Installation

How to get `llm-handoff` onto your machine: the requirements, installing it as
a package, running it from a source checkout, the `doctor` preflight, the
safety defaults every install turns on, and how to debug a run or run the
test suite. The first-run steps (initialize a target repository, write a
handoff, dry run, run) are in the [README quick start](README.md#quick-start),
with per-platform commands in [QUICKSTART.md](QUICKSTART.md).

`llm-handoff` is in pre-release extraction and is not yet published to PyPI.

## Requirements

- Python 3.11 or newer.
- Git.
- One or more provider CLIs, depending on which roles your
  `dispatch_config.yaml` maps to them. The reference mapping puts the
  `planner` and `frontend` on Gemini CLI, the `backend` on Codex CLI, and the
  `auditor`, `validator`, and `finalizer` on Claude Code. A single-vendor
  setup (`init --provider claude` or `--provider codex`) needs only that one
  CLI.
- Docker or Podman on `PATH` for any role mapped to Gemini CLI (macOS can use
  `sandbox-exec`). Gemini roles run in its container sandbox and the
  dispatcher refuses to start without one.
- Optional: GitHub CLI (`gh`), signed in (`gh auth status`), for
  `publish.mode: pr` and repository checks.

Provider auth must already be valid before the dispatcher invokes a provider
CLI. The dispatcher orchestrates provider CLIs. It does not install them,
configure their accounts, or manage their API keys. The version-check
commands are in [QUICKSTART.md](QUICKSTART.md#0-check-prerequisites);
`llm-handoff doctor` (below) reports which configured CLIs are on `PATH`.

## Install As A Package

The repository ships a `pyproject.toml`, so you can install it from a checkout
or straight from Git.

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
its hidden `.claude/`, `.codex/`, `.gemini/`, `.agents/`, `.geminiignore`, and
`.gitignore` paths, so `llm-handoff init` works from any directory.

## Run From A Source Checkout

Clone the dispatcher somewhere outside the target repository:

```bash
git clone https://github.com/choughton/llm-handoff.git
cd llm-handoff
```

Create a virtual environment and install the source-checkout dependencies.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m llm_handoff --help
```

macOS or Linux:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m llm_handoff --help
```

When a command in this repository's docs is shown as `python -m ...`, Windows
PowerShell users who have not activated the environment should run it as
`.\.venv\Scripts\python.exe -m ...` instead. If you activate the environment
yourself, `python` is fine.

Run from source, `python -m llm_handoff` replaces `llm-handoff` in every
command, and `init` reads the template from `examples/reference-workflow/`
instead of the packaged copy. To work against a separate target repository
from the source checkout, pass `--repo-root` (and `--config` when
`dispatch_config.yaml` is not at that root). If `--config` is omitted, the
dispatcher looks for `dispatch_config.yaml` in the detected repo root.

## Verify Setup

Run the preflight check from inside the target repository, or point it at one
with `--repo-root` (and `--config` for a non-default `dispatch_config.yaml`):

```bash
llm-handoff doctor
llm-handoff doctor --repo-root path/to/your-project
```

From a source checkout, use `python -m llm_handoff doctor`. The command checks
that `git` is on PATH and the root is inside a Git repository, that each
configured role's provider CLI binary is on PATH, that Docker or Podman
(`sandbox-exec` on macOS) is available when a Gemini CLI role runs sandboxed,
and that the handoff file routes and validates. When a role runs on
Antigravity without `unsafe_bypass`, it also checks
`~/.gemini/antigravity-cli/settings.json` for the required `agy`
`permissions.allow` and `permissions.deny` entries and for `deny` or `ask`
rules that override them, reading that file only
(see [CONFIGURATION.md](CONFIGURATION.md)). It prints one `PASS`, `FAIL`,
or `WARN` line per check and a summary. It exits `0` when there are no errors
(warnings such as a dirty working tree or a missing handoff file are allowed)
and `1` when any check fails. It does not check provider sign-in, start a
dispatch, use the network, or write files.

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

## Debugging

When the loop pauses, inspect:

```text
docs/handoff/HANDOFF.md
PROJECT_STATE.md
logs/dispatch/
git log --oneline
```

The handoff file is the live state. Git is the durable history. See
[Common Pauses](WALKTHROUGH.md#common-pauses) for what each pause means.

## Running Tests

```bash
python -m pytest tests -q
```

The current scaffold tests describe the genericization contract. See
[docs/TESTING.md](docs/TESTING.md).
