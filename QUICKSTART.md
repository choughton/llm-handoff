# Quick Start Command Sheet

The five steps of the [README quick start](README.md#quick-start) with the
commands written out for Windows PowerShell and for macOS or Linux. The README
explains each step; this page only lists what to type. Replace
`C:\path\to\your-project` or `/path/to/your-project` with your target
repository.

This is a 0.x release. Read the [README status](README.md#status)
before using it against a repository with sensitive or production work.

## 0. Check Prerequisites

Python 3.11 or newer, Git, and the provider CLIs your roles will use. The
same commands work in PowerShell and in a POSIX shell; skip the line for any
CLI no role is mapped to:

```bash
python --version
git --version
codex --version
claude --version
agy --version
```

The dispatcher launches these CLIs. It does not install them or sign them in.
Agents run with an allowlisted environment and permission checks on; see [Agent Safety Defaults](README.md#agent-safety-defaults). On Windows,
run the dispatcher inside WSL2 or a container with no push credential.

## 1. Install

Windows PowerShell:

```powershell
pipx install llm-handoff
llm-handoff --help
```

macOS or Linux:

```bash
pipx install llm-handoff
llm-handoff --help
```

Without `pipx`, `python -m pip install llm-handoff` inside a virtual
environment works too. If you installed into a virtual environment that is not activated, call the
command by path: `.\.venv\Scripts\llm-handoff.exe` on Windows,
`.venv/bin/llm-handoff` on macOS or Linux.

To run from a source checkout instead, follow
[INSTALL.md](INSTALL.md#run-from-a-source-checkout) and replace `llm-handoff`
in every command below with `python -m llm_handoff`
(`.\.venv\Scripts\python.exe -m llm_handoff` in PowerShell when the
environment is not activated).

## 2. Initialize The Target Repository

Windows PowerShell:

```powershell
llm-handoff init C:\path\to\your-project --dry-run
llm-handoff init C:\path\to\your-project
```

macOS or Linux:

```bash
llm-handoff init /path/to/your-project --dry-run
llm-handoff init /path/to/your-project
```

Add `--provider claude`, `--provider codex`, or `--provider antigravity` to
put every role on one CLI.
To track work in Linear, read
[Files Or Linear](README.md#files-or-linear-where-work-is-tracked) first and
add the `--tracker` flags to the second command.

## 3. Run The Preflight Check

Windows PowerShell:

```powershell
llm-handoff doctor --repo-root C:\path\to\your-project
```

macOS or Linux:

```bash
llm-handoff doctor --repo-root /path/to/your-project
```

Fix every `FAIL` before continuing.

## 4. Tell The Planner What To Build

In the target repository, edit:

```text
docs/handoff/HANDOFF.md
PROJECT_STATE.md
```

The starter handoff text is in
[README step 4](README.md#4-tell-the-planner-what-to-build).

## 5. Dry Run, Then Run

Windows PowerShell:

```powershell
llm-handoff --repo-root C:\path\to\your-project --dry-run
llm-handoff --repo-root C:\path\to\your-project
```

macOS or Linux:

```bash
llm-handoff --repo-root /path/to/your-project --dry-run
llm-handoff --repo-root /path/to/your-project
```

Add `--config <path>` when `dispatch_config.yaml` is not at the target
repository root.

To stop cleanly, from a second terminal:

Windows PowerShell:

```powershell
llm-handoff stop --repo-root C:\path\to\your-project
```

macOS or Linux:

```bash
llm-handoff stop --repo-root /path/to/your-project
```

## Next Reading

- [README.md](README.md#quick-start) explains each step above.
- [WALKTHROUGH.md](WALKTHROUGH.md) explains how the two repositories, the
  roles, and the copied files fit together.
- [INSTALL.md](INSTALL.md) covers the source checkout, `doctor`, and
  debugging.
- [CONFIGURATION.md](CONFIGURATION.md) covers `dispatch_config.yaml`.
