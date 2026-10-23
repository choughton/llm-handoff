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
