# llm-handoff

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

`llm-handoff` is a reference implementation of a file-based dispatch loop for
multi-CLI AI coding workflows. It runs one agent CLI at a time against a
single repository, passes work between them through one Markdown file, and
only advances when that file parses, routes, and validates.

> Prompts are advisory. Validators are authoritative.

Built for solo developers coordinating Codex, Gemini CLI, Claude Code, or
Antigravity on one repo without git worktrees, pull-request choreography, or a
service orchestrator.

![llm-handoff dispatch loop](docs/assets/llm-handoff-hero.png)

## Contents

- [Status](#status)
- [Quick Start](#quick-start)
- [Files Or Linear: Where Work Is Tracked](#files-or-linear-where-work-is-tracked)
- [Gotchas](#gotchas)
- [Gates And Scanners](#gates-and-scanners)
- [How It Works](#how-it-works)
- [Agent Safety Defaults](#agent-safety-defaults)
- [What This Is And Is Not](#what-this-is-and-is-not)
- [Why Not LangGraph, AutoGen, Or CrewAI?](#why-not-langgraph-autogen-or-crewai)
- [Known Limitations](#known-limitations)
- [Further Reading](#further-reading)
- [License](#license)

## Status

Pre-release. The dispatcher is being extracted from a project-specific
implementation into a public reference workflow, and names, configuration,
and examples can still change before the first tagged release. It is not a
production-certified tool for arbitrary repositories. See
[CHANGELOG.md](CHANGELOG.md).

## Quick Start

You need Python 3.11 or newer, Git, and at least one provider CLI signed in:
Claude Code (`claude`), Codex (`codex`), or Gemini CLI (`gemini`). Any role
mapped to Gemini needs Docker or Podman on `PATH` (macOS can use
`sandbox-exec`). The dispatcher launches these CLIs; it does not install
them or manage their logins.

Two directories are involved: the `llm-handoff` install, and the target
repository where agents do the work. Commands below run from anywhere once the
package is installed.

### 1. Install

```bash
python -m pip install git+https://github.com/choughton/llm-handoff.git
llm-handoff --help
```

Not on PyPI yet. To run from a source checkout instead, see
[INSTALL.md](INSTALL.md); `python -m llm_handoff ...` then replaces
`llm-handoff ...` in every command.

### 2. Initialize the target repository

Preview what will be copied, then copy it:

```bash
llm-handoff init path/to/your-project --dry-run
llm-handoff init path/to/your-project
```

This writes the reference workflow into the target repo: `AGENTS.md`,
`PROJECT_STATE.md`, `dispatch_config.yaml`, `docs/handoff/HANDOFF.md`, the
role prompts under `docs/handoff/`, and provider agent files under `.claude/`,
`.codex/`, `.gemini/`, and `.agents/`. Existing identical files are skipped;
changed files stop the run unless you pass `--force`.

`init` picks a role-to-provider mapping for you. When only one of Claude
Code, Codex, or Gemini CLI is on `PATH`, every role goes on that one CLI.
Otherwise it writes the reference mapping (Antigravity `agy` planner and
frontend, Codex backend, Claude auditor, validator, and finalizer). With
`--tracker linear`, the reference mapping puts `planner` and `frontend` on
Claude instead, because Linear mode does not support `antigravity` roles.
Pass `--provider claude`, `--provider codex`, or `--provider gemini` to force
a single-vendor setup. The command prints the mapping it chose.

With every role on Codex, `init` also writes `commit_mode: dispatcher` in
place of the template's `commit_mode: agent`. Codex's sandbox keeps `.git`
read-only, so a Codex implementer asked to commit its own work hands back
`blocked_implementation_failure` instead. In dispatcher mode the dispatcher
commits the in-scope files for it.

To track work in Linear instead of in files, add the tracker flags here. See
[Files Or Linear](#files-or-linear-where-work-is-tracked) first, because this
choice is hard to change later.

### 3. Run the preflight check

```bash
llm-handoff doctor --repo-root path/to/your-project
```

`doctor` checks Git, each configured role's CLI binary, the Gemini sandbox
runtime and security extension, the gate and reviewer settings, configured
scanners, interpreter grants, and whether the handoff file routes. It exits `1` on any error. Fix every `FAIL` before
continuing; read the `WARN` lines too.

### 4. Tell the planner what to build

In the target repo, open `docs/handoff/HANDOFF.md`. The starter file already
routes to `planner`. Replace the body with what you want built, in enough
detail for a planner to scope a first bounded story:

```markdown
---
next_agent: planner
reason: Scope the first bounded implementation task.
producer: user
---

# Initial Handoff

Read AGENTS.md and PROJECT_STATE.md. The goal of this epic is <what you
want>. Scope the first story and write a Work Packet for it.
```

Also fill in `PROJECT_STATE.md`. It is the durable summary the planner reads
at the start of every cycle and the finalizer updates when an epic closes.

### 5. Dry run, then run

```bash
llm-handoff --repo-root path/to/your-project --dry-run
```

The dry run reports the route and the provider CLI it would launch without
launching anything. When it is clean:

```bash
llm-handoff --repo-root path/to/your-project
```

The loop now reads the handoff, runs one role, validates the rewritten
handoff, and continues, pauses, or escalates to you. Watch
`docs/handoff/HANDOFF.md`, `git log`, and `logs/dispatch/` in the target repo.

To stop cleanly, run `llm-handoff stop --repo-root path/to/your-project` from
another terminal. The running agent finishes, its work is committed and
checked as usual, and the dispatcher exits before the next dispatch. Ctrl+C
kills the running agent immediately.

When the loop pauses and you are at a terminal, an operator prompt lets you
press Enter to send the planner back in, type `@backend`, `@frontend`, or
`@auditor` with a message to dispatch that role directly, or type `retry` or
`quit`. Editing `HANDOFF.md` by hand also resumes it.

For the same path with per-platform commands, see [QUICKSTART.md](QUICKSTART.md).
For the slower explanation, see [WALKTHROUGH.md](WALKTHROUGH.md).

## Files Or Linear: Where Work Is Tracked

`llm-handoff` has two modes for where the backlog lives. `HANDOFF.md` is the
routing state in both. What differs is where the planner finds the next piece
of work and where evidence is recorded.

| | File mode (default) | Linear mode |
| --- | --- | --- |
| Backlog | Epics and stories the planner writes into `HANDOFF.md` frontmatter (`epic_id`, `story_id`, `remaining_stories`), seeded by what you describe in the handoff body and `PROJECT_STATE.md`. | A pool of Linear issues: one team, filtered by one project or one label, in a configured eligible state. |
| Who picks the next story | The planner, from its own plan. | The planner, by Linear priority, then manual order, then creation date. |
| Durable record | Git commits plus the ledger in `PROJECT_STATE.md`. | Git commits plus YAML evidence comments on the issue and issue-state changes. |
| Who talks to Linear | Nobody. | The agents, through their own CLI's Linear MCP connector. The dispatcher never calls Linear and holds no Linear credential. |
| Extra requirements | None. | `enforce_work_packet: true` (the default), a Linear MCP server configured in each provider CLI you use, and Linear-mode prompt text in `AGENTS.md` and `HANDBOOK.md`. |

### Starting in Linear mode

Add the tracker flags to `init`. It only works on a repository that has no
`dispatch_config.yaml` yet:

```bash
llm-handoff init path/to/your-project --tracker linear --tracker-team ENG --tracker-project Platform
```

Give exactly one of `--tracker-project` and `--tracker-label`. The Linear MCP
server name defaults to `linear`; change it with `--tracker-mcp-server`. This
writes a `tracker` block into `dispatch_config.yaml` and renders the Linear
prompt sections into `AGENTS.md` and `docs/handoff/HANDBOOK.md` between
`<!-- llm-handoff:linear:begin -->` and `<!-- llm-handoff:linear:end -->`
markers.

Then check the connectors. `doctor` prints one `tracker connector` line per
provider CLI, saying whether that CLI's MCP config defines the server. It
does not prove the connector authenticates; for that, run the opt-in probe
against a real issue:

```bash
llm-handoff doctor --repo-root path/to/your-project --probe-tracker ENG-123
```

Set `tracker.claude_mcp_config` to a file that defines only the Linear
server. Without it, Claude roles load every MCP server in your Claude
configuration, and the startup banner warns about it.

In Linear mode an empty or missing `HANDOFF.md` is not an error. The
dispatcher gives the planner one cycle to pick an issue from the pool and
write the lease.

### Switching modes later

`init` does not switch an initialized repository. Switching is a manual
config edit, done between stories, when `HANDOFF.md` has no active lease:

- **Linear to files.** In `dispatch_config.yaml`, set `tracker.provider:
  none` and remove every other `tracker.*` key; a half-configured block is
  rejected at load. The Linear prompt sections say to ignore themselves when
  the provider is not `linear`, so the marked blocks can stay. Write a fresh
  planner handoff describing the next epic.
- **Files to Linear.** Add a `tracker` block to `dispatch_config.yaml`
  (copy the shape from [dispatch_config.example.yaml](dispatch_config.example.yaml)).
  Render the prompt text by running `init --tracker linear ...` into an empty
  scratch directory and copying the marked blocks from its `AGENTS.md` and
  `docs/handoff/HANDBOOK.md` into yours. Then empty `HANDOFF.md` and start
  the dispatcher; the planner rebuilds the lease from the pool.

Run `doctor` after either change. The full key reference and validation rules
are in [Tracker (Optional Linear Mode)](CONFIGURATION.md#tracker-optional-linear-mode).

## Gotchas

Things that stop a first run or quietly change what the loop does.

- **Gemini roles need a container runtime.** Gemini CLI runs in its sandbox,
  so any role on `gemini` needs Docker or Podman on `PATH` (macOS:
  `sandbox-exec`). Without one the dispatcher refuses to start. An
  all-Gemini setup (`init --provider gemini`) puts every role in that
  sandbox and needs the Gemini CLI security extension for the security
  reviewer; the next-agent normalizer runs on the same Gemini CLI sign-in,
  with no API key required.
- **All-Codex setups need dispatcher commits.** Codex's `workspace-write`
  sandbox cannot write to `.git`, so `init --provider codex` writes
  `commit_mode: dispatcher` for you. Keep it, and keep the work-claim gate
  on (the default), which dispatcher commits require. With
  `commit_mode: agent` every implementer handback comes back as
  `blocked_implementation_failure`. See
  [Dispatcher Commits](CONFIGURATION.md#dispatcher-commits).
- **Two reviewers run on every audit by default.** A read-only security
  reviewer runs beside the quality auditor. It uses the first installed CLI
  that differs from the implementer's vendor. With a single vendor it still
  runs, with a warning that the review is not independent. A Gemini reviewer
  additionally needs the Gemini CLI security extension. Opting out takes
  `audit.reviewers: [quality]` plus a written `audit.security_opt_out_reason`.
- **The planner must write a Work Packet.** With the work-claim gate on (the
  default), a handoff that routes work to `backend` or `frontend` without a
  parseable `## Work Packet` fails validation. The shipped planner prompts ask
  for one. See [Gates And Scanners](#gates-and-scanners).
- **The Verification command is agent-written and runs with your
  permissions.** Set `verification_allowed_prefixes` (for example
  `["python -m pytest"]`) so the gate only runs commands you expect.
- **Scanners are off until you list them,** and `required: true` is the
  default, so a listed scanner that is not installed fails every claim it
  applies to. See [Gates And Scanners](#gates-and-scanners).
- **Codex with an API key needs a flag.** Codex roles authenticated with
  `OPENAI_API_KEY` instead of `codex login` need `use_api_key_env: true`. By
  default agents get an allowlisted environment, not your shell.
- **Editing control files stops the loop.** A role that changes `AGENTS.md`,
  the handoff prompts, `dispatch_config.yaml`, or anything under `.claude/`,
  `.codex/`, `.gemini/`, or `.agents/` without listing the path in its Work
  Packet routes the handoff to `user`.
- **Gemini guardrails do not apply until you copy them.** Gemini CLI ignores
  a project's `.gemini/policies/`. Copy `planner_guardrails.toml` to
  `~/.gemini/policies/` to enforce its deny rules.
- **Nothing pushes by default.** `publish.mode: local` commits locally.
  `merge-commit` and `pr` modes are explicit opt-ins; `pr` needs
  `commit_mode: dispatcher` and the `gh` CLI. See
  [Publishing](CONFIGURATION.md#publishing-story-branches-and-pull-requests).
- **Windows.** Run the dispatcher inside WSL2 or a container with no push
  credential. Claude Code's Bash sandbox needs WSL2, and Codex's native
  Windows sandbox is experimental. See the
  [threat model](SECURITY.md#threat-model).
- **Pauses are not failures.** With the default `pause_recovery: planner`,
  the planner gets one recovery attempt per pause before the dispatcher waits
  for you. Three bounces of the same claim trip a circuit breaker. See
  [Pause Recovery](CONFIGURATION.md#pause-recovery).

## Gates And Scanners

Three checks sit between an implementer's "done" and the auditor. Two are on
by default; scanners are opt-in and run inside the work-claim gate, so they
need it on.

| Gate | Default | What it checks | Details |
| --- | --- | --- | --- |
| Work-claim gate | on (`enforce_work_packet: true`) | For a `ready_for_review` handoff from `backend` or `frontend`: `scope_sha` is HEAD, the diff stays inside the Work Packet's `Files in scope`, the tree is clean, and the Verification command exits 0. | [Work-Claim Gate](CONFIGURATION.md#work-claim-gate) |
| Scanners | off (`scanners: []`) | Runs operator-listed tools such as gitleaks, semgrep, bandit, or pip-audit on the claimed diff, after the scope checks pass and before the Verification command. | [Scanners](CONFIGURATION.md#scanners) |
| Security reviewer | on (`audit.reviewers: [quality, security]`) | A read-only security review on the same commit range as the quality audit; findings at or above `audit.block_severity` fail the claim. | [Audit Fan-Out](CONFIGURATION.md#audit-fan-out) |

Turning off either default needs a written reason
(`work_packet_opt_out_reason`, `audit.security_opt_out_reason`), which every
dispatcher start and `doctor` report as a warning.

A scanner configuration that adds secret scanning and a pinned semgrep
ruleset:

```yaml
enforce_work_packet: true
scanners:
  - name: gitleaks
    command: gitleaks git --no-banner --redact --log-opts="{diff_range}" {repo_root}
    required: true
  - name: semgrep
    command: semgrep scan --config .semgrep/rules.yml --metrics off --error --quiet {changed_files}
    required: true
  - name: bandit
    command: bandit -q {changed_py_files}
    required: false
```

Scanner rules of thumb:

- `llm-handoff` installs nothing. Each tool must be on `PATH` in the target
  repo's environment. `doctor` prints a line per configured scanner saying
  whether the gate will run it.
- Use semgrep only with a ruleset committed to the repository. `--config
  auto` downloads rules at run time, so results can change without a commit.
- Scope gitleaks, semgrep, and bandit to the diff with `{diff_range}`,
  `{changed_files}`, or `{changed_py_files}`. A scanner that uses a changed-
  files placeholder is skipped when that list is empty. Run dependency audits
  such as pip-audit unscoped.
- Scanners get the same allowlisted environment as the Verification command.
  A token such as `SEMGREP_APP_TOKEN` or a proxy setting must be listed under
  `work_claim_env_passthrough`.
- A failed scanner bounces the claim to the implementer as `verified_fail`
  with the output tail. Every scanner runs even after one fails, so a single
  bounce reports every finding.

## How It Works

`llm-handoff` treats model output as untrusted input.

1. The dispatcher reads `HANDOFF.md`. YAML frontmatter declares `next_agent`.
2. A deterministic router checks the role. A non-canonical value may go to a
   constrained model-backed normalizer, which rewrites obvious values or
   returns `unknown`.
3. The configured provider CLI runs that one role with the role's prompt.
4. The rewritten handoff is validated: frontmatter, status enum, evidence
   block, and the route the status implies. A completion claim also passes
   the work-claim gate before the auditor is dispatched.
5. The loop continues, pauses, or escalates to `user`.

```text
agent writes HANDOFF.md
        |
        v
dispatcher reads state
        |
        v
router selects next_agent
        |
        v
normalizer resolves non-canonical values
        |
        v
provider CLI runs one role
        |
        v
agent updates HANDOFF.md
        |
        v
validator accepts, pauses, or escalates
```

The roles are generic: `planner`, `backend`, `frontend`, `auditor`,
`validator`, `finalizer`, and `user`. Which CLI runs each role is a config
mapping, not part of the protocol. A typical story goes planner (writes the
Work Packet) to backend or frontend (implements, claims `ready_for_review`)
to auditor (passes or fails) and back to planner for the next story; the
finalizer runs only when the auditor closes an epic.

The handoff file is the mutex and the debugger. There is no hidden queue,
database, or dashboard required to understand the current state. Git commit
SHAs are the durable record of completed work.

For long unattended runs, `python -m llm_handoff.supervisor` wraps the same
dispatcher with heartbeats, a run-state file, incident bundles, and an
optional bounded repair role. See
[docs/SUPERVISED_DISPATCH.md](docs/SUPERVISED_DISPATCH.md).

## Agent Safety Defaults

Provider CLIs launch with these protections on by default:

- **Allowlisted environment.** Agents get process basics and their own
  provider's variables, not a copy of the dispatcher's shell. `GITHUB_TOKEN`,
  cloud credentials, and other vendors' API keys stay out. See
  [Agent Environment](CONFIGURATION.md#agent-environment).
- **Permission checks and sandboxes on.** Claude Code runs in `dontAsk` mode
  with a tool allowlist, Codex in its `workspace-write` sandbox, and Gemini
  CLI in its container sandbox. See
  [Launch Permissions And Sandboxing](CONFIGURATION.md#launch-permissions-and-sandboxing).
- **Protected control files fail closed.** See the gotcha above and
  [Protected Control Files](CONFIGURATION.md#protected-control-files).
- **Shipped guardrails deny pushes and destructive commands.** The template's
  `.claude/settings.json` and its `PreToolUse` hook block `git push`, `curl`,
  `wget`, `rm -rf`, and `git reset --hard` in every permission mode. The
  Codex rules file forbids `git push`. The Gemini policy file does the same
  once copied to your user policies.

Each protection has a per-role or repo-wide opt-out:

```yaml
protected_paths: []      # stop checking control files
agents:
  planner:
    unsafe_bypass: true  # this role runs with approvals and sandbox off
    env_mode: inherit    # this role gets the dispatcher's full environment
```

Each opt-out removes a protection. Use one only to debug a specific role.
These rules match command prefixes; they catch mistakes and simple prompt
injection, not an agent that writes a script to do the same thing.

Role allowlists are a guardrail, not a sandbox: any interpreter grant can run
any command, `git push` included. The default allowlists therefore hold no
`python`, `python3`, or `pytest` grant; the dispatcher's work-claim gate runs
the Verification command itself. `interpreter_grants: true` puts those grants
back for roles that need them, and lets those agents run any command past the
allowlist (see
[Interpreter Grants](CONFIGURATION.md#interpreter-grants)). Protect the remote
with server-side branch protection, meaning a ruleset that requires pull
requests and has no bypass actors. The dispatcher does not configure it.

## What This Is And Is Not

It is:

- A Python CLI for serially dispatching work between AI coding CLIs.
- A file-based operating protocol centered on `HANDOFF.md`.
- A deterministic router with validator-backed failure handling.
- A deterministic work-claim gate with optional operator-configured scanners.
- A security reviewer that runs beside the quality auditor.
- A reference workflow that you can inspect, copy, fork, or adapt.
- A `doctor` preflight and an opt-in supervised wrapper.

It is not:

- A general multi-agent framework.
- Parallel execution across branches or worktrees.
- A hosted service or background daemon.
- A replacement for Codex, Gemini, Claude, or any other agent CLI.
- Self-healing. It detects, validates, and pauses. Humans or configured
  agents perform recovery.
- Production-certified for arbitrary repositories.

## Why Not LangGraph, AutoGen, Or CrewAI?

Those projects are broader orchestration frameworks. `llm-handoff` is
narrower: one repo, one branch, one handoff file, one agent at a time. The
differentiator is not a graph runtime. It is the inversion that prompts are
advisory while validators are authoritative. The router can propose, but the
loop only advances when the handoff state parses, routes, and validates.

## Known Limitations

- The reference implementation has been validated primarily in one workflow.
- Provider CLI behavior can change underneath the dispatcher. The default
  permission and sandbox flags follow each CLI's documentation but have not
  been run against every CLI version and platform.
- Validator calls optimize for correctness over token cost.
- Registered runtime adapters are `codex`, `gemini`, `claude`, and
  `antigravity`. Provider-specific prompt files and local auth are the
  user's responsibility.
- Switching an initialized repository between file mode and Linear mode is
  a manual edit; `init` does not do it.

## Further Reading

| Document | Read it when |
| --- | --- |
| [QUICKSTART.md](QUICKSTART.md) | You want the quick start above with Windows and POSIX commands spelled out. |
| [WALKTHROUGH.md](WALKTHROUGH.md) | You want the two-repo model, what `init` copies, and common pauses explained. |
| [INSTALL.md](INSTALL.md) | You are running from a source checkout or debugging the install. |
| [CONFIGURATION.md](CONFIGURATION.md) | You are editing `dispatch_config.yaml`. Every supported key is here. |
| [docs/INVENTORY.md](docs/INVENTORY.md) | You want the index of every doc, prompt file, agent template, and fixture in this repo. |
| [docs/handoff/README.md](docs/handoff/README.md) | You are writing or debugging a handoff: frontmatter, status enum, Work Packet format. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | You want the module map and design constraints. |
| [docs/SUPERVISED_DISPATCH.md](docs/SUPERVISED_DISPATCH.md) | You want heartbeats and incident bundles for long runs. |
| [SECURITY.md](SECURITY.md) | You want the threat model before pointing agents at a real repo. |
| [docs/TESTING.md](docs/TESTING.md) | You are contributing. Also see [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). |

## License

Apache License 2.0. See [LICENSE](LICENSE).
