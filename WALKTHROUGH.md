# Walkthrough

How the pieces of a first run fit together: the two directories involved, how
roles map to provider CLIs, every file `init` copies and what each one is for,
what a dry run should show, how to inspect state after a cycle, and what the
common pauses mean. The commands themselves are in the
[README quick start](README.md#quick-start), with per-platform variants in
[QUICKSTART.md](QUICKSTART.md).

## The Two-Repos Mental Model

There are usually two directories involved:

```text
llm-handoff/       # the dispatcher: an installed package or a source checkout
your-project/      # target repository where agents do work
```

The dispatcher contains the Python loop and the reference templates. The
target repository receives the workflow files the agents will read and write.
`--repo-root` tells the dispatcher which target repository to work in, and
`--config` defaults to `dispatch_config.yaml` in that root.

The dispatcher does not hide state in a database. The target repo owns the live
state:

```text
PROJECT_STATE.md
docs/handoff/HANDOFF.md
git log
```

`HANDOFF.md` is the current route. Git commits are the durable record.

## Roles And Providers

The reference workflow maps generic roles to provider CLIs:

| Role | Reference Provider |
| --- | --- |
| `planner` | Gemini CLI |
| `backend` | Codex CLI |
| `frontend` | Gemini CLI, or manual work with `--manual-frontend` |
| `auditor` | Claude Code |
| `validator` | Claude Code |
| `finalizer` | Claude Code |

These are adapter choices, not handoff protocol names. Handoffs use the generic
roles. `init` writes this mapping when more than one provider CLI is on
`PATH`; when only one of Claude Code, Codex, or Gemini CLI is installed, it
puts every role on that CLI, and `--provider claude`, `--provider codex`, or
`--provider gemini` forces that choice. Antigravity is also a registered
adapter. See [Agent Roles](CONFIGURATION.md#agent-roles) for the mapping
shape and the single-vendor variants, and the README's
[Gotchas](README.md#gotchas) for what all-Codex and all-Gemini setups need
in addition.

If a provider CLI is not signed in, fix that before running a real dispatch.
The dispatcher invokes provider tools; it does not manage their login flow.

## What `init` Copies

`llm-handoff init` copies everything under
[`examples/reference-workflow/`](examples/reference-workflow/) except that
directory's own `README.md`:

```text
AGENTS.md
PROJECT_STATE.md
dispatch_config.yaml
.gitignore
.geminiignore
docs/handoff/README.md
docs/handoff/HANDOFF.md
docs/handoff/HANDBOOK.md
docs/handoff/SHARED_REPO_INIT_PROMPT.md
docs/handoff/PLANNER_INITIAL_PROMPT.md
docs/handoff/PLANNER_HANDOFF_PROMPT.md
docs/handoff/BACKEND_HANDOFF_PROMPT.md
docs/handoff/FRONTEND_HANDOFF_PROMPT.md
docs/handoff/AUDITOR_HANDOFF_PROMPT.md
docs/handoff/FINALIZER_HANDOFF_PROMPT.md
.claude/agents/planner.md
.claude/agents/backend.md
.claude/agents/frontend.md
.claude/agents/auditor.md
.claude/agents/handoff-validator.md
.claude/agents/handoff-router.md
.claude/agents/ledger-updater.md
.claude/settings.json
.claude/hooks/block_dangerous_commands.py
.codex/skills/llm-handoff/SKILL.md
.codex/skills/llm-handoff/agents/openai.yaml
.codex/skills/llm-handoff-planner/SKILL.md
.codex/skills/llm-handoff-frontend/SKILL.md
.codex/skills/llm-handoff-auditor/SKILL.md
.codex/skills/llm-handoff-validator/SKILL.md
.codex/skills/llm-handoff-finalizer/SKILL.md
.codex/rules/llm-handoff.rules
.gemini/agents/planner.md
.gemini/agents/backend.md
.gemini/agents/frontend.md
.gemini/agents/auditor.md
.gemini/agents/validator.md
.gemini/agents/finalizer.md
.gemini/policies/planner_guardrails.toml
.agents/agents/planner.md
.agents/agents/backend.md
.agents/agents/frontend.md
.agents/agents/auditor.md
.agents/agents/handoff-validator.md
.agents/agents/handoff-router.md
.agents/agents/ledger-updater.md
.agents/agents/supervisor-repair.md
```

Four of these are merged rather than copied when the target already has them:
`AGENTS.md`, `.gitignore`, and `.geminiignore` get a managed block between
`llm-handoff:begin` and `llm-handoff:end` markers, and `.claude/settings.json`
gets the template's deny rules and `PreToolUse` hook added to your existing
settings. Every other existing identical file is skipped, and an existing
changed file stops the init unless you pass `--force` after reviewing the
conflict. `--force` overwrites non-managed files and keeps your content in the
managed ones.

`init` also chooses the role-to-provider mapping it writes into
`dispatch_config.yaml` (see [Roles And Providers](#roles-and-providers)) and,
with `--tracker linear`, adds a `tracker` block and the Linear prompt sections
(see [Files Or Linear](README.md#files-or-linear-where-work-is-tracked)).

Gemini CLI ignores policy files in a project's `.gemini/policies/` directory
today. To enforce the shipped Gemini guardrails, copy
`.gemini/policies/planner_guardrails.toml` into `~/.gemini/policies/`
(Windows: `%USERPROFILE%\.gemini\policies\`). See the Guardrails section of
[examples/reference-workflow/README.md](examples/reference-workflow/README.md).

You can also copy or fork the template files by hand if you want a custom
protocol layout. The template README describes the role and handoff contracts
the files must keep agreeing on.

## What Each Target-Repo File Is For

After `init`, open the target repo and inspect:

```text
AGENTS.md
PROJECT_STATE.md
dispatch_config.yaml
docs/handoff/HANDOFF.md
docs/handoff/README.md
```

The contract is:

- `AGENTS.md` tells agents how the workflow operates.
- `PROJECT_STATE.md` stores durable project context: the active epic,
  blockers, and the completed-scope ledger the finalizer updates.
- `dispatch_config.yaml` maps generic roles to provider CLIs and holds the
  gate, scanner, publishing, and tracker settings.
- `docs/handoff/HANDOFF.md` declares the next route.
- The other `docs/handoff/*.md` files are the startup and handoff prompts for
  each role, the shared `HANDBOOK.md` rules, and the format guide in
  `docs/handoff/README.md`.
- `.claude/`, `.codex/`, `.gemini/`, and `.agents/` hold each provider CLI's
  native agent files, skills, and guardrails for the same roles. The
  dispatcher's prompt names the role; the provider file carries that role's
  standing instructions.
- The `.gitignore` and `.geminiignore` blocks keep `logs/dispatch/` and the
  dispatch lock out of Git and out of Gemini CLI's context.

`AGENTS.md`, the handoff prompts, `dispatch_config.yaml`, and the provider
directories are protected control files: a role that edits them without
listing the path in its Work Packet stops the loop. See
[Protected Control Files](CONFIGURATION.md#protected-control-files).

## The First Handoff

A dispatchable handoff starts with YAML frontmatter that names `next_agent`,
`reason`, and `producer`. The body gives the next role enough context to act.
The starter text is in
[README step 4](README.md#4-tell-the-planner-what-to-build); the full field
and status reference is in [docs/handoff/README.md](docs/handoff/README.md).

The dispatcher treats frontmatter as authoritative. Body prose is context for
humans and agents. `next_agent` must be one of:

```text
planner
backend
frontend
auditor
validator
finalizer
user
```

## What A Dry Run Shows

A dry run (`--dry-run`) parses the handoff, resolves the configured role, and
reports the provider CLI it would invoke. Expected result:

- the dispatcher reads `docs/handoff/HANDOFF.md`;
- the router identifies `next_agent`;
- the CLI logs the role and provider it would dispatch;
- no provider agent is launched.

If the dry run reports `unknown`, inspect the frontmatter first and check
`next_agent` against the list above.

Without `--dry-run`, the same command runs the loop: the dispatcher invokes
one role, the role updates `HANDOFF.md`, the dispatcher validates the updated
handoff, and the loop continues, pauses, or escalates. Stopping it cleanly
and the operator prompt are described in
[README step 5](README.md#5-dry-run-then-run) and in
[Stopping The Dispatcher](CONFIGURATION.md#stopping-the-dispatcher).

## Inspecting State After A Cycle

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

With the default `pause_recovery: planner`, the planner gets one recovery
attempt at each of these pauses (except a `user` route) before the dispatcher
waits for you to edit `HANDOFF.md`; see
[Pause Recovery](CONFIGURATION.md#pause-recovery). When the dispatcher then
waits and you are at a terminal, the operator prompt lets you dispatch the
planner or another role, retry, quit, or edit `HANDOFF.md` to resume; see
[Operator Prompt](CONFIGURATION.md#operator-prompt).

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
