# Quickstart

This is the shortest source-checkout path for trying `llm-handoff` against a
target repository.

`llm-handoff` is not published to PyPI. Clone this repo, run it from source, and
point it at the repository you want to coordinate.

This is pre-release software. Read the [README status](README.md#status) before
using it against a repository with sensitive or production work.

## 1. Clone The Dispatcher

Windows PowerShell:

```powershell
cd $env:USERPROFILE\Downloads
git clone https://github.com/choughton/llm-handoff.git
cd llm-handoff
```

macOS or Linux:

```bash
cd ~/Downloads
git clone https://github.com/choughton/llm-handoff.git
cd llm-handoff
```

## 2. Create The Python Environment

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

## 3. Check Provider CLIs

Run the checks for the providers your workflow will use:

```bash
codex --version
gemini --version
claude --version
```

The dispatcher launches provider CLIs. It does not install them or configure
their accounts.

## 4. Initialize A Target Repo

Preview the files that would be copied:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m llm_handoff init C:\path\to\your-project --template reference-workflow --dry-run
```

macOS or Linux:

```bash
python -m llm_handoff init /path/to/your-project --template reference-workflow --dry-run
```

If the dry run looks right, initialize the target repo:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m llm_handoff init C:\path\to\your-project --template reference-workflow
```

macOS or Linux:

```bash
python -m llm_handoff init /path/to/your-project --template reference-workflow
```

The initializer copies the reference protocol files into the target repo,
including `AGENTS.md`, `PROJECT_STATE.md`, `dispatch_config.yaml`,
`docs/handoff/HANDOFF.md`, provider agent templates, and provider ignore files.

## 5. Edit The First Handoff

In the target repo, review and edit:

```text
PROJECT_STATE.md
dispatch_config.yaml
docs/handoff/HANDOFF.md
```

Start with a small planner assignment:

```markdown
---
next_agent: planner
reason: Scope the first bounded implementation task.
producer: user
---

# Initial Handoff

Read the repository instructions and project state, then write the first
dispatchable task assignment.
```

## 6. Dry Run Dispatch

From the `llm-handoff` source checkout, point the dispatcher at the target repo:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m llm_handoff --repo-root C:\path\to\your-project --config C:\path\to\your-project\dispatch_config.yaml --dry-run
```

macOS or Linux:

```bash
python -m llm_handoff --repo-root /path/to/your-project --config /path/to/your-project/dispatch_config.yaml --dry-run
```

The dry run should report the route and provider it would invoke without
launching an agent.

## 7. Run One Real Cycle

When provider auth is ready and the dry run is clean:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m llm_handoff --repo-root C:\path\to\your-project --config C:\path\to\your-project\dispatch_config.yaml
```

macOS or Linux:

```bash
python -m llm_handoff --repo-root /path/to/your-project --config /path/to/your-project/dispatch_config.yaml
```

The dispatcher reads `docs/handoff/HANDOFF.md`, runs one role at a time, checks
the updated handoff, and continues or pauses.

## Next Reading

- [INSTALL.md](INSTALL.md) for dependency and command details.
- [WALKTHROUGH.md](WALKTHROUGH.md) for a slower first-run explanation.
- [CONFIGURATION.md](CONFIGURATION.md) for `dispatch_config.yaml`.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the dispatch loop design.
