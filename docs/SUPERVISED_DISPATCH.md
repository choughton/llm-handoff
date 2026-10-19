# Supervised Dispatch

Supervised dispatch is an optional operator wrapper around the normal
`llm-handoff` loop. It keeps the default file-based protocol unchanged while
adding runtime controls that are useful for long local runs:

- a separate supervisor lock;
- periodic and boundary heartbeat messages;
- a small current-run state file;
- incident bundles when the child dispatcher stops;
- optional notification hooks;
- an optional bounded repair role.

The repair role is off by default. When enabled, it is a configured dispatcher
role that receives a failure packet and a narrow repair prompt. It is not a
background service, and it is not a general self-healing framework.

## Entry Points

Windows PowerShell:

```powershell
.\scripts\dispatch-supervised.ps1 -- --dry-run
```

Windows Command Prompt:

```cmd
scripts\dispatch-supervised.cmd -- --dry-run
```

Source checkout:

```bash
python -m llm_handoff.supervisor -- --dry-run
```

The Windows wrappers look for `venv`, then `.venv`, then `python`. Set
`LLM_HANDOFF_PYTHON` to an explicit interpreter path if your checkout uses a
different environment name:

```powershell
$env:LLM_HANDOFF_PYTHON = "C:\path\to\python.exe"
.\scripts\dispatch-supervised.ps1 --doctor
```

Arguments before `--` configure the supervisor. Arguments after `--` pass
through to the normal dispatcher. `--repo-root` and `--config` are shared:
wherever you place them, the supervisor resolves them once and passes the same
values to the child dispatcher and to the optional repair role, and it refuses
to start if the two sides disagree. For example, this forwards `--config` and
`--dry-run` to `python -m llm_handoff`:

```powershell
.\scripts\dispatch-supervised.ps1 --repair-role validator -- --dry-run --config dispatch_config.yaml
```

The wrapper also passes through child help:

```powershell
.\scripts\dispatch-supervised.ps1 -- --help
```

## Doctor Mode

Use `--doctor` to run supervisor preflight checks without starting the child
dispatcher:

```powershell
.\scripts\dispatch-supervised.ps1 --doctor
```

Doctor mode checks the target repo, handoff route, handoff validation, Git
availability, and stale supervisor lock state. It writes `current-run.json`
with a `doctor_passed` or `doctor_failed` status.

## Operator Info Block

At startup, the supervisor prints a dedicated info block before the child
dispatcher output. The block includes:

```text
[SUPERVISOR] [INFO] ================================================
[SUPERVISOR] [INFO]   llm-handoff supervised dispatcher starting
[SUPERVISOR] [INFO]   Operator wrapper, optional bounded repair role
[SUPERVISOR] [INFO] ================================================
[SUPERVISOR] [INFO] Repo root:          C:\path\to\repo
[SUPERVISOR] [INFO] Dispatcher args:    --dry-run
[SUPERVISOR] [INFO] Repair role:        (disabled)
[SUPERVISOR] [INFO] Repair budget:      0 attempts (repair disabled)
[SUPERVISOR] [INFO] Transient retries:  1
[SUPERVISOR] [INFO] Failure tail lines: 60
[SUPERVISOR] [INFO] Stall timeout:      1800s
[SUPERVISOR] [INFO] Heartbeat:          300s / boundary
[SUPERVISOR] [INFO] Failure packets:    C:\path\to\repo\logs\dispatch\supervisor
[SUPERVISOR] [INFO] Incident bundles:   C:\path\to\repo\logs\dispatch\supervisor\incidents
[SUPERVISOR] [INFO] Summary log:        C:\path\to\repo\logs\dispatch\supervisor\supervisor-summary.jsonl
[SUPERVISOR] [INFO] Run state:          C:\path\to\repo\logs\dispatch\supervisor\current-run.json
[SUPERVISOR] [INFO] Instance lock:      C:\path\to\repo\logs\dispatch\supervisor\dispatch-supervised.lock
```

This block is separate from the child dispatcher's logs so operators can tell
which process owns each message.

## Heartbeats

The supervisor emits heartbeat lines every 300 seconds by default and whenever
the child output crosses a recognized boundary such as a new cycle or route.
This is meant to show that the wrapper is still alive during quiet provider CLI
periods.

Configure the interval:

```powershell
.\scripts\dispatch-supervised.ps1 --heartbeat-interval-seconds 300 -- --config dispatch_config.yaml
```

Set the value to `0` to disable interval heartbeats. Boundary heartbeats still
emit when the child logs recognized cycle or route changes.

## Stalled Output Timeout

The supervisor kills the child dispatcher if no child output arrives before the
stalled-output timeout. The default is 1800 seconds.

```powershell
.\scripts\dispatch-supervised.ps1 --stalled-output-timeout-seconds 900 -- --config dispatch_config.yaml
```

A stalled child produces an incident bundle and a stopped run state.

## Run State And Locks

Supervisor state is written under:

```text
logs/dispatch/supervisor/
```

Important files:

- `current-run.json`: latest supervisor state, route, child pid, repair role,
  commit SHA, handoff SHA, and dirty files.
- `supervisor-summary.jsonl`: append-only event summary.
- `dispatch-supervised.lock`: supervisor instance lock.
- `incidents/`: failure bundles for human triage.

The supervisor lock is separate from the child dispatcher's `.dispatch.lock`.
The supervisor lock prevents two supervisor wrappers from managing the same
target repo at the same time. The child dispatcher still owns its normal
dispatch lock while it runs.

If a lock remains after a crash, run `--doctor` first. Delete
`dispatch-supervised.lock` only after confirming the recorded process is no
longer running.

## Incident Bundles

When the child dispatcher stops and no retry or repair path can continue, the
supervisor writes an incident directory such as:

```text
logs/dispatch/supervisor/incidents/incident-20260503T120000Z-abc123def456-1a2b3c4d/
```

The bundle includes:

- `summary.json`
- `output-tail.log`
- `handoff-current.md`
- `git-status.txt`
- `failure-packet.json`, when a repair packet was created
- `changed-files.txt`, when a repair role touched files outside the allowed
  repair scope

The bundle is intended for human triage or for a manually selected follow-up
agent. It does not replace reading `HANDOFF.md` and Git history.

## Optional Repair Role

Repair is off by default:

```powershell
.\scripts\dispatch-supervised.ps1 -- --config dispatch_config.yaml
```

Enable one bounded repair attempt by naming a configured role:

```powershell
.\scripts\dispatch-supervised.ps1 --repair-role validator -- --config dispatch_config.yaml
```

`--repair-role` uses the existing `agents.<role>` entry in
`dispatch_config.yaml`. If `--max-repair-attempts` is omitted, setting
`--repair-role` implies one attempt. You can set the budget explicitly:

```powershell
.\scripts\dispatch-supervised.ps1 --repair-role validator --max-repair-attempts 1 -- --config dispatch_config.yaml
```

The supervisor writes a failure packet, invokes the configured repair role with
a repair-only prompt, then requires the repair to create a new HEAD commit. If
HEAD does not change, the repair is treated as failed. The committed diff and
any new uncommitted files must stay inside the allowed repair scope, or the
supervisor stops with an incident instead of restarting; dispatcher runtime
state under `logs/dispatch/` is exempt from that check. After a successful
scoped repair commit, the supervisor restarts the child dispatcher.

The default allowed repair scope is deliberately narrow:

- `docs/`
- `.claude/agents/`
- `.codex/skills/llm-handoff/`
- `.agents/agents/`

The default no longer includes dispatcher source, tests, or top-level docs,
because in a target repository those paths hold that project's own code.

Use `--repair-allow-path PATH` only when the target workflow has an additional
known-safe repair surface. A repository that develops the dispatcher itself
can restore the wider surface, for example:

```powershell
.\scripts\dispatch-supervised.ps1 --repair-role validator --repair-allow-path llm_handoff/ --repair-allow-path tests/ -- --config dispatch_config.yaml
```

## Failure Classification

The supervisor classifies child failures conservatively:

- auth failures stop immediately;
- specific transient failures such as `network error` or `network unreachable`
  get a retry budget;
- repeated identical failure fingerprints stop;
- repairable dispatch failures can invoke the optional repair role;
- ambiguous cases stop with an incident bundle when repair is unavailable.

Markers are intentionally specific. For example, a help page mentioning
`auth login` should not be treated as an auth failure unless it asks the user
to run an auth-login command to authenticate.

## Notifications

Use `--notify-bell` for a terminal bell on stop or repair restart:

```powershell
.\scripts\dispatch-supervised.ps1 --notify-bell -- --config dispatch_config.yaml
```

Use `--notify-command` to run a local shell command on important supervisor
events:

```powershell
.\scripts\dispatch-supervised.ps1 --notify-command "echo supervisor event" -- --config dispatch_config.yaml
```

The notify command runs through the local shell with these environment
variables:

- `LLM_HANDOFF_SUPERVISOR_EVENT`
- `LLM_HANDOFF_SUPERVISOR_MESSAGE`
- `LLM_HANDOFF_SUPERVISOR_REPO_ROOT`

Because the command runs through the shell, treat it as an operator-controlled
local hook. Do not populate it from untrusted input.

## When To Use It

Use supervised dispatch when you want a long local run to leave better evidence
before stopping. Use the normal dispatcher when you want the smallest runtime
surface.

Good uses:

- unattended dry runs or local execution windows;
- collecting incident bundles for later debugging;
- retrying obvious transient provider failures;
- allowing a configured validator-style role to repair handoff or dispatcher
  protocol issues once.

Poor uses:

- arbitrary product implementation recovery;
- parallel branch orchestration;
- replacing code review or human judgment;
- running as a service daemon.
