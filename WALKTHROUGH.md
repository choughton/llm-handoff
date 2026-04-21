# Walkthrough

This walkthrough explains the first-run flow in more detail than
[QUICKSTART.md](QUICKSTART.md). It assumes you want to understand how the source
checkout, target repo, provider CLIs, and handoff files fit together.

## The Two-Repos Mental Model

There are usually two directories involved:

```text
llm-handoff/       # source checkout for the dispatcher
your-project/      # target repository where agents do work
```

The dispatcher source checkout contains the Python loop and reference templates.
The target repository receives the workflow files the agents will read and
write.

The dispatcher does not hide state in a database. The target repo owns the live
state:

```text
PROJECT_STATE.md
docs/handoff/HANDOFF.md
git log
```

`HANDOFF.md` is the current route. Git commits are the durable record.

## Step 1. Install The Dispatcher Source Checkout

Clone the dispatcher somewhere outside the target repo:

```bash
git clone https://github.com/choughton/llm-handoff.git
cd llm-handoff
```

Create a virtual environment and install the source-checkout dependencies:

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
python -m llm_handoff --help
```

On Windows PowerShell, use the venv interpreter directly if the environment is
not activated:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m llm_handoff --help
```

## Step 2. Confirm Provider CLIs

The reference workflow maps generic roles to provider CLIs:

| Role | Typical Provider |
| --- | --- |
| `planner` | Gemini |
| `backend` | Codex |
| `frontend` | Gemini or manual frontend work |
| `auditor` | Claude |
| `validator` | Claude |
| `finalizer` | Claude |

These are adapter choices, not handoff protocol names. Handoffs should use the
generic roles.

Check the CLIs you plan to use:

```bash
codex --version
gemini --version
claude --version
```

If a provider CLI is not authenticated, fix that before running a real dispatch.
The dispatcher invokes provider tools; it does not manage their login flow.

## Step 3. Initialize The Target Repository

From the dispatcher source checkout, preview the reference workflow copy:

```bash
python -m llm_handoff init /path/to/your-project --template reference-workflow --dry-run
```

Then copy the files:

```bash
python -m llm_handoff init /path/to/your-project --template reference-workflow
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

Existing identical files are skipped. Existing changed files stop the init
unless you pass `--force` after reviewing the conflict.

## Step 4. Review The Target Repo Files

Open the target repo and inspect:

```text
AGENTS.md
PROJECT_STATE.md
dispatch_config.yaml
docs/handoff/HANDOFF.md
docs/handoff/README.md
```

The important contract is:

- `AGENTS.md` tells agents how the workflow operates.
- `PROJECT_STATE.md` stores durable project context.
- `dispatch_config.yaml` maps generic roles to provider CLIs.
- `docs/handoff/HANDOFF.md` declares the next route.
- `docs/handoff/*.md` files are startup and handoff prompts for each role.

## Step 5. Write The First Dispatchable Handoff

A dispatchable handoff starts with YAML frontmatter:

```markdown
---
next_agent: planner
reason: Scope the first bounded implementation task.
producer: user
---
```

The body should give the next role enough context to act:

```markdown
# Initial Handoff

Read AGENTS.md, PROJECT_STATE.md, and the current repository state. Then write a
small backend, frontend, audit, or finalizer assignment in
docs/handoff/HANDOFF.md.
```

The dispatcher treats frontmatter as authoritative. Body prose is context for
humans and agents.

## Step 6. Run A Dry Dispatch

From the `llm-handoff` source checkout, run a dry dispatch while pointing the
dispatcher at the target repo.

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m llm_handoff --repo-root C:\path\to\your-project --config C:\path\to\your-project\dispatch_config.yaml --dry-run
```

macOS or Linux:

```bash
python -m llm_handoff --repo-root /path/to/your-project --config /path/to/your-project/dispatch_config.yaml --dry-run
```

If you prefer to run commands from inside the target repo before package
installation exists, set `PYTHONPATH` to the `llm-handoff` source checkout.

Expected result:

- the dispatcher reads `docs/handoff/HANDOFF.md`;
- the router identifies `next_agent`;
- the CLI logs the role it would dispatch;
- no provider agent is launched.

If dry run reports `unknown`, inspect the frontmatter first. `next_agent` must
be one of:

```text
planner
backend
frontend
auditor
validator
finalizer
user
```

## Step 7. Run A Real Cycle

When dry run is clean and provider auth is ready, run without `--dry-run`:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m llm_handoff --repo-root C:\path\to\your-project --config C:\path\to\your-project\dispatch_config.yaml
```

macOS or Linux:

```bash
python -m llm_handoff --repo-root /path/to/your-project --config /path/to/your-project/dispatch_config.yaml
```

The loop is serial:

```text
HANDOFF.md declares next_agent
        |
        v
dispatcher invokes one role
        |
        v
role updates HANDOFF.md
        |
        v
dispatcher validates the updated handoff
        |
        v
continue, pause, or escalate
```

## Step 8. Inspect State After A Cycle

After each cycle, inspect:

```bash
git status --short
git log --oneline -5
```

Then inspect:

```text
docs/handoff/HANDOFF.md
PROJECT_STATE.md
logs/dispatch/
```

The handoff should name the next role and include enough evidence for the next
agent to proceed. If the dispatcher pauses, it should log why.

## Common Pauses

`unknown` route:

The dispatcher could not parse a safe next role. Fix `next_agent` or route to
`user`.

Invalid frontmatter:

The YAML block is present but missing required routing fields such as `reason`.
The dispatcher can recover from narrow hygiene failures, but malformed state is
still treated as untrusted input.

Provider CLI failure:

Check provider auth, command availability, and the configured binary in
`dispatch_config.yaml`.

Self-loop:

The same role wrote a handoff back to itself. The validator decides whether this
is a valid pause or a malformed transition.

## Where To Go Next

- [CONFIGURATION.md](CONFIGURATION.md) for config fields.
- [docs/handoff/README.md](docs/handoff/README.md) for handoff format.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design constraints.
- [docs/TESTING.md](docs/TESTING.md) for local verification.
