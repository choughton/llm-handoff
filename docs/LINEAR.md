# Linear Mode Setup

This guide is the ordered path from `init --tracker linear` to a first
dispatcher run in Linear mode. For what Linear mode changes, see
[Files Or Linear](../README.md#files-or-linear-where-work-is-tracked). For
every `tracker` key and validation rule, see
[Tracker (Optional Linear Mode)](../CONFIGURATION.md#tracker-optional-linear-mode).

`init` only writes files. It does not configure or authenticate any MCP
connector, does not run a provider CLI, and never contacts Linear. It does
not migrate issues and does not rewrite the live `HANDOFF.md`. The agents
reach Linear through their own provider CLI's Linear MCP connector; the
dispatcher holds no Linear credential.

After an applied (not `--dry-run`) `init --tracker linear`, `init` prints a
short `Linear setup` section that repeats steps 2 to 4 below with your MCP
server name and repository root.

## 1. Initialize Or Switch

Pick the issue pool: one Linear team, filtered by exactly one project or one
label.

```bash
llm-handoff init path/to/your-project --tracker linear --tracker-team ENG --tracker-project Platform
llm-handoff init path/to/your-project --tracker linear --tracker-team ENG --tracker-label repo-a
```

The Linear MCP server name defaults to `linear`; pass
`--tracker-mcp-server <name>` when your provider CLIs use another name. The
name must match the server name in every provider CLI's MCP configuration.

Supported providers. Linear mode runs roles on Claude (`claude`) and Codex
(`codex`). It does not support `antigravity` roles or fallbacks: `init`
refuses `--provider antigravity` with `--tracker linear`, and a config with
an `antigravity` role does not load in Linear mode.

On a repository that already has `dispatch_config.yaml`, the same command
switches its tracker mode instead. Run it between stories, with no
dispatcher running, and preview it with `--dry-run` first. The safety checks,
conflicts, and `--force` behavior are in
[Switching modes later](../README.md#switching-modes-later) and in the
"Switching an initialized repository" part of the
[Tracker section](../CONFIGURATION.md#tracker-optional-linear-mode).

## 2. Configure And Authenticate Each Provider CLI

Configure a Linear MCP server with the name from step 1 in every provider
CLI that runs a role or a fallback, then sign it in with that CLI's own MCP
authentication flow. Credentials stay in the provider CLI's configuration.
Never put a Linear token in `dispatch_config.yaml`, in a prompt file, or in
an example you commit.

Where the doctor looks for the server definition:

- Claude, strict mode (recommended). Set `tracker.claude_mcp_config` in
  `dispatch_config.yaml` to a JSON file whose top-level `mcpServers` defines
  only the Linear server. Claude launches then pass `--mcp-config <file>
  --strict-mcp-config` and load no other MCP server, so the server must be
  defined in that file.
- Claude, without `tracker.claude_mcp_config`. Claude roles load every MCP
  server in your Claude configuration, and the startup banner warns about
  it. The doctor reads `<repo>/.mcp.json`, then `~/.claude.json` (or
  `$CLAUDE_CONFIG_DIR/.claude.json` when that variable is set).
- Codex. `$CODEX_HOME/config.toml` (or `~/.codex/config.toml`), then
  `<repo>/.codex/config.toml`, with an `[mcp_servers.<name>]` table.

The exact rules are in the
[Tracker section](../CONFIGURATION.md#tracker-optional-linear-mode).

## 3. Run The Doctor

Change to the initialized repository root first, then run:

```bash
llm-handoff doctor
```

In Linear mode the doctor prints one `tracker connector` line per provider
launch group of your configured roles and fallbacks. Each line says whether
that CLI's MCP config defines the server. The doctor only reads those files:
it does not run a provider CLI or contact Linear, so a PASS does not prove
that the connector authenticates.

## 4. Optional: Probe Linear Access

From the same repository root, run:

```bash
llm-handoff doctor --probe-tracker ENG-123
```

Replace `ENG-123` with a Linear issue you can access. The probe launches the
first configured Claude role or fallback once, with only two Linear read
tools, to read that issue and the team's workflow states, and checks that
the configured state names exist. It is a limited read check:

- It needs at least one role or fallback with `provider: claude`. A
  Codex-only setup cannot run it; the probe line reports FAIL without
  launching anything.
- It checks one Claude launch only. It does not prove that any other
  provider CLI's connector authenticates, and it does not exercise the
  write tools the planner and auditor use.

Read the probe's residual risks (user-level Claude allow rules, and every
MCP server loading when `tracker.claude_mcp_config` is unset) in the
"Probing Linear access with `--probe-tracker`" part of the
[Tracker section](../CONFIGURATION.md#tracker-optional-linear-mode).

## 5. Start The Dispatcher

In Linear mode an empty or missing `docs/handoff/HANDOFF.md` is not an
error: the dispatcher gives the planner one cycle to pick an issue from the
pool and write the lease. After a switch to Linear, `init` leaves
`HANDOFF.md` as it was, so empty it before the first run. Then, from the
repository root:

```bash
llm-handoff --dry-run
llm-handoff
```

The dry run reports the route without launching a provider CLI. If a run
pauses, the dispatcher reports why and waits; recovery is up to you or a
configured agent.

## 6. Ask For New Work

When the pool has no eligible issue, the planner pauses and invites a
plain-language request at the operator prompt instead of asking you to
create issues by hand. Type the request; the planner answers with a
`## Proposed Backlog` in `HANDOFF.md`. Proposals and revisions change only
`HANDOFF.md`: the planner creates, updates, comments on, labels, leases, and
moves no issue on those turns. Type changes to revise the proposal, or press
Enter to approve it.

Only the dispatcher's own approval instruction, sent when you press Enter at
the proposal review, lets the planner create issues. It records the approved
epics in the project state file, then creates one issue per approved story
outline, in execution order, in the configured `tracker.team` with
`tracker.project` or `tracker.label`, in `tracker.eligible_state`. It uses
only identifiers the connector returns and creates no project, team, or
label. With `agents.tpm` configured, the planner then routes the epic to the
tpm without leasing; otherwise it leases the first issue by the usual
selection rules. With `commit_mode: dispatcher`, the dispatcher commits the
project state file alone.

This needs the planner's Linear MCP connector with its write tool
(`save_issue`), which the planner's Linear-mode grants already include; the
dispatcher never calls Linear. If a creation fails or its result is unknown,
the planner reports the issues the connector confirmed and the unresolved
ones, does not repeat the creation blindly, and pauses with one question.
The automated tests check the generated instructions and the dispatcher's
approval text; they do not run a live Linear connector. The Claude planner
agent files (`.claude/agents/planner.md`) do not carry these rules yet; a
later operator story synchronizes them.
